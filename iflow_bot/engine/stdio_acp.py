"""Stdio-based ACP Connector for iflow CLI.

实现 ACP 协议连接器，直接通过 stdio 与 iflow 通信。
不需要启动 WebSocket 服务器，使用 iflow --experimental-acp 模式。

核心方法：
- initialize: 初始化连接，协商协议版本和能力
- session/create: 创建新会话
- session/prompt: 发送消息
- session/update: 接收更新通知
- session/cancel: 取消当前请求

消息类型：
- agent_message_chunk: Agent 响应块
- agent_thought_chunk: Agent 思考过程
- tool_call/tool_call_update: 工具调用
- stop_reason: 任务完成
"""

from __future__ import annotations

import asyncio
import json
import platform
import uuid
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Callable, Coroutine, Optional

from loguru import logger


def _is_windows() -> bool:
    """检查是否为 Windows 平台。"""
    return platform.system().lower() == "windows"


class StdioACPError(Exception):
    """Stdio ACP 连接器错误基类。"""
    pass


class StdioACPConnectionError(StdioACPError):
    """Stdio ACP 连接错误。"""
    pass


class StdioACPTimeoutError(StdioACPError):
    """Stdio ACP 超时错误。"""
    pass


class StopReason(str, Enum):
    """任务结束原因。"""
    END_TURN = "end_turn"
    MAX_TOKENS = "max_tokens"
    REFUSAL = "refusal"
    CANCELLED = "cancelled"
    ERROR = "error"


@dataclass
class AgentMessageChunk:
    """Agent 消息块。"""
    text: str = ""
    is_thought: bool = False


@dataclass
class ToolCall:
    """工具调用信息。"""
    tool_call_id: str
    tool_name: str
    status: str = "pending"
    args: dict = field(default_factory=dict)
    output: str = ""


@dataclass
class ACPResponse:
    """ACP 响应结果。"""
    content: str = ""
    thought: str = ""
    tool_calls: list[ToolCall] = field(default_factory=list)
    stop_reason: Optional[StopReason] = None
    error: Optional[str] = None


