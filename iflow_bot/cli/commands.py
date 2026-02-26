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
# iflow 检查
# ============================================================================

def check_iflow_installed() -> bool:
    """检查 iflow 是否已安装。

    Returns:
        True if installed, False otherwise
    """
    try:
        result = subprocess.run(
            ["iflow", "--version"],
            capture_output=True,
            text=True,
            timeout=10,
        )
        return result.returncode == 0
    except FileNotFoundError:
        return False
    except subprocess.TimeoutExpired:
        return False
    except Exception:
        return False


def check_iflow_logged_in() -> bool:
    """检查 iflow 是否已登录。

    Returns:
        True if logged in, False otherwise
    """
    try:
        # 检查 iflow 配置目录是否存在登录信息
        iflow_config_dir = Path.home() / ".iflow"
        if not iflow_config_dir.exists():
            return False

        # 检查是否有项目配置（说明已登录）
        projects_dir = iflow_config_dir / "projects"
        if projects_dir.exists() and list(projects_dir.iterdir()):
            return True

        # 尝试运行 iflow 看是否需要登录
        result = subprocess.run(
            ["iflow", "-p", "test"],
            capture_output=True,
            text=True,
            timeout=10,
        )
        # 如果返回 "Please login first" 或类似提示，说明未登录
        output = result.stdout + result.stderr
        if "login" in output.lower() or "please login" in output.lower():
            return False

        return True
    except Exception:
        return False


def ensure_iflow_ready() -> bool:
    """确保 iflow 已安装并登录。

    Returns:
        True if ready, False otherwise
    """
    # 检查是否安装
    if not check_iflow_installed():
        console.print("[yellow]iflow 未安装，正在自动安装...[/yellow]")
        console.print()
        console.print("[cyan]自动安装依赖中...[/cyan]")
        install_cmd = 'bash -c "$(curl -fsSL https://gitee.com/iflow-ai/iflow-cli/raw/main/install.sh)"'
        result = subprocess.run(install_cmd, shell=True)
        if result.returncode != 0:
            console.print("[red]自动安装失败，请手动执行以下命令:[/red]")
            console.print(f"  [cyan]{install_cmd}[/cyan]")
            return False

        # 重新检查是否安装成功
        if not check_iflow_installed():
            console.print("[red]安装后仍检测不到 iflow，请检查安装过程[/red]")
            return False
        console.print("[green]✓ iflow 安装成功![/green]")

    # 检查是否登录
    if not check_iflow_logged_in():
        console.print("[red]Error: iflow is not logged in.[/red]")
        console.print()
        console.print("Please login first:")
        console.print("  [cyan]iflow login[/cyan]")
        return False

    return True


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
    """初始化 workspace 目录，从模板目录复制文件。

    逻辑：
    - 如果 workspace 已存在 AGENTS.md 或 BOOT.md，说明已初始化，跳过模板复制
    - 只有全新的 workspace 才复制所有模板（包括 BOOTSTRAP.md）
    """
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

    # 检查 workspace 是否已经初始化（通过检查核心文件是否存在）
    core_files = ["AGENTS.md", "BOOT.md", "SOUL.md"]
    is_initialized = any((workspace / f).exists() for f in core_files)

    if is_initialized:
        console.print(f"[dim]Workspace already initialized, skipping template copy[/dim]")
        return

    # 从模板目录复制文件（仅首次初始化）
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

    # 创建 channel 目录（用于记录各渠道对话）
    channel_dir = workspace / "channel"
    channel_dir.mkdir(exist_ok=True)


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


@app.command()
def version():
    """查看版本信息。"""
    console.print(f"[bold cyan]{__logo__}[/bold cyan] iflow-bot [green]v{__version__}[/green]")
    console.print()
    console.print(f"  Python:     {sys.version.split()[0]}")
    console.print(f"  Platform:   {sys.platform}")
    console.print(f"  Config:     {get_config_path()}")
    console.print(f"  Workspace:  {Path.home() / '.iflow-bot' / 'workspace'}")
    console.print()


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

    # 检查 iflow 是否就绪
    if not ensure_iflow_ready():
        raise typer.Exit(1)

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

    # 检查 iflow 是否就绪
    if not ensure_iflow_ready():
        raise typer.Exit(1)

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


