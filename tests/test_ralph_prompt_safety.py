from pathlib import Path

import pytest

from iflow_bot.bus.queue import MessageBus
from iflow_bot.engine.loop import AgentLoop


class _DummyAdapter:
    def __init__(self):
        self.workspace = Path('/tmp')
        self.mode = 'cli'
        self.timeout = 60
        self.session_mappings = type('SM', (), {'clear_session': lambda self, c, ch: True})()


@pytest.mark.asyncio
async def test_ralph_generation_prompts_forbid_tools(monkeypatch):
    loop = AgentLoop(bus=MessageBus(), adapter=_DummyAdapter(), model='glm-5', streaming=False)
    captured: list[tuple[str, str]] = []

    async def fake_call_model(message: str, run_dir: Path, system_prompt: str) -> str:
        captured.append((message, system_prompt))
        if 'questions' in message.lower():
            return '{"questions": [{"id": 1, "question": "Q?", "options": {"A": "A", "B": "B", "C": "C"}}]}'
        if 'Convert the PRD into prd.json' in message:
            return '{"project":"P","branchName":"ralph/p","userStories":[{"id":"US-001","title":"T","description":"D","acceptanceCriteria":["Typecheck passes"],"role":"researcher","priority":1,"passes":false,"notes":""}]}'
        return '# Title\n\n## User Stories\n- x'

    monkeypatch.setattr(loop, '_ralph_call_model', fake_call_model)

    await loop._ralph_generate_questions('test prompt', Path('/tmp/ralph'))
    await loop._ralph_generate_prd_md('test prompt', 'qa', Path('/tmp/ralph'))
    await loop._ralph_generate_prd_json('# PRD', Path('/tmp/ralph'))

    assert len(captured) == 3
    for message, system_prompt in captured:
        merged = f"{system_prompt}\n{message}".lower()
        assert 'do not use any tools' in merged
        assert 'do not inspect the filesystem' in merged
