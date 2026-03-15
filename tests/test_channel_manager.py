import asyncio
import logging

import pytest

from iflow_bot.bus.queue import MessageBus
from iflow_bot.channels.base import BaseChannel
from iflow_bot.channels.manager import ChannelManager, register_channel, _CHANNEL_REGISTRY
from iflow_bot.config.schema import Config


class _DummyConfig:
    enabled = True
    allow_from = []


@register_channel("_test_ok")
class _OkChannel(BaseChannel):
    async def start(self) -> None:
        self._running = True

    async def stop(self) -> None:
        self._running = False

    async def send(self, msg) -> None:
        return None


@register_channel("_test_fail")
class _FailChannel(BaseChannel):
    async def start(self) -> None:
        await asyncio.sleep(1.1)
        raise asyncio.TimeoutError("boom")

    async def stop(self) -> None:
        self._running = False

    async def send(self, msg) -> None:
        return None


@pytest.fixture
def config_with_test_channels(monkeypatch):
    config = Config()
    monkeypatch.setattr(Config, "get_enabled_channels", lambda self: ["_test_ok", "_test_fail"])
    return config


@pytest.mark.asyncio
async def test_start_all_keeps_successful_channels_when_one_fails(config_with_test_channels, caplog):
    manager = ChannelManager(config_with_test_channels, MessageBus())
    channels = {
        "_test_ok": _OkChannel(_DummyConfig(), manager.bus),
        "_test_fail": _FailChannel(_DummyConfig(), manager.bus),
    }
    manager._create_channel = channels.get

    with caplog.at_level(logging.ERROR):
        await manager.start_all()
        await asyncio.sleep(1.15)

    assert "_test_ok" in manager.channels
    assert manager.get_channel("_test_ok").is_running is True
    assert "_test_fail" not in manager.channels
    assert any("failed to start" in rec.message for rec in caplog.records)

    await manager.stop_all()


@pytest.mark.asyncio
async def test_start_all_consumes_late_start_exception(config_with_test_channels):
    manager = ChannelManager(config_with_test_channels, MessageBus())
    channels = {
        "_test_ok": _OkChannel(_DummyConfig(), manager.bus),
        "_test_fail": _FailChannel(_DummyConfig(), manager.bus),
    }
    manager._create_channel = channels.get

    await manager.start_all()
    await asyncio.sleep(1.15)

    task = manager._channel_tasks.get("_test_fail")
    assert task is None
    assert "_test_fail" not in manager.channels
