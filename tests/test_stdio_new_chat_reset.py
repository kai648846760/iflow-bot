from pathlib import Path

from iflow_bot.engine.stdio_acp import StdioACPAdapter


def test_stdio_clear_session_clears_all_runtime_state(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    adapter = StdioACPAdapter(workspace=workspace)
    adapter._session_map_file = tmp_path / "session_mappings.json"

    key = "feishu:ou_test"
    session_id = "session-123"

    adapter._session_map[key] = session_id
    adapter._loaded_sessions.add(session_id)
    adapter._rehydrate_history[key] = "<history_context>old</history_context>"
    adapter._save_session_map()

    changed = adapter.clear_session("feishu", "ou_test")

    assert changed is True
    assert key not in adapter._session_map
    assert session_id not in adapter._loaded_sessions
    assert key not in adapter._rehydrate_history
    assert adapter._session_map_file.exists()
    assert adapter._session_map_file.read_text(encoding="utf-8").strip() == "{}"


def test_stdio_clear_session_returns_false_when_nothing_to_clear(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    adapter = StdioACPAdapter(workspace=workspace)
    adapter._session_map_file = tmp_path / "session_mappings.json"

    changed = adapter.clear_session("feishu", "ou_none")

    assert changed is False
