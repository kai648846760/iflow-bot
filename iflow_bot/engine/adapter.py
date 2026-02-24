"""IFlow CLI Adapter - 复用 iflow 原生会话管理。

核心设计：
- 使用配置中的 workspace 路径作为 iflow 工作目录
- 所有用户共享这个 workspace
- 通过会话 ID 映射区分不同用户
- iflow 会话存储在 ~/.iflow/projects/{project_hash}/ 下
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
from datetime import datetime
from pathlib import Path
from typing import Callable, Optional

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
    """IFlow CLI 适配器 - 使用配置的 workspace。
    
    所有用户共享同一个 workspace，通过会话 ID 映射区分不同用户。
    """

    def __init__(
        self,
        default_model: str = "glm-5",
        timeout: int = 300,
        iflow_path: str = "iflow",
        workspace: Optional[Path] = None,
        thinking: bool = False,
    ):
        self.default_model = default_model
        self.thinking = thinking
        self.timeout = timeout
        self.iflow_path = iflow_path
        
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
        
        logger.info(f"IFlowAdapter: workspace={self.workspace}, model={default_model}, thinking={thinking}")

    @property
    def project_hash(self) -> str:
        return hashlib.sha256(str(self.workspace.resolve()).encode()).hexdigest()[:64]

    @property
    def iflow_sessions_dir(self) -> Path:
        return Path.home() / ".iflow" / "projects" / f"-{self.project_hash}"

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
        """发送消息并获取响应。"""
        session_id = self.session_mappings.get_session_id(channel, chat_id)
        
        logger.info(f"Chat: {channel}:{chat_id} (session={session_id or 'new'})")
        
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

    async def new_chat(
        self,
        message: str,
        channel: str = "cli",
        chat_id: str = "direct",
        model: Optional[str] = None,
        timeout: Optional[int] = None,
    ) -> str:
        """开始新对话。"""
        self.session_mappings.clear_session(channel, chat_id)
        logger.info(f"Cleared session for {channel}:{chat_id}, starting fresh")
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
        for _, process in list(self._running_processes.items()):
            if process.returncode is None:
                try:
                    process.kill()
                    await process.wait()
                except:
                    pass
        self._running_processes.clear()

    async def health_check(self) -> bool:
        """检查 iflow 是否可用。"""
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