class StdioACPClient:
    """
    Stdio ACP 协议客户端 - 与 iflow CLI 通过 stdio 通信。
    
    使用 subprocess 启动 iflow --experimental-acp，
    通过 stdin/stdout 进行 JSON-RPC 通信。
    
    Example:
        client = StdioACPClient(iflow_path="iflow", workspace="/path/to/workspace")
        await client.start()
        await client.initialize()
        session_id = await client.create_session(workspace="/path/to/workspace")
        response = await client.prompt(session_id, "Hello!")
        print(response.content)
    """
    
    PROTOCOL_VERSION = 1
    
    def __init__(
        self,
        iflow_path: str = "iflow",
        workspace: Optional[Path] = None,
        timeout: int = 300,
    ):
        self.iflow_path = iflow_path
        self.workspace = workspace or Path.cwd()
        self.timeout = timeout
        
        self._process: Optional[asyncio.subprocess.Process] = None
        self._started = False
        self._initialized = False
        self._request_id = 0
        self._pending_requests: dict[int, asyncio.Future] = {}
        self._receive_task: Optional[asyncio.Task] = None
        self._message_queue: asyncio.Queue[dict] = asyncio.Queue()
        
        self._agent_capabilities: dict = {}
        
        logger.info(f"StdioACPClient initialized: {iflow_path}, workspace={workspace}")
    
    async def start(self) -> None:
        """启动 iflow 进程。"""
        if self._started:
            return
        
        try:
            if _is_windows():
                # Windows 上使用 shell 启动 iflow 命令，确保 .CMD 文件能被正确执行
                cmd = f'"{self.iflow_path}" --experimental-acp --stream'
                self._process = await asyncio.create_subprocess_shell(
                    cmd,
                    stdin=asyncio.subprocess.PIPE,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                    cwd=str(self.workspace),
                )
            else:
                # Unix 系统使用 exec 方式
                self._process = await asyncio.create_subprocess_exec(
                    self.iflow_path,
                    "--experimental-acp",
                    "--stream",
                    stdin=asyncio.subprocess.PIPE,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                    cwd=str(self.workspace),
                )
            
            self._started = True
            
            self._receive_task = asyncio.create_task(self._receive_loop())
            
            logger.info(f"StdioACP started: pid={self._process.pid}")
            
            await asyncio.sleep(2)
            
        except Exception as e:
            raise StdioACPConnectionError(f"Failed to start iflow process: {e}")
    
    async def stop(self) -> None:
        """停止 iflow 进程。"""
        if self._receive_task:
            self._receive_task.cancel()
            try:
                await self._receive_task
            except asyncio.CancelledError:
                pass
            self._receive_task = None
        
        if self._process:
            try:
                self._process.terminate()
                await asyncio.wait_for(self._process.wait(), timeout=5)
            except Exception:
                try:
                    self._process.kill()
                except Exception:
                    pass
            self._process = None
        
        self._started = False
        self._initialized = False
        logger.info("StdioACP stopped")
    
    async def _receive_loop(self) -> None:
        """消息接收循环。"""
        while self._started and self._process and self._process.stdout:
            try:
                line = await asyncio.wait_for(
                    self._process.stdout.readline(),
                    timeout=1.0
                )
                
                if not line:
                    break
                
                raw = line.decode("utf-8", errors="replace").strip()
                
                if not raw:
                    continue
                
                if not raw.startswith("{"):
                    logger.debug(f"StdioACP non-JSON: {raw[:100]}")
                    continue
                
                try:
                    message = json.loads(raw)
                except json.JSONDecodeError as e:
                    logger.debug(f"StdioACP JSON decode error: {e}, raw={raw[:100]}")
                    continue
                
                if "id" in message:
                    request_id = message["id"]
                    if request_id in self._pending_requests:
                        future = self._pending_requests.pop(request_id)
                        if not future.done():
                            future.set_result(message)
                else:
                    await self._message_queue.put(message)
                    
            except asyncio.TimeoutError:
                continue
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"StdioACP receive error: {e}")
                break
        
        logger.debug("StdioACP receive loop ended")
    
    def _next_request_id(self) -> int:
        self._request_id += 1
        return self._request_id
    
    async def _send_request(
        self,
        method: str,
        params: dict,
        timeout: Optional[int] = None,
    ) -> dict:
        """发送 JSON-RPC 请求并等待响应。"""
        if not self._started or not self._process:
            raise StdioACPConnectionError("ACP process not started")
        
        request_id = self._next_request_id()
        
        request = {
            "jsonrpc": "2.0",
            "id": request_id,
            "method": method,
            "params": params,
        }
        
        future: asyncio.Future[dict] = asyncio.get_event_loop().create_future()
        self._pending_requests[request_id] = future
        
        try:
            request_str = json.dumps(request) + "\n"
            self._process.stdin.write(request_str.encode())
            await self._process.stdin.drain()
            logger.debug(f"StdioACP request: {method} (id={request_id})")
            
            timeout = timeout or self.timeout
            response = await asyncio.wait_for(future, timeout=timeout)
            
            if "error" in response:
                error = response["error"]
                raise StdioACPError(f"ACP error: {error.get('message', str(error))}")
            
            return response.get("result", {})
            
        except asyncio.TimeoutError:
            self._pending_requests.pop(request_id, None)
            raise StdioACPTimeoutError(f"ACP request timeout: {method}")
    
    async def initialize(self) -> dict:
        """初始化 ACP 连接。"""
        if self._initialized:
            return self._agent_capabilities
        
        client_capabilities = {
            "fs": {
                "readTextFile": True,
                "writeTextFile": True,
            }
        }
        
        result = await self._send_request("initialize", {
            "protocolVersion": self.PROTOCOL_VERSION,
            "clientCapabilities": client_capabilities,
        })
        
        self._agent_capabilities = result.get("agentCapabilities", {})
        self._initialized = True
        
        logger.info(f"StdioACP initialized: version={result.get('protocolVersion')}")
        
        return self._agent_capabilities
    
    async def authenticate(self, method_id: str = "iflow") -> bool:
        """进行认证。"""
        if not self._initialized:
            await self.initialize()
        
        try:
            result = await self._send_request("authenticate", {
                "methodId": method_id,
            })
            success = result.get("methodId") == method_id
            if success:
                logger.info(f"StdioACP authenticated with method: {method_id}")
            return success
        except StdioACPError as e:
            logger.error(f"StdioACP authentication failed: {e}")
            return False
    
    async def create_session(
        self,
        workspace: Optional[Path] = None,
        model: Optional[str] = None,
        system_prompt: Optional[str] = None,
        approval_mode: str = "yolo",
    ) -> str:
        """创建新会话。"""
        if not self._initialized:
            await self.initialize()
        
        ws_path = str(workspace or self.workspace)
        
        params: dict = {
            "cwd": ws_path,
            "mcpServers": [],
        }
        
        settings: dict = {}
        if approval_mode:
            settings["permission_mode"] = approval_mode
        if model:
            settings["model"] = model
        if system_prompt:
            settings["system_prompt"] = system_prompt
        
        if settings:
            params["settings"] = settings
        
        result = await self._send_request("session/new", params)
        session_id = result.get("sessionId", "")
        
        if model:
            try:
                await self._send_request("session/set_model", {
                    "sessionId": session_id,
                    "modelId": model,
                }, timeout=10)
                logger.debug(f"Set model to {model} for session")
            except Exception as e:
                logger.warning(f"Failed to set model via session/set_model: {e}, trying set_config_option")
                try:
                    await self._send_request("session/set_config_option", {
                        "sessionId": session_id,
                        "configId": "model",
                        "value": model,
                    }, timeout=10)
                    logger.debug(f"Set model to {model} via set_config_option")
                except Exception as e2:
                    logger.debug(f"Failed to set model via set_config_option: {e2}")
        
        logger.info(f"StdioACP session created: {session_id[:16] if session_id else 'unknown'}...")
        
        return session_id
    
    async def load_session(self, session_id: str) -> bool:
        """加载已有会话。"""
        if not self._initialized:
            await self.initialize()
        
        try:
            result = await self._send_request("session/load", {
                "sessionId": session_id,
                "cwd": str(self.workspace),
                "mcpServers": [],
            })
            return result.get("loaded", False)
        except StdioACPError as e:
            logger.warning(f"Failed to load session {session_id[:16]}...: {e}")
            return False
    
    async def prompt(
        self,
        session_id: str,
        message: str,
        timeout: Optional[int] = None,
        on_chunk: Optional[Callable[[AgentMessageChunk], Coroutine]] = None,
        on_tool_call: Optional[Callable[[ToolCall], Coroutine]] = None,
    ) -> ACPResponse:
        """发送消息并获取响应。"""
        if not self._started or not self._process:
            raise StdioACPConnectionError("ACP process not started")
        
        response = ACPResponse()
        content_parts: list[str] = []
        thought_parts: list[str] = []
        tool_calls_map: dict[str, ToolCall] = {}
        
        request_id = self._next_request_id()
        
        request = {
            "jsonrpc": "2.0",
            "id": request_id,
            "method": "session/prompt",
            "params": {
                "sessionId": session_id,
                "prompt": [
                    {"type": "text", "text": message}
                ],
            },
        }
        
        future: asyncio.Future[dict] = asyncio.get_event_loop().create_future()
        self._pending_requests[request_id] = future
        
        try:
            request_str = json.dumps(request) + "\n"
            self._process.stdin.write(request_str.encode())
            await self._process.stdin.drain()
            logger.debug(f"StdioACP prompt sent (session={session_id[:16]}...)")
            
            timeout = timeout or self.timeout
            start_time = asyncio.get_event_loop().time()
            
            while True:
                remaining = timeout - (asyncio.get_event_loop().time() - start_time)
                if remaining <= 0:
                    raise StdioACPTimeoutError("Prompt timeout")
                
                try:
                    if future.done():
                        break
                    
                    msg = await asyncio.wait_for(
                        self._message_queue.get(),
                        timeout=min(remaining, 5.0)
                    )
                    
                    if msg.get("method") == "session/update":
                        params = msg.get("params", {})
                        update = params.get("update", {})
                        update_type = update.get("sessionUpdate", "")
                        
                        if update_type == "agent_message_chunk":
                            content = update.get("content", {})
                            if isinstance(content, dict) and content.get("type") == "text":
                                chunk_text = content.get("text", "")
                                if chunk_text:
                                    content_parts.append(chunk_text)
                                    if on_chunk:
                                        await on_chunk(AgentMessageChunk(text=chunk_text))
                        
                        elif update_type == "agent_thought_chunk":
                            content = update.get("content", {})
                            if isinstance(content, dict) and content.get("type") == "text":
                                chunk_text = content.get("text", "")
                                if chunk_text:
                                    thought_parts.append(chunk_text)
                                    if on_chunk:
                                        await on_chunk(AgentMessageChunk(text=chunk_text, is_thought=True))
                        
                        elif update_type == "tool_call":
                            tool_call_id = update.get("toolCallId", "")
                            tool_name = update.get("name", "")
                            args = update.get("args", {})
                            
                            tc = ToolCall(
                                tool_call_id=tool_call_id,
                                tool_name=tool_name,
                                status="pending",
                                args=args,
                            )
                            tool_calls_map[tool_call_id] = tc
                            
                            if on_tool_call:
                                await on_tool_call(tc)
                        
                        elif update_type == "tool_call_update":
                            tool_call_id = update.get("toolCallId", "")
                            status = update.get("status", "")
                            output_text = ""
                            
                            content = update.get("content", [])
                            if isinstance(content, list):
                                for c in content:
                                    if c.get("type") == "text":
                                        output_text += c.get("text", "")
                            elif isinstance(content, dict) and content.get("type") == "text":
                                output_text = content.get("text", "")
                            
                            if tool_call_id in tool_calls_map:
                                tc = tool_calls_map[tool_call_id]
                                if status:
                                    tc.status = status
                                if output_text:
                                    tc.output = output_text
                                
                                if on_tool_call:
                                    await on_tool_call(tc)
                
                except asyncio.TimeoutError:
                    continue
                
                if future.done():
                    break
            
            final_response = future.result()
            
            if "error" in final_response:
                response.error = final_response["error"].get("message", str(final_response["error"]))
                response.stop_reason = StopReason.ERROR
            else:
                result = final_response.get("result", {})
                stop_reason_str = result.get("stopReason", "end_turn")
                try:
                    response.stop_reason = StopReason(stop_reason_str)
                except ValueError:
                    response.stop_reason = StopReason.END_TURN
            
            response.content = "".join(content_parts)
            response.thought = "".join(thought_parts)
            response.tool_calls = list(tool_calls_map.values())
            
            logger.debug(f"StdioACP prompt completed: stop_reason={response.stop_reason}")
            
            return response
            
        except asyncio.TimeoutError:
            self._pending_requests.pop(request_id, None)
            raise StdioACPTimeoutError("Prompt timeout")
        except Exception as e:
            self._pending_requests.pop(request_id, None)
            raise StdioACPError(f"Prompt error: {e}")
    
    async def cancel(self, session_id: str) -> None:
        """取消当前请求。"""
        if not self._started or not self._process:
            return
        
        notification = {
            "jsonrpc": "2.0",
            "method": "session/cancel",
            "params": {
                "sessionId": session_id,
            },
        }
        
        try:
            notification_str = json.dumps(notification) + "\n"
            self._process.stdin.write(notification_str.encode())
            await self._process.stdin.drain()
            logger.debug(f"StdioACP cancel sent (session={session_id[:16]}...)")
        except Exception as e:
            logger.warning(f"Failed to send cancel: {e}")
    
    async def is_connected(self) -> bool:
        """检查连接状态。"""
        return self._started and self._process is not None and self._process.returncode is None


