"""CLI commands for iflow-bot.

命令结构:
- iflow-bot gateway start   # 后台启动服务
- iflow-bot gateway run     # 前台运行（debug模式）
- iflow-bot gateway restart # 重启服务
- iflow-bot gateway stop    # 停止服务
- iflow-bot status          # 查看服务状态
- iflow-bot model <name>    # 切换模型
- iflow-bot thinking on/off # 思考模式开关
- iflow-bot iflow <args>    # iflow 命令透传
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import signal
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from typing import Optional

import typer
from rich.console import Console
from rich.table import Table

console = Console()

__version__ = "0.2.0"
__logo__ = "🤖"


# ============================================================================
# 路径配置
# ============================================================================

def get_config_dir() -> Path:
    return Path.home() / ".iflow-bot"

def get_config_path() -> Path:
    return get_config_dir() / "config.json"

def get_pid_file() -> Path:
    return get_config_dir() / "gateway.pid"

def get_log_file() -> Path:
    return get_config_dir() / "gateway.log"

def get_templates_dir() -> Path:
    """获取项目模板目录。"""
    return Path(__file__).parent.parent / "templates"


# ============================================================================
# 配置管理
# ============================================================================

def load_config():
    """加载配置。"""
    from iflow_bot.config.schema import Config
    config_path = get_config_path()
    
    if config_path.exists():
        try:
            with open(config_path, "r", encoding="utf-8") as f:
                data = json.load(f)
            return Config(**data)
        except Exception as e:
            console.print(f"[yellow]Warning: Invalid config file: {e}[/yellow]")
    
    return Config()


def save_config(config) -> None:
    """保存配置。"""
    config_path = get_config_path()
    config_path.parent.mkdir(parents=True, exist_ok=True)
    
    if hasattr(config, "model_dump"):
        data = config.model_dump()
    elif hasattr(config, "dict"):
        data = config.dict()
    else:
        data = dict(config)
    
    with open(config_path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)


# ============================================================================
# Workspace 初始化
# ============================================================================

def init_workspace(workspace: Path) -> None:
    """初始化 workspace 目录，从模板目录复制文件。"""
    # 展开波浪号路径
    workspace = Path(str(workspace).replace("~", str(Path.home())))
    workspace.mkdir(parents=True, exist_ok=True)
    
    # 创建 .iflow 目录
    iflow_dir = workspace / ".iflow"
    iflow_dir.mkdir(exist_ok=True)
    
    # 创建 .iflow/settings.json
    settings_path = iflow_dir / "settings.json"
    if not settings_path.exists():
        default_settings = {
            "contextFileName": ["AGENTS.md", "BOOT.md", "BOOTSTRAP.md", "HEARTBEAT.md", "IDENTITY.md", "SOUL.md", "TOOLS.md", "USER.md"],
            "approvalMode": "yolo",
            "language": "zh-CN",
        }
        with open(settings_path, "w", encoding="utf-8") as f:
            json.dump(default_settings, f, indent=2, ensure_ascii=False)
        console.print(f"[green]✓[/green] Created {settings_path}")
    
    # 从模板目录复制文件
    templates_dir = get_templates_dir()
    
    # 需要复制的模板文件
    template_files = [
        "AGENTS.md",
        "BOOT.md", 
        "BOOTSTRAP.md",
        "HEARTBEAT.md",
        "IDENTITY.md",
        "SOUL.md",
        "TOOLS.md",
        "USER.md",
    ]
    
    for filename in template_files:
        src = templates_dir / filename
        dst = workspace / filename
        if src.exists() and not dst.exists():
            shutil.copy2(src, dst)
            console.print(f"[green]✓[/green] Created {dst}")
    
    # 创建 memory 目录并复制 MEMORY.md
    memory_dir = workspace / "memory"
    memory_dir.mkdir(exist_ok=True)
    
    memory_src = templates_dir / "memory" / "MEMORY.md"
    memory_dst = memory_dir / "MEMORY.md"
    if memory_src.exists() and not memory_dst.exists():
        shutil.copy2(memory_src, memory_dst)
        console.print(f"[green]✓[/green] Created {memory_dst}")


# ============================================================================
# 主命令
# ============================================================================

app = typer.Typer(
    name="iflow-bot",
    help=f"{__logo__} iflow-bot - Multi-channel AI Assistant (powered by iflow)",
    no_args_is_help=True,
    add_completion=False,
)


def print_banner() -> None:
    console.print(r"""
                  
 /$$ /$$$$$$$$ /$$                                 /$$$$$$$              /$$    