def get_data_dir() -> Path:
    """获取数据存储目录。"""
    data_dir = get_config_dir() / "data"
    data_dir.mkdir(parents=True, exist_ok=True)
    return data_dir


async def _start_acp_server(port: int = 8090) -> Optional[asyncio.subprocess.Process]:
    """启动 iflow ACP 服务。
    
    如果端口已被占用，则复用现有进程。
    
    执行: iflow --experimental-acp --stream --port {port}
    
    Args:
        port: ACP 服务端口
        
    Returns:
        成功返回进程对象，复用现有进程返回 None
    """
    import socket
    
    # 检查端口是否已被占用
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    result = sock.connect_ex(('localhost', port))
    sock.close()
    
    if result == 0:
        # 端口已被占用，复用现有进程
        print(f"ACP 服务已在运行 (端口 {port})，复用现有进程")
        return None
    
    try:
        process = await asyncio.create_subprocess_exec(
            "iflow",
            "--experimental-acp",
            "--stream",
            "--port", str(port),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            start_new_session=True,
        )
        
        # 等待服务启动
        await asyncio.sleep(2)
        
        # 检查进程是否还在运行
        if process.returncode is not None:
            stderr = await process.stderr.read()
            logger.error(f"ACP server failed to start: {stderr.decode()}")
            return None
        
        return process
    except FileNotFoundError:
        logger.error("iflow command not found")
        return None
    except Exception as e:
        logger.error(f"Failed to start ACP server: {e}")
        return None


