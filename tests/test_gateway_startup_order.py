from __future__ import annotations

import types
from pathlib import Path

import pytest

from iflow_bot.cli import commands


class _FakeConfig:
    def __init__(self, workspace: Path):
        self.driver = types.SimpleNamespace(
            mode="stdio",
            thinking=False,
            timeout=30,
            acp_port=8090,
            compression_trigger_tokens=88888,
            mcp_proxy_port=8888,
            mcp_servers_auto_discover=True,
            mcp_servers_max=10,
            mcp_servers_allowlist=None,
            mcp_servers_blocklist=None,
        )
        self._workspace = workspace

    def get_workspace(self):
        return self._workspace

    def get_model(self):
        return "glm-5"

    def get_timeout(self):
        return 30


@pytest.mark.asyncio
async def test_run_gateway_starts_agent_loop_before_channels(monkeypatch, tmp_path: Path):
    order: list[str] = []
    fake_agent_loop_holder: dict[str, object] = {}

    class FakeMessageBus:
        pass

    class FakeAdapter:
        def __init__(self, **kwargs):
            self.kwargs = kwargs
            self._stdio_adapter = None

        async def _get_stdio_adapter(self):
            order.append("preheat")
            return object()

        async def close(self):
            order.append("adapter.close")

    class FakeAgentLoop:
        def __init__(self, **kwargs):
            self.started = False
            fake_agent_loop_holder["instance"] = self

        async def start_background(self):
            self.started = True
            order.append("agent.start_background")

        def stop(self):
            order.append("agent.stop")

        async def process_direct(self, *args, **kwargs):
            return ""

    class FakeChannelManager:
        def __init__(self, config, bus):
            self.enabled_channels = ["feishu"]

        async def start_all(self):
            started = getattr(fake_agent_loop_holder.get("instance"), "started", False)
            order.append(f"channels.start_all(agent_started={started})")
            raise KeyboardInterrupt()

        async def stop_all(self):
            order.append("channels.stop_all")

    class FakeCronService:
        def __init__(self, *args, **kwargs):
            self.on_job = None

        async def start(self):
            order.append("cron.start")

        def stop(self):
            order.append("cron.stop")

        def status(self):
            return {"jobs": 0}

    class FakeHeartbeatService:
        def __init__(self, *args, **kwargs):
            pass

        async def start(self):
            order.append("heartbeat.start")

        def stop(self):
            order.append("heartbeat.stop")

    monkeypatch.setattr("iflow_bot.bus.MessageBus", FakeMessageBus)
    monkeypatch.setattr("iflow_bot.engine.IFlowAdapter", FakeAdapter)
    monkeypatch.setattr("iflow_bot.engine.loop.AgentLoop", FakeAgentLoop)
    monkeypatch.setattr("iflow_bot.channels.ChannelManager", FakeChannelManager)
    monkeypatch.setattr("iflow_bot.cron.service.CronService", FakeCronService)
    monkeypatch.setattr("iflow_bot.heartbeat.service.HeartbeatService", FakeHeartbeatService)
    monkeypatch.setattr(commands, "get_data_dir", lambda: tmp_path)

    await commands._run_gateway(_FakeConfig(tmp_path))

    assert "agent.start_background" in order
    assert "channels.start_all(agent_started=True)" in order
