"""Utility functions for iflow-bot."""

from pathlib import Path
from typing import Any, Optional

import aiohttp
from loguru import logger


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


def get_channel_dir() -> Path:
    """Get the channel message recording directory.

    Directory structure:
        ~/.iflow-bot/workspace/channel/{channel_name}/{date}.json
    """
    channel_dir = get_workspace_dir() / "channel"
    channel_dir.mkdir(parents=True, exist_ok=True)
    return channel_dir


def ensure_directories() -> None:
    """Ensure all required directories exist."""
    get_config_dir()
    get_data_dir()
    get_workspace_dir()
    get_sessions_dir()
    get_media_dir()
    get_channel_dir()


def get_iflow_config_dir() -> Path:
    """Get iflow CLI config directory.

    Supports multiple platforms:
    - Linux/Android (Termux): ~/.iflow/
    - macOS: ~/Library/Application Support/iflow/
    - Windows: %APPDATA%/iflow/
    """
    import platform
    system = platform.system().lower()

    if system == "windows":
        appdata = Path.home() / "AppData" / "Roaming"
        return appdata / "iflow"
    elif system == "darwin":  # macOS
        return Path.home() / "Library" / "Application Support" / "iflow"
    else:  # Linux, Android, etc.
        return Path.home() / ".iflow"


def ensure_iflow_skills_dir(workspace: Path) -> None:
    """Ensure iflow CLI loads skills from workspace/skills.

    Prefer a symlink from ~/.iflow/skills to workspace/skills. If symlink
    is unavailable, fall back to a one-way sync copy.
    """
    import os
    import shutil
    import time

    try:
        workspace = Path(str(workspace).replace("~", str(Path.home())))
        skills_dir = workspace / "skills"
        skills_dir.mkdir(parents=True, exist_ok=True)

        iflow_dir = get_iflow_config_dir()
        iflow_dir.mkdir(parents=True, exist_ok=True)
        target = iflow_dir / "skills"

        if target.exists() or target.is_symlink():
            try:
                if target.resolve() == skills_dir.resolve():
                    return
            except Exception:
                pass

            if target.is_symlink():
                try:
                    target.unlink()
                except Exception as e:
                    logger.warning(f"Failed to remove existing skills symlink: {e}")
            else:
                backup = iflow_dir / f"skills.bak.{int(time.time())}"
                try:
                    target.rename(backup)
                    logger.info(f"Backed up existing iflow skills dir to {backup}")
                except Exception as e:
                    logger.warning(f"Failed to backup existing iflow skills dir: {e}")

        try:
            os.symlink(skills_dir, target, target_is_directory=True)
            logger.info(f"Linked iflow skills dir to workspace: {skills_dir}")
            return
        except Exception as e:
            logger.warning(f"Failed to symlink iflow skills dir: {e}")

        try:
            target.mkdir(parents=True, exist_ok=True)
            shutil.copytree(skills_dir, target, dirs_exist_ok=True)
            logger.info(f"Synced workspace skills to {target}")
        except Exception as e:
            logger.warning(f"Failed to sync skills dir: {e}")
    except Exception as e:
        logger.warning(f"Failed to ensure iflow skills dir: {e}")


