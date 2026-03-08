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
import re
import uuid
from datetime import datetime
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Callable, Coroutine, Optional

from loguru import logger

from iflow_bot.config.loader import DEFAULT_TIMEOUT


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
    LINE_LIMIT = 10 * 1024 * 1024  # 10MB - readline 最大行长度，防止大 JSON 被截断
    
    def __init__(
        self,
        iflow_path: str = "iflow",
        workspace: Optional[Path] = None,
        timeout: int = DEFAULT_TIMEOUT,
        mcp_proxy_port: int = 8888,
        mcp_servers_auto_discover: bool = True,
        mcp_servers_max: int = 10,
        mcp_servers_allowlist: Optional[list[str]] = None,
        mcp_servers_blocklist: Optional[list[str]] = None,
        mcp_servers_cached: Optional[list[dict]] = None,
    ):
        self.iflow_path = iflow_path
        self.workspace = workspace or Path.cwd()
        self.timeout = timeout
        self.mcp_proxy_port = mcp_proxy_port
        self.mcp_servers_auto_discover = mcp_servers_auto_discover
        self.mcp_servers_max = mcp_servers_max
        self.mcp_servers_allowlist = mcp_servers_allowlist or []
        self.mcp_servers_blocklist = mcp_servers_blocklist or []
        self.mcp_servers_cached = mcp_servers_cached
        
        self._process: Optional[asyncio.subprocess.Process] = None
        self._started = False
        self._initialized = False
        self._request_id = 0
        self._pending_requests: dict[int, asyncio.Future] = {}
        self._receive_task: Optional[asyncio.Task] = None
        self._stderr_receive_task: Optional[asyncio.Task] = None  # 持续读取 stderr 防止缓冲区满导致死锁
        self._message_queue: asyncio.Queue[dict] = asyncio.Queue()
        self._session_queues: dict[str, asyncio.Queue[dict]] = {}
        self._prompt_lock = asyncio.Lock()  # 保证请求写入的原子性，以及作为并发回退保障
        
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
            
            # 设置 StreamReader 的 limit，防止大 JSON 行被截断
            if self._process.stdout:
                self._process.stdout._limit = self.LINE_LIMIT
            if self._process.stderr:
                self._process.stderr._limit = self.LINE_LIMIT
            
            self._receive_task = asyncio.create_task(self._receive_loop())
            self._stderr_receive_task = asyncio.create_task(self._stderr_receive_loop())
            
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
        
        if self._stderr_receive_task:
            self._stderr_receive_task.cancel()
            try:
                await self._stderr_receive_task
            except asyncio.CancelledError:
                pass
            self._stderr_receive_task = None
        
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
                    # 这是一个通知，根据 sessionId 分发
                    params = message.get("params", {})
                    session_id = params.get("sessionId")
                    if session_id and session_id in self._session_queues:
                        await self._session_queues[session_id].put(message)
                    else:
                        # 如果没有 sessionId 或没有当前监听的 Session，放入全局队列
                        await self._message_queue.put(message)
                    
            except asyncio.TimeoutError:
                continue
            except asyncio.CancelledError:
                break
            except asyncio.LimitOverrunError as e:
                # 当单行超过 StreamReader limit 且未找到分隔符时，不要退出接收循环。
                # 丢弃当前异常大块并继续，避免网关在 Windows 上直接“启动后退出”。
                logger.warning(
                    f"StdioACP receive oversized chunk (consumed={e.consumed}), draining and continue"
                )
                try:
                    await self._process.stdout.readexactly(e.consumed)
                except Exception:
                    try:
                        await self._process.stdout.read(max(e.consumed, 4096))
                    except Exception:
                        pass
                continue
            except ValueError as e:
                # 某些 Python/平台组合会以 ValueError 形式抛出同类错误信息。
                msg = str(e)
                if "Separator is not found" in msg and "chunk exceed the limit" in msg:
                    logger.warning(f"StdioACP receive oversized chunk (value error): {msg}")
                    try:
                        await self._process.stdout.read(4096)
                    except Exception:
                        pass
                    continue
                logger.error(f"StdioACP receive error: {e}")
                break
            except Exception as e:
                logger.error(f"StdioACP receive error: {e}")
                break
        
        logger.debug("StdioACP receive loop ended")
    
    async def _stderr_receive_loop(self) -> None:
        """stderr 接收循环 - 持续读取 stderr 防止缓冲区满导致进程阻塞。"""
        while self._started and self._process and self._process.stderr:
            try:
                line = await asyncio.wait_for(
                    self._process.stderr.readline(),
                    timeout=1.0
                )
                
                if not line:
                    break
                
                raw = line.decode("utf-8", errors="replace").strip()
                
                if raw:
                    logger.debug(f"iflow stderr: {raw[:200]}")
                    
            except asyncio.TimeoutError:
                continue
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.debug(f"StdioACP stderr receive error: {e}")
                break
        
        logger.debug("StdioACP stderr receive loop ended")
    
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
        
        future: asyncio.Future[dict] = asyncio.get_running_loop().create_future()
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

    async def _get_mcp_servers(self) -> list[dict]:
        """获取 MCP 服务器列表。

        优先级：
        1. 使用预缓存的服务器列表 (mcp_servers_cached)
        2. 自动从 MCP 代理发现 (mcp_servers_auto_discover=True)
        3. 降级为空列表（让 iflow 使用其默认配置）
        """
        # 优先使用预缓存的列表（由外部传入）
        if self.mcp_servers_cached:
            logger.debug(f"Using cached MCP servers: {len(self.mcp_servers_cached)}")
            return self.mcp_servers_cached

        # 自动发现
        if self.mcp_servers_auto_discover:
            try:
                from iflow_bot.utils.helpers import discover_mcp_servers
                servers = await discover_mcp_servers(
                    proxy_port=self.mcp_proxy_port,
                    allowlist=self.mcp_servers_allowlist or None,
                    blocklist=self.mcp_servers_blocklist or None,
                    max_servers=self.mcp_servers_max,
                )
                if servers:
                    return servers
            except ImportError as e:
                logger.warning(f"Failed to import discover_mcp_servers: {e}")
            except Exception as e:
                logger.warning(f"Failed to discover MCP servers: {e}")

        # 降级：返回空列表，让 iflow 使用其默认配置
        logger.debug("No MCP servers configured, using empty list")
        return []

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
        
        # 获取 MCP 服务器列表（动态发现或缓存）
        mcp_servers = await self._get_mcp_servers()
        
        params: dict = {
            "cwd": ws_path,
            "mcpServers": mcp_servers,
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
        on_event: Optional[Callable[[dict[str, Any]], Coroutine]] = None,
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
        
        future: asyncio.Future[dict] = asyncio.get_running_loop().create_future()
        self._pending_requests[request_id] = future
        
        # 为当前 session 注册专用消息队列
        session_queue = asyncio.Queue()
        self._session_queues[session_id] = session_queue
        
        async with self._prompt_lock:
            try:
                request_str = json.dumps(request) + "\n"
                self._process.stdin.write(request_str.encode())
                await self._process.stdin.drain()
                logger.debug(f"StdioACP prompt sent (session={session_id[:16]}...)")
            except Exception as e:
                self._pending_requests.pop(request_id, None)
                self._session_queues.pop(session_id, None)
                raise e
        
        try:
            timeout = timeout or self.timeout
            # 使用空闲超时：每次收到消息后重置计时器
            # 这样长时间生成内容不会触发超时，只有真正卡住才会超时
            last_activity_time = asyncio.get_running_loop().time()

            while True:
                idle_time = asyncio.get_running_loop().time() - last_activity_time
                if idle_time >= timeout:
                    raise StdioACPTimeoutError("Prompt timeout (idle)")

                try:
                    if future.done():
                        break

                    # 优先检查自己的私有队列，找不到再看全局队列（Legacy 兼容）
                    try:
                        msg = await asyncio.wait_for(
                            session_queue.get(),
                            timeout=0.1
                        )
                        last_activity_time = asyncio.get_running_loop().time()  # 收到消息，重置计时器
                    except asyncio.TimeoutError:
                        # 尝试从全局队列获取（如果没有 sessionId 字段）
                        remaining_idle = timeout - idle_time
                        msg = await asyncio.wait_for(
                            self._message_queue.get(),
                            timeout=min(remaining_idle, 4.9)
                        )
                        last_activity_time = asyncio.get_running_loop().time()  # 收到消息，重置计时器
                    
                    if msg.get("method") == "session/update":
                        params = msg.get("params", {})
                        update = params.get("update", {})
                        update_type = update.get("sessionUpdate", "")
                        if on_event:
                            await on_event(
                                {
                                    "session_id": session_id,
                                    "update_type": update_type,
                                    "update": update,
                                }
                            )
                        
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
            
            # 关键修复：future.done() 后，_receive_loop 可能还来得及把最后几个
            # agent_message_chunk 放入 _message_queue。这里 drain 掉所有残留消息，
            # 避免最后几段内容被丢弃导致消息截断。
            while True:
                try:
                    # 同样先 drain 自己的私有队列
                    try:
                        msg = await asyncio.wait_for(session_queue.get(), timeout=0.1)
                    except asyncio.TimeoutError:
                        # 尝试全局
                        msg = await asyncio.wait_for(self._message_queue.get(), timeout=0.1)
                        
                    if msg.get("method") == "session/update":
                        params = msg.get("params", {})
                        update = params.get("update", {})
                        update_type = update.get("sessionUpdate", "")
                        if on_event:
                            await on_event(
                                {
                                    "session_id": session_id,
                                    "update_type": update_type,
                                    "update": update,
                                }
                            )
                        if update_type == "agent_message_chunk":
                            content = update.get("content", {})
                            if isinstance(content, dict) and content.get("type") == "text":
                                chunk_text = content.get("text", "")
                                if chunk_text:
                                    content_parts.append(chunk_text)
                                    if on_chunk:
                                        await on_chunk(AgentMessageChunk(text=chunk_text))
                except asyncio.TimeoutError:
                    break  # 队列已回，退出 drain

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
        except StdioACPTimeoutError:
            self._pending_requests.pop(request_id, None)
            raise
        except Exception as e:
            self._pending_requests.pop(request_id, None)
            raise StdioACPError(f"Prompt error: {e}")
        finally:
            # 清理
            self._session_queues.pop(session_id, None)
            self._pending_requests.pop(request_id, None)
    
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
        timeout: int = DEFAULT_TIMEOUT,
        default_model: str = "glm-5",
        thinking: bool = False,
        active_compress_trigger_tokens: int = 60000,
        mcp_proxy_port: int = 8888,
        mcp_servers_auto_discover: bool = True,
        mcp_servers_max: int = 10,
        mcp_servers_allowlist: Optional[list[str]] = None,
        mcp_servers_blocklist: Optional[list[str]] = None,
        mcp_servers_cached: Optional[list[dict]] = None,
    ):
        self.iflow_path = iflow_path
        self.workspace = workspace
        self.timeout = timeout
        self.default_model = default_model
        self.thinking = thinking
        self.mcp_proxy_port = mcp_proxy_port
        self.mcp_servers_auto_discover = mcp_servers_auto_discover
        self.mcp_servers_max = mcp_servers_max
        self.mcp_servers_allowlist = mcp_servers_allowlist or []
        self.mcp_servers_blocklist = mcp_servers_blocklist or []
        self.mcp_servers_cached = mcp_servers_cached
        
        self._client: Optional[StdioACPClient] = None
        self._session_map: dict[str, str] = {}
        self._loaded_sessions: set[str] = set()
        self._rehydrate_history: dict[str, str] = {}
        self._memory_constraints_cache: Optional[str] = None
        self._active_compress_trigger_tokens = max(0, int(active_compress_trigger_tokens))
        self._active_compress_budget_tokens = 2200
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

    def _build_session_system_prompt(self) -> Optional[str]:
        workspace = self.workspace or Path.cwd()
        agents_file = workspace / "AGENTS.md"
        if not agents_file.exists():
            return None

        try:
            agents_content = agents_file.read_text(encoding="utf-8")
        except Exception as e:
            logger.warning(f"Failed to read AGENTS.md for system_prompt: {e}")
            return None

        return f"""[AGENTS - 工作空间指南]
以下是当前工作空间的行为指南，请严格遵循。

{agents_content}
[/AGENTS]

SOUL.md - Who You Are（你的灵魂）定义了你是谁，你的性格、特点、行为准则等核心信息。
IDENTITY.md - Your Identity（你的身份）定义了你的具体身份信息，如名字、年龄、职业、兴趣爱好等。
USERY.md - User Identity（用户身份）定义了用户的具体身份信息，如名字、年龄、职业、兴趣爱好等。
TOOLS.md - Your Tools（你的工具）定义了你可以使用的工具列表，包括每个工具的名称、功能描述、使用方法等, 每次学会一个工具，你便要主动更新该文件。"""
    
    def _extract_conversation_history(
        self,
        session_id: str,
        max_turns: int = 20,
        token_budget: int = 3000,
    ) -> Optional[str]:
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
            
            all_conversations: list[tuple[str, str, str]] = []
            for chat in chat_history:
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
                    all_conversations.append(("user", time_str, content))
                
                elif role == "model":
                    content = full_text.strip()
                    
                    if len(content) > 3000:
                        content = content[:3000] + "..."
                    
                    if "<system-reminder>" in content or "[AGENTS - 工作空间指南]" in content:
                        continue
                    
                    if len(content) > 10:
                        all_conversations.append(("assistant", "", content))
            
            if not all_conversations:
                return None

            history = self._build_budgeted_history_context(
                all_conversations,
                token_budget=token_budget,
                recent_turns=max_turns,
            )
            logger.info(
                f"Extracted {len(all_conversations)} conversation turns from session {session_id[:16]}..."
            )
            return history
            
        except Exception as e:
            logger.warning(f"Failed to extract conversation history: {e}")
            return None

    @staticmethod
    def _estimate_tokens(text: str) -> int:
        if not text:
            return 0
        # 粗略估算：中英文混合场景按 4 chars ≈ 1 token
        return max(1, len(text) // 4)

    @staticmethod
    def _clip_text(text: str, max_chars: int) -> str:
        clean = (text or "").strip()
        if len(clean) <= max_chars:
            return clean
        return clean[:max_chars].rstrip() + "..."

    def _build_budgeted_history_context(
        self,
        conversations: list[tuple[str, str, str]],
        token_budget: int = 3000,
        recent_turns: int = 20,
    ) -> str:
        if not conversations:
            return "<history_context>\n</history_context>"

        def fmt(role: str, time_str: str, content: str, max_chars: int) -> str:
            body = self._clip_text(content, max_chars)
            if role == "user":
                return f"{time_str}\n用户：{body}".strip()
            return f"我：{body}"

        recent = conversations[-recent_turns:] if recent_turns > 0 else []
        older = conversations[:-len(recent)] if recent else conversations[:]

        summary_lines: list[str] = []
        if older:
            summary_lines.append("更早对话摘要：")
            for role, time_str, content in older[-40:]:
                prefix = "用户" if role == "user" else "我"
                item = self._clip_text(content, 120)
                if time_str:
                    summary_lines.append(f"- {time_str} {prefix}: {item}")
                else:
                    summary_lines.append(f"- {prefix}: {item}")

        blocks = [fmt(role, time_str, content, 1600) for role, time_str, content in recent]
        summary_block = "\n".join(summary_lines).strip()

        def build_text(cur_summary: str, cur_blocks: list[str]) -> str:
            parts: list[str] = []
            if cur_summary:
                parts.append(cur_summary)
            parts.extend([b for b in cur_blocks if b.strip()])
            return "<history_context>\n" + "\n\n".join(parts).strip() + "\n</history_context>"

        text = build_text(summary_block, blocks)

        # 优先丢弃最老的 recent blocks，确保最新上下文保留
        while self._estimate_tokens(text) > token_budget and len(blocks) > 4:
            blocks.pop(0)
            text = build_text(summary_block, blocks)

        # 再压缩摘要粒度
        if self._estimate_tokens(text) > token_budget and summary_block:
            compact_summary_lines = []
            for line in summary_lines[:24]:
                compact_summary_lines.append(self._clip_text(line, 90))
            summary_block = "\n".join(compact_summary_lines)
            text = build_text(summary_block, blocks)

        # 最后兜底裁剪
        while self._estimate_tokens(text) > token_budget and len(text) > 200:
            text = text[: max(200, int(len(text) * 0.9))].rstrip() + "\n</history_context>"

        return text
    
    async def connect(self) -> None:
        if self._client is None:
            self._client = StdioACPClient(
                iflow_path=self.iflow_path,
                workspace=self.workspace,
                timeout=self.timeout,
                mcp_proxy_port=self.mcp_proxy_port,
                mcp_servers_auto_discover=self.mcp_servers_auto_discover,
                mcp_servers_max=self.mcp_servers_max,
                mcp_servers_allowlist=self.mcp_servers_allowlist,
                mcp_servers_blocklist=self.mcp_servers_blocklist,
                mcp_servers_cached=self.mcp_servers_cached,
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

    @staticmethod
    def _inject_history_before_user_message(message: str, history_context: str) -> str:
        if not history_context:
            return message
        user_msg_marker = "用户消息:"
        if user_msg_marker in message:
            idx = message.find(user_msg_marker)
            return message[:idx] + history_context + "\n\n" + message[idx:]
        return f"{history_context}\n\n{message}"

    @staticmethod
    def _extract_json_payload(text: str) -> dict[str, Any] | None:
        raw = (text or "").strip()
        if not raw:
            return None
        if raw.startswith("```"):
            raw = re.sub(r"^```[a-zA-Z]*\n?", "", raw)
            raw = re.sub(r"\n?```$", "", raw).strip()
        try:
            payload = json.loads(raw)
            if isinstance(payload, dict):
                return payload
        except Exception:
            pass
        m = re.search(r"\{[\s\S]*\}", raw)
        if not m:
            return None
        try:
            payload = json.loads(m.group(0))
            if isinstance(payload, dict):
                return payload
        except Exception:
            return None
        return None

    @staticmethod
    def _normalize_summary_items(items: Any, limit: int, fallback: str = "") -> list[str]:
        if not isinstance(items, list):
            return [fallback] if fallback else []
        out: list[str] = []
        seen: set[str] = set()
        for item in items:
            text = " ".join(str(item or "").split())
            if not text:
                continue
            key = text.lower()
            if key in seen:
                continue
            seen.add(key)
            out.append(text)
            if len(out) >= limit:
                break
        if not out and fallback:
            out = [fallback]
        return out

    def _build_memory_summary_heuristic(self, text: str) -> dict[str, list[str]]:
        def clip(s: str, max_chars: int = 140) -> str:
            clean = " ".join((s or "").split())
            if len(clean) <= max_chars:
                return clean
            return clean[:max_chars].rstrip() + "..."

        user_msgs: list[str] = []
        assistant_msgs: list[str] = []
        for raw in text.splitlines():
            line = raw.strip()
            if not line or line.startswith("更早对话摘要") or line.startswith("- "):
                continue
            m = re.match(r"^(用户|我)[：:]\s*(.+)$", line)
            if not m:
                continue
            who = m.group(1)
            body = clip(m.group(2))
            if who == "用户":
                user_msgs.append(body)
            else:
                assistant_msgs.append(body)

        unresolved_keywords = ("?", "？", "报错", "错误", "失败", "没", "未", "为什么", "怎么", "如何", "问题", "bug")
        solution_keywords = ("修复", "改为", "实现", "新增", "支持", "配置", "阈值", "压缩", "持久化", "重启", "降级", "回退", "优化")
        learning_keywords = ("通过", "使用", "采用", "机制", "策略", "约束", "预算", "滚动")
        resolved_keywords = ("已修复", "已解决", "已恢复", "已处理", "已完成", "已生效", "已支持")

        highlights = self._normalize_summary_items(user_msgs[-3:] + assistant_msgs[-2:], 4)
        unresolved = self._normalize_summary_items(
            [msg for msg in user_msgs if any(k in msg for k in unresolved_keywords)][-4:],
            3,
        )
        solutions = self._normalize_summary_items(
            [msg for msg in assistant_msgs if any(k in msg for k in solution_keywords)][-6:],
            4,
        )
        learnings = self._normalize_summary_items(
            [msg for msg in assistant_msgs if any(k in msg for k in learning_keywords)][-5:],
            3,
        )
        resolved = self._normalize_summary_items(
            [msg for msg in assistant_msgs if any(k in msg for k in resolved_keywords)][-5:],
            4,
        )
        return {
            "highlights": highlights,
            "unresolved": unresolved,
            "solutions": solutions,
            "learnings": learnings,
            "resolved": resolved,
        }

    async def _build_memory_summary_by_agent(
        self,
        channel: str,
        chat_id: str,
        reason: str,
        history_text: str,
    ) -> dict[str, list[str]] | None:
        if not self._client:
            return None
        try:
            sub_session_id = await self._client.create_session(
                workspace=self.workspace,
                model=self.default_model,
                approval_mode="yolo",
                system_prompt="你是会话记忆压缩助手。仅输出合法 JSON，不要输出解释。",
            )
            constraints = self._load_memory_constraints()
            prompt = (
                "请从下面对话摘要中提取结构化记忆。\n"
                "要求：\n"
                "1) highlights: 今日重点（<=4）\n"
                "2) unresolved: 未解决问题（<=4）\n"
                "3) solutions: 技术方案/结论（<=4）\n"
                "4) learnings: 新增学习（<=3）\n"
                "5) resolved: 本轮已解决的问题（<=4）\n"
                "输出 JSON 对象，键固定为 highlights/unresolved/solutions/learnings/resolved，值为字符串数组。\n"
                "如果某项为空，返回空数组。\n\n"
                f"session={channel}:{chat_id}\nreason={reason}\n\n"
                f"memory_constraints:\n{constraints or '(none)'}\n\n"
                f"{history_text}"
            )
            logger.debug(f"Memory summary sub-session created: {sub_session_id[:16]}... for {channel}:{chat_id}")
            response = await self._client.prompt(
                session_id=sub_session_id,
                message=prompt,
                timeout=min(self.timeout, 120),
            )
            if response.error:
                return None
            payload = self._extract_json_payload(response.content)
            if not payload:
                return None
            return {
                "highlights": self._normalize_summary_items(payload.get("highlights"), 4),
                "unresolved": self._normalize_summary_items(payload.get("unresolved"), 4),
                "solutions": self._normalize_summary_items(payload.get("solutions"), 4),
                "learnings": self._normalize_summary_items(payload.get("learnings"), 3),
                "resolved": self._normalize_summary_items(payload.get("resolved"), 4),
            }
        except Exception as e:
            logger.debug(f"Memory summary by agent failed, fallback to heuristic: {e}")
            return None

    async def _persist_compression_snapshot(
        self,
        channel: str,
        chat_id: str,
        reason: str,
        history_context: str,
        estimated_tokens: int = 0,
    ) -> None:
        text = (history_context or "").strip()
        if not text:
            return
        text = text.replace("<history_context>", "").replace("</history_context>", "").strip()
        if not text:
            return

        summary = await self._build_memory_summary_by_agent(channel, chat_id, reason, text)
        if not summary:
            summary = self._build_memory_summary_heuristic(text)

        highlights = self._normalize_summary_items(
            summary.get("highlights"),
            4,
            fallback="本轮触发压缩，但未提取到足够明确的重点语句。",
        )
        unresolved = self._normalize_summary_items(
            summary.get("unresolved"),
            4,
            fallback="暂无明确未解决问题（建议人工复核最新对话）。",
        )
        solutions = self._normalize_summary_items(
            summary.get("solutions"),
            4,
            fallback="本轮未检测到稳定技术方案结论。",
        )
        learnings = self._normalize_summary_items(
            summary.get("learnings"),
            3,
            fallback="本轮未检测到可沉淀的新技术要点。",
        )
        resolved_for_todo = self._normalize_summary_items(summary.get("resolved"), 4)

        unresolved_for_todo = [
            item
            for item in self._normalize_summary_items(summary.get("unresolved"), 4)
            if "暂无明确未解决问题" not in item
        ]

        workspace = self.workspace or Path.cwd()
        memory_dir = workspace / "memory"
        try:
            memory_dir.mkdir(parents=True, exist_ok=True)
            day_file = memory_dir / f"{datetime.now().strftime('%Y-%m-%d')}.md"
            ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            reason_map = {
                "active_session_rotate": "活跃会话过长，触发滚动压缩",
                "load_failed_rehydrate": "旧会话加载失败，触发历史重建",
                "invalid_request_recreate": "会话请求失效，触发重建",
                "invalid_request_recreate_stream": "流式会话请求失效，触发重建",
                "context_overflow_compact_retry": "上下文疑似超限，触发紧凑重试",
                "context_overflow_compact_retry_stream": "流式上下文疑似超限，触发紧凑重试",
            }
            reason_text = reason_map.get(reason, reason)
            highlights_block = "\n".join([f"- {x}" for x in highlights])
            unresolved_block = "\n".join([f"- {x}" for x in unresolved])
            solutions_block = "\n".join([f"- {x}" for x in solutions])
            learnings_block = "\n".join([f"- {x}" for x in learnings])
            entry = (
                f"\n\n## [{ts}] 会话压缩重点\n"
                f"- session: {channel}:{chat_id}\n"
                f"- reason: {reason_text}\n"
                f"- estimated_tokens: {estimated_tokens}\n\n"
                f"### 今日重点\n{highlights_block}\n\n"
                f"### 未解决问题\n{unresolved_block}\n\n"
                f"### 技术方案/结论\n{solutions_block}\n\n"
                f"### 新增学习\n{learnings_block}\n"
            )
            with open(day_file, "a", encoding="utf-8") as f:
                f.write(entry)
            self._sync_todo_items(
                memory_dir=memory_dir,
                channel=channel,
                chat_id=chat_id,
                unresolved_items=unresolved_for_todo,
                resolved_items=resolved_for_todo,
            )
            logger.info(
                f"Persisted compression memory summary to {day_file} for {channel}:{chat_id} ({reason})"
            )
        except Exception as e:
            logger.warning(f"Failed to persist compression snapshot: {e}")

    def _schedule_persist_compression_snapshot(
        self,
        channel: str,
        chat_id: str,
        reason: str,
        history_context: str,
        estimated_tokens: int = 0,
    ) -> None:
        async def _runner() -> None:
            # 延迟执行，优先让主会话 prompt 先走，避免回包抖动
            await asyncio.sleep(0.5)
            await self._persist_compression_snapshot(
                channel=channel,
                chat_id=chat_id,
                reason=reason,
                history_context=history_context,
                estimated_tokens=estimated_tokens,
            )

        task = asyncio.create_task(_runner())
        task.add_done_callback(
            lambda t: logger.warning(f"Background memory persistence failed: {t.exception()}")
            if t.exception()
            else None
        )

    def _sync_todo_items(
        self,
        memory_dir: Path,
        channel: str,
        chat_id: str,
        unresolved_items: list[str],
        resolved_items: list[str],
    ) -> None:
        if not unresolved_items and not resolved_items:
            return
        todo_file = memory_dir / "TODO.md"
        try:
            lines = todo_file.read_text(encoding="utf-8").splitlines() if todo_file.exists() else []
            if not lines:
                lines = ["# 未解决问题跟踪", ""]

            task_re = re.compile(r"^- \[( |x|X)\]\s+(.+)$")

            def norm(s: str) -> str:
                return " ".join((s or "").split()).strip().lower()

            def matched(a: str, b: str) -> bool:
                na, nb = norm(a), norm(b)
                if not na or not nb:
                    return False
                if na == nb:
                    return True
                if len(na) >= 6 and na in nb:
                    return True
                if len(nb) >= 6 and nb in na:
                    return True
                return False

            changed = False
            existing_tasks: list[tuple[int, bool, str]] = []
            for idx, line in enumerate(lines):
                m = task_re.match(line.strip())
                if not m:
                    continue
                done = m.group(1).lower() == "x"
                text = " ".join(m.group(2).split())
                existing_tasks.append((idx, done, text))

            # 自动勾选已完成
            for resolved in resolved_items:
                for idx, done, task_text in existing_tasks:
                    if done:
                        continue
                    if matched(resolved, task_text):
                        lines[idx] = f"- [x] {task_text}"
                        changed = True

            all_task_norm = {
                norm(task_text)
                for _, _, task_text in existing_tasks
                if task_text
            }
            new_items: list[str] = []
            for item in unresolved_items:
                clean = " ".join((item or "").split())
                if not clean:
                    continue
                if norm(clean) in all_task_norm:
                    continue
                new_items.append(clean)
                all_task_norm.add(norm(clean))

            if new_items:
                ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                if lines and lines[-1].strip():
                    lines.append("")
                lines.append(f"## {ts} ({channel}:{chat_id})")
                for item in new_items:
                    lines.append(f"- [ ] {item}")
                lines.append("")
                changed = True

            if changed:
                todo_file.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")
                logger.info(
                    f"Updated TODO file: {todo_file} (+{len(new_items)} unresolved, {len(resolved_items)} resolved candidates)"
                )
        except Exception as e:
            logger.warning(f"Failed to sync TODO items: {e}")

    def _load_memory_constraints(self) -> str:
        if self._memory_constraints_cache is not None:
            return self._memory_constraints_cache
        workspace = self.workspace or Path.cwd()
        agents_file = workspace / "AGENTS.md"
        if not agents_file.exists():
            self._memory_constraints_cache = ""
            return ""
        try:
            content = agents_file.read_text(encoding="utf-8")
        except Exception:
            self._memory_constraints_cache = ""
            return ""
        marker = "## Memory"
        start = content.find(marker)
        if start < 0:
            self._memory_constraints_cache = ""
            return ""
        tail = content[start:]
        next_idx = tail.find("\n## ", len(marker))
        section = tail if next_idx < 0 else tail[:next_idx]
        lines = [line.strip() for line in section.splitlines() if line.strip()]
        keep: list[str] = []
        for line in lines:
            lower = line.lower()
            if "daily notes" in lower or "long-term" in lower:
                keep.append(line)
            if "only load in main session" in lower:
                keep.append(line)
            if "do not load in shared contexts" in lower:
                keep.append(line)
            if "security" in lower and "shared contexts" in lower:
                keep.append(line)
        self._memory_constraints_cache = "\n".join(keep[:8]) if keep else ""
        return self._memory_constraints_cache

    def _apply_compression_constraints(self, history_context: str, channel: str, chat_id: str) -> str:
        if not history_context:
            return history_context
        constraints = self._load_memory_constraints()
        if not constraints:
            return history_context
        is_group_like = False
        chat_text = str(chat_id or "")
        if channel == "telegram" and chat_text.startswith("-"):
            is_group_like = True
        if channel in {"discord", "slack"}:
            is_group_like = True
        security_note = (
            "共享会话安全约束：禁止注入或泄露 MEMORY.md 的个人长期记忆，只保留任务相关事实。"
            if is_group_like
            else "主会话约束：可参考 memory/MEMORY.md 的长期记忆，但仅用于任务连续性。"
        )
        return (
            "<memory_constraints>\n"
            f"{constraints}\n"
            f"{security_note}\n"
            "压缩要求：保留关键决策、用户偏好、未完成事项；优先保留最近信息，去除冗余。\n"
            "</memory_constraints>\n\n"
            f"{history_context}"
        )

    def _estimate_session_history_tokens(self, session_id: str) -> int:
        session_file = self._find_session_file(session_id)
        if not session_file:
            return 0
        try:
            data = json.loads(session_file.read_text(encoding="utf-8"))
        except Exception:
            return 0
        chat_history = data.get("chatHistory") or []
        if not isinstance(chat_history, list):
            return 0
        total_chars = 0
        for chat in chat_history:
            if not isinstance(chat, dict):
                continue
            parts = chat.get("parts", [])
            if not isinstance(parts, list):
                continue
            for part in parts:
                if isinstance(part, dict):
                    total_chars += len(str(part.get("text", "")))
        return max(1, total_chars // 4)

    async def _maybe_compress_active_session(
        self,
        key: str,
        channel: str,
        chat_id: str,
        session_id: str,
        message: str,
        model: Optional[str],
    ) -> tuple[str, str]:
        estimated_tokens = self._estimate_session_history_tokens(session_id)
        if estimated_tokens < self._active_compress_trigger_tokens:
            return session_id, message

        logger.warning(
            f"Active session too large ({estimated_tokens} tokens est), rotating with compressed context: {key}"
        )
        history_context = self._extract_conversation_history(
            session_id,
            max_turns=40,
            token_budget=self._active_compress_budget_tokens,
        ) or ""
        if not history_context:
            return session_id, message

        self._schedule_persist_compression_snapshot(
            channel=channel,
            chat_id=chat_id,
            reason="active_session_rotate",
            history_context=history_context,
            estimated_tokens=estimated_tokens,
        )
        history_context = self._apply_compression_constraints(history_context, channel, chat_id)
        await self._invalidate_session(key)
        new_session_id = await self._create_new_session(key, model)
        new_message = self._inject_history_before_user_message(message, history_context)
        logger.info(f"Active session compressed and rotated for {key}")
        return new_session_id, new_message

    @staticmethod
    def _is_context_overflow_error(error_text: str) -> bool:
        text = (error_text or "").lower()
        keywords = [
            "context",
            "token",
            "too long",
            "max_tokens",
            "max token",
            "exceed",
            "length",
        ]
        return any(keyword in text for keyword in keywords)
    
    async def _get_or_create_session(
        self,
        channel: str,
        chat_id: str,
        model: Optional[str] = None,
    ) -> str:
        key = self._get_session_key(channel, chat_id)
        
        if key in self._session_map:
            session_id = self._session_map[key]
            if session_id in self._loaded_sessions:
                logger.debug(f"Reusing existing session: {key} -> {session_id[:16]}...")
                return session_id

            if not self._client:
                raise StdioACPConnectionError("StdioACP client not connected")

            loaded = await self._client.load_session(session_id)
            if loaded:
                self._loaded_sessions.add(session_id)
                logger.info(f"Loaded existing session for {key}: {session_id[:16]}...")
                return session_id

            logger.warning(f"Failed to load mapped session for {key}, recreating")
            history_context = self._extract_conversation_history(session_id) or ""
            if history_context:
                self._schedule_persist_compression_snapshot(
                    channel=channel,
                    chat_id=chat_id,
                    reason="load_failed_rehydrate",
                    history_context=history_context,
                    estimated_tokens=self._estimate_tokens(history_context),
                )
                self._rehydrate_history[key] = history_context
                logger.info(f"Queued history rehydrate for {key} before first prompt")
            await self._invalidate_session(key)
        
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
            
            system_prompt = self._build_session_system_prompt()
            session_id = await self._client.create_session(
                workspace=self.workspace,
                model=model or self.default_model,
                approval_mode="yolo",
                system_prompt=system_prompt,
            )
            
            self._session_map[key] = session_id
            self._loaded_sessions.add(session_id)
            self._save_session_map()
            logger.info(f"StdioACP session mapped: {key} -> {session_id[:16]}...")
            
            return session_id
    
    async def _invalidate_session(self, key: str) -> Optional[str]:
        old_session = self._session_map.pop(key, None)
        if old_session:
            self._loaded_sessions.discard(old_session)
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
        queued_history = self._rehydrate_history.pop(key, "")
        if queued_history:
            queued_history = self._apply_compression_constraints(queued_history, channel, chat_id)
            message = self._inject_history_before_user_message(message, queued_history)
            logger.info(f"Injected queued history for {key}")
        session_id, message = await self._maybe_compress_active_session(
            key, channel, chat_id, session_id, message, model
        )

        try:
            response = await self._client.prompt(
                session_id=session_id,
                message=message,
                timeout=timeout or self.timeout,
            )
        except StdioACPTimeoutError:
            logger.warning(f"Prompt timeout, cancel and recreate session: {key}")
            try:
                await self._client.cancel(session_id)
            except Exception as e:
                logger.debug(f"Failed to cancel timed-out session {session_id[:16]}...: {e}")

            await self._invalidate_session(key)
            session_id = await self._create_new_session(key, model)
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
                self._schedule_persist_compression_snapshot(
                    channel=channel,
                    chat_id=chat_id,
                    reason="invalid_request_recreate",
                    history_context=history_context,
                    estimated_tokens=self._estimate_tokens(history_context),
                )
                history_context = self._apply_compression_constraints(history_context, channel, chat_id)
                message = self._inject_history_before_user_message(message, history_context)
                logger.info(f"Injected conversation history before user message")
            
            response = await self._client.prompt(
                session_id=session_id,
                message=message,
                timeout=timeout or self.timeout,
            )

        if response.error and self._is_context_overflow_error(response.error):
            logger.warning(f"Context overflow suspected, recreating with compact history: {key}")
            old_session_id = await self._invalidate_session(key)
            history_context = ""
            if old_session_id:
                history_context = self._extract_conversation_history(
                    old_session_id,
                    max_turns=4,
                    token_budget=1200,
                ) or ""

            session_id = await self._create_new_session(key, model)

            retry_message = message
            if history_context:
                self._schedule_persist_compression_snapshot(
                    channel=channel,
                    chat_id=chat_id,
                    reason="context_overflow_compact_retry",
                    history_context=history_context,
                    estimated_tokens=self._estimate_tokens(history_context),
                )
                history_context = self._apply_compression_constraints(history_context, channel, chat_id)
                retry_message = self._inject_history_before_user_message(retry_message, history_context)
                logger.info("Injected compact conversation history before user message")

            response = await self._client.prompt(
                session_id=session_id,
                message=retry_message,
                timeout=timeout or self.timeout,
            )
        
        if response.error:
            if "terminated" in response.error.lower() and (response.content or "").strip():
                logger.warning("Chat returned terminated with content, returning partial content")
            else:
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
        on_event: Optional[Callable[[dict[str, Any]], Coroutine]] = None,
    ) -> str:
        if not self._client:
            raise StdioACPConnectionError("StdioACP client not connected")
        
        key = self._get_session_key(channel, chat_id)
        session_id = await self._get_or_create_session(channel, chat_id, model)
        queued_history = self._rehydrate_history.pop(key, "")
        if queued_history:
            queued_history = self._apply_compression_constraints(queued_history, channel, chat_id)
            message = self._inject_history_before_user_message(message, queued_history)
            logger.info(f"Injected queued history for {key} (stream)")
        session_id, message = await self._maybe_compress_active_session(
            key, channel, chat_id, session_id, message, model
        )
        
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

        async def handle_event(event: dict[str, Any]):
            if on_event:
                result = on_event(event)
                if asyncio.iscoroutine(result):
                    await result

        try:
            response = await self._client.prompt(
                session_id=session_id,
                message=message,
                timeout=timeout or self.timeout,
                on_chunk=handle_chunk,
                on_tool_call=handle_tool_call,
                on_event=handle_event,
            )
        except StdioACPTimeoutError:
            logger.warning(f"Stream prompt timeout, cancel and recreate session: {key}")
            try:
                await self._client.cancel(session_id)
            except Exception as e:
                logger.debug(f"Failed to cancel timed-out session {session_id[:16]}...: {e}")

            await self._invalidate_session(key)
            session_id = await self._create_new_session(key, model)
            content_parts.clear()
            response = await self._client.prompt(
                session_id=session_id,
                message=message,
                timeout=timeout or self.timeout,
                on_chunk=handle_chunk,
                on_tool_call=handle_tool_call,
                on_event=handle_event,
            )
        
        if response.error and "Invalid request" in response.error:
            logger.warning(f"Session invalid (stream), recreating: {key}")
            old_session_id = await self._invalidate_session(key)
            history_context = ""
            if old_session_id:
                history_context = self._extract_conversation_history(old_session_id) or ""
            
            session_id = await self._create_new_session(key, model)
            
            if history_context:
                self._schedule_persist_compression_snapshot(
                    channel=channel,
                    chat_id=chat_id,
                    reason="invalid_request_recreate_stream",
                    history_context=history_context,
                    estimated_tokens=self._estimate_tokens(history_context),
                )
                history_context = self._apply_compression_constraints(history_context, channel, chat_id)
                message = self._inject_history_before_user_message(message, history_context)
                logger.info(f"Injected conversation history before user message (stream)")
            
            content_parts.clear()
            response = await self._client.prompt(
                session_id=session_id,
                message=message,
                timeout=timeout or self.timeout,
                on_chunk=handle_chunk,
                on_tool_call=handle_tool_call,
                on_event=handle_event,
            )

        stream_content = "".join(content_parts) or response.content

        should_recover = False
        if response.error and self._is_context_overflow_error(response.error):
            should_recover = True
            logger.warning(f"Context overflow suspected (stream), recreating with compact history: {key}")
        elif not (stream_content or "").strip():
            should_recover = True
            logger.warning(f"Empty stream response detected, recreating with compact history: {key}")

        if should_recover:
            old_session_id = await self._invalidate_session(key)
            history_context = ""
            if old_session_id:
                history_context = self._extract_conversation_history(
                    old_session_id,
                    max_turns=4,
                    token_budget=1200,
                ) or ""

            session_id = await self._create_new_session(key, model)

            retry_message = message
            if history_context:
                self._schedule_persist_compression_snapshot(
                    channel=channel,
                    chat_id=chat_id,
                    reason="context_overflow_compact_retry_stream",
                    history_context=history_context,
                    estimated_tokens=self._estimate_tokens(history_context),
                )
                history_context = self._apply_compression_constraints(history_context, channel, chat_id)
                retry_message = self._inject_history_before_user_message(retry_message, history_context)
                logger.info("Injected compact conversation history before user message (stream)")

            content_parts.clear()
            response = await self._client.prompt(
                session_id=session_id,
                message=retry_message,
                timeout=timeout or self.timeout,
                on_chunk=handle_chunk,
                on_tool_call=handle_tool_call,
                on_event=handle_event,
            )
            stream_content = "".join(content_parts) or response.content

        if response.error:
            if "terminated" in response.error.lower() and (stream_content or "").strip():
                logger.warning("Chat stream returned terminated with content, returning partial content")
            else:
                raise StdioACPError(f"Chat error: {response.error}")
        
        return stream_content
    
    async def new_chat(
        self,
        message: str,
        channel: str = "cli",
        chat_id: str = "direct",
        model: Optional[str] = None,
        timeout: Optional[int] = None,
    ) -> str:
        self.clear_session(channel, chat_id)
        return await self.chat(message, channel, chat_id, model, timeout)
    
    async def health_check(self) -> bool:
        if self._client is None:
            return False
        return await self._client.is_connected()
    
    def clear_session(self, channel: str, chat_id: str) -> bool:
        key = self._get_session_key(channel, chat_id)
        changed = False

        old_session = self._session_map.pop(key, None)
        if old_session:
            self._loaded_sessions.discard(old_session)
            changed = True

        if key in self._rehydrate_history:
            self._rehydrate_history.pop(key, None)
            changed = True

        if changed:
            self._save_session_map()
            logger.info(f"Cleared Stdio session state for {key}")

        return changed
    
    def list_sessions(self) -> dict[str, str]:
        return self._session_map.copy()
