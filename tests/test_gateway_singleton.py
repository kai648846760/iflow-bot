from pathlib import Path

from typer.testing import CliRunner

from iflow_bot.cli.commands import app
import iflow_bot.cli.commands as commands


runner = CliRunner()


def test_gateway_run_refuses_when_pid_file_points_to_live_process(tmp_path: Path, monkeypatch):
    pid_file = tmp_path / "gateway.pid"
    pid_file.write_text("4321", encoding="utf-8")

    monkeypatch.setattr(commands, "print_banner", lambda: None)
    monkeypatch.setattr(commands, "load_config", lambda: None)
    monkeypatch.setattr(commands, "get_pid_file", lambda: pid_file)
    monkeypatch.setattr(commands, "process_exists", lambda pid: pid == 4321)

    result = runner.invoke(app, ["gateway", "run"])

    assert result.exit_code == 0
    assert "Gateway already running (PID: 4321)" in result.stdout


def test_gateway_run_ignores_stale_pid_file_and_continues(tmp_path: Path, monkeypatch):
    pid_file = tmp_path / "gateway.pid"
    pid_file.write_text("4321", encoding="utf-8")

    class _Driver:
        mcp_proxy_auto_start = False

    class _Config:
        driver = _Driver()

        def get_workspace(self):
            return str(tmp_path / "workspace")

        def get_enabled_channels(self):
            return ["feishu"]

        def get_model(self):
            return "glm-5"

    invoked = {"run": False}

    monkeypatch.setattr(commands, "print_banner", lambda: None)
    monkeypatch.setattr(commands, "load_config", lambda: _Config())
    monkeypatch.setattr(commands, "get_pid_file", lambda: pid_file)
    monkeypatch.setattr(commands, "process_exists", lambda pid: False)
    monkeypatch.setattr(commands, "ensure_iflow_ready", lambda: True)
    monkeypatch.setattr(commands, "init_workspace", lambda workspace: None)
    def _fake_asyncio_run(coro):
        invoked["run"] = True
        coro.close()

    monkeypatch.setattr(commands.asyncio, "run", _fake_asyncio_run)
    monkeypatch.setattr(commands.os, "getpid", lambda: 9999)

    result = runner.invoke(app, ["gateway", "run"])

    assert result.exit_code == 0
    assert invoked["run"] is True
    assert pid_file.exists() is False


def test_gateway_run_claims_pid_file_for_foreground_process(tmp_path: Path, monkeypatch):
    pid_file = tmp_path / "gateway.pid"

    class _Driver:
        mcp_proxy_auto_start = False

    class _Config:
        driver = _Driver()

        def get_workspace(self):
            return str(tmp_path / "workspace")

        def get_enabled_channels(self):
            return ["feishu"]

        def get_model(self):
            return "glm-5"

    captured = {"pid_during_run": None}

    monkeypatch.setattr(commands, "print_banner", lambda: None)
    monkeypatch.setattr(commands, "load_config", lambda: _Config())
    monkeypatch.setattr(commands, "get_pid_file", lambda: pid_file)
    monkeypatch.setattr(commands, "process_exists", lambda pid: False)
    monkeypatch.setattr(commands, "ensure_iflow_ready", lambda: True)
    monkeypatch.setattr(commands, "init_workspace", lambda workspace: None)
    monkeypatch.setattr(commands.os, "getpid", lambda: 9999)

    def _fake_asyncio_run(coro):
        captured["pid_during_run"] = pid_file.read_text(encoding="utf-8")
        coro.close()

    monkeypatch.setattr(commands.asyncio, "run", _fake_asyncio_run)

    result = runner.invoke(app, ["gateway", "run"])

    assert result.exit_code == 0
    assert captured["pid_during_run"] == "9999"
    assert pid_file.exists() is False


def test_hidden_run_gateway_refuses_when_another_live_gateway_exists(tmp_path: Path, monkeypatch):
    pid_file = tmp_path / "gateway.pid"
    pid_file.write_text("4321", encoding="utf-8")

    invoked = {"run": False}

    monkeypatch.setattr(commands, "load_config", lambda: object())
    monkeypatch.setattr(commands, "get_pid_file", lambda: pid_file)
    monkeypatch.setattr(commands, "process_exists", lambda pid: pid == 4321)
    monkeypatch.setattr(commands.os, "getpid", lambda: 9999)

    def _fake_asyncio_run(coro):
        invoked["run"] = True
        coro.close()

    monkeypatch.setattr(commands.asyncio, "run", _fake_asyncio_run)

    result = runner.invoke(app, ["_run_gateway"])

    assert result.exit_code == 0
    assert "Gateway already running (PID: 4321)" in result.stdout
    assert invoked["run"] is False


def test_hidden_run_gateway_claims_pid_file_for_current_process(tmp_path: Path, monkeypatch):
    pid_file = tmp_path / "gateway.pid"

    captured = {"pid_during_run": None}

    monkeypatch.setattr(commands, "load_config", lambda: object())
    monkeypatch.setattr(commands, "get_pid_file", lambda: pid_file)
    monkeypatch.setattr(commands, "process_exists", lambda pid: False)
    monkeypatch.setattr(commands.os, "getpid", lambda: 9999)

    def _fake_asyncio_run(coro):
        captured["pid_during_run"] = pid_file.read_text(encoding="utf-8")
        coro.close()

    monkeypatch.setattr(commands.asyncio, "run", _fake_asyncio_run)

    result = runner.invoke(app, ["_run_gateway"])

    assert result.exit_code == 0
    assert captured["pid_during_run"] == "9999"
    assert pid_file.exists() is False
