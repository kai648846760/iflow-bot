from pathlib import Path

import pytest
from loguru import logger

from iflow_bot.engine.stdio_acp import ACPResponse, StdioACPAdapter


class _FakeClient:
    def __init__(self, responses):
        self._responses = list(responses)
        self.prompt_calls = []

    async def prompt(self, session_id, message, timeout, on_chunk=None, on_tool_call=None, on_event=None):
        self.prompt_calls.append(
            {
                "session_id": session_id,
                "message": message,
                "timeout": timeout,
                "has_on_chunk": on_chunk is not None,
            }
        )
        if not self._responses:
            return ACPResponse(content="", error="no fake response")
        return self._responses.pop(0)


@pytest.mark.asyncio
async def test_active_compress_rotate_path(monkeypatch, tmp_path):
    adapter = StdioACPAdapter(workspace=tmp_path)
    adapter._session_map_file = tmp_path / "session_map.json"

    async def fake_invalidate(_key):
        return "old-session"

    async def fake_create_new(_key, _model=None):
        return "new-session"

    monkeypatch.setattr(adapter, "_estimate_session_history_tokens", lambda _sid: 70000)
    monkeypatch.setattr(adapter, "_extract_conversation_history", lambda *_args, **_kwargs: "<history_context>compact</history_context>")
    monkeypatch.setattr(adapter, "_apply_compression_constraints", lambda h, *_a, **_k: h)
    monkeypatch.setattr(adapter, "_schedule_persist_compression_snapshot", lambda **_kwargs: None)
    monkeypatch.setattr(adapter, "_invalidate_session", fake_invalidate)
    monkeypatch.setattr(adapter, "_create_new_session", fake_create_new)

    sid, msg = await adapter._maybe_compress_active_session(
        key="feishu:ou_1",
        channel="feishu",
        chat_id="ou_1",
        session_id="old-session",
        message="用户消息: hello",
        model=None,
    )

    assert sid == "new-session"
    assert "<history_context>compact</history_context>" in msg
    assert "用户消息: hello" in msg


@pytest.mark.asyncio
async def test_chat_stream_empty_response_triggers_compact_retry(monkeypatch, tmp_path):
    adapter = StdioACPAdapter(workspace=tmp_path)
    adapter._session_map_file = tmp_path / "session_map.json"

    fake_client = _FakeClient(
        [
            ACPResponse(content="", error=None),
            ACPResponse(content="recovered stream", error=None),
        ]
    )
    adapter._client = fake_client

    async def fake_get_or_create(_channel, _chat_id, _model=None):
        return "sess-1"

    async def fake_maybe_compress(_key, _channel, _chat_id, sid, msg, _model):
        return sid, msg

    async def fake_invalidate(_key):
        return "sess-1"

    async def fake_create_new(_key, _model=None):
        return "sess-2"

    monkeypatch.setattr(adapter, "_get_or_create_session", fake_get_or_create)
    monkeypatch.setattr(adapter, "_maybe_compress_active_session", fake_maybe_compress)
    monkeypatch.setattr(adapter, "_invalidate_session", fake_invalidate)
    monkeypatch.setattr(adapter, "_extract_conversation_history", lambda *_a, **_k: "<history_context>short</history_context>")
    monkeypatch.setattr(adapter, "_apply_compression_constraints", lambda h, *_a, **_k: h)
    monkeypatch.setattr(adapter, "_schedule_persist_compression_snapshot", lambda **_kwargs: None)
    monkeypatch.setattr(adapter, "_create_new_session", fake_create_new)

    logs = []
    hid = logger.add(logs.append, format="{message}")
    try:
        out = await adapter.chat_stream(
            message="用户消息: ping",
            channel="feishu",
            chat_id="ou_1",
            model=None,
        )
    finally:
        logger.remove(hid)

    assert out == "recovered stream"
    assert len(fake_client.prompt_calls) == 2
    assert fake_client.prompt_calls[1]["session_id"] == "sess-2"
    assert "<history_context>short</history_context>" in fake_client.prompt_calls[1]["message"]
    assert any("Empty stream response detected" in line for line in logs)


