from __future__ import annotations

import asyncio
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


@pytest.mark.asyncio
async def test_run_gateway_continues_when_stdio_prewarm_times_out(monkeypatch, tmp_path: Path):
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
            await asyncio.Future()

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

    assert "preheat" in order
    assert "agent.start_background" in order
    assert "channels.start_all(agent_started=True)" in order


@pytest.mark.asyncio
async def test_run_gateway_stdio_prewarm_budget_covers_auth_timeout(monkeypatch, tmp_path: Path):
    order: list[str] = []
    fake_agent_loop_holder: dict[str, object] = {}
    real_wait_for = asyncio.wait_for

    class FakeMessageBus:
        pass

    class FakeAdapter:
        def __init__(self, **kwargs):
            self.kwargs = kwargs
            self._stdio_adapter = None

        async def _get_stdio_adapter(self):
            order.append("preheat.start")
            await asyncio.sleep(0.082)
            order.append("preheat.done")
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

    async def scaled_wait_for(awaitable, timeout):
        return await real_wait_for(awaitable, timeout / 100.0)

    monkeypatch.setattr(commands.asyncio, "wait_for", scaled_wait_for)
    monkeypatch.setattr("iflow_bot.bus.MessageBus", FakeMessageBus)
    monkeypatch.setattr("iflow_bot.engine.IFlowAdapter", FakeAdapter)
    monkeypatch.setattr("iflow_bot.engine.loop.AgentLoop", FakeAgentLoop)
    monkeypatch.setattr("iflow_bot.channels.ChannelManager", FakeChannelManager)
    monkeypatch.setattr("iflow_bot.cron.service.CronService", FakeCronService)
    monkeypatch.setattr("iflow_bot.heartbeat.service.HeartbeatService", FakeHeartbeatService)
    monkeypatch.setattr(commands, "get_data_dir", lambda: tmp_path)

    await commands._run_gateway(_FakeConfig(tmp_path))

    assert "preheat.done" in order
    assert "agent.start_background" in order
    assert "channels.start_all(agent_started=True)" in order


@pytest.mark.asyncio
async def test_run_gateway_keeps_stdio_adapter_when_prewarm_completes_before_timeout(monkeypatch, tmp_path: Path):
    order: list[str] = []
    fake_adapter_holder: dict[str, object] = {}

    class FakeMessageBus:
        pass

    class FakeStdioAdapter:
        def __init__(self):
            self.disconnected = False

        async def disconnect(self):
            self.disconnected = True
            order.append('stdio.disconnect')

    class FakeAdapter:
        def __init__(self, **kwargs):
            self.kwargs = kwargs
            self._stdio_adapter = None
            fake_adapter_holder['instance'] = self

        async def _get_stdio_adapter(self):
            order.append('preheat')
            self._stdio_adapter = FakeStdioAdapter()
            await asyncio.sleep(0.09)
            return self._stdio_adapter

        async def close(self):
            order.append('adapter.close')

    class FakeAgentLoop:
        def __init__(self, **kwargs):
            pass

        async def start_background(self):
            order.append('agent.start_background')

        def stop(self):
            order.append('agent.stop')

        async def process_direct(self, *args, **kwargs):
            return ''

    class FakeChannelManager:
        def __init__(self, config, bus):
            self.enabled_channels = ['feishu']

        async def start_all(self):
            order.append('channels.start_all')
            raise KeyboardInterrupt()

        async def stop_all(self):
            order.append('channels.stop_all')

    class FakeCronService:
        def __init__(self, *args, **kwargs):
            self.on_job = None

        async def start(self):
            order.append('cron.start')

        def stop(self):
            order.append('cron.stop')

        def status(self):
            return {'jobs': 0}

    class FakeHeartbeatService:
        def __init__(self, *args, **kwargs):
            pass

        async def start(self):
            order.append('heartbeat.start')

        def stop(self):
            order.append('heartbeat.stop')

    monkeypatch.setattr('iflow_bot.bus.MessageBus', FakeMessageBus)
    monkeypatch.setattr('iflow_bot.engine.IFlowAdapter', FakeAdapter)
    monkeypatch.setattr('iflow_bot.engine.loop.AgentLoop', FakeAgentLoop)
    monkeypatch.setattr('iflow_bot.channels.ChannelManager', FakeChannelManager)
    monkeypatch.setattr('iflow_bot.cron.service.CronService', FakeCronService)
    monkeypatch.setattr('iflow_bot.heartbeat.service.HeartbeatService', FakeHeartbeatService)
    monkeypatch.setattr(commands, 'get_data_dir', lambda: tmp_path)
    monkeypatch.setattr(asyncio, 'wait_for', lambda coro, timeout: coro)

    await commands._run_gateway(_FakeConfig(tmp_path))

    adapter = fake_adapter_holder['instance']
    assert 'preheat' in order
    assert 'stdio.disconnect' not in order
    assert adapter._stdio_adapter is not None
    assert adapter._stdio_adapter.disconnected is False


@pytest.mark.asyncio
async def test_run_gateway_uses_long_enough_stdio_prewarm_timeout(monkeypatch, tmp_path: Path):
    seen: dict[str, float] = {}

    class FakeMessageBus:
        pass

    class FakeAdapter:
        def __init__(self, **kwargs):
            self._stdio_adapter = None

        async def _get_stdio_adapter(self):
            return object()

        async def close(self):
            return None

    class FakeAgentLoop:
        def __init__(self, **kwargs):
            pass

        async def start_background(self):
            return None

        def stop(self):
            return None

        async def process_direct(self, *args, **kwargs):
            return ''

    class FakeChannelManager:
        def __init__(self, config, bus):
            self.enabled_channels = ['feishu']

        async def start_all(self):
            raise KeyboardInterrupt()

        async def stop_all(self):
            return None

    class FakeCronService:
        def __init__(self, *args, **kwargs):
            self.on_job = None

        async def start(self):
            return None

        def stop(self):
            return None

        def status(self):
            return {'jobs': 0}

    class FakeHeartbeatService:
        def __init__(self, *args, **kwargs):
            pass

        async def start(self):
            return None

        def stop(self):
            return None

    async def fake_wait_for(awaitable, timeout):
        seen['timeout'] = timeout
        return await awaitable

    monkeypatch.setattr('iflow_bot.bus.MessageBus', FakeMessageBus)
    monkeypatch.setattr('iflow_bot.engine.IFlowAdapter', FakeAdapter)
    monkeypatch.setattr('iflow_bot.engine.loop.AgentLoop', FakeAgentLoop)
    monkeypatch.setattr('iflow_bot.channels.ChannelManager', FakeChannelManager)
    monkeypatch.setattr('iflow_bot.cron.service.CronService', FakeCronService)
    monkeypatch.setattr('iflow_bot.heartbeat.service.HeartbeatService', FakeHeartbeatService)
    monkeypatch.setattr(commands, 'get_data_dir', lambda: tmp_path)
    monkeypatch.setattr(asyncio, 'wait_for', fake_wait_for)

    await commands._run_gateway(_FakeConfig(tmp_path))

    assert seen['timeout'] >= 12.0
