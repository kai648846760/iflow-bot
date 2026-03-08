import asyncio
from pathlib import Path

import pytest
from loguru import logger

from iflow_bot.bus.events import InboundMessage, OutboundMessage
from iflow_bot.bus.queue import MessageBus
from iflow_bot.channels.feishu import FeishuChannel
from iflow_bot.config.schema import FeishuConfig
from iflow_bot.engine.loop import AgentLoop


class _FakeSessionMappings:
    def clear_session(self, channel: str, chat_id: str) -> bool:
        return True


class _TrackingAdapter:
    def __init__(self):
        self.workspace = Path('/tmp')
        self.mode = 'cli'
        self.session_mappings = _FakeSessionMappings()
        self.active_per_key: dict[str, int] = {}
        self.max_active_per_key: dict[str, int] = {}

    async def chat(self, message: str, channel: str, chat_id: str, model: str):
        key = f"{channel}:{chat_id}"
        cur = self.active_per_key.get(key, 0) + 1
        self.active_per_key[key] = cur
        self.max_active_per_key[key] = max(cur, self.max_active_per_key.get(key, 0))
        await asyncio.sleep(0.03)
        self.active_per_key[key] -= 1
        return f"ok:{key}"

    async def chat_stream(self, message: str, channel: str, chat_id: str, model: str, on_chunk):
        await on_chunk(channel, chat_id, "x")
        return "x"


@pytest.mark.asyncio
async def test_bus_queue_overflow_drops_extra_messages():
    bus = MessageBus(max_size=1)
    await bus.publish_outbound(OutboundMessage(channel='telegram', chat_id='c1', content='m1'))
    await bus.publish_outbound(OutboundMessage(channel='telegram', chat_id='c1', content='m2'))

    assert bus.outbound_size == 1
    first = await bus.consume_outbound()
    assert first.content == 'm1'


@pytest.mark.asyncio
async def test_loop_same_user_is_serialized():
    bus = MessageBus()
    adapter = _TrackingAdapter()
    loop = AgentLoop(bus=bus, adapter=adapter, model='kimi-k2.5', streaming=False)

    m1 = InboundMessage(channel='feishu', sender_id='u1', chat_id='same', content='a', metadata={'message_id': '1'})
    m2 = InboundMessage(channel='feishu', sender_id='u1', chat_id='same', content='b', metadata={'message_id': '2'})

    await asyncio.gather(loop._process_message(m1), loop._process_message(m2))

    assert adapter.max_active_per_key.get('feishu:same', 0) == 1


@pytest.mark.asyncio
async def test_feishu_streaming_failure_path_logs(monkeypatch):
    import iflow_bot.channels.feishu as feishu_mod

    ch = FeishuChannel(FeishuConfig(app_id='x', app_secret='y', enabled=True), MessageBus())
    ch._client = object()
    ch._streaming_message_ids['ou_1'] = 'msg_old'

    async def fake_remove(_):
        return None

    calls = []

    def fake_patch(*_args, **_kwargs):
        return False

    def fake_send(receive_id_type, receive_id, msg_type, content):
        calls.append((receive_id_type, receive_id, msg_type, content))
        # interactive fails, text fallback succeeds
        if msg_type == 'interactive':
            return None
        if msg_type == 'text':
            return 'msg_text'
        return None

    observed_logs: list[str] = []

    def fake_warning(msg, *args, **kwargs):
        observed_logs.append((str(msg) % args) if args else str(msg))

    def fake_error(msg, *args, **kwargs):
        observed_logs.append((str(msg) % args) if args else str(msg))

    monkeypatch.setattr(ch, '_remove_typing_reaction', fake_remove)
    monkeypatch.setattr(ch, '_patch_message_sync', fake_patch)
    monkeypatch.setattr(ch, '_send_message_sync', fake_send)
    monkeypatch.setattr(feishu_mod.logger, 'warning', fake_warning)
    monkeypatch.setattr(feishu_mod.logger, 'error', fake_error)

    await ch._handle_streaming_message(
        OutboundMessage(
            channel='feishu',
            chat_id='ou_1',
            content='hello failover',
            metadata={'_streaming': True, 'reply_to_id': 'src1'},
        ),
        'open_id',
    )

    assert [c[2] for c in calls] == ['interactive', 'text']
    lower_logs = [line.lower() for line in observed_logs]
    assert any('patch failed' in line and 'recreating' in line for line in lower_logs)
    assert any('placeholder send failed' in line for line in lower_logs)
    assert any('text fallback' in line for line in lower_logs)
