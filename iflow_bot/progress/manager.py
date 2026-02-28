"""Progress Manager - Periodic progress monitoring for long-running tasks.

Ported from feishu-iflow-bridge/src/modules/ProgressManager.js
Original author: Wuguoshuo

Features:
- Register/unregister sessions for progress tracking
- Periodic progress summary generation
- Duration formatting
- Integration with any channel's send method
"""

import asyncio
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Callable, Coroutine, Optional

from loguru import logger


@dataclass
class ProgressSession:
    """Tracks progress for a single session."""
    channel: str
    chat_id: str
    start_time: float = field(default_factory=lambda: datetime.now().timestamp())
    last_summary_time: float = field(default_factory=lambda: datetime.now().timestamp())
    loop_count: int = 0
    last_phase: Optional[str] = None
    last_status: str = "running"


class ProgressManager:
    """Monitors long-running tasks and sends periodic progress summaries.

    Usage:
        pm = ProgressManager(interval_seconds=180)
        pm.set_send_callback(my_send_func)
        await pm.start()
        pm.register_session("session-123", "telegram", "chat-456")
        ...
        pm.update_progress("session-123", loop_count=3, phase="Generating images")
        ...
        pm.unregister_session("session-123")
        await pm.stop()
    """

    def __init__(self, interval_seconds: int = 180, enabled: bool = True):
        self.interval = interval_seconds
        self.enabled = enabled
        self._sessions: dict[str, ProgressSession] = {}
        self._task: Optional[asyncio.Task] = None
        self._send_callback: Optional[Callable[..., Coroutine]] = None

    def set_send_callback(self, callback: Callable[..., Coroutine]) -> None:
        """Set the callback for sending progress messages.

        Callback signature: async def callback(channel: str, chat_id: str, message: str)
        """
        self._send_callback = callback

    async def start(self) -> None:
        """Start the periodic progress check loop."""
        if not self.enabled:
            logger.info("Progress monitoring disabled")
            return

        if self._task and not self._task.done():
            return

        self._task = asyncio.create_task(self._check_loop())
        logger.info(f"Progress monitoring started (interval={self.interval}s)")

    async def stop(self) -> None:
        """Stop the progress check loop."""
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None
        logger.info("Progress monitoring stopped")

    def register_session(
        self, session_id: str, channel: str, chat_id: str
    ) -> None:
        """Register a session for progress monitoring."""
        self._sessions[session_id] = ProgressSession(
            channel=channel, chat_id=chat_id
        )
        logger.debug(f"Registered progress session: {session_id}")

    def unregister_session(self, session_id: str) -> None:
        """Unregister a session from progress monitoring."""
        if session_id in self._sessions:
            del self._sessions[session_id]
            logger.debug(f"Unregistered progress session: {session_id}")

    def update_progress(
        self,
        session_id: str,
        loop_count: Optional[int] = None,
        phase: Optional[str] = None,
        status: Optional[str] = None,
    ) -> None:
        """Update progress data for a session."""
        session = self._sessions.get(session_id)
        if not session:
            return

        if loop_count is not None:
            session.loop_count = loop_count
        if phase is not None:
            session.last_phase = phase
        if status is not None:
            session.last_status = status

    async def _check_loop(self) -> None:
        """Periodically check and send progress summaries."""
        while True:
            try:
                await asyncio.sleep(self.interval)
                await self._check_all_sessions()
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Progress check error: {e}")

    async def _check_all_sessions(self) -> None:
        """Check all active sessions and send summaries if needed."""
        now = datetime.now().timestamp()

        for session_id, session in list(self._sessions.items()):
            if now - session.last_summary_time >= self.interval:
                await self._send_summary(session_id, session)
                session.last_summary_time = now

    async def _send_summary(self, session_id: str, session: ProgressSession) -> None:
        """Send a progress summary for a session."""
        if not self._send_callback:
            return

        total_time = self._format_duration(
            datetime.now().timestamp() - session.start_time
        )

        message = (
            f"📊 **任务进度摘要**\n"
            f"当前阶段: {session.last_phase or '执行中'}\n"
            f"已完成循环: {session.loop_count}\n"
            f"总执行时间: {total_time}\n"
            f"最近状态: {'✅' if session.last_status == 'success' else '⏳'} {session.last_status}"
        )

        try:
            await self._send_callback(session.channel, session.chat_id, message)
            logger.debug(f"Sent progress summary for {session_id}")
        except Exception as e:
            logger.error(f"Failed to send progress summary: {e}")

    @staticmethod
    def _format_duration(seconds: float) -> str:
        """Format seconds into human-readable duration string."""
        s = int(seconds)
        hours, remainder = divmod(s, 3600)
        minutes, secs = divmod(remainder, 60)

        if hours > 0:
            return f"{hours}小时{minutes}分钟"
        elif minutes > 0:
            return f"{minutes}分钟{secs}秒"
        else:
            return f"{secs}秒"

    @property
    def active_session_count(self) -> int:
        return len(self._sessions)


# Singleton instance
progress_manager = ProgressManager()