@pytest.mark.asyncio
async def test_chat_context_overflow_triggers_compact_retry(monkeypatch, tmp_path):
    adapter = StdioACPAdapter(workspace=tmp_path)
    adapter._session_map_file = tmp_path / "session_map.json"

    fake_client = _FakeClient(
        [
            ACPResponse(content="", error="context length exceeded"),
            ACPResponse(content="recovered non-stream", error=None),
        ]
    )
    adapter._client = fake_client

    async def fake_get_or_create(_channel, _chat_id, _model=None):
        return "sess-a"

    async def fake_maybe_compress(_key, _channel, _chat_id, sid, msg, _model):
        return sid, msg

    async def fake_invalidate(_key):
        return "sess-a"

    async def fake_create_new(_key, _model=None):
        return "sess-b"

    monkeypatch.setattr(adapter, "_get_or_create_session", fake_get_or_create)
    monkeypatch.setattr(adapter, "_maybe_compress_active_session", fake_maybe_compress)
    monkeypatch.setattr(adapter, "_invalidate_session", fake_invalidate)
    monkeypatch.setattr(adapter, "_extract_conversation_history", lambda *_a, **_k: "<history_context>retry</history_context>")
    monkeypatch.setattr(adapter, "_apply_compression_constraints", lambda h, *_a, **_k: h)
    monkeypatch.setattr(adapter, "_schedule_persist_compression_snapshot", lambda **_kwargs: None)
    monkeypatch.setattr(adapter, "_create_new_session", fake_create_new)

    logs = []
    hid = logger.add(logs.append, format="{message}")
    try:
        out = await adapter.chat(
            message="用户消息: ping",
            channel="feishu",
            chat_id="ou_1",
            model=None,
        )
    finally:
        logger.remove(hid)

    assert out == "recovered non-stream"
    assert len(fake_client.prompt_calls) == 2
    assert fake_client.prompt_calls[1]["session_id"] == "sess-b"
    assert "<history_context>retry</history_context>" in fake_client.prompt_calls[1]["message"]
    assert any("Context overflow suspected" in line for line in logs)


def test_extract_conversation_history_supports_message_source_format(monkeypatch, tmp_path):
    adapter = StdioACPAdapter(workspace=tmp_path)
    session_dir = tmp_path / ".iflow" / "acp" / "sessions"
    session_dir.mkdir(parents=True, exist_ok=True)
    session_id = "sess-history"
    session_file = session_dir / f"{session_id}.json"
    session_file.write_text(
        """
{
  "sessionId": "sess-history",
  "createdAt": "2026-03-17T08:00:00.000Z",
  "chatHistory": [
    {
      "role": "user",
      "parts": [
        {
          "text": "<system-reminder>ignore</system-reminder>"
        },
        {
          "text": "[message_source]\\nchannel: feishu\\nchat_id: ou_test\\nsession: feishu:ou_test\\ntime: 2026-03-17 16:00:01\\n[/message_source]\\n\\n[language]\\nlocale: zh-CN\\npolicy: 回复必须严格使用中文（简体），不要混用其他语言。\\n[/language]\\n\\n压测消息 001：请只回复 OK。上下文填充。"
        }
      ]
    },
    {
      "role": "model",
      "parts": [{"text": "OK"}]
    }
  ]
}
        """.strip(),
        encoding="utf-8",
    )

    monkeypatch.setattr(adapter, "_find_session_file", lambda _session_id: session_file)

    history = adapter._extract_conversation_history(session_id, max_turns=10, token_budget=500)

    assert history is not None
    assert "压测消息 001：请只回复 OK。上下文填充。" in history
    assert "我：OK" in history


def test_get_session_status_uses_fast_history_token_estimate(monkeypatch, tmp_path):
    adapter = StdioACPAdapter(workspace=tmp_path)
    adapter._session_map = {"feishu:ou_test": "sess-history"}
    monkeypatch.setattr(adapter, "_estimate_session_history_tokens", lambda _sid: 4321)

    def _boom(*_args, **_kwargs):
        raise AssertionError("history extraction should not be used for /status")

    monkeypatch.setattr(adapter, "_extract_conversation_history", _boom)

    status = adapter.get_session_status("feishu", "ou_test")

    assert status["session_id"] == "sess-history"
    assert status["estimated_tokens"] == 4321
    assert status["compression_count"] == 0
