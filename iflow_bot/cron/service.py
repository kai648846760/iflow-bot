"""Cron service for scheduled task execution."""

from __future__ import annotations

import asyncio
import json
import time
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Coroutine, Optional

from loguru import logger

from iflow_bot.cron.types import CronJob, CronJobState, CronPayload, CronSchedule, CronStore


def _now_ms() -> int:
    """Get current timestamp in milliseconds."""
    return int(time.time() * 1000)


def _compute_next_run(schedule: CronSchedule, now_ms: int) -> Optional[int]:
    """Compute next run time in ms based on schedule type.
    
    对于一次性任务（at 类型）：
    - 如果时间未到，返回预定时间
    - 如果时间已过但在 5 分钟内，仍返回预定时间（允许延迟执行）
    - 如果时间已过超过 5 分钟，返回 None（任务太旧，跳过）
    """
    if schedule.kind == "at":
        if not schedule.at_ms:
            return None
        # 允许 5 分钟内的延迟执行
        max_delay_ms = 5 * 60 * 1000  # 5 分钟
        if schedule.at_ms > now_ms - max_delay_ms:
            return schedule.at_ms
        return None  # 任务太旧，跳过
    
    if schedule.kind == "every":
        # Interval-based
        if not schedule.every_ms or schedule.every_ms <= 0:
            return None
        return now_ms + schedule.every_ms
    
    if schedule.kind == "cron" and schedule.expr:
        # Cron expression
        try:
            from croniter import croniter
            from zoneinfo import ZoneInfo
            
            base_time = now_ms / 1000
            tz = ZoneInfo(schedule.tz) if schedule.tz else datetime.now().astimezone().tzinfo
            base_dt = datetime.fromtimestamp(base_time, tz=tz)
            cron = croniter(schedule.expr, base_dt)
            next_dt = cron.get_next(datetime)
            return int(next_dt.timestamp() * 1000)
        except ImportError:
            logger.warning("croniter not installed, falling back to simple parsing")
            return _parse_simple_cron(schedule.expr, now_ms)
        except Exception as e:
            logger.error(f"Failed to parse cron expression: {schedule.expr} - {e}")
            return None
    
    return None


def _parse_simple_cron(expr: Optional[str], now_ms: int) -> Optional[int]:
    """
    Simple cron expression parser for common patterns.
    
    Supported formats:
    - "every N" - every N seconds
    - "hourly" - every hour
    - "daily" - every day at midnight
    - "weekly" - every week
    """
    if not expr:
        return None
    
    expr = expr.strip().lower()
    
    if expr == "hourly":
        return now_ms + 3600000
    elif expr == "daily":
        now_s = now_ms // 1000
        seconds_until_midnight = 86400 - (now_s % 86400)
        return now_ms + seconds_until_midnight * 1000
    elif expr == "weekly":
        return now_ms + 7 * 86400 * 1000
    elif expr.startswith("every "):
        try:
            seconds = int(expr.split()[1])
            return now_ms + seconds * 1000
        except (IndexError, ValueError):
            return None
    
    return None


def _validate_schedule_for_add(schedule: CronSchedule) -> None:
    """Validate schedule fields."""
    if schedule.tz and schedule.kind != "cron":
        raise ValueError("tz can only be used with cron schedules")

    if schedule.kind == "cron" and schedule.tz:
        try:
            from zoneinfo import ZoneInfo
            ZoneInfo(schedule.tz)
        except Exception:
            raise ValueError(f"unknown timezone '{schedule.tz}'") from None


