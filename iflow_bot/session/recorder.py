"""Channel message recorder for debugging and history purposes.

Records all inbound (user) and outbound (AI) messages to channel-specific
JSON files for easier debugging and issue tracking.
"""

import json
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from loguru import logger

from iflow_bot.bus.events import InboundMessage, OutboundMessage
from iflow_bot.utils import get_channel_dir


class ChannelRecorder:
    """Records channel messages to JSON files.
    
    Directory structure:
        ~/.iflow-bot/workspace/channel/{channel_name}/{chat_id}-{date}.json
    
    Each JSON file contains:
    {
        "channel": "telegram",
        "chat_id": "5583237352",
        "date": "2026-02-26",
        "messages": [
            {
                "id": "msg_xxx",
                "timestamp": "2026-02-26T10:30:00Z",
                "direction": "inbound|outbound",
                "role": "user|assistant",
                "content": "消息内容",
                "chat_id": "5583237352"
            }
        ]
    }
    """
    
    def __init__(self, channel_dir: Optional[Path] = None):
        """Initialize the recorder.
        
        Args:
            channel_dir: Custom channel directory path. Defaults to ~/.iflow-bot/workspace/channel
        """
        self.channel_dir = channel_dir or get_channel_dir()
    
    def _get_channel_dir(self, channel: str) -> Path:
        """Get the directory for a specific channel."""
        dir_path = self.channel_dir / channel
        dir_path.mkdir(parents=True, exist_ok=True)
        return dir_path
    
    def _get_date_file(self, channel: str, chat_id: str, date: Optional[str] = None) -> Path:
        """Get the JSON file path for a specific channel, chat_id and date.
        
        Args:
            channel: Channel name (e.g., 'telegram', 'qq')
            chat_id: User/chat identifier for isolation
            date: Date string in YYYY-MM-DD format. Defaults to current UTC date.
        
        Returns:
            Path to the JSON file: {chat_id}-{date}.json
        """
        if date is None:
            date = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        return self._get_channel_dir(channel) / f"{chat_id}-{date}.json"
    
    def _load_messages(self, file_path: Path) -> dict:
        """Load existing messages from file."""
        if file_path.exists():
            try:
                with open(file_path, "r", encoding="utf-8") as f:
                    return json.load(f)
            except (json.JSONDecodeError, IOError) as e:
                logger.warning(f"Failed to load channel log {file_path}: {e}")
        
        # Return empty structure - extract chat_id from filename
        # Format: {chat_id}-{date}.json
        filename = file_path.stem
        parts = filename.rsplit("-", 2)  # Split from right to handle chat_id with dashes
        if len(parts) >= 3:
            chat_id = "-".join(parts[:-2])
            date = "-".join(parts[-2:])
        else:
            chat_id = "unknown"
            date = filename
        
        return {
            "channel": file_path.parent.name,
            "chat_id": chat_id,
            "date": date,
            "messages": []
        }
    
    def _save_messages(self, file_path: Path, data: dict) -> None:
        """Save messages to file."""
        try:
            with open(file_path, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2, ensure_ascii=False)
        except IOError as e:
            logger.error(f"Failed to save channel log {file_path}: {e}")
    
    def record_inbound(self, msg: InboundMessage) -> None:
        """Record an inbound (user) message.
        
        Args:
            msg: The inbound message to record.
        """
        file_path = self._get_date_file(msg.channel, msg.chat_id)
        data = self._load_messages(file_path)
        
        message_entry = {
            "id": str(uuid.uuid4())[:12],
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "direction": "inbound",
            "role": "user",
            "content": msg.content,
            "chat_id": msg.chat_id,
            "sender_id": msg.sender_id,
            "media": msg.media if msg.media else []
        }
        
        data["messages"].append(message_entry)
        self._save_messages(file_path, data)
        logger.debug(f"[Recorder] Recorded inbound message to {msg.channel}/{file_path.name}")
    
    def record_outbound(self, msg: OutboundMessage) -> None:
        """Record an outbound (AI) message.
        
        Args:
            msg: The outbound message to record.
        """
        # 跳过纯 progress 消息（工具提示、流式中间过程）
        # 但记录包含实际内容的流式消息（最终累积内容）
        is_progress = msg.metadata.get("_progress", False)
        is_streaming = msg.metadata.get("_streaming", False)
        is_streaming_end = msg.metadata.get("_streaming_end", False)
        
        # 跳过工具提示（无 streaming 标记的 progress）
        if is_progress and not is_streaming and not is_streaming_end:
            return
        
        # 跳过流式中间过程（有内容，但不是最终消息）
        # 这里的逻辑是：如果有 _progress + _streaming，我们记录它（因为它包含累积内容）
        # 但我们需要跳过纯流结束的空消息
        
        # 跳过空的流结束消息
        if is_streaming_end and not msg.content:
            return
        
        file_path = self._get_date_file(msg.channel, msg.chat_id)
        data = self._load_messages(file_path)
        
        message_entry = {
            "id": str(uuid.uuid4())[:12],
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "direction": "outbound",
            "role": "assistant",
            "content": msg.content,
            "chat_id": msg.chat_id,
            "reply_to_id": msg.reply_to_id,
            "is_streaming": is_streaming
        }
        
        data["messages"].append(message_entry)
        self._save_messages(file_path, data)
        logger.debug(f"[Recorder] Recorded outbound message to {msg.channel}/{file_path.name}")


# Global recorder instance
_recorder: Optional[ChannelRecorder] = None


def get_recorder() -> ChannelRecorder:
    """Get the global recorder instance."""
    global _recorder
    if _recorder is None:
        _recorder = ChannelRecorder()
    return _recorder


def set_recorder(recorder: ChannelRecorder) -> None:
    """Set the global recorder instance."""
    global _recorder
    _recorder = recorder