class StdioACPAdapter:
    """
    Stdio ACP 适配器 - 管理会话映射。
    """
    
    def __init__(
        self,
        iflow_path: str = "iflow",
        workspace: Optional[Path] = None,
        timeout: int = 300,
        default_model: str = "glm-5",
        thinking: bool = False,
    ):
        self.iflow_path = iflow_path
        self.workspace = workspace
        self.timeout = timeout
        self.default_model = default_model
        self.thinking = thinking
        
        self._client: Optional[StdioACPClient] = None
        self._session_map: dict[str, str] = {}
        self._session_map_file = Path.home() / ".iflow-bot" / "session_mappings.json"
        self._session_lock = asyncio.Lock()
        self._load_session_map()
        
        logger.info(f"StdioACPAdapter: iflow_path={iflow_path}, workspace={workspace}")
    
    def _load_session_map(self) -> None:
        if self._session_map_file.exists():
            try:
                with open(self._session_map_file, "r", encoding="utf-8") as f:
                    self._session_map = json.load(f)
                logger.debug(f"Loaded {len(self._session_map)} session mappings")
            except json.JSONDecodeError:
                logger.warning("Invalid session mapping file, starting fresh")
                self._session_map = {}
    
    def _save_session_map(self) -> None:
        self._session_map_file.parent.mkdir(parents=True, exist_ok=True)
        with open(self._session_map_file, "w", encoding="utf-8") as f:
            json.dump(self._session_map, f, indent=2, ensure_ascii=False)
    
    def _find_session_file(self, session_id: str) -> Optional[Path]:
        sessions_dir = Path.home() / ".iflow" / "acp" / "sessions"
        
        if not sessions_dir.exists():
            return None
        
        session_file = sessions_dir / f"{session_id}.json"
        if session_file.exists():
            return session_file
        
        return None
    
    def _extract_conversation_history(self, session_id: str, max_turns: int = 20) -> Optional[str]:
        import datetime
        
        session_file = self._find_session_file(session_id)
        if not session_file:
            logger.debug(f"Session file not found for: {session_id[:16]}...")
            return None
        
        try:
            with open(session_file, "r", encoding="utf-8") as f:
                data = json.load(f)
            
            chat_history = data.get("chatHistory", [])
            if not chat_history:
                return None
            
            recent_chats = chat_history[-max_turns:] if len(chat_history) > max_turns else chat_history
            
            conversations = []
            for chat in recent_chats:
                role = chat.get("role")
                parts = chat.get("parts", [])
                
                full_text = ""
                for part in parts:
                    if isinstance(part, dict):
                        text = part.get("text", "")
                        if text:
                            full_text += text + "\n"
                
                if not full_text.strip():
                    continue
                
                if role == "user":
                    if "用户消息:" in full_text:
                        idx = full_text.find("用户消息:") + len("用户消息:")
                        content = full_text[idx:].strip()
                    else:
                        continue
                    
                    if len(content) < 2 or len(content) > 2000:
                        continue
                    
                    timestamp = chat.get("timestamp") or data.get("createdAt", "")
                    time_str = ""
                    if timestamp:
                        try:
                            ts = timestamp.replace("Z", "+00:00")
                            dt = datetime.datetime.fromisoformat(ts.replace("+00:00", ""))
                            time_str = dt.strftime("%Y-%m-%d %H:%M:%S")
                        except:
                            pass
                    conversations.append(f"{time_str}\n用户：{content}")
                
                elif role == "model":
                    content = full_text.strip()
                    
                    if len(content) > 3000:
                        content = content[:3000] + "..."
                    
                    if "<system-reminder>" in content or "[AGENTS - 工作空间指南]" in content:
                        continue
                    
                    if len(content) > 10:
                        conversations.append(f"我：{content}")
            
            if not conversations:
                return None
            
            history = "<history_context>\n" + "\n\n".join(conversations) + "\n</history_context>"
            logger.info(f"Extracted {len(conversations)} conversation turns from session {session_id[:16]}...")
            return history
            
        except Exception as e:
            logger.warning(f"Failed to extract conversation history: {e}")
            return None
    
    async def connect(self) -> None:
        if self._client is None:
            self._client = StdioACPClient(
                iflow_path=self.iflow_path,
                workspace=self.workspace,
                timeout=self.timeout,
            )
        
        await self._client.start()
        await self._client.initialize()
        
        authenticated = await self._client.authenticate("iflow")
        if not authenticated:
            logger.warning("StdioACP authentication failed, some features may not work")
    
    async def disconnect(self) -> None:
        if self._client:
            await self._client.stop()
            self._client = None
    
    def _get_session_key(self, channel: str, chat_id: str) -> str:
        return f"{channel}:{chat_id}"
    
    async def _get_or_create_session(
        self,
        channel: str,
        chat_id: str,
        model: Optional[str] = None,
    ) -> str:
        key = self._get_session_key(channel, chat_id)
        
        if key in self._session_map:
            logger.debug(f"Reusing existing session: {key} -> {self._session_map[key][:16]}...")
            return self._session_map[key]
        
        return await self._create_new_session(key, model)
    
    async def _create_new_session(
        self,
        key: str,
        model: Optional[str] = None,
    ) -> str:
        async with self._session_lock:
            if key in self._session_map:
                return self._session_map[key]
            
            if not self._client:
                raise StdioACPConnectionError("StdioACP client not connected")
            
            session_id = await self._client.create_session(
                workspace=self.workspace,
                model=model or self.default_model,
                approval_mode="yolo",
            )
            
            self._session_map[key] = session_id
            self._save_session_map()
            logger.info(f"StdioACP session mapped: {key} -> {session_id[:16]}...")
            
            return session_id
    
    async def _invalidate_session(self, key: str) -> Optional[str]:
        old_session = self._session_map.pop(key, None)
        if old_session:
            self._save_session_map()
            logger.info(f"Session invalidated: {key} -> {old_session[:16]}...")
        return old_session
    
    async def chat(
        self,
        message: str,
        channel: str = "cli",
        chat_id: str = "direct",
        model: Optional[str] = None,
        timeout: Optional[int] = None,
    ) -> str:
        if not self._client:
            raise StdioACPConnectionError("StdioACP client not connected")
        
        key = self._get_session_key(channel, chat_id)
        session_id = await self._get_or_create_session(channel, chat_id, model)
        
        response = await self._client.prompt(
            session_id=session_id,
            message=message,
            timeout=timeout or self.timeout,
        )
        
        if response.error and "Invalid request" in response.error:
            logger.warning(f"Session invalid, recreating: {key}")
            old_session_id = await self._invalidate_session(key)
            history_context = ""
            if old_session_id:
                history_context = self._extract_conversation_history(old_session_id) or ""
            
            session_id = await self._create_new_session(key, model)
            
            if history_context:
                user_msg_marker = "用户消息:"
                if user_msg_marker in message:
                    idx = message.find(user_msg_marker)
                    message = message[:idx] + history_context + "\n\n" + message[idx:]
                else:
                    message = f"{history_context}\n\n{message}"
                logger.info(f"Injected conversation history before user message")
            
            response = await self._client.prompt(
                session_id=session_id,
                message=message,
                timeout=timeout or self.timeout,
            )
        
        if response.error:
            raise StdioACPError(f"Chat error: {response.error}")
        
        if self.thinking and response.thought:
            return f"[Thinking]\n{response.thought}\n\n[Response]\n{response.content}"
        
        return response.content
    
    async def chat_stream(
        self,
        message: str,
        channel: str = "cli",
        chat_id: str = "direct",
        model: Optional[str] = None,
        timeout: Optional[int] = None,
        on_chunk: Optional[Callable[[AgentMessageChunk], Coroutine]] = None,
        on_tool_call: Optional[Callable[[ToolCall], Coroutine]] = None,
    ) -> str:
        if not self._client:
            raise StdioACPConnectionError("StdioACP client not connected")
        
        key = self._get_session_key(channel, chat_id)
        session_id = await self._get_or_create_session(channel, chat_id, model)
        
        content_parts: list[str] = []
        
        async def handle_chunk(chunk: AgentMessageChunk):
            if not chunk.is_thought and chunk.text:
                content_parts.append(chunk.text)
            if on_chunk:
                result = on_chunk(chunk)
                if asyncio.iscoroutine(result):
                    await result
        
        async def handle_tool_call(tool_call: ToolCall):
            if on_tool_call:
                result = on_tool_call(tool_call)
                if asyncio.iscoroutine(result):
                    await result
        
        response = await self._client.prompt(
            session_id=session_id,
            message=message,
            timeout=timeout or self.timeout,
            on_chunk=handle_chunk,
            on_tool_call=handle_tool_call,
        )
        
        if response.error and "Invalid request" in response.error:
            logger.warning(f"Session invalid (stream), recreating: {key}")
            old_session_id = await self._invalidate_session(key)
            history_context = ""
            if old_session_id:
                history_context = self._extract_conversation_history(old_session_id) or ""
            
            session_id = await self._create_new_session(key, model)
            
            if history_context:
                user_msg_marker = "用户消息:"
                if user_msg_marker in message:
                    idx = message.find(user_msg_marker)
                    message = message[:idx] + history_context + "\n\n" + message[idx:]
                else:
                    message = f"{history_context}\n\n{message}"
                logger.info(f"Injected conversation history before user message (stream)")
            
            content_parts.clear()
            response = await self._client.prompt(
                session_id=session_id,
                message=message,
                timeout=timeout or self.timeout,
                on_chunk=handle_chunk,
                on_tool_call=handle_tool_call,
            )
        
        if response.error:
            raise StdioACPError(f"Chat error: {response.error}")
        
        return "".join(content_parts) or response.content
    
    async def new_chat(
        self,
        message: str,
        channel: str = "cli",
        chat_id: str = "direct",
        model: Optional[str] = None,
        timeout: Optional[int] = None,
    ) -> str:
        key = self._get_session_key(channel, chat_id)
        if key in self._session_map:
            del self._session_map[key]
        
        return await self.chat(message, channel, chat_id, model, timeout)
    
    async def health_check(self) -> bool:
        if self._client is None:
            return False
        return await self._client.is_connected()
    
    def clear_session(self, channel: str, chat_id: str) -> bool:
        key = self._get_session_key(channel, chat_id)
        if key in self._session_map:
            del self._session_map[key]
            return True
        return False
    
    def list_sessions(self) -> dict[str, str]:
        return self._session_map.copy()
