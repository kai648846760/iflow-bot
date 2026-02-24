"""Utility functions for iflow-bot."""

from pathlib import Path


def get_home_dir() -> Path:
    """Get the iflow-bot home directory."""
    return Path.home() / ".iflow-bot"


def get_config_dir() -> Path:
    """Get the configuration directory."""
    return get_home_dir()


def get_data_dir() -> Path:
    """Get the data directory."""
    data_dir = get_home_dir() / "data"
    data_dir.mkdir(parents=True, exist_ok=True)
    return data_dir


def get_workspace_dir() -> Path:
    """Get the default workspace directory."""
    workspace = get_home_dir() / "workspace"
    workspace.mkdir(parents=True, exist_ok=True)
    return workspace


def get_sessions_dir() -> Path:
    """Get the sessions directory."""
    sessions = get_data_dir() / "sessions"
    sessions.mkdir(parents=True, exist_ok=True)
    return sessions


def get_media_dir() -> Path:
    """Get the media directory."""
    media = get_data_dir() / "media"
    media.mkdir(parents=True, exist_ok=True)
    return media


def ensure_directories() -> None:
    """Ensure all required directories exist."""
    get_config_dir()
    get_data_dir()
    get_workspace_dir()
    get_sessions_dir()
    get_media_dir()
