"""Cron service for scheduled task execution."""

import asyncio
import json
import time
from pathlib import Path
from typing import Optional, Callable, Awaitable

from loguru import logger

from iflow_bot.cron.types import CronJob, ScheduleKind


class CronService:
    """
    Cron service for managing and executing scheduled tasks.
    
    Features:
    - Support for interval-based (EVERY), cron expression (CRON), and one-time (ONCE) schedules
    - Persistent storage of job state
    - Async job execution with callback support
    - Graceful start/stop with cleanup
    
    Usage:
        service = CronService(Path("data/cron_jobs.json"))
        service.on_job = my_job_handler
        
        # Add a job
        job = CronJob.create(
            name="Daily Report",
            schedule=Schedule(kind=ScheduleKind.EVERY, every_ms=86400000),
            payload=CronPayload(message="Daily report time!", channel="telegram", to="user123"),
        )
        await service.add_job(job)
        
        # Start the service
        await service.start()
        
        # Later, stop the service
        service.stop()
    """
    
    def __init__(self, store_path: Path):
        """
        Initialize the cron service.
        
        Args:
            store_path: Path to the JSON file for persisting jobs
        """
        self.store_path = store_path
        self._jobs: dict[str, CronJob] = {}
        self._running = False
        self._task: Optional[asyncio.Task] = None
        self.on_job: Optional[Callable[[CronJob], Awaitable[str | None]]] = None
        self._check_interval = 1.0  # Check every second
        
        # Ensure parent directory exists
        self.store_path.parent.mkdir(parents=True, exist_ok=True)
    
    async def start(self) -> None:
        """Start the cron service."""
        if self._running:
            logger.warning("Cron service is already running")
            return
        
        # Load persisted jobs
        await self._load_jobs()
        
        self._running = True
        self._task = asyncio.create_task(self._run_loop())
        logger.info(f"Cron service started with {len(self._jobs)} jobs")
    
    def stop(self) -> None:
        """Stop the cron service."""
        if not self._running:
            return
        
        self._running = False
        
        if self._task:
            self._task.cancel()
            try:
                asyncio.get_event_loop().run_until_complete(self._task)
            except asyncio.CancelledError:
                pass
            self._task = None
        
        # Persist jobs before stopping
        asyncio.create_task(self._save_jobs())
        logger.info("Cron service stopped")
    
    async def add_job(self, job: CronJob) -> str:
        """
        Add a new cron job.
        
        Args:
            job: The cron job to add
        
        Returns:
            The job ID
        """
        # Calculate next run time
        job.state.next_run_at_ms = self._calculate_next_run(job)
        
        self._jobs[job.id] = job
        await self._save_jobs()
        
        logger.info(f"Added cron job: {job.name} (id={job.id}, next_run={job.state.next_run_at_ms})")
        return job.id
    
    async def remove_job(self, job_id: str) -> bool:
        """
        Remove a cron job.
        
        Args:
            job_id: The job ID to remove
        
        Returns:
            True if the job was removed, False if not found
        """
        if job_id not in self._jobs:
            return False
        
        del self._jobs[job_id]
        await self._save_jobs()
        
        logger.info(f"Removed cron job: {job_id}")
        return True
    
    async def enable_job(self, job_id: str, enabled: bool = True) -> bool:
        """
        Enable or disable a cron job.
        
        Args:
            job_id: The job ID
            enabled: True to enable, False to disable
        
        Returns:
            True if successful, False if job not found
        """
        if job_id not in self._jobs:
            return False
        
        self._jobs[job_id].enabled = enabled
        
        if enabled:
            # Recalculate next run time when re-enabling
            self._jobs[job_id].state.next_run_at_ms = self._calculate_next_run(self._jobs[job_id])
        
        await self._save_jobs()
        logger.info(f"{'Enabled' if enabled else 'Disabled'} cron job: {job_id}")
        return True
    
    def get_job(self, job_id: str) -> Optional[CronJob]:
        """
        Get a specific cron job.
        
        Args:
            job_id: The job ID
        
        Returns:
            The cron job or None if not found
        """
        return self._jobs.get(job_id)
    
    def list_jobs(self, include_disabled: bool = False) -> list[CronJob]:
        """
        List all cron jobs.
        
        Args:
            include_disabled: Whether to include disabled jobs
        
        Returns:
            List of cron jobs
        """
        if include_disabled:
            return list(self._jobs.values())
        return [job for job in self._jobs.values() if job.enabled]
    
    def status(self) -> dict:
        """
        Get the service status.
        
        Returns:
            Status dictionary with service metrics
        """
        enabled_jobs = [j for j in self._jobs.values() if j.enabled]
        now_ms = int(time.time() * 1000)
        
        return {
            "running": self._running,
            "total_jobs": len(self._jobs),
            "enabled_jobs": len(enabled_jobs),
            "jobs": [
                {
                    "id": job.id,
                    "name": job.name,
                    "enabled": job.enabled,
                    "next_run_in_ms": job.state.next_run_at_ms - now_ms if job.state.next_run_at_ms else None,
                    "last_error": job.state.last_error,
                    "run_count": job.state.run_count,
                }
                for job in enabled_jobs
            ],
        }
    
    async def trigger_job(self, job_id: str) -> Optional[str]:
        """
        Manually trigger a job execution.
        
        Args:
            job_id: The job ID to trigger
        
        Returns:
            Result from the job handler or None
        """
        job = self._jobs.get(job_id)
        if not job:
            logger.warning(f"Job not found: {job_id}")
            return None
        
        return await self._execute_job(job)
    
    # Private methods
    
    async def _run_loop(self) -> None:
        """Main loop for checking and executing jobs."""
        logger.debug("Cron service loop started")
        
        while self._running:
            try:
                await self._check_jobs()
                await asyncio.sleep(self._check_interval)
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Error in cron loop: {e}")
                await asyncio.sleep(self._check_interval)
        
        logger.debug("Cron service loop ended")
    
    async def _check_jobs(self) -> None:
        """Check all jobs and execute due ones."""
        now_ms = int(time.time() * 1000)
        
        for job in self._jobs.values():
            if not job.enabled:
                continue
            
            if job.state.next_run_at_ms and now_ms >= job.state.next_run_at_ms:
                await self._execute_job(job)
    
    async def _execute_job(self, job: CronJob) -> Optional[str]:
        """
        Execute a cron job.
        
        Args:
            job: The job to execute
        
        Returns:
            Result from the job handler
        """
        logger.info(f"Executing cron job: {job.name} (id={job.id})")
        
        result = None
        now_ms = int(time.time() * 1000)
        
        try:
            # Update state before execution
            job.state.last_run_at_ms = now_ms
            
            # Execute the job handler
            if self.on_job:
                result = await self.on_job(job)
            
            # Clear error on success
            job.state.last_error = None
            job.state.run_count += 1
            
            logger.info(f"Cron job completed: {job.name} (runs={job.state.run_count})")
            
        except Exception as e:
            job.state.last_error = str(e)
            logger.error(f"Cron job failed: {job.name} - {e}")
        
        finally:
            # Calculate next run time
            if job.schedule.kind == ScheduleKind.ONCE:
                # One-time jobs are disabled after execution
                job.enabled = False
                job.state.next_run_at_ms = None
                logger.info(f"One-time job completed and disabled: {job.name}")
            else:
                job.state.next_run_at_ms = self._calculate_next_run(job)
            
            await self._save_jobs()
        
        return result
    
    def _calculate_next_run(self, job: CronJob) -> Optional[int]:
        """
        Calculate the next run timestamp for a job.
        
        Args:
            job: The job to calculate for
        
        Returns:
            Next run timestamp in milliseconds or None
        """
        now_ms = int(time.time() * 1000)
        
        if job.schedule.kind == ScheduleKind.EVERY:
            if not job.schedule.every_ms:
                return None
            
            # Calculate next run based on interval
            last_run = job.state.last_run_at_ms or now_ms
            return last_run + job.schedule.every_ms
        
        elif job.schedule.kind == ScheduleKind.CRON:
            # For cron expressions, use a simplified implementation
            # In production, consider using croniter library
            try:
                return self._parse_cron_expression(job.schedule.expr, now_ms)
            except Exception as e:
                logger.error(f"Failed to parse cron expression: {job.schedule.expr} - {e}")
                return None
        
        elif job.schedule.kind == ScheduleKind.ONCE:
            # For one-time jobs, run immediately if never run
            if job.state.last_run_at_ms is None:
                return now_ms
            return None
        
        return None
    
    def _parse_cron_expression(self, expr: Optional[str], now_ms: int) -> Optional[int]:
        """
        Parse a cron expression and calculate next run time.
        
        This is a simplified implementation. For production use,
        consider using the croniter library for full cron support.
        
        Supported formats:
        - "every N" - every N seconds
        - "hourly" - every hour
        - "daily" - every day at midnight
        - "weekly" - every week
        
        Args:
            expr: The cron expression
            now_ms: Current timestamp in milliseconds
        
        Returns:
            Next run timestamp in milliseconds
        """
        if not expr:
            return None
        
        expr = expr.strip().lower()
        
        # Simple keyword parsing
        if expr == "hourly":
            # Next hour
            return now_ms + 3600000
        
        elif expr == "daily":
            # Next day at midnight
            now_s = now_ms // 1000
            seconds_until_midnight = 86400 - (now_s % 86400)
            return now_ms + seconds_until_midnight * 1000
        
        elif expr == "weekly":
            # Next week
            return now_ms + 7 * 86400 * 1000
        
        elif expr.startswith("every "):
            # "every N" format (N in seconds)
            try:
                seconds = int(expr.split()[1])
                return now_ms + seconds * 1000
            except (IndexError, ValueError):
                return None
        
        # For full cron expressions (e.g., "0 9 * * 1-5"),
        # return a default interval of 1 minute for safety
        # In production, integrate croniter for proper parsing
        logger.warning(f"Complex cron expression not fully supported: {expr}, defaulting to 1 minute interval")
        return now_ms + 60000
    
    async def _load_jobs(self) -> None:
        """Load jobs from persistent storage."""
        if not self.store_path.exists():
            logger.debug("No existing cron jobs file found")
            return
        
        try:
            content = self.store_path.read_text(encoding="utf-8")
            data = json.loads(content)
            
            self._jobs.clear()
            for job_data in data.get("jobs", []):
                try:
                    job = CronJob.from_dict(job_data)
                    self._jobs[job.id] = job
                except Exception as e:
                    logger.error(f"Failed to load job: {e}")
            
            logger.info(f"Loaded {len(self._jobs)} cron jobs from storage")
            
        except Exception as e:
            logger.error(f"Failed to load cron jobs: {e}")
    
    async def _save_jobs(self) -> None:
        """Save jobs to persistent storage."""
        try:
            data = {
                "jobs": [job.to_dict() for job in self._jobs.values()],
                "updated_at": int(time.time() * 1000),
            }
            
            content = json.dumps(data, indent=2, ensure_ascii=False)
            self.store_path.write_text(content, encoding="utf-8")
            
            logger.debug(f"Saved {len(self._jobs)} cron jobs to storage")
            
        except Exception as e:
            logger.error(f"Failed to save cron jobs: {e}")
