"""Heartbeat service - periodic agent wake-up to check for tasks."""

import asyncio
from pathlib import Path
from typing import Any, Callable, Coroutine, Optional

from loguru import logger

# Default interval: 30 minutes
DEFAULT_HEARTBEAT_INTERVAL_S = 30 * 60

# Token the agent replies with when there is nothing to report
HEARTBEAT_OK_TOKEN = "HEARTBEAT_OK"

# The prompt sent to agent during heartbeat
HEARTBEAT_PROMPT = (
    "Read HEARTBEAT.md in your workspace and follow any instructions listed there. "
    f"If nothing needs attention, reply with exactly: {HEARTBEAT_OK_TOKEN}"
)


def _is_heartbeat_empty(content: str | None) -> bool:
    """Check if HEARTBEAT.md has no actionable content."""
    if not content:
        return True
    
    # Lines to skip: empty, headers, HTML comments, empty checkboxes
    skip_patterns = {"- [ ]", "* [ ]", "- [x]", "* [x]"}
    
    for line in content.split("\n"):
        line = line.strip()
        if not line or line.startswith("#") or line.startswith("<!--") or line in skip_patterns:
            continue
        return False  # Found actionable content
    
    return True


class HeartbeatService:
    """
    心跳服务 - 定期执行 HEARTBEAT.md 中的任务
    
    工作原理:
    1. 每隔指定时间（默认 30 分钟）检查 HEARTBEAT.md 文件
    2. 如果文件中有待执行的任务，调用 on_heartbeat 回调
    3. 将结果通过 on_notify 回调发送给用户
    
    The agent reads HEARTBEAT.md from the workspace and executes any tasks
    listed there. If it has something to report, the response is forwarded
    to the user via on_notify. If nothing needs attention, the agent replies
    HEARTBEAT_OK and the response is silently dropped.
    """

    def __init__(
        self,
        workspace: Path,
        on_heartbeat: Callable[[str], Coroutine[Any, Any, str]] | None = None,
        on_notify: Callable[[str], Coroutine[Any, Any, None]] | None = None,
        interval_s: float = DEFAULT_HEARTBEAT_INTERVAL_S,
        enabled: bool = True,
    ):
        """
        初始化心跳服务
        
        Args:
            workspace: 工作目录路径
            on_heartbeat: 心跳回调函数，接收 prompt 返回响应
            on_notify: 通知回调函数，用于向用户发送消息
            interval_s: 心跳间隔（秒），默认 30 分钟
            enabled: 是否启用心跳服务
        """
        self.workspace = workspace
        self.on_heartbeat = on_heartbeat
        self.on_notify = on_notify
        self.interval_s = interval_s
        self.enabled = enabled
        self._running = False
        self._task: asyncio.Task | None = None
    
    @property
    def heartbeat_file(self) -> Path:
        """获取 HEARTBEAT.md 文件路径"""
        return self.workspace / "HEARTBEAT.md"
    
    def _read_heartbeat_file(self) -> str | None:
        """读取 HEARTBEAT.md 内容"""
        if self.heartbeat_file.exists():
            try:
                return self.heartbeat_file.read_text(encoding="utf-8")
            except Exception as e:
                logger.error("Failed to read HEARTBEAT.md: {}", e)
                return None
        return None
    
    async def start(self) -> None:
        """启动心跳服务"""
        if not self.enabled:
            logger.info("Heartbeat service disabled")
            return
        if self._running:
            logger.warning("Heartbeat service already running")
            return
        
        self._running = True
        self._task = asyncio.create_task(self._run_loop())
        logger.info(f"Heartbeat service started (interval: {self.interval_s}s)")
    
    def stop(self) -> None:
        """停止心跳服务"""
        self._running = False
        if self._task:
            self._task.cancel()
            self._task = None
        logger.info("Heartbeat service stopped")
    
    async def _run_loop(self) -> None:
        """心跳循环"""
        while self._running:
            try:
                await asyncio.sleep(self.interval_s)
                if self._running:
                    await self._tick()
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Heartbeat error: {e}")
    
    async def _tick(self) -> None:
        """执行单次心跳检查"""
        content = self._read_heartbeat_file()
        
        # 如果 HEARTBEAT.md 为空或不存在，跳过
        if _is_heartbeat_empty(content):
            logger.debug("Heartbeat: no tasks (HEARTBEAT.md empty)")
            return
        
        logger.info("Heartbeat: checking for tasks...")
        
        if self.on_heartbeat:
            try:
                response = await self.on_heartbeat(HEARTBEAT_PROMPT)
                if HEARTBEAT_OK_TOKEN in response.upper():
                    logger.info("Heartbeat: OK (nothing to report)")
                else:
                    logger.info("Heartbeat: completed, delivering response")
                    if self.on_notify:
                        await self.on_notify(response)
            except Exception as e:
                logger.error(f"Heartbeat execution failed: {e}")
    
    async def trigger_now(self) -> str | None:
        """
        手动触发一次心跳
        
        Returns:
            心跳响应，如果没有 on_heartbeat 回调则返回 None
        """
        if self.on_heartbeat:
            return await self.on_heartbeat(HEARTBEAT_PROMPT)
        return None
    
    def is_running(self) -> bool:
        """检查心跳服务是否正在运行"""
        return self._running and self._task is not None and not self._task.done()
