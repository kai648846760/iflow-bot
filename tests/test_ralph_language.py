from pathlib import Path

import pytest

from iflow_bot.bus.events import InboundMessage
from iflow_bot.bus.queue import MessageBus
from iflow_bot.engine.loop import AgentLoop


class _FakeSessionMappings:
    def clear_session(self, channel: str, chat_id: str) -> bool:
        return True


class _FakeAdapter:
    def __init__(self, workspace: Path):
        self.workspace = workspace
        self.mode = "cli"
        self.session_mappings = _FakeSessionMappings()

    async def chat(self, message: str, channel: str, chat_id: str, model: str):
        return "ok"

    async def chat_stream(self, message: str, channel: str, chat_id: str, model: str, on_chunk):
        return "ok"


def _set_language(workspace: Path, lang: str) -> None:
    settings_dir = workspace / ".iflow"
    settings_dir.mkdir(parents=True, exist_ok=True)
    (settings_dir / "settings.json").write_text(
        f'{{"language": "{lang}"}}',
        encoding="utf-8",
    )


def test_ralph_default_questions_follow_en_us(tmp_path: Path):
    _set_language(tmp_path, "en-US")
    loop = AgentLoop(bus=MessageBus(), adapter=_FakeAdapter(tmp_path), model="glm-5", streaming=False)

    questions = loop._ralph_default_questions("build a report")

    assert questions == [
        {"question": "What is the goal and scope of this task?"},
        {"question": "What output or deliverable do you expect?"},
        {"question": "What constraints or prohibitions apply (for example, no code changes or no environment changes)?"},
    ]


@pytest.mark.asyncio
async def test_ralph_question_prompt_normalizes_generated_questions_to_session_language(tmp_path: Path):
    _set_language(tmp_path, "en-US")
    bus = MessageBus()
    loop = AgentLoop(bus=bus, adapter=_FakeAdapter(tmp_path), model="glm-5", streaming=False)

    async def _fake_generate_questions(prompt: str, run_dir: Path):
        return (
            [
                {"id": 1, "question": "这次任务的目标与范围是什么？"},
                {
                    "id": 2,
                    "question": "期望的输出形式是什么？",
                    "options": {"A": "报告", "B": "代码", "C": "其他"},
                },
            ],
            "",
        )

    loop._ralph_generate_questions = _fake_generate_questions  # type: ignore[method-assign]

    msg = InboundMessage(
        channel="feishu",
        sender_id="ou_test",
        chat_id="ou_test",
        content='/ralph "research something"',
        metadata={"message_id": "m1", "msg_type": "text"},
    )

    await loop._ralph_create(msg, 'research something')

    outs = []
    while bus.outbound_size:
        outs.append(await bus.consume_outbound())

    assert outs
    final = outs[-1].content
    assert "Please answer the following clarifying questions" in final
    assert "What is the goal and scope of this task?" in final
    assert "What output or deliverable do you expect?" in final
    assert "这次任务的目标与范围是什么" not in final
    assert "期望的输出形式是什么" not in final


@pytest.mark.asyncio
async def test_ralph_generate_prd_json_enforces_docs_only_constraints(tmp_path: Path):
    _set_language(tmp_path, "zh-CN")
    loop = AgentLoop(bus=MessageBus(), adapter=_FakeAdapter(tmp_path), model="glm-5", streaming=False)

    async def _fake_call_model(message: str, run_dir: Path, system_prompt: str):
        return """
        {
          "project": "TodoList Research",
          "branchName": "ralph/todolist-research",
          "stories": [
            {
              "id": "US-001",
              "title": "技术栈调研",
              "description": "输出技术选型结论",
              "acceptanceCriteria": ["输出技术选型文档", "给出推荐方案"],
              "role": "researcher",
              "priority": 1,
              "passes": false,
              "notes": ""
            },
            {
              "id": "US-002",
              "title": "项目结构设计",
              "description": "实现项目结构并完成类型检查",
              "acceptanceCriteria": ["提供完整目录结构图", "说明各目录/文件的职责", "Typecheck passes"],
              "role": "engineer",
              "priority": 2,
              "passes": false,
              "notes": ""
            }
          ]
        }
        """

    loop._ralph_call_model = _fake_call_model  # type: ignore[method-assign]

    prd, _raw = await loop._ralph_generate_prd_json(
        "# PRD",
        tmp_path / "workspace" / "ralph",
        prompt="请调研 Python 3 + uv 下 Todo List Web 应用的技术方案，只输出调研与架构文档，不写代码，不做实现任务。",
        qa_block="约束：只输出文档，不写代码，不改环境。",
    )

    assert prd is not None
    assert prd["stories"][0]["role"] == "researcher"
    assert prd["stories"][1]["role"] == "writer"
    assert "Typecheck passes" not in prd["stories"][1]["acceptanceCriteria"]
