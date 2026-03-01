"""Configuration loader for iflow-bot."""

import json
from pathlib import Path
from typing import Optional

from pydantic import ValidationError
from loguru import logger

from iflow_bot.config.schema import Config


def get_config_dir() -> Path:
    """Get the configuration directory."""
    return Path.home() / ".iflow-bot"


def get_config_path() -> Path:
    """Get the configuration file path."""
    return get_config_dir() / "config.json"


def get_data_dir() -> Path:
    """Get the data directory for iflow-bot."""
    data_dir = get_config_dir() / "data"
    data_dir.mkdir(parents=True, exist_ok=True)
    return data_dir


def get_workspace_path() -> Path:
    """Get the default workspace path."""
    workspace = get_config_dir() / "workspace"
    workspace.mkdir(parents=True, exist_ok=True)
    return workspace


def load_config(config_path: Optional[Path] = None, auto_create: bool = True) -> Config:
    """
    Load configuration from file.
    
    Args:
        config_path: Optional path to config file. If not provided,
                     uses the default path.
        auto_create: If True, create default config file when not exists.
    
    Returns:
        Config object.
    """
    if config_path is None:
        config_path = get_config_path()
    
    if config_path.exists():
        try:
            with open(config_path, "r", encoding="utf-8") as f:
                data = json.load(f)
            config = Config(**data)
            logger.info(f"Loaded config from {config_path}")
            return config
        except (json.JSONDecodeError, ValidationError) as e:
            logger.warning(f"Invalid config file: {e}. Using defaults.")
    else:
        logger.info("No config file found. Creating default config.")
        config = Config()
        if auto_create:
            # 创建默认配置文件
            _create_default_config(config_path)
        return config
    
    return Config()


def _create_default_config(config_path: Path) -> None:
    """创建默认配置文件。"""
    config_path.parent.mkdir(parents=True, exist_ok=True)
    
    default_config = {
        "driver": {
            "mode": "stdio",
            "acp_port": 8090,
            "iflow_path": "iflow",
            "model": "minimax-m2.5",
            "yolo": True,
            "thinking": False,
            "max_turns": 40,
            "timeout": 300,
            "workspace": str(Path.home() / ".iflow-bot" / "workspace"),
            "extra_args": []
        },
        "channels": {
            "telegram": {
                "enabled": False,
                "token": "",
                "allow_from": []
            },
            "discord": {
                "enabled": False,
                "token": "",
                "allow_from": []
            },
            "slack": {
                "enabled": False,
                "bot_token": "",
                "app_token": "",
                "allow_from": [],
                "group_policy": "mention",
                "group_allow_from": [],
                "reply_in_thread": True,
                "react_emoji": "eyes",
                "dm": {
                    "enabled": True,
                    "policy": "open",
                    "allow_from": []
                }
            },
            "feishu": {
                "enabled": False,
                "app_id": "",
                "app_secret": "",
                "encrypt_key": "",
                "verification_token": "",
                "allow_from": []
            },
            "dingtalk": {
                "enabled": False,
                "client_id": "",
                "client_secret": "",
                "allow_from": []
            },
            "qq": {
                "enabled": False,
                "app_id": "",
                "secret": "",
                "allow_from": [],
                "split_threshold": 3
            },
            "whatsapp": {
                "enabled": False,
                "bridge_url": "http://localhost:3001",
                "bridge_token": "",
                "allow_from": []
            },
            "email": {
                "enabled": False,
                "consent_granted": False,
                "imap_host": "imap.gmail.com",
                "imap_port": 993,
                "imap_username": "",
                "imap_password": "",
                "imap_use_ssl": True,
                "smtp_host": "smtp.gmail.com",
                "smtp_port": 587,
                "smtp_username": "",
                "smtp_password": "",
                "smtp_use_tls": True,
                "from_address": "",
                "allow_from": [],
                "auto_reply_enabled": True,
                "poll_interval_seconds": 30,
                "max_body_chars": 10000,
                "mark_seen": True,
                "subject_prefix": "Re: "
            },
            "mochat": {
                "enabled": False,
                "base_url": "https://mochat.io",
                "socket_url": "https://mochat.io",
                "socket_path": "/socket.io",
                "claw_token": "",
                "agent_user_id": "",
                "sessions": ["*"],
                "panels": ["*"],
                "watch_timeout_ms": 30000,
                "watch_limit": 50,
                "refresh_interval_ms": 60000,
                "reply_delay_mode": "non-mention",
                "reply_delay_ms": 120000,
                "socket_connect_timeout_ms": 10000,
                "socket_reconnect_delay_ms": 1000,
                "socket_max_reconnect_delay_ms": 5000,
                "max_retry_attempts": 5,
                "retry_delay_ms": 5000
            }
        },
        "heartbeat": {
            "enabled": True,
            "interval_s": 1800
        },
        "log_level": "INFO",
        "log_file": ""
    }
    
    with open(config_path, "w", encoding="utf-8") as f:
        json.dump(default_config, f, indent=2, ensure_ascii=False)
    
    logger.info(f"Created default config at {config_path}")


def save_config(config: Config, config_path: Optional[Path] = None) -> None:
    """
    Save configuration to file.
    
    Args:
        config: Config object to save.
        config_path: Optional path to config file. If not provided,
                     uses the default path.
    """
    if config_path is None:
        config_path = get_config_path()
    
    config_path.parent.mkdir(parents=True, exist_ok=True)
    
    with open(config_path, "w", encoding="utf-8") as f:
        json.dump(config.model_dump(), f, indent=2, ensure_ascii=False)
    
    logger.info(f"Saved config to {config_path}")


def get_session_dir() -> Path:
    """Get the sessions directory."""
    session_dir = get_data_dir() / "sessions"
    session_dir.mkdir(parents=True, exist_ok=True)
    return session_dir