|__/| $$_____/| $$                                | $$__  $$            | $$    
 /$$| $$      | $$  /$$$$$$  /$$  /$$  /$$        | $$  \ $$  /$$$$$$  /$$$$$$  
| $$| $$$$$   | $$ /$$__  $$| $$ | $$ | $$ /$$$$$$| $$$$$$$  /$$__  $$|_  $$_/  
| $$| $$__/   | $$| $$  \ $$| $$ | $$ | $$|______/| $$__  $$| $$  \ $$  | $$    
| $$| $$      | $$| $$  | $$| $$ | $$ | $$        | $$  \ $$| $$  | $$  | $$ /$$
| $$| $$      | $$|  $$$$$$/|  $$$$$/$$$$/        | $$$$$$$/|  $$$$$$/  |  $$$$/
|__/|__/      |__/ \______/  \_____/\___/         |_______/  \______/    \___/                                                                         
                                                                                
  Multi-channel AI Assistant (powered by iflow)
""")


def _version_callback(value: bool):
    if value:
        console.print(f"{__logo__} iflow-bot v{__version__}")
        raise typer.Exit()


@app.callback()
def main(
    version: bool = typer.Option(None, "--version", "-v", is_eager=True, callback=_version_callback),
) -> None:
    """iflow-bot - 多渠道 AI 助手（基于 iflow）。"""
    pass


# ============================================================================
# Gateway 命令组
# ============================================================================

gateway_app = typer.Typer(help="Gateway 服务管理")
app.add_typer(gateway_app, name="gateway")


@gateway_app.callback()
def gateway_callback():
    """Gateway 服务管理命令。"""
    pass


@gateway_app.command("start")
def gateway_start(
    daemon: bool = typer.Option(True, "--daemon/--no-daemon", "-d/-D", help="后台运行"),
) -> None:
    """后台启动 Gateway 服务。"""
    print_banner()
    
    config = load_config()
    workspace = Path(config.get_workspace())
    
    # 初始化 workspace
    init_workspace(workspace)
    
    # 检查是否已运行
    pid_file = get_pid_file()
    if pid_file.exists():
        try:
            pid = int(pid_file.read_text().strip())
            os.kill(pid, 0)  # 检查进程是否存在
            console.print(f"[yellow]Gateway already running (PID: {pid})[/yellow]")
            console.print("Use [cyan]iflow-bot gateway restart[/cyan] to restart")
            return
        except (ProcessLookupError, ValueError):
            pass
    
    enabled_channels = config.get_enabled_channels()
    if not enabled_channels:
        console.print("[yellow]No channels are enabled in the configuration.[/yellow]")
        console.print("Edit [cyan]~/.iflow-bot/config.json[/cyan] to enable channels.")
        return
    
    console.print(f"[bold]启动渠道网关:[/bold] {', '.join(enabled_channels)}")
    console.print(f"[bold]Workspace:[/bold] {workspace}")
    console.print(f"[bold]Model:[/bold] {config.get_model()}")
    console.print()
    
    if daemon:
        # 后台启动
        log_file = get_log_file()
        cmd = [sys.executable, "-m", "iflow_bot.cli.commands", "_run_gateway"]
        
        with open(log_file, "w") as log_f:
            process = subprocess.Popen(
                cmd,
                stdout=log_f,
                stderr=log_f,
                start_new_session=True,
            )
        
        # 保存 PID
        pid_file.write_text(str(process.pid))
        
        console.print(f"[green]✓[/green] Gateway started (PID: {process.pid})")
        console.print(f"[dim]Log file: {log_file}[/dim]")
    else:
        # 前台运行
        asyncio.run(_run_gateway(config))


@gateway_app.command("run")
def gateway_run(
    verbose: bool = typer.Option(False, "--verbose", "-v", help="详细输出"),
) -> None:
    """前台运行 Gateway 服务（debug 模式）。"""
    print_banner()
    
    config = load_config()
    workspace = Path(config.get_workspace())
    
    # 初始化 workspace
    init_workspace(workspace)
    
    enabled_channels = config.get_enabled_channels()
    if not enabled_channels:
        console.print("[yellow]No channels are enabled in the configuration.[/yellow]")
        return
    
    console.print(f"[bold]启动渠道网关:[/bold] {', '.join(enabled_channels)}")
    console.print(f"[bold]Workspace:[/bold] {workspace}")
    console.print(f"[bold]Model:[/bold] {config.get_model()}")
    console.print()
    
    asyncio.run(_run_gateway(config, verbose=verbose))


@gateway_app.command("stop")
def gateway_stop() -> None:
    """停止 Gateway 服务。"""
    pid_file = get_pid_file()
    
    if not pid_file.exists():
        console.print("[yellow]Gateway is not running[/yellow]")
        return
    
    try:
        pid = int(pid_file.read_text().strip())
        os.kill(pid, signal.SIGTERM)
        console.print(f"[green]✓[/green] Gateway stopped (PID: {pid})")
        pid_file.unlink()
    except ProcessLookupError:
        console.print("[yellow]Gateway process not found[/yellow]")
        pid_file.unlink()
    except Exception as e:
        console.print(f"[red]Error stopping gateway: {e}[/red]")


@gateway_app.command("restart")
def gateway_restart() -> None:
    """重启 Gateway 服务。"""
    gateway_stop()
    console.print()
    gateway_start()


# 内部命令 - 用于后台启动
@app.command("_run_gateway", hidden=True)
def _run_gateway_cmd():
    """内部命令：运行 Gateway。"""
    config = load_config()
    asyncio.run(_run_gateway(config))


async def _run_gateway(config, verbose: bool = False) -> None:
    """运行网关服务。"""
    from loguru import logger
    
    if verbose:
        logger.remove()
        logger.add(sys.stderr, level="DEBUG", format="<green>{time:HH:mm:ss}</green> | <level>{level:8}</level> | {message}")
    
    from iflow_bot.bus import MessageBus
    from iflow_bot.engine import IFlowAdapter
    from iflow_bot.engine.loop import AgentLoop
    from iflow_bot.channels import ChannelManager
    
    workspace = config.get_workspace()
    
    adapter = IFlowAdapter(
        default_model=config.get_model(),
        workspace=workspace if workspace else None,
        timeout=config.get_timeout(),
        thinking=config.driver.thinking if hasattr(config, "driver") and config.driver else False,
    )
    
    bus = MessageBus()
    channel_manager = ChannelManager(config, bus)
    
    agent_loop = AgentLoop(
        bus=bus,
        adapter=adapter,
        model=config.get_model(),
    )
    
    console.print("[bold green]Gateway 启动中...[/bold green]")
    
    try:
        await channel_manager.start_all()
        await agent_loop.start_background()
        
        console.print("[bold green]✓ Gateway 运行中！[/bold green]")
        console.print("[dim]按 Ctrl+C 停止[/dim]")
        
        while True:
            await asyncio.sleep(1)
    except KeyboardInterrupt:
        console.print("\n[yellow]正在关闭...[/yellow]")
    finally:
        agent_loop.stop()
        await channel_manager.stop_all()
        await adapter.close()


# ============================================================================
# Status 命令
# ============================================================================

@app.command()
def status() -> None:
    """显示 iflow-bot 状态。"""
    print_banner()
    
    config = load_config()
    config_path = get_config_path()
    pid_file = get_pid_file()
    
    # 服务状态
    console.print("[bold]服务状态:[/bold]")
    if pid_file.exists():
        try:
            pid = int(pid_file.read_text().strip())
            os.kill(pid, 0)
            console.print(f"  Gateway: [green]运行中[/green] (PID: {pid})")
        except ProcessLookupError:
            console.print("  Gateway: [red]已停止[/red] (进程不存在)")
    else:
        console.print("  Gateway: [dim]未启动[/dim]")
    
    console.print()
    
    # 配置信息
    console.print("[bold]配置信息:[/bold]")
    console.print(f"  Config: [cyan]{config_path}[/cyan]")
    console.print(f"  Workspace: [cyan]{config.get_workspace() or 'Not set'}[/cyan]")
    console.print(f"  Model: [cyan]{config.get_model()}[/cyan]")
    thinking = config.driver.thinking if hasattr(config, "driver") and config.driver else False
    console.print(f"  Thinking: [cyan]{'启用' if thinking else '禁用'}[/cyan]")
    console.print()
    
    # 渠道状态
    enabled_channels = config.get_enabled_channels()
    console.print(f"[bold]启用渠道:[/bold] {', '.join(enabled_channels) or 'None'}")
    
    # 会话映射
    from iflow_bot.engine.adapter import SessionMappingManager
    mappings = SessionMappingManager().list_all()
    if mappings:
        console.print(f"[bold]会话映射:[/bold] {len(mappings)} 个用户")


# ============================================================================
# 模型切换命令
# ============================================================================

@app.command()
def model(
    name: str = typer.Argument(..., help="模型名称 (如: glm-5, kimi-k2.5)"),
) -> None:
    """切换默认模型。"""
    config = load_config()
    config.model = name
    if hasattr(config, 'driver') and config.driver:
        config.driver.model = name
    save_config(config)
    
    console.print(f"[green]✓[/green] Model set to: [cyan]{name}[/cyan]")
    console.print("[dim]Restart gateway to apply: iflow-bot gateway restart[/dim]")


# ============================================================================
# 思考模式命令
# ============================================================================

@app.command()
def thinking(
    mode: str = typer.Argument(..., help="on 或 off"),
) -> None:
    """开启/关闭思考模式。"""
    if mode.lower() not in ("on", "off", "true", "false"):
        console.print("[red]Error: mode must be 'on' or 'off'[/red]")
        raise typer.Exit(1)
    
    enabled = mode.lower() in ("on", "true")
    
    config = load_config()
    if hasattr(config, 'driver') and config.driver:
        config.driver.thinking = enabled
    save_config(config)
    
    status = "启用" if enabled else "禁用"
    console.print(f"[green]✓[/green] Thinking mode: [cyan]{status}[/cyan]")
    console.print("[dim]Restart gateway to apply: iflow-bot gateway restart[/dim]")


# ============================================================================
# Sessions 命令
# ============================================================================

@app.command()
def sessions(
    channel: Optional[str] = typer.Option(None, "--channel", "-c", help="过滤渠道"),
    chat_id: Optional[str] = typer.Option(None, "--chat-id", help="过滤聊天ID"),
    clear: bool = typer.Option(False, "--clear", help="清除会话映射"),
) -> None:
    """管理会话映射。"""
    from iflow_bot.engine.adapter import SessionMappingManager, IFlowAdapter
    
    config = load_config()
    workspace = config.get_workspace()
    
    adapter = IFlowAdapter(
        default_model=config.get_model(),
        workspace=workspace if workspace else None,
    )
    mappings = adapter.session_mappings
    
    if clear and channel and chat_id:
        if mappings.clear_session(channel, chat_id):
            console.print(f"[green]✓[/green] Cleared session for {channel}:{chat_id}")
        else:
            console.print(f"[yellow]No session mapping found for {channel}:{chat_id}[/yellow]")
        return
    
    # 显示会话映射
    console.print("[bold]会话映射:[/bold]")
    all_mappings = mappings.list_all()
    
    if not all_mappings:
        console.print("[dim]暂无会话映射[/dim]")
    else:
        table = Table()
        table.add_column("Channel:ChatID", style="cyan")
        table.add_column("Session ID", style="green")
        
        for key, session_id in all_mappings.items():
            if channel and not key.startswith(f"{channel}:"):
                continue
            if chat_id and chat_id not in key:
                continue
            table.add_row(key, session_id[:30] + "...")
        
        console.print(table)


# ============================================================================
# Config 命令
# ============================================================================

@app.command()
def config_cmd(
    show: bool = typer.Option(False, "--show", help="显示配置"),
    edit: bool = typer.Option(False, "--edit", "-e", help="编辑配置"),
) -> None:
    """管理配置。"""
    config_path = get_config_path()
    
    if show:
        if config_path.exists():
            console.print(f"[dim]Config file: {config_path}[/dim]")
            console.print(config_path.read_text())
        else:
            console.print("[yellow]No config file found.[/yellow]")
        return
    
    if edit:
        editor = os.environ.get("EDITOR", "vim")
        subprocess.run([editor, str(config_path)])
        return
    
    console.print(f"Config file: [cyan]{config_path}[/cyan]")
    if config_path.exists():
        cfg = load_config()
        console.print(f"Model: [cyan]{cfg.get_model()}[/cyan]")
        console.print(f"Workspace: [cyan]{cfg.get_workspace() or 'Not set'}[/cyan]")
        thinking = cfg.driver.thinking if hasattr(cfg, "driver") and cfg.driver else False
        console.print(f"Thinking: [cyan]{'启用' if thinking else '禁用'}[/cyan]")

app.command(name="config")(config_cmd)


# ============================================================================
# iflow 命令透传
# ============================================================================

@app.command(name="iflow")
def iflow_passthrough(
    args: list[str] = typer.Argument(None, help="iflow 命令参数"),
) -> None:
    """透传命令到 iflow CLI。"""
    config = load_config()
    workspace = config.get_workspace()
    
    cmd = ["iflow"] + (args or [])
    
    cwd = Path(workspace) if workspace else None
    result = subprocess.run(cmd, cwd=cwd)
    raise typer.Exit(result.returncode)


# ============================================================================
# 其他 iflow 命令透传
# ============================================================================

@app.command(name="mcp")
def mcp_passthrough(args: list[str] = typer.Argument(None)) -> None:
    """透传到 iflow mcp 命令。"""
    cmd = ["iflow", "mcp"] + (args or [])
    result = subprocess.run(cmd)
    raise typer.Exit(result.returncode)


@app.command(name="agent")
def agent_passthrough(args: list[str] = typer.Argument(None)) -> None:
    """透传到 iflow agent 命令。"""
    cmd = ["iflow", "agent"] + (args or [])
    result = subprocess.run(cmd)
    raise typer.Exit(result.returncode)


@app.command(name="workflow")
def workflow_passthrough(args: list[str] = typer.Argument(None)) -> None:
    """透传到 iflow workflow 命令。"""
    cmd = ["iflow", "workflow"] + (args or [])
    result = subprocess.run(cmd)
    raise typer.Exit(result.returncode)


@app.command(name="skill")
def skill_passthrough(args: list[str] = typer.Argument(None)) -> None:
    """透传到 iflow skill 命令。"""
    cmd = ["iflow", "skill"] + (args or [])
    result = subprocess.run(cmd)
    raise typer.Exit(result.returncode)


@app.command(name="commands")
def commands_passthrough(args: list[str] = typer.Argument(None)) -> None:
    """透传到 iflow commands 命令。"""
    cmd = ["iflow", "commands"] + (args or [])
    result = subprocess.run(cmd)
    raise typer.Exit(result.returncode)


# ============================================================================
# Onboard 命令
# ============================================================================

@app.command()
def onboard(
    force: bool = typer.Option(False, "--force", "-f", help="覆盖现有配置"),
) -> None:
    """初始化 iflow-bot 配置。"""
    print_banner()
    
    config_path = get_config_path()
    config_dir = get_config_dir()
    
    if config_path.exists() and not force:
        console.print(f"[yellow]配置已存在: {config_path}[/yellow]")
        console.print("使用 [bold]--force[/bold] 覆盖")
        return
    
    config_dir.mkdir(parents=True, exist_ok=True)
    
    default_config = {
        "model": "glm-5",
        "driver": {
            "iflow_path": "iflow",
            "model": "glm-5",
            "yolo": True,
            "thinking": False,
            "max_turns": 40,
            "timeout": 300,
            "workspace": str(Path.home() / ".iflow-bot" / "workspace"),
            "extra_args": []
        },
        "channels": {
            "telegram": {"enabled": False, "token": "", "allow_from": []},
            "discord": {"enabled": False, "token": "", "allow_from": []},
            "whatsapp": {"enabled": False, "bridge_url": "http://localhost:3001"},
            "feishu": {"enabled": False, "app_id": "", "app_secret": ""},
            "slack": {"enabled": False, "bot_token": "", "app_token": ""},
            "dingtalk": {"enabled": False, "client_id": "", "client_secret": ""},
            "qq": {"enabled": False, "app_id": "", "secret": ""},
            "email": {"enabled": False, "imap_host": "", "smtp_host": ""},
            "mochat": {"enabled": False, "base_url": "https://mochat.io"},
        },
        "log_level": "INFO"
    }
    
    with open(config_path, "w", encoding="utf-8") as f:
        json.dump(default_config, f, indent=2, ensure_ascii=False)
    
    # 初始化 workspace
    workspace = Path(default_config["driver"]["workspace"])
    init_workspace(workspace)
    
    console.print()
    console.print("[green]✓[/green] 初始化完成!")
    console.print()
    console.print("下一步:")
    console.print("  1. 编辑 [cyan]~/.iflow-bot/config.json[/cyan] 启用渠道")
    console.print("  2. 运行 [cyan]iflow-bot gateway start[/cyan] 启动服务")


if __name__ == "__main__":
    app()
