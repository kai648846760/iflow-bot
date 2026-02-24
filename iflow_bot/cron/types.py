"""Cron job types for iflow-bot."""

from dataclasses import dataclass, field
from typing import Optional, Literal
from enum import Enum
import uuid


class ScheduleKind(str, Enum):
    """Schedule type for cron jobs."""
    
    AT = "at"           # 在指定时间运行一次
    EVERY = "every"     # 每隔 N 毫秒
    CRON = "cron"       # cron 表达式


@dataclass
class CronSchedule:
    """
    Schedule definition for a cron job.
    
    匹配 nanobot 的调度定义。
    
    Supports three schedule types:
    - AT: Run once at a specific timestamp
    - EVERY: Run every N milliseconds  
    - CRON: Run based on cron expression
    """
    kind: Literal["at", "every", "cron"]
    """Schedule type"""
    
    at_ms: Optional[int] = None
    """Timestamp in milliseconds (for AT type)"""
    
    every_ms: Optional[int] = None
    """Interval in milliseconds (for EVERY type)"""
    
    expr: Optional[str] = None
    """Cron expression (for CRON type, e.g., '0 9 * * 1-5' for 9 AM weekdays)"""
    
    tz: Optional[str] = None
    """Timezone (e.g., 'Asia/Shanghai', 'UTC')"""


# 保留旧名称兼容
Schedule = CronSchedule


@dataclass
class CronPayload:
    """
    What to do when the job runs.
    
    匹配 nanobot 的负载定义，增加 reminder 类型。
    
    Payload types:
    - agent_turn: 让 Agent 执行任务并返回结果
    - reminder: 直接发送消息给用户（不经过 Agent）
    - system_event: 系统事件
    """
    kind: Literal["system_event", "agent_turn", "reminder"] = "agent_turn"
    """Payload type: system_event, agent_turn, or reminder"""
    
    message: str = ""
    """Message content to send"""
    
    deliver: bool = False
    """Whether to deliver the message through the channel"""
    
    channel: Optional[str] = None
    """Target channel (telegram, discord, etc.)"""
    
    to: Optional[str] = None
    """Target chat/user identifier"""


@dataclass
class CronJobState:
    """
    Runtime state of a job.
    
    匹配 nanobot 的状态定义。
    """
    next_run_at_ms: Optional[int] = None
    """Next scheduled execution timestamp in milliseconds"""
    
    last_run_at_ms: Optional[int] = None
    """Last execution timestamp in milliseconds"""
    
    last_status: Optional[Literal["ok", "error", "skipped"]] = None
    """Last execution status"""
    
    last_error: Optional[str] = None
    """Last error message if execution failed"""


@dataclass
class CronJob:
    """
    A scheduled job.
    
    匹配 nanobot 的任务定义。
    """
    id: str
    """Unique job identifier (short uuid)"""
    
    name: str
    """Human-readable job name"""
    
    enabled: bool = True
    """Whether the job is enabled"""
    
    schedule: CronSchedule = field(default_factory=lambda: CronSchedule(kind="every"))
    """Schedule configuration"""
    
    payload: CronPayload = field(default_factory=CronPayload)
    """Execution payload"""
    
    state: CronJobState = field(default_factory=CronJobState)
    """Runtime state"""
    
    created_at_ms: int = 0
    """Job creation timestamp in milliseconds"""
    
    updated_at_ms: int = 0
    """Last update timestamp in milliseconds"""
    
    delete_after_run: bool = False
    """Whether to delete the job after one-time execution"""
    
    @classmethod
    def create(
        cls,
        name: str,
        schedule: CronSchedule,
        payload: CronPayload,
        job_id: Optional[str] = None,
        enabled: bool = True,
        delete_after_run: bool = False,
    ) -> "CronJob":
        """
        Create a new CronJob with auto-generated ID.
        
        Args:
            name: Human-readable job name
            schedule: Schedule configuration
            payload: Execution payload
            job_id: Optional custom ID (auto-generated if not provided)
            enabled: Whether the job is enabled
            delete_after_run: Whether to delete after one-time execution
        
        Returns:
            A new CronJob instance
        """
        import time
        
        now = int(time.time() * 1000)
        return cls(
            id=job_id or str(uuid.uuid4())[:8],
            name=name,
            schedule=schedule,
            payload=payload,
            enabled=enabled,
            created_at_ms=now,
            updated_at_ms=now,
            delete_after_run=delete_after_run,
        )
    
    def to_dict(self) -> dict:
        """Convert to dictionary for serialization."""
        return {
            "id": self.id,
            "name": self.name,
            "enabled": self.enabled,
            "schedule": {
                "kind": self.schedule.kind,
                "atMs": self.schedule.at_ms,
                "everyMs": self.schedule.every_ms,
                "expr": self.schedule.expr,
                "tz": self.schedule.tz,
            },
            "payload": {
                "kind": self.payload.kind,
                "message": self.payload.message,
                "deliver": self.payload.deliver,
                "channel": self.payload.channel,
                "to": self.payload.to,
            },
            "state": {
                "nextRunAtMs": self.state.next_run_at_ms,
                "lastRunAtMs": self.state.last_run_at_ms,
                "lastStatus": self.state.last_status,
                "lastError": self.state.last_error,
            },
            "createdAtMs": self.created_at_ms,
            "updatedAtMs": self.updated_at_ms,
            "deleteAfterRun": self.delete_after_run,
        }
    
    @classmethod
    def from_dict(cls, data: dict) -> "CronJob":
        """Create from dictionary."""
        schedule_data = data.get("schedule", {})
        payload_data = data.get("payload", {})
        state_data = data.get("state", {})
        
        return cls(
            id=data["id"],
            name=data["name"],
            enabled=data.get("enabled", True),
            schedule=CronSchedule(
                kind=schedule_data.get("kind", "every"),
                at_ms=schedule_data.get("atMs"),
                every_ms=schedule_data.get("everyMs"),
                expr=schedule_data.get("expr"),
                tz=schedule_data.get("tz"),
            ),
            payload=CronPayload(
                kind=payload_data.get("kind", "agent_turn"),
                message=payload_data.get("message", ""),
                deliver=payload_data.get("deliver", False),
                channel=payload_data.get("channel"),
                to=payload_data.get("to"),
            ),
            state=CronJobState(
                next_run_at_ms=state_data.get("nextRunAtMs"),
                last_run_at_ms=state_data.get("lastRunAtMs"),
                last_status=state_data.get("lastStatus"),
                last_error=state_data.get("lastError"),
            ),
            created_at_ms=data.get("createdAtMs", 0),
            updated_at_ms=data.get("updatedAtMs", 0),
            delete_after_run=data.get("deleteAfterRun", False),
        )


@dataclass
class CronStore:
    """
    Persistent store for cron jobs.
    
    匹配 nanobot 的存储结构。
    """
    version: int = 1
    jobs: list[CronJob] = field(default_factory=list)
    
    def to_dict(self) -> dict:
        """Convert to dictionary for serialization."""
        return {
            "version": self.version,
            "jobs": [job.to_dict() for job in self.jobs],
        }
    
    @classmethod
    def from_dict(cls, data: dict) -> "CronStore":
        """Create from dictionary."""
        return cls(
            version=data.get("version", 1),
            jobs=[CronJob.from_dict(j) for j in data.get("jobs", [])],
        )
