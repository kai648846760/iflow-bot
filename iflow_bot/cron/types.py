"""Cron job types for iflow-bot."""

from dataclasses import dataclass, field
from typing import Optional
from enum import Enum
import uuid


class ScheduleKind(str, Enum):
    """Schedule type for cron jobs."""
    
    EVERY = "every"     # 每隔 N 毫秒
    CRON = "cron"       # cron 表达式
    ONCE = "once"       # 一次性任务


@dataclass
class Schedule:
    """
    Schedule configuration for a cron job.
    
    Supports three schedule types:
    - EVERY: Run every N milliseconds
    - CRON: Run based on cron expression
    - ONCE: Run once at a specific time
    """
    kind: ScheduleKind
    """Schedule type"""
    
    every_ms: Optional[int] = None
    """Interval in milliseconds (for EVERY type)"""
    
    expr: Optional[str] = None
    """Cron expression (for CRON type, e.g., '0 9 * * 1-5' for 9 AM weekdays)"""
    
    tz: Optional[str] = None
    """Timezone (e.g., 'Asia/Shanghai', 'UTC')"""


@dataclass
class CronPayload:
    """
    Payload for a cron job execution.
    
    Defines what should happen when the job is triggered.
    """
    message: str
    """Message content to send"""
    
    channel: Optional[str] = None
    """Target channel (telegram, discord, etc.)"""
    
    to: Optional[str] = None
    """Target chat/user identifier"""
    
    deliver: bool = False
    """Whether to deliver the message through the channel"""


@dataclass
class CronJobState:
    """
    Runtime state for a cron job.
    
    Tracks execution history and errors.
    """
    last_run_at_ms: Optional[int] = None
    """Last execution timestamp in milliseconds"""
    
    next_run_at_ms: Optional[int] = None
    """Next scheduled execution timestamp in milliseconds"""
    
    last_error: Optional[str] = None
    """Last error message if execution failed"""
    
    run_count: int = 0
    """Total number of successful executions"""


@dataclass
class CronJob:
    """
    A cron job definition with schedule and payload.
    
    Represents a scheduled task that can send messages
    through configured channels at specified intervals.
    """
    id: str
    """Unique job identifier"""
    
    name: str
    """Human-readable job name"""
    
    schedule: Schedule
    """Schedule configuration"""
    
    payload: CronPayload
    """Execution payload"""
    
    state: CronJobState = field(default_factory=CronJobState)
    """Runtime state"""
    
    enabled: bool = True
    """Whether the job is enabled"""
    
    created_at_ms: Optional[int] = None
    """Job creation timestamp in milliseconds"""
    
    @classmethod
    def create(
        cls,
        name: str,
        schedule: Schedule,
        payload: CronPayload,
        job_id: Optional[str] = None,
        enabled: bool = True,
    ) -> "CronJob":
        """
        Create a new CronJob with auto-generated ID.
        
        Args:
            name: Human-readable job name
            schedule: Schedule configuration
            payload: Execution payload
            job_id: Optional custom ID (auto-generated if not provided)
            enabled: Whether the job is enabled
        
        Returns:
            A new CronJob instance
        """
        import time
        
        return cls(
            id=job_id or str(uuid.uuid4()),
            name=name,
            schedule=schedule,
            payload=payload,
            enabled=enabled,
            created_at_ms=int(time.time() * 1000),
        )
    
    def to_dict(self) -> dict:
        """Convert to dictionary for serialization."""
        return {
            "id": self.id,
            "name": self.name,
            "schedule": {
                "kind": self.schedule.kind.value,
                "every_ms": self.schedule.every_ms,
                "expr": self.schedule.expr,
                "tz": self.schedule.tz,
            },
            "payload": {
                "message": self.payload.message,
                "channel": self.payload.channel,
                "to": self.payload.to,
                "deliver": self.payload.deliver,
            },
            "state": {
                "last_run_at_ms": self.state.last_run_at_ms,
                "next_run_at_ms": self.state.next_run_at_ms,
                "last_error": self.state.last_error,
                "run_count": self.state.run_count,
            },
            "enabled": self.enabled,
            "created_at_ms": self.created_at_ms,
        }
    
    @classmethod
    def from_dict(cls, data: dict) -> "CronJob":
        """Create from dictionary."""
        return cls(
            id=data["id"],
            name=data["name"],
            schedule=Schedule(
                kind=ScheduleKind(data["schedule"]["kind"]),
                every_ms=data["schedule"].get("every_ms"),
                expr=data["schedule"].get("expr"),
                tz=data["schedule"].get("tz"),
            ),
            payload=CronPayload(
                message=data["payload"]["message"],
                channel=data["payload"].get("channel"),
                to=data["payload"].get("to"),
                deliver=data["payload"].get("deliver", False),
            ),
            state=CronJobState(
                last_run_at_ms=data["state"].get("last_run_at_ms"),
                next_run_at_ms=data["state"].get("next_run_at_ms"),
                last_error=data["state"].get("last_error"),
                run_count=data["state"].get("run_count", 0),
            ),
            enabled=data.get("enabled", True),
            created_at_ms=data.get("created_at_ms"),
        )
