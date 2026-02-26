"""IFlow CLI Adapter - 复用 iflow 原生会话管理。

核心设计：
- 使用配置中的 workspace 路径作为 iflow 工作目录
- 所有用户共享这个 workspace
- 通过会话 ID 映射区分不同用户
- iflow 会话存储在 ~/.iflow/projects/{project_hash}/ 下
- 支持两种通信模式：CLI (子进程) 和 ACP (WebSocket)
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
from datetime import datetime
from pathlib import Path
from typing import Callable, Literal, Optional

from loguru import logger


class IFlowAdapterError(Exception):
    """IFlow 适配器错误基类。"""
    pass


class IFlowTimeoutError(IFlowAdapterError):
    """IFlow 命令超时错误。"""
    pass


class IFlowProcessError(IFlowAdapterError):
    """IFlow 进程执行错误。"""
    pass


class SessionMappingManager:
    """管理渠道用户到 iflow 会话 ID 的映射。
    
    存储格式:
    {
        "telegram:123456": "session-abc123...",
        "discord:789012": "session-def456...",
    }
    """

    def __init__(self, mapping_file: Optional[Path] = None):
        self.mapping_file = mapping_file or Path.home() / ".iflow-bot" / "session_mappings.json"
        self.mapping_file.parent.mkdir(parents=True, exist_ok=True)
        self._mappings: dict[str, str] = {}
        self._load()

    def _load(self) -> None:
        if self.mapping_file.exists():
            try:
                with open(self.mapping_file, "r", encoding="utf-8") as f:
                    self._mappings = json.load(f)
                logger.debug(f"Loaded {len(self._mappings)} session mappings")
            except json.JSONDecodeError:
                logger.warning("Invalid mapping file, starting fresh")
                self._mappings = {}

    def _save(self) -> None:
        with open(self.mapping_file, "w", encoding="utf-8") as f:
            json.dump(self._mappings, f, indent=2, ensure_ascii=False)

    def get_session_id(self, channel: str, chat_id: str) -> Optional[str]:
        key = f"{channel}:{chat_id}"
        return self._mappings.get(key)

    def set_session_id(self, channel: str, chat_id: str, session_id: str) -> None:
        key = f"{channel}:{chat_id}"
        self._mappings[key] = session_id
        self._save()
        logger.debug(f"Session mapping: {key} -> {session_id}")

    def clear_session(self, channel: str, chat_id: str) -> bool:
        key = f"{channel}:{chat_id}"
        if key in self._mappings:
            del self._mappings[key]
            self._save()
            return True
        return False

    def list_all(self) -> dict[str, str]:
        return self._mappings.copy()


class IFlowAdapter:
    """IFlow CLI 适配器 - 支持三种通信模式。
    
    模式：
    - cli: 通过子进程调用 iflow 命令（默认）
    - acp: 通过 WebSocket 连接 iflow ACP 服务
    - stdio: 通过 stdio 直接与 iflow --experimental-acp 通信
    
    所有用户共享同一个 workspace，通过会话 ID 映射区分不同用户。
    """

    def __init__(
        self,
        default_model: str = "glm-5",
        timeout: int = 300,
        iflow_path: str = "iflow",
        workspace: Optional[Path] = None,
        thinking: bool = False,
        mode: Literal["cli", "acp", "stdio"] = "cli",
        acp_host: str = "localhost",
        acp_port: int = 8090,
    ):
        self.default_model = default_model
        self.thinking = thinking
        self.timeout = timeout
        self.iflow_path = iflow_path
        self.mode = mode
        self.acp_host = acp_host
        self.acp_port = acp_port
        
        # workspace 是 iflow 执行的工作目录
        if workspace:
            ws = str(workspace)
            if ws.startswith("~"):
                ws = str(Path.home() / ws[2:])
            self.workspace = Path(ws).resolve()
        else:
            self.workspace = Path.home() / ".iflow-bot" / "workspace"
        
        self.workspace.mkdir(parents=True, exist_ok=True)
        
        self.session_mappings = SessionMappingManager()
        self._running_processes: dict[str, asyncio.subprocess.Process] = {}
        
        # ACP 模式适配器（懒加载）
        self._acp_adapter: Optional["ACPAdapter"] = None
        # Stdio ACP 模式适配器（懒加载）
        self._stdio_adapter: Optional["StdioACPAdapter"] = None
        
        logger.info(f"IFlowAdapter: mode={mode}, workspace={self.workspace}, model={default_model}, thinking={thinking}")
    
    @property
    def project_hash(self) -> str:
        return hashlib.sha256(str(self.workspace.resolve()).encode()).hexdigest()[:64]

    @property
    def iflow_sessions_dir(self) -> Path:
        return Path.home() / ".iflow" / "projects" / f"-{self.project_hash}"
    
    async def _get_acp_adapter(self) -> "ACPAdapter":
        """获取或创建 ACP 适配器。"""
        if self._acp_adapter is None:
            from iflow_bot.engine.acp import ACPAdapter
            self._acp_adapter = ACPAdapter(
                host=self.acp_host,
                port=self.acp_port,
                timeout=self.timeout,
                workspace=self.workspace,
                default_model=self.default_model,
                thinking=self.thinking,
            )
            await self._acp_adapter.connect()
            logger.info(f"ACP adapter connected: {self.acp_host}:{self.acp_port}")
        return self._acp_adapter

    async def _get_stdio_adapter(self) -> "StdioACPAdapter":
        """获取或创建 Stdio ACP 适配器。"""
        if self._stdio_adapter is None:
            from iflow_bot.engine.stdio_acp import StdioACPAdapter
            self._stdio_adapter = StdioACPAdapter(
                iflow_path=self.iflow_path,
                workspace=self.workspace,
                timeout=self.timeout,
                default_model=self.default_model,
                thinking=self.thinking,
            )
            await self._stdio_adapter.connect()
            logger.info(f"StdioACP adapter connected")
        return self._stdio_adapter

    def list_iflow_sessions(self) -> list[dict]:
        sessions_dir = self.iflow_sessions_dir
        if not sessions_dir.exists():
            return []
        
        sessions = []
        for session_file in sessions_dir.glob("session-*.jsonl"):
            try:
                stat = session_file.stat()
                session_id = session_file.stem
                
                with open(session_file, "r", encoding="utf-8") as f:
                    lines = f.readlines()
                    message_count = len([l for l in lines if l.strip()])
                
                first_msg = None
                last_msg = None
                if lines:
                    try:
                        first = json.loads(lines[0])
                        first_msg = first.get("timestamp")
                        last = json.loads(lines[-1])
                        last_msg = last.get("timestamp")
                    except:
                        pass
                
                sessions.append({
                    "id": session_id,
                    "file": str(session_file),
                    "created_at": first_msg or datetime.fromtimestamp(stat.st_ctime).isoformat(),
                    "updated_at": last_msg or datetime.fromtimestamp(stat.st_mtime).isoformat(),
                    "message_count": message_count,
                })
            except Exception as e:
                logger.debug(f"Error reading session {session_file}: {e}")
        
        sessions.sort(key=lambda x: x["updated_at"], reverse=True)
        return sessions

    def _filter_progress_output(self, output: str) -> str:
        """过滤 iflow 输出中的进度信息。"""
        if not output:
            return ""
        
        lines = output.split("\n")
        filtered_lines = []
        in_execution_info = False
        
        for line in lines:
            stripped = line.strip()
            
            if stripped.startswith("<Execution Info>") or stripped.startswith("〈Execution Info〉"):
                in_execution_info = True
                continue
            if stripped.startswith("</Execution Info>") or stripped.startswith("〈/Execution Info〉"):
                in_execution_info = False
                continue
            if in_execution_info:
                continue
            
            # 跳过进度和思考消息
            if stripped in ["Thinking...", "正在思考...", "Processing..."]:
                continue
            if stripped.startswith("[") and stripped.endswith("]"):
                continue
            if stripped.startswith("ℹ️") and "Resuming session" in stripped:
                continue
            
            filtered_lines.append(line)
        
        return "\n".join(filtered_lines).strip()

    def _extract_session_id_from_output(self, output: str) -> Optional[str]:
        """从 iflow 输出中提取会话 ID。"""
        import re
        match = re.search(r'"session-id"\s*:\s*"(session-[^"]+)"', output)
        if match:
            return match.group(1)
        return None

    async def _build_command(
        self,
        message: str,
        model: Optional[str] = None,
        session_id: Optional[str] = None,
        continue_session: bool = False,
        yolo: bool = True,
        thinking: bool = False,
    ) -> list[str]:
        """构建 iflow 命令。"""
        cmd = [self.iflow_path]
        
        if model:
            cmd.extend(["-m", model])
        
        if session_id:
            cmd.extend(["-r", session_id])
        elif continue_session:
            cmd.append("-c")
        
        if yolo:
            cmd.append("-y")
        
        if thinking:
            cmd.append("--thinking")
        
        cmd.extend(["-p", message])
        return cmd

    async def _run_process(
        self,
        cmd: list[str],
        timeout: Optional[int] = None,
    ) -> tuple[str, str]:
        """运行 iflow 子进程。"""
        timeout = timeout or self.timeout
        
        logger.debug(f"Running: {' '.join(cmd)} in {self.workspace}")
        
        process = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=str(self.workspace),
        )
        
        self._running_processes[str(id(process))] = process
        
        stdout_lines: list[str] = []
        stderr_lines: list[str] = []

        async def read_stream(stream, lines):
            while True:
                line = await stream.readline()
                if not line:
                    break
                decoded = line.decode("utf-8", errors="replace").rstrip("\n\r")
                lines.append(decoded)

        try:
            await asyncio.wait_for(
                asyncio.gather(
                    read_stream(process.stdout, stdout_lines),
                    read_stream(process.stderr, stderr_lines),
                ),
                timeout=timeout,
            )
        except asyncio.TimeoutError:
            process.kill()
            await process.wait()
            raise IFlowTimeoutError(f"Timeout after {timeout}s")

        await process.wait()
        
        return "\n".join(stdout_lines), "\n".join(stderr_lines)

    async def chat(
        self,
        message: str,
        channel: str = "cli",
        chat_id: str = "direct",
        model: Optional[str] = None,
        timeout: Optional[int] = None,
    ) -> str:
        """发送消息并获取响应。
        
        根据模式选择不同的通信方式：
        - cli: 通过子进程调用 iflow 命令
        - acp: 通过 WebSocket 连接 iflow ACP 服务
        - stdio: 通过 stdio 直接与 iflow --experimental-acp 通信
        """
        if self.mode == "acp":
            return await self._chat_acp(message, channel, chat_id, model, timeout)
        elif self.mode == "stdio":
            return await self._chat_stdio(message, channel, chat_id, model, timeout)
        else:
            return await self._chat_cli(message, channel, chat_id, model, timeout)
    
    async def _chat_cli(
        self,
        message: str,
        channel: str = "cli",
        chat_id: str = "direct",
        model: Optional[str] = None,
        timeout: Optional[int] = None,
    ) -> str:
        """CLI 模式：通过子进程调用 iflow 命令。"""
        session_id = self.session_mappings.get_session_id(channel, chat_id)
        
        logger.info(f"Chat (CLI): {channel}:{chat_id} (session={session_id or 'new'})")
        
        cmd = await self._build_command(
            message=message,
            model=model or self.default_model,
            session_id=session_id,
            continue_session=False,
            thinking=self.thinking,
        )
        
        stdout, stderr = await self._run_process(cmd, timeout=timeout)
        
        combined_output = stdout + "\n" + stderr
        
        extracted_session_id = self._extract_session_id_from_output(combined_output)
        
        if not session_id and extracted_session_id:
            self.session_mappings.set_session_id(channel, chat_id, extracted_session_id)
            logger.info(f"New session: {channel}:{chat_id} -> {extracted_session_id}")
        
        response = self._filter_progress_output(stdout.strip())
        
        return response
    
    async def _chat_acp(
        self,
        message: str,
        channel: str = "cli",
        chat_id: str = "direct",
        model: Optional[str] = None,
        timeout: Optional[int] = None,
    ) -> str:
        """ACP 模式：通过 WebSocket 连接 iflow ACP 服务。"""
        adapter = await self._get_acp_adapter()
        
        logger.info(f"Chat (ACP): {channel}:{chat_id}")
        
        response = await adapter.chat(
            message=message,
            channel=channel,
            chat_id=chat_id,
            model=model or self.default_model,
            timeout=timeout or self.timeout,
        )
        
        return response

    async def _chat_stdio(
        self,
        message: str,
        channel: str = "cli",
        chat_id: str = "direct",
        model: Optional[str] = None,
        timeout: Optional[int] = None,
    ) -> str:
        """Stdio 模式：通过 stdio 直接与 iflow --experimental-acp 通信。"""
        adapter = await self._get_stdio_adapter()
        
        logger.info(f"Chat (Stdio): {channel}:{chat_id}")
        
        response = await adapter.chat(
            message=message,
            channel=channel,
            chat_id=chat_id,
            model=model or self.default_model,
            timeout=timeout or self.timeout,
        )
        
        return response

    async def chat_stream(
        self,
        message: str,
        channel: str = "cli",
        chat_id: str = "direct",
        model: Optional[str] = None,
        timeout: Optional[int] = None,
        on_chunk: Optional[Callable] = None,
    ) -> str:
        """
        发送消息并流式获取响应。
        
        ACP 和 Stdio 模式支持流式输出，CLI 模式会回退到普通 chat。
        
        Args:
            message: 用户消息
            channel: 渠道名称
            chat_id: 聊天 ID
            model: 模型名称
            timeout: 超时时间
            on_chunk: 消息块回调，接收 (channel, chat_id, chunk_text)
        
        Returns:
            最终响应文本
        """
        if self.mode == "acp" or self.mode == "stdio":
            if self.mode == "acp":
                adapter = await self._get_acp_adapter()
                from iflow_bot.engine.acp import AgentMessageChunk
                logger.info(f"Chat Stream (ACP): {channel}:{chat_id}")
            else:
                adapter = await self._get_stdio_adapter()
                from iflow_bot.engine.stdio_acp import AgentMessageChunk
                logger.info(f"Chat Stream (Stdio): {channel}:{chat_id}")
            
            chunk_count = 0
            
            async def handle_chunk(chunk: AgentMessageChunk):
                nonlocal chunk_count
                if not chunk.is_thought and chunk.text and on_chunk:
                    chunk_count += 1
                    logger.debug(f"Stream chunk #{chunk_count}: {len(chunk.text)} chars")
                    await on_chunk(channel, chat_id, chunk.text)
            
            response = await adapter.chat_stream(
                message=message,
                channel=channel,
                chat_id=chat_id,
                model=model or self.default_model,
                timeout=timeout or self.timeout,
                on_chunk=handle_chunk,
            )
            
            logger.info(f"Chat stream completed: {chunk_count} chunks sent")
            
            return response
        else:
            return await self.chat(message, channel, chat_id, model, timeout)

    async def new_chat(
        self,
        message: str,
        channel: str = "cli",
        chat_id: str = "direct",
        model: Optional[str] = None,
        timeout: Optional[int] = None,
    ) -> str:
        """开始新对话。"""
        if self.mode == "acp":
            adapter = await self._get_acp_adapter()
            adapter.clear_session(channel, chat_id)
            logger.info(f"Cleared ACP session for {channel}:{chat_id}, starting fresh")
            return await adapter.new_chat(
                message=message,
                channel=channel,
                chat_id=chat_id,
                model=model or self.default_model,
                timeout=timeout or self.timeout,
            )
        elif self.mode == "stdio":
            adapter = await self._get_stdio_adapter()
            adapter.clear_session(channel, chat_id)
            logger.info(f"Cleared Stdio session for {channel}:{chat_id}, starting fresh")
            return await adapter.new_chat(
                message=message,
                channel=channel,
                chat_id=chat_id,
                model=model or self.default_model,
                timeout=timeout or self.timeout,
            )
        else:
            self.session_mappings.clear_session(channel, chat_id)
            logger.info(f"Cleared CLI session for {channel}:{chat_id}, starting fresh")
            return await self.chat(message, channel, chat_id, model, timeout)

    async def run_iflow_command(
        self,
        args: list[str],
        timeout: Optional[int] = None,
    ) -> tuple[str, str]:
        """直接运行 iflow 命令。"""
        cmd = [self.iflow_path] + args
        return await self._run_process(cmd, timeout=timeout)

    async def close(self) -> None:
        """清理资源。"""
        # 清理 CLI 进程
        for _, process in list(self._running_processes.items()):
            if process.returncode is None:
                try:
                    process.kill()
                    await process.wait()
                except:
                    pass
        self._running_processes.clear()
        
        # 断开 ACP 连接
        if self._acp_adapter:
            try:
                await self._acp_adapter.disconnect()
            except Exception as e:
                logger.warning(f"Failed to disconnect ACP adapter: {e}")
            self._acp_adapter = None
        
        # 断开 Stdio ACP 连接
        if self._stdio_adapter:
            try:
                await self._stdio_adapter.disconnect()
            except Exception as e:
                logger.warning(f"Failed to disconnect Stdio adapter: {e}")
            self._stdio_adapter = None

    async def health_check(self) -> bool:
        """检查 iflow 是否可用。"""
        if self.mode == "acp":
            if self._acp_adapter:
                return await self._acp_adapter.health_check()
            return False
        elif self.mode == "stdio":
            if self._stdio_adapter:
                return await self._stdio_adapter.health_check()
            return False
        else:
            try:
                process = await asyncio.create_subprocess_exec(
                    self.iflow_path, "--version",
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                )
                await asyncio.wait_for(process.wait(), timeout=10)
                return process.returncode == 0
            except:
                return False
