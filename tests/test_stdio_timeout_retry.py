import asyncio
import time
from pathlib import Path

import pytest

from iflow_bot.engine.stdio_acp import (
    ACPResponse,
    StdioACPAdapter,
    StdioACPClient,
    StdioACPTimeoutError,
    StopReason,
)


class _TimeoutThenSuccessClient:
    def __init__(self, fail_times=2):
        self.fail_times = fail_times
        self.calls = []
        self.cancels = []

    async def prompt(self, session_id, message, timeout, on_chunk=None, on_tool_call=None, on_event=None):
        self.calls.append({"session_id": session_id, "timeout": timeout, "message": message})
        if self.fail_times > 0:
            self.fail_times -= 1
            raise StdioACPTimeoutError("Prompt timeout (idle)")
        return ACPResponse(content="ok after retries", error=None)

    async def cancel(self, session_id):
        self.cancels.append(session_id)


class _HangingAuthClient:
    def __init__(self):
        self.started = False
        self.initialized = False

    async def start(self):
        self.started = True

    async def initialize(self):
        self.initialized = True

    async def authenticate(self, method_id="iflow"):
        await asyncio.Future()

    async def is_connected(self):
        return True

    async def stop(self):
        return None


class _CountingHangingAuthClient:
    def __init__(self):
        self.started = 0
        self.initialized = 0
        self.auth_calls = 0

    async def start(self):
        self.started += 1

    async def initialize(self):
        self.initialized += 1

    async def authenticate(self, method_id="iflow"):
        self.auth_calls += 1
        await asyncio.Future()

    async def is_connected(self):
        return True

    async def stop(self):
        return None


class _DisconnectedClient:
    def __init__(self):
        self.stop_called = False

    async def is_connected(self):
        return False

    async def stop(self):
        self.stop_called = True


class _HealthyReconnectClient:
    def __init__(self):
        self.started = False
        self.initialized = False
        self.authenticated = False

    async def start(self):
        self.started = True

    async def initialize(self):
        self.initialized = True

    async def authenticate(self, method_id="iflow"):
        self.authenticated = True
        return True

    async def is_connected(self):
        return True

    async def stop(self):
        return None


class _FakePromptStdin:
    def __init__(self):
        self.writes = []

    def write(self, data):
        self.writes.append(data)

    async def drain(self):
        return None


class _FakePromptProcess:
    def __init__(self):
        self.stdin = _FakePromptStdin()


@pytest.mark.asyncio
async def test_chat_stream_retries_timeout_with_increasing_budget(monkeypatch, tmp_path):
    adapter = StdioACPAdapter(workspace=tmp_path, timeout=10)
    adapter._session_map_file = tmp_path / 'session_map.json'
    client = _TimeoutThenSuccessClient(fail_times=2)
    adapter._client = client

    sessions = iter(["sess-1", "sess-2", "sess-3"])

    async def fake_get_or_create(_channel, _chat_id, _model=None):
        return next(sessions)

    async def fake_maybe_compress(_key, _channel, _chat_id, sid, msg, _model):
        return sid, msg

    async def fake_invalidate(_key):
        return "old-sess"

    async def fake_create_new(_key, _model=None):
        return next(sessions)

    monkeypatch.setattr(adapter, "_get_or_create_session", fake_get_or_create)
    monkeypatch.setattr(adapter, "_maybe_compress_active_session", fake_maybe_compress)
    monkeypatch.setattr(adapter, "_invalidate_session", fake_invalidate)
    monkeypatch.setattr(adapter, "_create_new_session", fake_create_new)

    out = await adapter.chat_stream("hello", channel="feishu", chat_id="u1")

    assert out == "ok after retries"
    assert [call["timeout"] for call in client.calls] == [10, 20, 40]
    assert client.cancels == ["sess-1", "sess-2"]


@pytest.mark.asyncio
async def test_connect_does_not_block_long_on_auth_timeout(tmp_path):
    adapter = StdioACPAdapter(workspace=tmp_path, timeout=600)
    adapter._client = _HangingAuthClient()
    adapter._auth_timeout_seconds = 0.05

    start = time.perf_counter()
    await adapter.connect()
    elapsed = time.perf_counter() - start

    assert elapsed < 0.5


@pytest.mark.asyncio
async def test_connect_skips_repeated_auth_timeout_for_same_client(tmp_path):
    adapter = StdioACPAdapter(workspace=tmp_path, timeout=600)
    client = _CountingHangingAuthClient()
    adapter._client = client
    adapter._auth_timeout_seconds = 0.05

    start = time.perf_counter()
    await adapter.connect()
    first_elapsed = time.perf_counter() - start

    start = time.perf_counter()
    await adapter.connect()
    second_elapsed = time.perf_counter() - start

    assert first_elapsed < 0.5
    assert second_elapsed < 0.02
    assert client.auth_calls == 1


