"""iflow 命令透传模块

这个模块用于透传 iflow CLI 的所有命令，确保 iflow-bot 完全兼容 iflow 的功能。
"""
import platform
import subprocess
import sys
from typing import Optional, List
import typer
from rich.console import Console

console = Console()


def _is_windows() -> bool:
    """检查是否为 Windows 平台。"""
    return platform.system().lower() == "windows"


def create_passthrough_app():
    """创建透传 iflow 命令的 Typer 应用"""
    app = typer.Typer(
        name="iflow-passthrough",
        help="Passthrough commands to iflow CLI",
    )
    
    @app.command("mcp")
    def mcp_passthrough(args: List[str] = typer.Argument(None)):
        """管理 MCP 服务器 - 透传到 iflow mcp"""
        _run_iflow(["mcp"] + (args or []))
    
    @app.command("agent")
    def agent_passthrough(args: List[str] = typer.Argument(None)):
        """管理代理 - 透传到 iflow agent"""
        _run_iflow(["agent"] + (args or []))
    
    @app.command("workflow")
    def workflow_passthrough(args: List[str] = typer.Argument(None)):
        """管理工作流 - 透传到 iflow workflow"""
        _run_iflow(["workflow"] + (args or []))
    
    @app.command("skill")
    def skill_passthrough(args: List[str] = typer.Argument(None)):
        """管理技能 - 透传到 iflow skill"""
        _run_iflow(["skill"] + (args or []))
    
    @app.command("commands")
    def commands_passthrough(args: List[str] = typer.Argument(None)):
        """管理市场命令 - 透传到 iflow commands"""
        _run_iflow(["commands"] + (args or []))
    
    return app


def _run_iflow(args: List[str]) -> int:
    """执行 iflow 命令并返回退出码"""
    cmd = ["iflow"] + args
    if _is_windows():
        cmd_str = " ".join(f'"{c}"' if " " in c else c for c in cmd)
        result = subprocess.run(cmd_str, shell=True)
    else:
        result = subprocess.run(cmd)
    return result.returncode


def run_iflow_interactive() -> None:
    """运行 iflow 交互模式"""
    if _is_windows():
        subprocess.run("iflow", shell=True)
    else:
        subprocess.run(["iflow"])