async def _stop_acp_server(process: asyncio.subprocess.Process) -> None:
    """停止 ACP 服务进程。"""
    if process.returncode is None:
        try:
            process.terminate()
            await asyncio.wait_for(process.wait(), timeout=5)
        except asyncio.TimeoutError:
            process.kill()
            await process.wait()
        except Exception as e:
            logger.warning(f"Error stopping ACP server: {e}")


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
    from iflow_bot.bus.events import OutboundMessage
    from iflow_bot.engine import IFlowAdapter
    from iflow_bot.engine.loop import AgentLoop
    from iflow_bot.channels import ChannelManager
    from iflow_bot.heartbeat.service import HeartbeatService
    from iflow_bot.cron.service import CronService
    from iflow_bot.cron.types import CronJob
    
    workspace = config.get_workspace()
    
    # 获取模式配置
    mode = getattr(config.driver, "mode", "cli") if hasattr(config, "driver") and config.driver else "cli"
    acp_port = getattr(config.driver, "acp_port", 8090) if hasattr(config, "driver") and config.driver else 8090
    
    # ACP 模式：启动 iflow ACP 服务
    acp_process = None
    if mode == "acp":
        console.print(f"[bold cyan]启动 ACP 服务 (端口: {acp_port})...[/bold cyan]")
        result = await _start_acp_server(acp_port)
        if result is not None:
            acp_process = result
            console.print(f"[green]✓[/green] ACP 服务已启动 (PID: {acp_process.pid})")
        else:
            # 检查端口是否已被占用（复用现有进程）
            import socket
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            port_in_use = sock.connect_ex(('localhost', acp_port)) == 0
            sock.close()
            if port_in_use:
                console.print(f"[green]✓[/green] 复用现有 ACP 服务 (端口: {acp_port})")
            else:
                console.print("[red]✗ ACP 服务启动失败，回退到 CLI 模式[/red]")
                mode = "cli"
    
    # 创建适配器
    adapter = IFlowAdapter(
        default_model=config.get_model(),
        workspace=workspace if workspace else None,
        timeout=config.get_timeout(),
        thinking=config.driver.thinking if hasattr(config, "driver") and config.driver else False,
        mode=mode,
        acp_port=acp_port,
    )
    
    bus = MessageBus()
    channel_manager = ChannelManager(config, bus)
    
    agent_loop = AgentLoop(
        bus=bus,
        adapter=adapter,
        model=config.get_model(),
        channel_manager=channel_manager,
    )
    
    # 创建 Cron 服务
    cron_store_path = get_data_dir() / "cron" / "jobs.json"
    cron = CronService(cron_store_path)
    
    # 设置 cron 任务回调
    async def on_cron_job(job: CronJob) -> str | None:
        """执行 cron 任务通过 agent。"""
        # 通过 agent 处理消息
        # 构建 cron 任务上下文前缀
        import time
        from datetime import datetime as _dt
        
        next_run_info = ""
        if job.state.next_run_at_ms:
            next_run_info = f"下次执行: {_dt.fromtimestamp(job.state.next_run_at_ms / 1000).strftime('%Y-%m-%d %H:%M:%S')}"
        
        context_prefix = f"""[系统消息：这是一个定时任务触发]
任务名称: {job.name}
任务ID: {job.id}
调度类型: {job.schedule.kind}
执行时间: {_dt.now().strftime('%Y-%m-%d %H:%M:%S')}
{next_run_info}

--- 任务消息 ---
"""
        
        full_message = context_prefix + job.payload.message
        
        response = await agent_loop.process_direct(
            full_message,
            session_key=f"cron:{job.id}",
            channel=job.payload.channel or "cron",
            chat_id=job.payload.to or "direct",
        )
        
        # 如果需要投递响应
        if job.payload.deliver and job.payload.to and job.payload.channel:
            await bus.publish_outbound(OutboundMessage(
                channel=job.payload.channel,
                chat_id=job.payload.to,
                content=response or ""
            ))
        
        return response
    
    cron.on_job = on_cron_job
    
    # 选择心跳通知目标的函数
    def _pick_heartbeat_target() -> tuple[str, str]:
        """选择一个可用的渠道/聊天目标用于心跳触发消息。"""
        enabled = set(channel_manager.enabled_channels)
        # 优先使用最近更新的非内部会话
        # 这里简化处理，返回第一个启用的渠道
        if enabled:
            first_channel = list(enabled)[0]
            return first_channel, "heartbeat"
        return "cli", "direct"
    
    # 创建 Heartbeat 服务
    async def on_heartbeat(prompt: str) -> str:
        """执行心跳通过 agent。"""
        channel, chat_id = _pick_heartbeat_target()
        
        async def _silent(*_args, **_kwargs):
            pass
        
        return await agent_loop.process_direct(
            prompt,
            session_key="heartbeat",
            channel=channel,
            chat_id=chat_id,
        )
    
    async def on_heartbeat_notify(response: str) -> None:
        """投递心跳响应到用户渠道。"""
        channel, chat_id = _pick_heartbeat_target()
        if channel == "cli":
            return  # 没有外部渠道可用
        await bus.publish_outbound(OutboundMessage(
            channel=channel,
            chat_id=chat_id,
            content=response
        ))
    
    heartbeat = HeartbeatService(
        workspace=workspace if workspace else Path.home() / ".iflow-bot" / "workspace",
        on_heartbeat=on_heartbeat,
        on_notify=on_heartbeat_notify,
        interval_s=30 * 60,  # 30 分钟
        enabled=True
    )
    
    console.print("[bold green]Gateway 启动中...[/bold green]")
    
    try:
        # 启动服务
        await cron.start()
        await heartbeat.start()
        await channel_manager.start_all()
        await agent_loop.start_background()
        
        # 显示状态
        console.print("[bold green]✓ Gateway 运行中！[/bold green]")
        if channel_manager.enabled_channels:
            console.print(f"[dim]  渠道: {', '.join(channel_manager.enabled_channels)}[/dim]")
        
        cron_status = cron.status()
        if cron_status["jobs"] > 0:
            console.print(f"[dim]  定时任务: {cron_status['jobs']} 个[/dim]")
        
        console.print("[dim]  心跳: 每 30 分钟[/dim]")
        console.print(f"[dim]  模式: {mode.upper()}[/dim]")
        console.print("[dim]按 Ctrl+C 停止[/dim]")
        
        while True:
            await asyncio.sleep(1)
    except KeyboardInterrupt:
        console.print("\n[yellow]正在关闭...[/yellow]")
    finally:
        heartbeat.stop()
        cron.stop()
        agent_loop.stop()
        await channel_manager.stop_all()
        await adapter.close()
        
        # 关闭 ACP 服务
        if acp_process:
            console.print("[dim]正在关闭 ACP 服务...[/dim]")
            await _stop_acp_server(acp_process)


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

    # iflow 状态
    console.print("[bold]iflow 状态:[/bold]")
    if check_iflow_installed():
        console.print("  iflow: [green]已安装[/green]")
        if check_iflow_logged_in():
            console.print("  登录状态: [green]已登录[/green]")
        else:
            console.print("  登录状态: [red]未登录[/red] (运行 [cyan]iflow login[/cyan] 登录)")
    else:
        console.print("  iflow: [red]未安装[/red]")
        console.print("  安装命令: [cyan]npm install -g @anthropic-ai/iflow[/cyan] 或 [cyan]pip install iflow-cli[/cyan]")
    console.print()

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
    if not config.driver:
        from iflow_bot.config.schema import DriverConfig
        config.driver = DriverConfig()
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
    if not config.driver:
        from iflow_bot.config.schema import DriverConfig
        config.driver = DriverConfig()
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

    # 完整的默认配置模板
    default_config = {
        "driver": {
            "mode": "acp",
            "acp_port": 8090,
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
                "reply_in_thread": True,
                "react_emoji": "eyes"
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
                "allow_from": []
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
        "log_level": "INFO",
        "log_file": ""
    }

    with open(config_path, "w", encoding="utf-8") as f:
        json.dump(default_config, f, indent=2, ensure_ascii=False)

    # 初始化 workspace
    workspace = Path(default_config["driver"]["workspace"])
    init_workspace(workspace)

    console.print()
    console.print("[green]✓[/green] 初始化完成!")
    console.print()
    console.print("[bold]配置文件位置:[/bold]")
    console.print(f"  {config_path}")
    console.print()
    console.print("[bold]工作空间位置:[/bold]")
    console.print(f"  {workspace}")
    console.print()
    console.print("[bold]下一步操作:[/bold]")
    console.print()
    console.print("  [yellow]1.[/yellow] 编辑配置文件启用需要的渠道:")
    console.print("     [cyan]~/.iflow-bot/config.json[/cyan]")
    console.print()
    console.print("  [yellow]2.[/yellow] 配置渠道参数:")
    console.print("     • Telegram: 设置 bot token (从 @BotFather 获取)")
    console.print("     • Discord: 设置 bot token (从 Discord Developer Portal 获取)")
    console.print("     • Slack: 设置 bot_token 和 app_token")
    console.print("     • Feishu: 设置 app_id 和 app_secret")
    console.print("     • DingTalk: 设置 client_id 和 client_secret")
    console.print("     • QQ: 设置 app_id 和 secret")
    console.print("     • Email: 设置 IMAP/SMTP 服务器和凭据")
    console.print("     • WhatsApp: 配置 bridge 服务地址")
    console.print("     • Mochat: 设置 claw_token 和 agent_user_id")
    console.print()
    console.print("  [yellow]3.[/yellow] 确保 iflow CLI 已安装并登录:")
    console.print("     [cyan]iflow --version[/cyan]")
    console.print("     [cyan]iflow auth status[/cyan]")
    console.print()
    console.print("  [yellow]4.[/yellow] 启动网关服务:")
    console.print("     [cyan]iflow-bot gateway start[/cyan]")
    console.print()
    console.print("  [yellow]5.[/yellow] 或使用前台模式运行(便于调试):")
    console.print("     [cyan]iflow-bot gateway run[/cyan]")
    console.print()
    console.print("[bold]常用命令:[/bold]")
    console.print("  • [cyan]iflow-bot config[/cyan]    - 查看当前配置")
    console.print("  • [cyan]iflow-bot channels[/cyan]  - 查看渠道状态")
    console.print("  • [cyan]iflow-bot cron list[/cyan] - 查看定时任务")
    console.print("  • [cyan]iflow-bot version[/cyan]   - 查看版本信息")
    console.print()
    console.print("[dim]提示: 使用 --help 查看每个命令的详细用法[/dim]")


# ============================================================================
# Cron 命令
# ============================================================================

cron_app = typer.Typer(help="管理定时任务")
app.add_typer(cron_app, name="cron")


@cron_app.command("list")
def cron_list(
    all: bool = typer.Option(False, "--all", "-a", help="包含已禁用的任务"),
):
    """列出定时任务。"""
    from iflow_bot.cron.service import CronService
    
    store_path = get_data_dir() / "cron" / "jobs.json"
    service = CronService(store_path)
    
    jobs = service.list_jobs(include_disabled=all)
    
    if not jobs:
        console.print("没有定时任务。")
        console.print("\n添加任务: [cyan]iflow-bot cron add --name \"任务名\" --message \"消息\" --every 60[/cyan]")
        return
    
    table = Table(title="定时任务")
    table.add_column("ID", style="cyan")
    table.add_column("名称")
    table.add_column("调度")
    table.add_column("投递", style="yellow")
    table.add_column("状态")
    table.add_column("下次运行")
    
    import time
    from datetime import datetime as _dt
    
    for job in jobs:
        # 格式化调度信息
        if job.schedule.kind == "every":
            seconds = (job.schedule.every_ms or 0) // 1000
            if seconds >= 86400:
                sched = f"每 {seconds // 86400} 天"
            elif seconds >= 3600:
                sched = f"每 {seconds // 3600} 小时"
            elif seconds >= 60:
                sched = f"每 {seconds // 60} 分钟"
            else:
                sched = f"每 {seconds} 秒"
        elif job.schedule.kind == "cron":
            sched = f"cron: {job.schedule.expr}"
            if job.schedule.tz:
                sched += f" ({job.schedule.tz})"
        elif job.schedule.kind == "at":
            sched = "一次性"
        else:
            sched = "未知"
        
        # 格式化下次运行时间
        next_run = ""
        if job.state.next_run_at_ms:
            ts = job.state.next_run_at_ms / 1000
            next_run = _dt.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M:%S")
        
        # 格式化投递信息
        if job.payload.deliver and job.payload.channel:
            deliver_info = f"[green]{job.payload.channel}[/green]"
            if job.payload.to:
                deliver_info += f":{job.payload.to[:8]}..."
        else:
            deliver_info = "[dim]无[/dim]"
        
        status = "[green]启用[/green]" if job.enabled else "[dim]禁用[/dim]"
        if job.state.last_status == "error":
            status += " [red](错误)[/red]"
        elif job.state.last_status == "ok":
            status += " [green](✓)[/green]"
        
        table.add_row(job.id, job.name, sched, deliver_info, status, next_run)
    
    console.print(table)


@cron_app.command("add")
def cron_add(
    name: str = typer.Option(..., "--name", "-n", help="任务名称"),
    message: str = typer.Option(..., "--message", "-m", help="提醒消息内容"),
    every: int = typer.Option(None, "--every", "-e", help="每隔 N 秒执行"),
    cron_expr: str = typer.Option(None, "--cron", "-c", help="Cron 表达式 (如 '0 9 * * *')"),
    at: Optional[str] = typer.Option(None, "--at", "-a", help="一次性任务，指定执行时间 (ISO格式: 'YYYY-MM-DDTHH:MM:SS')"),
    tz: Optional[str] = typer.Option(None, "--tz", help="时区 (如 'Asia/Shanghai')"),
    deliver: bool = typer.Option(False, "--deliver", "-d", help="投递响应到渠道"),
    to: Optional[str] = typer.Option(None, "--to", help="投递目标 (如用户ID或群组ID)"),
    channel: Optional[str] = typer.Option(None, "--channel", help="投递渠道 (如 telegram, discord)"),
    delete_after_run: bool = typer.Option(False, "--delete-after-run", help="执行后自动删除任务"),
    silent: bool = typer.Option(False, "--silent", "-s", help="静默模式：不投递通知，仅执行任务"),
):
    """添加定时任务。"""
    from datetime import datetime as _dt
    from iflow_bot.cron.service import CronService
    from iflow_bot.cron.types import CronSchedule
    
    # 检查参数冲突
    schedule_count = sum(1 for x in [every, cron_expr, at] if x)
    if schedule_count == 0:
        console.print("[red]错误: 必须指定 --every, --cron 或 --at 其中之一[/red]")
        raise typer.Exit(1)
    
    if schedule_count > 1:
        console.print("[red]错误: --every, --cron 和 --at 不能同时使用[/red]")
        raise typer.Exit(1)
    
    store_path = get_data_dir() / "cron" / "jobs.json"
    service = CronService(store_path)
    
    # 解析调度类型
    if every:
        schedule = CronSchedule(kind="every", every_ms=every * 1000)
    elif cron_expr:
        schedule = CronSchedule(kind="cron", expr=cron_expr, tz=tz)
    elif at:
        # 解析 ISO 格式时间
        try:
            # 尝试解析 ISO 格式
            if "T" in at:
                target_dt = _dt.fromisoformat(at.replace("Z", "+00:00"))
            else:
                # 只有日期，默认为当天 00:00:00
                target_dt = _dt.fromisoformat(at)
            
            at_ms = int(target_dt.timestamp() * 1000)
            schedule = CronSchedule(kind="at", at_ms=at_ms)
        except ValueError:
            console.print(f"[red]错误: 无效的时间格式 '{at}'，请使用 ISO 格式 (如 '2024-12-25T09:00:00')[/red]")
            raise typer.Exit(1)
    
    try:
        job = service.add_job(
            name=name,
            schedule=schedule,
            message=message,
            deliver=deliver,
            channel=channel,
            to=to,
            delete_after_run=delete_after_run,
        )
        
        console.print(f"[green]✓[/green] 已添加定时任务: {job.name} (ID: {job.id})")
        
        if job.state.next_run_at_ms:
            next_run = _dt.fromtimestamp(job.state.next_run_at_ms / 1000)
            console.print(f"[dim]执行时间: {next_run.strftime('%Y-%m-%d %H:%M:%S')}[/dim]")
        
        if at:
            console.print("[dim]类型: 一次性任务（执行后自动禁用）[/dim]")
        
        if silent:
            console.print("[dim]模式: 静默模式（不发送通知）[/dim]")
        elif deliver and channel and to:
            console.print(f"[dim]投递: {channel}:{to[:8]}...[/dim]")
        
        console.print("\n[dim]提示: 无需重启 Gateway，任务会自动加载[/dim]")
        
    except ValueError as e:
        console.print(f"[red]错误: {e}[/red]")
        raise typer.Exit(1)


@cron_app.command("remove")
def cron_remove(
    job_id: str = typer.Argument(..., help="任务 ID"),
):
    """移除定时任务。"""
    from iflow_bot.cron.service import CronService
    
    store_path = get_data_dir() / "cron" / "jobs.json"
    service = CronService(store_path)
    
    if service.remove_job(job_id):
        console.print(f"[green]✓[/green] 已移除任务: {job_id}")
    else:
        console.print(f"[red]错误: 未找到任务 {job_id}[/red]")
        raise typer.Exit(1)


@cron_app.command("enable")
def cron_enable(
    job_id: str = typer.Argument(..., help="任务 ID"),
):
    """启用定时任务。"""
    from iflow_bot.cron.service import CronService
    
    store_path = get_data_dir() / "cron" / "jobs.json"
    service = CronService(store_path)
    
    job = service.enable_job(job_id, enabled=True)
    if job:
        console.print(f"[green]✓[/green] 已启用任务: {job.name} ({job_id})")
    else:
        console.print(f"[red]错误: 未找到任务 {job_id}[/red]")
        raise typer.Exit(1)


@cron_app.command("disable")
def cron_disable(
    job_id: str = typer.Argument(..., help="任务 ID"),
):
    """禁用定时任务。"""
    from iflow_bot.cron.service import CronService
    
    store_path = get_data_dir() / "cron" / "jobs.json"
    service = CronService(store_path)
    
    job = service.enable_job(job_id, enabled=False)
    if job:
        console.print(f"[green]✓[/green] 已禁用任务: {job.name} ({job_id})")
    else:
        console.print(f"[red]错误: 未找到任务 {job_id}[/red]")
        raise typer.Exit(1)


@cron_app.command("run")
def cron_run(
    job_id: str = typer.Argument(..., help="任务 ID"),
    force: bool = typer.Option(False, "--force", "-f", help="强制执行（即使已禁用）"),
):
    """立即执行定时任务。"""
    import asyncio
    from iflow_bot.cron.service import CronService
    
    store_path = get_data_dir() / "cron" / "jobs.json"
    service = CronService(store_path)
    
    job = service.get_job(job_id)
    if not job:
        console.print(f"[red]错误: 未找到任务 {job_id}[/red]")
        raise typer.Exit(1)
    
    console.print(f"[yellow]正在执行任务: {job.name}[/yellow]")
    console.print(f"[dim]消息: {job.payload.message}[/dim]")
    
    async def run_job():
        success = await service.run_job(job_id, force=force)
        if success:
            console.print("[green]✓ 任务执行完成[/green]")
        else:
            console.print("[red]✗ 任务未执行（可能已禁用，使用 --force 强制执行）[/red]")
    
    asyncio.run(run_job())


if __name__ == "__main__":
    app()