def sync_mcp_from_iflow(overwrite: bool = False) -> bool:
    """Sync MCP servers from iflow CLI settings to iflow-bot.

    Args:
        overwrite: If True, overwrite existing config. If False, merge.

    Returns:
        True if sync successful, False otherwise.
    """
    import json
    import shutil

    iflow_config_dir = get_iflow_config_dir()
    iflow_settings = iflow_config_dir / "settings.json"

    if not iflow_settings.exists():
        logger.warning(f"iflow settings.json not found at {iflow_settings}")
        return False

    try:
        with open(iflow_settings, "r", encoding="utf-8") as f:
            iflow_data = json.load(f)
    except json.JSONDecodeError as e:
        logger.error(f"Failed to parse iflow settings: {e}")
        return False

    mcp_servers = iflow_data.get("mcpServers", {})
    if not mcp_servers:
        logger.info("No MCP servers found in iflow settings")
        return False

    # 构建 iflow-bot MCP 代理配置
    bot_mcp_config = {"mcpServers": {}}
    for name, server_config in mcp_servers.items():
        # 转换 iflow 格式到 MCP Proxy 格式
        # iflow 使用 disabled，MCP Proxy 也使用 disabled
        bot_mcp_config["mcpServers"][name] = {
            "type": server_config.get("type", "stdio"),
            "command": server_config.get("command", ""),
            "args": server_config.get("args", []),
            "env": server_config.get("env", {}),
            "disabled": server_config.get("disabled", False),
        }

    # 确定目标配置文件路径
    runtime_config_dir = get_config_dir() / "config"
    runtime_config_dir.mkdir(parents=True, exist_ok=True)
    target_config = runtime_config_dir / ".mcp_proxy_config.json"

    if target_config.exists() and not overwrite:
        # 合并配置
        try:
            with open(target_config, "r", encoding="utf-8") as f:
                existing_config = json.load(f)

            # 合并 MCP 服务器
            for name, server_config in bot_mcp_config["mcpServers"].items():
                if name not in existing_config.get("mcpServers", {}):
                    existing_config.setdefault("mcpServers", {})[name] = server_config

            bot_mcp_config = existing_config
        except json.JSONDecodeError:
            logger.warning("Existing config is invalid, overwriting")

    # 写入配置文件
    with open(target_config, "w", encoding="utf-8") as f:
        json.dump(bot_mcp_config, f, indent=2, ensure_ascii=False)

    logger.info(f"Synced {len(bot_mcp_config['mcpServers'])} MCP servers to {target_config}")
    return True


async def discover_mcp_servers(
    proxy_host: str = "localhost",
    proxy_port: int = 8888,
    allowlist: Optional[list[str]] = None,
    blocklist: Optional[list[str]] = None,
    max_servers: int = 10,
    timeout: int = 5,
) -> list[dict[str, Any]]:
    """从 MCP 代理自动发现启用的服务器。

    Args:
        proxy_host: MCP 代理主机地址
        proxy_port: MCP 代理端口
        allowlist: 允许使用的服务器名称列表（空表示全部）
        blocklist: 禁用的服务器名称列表
        max_servers: 最多返回的服务器数量
        timeout: HTTP 请求超时时间（秒）

    Returns:
        MCP 服务器配置列表，每个元素包含 {"name": str, "type": "http", "url": str}
    """
    allowlist = allowlist or []
    blocklist = blocklist or []

    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(
                f"http://{proxy_host}:{proxy_port}/health",
                timeout=aiohttp.ClientTimeout(total=timeout)
            ) as resp:
                if resp.status != 200:
                    logger.warning(f"MCP proxy health check failed: {resp.status}")
                    return []

                health_data = await resp.json()
                available_servers = health_data.get("servers", [])

    except Exception as e:
        logger.warning(f"Failed to discover MCP servers: {e}")
        return []

    # 过滤服务器列表
    filtered_servers = []
    for server_name in available_servers:
        # 黑名单优先
        if server_name in blocklist:
            logger.debug(f"MCP server {server_name} blocked by blocklist")
            continue

        # 白名单检查（如果配置了白名单）
        if allowlist and server_name not in allowlist:
            logger.debug(f"MCP server {server_name} not in allowlist")
            continue

        filtered_servers.append(server_name)

    # 限制数量
    if len(filtered_servers) > max_servers:
        logger.info(f"Limiting MCP servers from {len(filtered_servers)} to {max_servers}")
        filtered_servers = filtered_servers[:max_servers]

    # 构建服务器配置
    mcp_servers = [
        {
            "name": name,
            "type": "http",
            "url": f"http://{proxy_host}:{proxy_port}/{name}"
        }
        for name in filtered_servers
    ]

    logger.info(f"Discovered {len(mcp_servers)} MCP servers: {filtered_servers}")
    return mcp_servers
