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


def test_ralph_prd_preview_is_compact_and_structured():
    loop = AgentLoop(bus=MessageBus(), adapter=_DummyAdapter(), model='glm-5', streaming=False)
    loop._load_language_setting = lambda: 'en-US'
    prd = {
        'project': 'Cron vs Ralph Loop Comparison',
        'userStories': [
            {'id': 'US-001', 'title': 'Document Cron Architecture and Characteristics'},
            {'id': 'US-002', 'title': 'Document Ralph Loop Pattern Characteristics'},
            {'id': 'US-003', 'title': 'Compare State Management Capabilities'},
            {'id': 'US-004', 'title': 'Compare Error Handling and Recovery'},
        ],
    }

    preview = loop._ralph_build_prd_preview(prd)

    assert 'Project: Cron vs Ralph Loop Comparison' in preview
    assert 'Stories: 4' in preview
    assert 'US-001' in preview
    assert 'US-003' in preview
    assert 'US-004' not in preview
    assert len(preview) < 800


@pytest.mark.asyncio
async def test_ralph_generate_prd_json_falls_back_from_invalid_fenced_json_using_chinese_markdown():
    loop = AgentLoop(bus=MessageBus(), adapter=_DummyAdapter(), model='glm-5', streaming=False)
    loop._load_language_setting = lambda: 'zh-CN'

    async def _fake_call_model(message: str, run_dir: Path, system_prompt: str):
        return """```json
{
  "project": "Todo List Web Demo",
  "userStories": [
    {
      "id": "US-001",
      "title": "任务创建功能",
      "description": "作为用户，我希望能够创建新任务。",
      "acceptanceCriteria": [
        "首页提供"新增任务"表单"
      ],
      "role": "engineer"
    }
  ]
}
```"""

    loop._ralph_call_model = _fake_call_model  # type: ignore[method-assign]

    prd_md = """# Todo List Web Demo 产品需求文档 (PRD)

## 标题

极简 Todo List Web Demo - Python FastAPI 版本

## 简介

这是一个中文 PRD。

## 用户故事

### Story 0: 技术调研与方案确认

- **描述**: 作为研究员，我希望在开发前完成技术调研与方案确认。
- **角色**: researcher
- **验收标准**:
  - 输出技术调研报告
  - Typecheck passes

### Story 1: 任务创建功能

- **描述**: 作为用户，我希望能够创建新任务。
- **角色**: engineer
- **验收标准**:
  - 首页提供"新增任务"表单
  - Tests pass
"""

    prd, _raw = await loop._ralph_generate_prd_json(
        prd_md,
        Path('/tmp/ralph-run'),
        prompt='请严格使用中文输出',
        qa_block='',
    )

    assert prd is not None
    stories = prd.get('stories') or prd.get('userStories') or []
    assert len(stories) == 2
    assert stories[0]['role'] == 'researcher'
    assert stories[1]['title'] == '任务创建功能'
    assert 'Stories: 2' in loop._ralph_build_prd_preview(prd)