@pytest.mark.asyncio
async def test_connect_recreates_dead_client_before_auth(monkeypatch, tmp_path):
    adapter = StdioACPAdapter(workspace=tmp_path, timeout=600)
    dead = _DisconnectedClient()
    fresh = _HealthyReconnectClient()
    adapter._client = dead

    monkeypatch.setattr("iflow_bot.engine.stdio_acp.StdioACPClient", lambda *args, **kwargs: fresh)

    await adapter.connect()

    assert dead.stop_called is True
    assert adapter._client is fresh
    assert fresh.started is True
    assert fresh.initialized is True
    assert fresh.authenticated is True


@pytest.mark.asyncio
async def test_prompt_does_not_fail_early_when_tool_call_fails_but_prompt_completes(tmp_path: Path):
    client = StdioACPClient(workspace=tmp_path, timeout=1)
    client._started = True
    client._process = _FakePromptProcess()

    session_id = "sess-1"

    async def feed_updates():
        while session_id not in client._session_queues or not client._pending_requests:
            await asyncio.sleep(0)

        queue = client._session_queues[session_id]
        await queue.put(
            {
                "method": "session/update",
                "params": {
                    "sessionId": session_id,
                    "update": {
                        "sessionUpdate": "tool_call",
                        "toolCallId": "tc-1",
                        "name": "shell",
                        "args": {"command": "broken"},
                    },
                },
            }
        )
        await asyncio.sleep(0)
        await queue.put(
            {
                "method": "session/update",
                "params": {
                    "sessionId": session_id,
                    "update": {
                        "sessionUpdate": "tool_call_update",
                        "toolCallId": "tc-1",
                        "status": "failed",
                        "content": [{"type": "text", "text": "command failed"}],
                    },
                },
            }
        )
        await asyncio.sleep(0.05)
        await queue.put(
            {
                "method": "session/update",
                "params": {
                    "sessionId": session_id,
                    "update": {
                        "sessionUpdate": "agent_message_chunk",
                        "content": {"type": "text", "text": "recovered after tool failure"},
                    },
                },
            }
        )
        future = next(iter(client._pending_requests.values()))
        if not future.done():
            future.set_result({"result": {"stopReason": "end_turn"}})

    feeder = asyncio.create_task(feed_updates())
    response = await client.prompt(session_id, "hello", timeout=1)
    await feeder

    assert response.error is None
    assert response.stop_reason == StopReason.END_TURN
    assert response.content == "recovered after tool failure"
    assert response.tool_calls[0].status == "failed"


@pytest.mark.asyncio
async def test_prompt_falls_back_to_result_payload_when_chunks_are_absent(tmp_path: Path):
    client = StdioACPClient(workspace=tmp_path, timeout=1)
    client._started = True
    client._process = _FakePromptProcess()

    session_id = "sess-final-only"

    async def feed_result_only():
        while session_id not in client._session_queues or not client._pending_requests:
            await asyncio.sleep(0)

        future = next(iter(client._pending_requests.values()))
        if not future.done():
            future.set_result(
                {
                    "result": {
                        "stopReason": "end_turn",
                        "content": [
                            {"type": "text", "text": "# PRD\n\n- story 1"}
                        ],
                    }
                }
            )

    feeder = asyncio.create_task(feed_result_only())
    response = await client.prompt(session_id, "hello", timeout=1)
    await feeder

    assert response.error is None
    assert response.stop_reason == StopReason.END_TURN
    assert response.content == "# PRD\n\n- story 1"


@pytest.mark.asyncio
async def test_connect_skips_repeated_auth_timeout_across_recreated_clients(monkeypatch, tmp_path):
    adapter = StdioACPAdapter(workspace=tmp_path, timeout=600)
    first = _CountingHangingAuthClient()
    second = _CountingHangingAuthClient()
    adapter._client = first
    adapter._auth_timeout_seconds = 0.05

    start = time.perf_counter()
    await adapter.connect()
    first_elapsed = time.perf_counter() - start

    dead = _DisconnectedClient()
    adapter._client = dead
    monkeypatch.setattr("iflow_bot.engine.stdio_acp.StdioACPClient", lambda *args, **kwargs: second)

    start = time.perf_counter()
    await adapter.connect()
    second_elapsed = time.perf_counter() - start

    assert first_elapsed < 0.5
    assert second_elapsed < 0.02
    assert dead.stop_called is True
    assert first.auth_calls == 1
    assert second.auth_calls == 0
