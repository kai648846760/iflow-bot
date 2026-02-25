"""ACP (Agent Communication Protocol) Connector for iflow CLI.

实现 ACP 协议连接器，不依赖官方 SDK，直接使用 WebSocket 进行通信。

ACP 协议基于 JSON-RPC 2.0，使用 WebSocket 作为传输层。

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
import time
import uuid
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Callable, Coroutine, Optional, Union

from loguru import logger

try:
    import websockets
    WEBSOCKETS_AVAILABLE = True
except ImportError:
    WEBSOCKETS_AVAILABLE = False
    websockets = None  # type: ignore


class ACPError(Exception):
    """ACP 连接器错误基类。"""
    pass


class ACPConnectionError(ACPError):
    """ACP 连接错误。"""
    pass


class ACPTimeoutError(ACPError):
    """ACP 超时错误。"""
    pass


class StopReason(str, Enum):
    """任务结束原因。"""
    END_TURN = "end_turn"
    MAX_TOKENS = "max_tokens"
    REFUSAL = "refusal"
    CANCELLED = "cancelled"
    ERROR = "error"


@dataclass
class ContentBlock:
    """内容块基类。"""
    type: str = "text"
    text: str = ""


@dataclass
class TextContent(ContentBlock):
    """文本内容块。"""
    type: str = "text"
    text: str = ""


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
    status: str = "pending"  # pending, in_progress, completed, failed
    content: list[ContentBlock] = field(default_factory=list)
    args: dict = field(default_factory=dict)
    output: str = ""


@dataclass
class SessionUpdate:
    """会话更新消息。"""
    update_type: str
    data: dict = field(default_factory=dict)


@dataclass
class ACPResponse:
    """ACP 响应结果。"""
    content: str = ""
    thought: str = ""
    tool_calls: list[ToolCall] = field(default_factory=list)
    stop_reason: Optional[StopReason] = None
    error: Optional[str] = None


class ACPClient:
    """
    ACP 协议客户端 - 与 iflow CLI 的 ACP 模式通信。
    
    使用 WebSocket 连接到 iflow --experimental-acp 启动的服务器。
    实现完整的 JSON-RPC 2.0 消息格式。
    
    Example:
        client = ACPClient(host="localhost", port=8090)
        await client.connect()
        await client.initialize()
        session_id = await client.create_session(workspace="/path/to/workspace")
        response = await client.prompt(session_id, "Hello!")
        print(response.content)
    """
    
    PROTOCOL_VERSION = 1
    
    def __init__(
        self,
        host: str = "localhost",
        port: int = 8090,
        timeout: int = 300,
        workspace: Optional[Path] = None,
    ):
        """
        初始化 ACP 客户端。
        
        Args:
            host: ACP 服务器主机地址
            port: ACP 服务器端口
            timeout: 请求超时时间（秒）
            workspace: 工作目录路径
        """
        if not WEBSOCKETS_AVAILABLE:
            raise ImportError("websockets library not installed. Run: pip install websockets")
        
        self.host = host
        self.port = port
        self.timeout = timeout
        self.workspace = workspace
        
        self._ws: Any = None
        self._connected = False
        self._initialized = False
        self._request_id = 0
        self._pending_requests: dict[int, asyncio.Future] = {}
        self._receive_task: Optional[asyncio.Task] = None
        self._message_queue: asyncio.Queue[dict] = asyncio.Queue()
        
        # Agent 能力
        self._agent_capabilities: dict = {}
        
        logger.info(f"ACPClient initialized: ws://{host}:{port}/acp")
    
    @property
    def ws_url(self) -> str:
        """获取 WebSocket URL。"""
        return f"ws://{self.host}:{self.port}/acp"
    
    async def connect(self) -> None:
        """连接到 ACP 服务器。"""
        if self._connected:
            return
        
        try:
            self._ws = await websockets.connect(
                self.ws_url,
                ping_interval=30,
                ping_timeout=10,
                close_timeout=5,
            )
            
            self._connected = True
            
            # 启动消息接收任务
            self._receive_task = asyncio.create_task(self._receive_loop())
            
            logger.info(f"ACP connected to {self.ws_url}")
            
            # 等待服务器的就绪信号 "//ready"
            # 然后等待 peer 准备好（大约需要 2-3 秒）
            await asyncio.sleep(3)
            
        except Exception as e:
            raise ACPConnectionError(f"Failed to connect to ACP server: {e}")
    
    async def disconnect(self) -> None:
        """断开与 ACP 服务器的连接。"""
        if self._receive_task:
            self._receive_task.cancel()
            try:
                await self._receive_task
            except asyncio.CancelledError:
                pass
            self._receive_task = None
        
        if self._ws:
            try:
                await self._ws.close()
            except Exception:
                pass
            self._ws = None
        
        self._connected = False
        self._initialized = False
        logger.info("ACP disconnected")
    
    async def _receive_loop(self) -> None:
        """消息接收循环。"""
        while self._connected:
            try:
                raw = await self._ws.recv()
                
                # 跳过非 JSON 消息（如 //ready, //stderr 等）
                if isinstance(raw, str) and not raw.strip().startswith("{"):
                    logger.debug(f"ACP non-JSON message: {raw[:100]}")
                    continue
                
                message = json.loads(raw)
                
                # 处理响应或通知
                if "id" in message:
                    # 这是一个响应
                    request_id = message["id"]
                    if request_id in self._pending_requests:
                        future = self._pending_requests.pop(request_id)
                        if not future.done():
                            future.set_result(message)
                else:
                    # 这是一个通知，放入队列
                    await self._message_queue.put(message)
                    
            except asyncio.CancelledError:
                break
            except websockets.ConnectionClosed:
                logger.warning("ACP WebSocket connection closed")
                self._connected = False
                break
            except json.JSONDecodeError as e:
                logger.debug(f"ACP JSON decode error: {e}")
                continue
            except Exception as e:
                logger.error(f"ACP receive error: {e}")
    
    def _next_request_id(self) -> int:
        """获取下一个请求 ID。"""
        self._request_id += 1
        return self._request_id
    
    async def _send_request(
        self,
        method: str,
        params: dict,
        timeout: Optional[int] = None,
    ) -> dict:
        """
        发送 JSON-RPC 请求并等待响应。
        
        Args:
            method: JSON-RPC 方法名
            params: 方法参数
            timeout: 超时时间（秒）
        
        Returns:
            响应结果
        """
        if not self._connected or not self._ws:
            raise ACPConnectionError("Not connected to ACP server")
        
        request_id = self._next_request_id()
        
        request = {
            "jsonrpc": "2.0",
            "id": request_id,
            "method": method,
            "params": params,
        }
        
        # 创建 Future 等待响应
        future: asyncio.Future[dict] = asyncio.get_event_loop().create_future()
        self._pending_requests[request_id] = future
        
        try:
            await self._ws.send(json.dumps(request))
            logger.debug(f"ACP request: {method} (id={request_id})")
            
            # 等待响应
            timeout = timeout or self.timeout
            response = await asyncio.wait_for(future, timeout=timeout)
            
            if "error" in response:
                error = response["error"]
                raise ACPError(f"ACP error: {error.get('message', str(error))}")
            
            return response.get("result", {})
            
        except asyncio.TimeoutError:
            self._pending_requests.pop(request_id, None)
            raise ACPTimeoutError(f"ACP request timeout: {method}")
    
    async def initialize(self) -> dict:
        """
        初始化 ACP 连接。
        
        协商协议版本并交换能力。
        """
        if self._initialized:
            return self._agent_capabilities
        
        # 客户端能力 - 使用 camelCase 格式
        client_capabilities = {
            "fs": {
                "readTextFile": True,
                "writeTextFile": True,
            }
        }
        
        # initialize 必须包含 protocolVersion 和 clientCapabilities
        result = await self._send_request("initialize", {
            "protocolVersion": self.PROTOCOL_VERSION,
            "clientCapabilities": client_capabilities,
        })
        
        self._agent_capabilities = result.get("agentCapabilities", {})
        self._initialized = True
        
        logger.info(f"ACP initialized: version={result.get('protocolVersion')}, "
                   f"capabilities={list(self._agent_capabilities.keys())}")
        
        return self._agent_capabilities
    
    async def authenticate(self, method_id: str = "iflow") -> bool:
        """
        进行认证。
        
        Args:
            method_id: 认证方法 ID (iflow, oauth-iflow, openai-compatible)
        
        Returns:
            是否认证成功
        """
        if not self._initialized:
            await self.initialize()
        
        try:
            result = await self._send_request("authenticate", {
                "methodId": method_id,
            })
            success = result.get("methodId") == method_id
            if success:
                logger.info(f"ACP authenticated with method: {method_id}")
            return success
        except ACPError as e:
            logger.error(f"ACP authentication failed: {e}")
            return False
    
    async def create_session(
        self,
        workspace: Optional[Path] = None,
        model: Optional[str] = None,
        system_prompt: Optional[str] = None,
        approval_mode: str = "yolo",
    ) -> str:
        """
        创建新会话。
        
        Args:
            workspace: 工作目录
            model: 模型名称
            system_prompt: 系统提示
            approval_mode: 审批模式 (default, smart, yolo, plan)
        
        Returns:
            会话 ID
        """
        if not self._initialized:
            await self.initialize()
        
        ws_path = str(workspace or self.workspace or Path.cwd())
        
        # 按照正确的 ACP 协议格式
        # session/new 需要: cwd, mcpServers (必须)
        params: dict = {
            "cwd": ws_path,
            "mcpServers": [],  # 必须参数，空数组表示不使用 MCP
        }
        
        # settings 中包含 approval_mode 等
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
        
        logger.info(f"ACP session created: {session_id[:16] if session_id else 'unknown'}...")
        
        return session_id
    
    async def load_session(self, session_id: str) -> bool:
        """
        加载已有会话。
        
        Args:
            session_id: 会话 ID
        
        Returns:
            是否成功加载
        """
        if not self._initialized:
            await self.initialize()
        
        try:
            # session/load 需要 cwd 和 mcpServers 参数
            result = await self._send_request("session/load", {
                "sessionId": session_id,
                "cwd": str(self.workspace),
                "mcpServers": [],
            })
            return result.get("loaded", False)
        except ACPError as e:
            logger.warning(f"Failed to load session {session_id[:16]}...: {e}")
            return False
    
    async def prompt(
        self,
        session_id: str,
        message: str,
        timeout: Optional[int] = None,
        on_chunk: Optional[Callable[[AgentMessageChunk], None]] = None,
        on_tool_call: Optional[Callable[[ToolCall], None]] = None,
    ) -> ACPResponse:
        """
        发送消息并获取响应。
        
        Args:
            session_id: 会话 ID
            message: 用户消息
            timeout: 超时时间（秒）
            on_chunk: 消息块回调
            on_tool_call: 工具调用回调
        
        Returns:
            ACP 响应结果
        """
        if not self._connected:
            raise ACPConnectionError("Not connected to ACP server")
        
        response = ACPResponse()
        content_parts: list[str] = []
        thought_parts: list[str] = []
        tool_calls_map: dict[str, ToolCall] = {}
        
        # 发送 prompt 请求 - 使用正确的参数格式
        # session/prompt 需要: sessionId, prompt (数组)
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
            await self._ws.send(json.dumps(request))
            logger.debug(f"ACP prompt sent (session={session_id[:16]}...)")
            
            # 接收更新直到收到最终响应
            timeout = timeout or self.timeout
            start_time = time.time()
            
            while True:
                remaining = timeout - (time.time() - start_time)
                if remaining <= 0:
                    raise ACPTimeoutError("Prompt timeout")
                
                try:
                    # 优先检查最终响应
                    if future.done():
                        break
                    
                    # 等待通知消息
                    msg = await asyncio.wait_for(
                        self._message_queue.get(),
                        timeout=min(remaining, 5.0)
                    )
                    
                    # 处理 session/update 通知
                    if msg.get("method") == "session/update":
                        params = msg.get("params", {})
                        update = params.get("update", {})
                        update_type = update.get("sessionUpdate", "")
                        
                        if update_type == "agent_message_chunk":
                            # Agent 消息块 - content 是一个 dict，不是数组
                            content = update.get("content", {})
                            if isinstance(content, dict) and content.get("type") == "text":
                                chunk_text = content.get("text", "")
                                if chunk_text:
                                    content_parts.append(chunk_text)
                                    if on_chunk:
                                        await on_chunk(AgentMessageChunk(text=chunk_text))
                        
                        elif update_type == "agent_thought_chunk":
                            # Agent 思考过程
                            content = update.get("content", {})
                            if isinstance(content, dict) and content.get("type") == "text":
                                chunk_text = content.get("text", "")
                                if chunk_text:
                                    thought_parts.append(chunk_text)
                                    if on_chunk:
                                        await on_chunk(AgentMessageChunk(text=chunk_text, is_thought=True))
                        
                        elif update_type == "tool_call":
                            # 新的工具调用
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
                            # 工具调用更新
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
                    # 继续检查最终响应
                    continue
                
                # 检查最终响应
                if future.done():
                    break
            
            # 获取最终响应
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
            
            # 组装响应
            response.content = "".join(content_parts)
            response.thought = "".join(thought_parts)
            response.tool_calls = list(tool_calls_map.values())
            
            logger.debug(f"ACP prompt completed: stop_reason={response.stop_reason}, "
                        f"content_len={len(response.content)}")
            
            return response
            
        except asyncio.TimeoutError:
            self._pending_requests.pop(request_id, None)
            raise ACPTimeoutError("Prompt timeout")
        except Exception as e:
            self._pending_requests.pop(request_id, None)
            raise ACPError(f"Prompt error: {e}")
    
    async def cancel(self, session_id: str) -> None:
        """
        取消当前请求。
        
        Args:
            session_id: 会话 ID
        """
        if not self._connected or not self._ws:
            return
        
        notification = {
            "jsonrpc": "2.0",
            "method": "session/cancel",
            "params": {
                "sessionId": session_id,
            },
        }
        
        try:
            await self._ws.send(json.dumps(notification))
            logger.debug(f"ACP cancel sent (session={session_id[:16]}...)")
        except Exception as e:
            logger.warning(f"Failed to send cancel: {e}")
    
    async def is_connected(self) -> bool:
        """检查连接状态。"""
        return self._connected and self._ws is not None


class ACPAdapter:
    """
    ACP 适配器 - 封装 ACPClient 提供与 IFlowAdapter 兼容的接口。
    
    管理会话映射，提供 chat 和 new_chat 方法。
    """
    
    def __init__(
        self,
        host: str = "localhost",
        port: int = 8090,
        timeout: int = 300,
        workspace: Optional[Path] = None,
        default_model: str = "glm-5",
        thinking: bool = False,
    ):
        """
        初始化 ACP 适配器。
        
        Args:
            host: ACP 服务器主机地址
            port: ACP 服务器端口
            timeout: 请求超时时间（秒）
            workspace: 工作目录路径
            default_model: 默认模型
            thinking: 是否启用思考模式
        """
        self.host = host
        self.port = port
        self.timeout = timeout
        self.workspace = workspace
        self.default_model = default_model
        self.thinking = thinking
        
        self._client: Optional[ACPClient] = None
        self._session_map: dict[str, str] = {}  # channel:chat_id -> session_id
        
        logger.info(f"ACPAdapter: host={host}, port={port}, workspace={workspace}")
    
    async def connect(self) -> None:
        """连接到 ACP 服务器并进行认证。"""
        if self._client is None:
            self._client = ACPClient(
                host=self.host,
                port=self.port,
                timeout=self.timeout,
                workspace=self.workspace,
            )
        
        await self._client.connect()
        await self._client.initialize()
        
        # 进行认证 (使用 iflow 方法，需要设置 IFLOW_API_KEY 环境变量)
        authenticated = await self._client.authenticate("iflow")
        if not authenticated:
            logger.warning("ACP authentication failed, some features may not work")
    
    async def disconnect(self) -> None:
        """断开连接。"""
        if self._client:
            await self._client.disconnect()
            self._client = None
    
    def _get_session_key(self, channel: str, chat_id: str) -> str:
        """获取会话键。"""
        return f"{channel}:{chat_id}"
    
    async def _get_or_create_session(
        self,
        channel: str,
        chat_id: str,
        model: Optional[str] = None,
    ) -> str:
        """
        获取或创建会话。
        
        Args:
            channel: 渠道名称
            chat_id: 聊天 ID
            model: 模型名称
        
        Returns:
            会话 ID
        """
        key = self._get_session_key(channel, chat_id)
        
        if key in self._session_map:
            session_id = self._session_map[key]
            # 尝试加载已有会话
            if self._client and await self._client.load_session(session_id):
                return session_id
        
        # 创建新会话
        if not self._client:
            raise ACPConnectionError("ACP client not connected")
        
        session_id = await self._client.create_session(
            workspace=self.workspace,
            model=model or self.default_model,
            approval_mode="yolo",
        )
        
        self._session_map[key] = session_id
        logger.info(f"ACP session mapped: {key} -> {session_id[:16]}...")
        
        return session_id
    
    async def chat(
        self,
        message: str,
        channel: str = "cli",
        chat_id: str = "direct",
        model: Optional[str] = None,
        timeout: Optional[int] = None,
    ) -> str:
        """
        发送消息并获取响应。
        
        Args:
            message: 用户消息
            channel: 渠道名称
            chat_id: 聊天 ID
            model: 模型名称
            timeout: 超时时间
        
        Returns:
            响应文本
        """
        if not self._client:
            raise ACPConnectionError("ACP client not connected")
        
        session_id = await self._get_or_create_session(channel, chat_id, model)
        
        response = await self._client.prompt(
            session_id=session_id,
            message=message,
            timeout=timeout or self.timeout,
        )
        
        if response.error:
            raise ACPError(f"Chat error: {response.error}")
        
        # 如果启用思考模式，包含思考过程
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
        """
        发送消息并流式获取响应。
        
        Args:
            message: 用户消息
            channel: 渠道名称
            chat_id: 聊天 ID
            model: 模型名称
            timeout: 超时时间
            on_chunk: 消息块回调（支持异步）
            on_tool_call: 工具调用回调（支持异步）
        
        Returns:
            最终响应文本
        """
        if not self._client:
            raise ACPConnectionError("ACP client not connected")
        
        session_id = await self._get_or_create_session(channel, chat_id, model)
        
        # 收集完整响应
        content_parts: list[str] = []
        
        async def handle_chunk(chunk: AgentMessageChunk):
            """处理消息块。"""
            if not chunk.is_thought and chunk.text:
                content_parts.append(chunk.text)
            if on_chunk:
                result = on_chunk(chunk)
                if asyncio.iscoroutine(result):
                    await result
        
        async def handle_tool_call(tool_call: ToolCall):
            """处理工具调用。"""
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
        
        if response.error:
            raise ACPError(f"Chat error: {response.error}")
        
        # 返回收集的完整响应
        return "".join(content_parts) or response.content
    
    async def new_chat(
        self,
        message: str,
        channel: str = "cli",
        chat_id: str = "direct",
        model: Optional[str] = None,
        timeout: Optional[int] = None,
    ) -> str:
        """
        开始新对话。
        
        清除已有会话映射，创建新会话。
        """
        key = self._get_session_key(channel, chat_id)
        if key in self._session_map:
            del self._session_map[key]
        
        return await self.chat(message, channel, chat_id, model, timeout)
    
    async def health_check(self) -> bool:
        """检查连接状态。"""
        if self._client is None:
            return False
        return await self._client.is_connected()
    
    def clear_session(self, channel: str, chat_id: str) -> bool:
        """清除会话映射。"""
        key = self._get_session_key(channel, chat_id)
        if key in self._session_map:
            del self._session_map[key]
            return True
        return False
    
    def list_sessions(self) -> dict[str, str]:
        """列出所有会话映射。"""
        return self._session_map.copy()
