"""Session manager for iflow-bot.

Manages user sessions across different channels. Each session is stored
as a JSON file tracking metadata like creation time, last active time,
and message count. The actual conversation history is managed by iflow.
"""

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from pydantic import BaseModel


class SessionMetadata(BaseModel):
    """Session metadata model."""
    
    key: str
    """Session key in format: channel:chat_id"""
    
    channel: str
    """Channel name (telegram, discord, etc.)"""
    
    chat_id: str
    """Chat/channel/conversation identifier"""
    
    created_at: str
    """ISO format creation timestamp"""
    
    last_active: str
    """ISO format last active timestamp"""
    
    message_count: int = 0
    """Number of messages in this session"""


class SessionManager:
    """Manages user sessions for iflow-bot.
    
    Sessions are stored as JSON files in the workspace/sessions directory.
    Each session tracks metadata for a unique channel+chat_id combination.
    
    The actual conversation history is managed by iflow CLI (stored in
    ~/.iflow/sessions/), while this module tracks iflow-bot specific
    session state.
    
    Example:
        >>> manager = SessionManager("/path/to/workspace")
        >>> key = manager.get_session_key("telegram", "123456")
        >>> manager.create_session(key)
        '/path/to/workspace/sessions/telegram_123456.json'
    """
    
    def __init__(self, workspace_path: str):
        """Initialize the session manager.
        
        Args:
            workspace_path: Path to the workspace directory where sessions
                           will be stored.
        """
        self.workspace_path = Path(workspace_path)
        self.session_dir = self.workspace_path / "sessions"
        self.session_dir.mkdir(parents=True, exist_ok=True)
    
    def get_session_key(self, channel: str, chat_id: str) -> str:
        """Get session key from channel and chat_id.
        
        Args:
            channel: Channel name (e.g., 'telegram', 'discord')
            chat_id: Chat/conversation identifier
            
        Returns:
            Session key in format 'channel:chat_id'
        """
        return f"{channel}:{chat_id}"
    
    def _get_session_filename(self, session_key: str) -> str:
        """Convert session key to filename.
        
        Args:
            session_key: Session key in format 'channel:chat_id'
            
        Returns:
            Filename in format 'channel_chat_id.json'
        """
        # Replace ':' with '_' for filesystem compatibility
        return f"{session_key.replace(':', '_')}.json"
    
    def get_session_file(self, session_key: str) -> Path:
        """Get the path to the session file.
        
        Args:
            session_key: Session key in format 'channel:chat_id'
            
        Returns:
            Path to the session JSON file
        """
        filename = self._get_session_filename(session_key)
        return self.session_dir / filename
    
    def session_exists(self, session_key: str) -> bool:
        """Check if a session exists.
        
        Args:
            session_key: Session key in format 'channel:chat_id'
            
        Returns:
            True if session file exists, False otherwise
        """
        session_file = self.get_session_file(session_key)
        return session_file.exists()
    
    def create_session(self, session_key: str) -> str:
        """Create a new session.
        
        Creates a new session file with initial metadata. If session
        already exists, returns the existing session file path.
        
        Args:
            session_key: Session key in format 'channel:chat_id'
            
        Returns:
            Path to the session file (as string)
        """
        if self.session_exists(session_key):
            return str(self.get_session_file(session_key))
        
        # Parse channel and chat_id from key
        parts = session_key.split(":", 1)
        if len(parts) != 2:
            raise ValueError(f"Invalid session key format: {session_key}")
        
        channel, chat_id = parts
        now = datetime.now(timezone.utc).isoformat()
        
        metadata = SessionMetadata(
            key=session_key,
            channel=channel,
            chat_id=chat_id,
            created_at=now,
            last_active=now,
            message_count=0,
        )
        
        session_file = self.get_session_file(session_key)
        session_file.write_text(
            metadata.model_dump_json(indent=2),
            encoding="utf-8"
        )
        
        return str(session_file)
    
    def get_session(self, session_key: str) -> Optional[SessionMetadata]:
        """Get session metadata.
        
        Args:
            session_key: Session key in format 'channel:chat_id'
            
        Returns:
            SessionMetadata if session exists, None otherwise
        """
        if not self.session_exists(session_key):
            return None
        
        session_file = self.get_session_file(session_key)
        try:
            data = json.loads(session_file.read_text(encoding="utf-8"))
            return SessionMetadata(**data)
        except (json.JSONDecodeError, ValueError):
            return None
    
    def list_sessions(self) -> list[dict]:
        """List all sessions.
        
        Returns:
            List of session metadata dictionaries, sorted by last_active
            (most recent first)
        """
        sessions = []
        
        for session_file in self.session_dir.glob("*.json"):
            try:
                data = json.loads(session_file.read_text(encoding="utf-8"))
                sessions.append(data)
            except (json.JSONDecodeError, ValueError):
                # Skip invalid session files
                continue
        
        # Sort by last_active, most recent first
        sessions.sort(key=lambda s: s.get("last_active", ""), reverse=True)
        
        return sessions
    
    def update_session(
        self,
        session_key: str,
        metadata: Optional[dict] = None,
        increment_count: bool = False,
    ) -> bool:
        """Update session metadata.
        
        Updates the session's last_active timestamp and optionally
        other metadata fields.
        
        Args:
            session_key: Session key in format 'channel:chat_id'
            metadata: Optional dict of additional metadata to update
            increment_count: If True, increment message_count by 1
            
        Returns:
            True if update succeeded, False if session doesn't exist
        """
        session_data = self.get_session(session_key)
        if session_data is None:
            return False
        
        # Update last_active timestamp
        now = datetime.now(timezone.utc).isoformat()
        session_data.last_active = now
        
        # Increment message count if requested
        if increment_count:
            session_data.message_count += 1
        
        # Apply additional metadata updates
        if metadata:
            for key, value in metadata.items():
                if hasattr(session_data, key):
                    setattr(session_data, key, value)
        
        # Write updated metadata
        session_file = self.get_session_file(session_key)
        session_file.write_text(
            session_data.model_dump_json(indent=2),
            encoding="utf-8"
        )
        
        return True
    
    def delete_session(self, session_key: str) -> bool:
        """Delete a session.
        
        Removes the session file. Note: This only removes the iflow-bot
        session metadata. The actual conversation history managed by
        iflow CLI is not affected.
        
        Args:
            session_key: Session key in format 'channel:chat_id'
            
        Returns:
            True if deletion succeeded, False if session didn't exist
        """
        if not self.session_exists(session_key):
            return False
        
        session_file = self.get_session_file(session_key)
        session_file.unlink()
        return True
    
    def get_or_create_session(self, channel: str, chat_id: str) -> str:
        """Get existing session or create a new one.
        
        Convenience method that combines get_session_key and create_session.
        
        Args:
            channel: Channel name (e.g., 'telegram', 'discord')
            chat_id: Chat/conversation identifier
            
        Returns:
            Path to the session file (as string)
        """
        session_key = self.get_session_key(channel, chat_id)
        return self.create_session(session_key)
    
    def touch_session(self, channel: str, chat_id: str) -> bool:
        """Update session's last_active timestamp.
        
        Convenience method that updates a session's activity timestamp
        and increments the message count.
        
        Args:
            channel: Channel name
            chat_id: Chat/conversation identifier
            
        Returns:
            True if session was updated, False if it doesn't exist
        """
        session_key = self.get_session_key(channel, chat_id)
        return self.update_session(session_key, increment_count=True)
    
    def get_sessions_by_channel(self, channel: str) -> list[dict]:
        """Get all sessions for a specific channel.
        
        Args:
            channel: Channel name to filter by
            
        Returns:
            List of session metadata dictionaries for the channel
        """
        all_sessions = self.list_sessions()
        return [s for s in all_sessions if s.get("channel") == channel]
    
    def cleanup_old_sessions(
        self,
        days_old: int = 30,
        dry_run: bool = False,
    ) -> list[str]:
        """Clean up sessions older than specified days.
        
        Args:
            days_old: Number of days after which to consider a session old
            dry_run: If True, only list sessions to be deleted without
                    actually deleting them
                    
        Returns:
            List of session keys that were (or would be) deleted
        """
        cutoff = datetime.now(timezone.utc)
        deleted = []
        
        for session_data in self.list_sessions():
            last_active_str = session_data.get("last_active", "")
            if not last_active_str:
                continue
            
            try:
                last_active = datetime.fromisoformat(
                    last_active_str.replace("Z", "+00:00")
                )
                age_days = (cutoff - last_active.replace(tzinfo=timezone.utc)).days
                
                if age_days > days_old:
                    session_key = session_data.get("key", "")
                    if not dry_run:
                        self.delete_session(session_key)
                    deleted.append(session_key)
            except ValueError:
                continue
        
        return deleted