class CronService:
    """
    Service for managing and executing scheduled jobs.
    
    使用 Timer 模式（参考 nanobot）：
    - 不是轮询检查，而是计算下次运行时间并设置精确的定时器
    - 支持间隔（every）、一次性（at）、cron 表达式三种调度类型
    - 持久化存储任务状态
    
    Features:
    - Support for interval-based (every), one-time (at), and cron expression schedules
    - Persistent storage of job state
    - Async job execution with callback support
    - Graceful start/stop with cleanup
    """
    
    def __init__(
        self,
        store_path: Path,
        on_job: Optional[Callable[[CronJob], Coroutine[Any, Any, str | None]]] = None
    ):
        """
        Initialize the cron service.
        
        Args:
            store_path: Path to the JSON file for persisting jobs
            on_job: Callback for job execution
        """
        self.store_path = store_path
        self.on_job = on_job
        self._store: Optional[CronStore] = None
        self._timer_task: Optional[asyncio.Task] = None
        self._running = False
    
    def _load_store(self) -> CronStore:
        """Load jobs from disk."""
        if self._store:
            return self._store
        
        if self.store_path.exists():
            try:
                data = json.loads(self.store_path.read_text(encoding="utf-8"))
                self._store = CronStore.from_dict(data)
                logger.info(f"Loaded {len(self._store.jobs)} cron jobs from storage")
            except Exception as e:
                logger.warning(f"Failed to load cron store: {e}")
                self._store = CronStore()
        else:
            self._store = CronStore()
        
        return self._store
    
    def _save_store(self) -> None:
        """Save jobs to disk."""
        if not self._store:
            return
        
        self.store_path.parent.mkdir(parents=True, exist_ok=True)
        
        data = self._store.to_dict()
        self.store_path.write_text(
            json.dumps(data, indent=2, ensure_ascii=False),
            encoding="utf-8"
        )
    
    async def start(self) -> None:
        """Start the cron service."""
        self._running = True
        self._load_store()
        self._recompute_next_runs()
        self._save_store()
        self._arm_timer()
        self._start_file_watcher()
        
        job_count = len(self._store.jobs) if self._store else 0
        logger.info(f"Cron service started with {job_count} jobs")
    
    def stop(self) -> None:
        """Stop the cron service."""
        self._running = False
        if self._timer_task:
            self._timer_task.cancel()
            self._timer_task = None
        logger.info("Cron service stopped")
    
    def _start_file_watcher(self) -> None:
        """Start background task to watch for file changes."""
        async def watch_file():
            last_mtime = 0
            while self._running:
                try:
                    if self.store_path.exists():
                        current_mtime = self.store_path.stat().st_mtime
                        if current_mtime != last_mtime:
                            last_mtime = current_mtime
                            # Reload and re-arm timer
                            logger.debug("Cron: detected file change, reloading...")
                            old_count = len(self._store.jobs) if self._store else 0
                            self._store = None  # Clear cache
                            self._load_store()
                            self._recompute_next_runs()
                            new_count = len(self._store.jobs) if self._store else 0
                            if old_count != new_count:
                                logger.info(f"Cron: reloaded, {new_count} jobs")
                            self._arm_timer()
                except Exception as e:
                    logger.error(f"Cron file watcher error: {e}")
                
                await asyncio.sleep(5)  # Check every 5 seconds
        
        asyncio.create_task(watch_file())
    
    def _recompute_next_runs(self) -> None:
        """Recompute next run times for all enabled jobs."""
        if not self._store:
            return
        now = _now_ms()
        
        for job in self._store.jobs:
            if job.enabled:
                job.state.next_run_at_ms = _compute_next_run(job.schedule, now)
    
    def _get_next_wake_ms(self) -> Optional[int]:
        """Get the earliest next run time across all jobs."""
        if not self._store:
            return None
        times = [
            j.state.next_run_at_ms 
            for j in self._store.jobs 
            if j.enabled and j.state.next_run_at_ms
        ]
        return min(times) if times else None
    
    def _arm_timer(self) -> None:
        """Schedule the next timer tick."""
        if self._timer_task:
            self._timer_task.cancel()
            self._timer_task = None
        
        next_wake = self._get_next_wake_ms()
        if not next_wake or not self._running:
            return
        
        delay_ms = max(0, next_wake - _now_ms())
        delay_s = delay_ms / 1000
        
        async def tick():
            await asyncio.sleep(delay_s)
            if self._running:
                await self._on_timer()
        
        self._timer_task = asyncio.create_task(tick())
        logger.debug(f"Cron timer armed: next wake in {delay_s:.1f}s")
    
    async def _on_timer(self) -> None:
        """Handle timer tick - run due jobs."""
        if not self._store:
            return
        
        now = _now_ms()
        due_jobs = [
            j for j in self._store.jobs
            if j.enabled and j.state.next_run_at_ms and now >= j.state.next_run_at_ms
        ]
        
        for job in due_jobs:
            await self._execute_job(job)
        
        self._save_store()
        self._arm_timer()
    
    async def _execute_job(self, job: CronJob) -> Optional[str]:
        """Execute a single job."""
        start_ms = _now_ms()
        logger.info(f"Cron: executing job '{job.name}' ({job.id})")
        
        try:
            response = None
            if self.on_job:
                response = await self.on_job(job)
            
            job.state.last_status = "ok"
            job.state.last_error = None
            logger.info(f"Cron: job '{job.name}' completed")
            
        except Exception as e:
            job.state.last_status = "error"
            job.state.last_error = str(e)
            logger.error(f"Cron: job '{job.name}' failed: {e}")
        
        job.state.last_run_at_ms = start_ms
        job.updated_at_ms = _now_ms()
        
        # Handle one-shot jobs: delete after execution
        if job.schedule.kind == "at":
            self._store.jobs = [j for j in self._store.jobs if j.id != job.id]
            logger.info(f"Cron: one-time job '{job.name}' deleted after execution")
        else:
            # Compute next run for recurring jobs
            job.state.next_run_at_ms = _compute_next_run(job.schedule, _now_ms())
        
        return response
    
    # ========== Public API ==========
    
    def list_jobs(self, include_disabled: bool = False) -> list[CronJob]:
        """List all jobs."""
        store = self._load_store()
        jobs = store.jobs if include_disabled else [j for j in store.jobs if j.enabled]
        return sorted(jobs, key=lambda j: j.state.next_run_at_ms or float('inf'))
    
    def add_job(
        self,
        name: str,
        schedule: CronSchedule,
        message: str,
        deliver: bool = False,
        channel: Optional[str] = None,
        to: Optional[str] = None,
        delete_after_run: bool = False,
    ) -> CronJob:
        """Add a new job."""
        store = self._load_store()
        _validate_schedule_for_add(schedule)
        now = _now_ms()
        
        job = CronJob(
            id=str(uuid.uuid4())[:8],
            name=name,
            enabled=True,
            schedule=schedule,
            payload=CronPayload(
                kind="agent_turn",
                message=message,
                deliver=deliver,
                channel=channel,
                to=to,
            ),
            state=CronJobState(next_run_at_ms=_compute_next_run(schedule, now)),
            created_at_ms=now,
            updated_at_ms=now,
            delete_after_run=delete_after_run,
        )
        
        store.jobs.append(job)
        self._save_store()
        self._arm_timer()
        
        logger.info(f"Cron: added job '{name}' ({job.id})")
        return job
    
    def remove_job(self, job_id: str) -> bool:
        """Remove a job by ID."""
        store = self._load_store()
        before = len(store.jobs)
        store.jobs = [j for j in store.jobs if j.id != job_id]
        removed = len(store.jobs) < before
        
        if removed:
            self._save_store()
            self._arm_timer()
            logger.info(f"Cron: removed job {job_id}")
        
        return removed
    
    def enable_job(self, job_id: str, enabled: bool = True) -> Optional[CronJob]:
        """Enable or disable a job."""
        store = self._load_store()
        for job in store.jobs:
            if job.id == job_id:
                job.enabled = enabled
                job.updated_at_ms = _now_ms()
                if enabled:
                    job.state.next_run_at_ms = _compute_next_run(job.schedule, _now_ms())
                else:
                    job.state.next_run_at_ms = None
                self._save_store()
                self._arm_timer()
                return job
        return None
    
    async def run_job(self, job_id: str, force: bool = False) -> bool:
        """Manually run a job."""
        store = self._load_store()
        for job in store.jobs:
            if job.id == job_id:
                if not force and not job.enabled:
                    return False
                await self._execute_job(job)
                self._save_store()
                self._arm_timer()
                return True
        return False
    
    def get_job(self, job_id: str) -> Optional[CronJob]:
        """Get a specific job by ID."""
        store = self._load_store()
        for job in store.jobs:
            if job.id == job_id:
                return job
        return None
    
    def status(self) -> dict:
        """Get service status."""
        store = self._load_store()
        return {
            "enabled": self._running,
            "jobs": len(store.jobs),
            "next_wake_at_ms": self._get_next_wake_ms(),
        }