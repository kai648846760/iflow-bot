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


def load_config(config_path: Optional[Path] = None) -> Config:
    """
    Load configuration from file.
    
    Args:
        config_path: Optional path to config file. If not provided,
                     uses the default path.
    
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
        logger.info("No config file found. Using defaults.")
    
    return Config()


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
