from pathlib import Path
import os
from types import SimpleNamespace

import pytest
import json
import asyncio
import time

from iflow_bot.bus.events import InboundMessage, OutboundMessage
from iflow_bot.bus.queue import MessageBus
from iflow_bot.engine.loop import AgentLoop
from iflow_bot.engine.stdio_acp import ACPResponse, StdioACPTimeoutError


class _FakeSessionMappings:
    def clear_session(self, channel: str, chat_id: str) -> bool:
        return True


class _FakeAdapter:
    def __init__(self, workspace: Path):
        self.workspace = workspace
        self.mode = "cli"
        self.timeout = 60
        self.session_mappings = _FakeSessionMappings()
        self.chat_calls = []
        self.chat_stream_calls = []

    async def chat(self, message: str, channel: str, chat_id: str, model: str):
        self.chat_calls.append(
            {
                "message": message,
                "channel": channel,
                "chat_id": chat_id,
                "model": model,
            }
        )
        return "ok"

    async def chat_stream(self, message: str, channel: str, chat_id: str, model: str, on_chunk):
        self.chat_stream_calls.append(
            {
                "message": message,
                "channel": channel,
                "chat_id": chat_id,
                "model": model,
            }
        )
        return "ok"


class _FakeRalphClient:
    def __init__(self, after_prompt=None, responses=None):
        self._after_prompt = after_prompt
        self._responses = list(responses or [])
        self.prompts = []
        self.create_session_calls = []
        self.cancel_calls = []

    async def create_session(self, **kwargs):
        self.create_session_calls.append(kwargs)
        return "ralph-session-1"

    async def prompt(self, session_id, message, timeout):
        self.prompts.append(message)
        if self._responses:
            item = self._responses.pop(0)
            callback = item.get("after_prompt")
            if callback is not None:
                asyncio.create_task(callback())
            exception = item.get("exception")
            if exception is not None:
                raise exception
            return item.get("response", ACPResponse(content="", error=None))
        if self._after_prompt is not None:
            asyncio.create_task(self._after_prompt())
        return ACPResponse(content="我来开始执行当前任务。", error="Tool shell failed")

    async def cancel(self, session_id):
        self.cancel_calls.append(session_id)
        return None


class _HangingRalphClient(_FakeRalphClient):
    async def prompt(self, session_id, message, timeout):
        self.prompts.append(message)
        if self._after_prompt is not None:
            asyncio.create_task(self._after_prompt())
        await asyncio.Future()


class _RecoveryHeartbeatClient(_FakeRalphClient):
    async def prompt(
        self,
        session_id,
        message,
        timeout,
        on_chunk=None,
        on_tool_call=None,
        on_event=None,
    ):
        self.prompts.append(message)
        for _ in range(4):
            await asyncio.sleep(0.03)
            if on_tool_call is not None:
                await on_tool_call()
        return ACPResponse(content="恢复完成", error=None)


class _BusyHeartbeatRalphClient(_FakeRalphClient):
    async def prompt(
        self,
        session_id,
        message,
        timeout,
        on_chunk=None,
        on_tool_call=None,
        on_event=None,
    ):
        self.prompts.append(message)
        try:
            while True:
                await asyncio.sleep(0.02)
                if on_event is not None:
                    await on_event()
        except asyncio.CancelledError:
            raise


class _FakeRalphStdio:
    def __init__(self, client):
        self._client = client

    async def connect(self):
        return None


class _FakeRalphAdapter(_FakeAdapter):
    def __init__(self, workspace: Path, stdio):
        super().__init__(workspace)
        self.mode = "stdio"
        self._stdio = stdio

    async def _get_stdio_adapter(self):
        return self._stdio


class _FakeChannel:
    def __init__(self):
        self.is_running = True
        self.messages = []

    async def send(self, msg):
        self.messages.append(msg)


class _FakeChannelManager:
    def __init__(self, channel):
        self._channel = channel

    def get_channel(self, name: str):
        if name == "feishu":
            return self._channel
        return None

    async def send_to(self, channel: str, msg):
        await self._channel.send(msg)


class _PendingTask:
    def done(self) -> bool:
        return False


def _set_language(workspace: Path, lang: str) -> None:
    settings_dir = workspace / ".iflow"
    settings_dir.mkdir(parents=True, exist_ok=True)
    (settings_dir / "settings.json").write_text(
        f'{{"language": "{lang}"}}',
        encoding="utf-8",
    )


def test_ralph_resolve_project_dir_uses_iflow_workspace_root(tmp_path: Path):
    loop = AgentLoop(bus=MessageBus(), adapter=_FakeAdapter(tmp_path / "workspace"), model="glm-5", streaming=False)

    resolved = loop._ralph_resolve_project_dir("project/todolist")

    assert resolved == tmp_path / "workspace" / "project" / "todolist"


def test_ralph_extract_project_dir_stops_at_chinese_punctuation(tmp_path: Path):
    loop = AgentLoop(bus=MessageBus(), adapter=_FakeAdapter(tmp_path / "workspace"), model="glm-5", streaming=False)

    extracted = loop._ralph_extract_project_dir(
        "请做一个待办事项 Web 应用。技术栈限定 Python 3 + uv。输出目录固定为 project/todolist。要求包含：新增、完成、删除。"
    )

    assert extracted == "project/todolist"


def test_ralph_extract_project_dir_from_create_prompt_with_absolute_path(tmp_path: Path):
    loop = AgentLoop(bus=MessageBus(), adapter=_FakeAdapter(tmp_path / "workspace"), model="glm-5", streaming=False)

    extracted = loop._ralph_extract_project_dir(
        "请在 /Users/LokiTina/.iflow-bot/workspace/project/ralph-e2e-todo-app-v3 创建一个最小可运行的 Todo List Web Demo。"
    )

    assert extracted == "/Users/LokiTina/.iflow-bot/workspace/project/ralph-e2e-todo-app-v3"


def test_ralph_extract_project_dir_from_output_prompt_with_absolute_path(tmp_path: Path):
    loop = AgentLoop(bus=MessageBus(), adapter=_FakeAdapter(tmp_path / "workspace"), model="glm-5", streaming=False)

    extracted = loop._ralph_extract_project_dir(
        "请在 /Users/LokiTina/.iflow-bot/workspace/project/ralph-feishu-local-report 输出一份 Markdown 内部设计报告。"
    )

    assert extracted == "/Users/LokiTina/.iflow-bot/workspace/project/ralph-feishu-local-report"


def test_new_conversation_message_uses_session_language_when_config_is_default_english(tmp_path: Path):
    workspace = tmp_path / "workspace"
    _set_language(workspace, "zh-CN")
    channel_manager = SimpleNamespace(
        config=SimpleNamespace(
            messages=SimpleNamespace(
                new_conversation="✨ New conversation started, previous context has been cleared."
            )
        )
    )
    loop = AgentLoop(
        bus=MessageBus(),
        adapter=_FakeAdapter(workspace),
        model="glm-5",
        streaming=False,
        channel_manager=channel_manager,
    )

    assert loop._get_new_conversation_message() == "✨ 已开启新会话，上一轮上下文已清空。"


def test_ralph_subagent_prompt_forbids_parent_workspace_scans(tmp_path: Path):
    loop = AgentLoop(bus=MessageBus(), adapter=_FakeAdapter(tmp_path / "workspace"), model="glm-5", streaming=False)
    run_dir = tmp_path / "workspace" / "ralph" / "chat" / "run-1"
    project_dir = tmp_path / "workspace" / "project" / "todolist"
    prompt = loop._ralph_build_subagent_prompt(
        run_dir=run_dir,
        project_dir=project_dir,
        story={"id": "US-001", "title": "Build app", "role": "engineer", "acceptanceCriteria": ["Typecheck passes"]},
        story_index=1,
        story_total=3,
        pass_index=1,
        passes=1,
        progress_before="# Ralph Progress\n",
    )

    assert str(run_dir) in prompt
    assert str(project_dir) in prompt
    assert "Do not read outside the allowed roots above." in prompt
    assert "Only read files inside these paths" in prompt
    assert "The runtime workspace may cover a broader ancestor directory" in prompt


def test_ralph_subagent_prompt_forbids_internet_when_task_requires_local_sources(tmp_path: Path):
    loop = AgentLoop(bus=MessageBus(), adapter=_FakeAdapter(tmp_path / "workspace"), model="glm-5", streaming=False)
    run_dir = tmp_path / "workspace" / "ralph" / "chat" / "run-1"
    project_dir = tmp_path / "workspace" / "project" / "docs"

    prompt = loop._ralph_build_subagent_prompt(
        run_dir=run_dir,
        project_dir=project_dir,
        story={"id": "US-001", "title": "本地调研", "role": "researcher", "acceptanceCriteria": ["输出 docs/report.md"]},
        story_index=1,
        story_total=2,
        pass_index=1,
        passes=1,
        progress_before="# Ralph Progress\n",
        task_prompt="只使用当前仓库与 workspace 中的本地代码/文档作为资料来源，不要访问互联网。",
    )

    assert "Use only local files under the allowed roots above as sources." in prompt
    assert "Do not access the internet" in prompt


def test_ralph_expected_artifact_paths_detects_standalone_readme_filename(tmp_path: Path):
    workspace = tmp_path / "workspace"
    loop = AgentLoop(bus=MessageBus(), adapter=_FakeAdapter(workspace), model="glm-5", streaming=False)
    project_dir = workspace / "project" / "ralph-docs-e2e"

    story = {
        "id": "US-005",
        "title": "项目概述与文档索引",
        "description": "作为 writer，我希望输出 README.md，以便跨职能团队了解项目全貌与文档导航。",
        "acceptanceCriteria": [
            "完成 README.md 初稿",
            "包含项目背景、目标、文档索引与快速导航",
            "文档结构清晰，语言简洁，适合跨职能团队阅读",
        ],
        "role": "writer",
    }

    paths = loop._ralph_expected_artifact_paths(story, project_dir)

    assert project_dir / "README.md" in paths
    assert project_dir / "docs" / "US-005-researcher-notes.md" not in paths


@pytest.mark.asyncio
async def test_ralph_resume_does_not_autofinalize_writer_story_from_unrelated_project_files(tmp_path: Path):
    workspace = tmp_path / "workspace"
    workspace.mkdir(parents=True, exist_ok=True)
    bus = MessageBus()
    channel = _FakeChannel()

    run_id = "run-writer-resume-1"
    chat_id = "ou_test"
    run_dir = workspace / "ralph" / chat_id / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    project_dir = workspace / "project" / "ralph-docs-e2e"
    docs_dir = project_dir / "docs"
    docs_dir.mkdir(parents=True, exist_ok=True)
    (docs_dir / "compare.md").write_text("# compare\n", encoding="utf-8")
    (docs_dir / "research-notes.md").write_text("# notes\n", encoding="utf-8")
    (docs_dir / "risks.md").write_text("# risks\n", encoding="utf-8")

    prd_path = run_dir / "prd.json"
    progress_path = run_dir / "progress.txt"
    progress_path.write_text("# Ralph Progress\nRESUMED\n", encoding="utf-8")
    prd_path.write_text(
        json.dumps(
            {
                "stories": [
                    {
                        "id": "US-005",
                        "title": "项目概述与文档索引",
                        "description": "作为 writer，我希望输出 README.md，以便跨职能团队了解项目全貌与文档导航。",
                        "role": "writer",
                        "acceptanceCriteria": [
                            "完成 README.md 初稿",
                            "包含项目背景、目标、文档索引与快速导航",
                        ],
                        "passes": False,
                    }
                ]
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )

    fake_client = _HangingRalphClient()
    adapter = _FakeRalphAdapter(workspace, _FakeRalphStdio(fake_client))
    loop = AgentLoop(
        bus=bus,
        adapter=adapter,
        model="glm-5",
        streaming=False,
        channel_manager=_FakeChannelManager(channel),
    )
    loop._ralph_prompt_poll_seconds = 0.02
    loop._ralph_artifact_watchdog_seconds = 0.05
    loop._ralph_story_settle_timeout_seconds = 0.05
    loop._ralph_idle_watchdog_seconds = lambda story, base_seconds: 0.1  # type: ignore[method-assign]
    loop._ralph_recovery_idle_watchdog_seconds_for_attempt = lambda story, base_seconds, latest_output="": 0.1  # type: ignore[method-assign]
    loop._get_ralph_stdio_adapter = lambda: asyncio.sleep(0, result=adapter._stdio)  # type: ignore[method-assign]
    loop._ralph_retry_incomplete_story = lambda **kwargs: asyncio.sleep(0, result=ACPResponse(content="恢复失败", error="Prompt timeout (idle)"))  # type: ignore[method-assign]

    async def _verification_failed(project_dir: Path, story: dict) -> bool:
        return False

    loop._ralph_verification_passed = _verification_failed  # type: ignore[method-assign]

    loop._ralph_set_current(chat_id, run_id)
    loop._ralph_save_state(
        run_dir,
        {
            "run_id": run_id,
            "status": "approved",
            "channel": "feishu",
            "story_index": 0,
            "pass_index": 0,
            "project_dir": str(project_dir),
            "current_started_at": time.time() - 10,
            "current_story_index": 1,
            "current_story_total": 1,
            "current_pass_index": 1,
            "current_pass_total": 1,
            "current_story_title": "项目概述与文档索引",
            "current_story_role": "writer",
        },
    )

    task = asyncio.create_task(loop._ralph_run_loop("feishu", chat_id, run_dir))
    await asyncio.sleep(0.3)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    state = json.loads((run_dir / "state.json").read_text(encoding="utf-8"))
    story = json.loads(prd_path.read_text(encoding="utf-8"))["stories"][0]

    assert story["passes"] is False
    assert state["status"] == "running"
    assert state["current_phase"] in {"executing", "recovery", "recovery_wait"}
    assert fake_client.prompts
    assert not (project_dir / "README.md").exists()


def test_ralph_researcher_prompt_requires_docs_only_outputs(tmp_path: Path):
    loop = AgentLoop(bus=MessageBus(), adapter=_FakeAdapter(tmp_path / "workspace"), model="glm-5", streaming=False)
    run_dir = tmp_path / "workspace" / "ralph" / "chat" / "run-1"
    project_dir = tmp_path / "workspace" / "project" / "todolist"

    prompt = loop._ralph_build_subagent_prompt(
        run_dir=run_dir,
        project_dir=project_dir,
        story={
            "id": "US-001",
            "title": "技术调研与架构设计",
            "role": "researcher",
            "acceptanceCriteria": ["输出技术选型文档"],
        },
        story_index=1,
        story_total=3,
        pass_index=1,
        passes=1,
        progress_before="# Ralph Progress\n",
    )

    assert "Do not start the next story" in prompt
    assert "For researcher stories, create documentation artifacts under" in prompt
    assert "do not initialize app code, virtualenvs, or runtime scaffolding" in prompt


def test_ralph_targeted_story_hints_include_mypy_file_and_fix_guidance(tmp_path: Path):
    workspace = tmp_path / "workspace"
    project_dir = workspace / "project" / "demo"
    app_dir = project_dir / "app"
    app_dir.mkdir(parents=True, exist_ok=True)
    (app_dir / "main.py").write_text("async def lifespan(app):\n    yield\n", encoding="utf-8")
    loop = AgentLoop(bus=MessageBus(), adapter=_FakeAdapter(workspace), model="glm-5", streaming=False)

    hints = loop._ralph_targeted_story_hints(
        story={"id": "US-002", "title": "项目初始化与数据库搭建", "role": "engineer"},
        project_dir=project_dir,
        latest_output="app/main.py:12: error: Function is missing a return type annotation  [no-untyped-def]",
    )

    assert "app/main.py:12" in hints
    assert "missing a return type annotation" in hints
    assert "explicit return type" in hints


def test_ralph_targeted_story_hints_cover_scaffold_dependency_gaps(tmp_path: Path):
    workspace = tmp_path / "workspace"
    project_dir = workspace / "project" / "demo"
    project_dir.mkdir(parents=True, exist_ok=True)
    (project_dir / "pyproject.toml").write_text(
        "[project]\nname='demo'\nversion='0.1.0'\ndependencies=[]\n",
        encoding="utf-8",
    )
    (project_dir / "app").mkdir(parents=True, exist_ok=True)
    loop = AgentLoop(bus=MessageBus(), adapter=_FakeAdapter(workspace), model="glm-5", streaming=False)

    hints = loop._ralph_targeted_story_hints(
        story={
            "id": "US-001",
            "title": "项目脚手架搭建",
            "role": "engineer",
            "acceptanceCriteria": [
                "使用 uv init 初始化项目",
                "添加 FastAPI、Jinja2 等核心依赖",
                "Typecheck passes",
            ],
        },
        project_dir=project_dir,
        latest_output="/Users/demo/.venv/bin/python: No module named pytest\n/Users/demo/.venv/bin/python: No module named mypy",
    )

    assert "pyproject.toml" in hints
    assert "uv sync --extra dev" in hints
    assert "pytest" in hints
    assert "mypy" in hints
    assert "fastapi" in hints.lower()
    assert "jinja2" in hints.lower()


def test_ralph_targeted_story_hints_add_httpx_fix_for_fastapi_testclient_error(tmp_path: Path):
    workspace = tmp_path / "workspace"
    project_dir = workspace / "project" / "demo"
    project_dir.mkdir(parents=True, exist_ok=True)
    (project_dir / "pyproject.toml").write_text(
        "[project]\nname='demo'\nversion='0.1.0'\ndependencies=['fastapi']\n"
        "[dependency-groups]\ndev=['pytest','mypy']\n",
        encoding="utf-8",
    )
    loop = AgentLoop(bus=MessageBus(), adapter=_FakeAdapter(workspace), model="glm-5", streaming=False)

    hints = loop._ralph_targeted_story_hints(
        story={
            "id": "US-003",
            "title": "REST API 接口实现",
            "role": "engineer",
            "acceptanceCriteria": [
                "实现 GET /api/tasks",
                "Tests pass",
                "Typecheck passes",
            ],
        },
        project_dir=project_dir,
        latest_output=(
            "RuntimeError: The starlette.testclient module requires the httpx package to be installed.\n"
            "E   ModuleNotFoundError: No module named httpx"
        ),
    )

    assert "httpx" in hints.lower()
    assert "uv add --dev httpx" in hints


def test_ralph_status_current_lines_include_phase_and_role(tmp_path: Path):
    workspace = tmp_path / "workspace"
    _set_language(workspace, "zh-CN")
    loop = AgentLoop(bus=MessageBus(), adapter=_FakeAdapter(workspace), model="glm-5", streaming=False)

    lines = loop._ralph_status_current_lines(
        {
            "current_story_index": 3,
            "current_story_total": 8,
            "current_pass_index": 1,
            "current_pass_total": 1,
            "current_story_title": "REST API 接口实现",
            "current_story_id": "US-003",
            "current_story_role": "engineer",
            "current_phase": "recovery",
            "current_recovery_round": 4,
            "current_started_at": time.time() - 5,
        },
        "running",
    )

    joined = "\n".join(lines)
    assert "当前: 第 3/8 个任务，第 1/1 轮" in joined
    assert "子角色: 工程" in joined
    assert "阶段: 恢复中（第 4 次）" in joined


@pytest.mark.asyncio
async def test_try_fast_path_routes_natural_language_progress_query_to_ralph_status(tmp_path: Path):
    workspace = tmp_path / "workspace"
    _set_language(workspace, "zh-CN")
    bus = MessageBus()
    channel = _FakeChannel()
    loop = AgentLoop(
        bus=bus,
        adapter=_FakeAdapter(workspace),
        model="glm-5",
        streaming=False,
        channel_manager=_FakeChannelManager(channel),
    )

    chat_id = "ou_test"
    run_id = "run-progress-query"
    run_dir = workspace / "ralph" / chat_id / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    loop._ralph_set_current(chat_id, run_id)
    loop._ralph_save_state(
        run_dir,
        {
            "run_id": run_id,
            "status": "running",
            "channel": "feishu",
            "current_story_index": 3,
            "current_story_total": 8,
            "current_pass_index": 1,
            "current_pass_total": 1,
            "current_story_title": "REST API 接口实现",
            "current_story_id": "US-003",
            "current_story_role": "engineer",
            "current_phase": "recovery",
            "current_recovery_round": 2,
            "current_started_at": time.time() - 3,
        },
    )

    handled = await loop._try_fast_path(
        InboundMessage(
            channel="feishu",
            sender_id=chat_id,
            chat_id=chat_id,
            content="你现在在做什么？",
        )
    )

    assert handled is True
    assert channel.messages
    reply = channel.messages[-1].content
    assert "Ralph 状态: 运行中" in reply
    assert "子角色: 工程" in reply
    assert "阶段: 恢复中（第 2 次）" in reply


def test_ralph_pick_role_overrides_researcher_when_criteria_require_implementation(tmp_path: Path):
    loop = AgentLoop(bus=MessageBus(), adapter=_FakeAdapter(tmp_path / "workspace"), model="glm-5", streaming=False)

    role = loop._ralph_pick_role(
        {
            "id": "US-002",
            "title": "Todo 数据模型与数据库设计",
            "role": "researcher",
            "acceptanceCriteria": [
                "定义 Todo 模型，包含字段：id、title、completed、created_at、updated_at、order",
                "创建数据库初始化脚本",
                "编写数据模型的单元测试",
            ],
        }
    )

    assert role == "engineer"


def test_ralph_pick_role_keeps_researcher_for_research_doc_with_stack_mentions(tmp_path: Path):
    loop = AgentLoop(bus=MessageBus(), adapter=_FakeAdapter(tmp_path / "workspace"), model="glm-5", streaming=False)

    role = loop._ralph_pick_role(
        {
            "id": "US-004",
            "title": "技术方案调研与文档撰写",
            "role": "researcher",
            "description": "作为研究员，我希望调研并记录技术方案的选型依据，以便团队理解架构决策。",
            "acceptanceCriteria": [
                "完成 FastAPI + Jinja2 + SQLite 架构的可行性分析文档",
                "文档包含各依赖的推荐版本和兼容性说明",
                "文档包含项目目录结构说明",
                "类型检查通过",
            ],
        }
    )

    assert role == "researcher"


def test_ralph_pick_role_keeps_writer_for_ui_copy_story(tmp_path: Path):
    loop = AgentLoop(bus=MessageBus(), adapter=_FakeAdapter(tmp_path / "workspace"), model="glm-5", streaming=False)

    role = loop._ralph_pick_role(
        {
            "id": "US-005",
            "title": "用户界面文案与提示信息编写",
            "role": "writer",
            "description": "作为文档作者，我希望编写页面标题、按钮文案和提示信息，以便用户获得清晰反馈。",
            "acceptanceCriteria": [
                "编写所有页面的标题、按钮文案和操作提示",
                "输出文案规范说明文档",
            ],
        }
    )

    assert role == "writer"


def test_ralph_expected_artifact_paths_skip_docs_fallback_for_impl_story_with_bad_role(tmp_path: Path):
    loop = AgentLoop(bus=MessageBus(), adapter=_FakeAdapter(tmp_path / "workspace"), model="glm-5", streaming=False)
    project_dir = tmp_path / "workspace" / "project" / "todolist"

    artifact_paths = loop._ralph_expected_artifact_paths(
        {
            "id": "US-002",
            "title": "Todo 数据模型与数据库设计",
            "role": "researcher",
            "acceptanceCriteria": [
                "定义 Todo 模型，包含字段：id、title、completed、created_at、updated_at、order",
                "创建数据库初始化脚本",
                "编写数据模型的单元测试",
            ],
        },
        project_dir,
    )

    assert project_dir / "docs" / "us-002-researcher-notes.md" not in artifact_paths


def test_ralph_recovery_prompt_adds_default_docs_target_for_researcher(tmp_path: Path):
    loop = AgentLoop(bus=MessageBus(), adapter=_FakeAdapter(tmp_path / "workspace"), model="glm-5", streaming=False)
    run_dir = tmp_path / "workspace" / "ralph" / "chat" / "run-1"
    project_dir = tmp_path / "workspace" / "project" / "todolist"

    prompt = loop._ralph_build_recovery_prompt(
        {
            "id": "US-001",
            "title": "技术栈选型调研",
            "role": "researcher",
            "acceptanceCriteria": ["输出技术选型对比表", "给出明确推荐方案"],
        },
        run_dir=run_dir,
        project_dir=project_dir,
    )

    assert str(project_dir / "docs" / "us-001-researcher-notes.md") in prompt
    assert "Create the documentation file even if the project directory is empty." in prompt


def test_ralph_recovery_prompt_adds_targeted_cli_fix_hints_from_import_error(tmp_path: Path):
    loop = AgentLoop(bus=MessageBus(), adapter=_FakeAdapter(tmp_path / "workspace"), model="glm-5", streaming=False)
    run_dir = tmp_path / "workspace" / "ralph" / "chat" / "run-1b"
    project_dir = tmp_path / "workspace" / "project" / "demo"
    (project_dir / "src" / "todo_cli").mkdir(parents=True, exist_ok=True)
    (project_dir / "tests").mkdir(parents=True, exist_ok=True)
    (project_dir / "src" / "todo_cli" / "cli.py").write_text("def main():\n    return 0\n", encoding="utf-8")
    (project_dir / "tests" / "test_cli_list.py").write_text("from todo_cli.cli import cmd_list\n", encoding="utf-8")

    prompt = loop._ralph_build_recovery_prompt(
        {
            "id": "US-004",
            "title": "list 命令实现",
            "description": "实现 todo list 命令",
            "role": "engineer",
            "acceptanceCriteria": [
                "执行 `todo list` 显示所有任务，格式清晰",
                "Typecheck passes",
                "Tests pass",
            ],
        },
        run_dir=run_dir,
        project_dir=project_dir,
        latest_output="$ .venv/bin/pytest -q\nImportError: cannot import name 'cmd_list' from 'todo_cli.cli'",
        failure_reason="Prompt idle watchdog exceeded (60s)",
    )

    assert "Start with these files:" in prompt
    assert "src/todo_cli/cli.py" in prompt
    assert "tests/test_cli_list.py" in prompt
    assert "Implement the missing symbol `cmd_list`" in prompt
    assert "Wire the `todo list` command through the CLI entrypoint" in prompt


def test_ralph_recovery_prompt_includes_run_paths_and_failure_context(tmp_path: Path):
    loop = AgentLoop(bus=MessageBus(), adapter=_FakeAdapter(tmp_path / "workspace"), model="glm-5", streaming=False)
    run_dir = tmp_path / "workspace" / "ralph" / "chat" / "run-2"
    project_dir = tmp_path / "workspace" / "project" / "todolist"
    project_dir.mkdir(parents=True, exist_ok=True)
    (project_dir / "app").mkdir(parents=True, exist_ok=True)
    (project_dir / "pyproject.toml").write_text(
        "[project]\nname='todolist'\nversion='0.1.0'\n",
        encoding="utf-8",
    )

    prompt = loop._ralph_build_recovery_prompt(
        {
            "id": "US-002",
            "title": "项目结构设计",
            "role": "engineer",
            "acceptanceCriteria": ["提供完整目录结构图", "说明各目录/文件的职责", "Typecheck passes"],
        },
        run_dir=run_dir,
        project_dir=project_dir,
        latest_output="uv run mypy app failed because editable build could not determine packaged files.",
        failure_reason="Previous verification failed: uv run mypy app",
    )

    assert str(run_dir / "prd.json") in prompt
    assert str(run_dir / "progress.txt") in prompt
    assert "Current story:" in prompt
    assert "项目结构设计" in prompt
    assert "Previous verification failed: uv run mypy app" in prompt
    assert "uv run mypy app failed because editable build could not determine packaged files." in prompt
    assert "uv run --no-project <tool>" in prompt
    assert "install or sync the required dependencies first" in prompt
    assert 'packages = ["app"]' in prompt


def test_ralph_subagent_prompt_includes_python_uv_verification_fallback(tmp_path: Path):
    loop = AgentLoop(bus=MessageBus(), adapter=_FakeAdapter(tmp_path / "workspace"), model="glm-5", streaming=False)
    run_dir = tmp_path / "workspace" / "ralph" / "chat" / "run-2b"
    project_dir = tmp_path / "workspace" / "project" / "todolist"

    prompt = loop._ralph_build_subagent_prompt(
        run_dir=run_dir,
        project_dir=project_dir,
        story={
            "id": "US-002",
            "title": "项目初始化与基础架构搭建",
            "role": "engineer",
            "acceptanceCriteria": ["数据库模型定义完成", "Typecheck passes"],
        },
        story_index=2,
        story_total=5,
        pass_index=1,
        passes=1,
        progress_before="# Ralph Progress\n",
    )

    assert "uv run --no-project <tool>" in prompt
    assert "install/sync the required dependencies first" in prompt
    assert "cannot determine which files to ship" in prompt


def test_ralph_subagent_prompt_adds_targeted_command_hints_when_matching_tests_exist(tmp_path: Path):
    loop = AgentLoop(bus=MessageBus(), adapter=_FakeAdapter(tmp_path / "workspace"), model="glm-5", streaming=False)
    run_dir = tmp_path / "workspace" / "ralph" / "chat" / "run-2c"
    project_dir = tmp_path / "workspace" / "project" / "demo"
    (project_dir / "src" / "todo_cli").mkdir(parents=True, exist_ok=True)
    (project_dir / "tests").mkdir(parents=True, exist_ok=True)
    (project_dir / "src" / "todo_cli" / "cli.py").write_text("def main():\n    return 0\n", encoding="utf-8")
    (project_dir / "tests" / "test_cli_list.py").write_text("def test_list():\n    assert True\n", encoding="utf-8")

    prompt = loop._ralph_build_subagent_prompt(
        run_dir=run_dir,
        project_dir=project_dir,
        story={
            "id": "US-004",
            "title": "list 命令实现",
            "description": "实现 todo list 命令",
            "role": "engineer",
            "acceptanceCriteria": ["执行 `todo list` 显示所有任务，格式清晰", "Tests pass"],
        },
        story_index=4,
        story_total=9,
        pass_index=1,
        passes=1,
        progress_before="# Ralph Progress\n",
    )

    assert "Start with these files:" in prompt
    assert "src/todo_cli/cli.py" in prompt
    assert "tests/test_cli_list.py" in prompt
    assert "Wire the `todo list` command through the CLI entrypoint" in prompt


def test_ralph_recovery_prompt_requires_real_project_changes_and_lists_current_tree(tmp_path: Path):
    loop = AgentLoop(bus=MessageBus(), adapter=_FakeAdapter(tmp_path / "workspace"), model="glm-5", streaming=False)
    run_dir = tmp_path / "workspace" / "ralph" / "chat" / "run-3"
    project_dir = tmp_path / "workspace" / "project" / "todolist"
    project_dir.mkdir(parents=True, exist_ok=True)
    (project_dir / "pyproject.toml").write_text("[project]\nname='demo'\n", encoding="utf-8")
    (project_dir / "README.md").write_text("# demo\n", encoding="utf-8")

    prompt = loop._ralph_build_recovery_prompt(
        {
            "id": "US-002",
            "title": "Todo 数据模型与数据库设计",
            "role": "engineer",
            "acceptanceCriteria": ["定义 Todo 模型", "创建数据库初始化脚本", "编写数据模型的单元测试"],
        },
        run_dir=run_dir,
        project_dir=project_dir,
        failure_reason="Prompt idle watchdog exceeded (60s)",
    )

    assert "At least one file inside the Project Directory must be created or updated" in prompt
    assert "pyproject.toml" in prompt
    assert "README.md" in prompt


def test_ralph_project_tree_summary_ignores_virtualenv_and_cache_dirs(tmp_path: Path):
    loop = AgentLoop(bus=MessageBus(), adapter=_FakeAdapter(tmp_path / "workspace"), model="glm-5", streaming=False)
    project_dir = tmp_path / "workspace" / "project" / "demo"
    (project_dir / ".venv" / "bin").mkdir(parents=True, exist_ok=True)
    (project_dir / ".mypy_cache").mkdir(parents=True, exist_ok=True)
    (project_dir / "__pycache__").mkdir(parents=True, exist_ok=True)
    (project_dir / "app").mkdir(parents=True, exist_ok=True)
    (project_dir / ".venv" / "bin" / "python").write_text("", encoding="utf-8")
    (project_dir / ".mypy_cache" / "cache.json").write_text("{}", encoding="utf-8")
    (project_dir / "__pycache__" / "x.pyc").write_text("", encoding="utf-8")
    (project_dir / "app" / "main.py").write_text("print('ok')\n", encoding="utf-8")

    summary = loop._ralph_project_tree_summary(project_dir)

    assert ".venv" not in summary
    assert ".mypy_cache" not in summary
    assert "__pycache__" not in summary
    assert "app/main.py" in summary


@pytest.mark.asyncio
async def test_ralph_collects_verification_evidence_from_existing_mypy_binary(tmp_path: Path):
    loop = AgentLoop(bus=MessageBus(), adapter=_FakeAdapter(tmp_path / "workspace"), model="glm-5", streaming=False)
    project_dir = tmp_path / "workspace" / "project" / "demo"
    mypy_bin = project_dir / ".venv" / "bin" / "mypy"
    mypy_bin.parent.mkdir(parents=True, exist_ok=True)
    mypy_bin.write_text("#!/bin/sh\necho 'app/main.py:1: error: missing import'\nexit 1\n", encoding="utf-8")
    mypy_bin.chmod(0o755)
    (project_dir / "app").mkdir(parents=True, exist_ok=True)
    (project_dir / "app" / "main.py").write_text("print('ok')\n", encoding="utf-8")

    evidence = await loop._ralph_collect_verification_evidence(
        project_dir,
        {"acceptanceCriteria": ["实现功能", "Typecheck passes"]},
    )

    assert str(mypy_bin) in evidence
    assert "missing import" in evidence


@pytest.mark.asyncio
async def test_ralph_verification_requires_pytest_when_story_demands_tests(tmp_path: Path):
    loop = AgentLoop(bus=MessageBus(), adapter=_FakeAdapter(tmp_path / "workspace"), model="glm-5", streaming=False)
    project_dir = tmp_path / "workspace" / "project" / "demo"
    venv_bin = project_dir / ".venv" / "bin"
    venv_bin.mkdir(parents=True, exist_ok=True)

    mypy_bin = venv_bin / "mypy"
    mypy_bin.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    mypy_bin.chmod(0o755)

    pytest_bin = venv_bin / "pytest"
    pytest_bin.write_text("#!/bin/sh\necho '1 failed'\nexit 1\n", encoding="utf-8")
    pytest_bin.chmod(0o755)

    (project_dir / "src").mkdir(parents=True, exist_ok=True)
    (project_dir / "src" / "main.py").write_text("print('ok')\n", encoding="utf-8")
    (project_dir / "tests").mkdir(parents=True, exist_ok=True)
    (project_dir / "tests" / "test_main.py").write_text("def test_ok():\n    assert True\n", encoding="utf-8")

    passed = await loop._ralph_verification_passed(
        project_dir,
        {"acceptanceCriteria": ["实现功能", "Typecheck passes", "Tests pass"]},
    )

    assert passed is False


@pytest.mark.asyncio
async def test_ralph_collect_verification_evidence_prefers_pytest_failure_over_mypy_success(tmp_path: Path):
    loop = AgentLoop(bus=MessageBus(), adapter=_FakeAdapter(tmp_path / "workspace"), model="glm-5", streaming=False)
    project_dir = tmp_path / "workspace" / "project" / "demo"
    venv_bin = project_dir / ".venv" / "bin"
    venv_bin.mkdir(parents=True, exist_ok=True)

    mypy_bin = venv_bin / "mypy"
    mypy_bin.write_text("#!/bin/sh\necho 'Success: no issues found in 1 source file'\nexit 0\n", encoding="utf-8")
    mypy_bin.chmod(0o755)

    pytest_bin = venv_bin / "pytest"
    pytest_bin.write_text(
        "#!/bin/sh\necho 'ImportError: cannot import name cmd_list from todo_cli.cli'\nexit 1\n",
        encoding="utf-8",
    )
    pytest_bin.chmod(0o755)

    (project_dir / "src").mkdir(parents=True, exist_ok=True)
    (project_dir / "src" / "main.py").write_text("print('ok')\n", encoding="utf-8")
    (project_dir / "tests").mkdir(parents=True, exist_ok=True)
    (project_dir / "tests" / "test_main.py").write_text("def test_ok():\n    assert True\n", encoding="utf-8")

    evidence = await loop._ralph_collect_verification_evidence(
        project_dir,
        {"acceptanceCriteria": ["实现功能", "Typecheck passes", "Tests pass"]},
    )

    assert str(pytest_bin) in evidence
    assert "cannot import name cmd_list" in evidence
    assert "Success: no issues found" not in evidence


def test_ralph_ensure_hatchling_wheel_packages_for_app_project(tmp_path: Path):
    loop = AgentLoop(bus=MessageBus(), adapter=_FakeAdapter(tmp_path / "workspace"), model="glm-5", streaming=False)
    project_dir = tmp_path / "workspace" / "project" / "demo"
    (project_dir / "app").mkdir(parents=True, exist_ok=True)
    pyproject_path = project_dir / "pyproject.toml"
    pyproject_path.write_text(
        "[project]\nname='demo'\nversion='0.1.0'\n\n[build-system]\nrequires=['hatchling']\nbuild-backend='hatchling.build'\n",
        encoding="utf-8",
    )

    changed = loop._ralph_ensure_hatchling_wheel_packages(project_dir)

    assert changed is True
    content = pyproject_path.read_text(encoding="utf-8")
    assert '[tool.hatch.build.targets.wheel]' in content
    assert 'packages = ["app"]' in content


@pytest.mark.asyncio
async def test_ralph_prepare_typecheck_environment_patches_hatchling_and_records_sync(monkeypatch, tmp_path: Path):
    loop = AgentLoop(bus=MessageBus(), adapter=_FakeAdapter(tmp_path / "workspace"), model="glm-5", streaming=False)
    project_dir = tmp_path / "workspace" / "project" / "demo"
    (project_dir / "app").mkdir(parents=True, exist_ok=True)
    (project_dir / "pyproject.toml").write_text(
        "[project]\nname='demo'\nversion='0.1.0'\n\n[build-system]\nrequires=['hatchling']\nbuild-backend='hatchling.build'\n",
        encoding="utf-8",
    )

    class _Proc:
        returncode = 0

        async def communicate(self):
            return (b"sync ok", None)

    async def _fake_create_subprocess_exec(*cmd, **kwargs):
        return _Proc()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", _fake_create_subprocess_exec)

    evidence = await loop._ralph_prepare_typecheck_environment(
        project_dir,
        {"acceptanceCriteria": ["实现功能", "Typecheck passes"]},
    )

    assert 'packages = ["app"]' in (project_dir / "pyproject.toml").read_text(encoding="utf-8")
    assert "Patched pyproject.toml" in evidence
    assert "$ uv sync --extra dev" in evidence
    assert "sync ok" in evidence


def test_ralph_syncs_misplaced_run_dir_outputs_into_project_dir(tmp_path: Path):
    loop = AgentLoop(bus=MessageBus(), adapter=_FakeAdapter(tmp_path / "workspace"), model="glm-5", streaming=False)
    run_dir = tmp_path / "workspace" / "ralph" / "chat" / "run-4"
    project_dir = tmp_path / "workspace" / "project" / "todolist"
    run_dir.mkdir(parents=True, exist_ok=True)
    project_dir.mkdir(parents=True, exist_ok=True)

    (run_dir / "app").mkdir()
    (run_dir / "app" / "models.py").write_text("class Todo: ...\n", encoding="utf-8")
    (run_dir / "tests").mkdir()
    (run_dir / "tests" / "test_models.py").write_text("def test_models(): ...\n", encoding="utf-8")
    (run_dir / "pyproject.toml").write_text("[project]\nname='demo'\n", encoding="utf-8")
    (run_dir / "prd.json").write_text("{}", encoding="utf-8")
    (run_dir / ".venv").mkdir()
    (run_dir / ".venv" / "ignored.txt").write_text("ignore\n", encoding="utf-8")

    synced = loop._ralph_sync_run_dir_outputs_to_project_dir(run_dir, project_dir)

    assert project_dir.joinpath("app", "models.py").exists()
    assert project_dir.joinpath("tests", "test_models.py").exists()
    assert project_dir.joinpath("pyproject.toml").exists()
    assert not project_dir.joinpath("prd.json").exists()
    assert not project_dir.joinpath(".venv", "ignored.txt").exists()
    assert str(project_dir / "app" / "models.py") in {str(path) for path in synced}


def test_ralph_changed_artifacts_ignores_stale_directory_files_until_new_write(tmp_path: Path):
    loop = AgentLoop(bus=MessageBus(), adapter=_FakeAdapter(tmp_path / "workspace"), model="glm-5", streaming=False)
    docs_dir = tmp_path / "workspace" / "project" / "demo" / "docs"
    docs_dir.mkdir(parents=True, exist_ok=True)
    stale = docs_dir / "old.md"
    stale.write_text("# old", encoding="utf-8")

    snapshot = loop._ralph_snapshot_artifacts([docs_dir])

    assert loop._ralph_changed_artifacts([docs_dir], snapshot) == []
    assert loop._ralph_changed_artifacts([docs_dir], snapshot, anchor_mtime=0) == []

    fresh = docs_dir / "new.md"
    fresh.write_text("# new", encoding="utf-8")

    changed = loop._ralph_changed_artifacts([docs_dir], snapshot)

    assert fresh in changed
    assert stale not in changed


def test_ralph_snapshot_and_change_detection_ignore_control_artifacts(tmp_path: Path):
    loop = AgentLoop(bus=MessageBus(), adapter=_FakeAdapter(tmp_path / "workspace"), model="glm-5", streaming=False)
    project_dir = tmp_path / "workspace" / "project" / "demo"
    (project_dir / "src").mkdir(parents=True, exist_ok=True)
    (project_dir / ".pytest_cache" / "v" / "cache").mkdir(parents=True, exist_ok=True)
    (project_dir / ".mypy_cache" / "3.12").mkdir(parents=True, exist_ok=True)
    (project_dir / ".venv" / "bin").mkdir(parents=True, exist_ok=True)
    (project_dir / ".git").mkdir(parents=True, exist_ok=True)
    (project_dir / "src" / "__pycache__").mkdir(parents=True, exist_ok=True)

    source_file = project_dir / "src" / "todo.py"
    cache_file = project_dir / ".pytest_cache" / "v" / "cache" / "nodeids"
    mypy_file = project_dir / ".mypy_cache" / "3.12" / "todo.meta.json"
    venv_file = project_dir / ".venv" / "bin" / "python"
    git_file = project_dir / ".git" / "HEAD"
    pyc_file = project_dir / "src" / "__pycache__" / "todo.cpython-312.pyc"

    source_file.write_text("print('ok')\n", encoding="utf-8")
    cache_file.write_text("[]\n", encoding="utf-8")
    mypy_file.write_text("{}\n", encoding="utf-8")
    venv_file.write_text("python\n", encoding="utf-8")
    git_file.write_text("ref: refs/heads/main\n", encoding="utf-8")
    pyc_file.write_bytes(b"pyc")

    snapshot = loop._ralph_snapshot_artifacts([project_dir])

    assert loop._ralph_changed_artifacts([project_dir], snapshot) == []

    time.sleep(0.01)
    cache_file.write_text("[\"tests/test_cli_list.py\"]\n", encoding="utf-8")
    mypy_file.write_text("{\"updated\": true}\n", encoding="utf-8")
    pyc_file.write_bytes(b"new-pyc")

    assert loop._ralph_changed_artifacts([project_dir], snapshot) == []

    time.sleep(0.01)
    source_file.write_text("print('updated')\n", encoding="utf-8")

    changed = loop._ralph_changed_artifacts([project_dir], snapshot)

    assert source_file in changed
    assert cache_file not in changed
    assert mypy_file not in changed
    assert venv_file not in changed
    assert git_file not in changed
    assert pyc_file not in changed


@pytest.mark.asyncio
async def test_ralph_plaintext_feedback_in_needs_approval_triggers_revision(tmp_path: Path, monkeypatch):
    workspace = tmp_path / "workspace"
    loop = AgentLoop(bus=MessageBus(), adapter=_FakeAdapter(workspace), model="glm-5", streaming=False)
    run_dir = workspace / "ralph" / "chat-1" / "run-1"
    run_dir.mkdir(parents=True, exist_ok=True)
    loop._ralph_set_current("chat-1", "run-1")
    loop._ralph_save_state(run_dir, {"run_id": "run-1", "status": "needs_approval"})

    captured = {"revision": None}

    async def _fake_revise(msg, text):
        captured["revision"] = text

    monkeypatch.setattr(loop, "_ralph_revise_prd", _fake_revise)

    handled = await loop._ralph_maybe_handle_followup(
        InboundMessage(channel="feishu", sender_id="u1", chat_id="chat-1", content="请补充 API 鉴权方案")
    )

    assert handled is True
    assert captured["revision"] == "请补充 API 鉴权方案"


@pytest.mark.asyncio
async def test_ralph_revision_includes_previous_prd_markdown_context(tmp_path: Path, monkeypatch):
    workspace = tmp_path / "workspace"
    loop = AgentLoop(bus=MessageBus(), adapter=_FakeAdapter(workspace), model="glm-5", streaming=False)
    run_dir = workspace / "ralph" / "chat-1" / "run-1"
    tasks_dir = run_dir / "tasks"
    tasks_dir.mkdir(parents=True, exist_ok=True)
    loop._ralph_set_current("chat-1", "run-1")
    loop._ralph_save_state(run_dir, {"run_id": "run-1", "status": "needs_approval", "prompt": "做一个 demo"})
    (run_dir / "answers.txt").write_text("1B 2A", encoding="utf-8")
    (run_dir / "questions.json").write_text(
        json.dumps([{"question": "范围?"}, {"question": "认证?"}], ensure_ascii=False),
        encoding="utf-8",
    )
    (tasks_dir / "prd-demo.md").write_text(
        "# 旧版 PRD\n\n## 用户故事\n\n### Story 1: 删除任务\n- 删除需要二次确认\n",
        encoding="utf-8",
    )

    captured: dict[str, str] = {}

    async def _fake_generate_prd_artifacts(**kwargs):
        captured["qa_block"] = kwargs["qa_block"]

    monkeypatch.setattr(loop, "_ralph_generate_prd_artifacts", _fake_generate_prd_artifacts)

    msg = InboundMessage(channel="feishu", sender_id="u1", chat_id="chat-1", content="请改成二次确认")
    await loop._ralph_revise_prd(msg, "请改成二次确认")

    assert "旧版 PRD" in captured["qa_block"]
    assert "删除需要二次确认" in captured["qa_block"]
    assert "请改成二次确认" in captured["qa_block"]


@pytest.mark.asyncio
async def test_ralph_plaintext_during_prd_generation_is_held(tmp_path: Path, monkeypatch):
    workspace = tmp_path / "workspace"
    loop = AgentLoop(bus=MessageBus(), adapter=_FakeAdapter(workspace), model="glm-5", streaming=False)
    run_dir = workspace / "ralph" / "chat-1" / "run-1"
    run_dir.mkdir(parents=True, exist_ok=True)
    loop._ralph_set_current("chat-1", "run-1")
    loop._ralph_save_state(run_dir, {"run_id": "run-1", "status": "generating_prd"})

    captured = {"reply": None}

    async def _fake_reply(msg, text, streaming=False):
        captured["reply"] = text

    monkeypatch.setattr(loop, "_send_command_reply", _fake_reply)

    handled = await loop._ralph_maybe_handle_followup(
        InboundMessage(channel="feishu", sender_id="u1", chat_id="chat-1", content="补充约束")
    )

    assert handled is True
    assert "PRD" in captured["reply"]


def test_ralph_story_completion_requires_explicit_prd_update(tmp_path: Path):
    loop = AgentLoop(bus=MessageBus(), adapter=_FakeAdapter(tmp_path / "workspace"), model="glm-5", streaming=False)

    assert loop._ralph_story_completed({"passes": True}) is True
    assert loop._ralph_story_completed({"passes": False}) is False
    assert loop._ralph_story_completed({"passes": 2, "completed_passes": 1}) is False
    assert loop._ralph_story_completed({"passes": 2, "completed_passes": 2}) is True


def test_ralph_normalize_story_criteria_drops_code_checks_for_researcher(tmp_path: Path):
    loop = AgentLoop(bus=MessageBus(), adapter=_FakeAdapter(tmp_path / "workspace"), model="glm-5", streaming=False)

    normalized = loop._ralph_normalize_story(
        {
            "id": "US-001",
            "title": "技术调研与架构设计",
            "description": "技术调研与架构设计",
            "acceptanceCriteria": ["输出技术选型文档", "Typecheck passes", "Tests pass"],
            "role": "researcher",
            "priority": 1,
            "passes": False,
            "notes": "",
        },
        1,
    )

    assert normalized["acceptanceCriteria"] == ["输出技术选型文档"]


@pytest.mark.asyncio
async def test_ralph_running_state_is_auto_resumed_on_startup(tmp_path: Path):
    workspace = tmp_path / "workspace"
    workspace.mkdir(parents=True, exist_ok=True)
    channel = _FakeChannel()
    loop = AgentLoop(
        bus=MessageBus(),
        adapter=_FakeAdapter(workspace),
        model="glm-5",
        streaming=False,
        channel_manager=_FakeChannelManager(channel),
    )

    chat_id = "ou_test"
    run_id = "run-1"
    run_dir = loop._ralph_run_dir(chat_id, run_id)
    run_dir.mkdir(parents=True, exist_ok=True)
    loop._ralph_set_current(chat_id, run_id)
    (run_dir / "prd.json").write_text('{"stories":[{"title":"Investigate","passes":1}]}', encoding="utf-8")

    state = {
        "run_id": run_id,
        "status": "running",
        "channel": "feishu",
        "current_story_index": 1,
        "current_story_total": 3,
        "current_pass_index": 1,
        "current_pass_total": 1,
        "current_story_title": "Investigate",
    }
    loop._ralph_save_state(run_dir, state)

    calls = []

    async def _fake_run_loop(channel_name: str, chat: str, path: Path):
        calls.append((channel_name, chat, path))
        loop._ralph_tasks.pop(chat, None)

    loop._ralph_run_loop = _fake_run_loop  # type: ignore[method-assign]

    await loop.start_background()
    await __import__("asyncio").sleep(0.05)
    loop.stop()

    assert calls == [("feishu", "ou_test", run_dir)]
    persisted = json.loads((run_dir / "state.json").read_text(encoding="utf-8"))
    assert persisted["status"] == "approved"


@pytest.mark.asyncio
async def test_language_command_fast_path_while_ralph_running(tmp_path: Path):
    workspace = tmp_path / "workspace"
    workspace.mkdir(parents=True, exist_ok=True)
    _set_language(workspace, "en-US")
    bus = MessageBus()
    loop = AgentLoop(bus=bus, adapter=_FakeAdapter(workspace), model="glm-5", streaming=False)

    pending = loop._ralph_tasks["ou_test"] = loop._loop.create_future() if hasattr(loop, "_loop") else None
    if pending is None:
        import asyncio
        pending = loop._ralph_tasks["ou_test"] = asyncio.get_running_loop().create_future()

    msg = InboundMessage(
        channel="feishu",
        sender_id="ou_test",
        chat_id="ou_test",
        content="/language zh-CN",
        metadata={"message_id": "m-language", "msg_type": "text"},
    )

    await loop._process_message(msg)

    out = await bus.consume_outbound()
    assert "zh-CN" in out.content
    assert loop._load_language_setting() == "zh-CN"

    pending.cancel()


@pytest.mark.asyncio
async def test_plain_chat_is_not_blocked_while_ralph_running(tmp_path: Path):
    workspace = tmp_path / "workspace"
    workspace.mkdir(parents=True, exist_ok=True)
    bus = MessageBus()
    adapter = _FakeAdapter(workspace)
    loop = AgentLoop(bus=bus, adapter=adapter, model="glm-5", streaming=False)

    import asyncio

    loop._ralph_tasks["ou_test"] = asyncio.get_running_loop().create_future()
    current_dir = loop._ralph_run_dir("ou_test", "run-1")
    current_dir.mkdir(parents=True, exist_ok=True)
    loop._ralph_set_current("ou_test", "run-1")
    loop._ralph_save_state(
        current_dir,
        {
            "status": "running",
            "current_story_index": 2,
            "current_story_total": 5,
            "current_pass_index": 1,
            "current_pass_total": 1,
            "current_story_title": "构建待办应用",
        },
    )

    msg = InboundMessage(
        channel="feishu",
        sender_id="ou_test",
        chat_id="ou_test",
        content="你在吗？",
        metadata={"message_id": "m-chat", "msg_type": "text"},
    )

    await loop._process_message(msg)

    out = await bus.consume_outbound()
    assert out.content == "ok"
    assert len(adapter.chat_calls) == 1
    assert "你在吗？" in adapter.chat_calls[0]["message"]


@pytest.mark.asyncio
async def test_plain_chat_keeps_streaming_path_while_ralph_running(tmp_path: Path):
    workspace = tmp_path / "workspace"
    workspace.mkdir(parents=True, exist_ok=True)
    bus = MessageBus()
    adapter = _FakeAdapter(workspace)
    loop = AgentLoop(bus=bus, adapter=adapter, model="glm-5", streaming=True)

    import asyncio

    loop._ralph_tasks["ou_test"] = asyncio.get_running_loop().create_future()
    current_dir = loop._ralph_run_dir("ou_test", "run-2")
    current_dir.mkdir(parents=True, exist_ok=True)
    loop._ralph_set_current("ou_test", "run-2")
    loop._ralph_save_state(
        current_dir,
        {
            "status": "running",
            "current_story_index": 1,
            "current_story_total": 3,
            "current_pass_index": 1,
            "current_pass_total": 1,
            "current_story_title": "构建待办应用",
        },
    )

    msg = InboundMessage(
        channel="feishu",
        sender_id="ou_test",
        chat_id="ou_test",
        content="主会话测试",
        metadata={"message_id": "m-chat-stream", "msg_type": "text"},
    )

    await loop._process_message(msg)

    out = await bus.consume_outbound()
    assert out.content == "ok"
    assert adapter.chat_calls == []
    assert len(adapter.chat_stream_calls) == 1


def test_ralph_status_text_is_localized_and_contains_elapsed(tmp_path: Path):
    workspace = tmp_path / "workspace"
    workspace.mkdir(parents=True, exist_ok=True)
    _set_language(workspace, "zh-CN")
    loop = AgentLoop(bus=MessageBus(), adapter=_FakeAdapter(workspace), model="glm-5", streaming=False)

    run_dir = loop._ralph_run_dir("ou_test", "run-1")
    run_dir.mkdir(parents=True, exist_ok=True)
    loop._ralph_set_current("ou_test", "run-1")
    loop._ralph_save_state(
        run_dir,
        {
            "status": "running",
            "current_story_index": 1,
            "current_story_total": 5,
            "current_pass_index": 1,
            "current_pass_total": 1,
            "current_story_id": "US-001",
            "current_story_title": "项目初始化与基础架构",
            "current_story_role": "engineer",
            "current_started_at": 1,
        },
    )
    loop._ralph_tasks["ou_test"] = _PendingTask()

    text = loop._ralph_current_status_text("ou_test")

    assert "Ralph 状态: 运行中" in text
    assert "当前: 第 1/5 个任务，第 1/1 轮" in text
    assert "项目初始化与基础架构" in text
    assert "[US-001]" in text
    assert "子角色: 工程" in text


def test_ralph_normalize_story_synthesizes_missing_acceptance_criteria_and_removes_external_skill_lines(tmp_path: Path):
    workspace = tmp_path / "workspace"
    workspace.mkdir(parents=True, exist_ok=True)
    loop = AgentLoop(bus=MessageBus(), adapter=_FakeAdapter(workspace), model="glm-5", streaming=False)

    researcher = loop._ralph_normalize_story(
        {
            "id": "US-001",
            "title": "技术调研与架构设计",
            "description": "输出调研文档",
            "acceptanceCriteria": [],
            "role": "researcher",
        },
        1,
    )
    engineer = loop._ralph_normalize_story(
        {
            "id": "US-002",
            "title": "REST API 实现",
            "description": "实现 CRUD 接口",
            "acceptanceCriteria": ["Verify in browser using dev-browser skill"],
            "role": "engineer",
        },
        2,
    )

    assert researcher["acceptanceCriteria"]
    assert "docs/architecture-research.md" in "\n".join(researcher["acceptanceCriteria"])
    assert engineer["acceptanceCriteria"]
    assert not any("dev-browser skill" in item.lower() for item in engineer["acceptanceCriteria"])
    assert any("Typecheck passes" == item for item in engineer["acceptanceCriteria"])


def test_ralph_prime_current_story_from_prd(tmp_path: Path):
    workspace = tmp_path / "workspace"
    workspace.mkdir(parents=True, exist_ok=True)
    loop = AgentLoop(bus=MessageBus(), adapter=_FakeAdapter(workspace), model="glm-5", streaming=False)

    run_dir = loop._ralph_run_dir("ou_test", "run-1")
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "prd.json").write_text(
        """
        {
          "stories": [
            {"id": "US-001", "title": "第一项", "passes": 1, "role": "researcher"},
            {"id": "US-002", "title": "第二项", "passes": 2, "role": "qa"}
          ]
        }
        """,
        encoding="utf-8",
    )

    state = {"status": "approved", "story_index": 1, "pass_index": 1}
    primed = loop._ralph_prime_current_story(run_dir, state)

    assert primed["current_story_index"] == 2
    assert primed["current_story_total"] == 2
    assert primed["current_pass_index"] == 2
    assert primed["current_pass_total"] == 2
    assert primed["current_story_id"] == "US-002"
    assert primed["current_story_title"] == "第二项"
    assert primed["current_story_role"] == "qa"
    assert primed["current_started_at"] > 0


def test_ralph_prime_current_story_preserves_started_at_for_same_story_pass(tmp_path: Path):
    workspace = tmp_path / "workspace"
    workspace.mkdir(parents=True, exist_ok=True)
    loop = AgentLoop(bus=MessageBus(), adapter=_FakeAdapter(workspace), model="glm-5", streaming=False)

    run_dir = loop._ralph_run_dir("ou_test", "run-1")
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "prd.json").write_text(
        """
        {
          "stories": [
            {"title": "第一项", "passes": 1},
            {"title": "第二项", "passes": 2}
          ]
        }
        """,
        encoding="utf-8",
    )

    started_at = time.time() - 120
    state = {
        "status": "approved",
        "story_index": 1,
        "pass_index": 1,
        "current_story_index": 2,
        "current_story_total": 2,
        "current_pass_index": 2,
        "current_pass_total": 2,
        "current_story_title": "第二项",
        "current_started_at": started_at,
    }

    primed = loop._ralph_prime_current_story(run_dir, state)

    assert primed["current_story_index"] == 2
    assert primed["current_pass_index"] == 2
    assert primed["current_started_at"] == started_at


def test_ralph_idle_watchdog_relaxes_for_researcher_and_writer(tmp_path: Path):
    workspace = tmp_path / "workspace"
    workspace.mkdir(parents=True, exist_ok=True)
    loop = AgentLoop(bus=MessageBus(), adapter=_FakeAdapter(workspace), model="glm-5", streaming=False)

    assert loop._ralph_idle_watchdog_seconds({"role": "engineer"}, base_seconds=60) == 60
    assert loop._ralph_idle_watchdog_seconds({"role": "qa"}, base_seconds=60) == 60
    assert loop._ralph_idle_watchdog_seconds({"role": "researcher"}, base_seconds=60) == 300
    assert loop._ralph_idle_watchdog_seconds({"role": "writer"}, base_seconds=60) == 300


def test_ralph_recovery_idle_watchdog_relaxes_for_engineer_with_verification_failures(tmp_path: Path):
    workspace = tmp_path / "workspace"
    workspace.mkdir(parents=True, exist_ok=True)
    loop = AgentLoop(bus=MessageBus(), adapter=_FakeAdapter(workspace), model="glm-5", streaming=False)

    relaxed = loop._ralph_recovery_idle_watchdog_seconds_for_attempt(
        {"role": "engineer"},
        base_seconds=60,
        latest_output=(
            "Supervisor verification evidence:\n"
            "$ .venv/bin/mypy app\n"
            "app/database.py:64: error: Incompatible return value type"
        ),
    )

    assert relaxed == 180


def test_ralph_should_ignore_ephemeral_artifacts(tmp_path: Path):
    workspace = tmp_path / "workspace"
    workspace.mkdir(parents=True, exist_ok=True)
    project_dir = workspace / "project" / "demo"
    loop = AgentLoop(bus=MessageBus(), adapter=_FakeAdapter(workspace), model="glm-5", streaming=False)

    assert loop._ralph_should_ignore_artifact(project_dir / ".venv" / "bin" / "pytest", project_dir) is True
    assert loop._ralph_should_ignore_artifact(project_dir / ".mypy_cache" / "x.data.json", project_dir) is True
    assert loop._ralph_should_ignore_artifact(project_dir / ".pytest_cache" / "README.md", project_dir) is True
    assert loop._ralph_should_ignore_artifact(project_dir / "app" / "__pycache__" / "main.pyc", project_dir) is True
    assert loop._ralph_should_ignore_artifact(project_dir / "app" / "main.py", project_dir) is False


@pytest.mark.asyncio
async def test_command_reply_can_bypass_bus_queue_via_direct_channel(tmp_path: Path):
    workspace = tmp_path / "workspace"
    workspace.mkdir(parents=True, exist_ok=True)
    bus = MessageBus(max_size=1)
    channel = _FakeChannel()
    loop = AgentLoop(
        bus=bus,
        adapter=_FakeAdapter(workspace),
        model="glm-5",
        streaming=False,
        channel_manager=_FakeChannelManager(channel),
    )

    await bus.publish_outbound(
        OutboundMessage(channel="feishu", chat_id="ou_test", content="queued")
    )
    msg = InboundMessage(
        channel="feishu",
        sender_id="ou_test",
        chat_id="ou_test",
        content="/help",
        metadata={"message_id": "m-help", "msg_type": "text"},
    )

    await loop._process_message(msg)

    assert channel.messages
    assert "可用命令" in channel.messages[-1].content
    assert bus.outbound_size == 1


@pytest.mark.asyncio
async def test_ralph_run_loop_waits_for_late_prd_and_progress_flush(tmp_path: Path):
    workspace = tmp_path / "workspace"
    workspace.mkdir(parents=True, exist_ok=True)
    bus = MessageBus()
    channel = _FakeChannel()

    run_id = "run-1"
    chat_id = "ou_test"
    run_dir = workspace / "ralph" / chat_id / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    project_dir = workspace / "project" / "demo"
    project_dir.mkdir(parents=True, exist_ok=True)

    prd_path = run_dir / "prd.json"
    progress_path = run_dir / "progress.txt"
    progress_path.write_text("# Ralph Progress\n", encoding="utf-8")
    prd_path.write_text(
        json.dumps(
            {
                "stories": [
                    {
                        "id": "US-001",
                        "title": "项目技术调研与架构设计",
                        "role": "researcher",
                        "passes": False,
                    }
                ]
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )

    async def _late_flush():
        await asyncio.sleep(2.2)
        project_docs = project_dir / "docs"
        project_docs.mkdir(parents=True, exist_ok=True)
        (project_docs / "tech-stack.md").write_text("# tech", encoding="utf-8")
        prd = json.loads(prd_path.read_text(encoding="utf-8"))
        prd["stories"][0]["passes"] = True
        prd_path.write_text(json.dumps(prd, ensure_ascii=False, indent=2), encoding="utf-8")
        with open(progress_path, "a", encoding="utf-8") as f:
            f.write("\n## 2026-03-15 00:00 - US-001\n- 完成调研\n---\n")

    adapter = _FakeRalphAdapter(workspace, _FakeRalphStdio(_FakeRalphClient(_late_flush)))
    loop = AgentLoop(
        bus=bus,
        adapter=adapter,
        model="glm-5",
        streaming=False,
        channel_manager=_FakeChannelManager(channel),
    )
    loop._get_ralph_stdio_adapter = lambda: asyncio.sleep(0, result=adapter._stdio)  # type: ignore[method-assign]

    loop._ralph_set_current(chat_id, run_id)
    loop._ralph_save_state(
        run_dir,
        {
            "run_id": run_id,
            "status": "approved",
            "channel": "feishu",
            "story_index": 0,
            "pass_index": 0,
            "project_dir": str(project_dir),
            "current_started_at": time.time() - 10,
            "current_story_index": 1,
            "current_story_total": 1,
            "current_pass_index": 1,
            "current_pass_total": 1,
            "current_story_title": "项目初始化与基础架构搭建",
        },
    )

    await loop._ralph_run_loop("feishu", chat_id, run_dir)

    state = json.loads((run_dir / "state.json").read_text(encoding="utf-8"))
    assert state["status"] != "stopped"
    assert json.loads(prd_path.read_text(encoding="utf-8"))["stories"][0]["passes"] is True
    sent = [m.content for m in channel.messages]
    assert not any("未真正完成" in content for content in sent)


@pytest.mark.asyncio
async def test_ralph_run_loop_uses_dedicated_stdio_adapter(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    workspace = tmp_path / "workspace"
    workspace.mkdir(parents=True, exist_ok=True)
    bus = MessageBus()
    channel = _FakeChannel()

    run_id = "run-dedicated"
    chat_id = "ou_test"
    run_dir = workspace / "ralph" / chat_id / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    project_dir = workspace / "project" / "demo"
    project_dir.mkdir(parents=True, exist_ok=True)

    prd_path = run_dir / "prd.json"
    progress_path = run_dir / "progress.txt"
    progress_path.write_text("# Ralph Progress\n", encoding="utf-8")
    prd_path.write_text(
        json.dumps(
            {
                "stories": [
                    {
                        "id": "US-001",
                        "title": "端到端流程技术调研",
                        "role": "researcher",
                        "passes": False,
                    }
                ]
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )

    async def _late_flush():
        await asyncio.sleep(0.1)
        docs_dir = project_dir / "docs"
        docs_dir.mkdir(parents=True, exist_ok=True)
        (docs_dir / "compare.md").write_text("# compare", encoding="utf-8")
        prd = json.loads(prd_path.read_text(encoding="utf-8"))
        prd["stories"][0]["passes"] = True
        prd_path.write_text(json.dumps(prd, ensure_ascii=False, indent=2), encoding="utf-8")
        with open(progress_path, "a", encoding="utf-8") as f:
            f.write("\n## 2026-03-15 00:00 - US-001\n- 完成调研\n---\n")

    main_stdio = _FakeRalphStdio(_FakeRalphClient())
    adapter = _FakeRalphAdapter(workspace, main_stdio)
    dedicated_client = _FakeRalphClient(_late_flush)
    dedicated_stdio = _FakeRalphStdio(dedicated_client)

    async def _unexpected_main_stdio():
        raise AssertionError("main stdio adapter should not be used by Ralph run loop")

    adapter._get_stdio_adapter = _unexpected_main_stdio  # type: ignore[method-assign]

    loop = AgentLoop(
        bus=bus,
        adapter=adapter,
        model="glm-5",
        streaming=False,
        channel_manager=_FakeChannelManager(channel),
    )

    async def _get_ralph_stdio():
        return dedicated_stdio

    monkeypatch.setattr(loop, "_get_ralph_stdio_adapter", _get_ralph_stdio, raising=False)

    loop._ralph_set_current(chat_id, run_id)
    loop._ralph_save_state(
        run_dir,
        {
            "run_id": run_id,
            "status": "approved",
            "channel": "feishu",
            "story_index": 0,
            "pass_index": 0,
            "project_dir": str(project_dir),
        },
    )

    await loop._ralph_run_loop("feishu", chat_id, run_dir)

    assert dedicated_client.create_session_calls
    state = json.loads((run_dir / "state.json").read_text(encoding="utf-8"))
    assert state["status"] == "done"


@pytest.mark.asyncio
async def test_ralph_stop_cancels_dedicated_stdio_session(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    workspace = tmp_path / "workspace"
    workspace.mkdir(parents=True, exist_ok=True)
    bus = MessageBus()

    main_client = _FakeRalphClient()
    main_stdio = _FakeRalphStdio(main_client)
    adapter = _FakeRalphAdapter(workspace, main_stdio)

    dedicated_client = _FakeRalphClient()
    dedicated_stdio = _FakeRalphStdio(dedicated_client)

    loop = AgentLoop(bus=bus, adapter=adapter, model="glm-5", streaming=False)

    async def _get_ralph_stdio():
        return dedicated_stdio

    monkeypatch.setattr(loop, "_get_ralph_stdio_adapter", _get_ralph_stdio, raising=False)

    chat_id = "ou_test"
    run_id = "run-stop"
    run_dir = workspace / "ralph" / chat_id / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    loop._ralph_set_current(chat_id, run_id)
    loop._ralph_save_state(run_dir, {"run_id": run_id, "status": "running"})
    loop._ralph_active_sessions[chat_id] = "ralph-session-1"

    msg = InboundMessage(
        channel="feishu",
        sender_id=chat_id,
        chat_id=chat_id,
        content="/ralph stop",
        metadata={"message_id": "m-stop", "msg_type": "text"},
    )

    await loop._ralph_stop(msg)

    assert dedicated_client.cancel_calls == ["ralph-session-1"]
    assert main_client.cancel_calls == []


@pytest.mark.asyncio
async def test_ralph_run_loop_retries_incomplete_noop_story_once(tmp_path: Path):
    workspace = tmp_path / "workspace"
    workspace.mkdir(parents=True, exist_ok=True)
    bus = MessageBus()
    channel = _FakeChannel()

    run_id = "run-1"
    chat_id = "ou_test"
    run_dir = workspace / "ralph" / chat_id / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    project_dir = workspace / "project" / "demo"
    project_dir.mkdir(parents=True, exist_ok=True)

    prd_path = run_dir / "prd.json"
    progress_path = run_dir / "progress.txt"
    progress_path.write_text("# Ralph Progress\n", encoding="utf-8")
    prd_path.write_text(
        json.dumps(
            {
                "stories": [
                    {
                        "id": "US-001",
                        "title": "技术调研与需求分析",
                        "role": "researcher",
                        "acceptanceCriteria": [
                            "输出 docs/research.md，包含技术选型理由",
                            "输出 docs/requirements.md，包含功能需求清单",
                        ],
                        "passes": False,
                    }
                ]
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )

    async def _complete_on_retry():
        await asyncio.sleep(0.05)
        docs = project_dir / "docs"
        docs.mkdir(parents=True, exist_ok=True)
        (docs / "research.md").write_text("# research", encoding="utf-8")
        (docs / "requirements.md").write_text("# requirements", encoding="utf-8")
        prd = json.loads(prd_path.read_text(encoding="utf-8"))
        prd["stories"][0]["passes"] = True
        prd_path.write_text(json.dumps(prd, ensure_ascii=False, indent=2), encoding="utf-8")
        with open(progress_path, "a", encoding="utf-8") as f:
            f.write("\n## 2026-03-15 00:00 - US-001\n- 完成调研文档\n---\n")

    fake_client = _FakeRalphClient(
        responses=[
            {"response": ACPResponse(content="我来执行这个调研任务。先读取上下文。", error="Tool shell failed")},
            {"response": ACPResponse(content="已补齐文档并完成当前 story。", error=None), "after_prompt": _complete_on_retry},
        ]
    )
    adapter = _FakeRalphAdapter(workspace, _FakeRalphStdio(fake_client))
    loop = AgentLoop(
        bus=bus,
        adapter=adapter,
        model="glm-5",
        streaming=False,
        channel_manager=_FakeChannelManager(channel),
    )
    loop._get_ralph_stdio_adapter = lambda: asyncio.sleep(0, result=adapter._stdio)  # type: ignore[method-assign]

    loop._ralph_set_current(chat_id, run_id)
    loop._ralph_save_state(
        run_dir,
        {
            "run_id": run_id,
            "status": "approved",
            "channel": "feishu",
            "story_index": 0,
            "pass_index": 0,
            "project_dir": str(project_dir),
            "current_started_at": time.time() - 10,
            "current_story_index": 1,
            "current_story_total": 1,
            "current_pass_index": 1,
            "current_pass_total": 1,
            "current_story_title": "项目初始化与基础架构搭建",
        },
    )

    await loop._ralph_run_loop("feishu", chat_id, run_dir)

    state = json.loads((run_dir / "state.json").read_text(encoding="utf-8"))
    assert state["status"] != "stopped"
    assert len(fake_client.prompts) == 2
    assert "Do not inspect or restate the task" in fake_client.prompts[1]
    assert (project_dir / "docs" / "research.md").exists()
    assert (project_dir / "docs" / "requirements.md").exists()


@pytest.mark.asyncio
async def test_ralph_run_loop_treats_completed_progress_entry_as_story_done_without_retry(tmp_path: Path):
    workspace = tmp_path / "workspace"
    workspace.mkdir(parents=True, exist_ok=True)
    bus = MessageBus()
    channel = _FakeChannel()

    run_id = "run-progress-complete"
    chat_id = "ou_test"
    run_dir = workspace / "ralph" / chat_id / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    project_dir = workspace / "project" / "demo"
    project_dir.mkdir(parents=True, exist_ok=True)

    prd_path = run_dir / "prd.json"
    progress_path = run_dir / "progress.txt"
    progress_path.write_text("# Ralph Progress\n", encoding="utf-8")
    prd_path.write_text(
        json.dumps(
            {
                "stories": [
                    {
                        "id": "US-001",
                        "title": "技术调研与方案确认",
                        "role": "researcher",
                        "acceptanceCriteria": [
                            "输出 docs/tech-research.md",
                        ],
                        "passes": False,
                    }
                ]
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )

    async def _write_completed_progress_only():
        await asyncio.sleep(0.05)
        docs = project_dir / "docs"
        docs.mkdir(parents=True, exist_ok=True)
        (docs / "tech-research.md").write_text("# tech research", encoding="utf-8")
        with open(progress_path, "a", encoding="utf-8") as f:
            f.write(
                "\n## [2026-03-15] US-001 完成\n\n"
                "**故事：** 技术调研与方案确认\n\n"
                "**完成内容：**\n"
                "- 创建技术调研报告 `docs/tech-research.md`\n\n"
                "**状态：** 已完成 (passes: true)\n"
            )

    fake_client = _FakeRalphClient(
        responses=[
            {
                "response": ACPResponse(content="我来先完成当前调研任务。", error="Tool shell failed"),
                "after_prompt": _write_completed_progress_only,
            }
        ]
    )
    adapter = _FakeRalphAdapter(workspace, _FakeRalphStdio(fake_client))
    loop = AgentLoop(
        bus=bus,
        adapter=adapter,
        model="glm-5",
        streaming=False,
        channel_manager=_FakeChannelManager(channel),
    )
    loop._get_ralph_stdio_adapter = lambda: asyncio.sleep(0, result=adapter._stdio)  # type: ignore[method-assign]

    loop._ralph_set_current(chat_id, run_id)
    loop._ralph_save_state(
        run_dir,
        {
            "run_id": run_id,
            "status": "approved",
            "channel": "feishu",
            "story_index": 0,
            "pass_index": 0,
            "project_dir": str(project_dir),
            "current_started_at": time.time() - 10,
            "current_story_index": 1,
            "current_story_total": 1,
            "current_pass_index": 1,
            "current_pass_total": 1,
            "current_story_title": "项目初始化与基础架构搭建",
        },
    )

    await loop._ralph_run_loop("feishu", chat_id, run_dir)

    state = json.loads((run_dir / "state.json").read_text(encoding="utf-8"))
    prd = json.loads(prd_path.read_text(encoding="utf-8"))

    assert len(fake_client.prompts) == 1
    assert state["status"] == "done"
    assert prd["stories"][0]["passes"] is True
    sent = [m.content for m in channel.messages]
    assert any("Story 1/1 Pass 1/1 完成" in content for content in sent)
    assert any("Ralph" in content and "完成" in content for content in sent)


@pytest.mark.asyncio
async def test_ralph_run_loop_retry_guides_researcher_to_default_docs_path(tmp_path: Path):
    workspace = tmp_path / "workspace"
    workspace.mkdir(parents=True, exist_ok=True)
    bus = MessageBus()
    channel = _FakeChannel()

    run_id = "run-2"
    chat_id = "ou_test"
    run_dir = workspace / "ralph" / chat_id / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    project_dir = workspace / "project" / "demo"
    project_dir.mkdir(parents=True, exist_ok=True)

    prd_path = run_dir / "prd.json"
    progress_path = run_dir / "progress.txt"
    progress_path.write_text("# Ralph Progress\n", encoding="utf-8")
    prd_path.write_text(
        json.dumps(
            {
                "stories": [
                    {
                        "id": "US-001",
                        "title": "技术栈选型调研",
                        "role": "researcher",
                        "acceptanceCriteria": [
                            "输出技术选型对比表（至少包含 2-3 个候选方案的优劣分析）",
                            "明确推荐方案并给出理由",
                        ],
                        "passes": False,
                    }
                ]
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )

    async def _complete_on_retry():
        await asyncio.sleep(0.05)
        docs = project_dir / "docs"
        docs.mkdir(parents=True, exist_ok=True)
        (docs / "us-001-researcher-notes.md").write_text("# tech stack research", encoding="utf-8")
        prd = json.loads(prd_path.read_text(encoding="utf-8"))
        prd["stories"][0]["passes"] = True
        prd_path.write_text(json.dumps(prd, ensure_ascii=False, indent=2), encoding="utf-8")
        with open(progress_path, "a", encoding="utf-8") as f:
            f.write("\n## 2026-03-15 00:00 - US-001\n- 完成技术调研文档\n---\n")

    fake_client = _FakeRalphClient(
        responses=[
            {"response": ACPResponse(content="我先看看目录和上下文。", error="Tool shell failed")},
            {"response": ACPResponse(content="已输出调研文档。", error=None), "after_prompt": _complete_on_retry},
        ]
    )
    adapter = _FakeRalphAdapter(workspace, _FakeRalphStdio(fake_client))
    loop = AgentLoop(
        bus=bus,
        adapter=adapter,
        model="glm-5",
        streaming=False,
        channel_manager=_FakeChannelManager(channel),
    )
    loop._get_ralph_stdio_adapter = lambda: asyncio.sleep(0, result=adapter._stdio)  # type: ignore[method-assign]

    loop._ralph_set_current(chat_id, run_id)
    loop._ralph_save_state(
        run_dir,
        {
            "run_id": run_id,
            "status": "approved",
            "channel": "feishu",
            "story_index": 0,
            "pass_index": 0,
            "project_dir": str(project_dir),
            "current_started_at": time.time() - 10,
        },
    )

    await loop._ralph_run_loop("feishu", chat_id, run_dir)

    state = json.loads((run_dir / "state.json").read_text(encoding="utf-8"))
    assert state["status"] != "stopped"
    assert len(fake_client.prompts) == 2
    assert str(project_dir / "docs" / "us-001-researcher-notes.md") in fake_client.prompts[1]
    assert (project_dir / "docs" / "us-001-researcher-notes.md").exists()


@pytest.mark.asyncio
async def test_ralph_run_loop_resume_skips_completed_story(tmp_path: Path):
    workspace = tmp_path / "workspace"
    workspace.mkdir(parents=True, exist_ok=True)
    bus = MessageBus()
    channel = _FakeChannel()

    run_id = "run-3"
    chat_id = "ou_test"
    run_dir = workspace / "ralph" / chat_id / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    project_dir = workspace / "project" / "demo"
    project_dir.mkdir(parents=True, exist_ok=True)

    prd_path = run_dir / "prd.json"
    progress_path = run_dir / "progress.txt"
    progress_path.write_text("# Ralph Progress\n", encoding="utf-8")
    prd_path.write_text(
        json.dumps(
            {
                "stories": [
                    {
                        "id": "US-001",
                        "title": "技术栈选型调研",
                        "role": "researcher",
                        "acceptanceCriteria": ["输出技术选型文档"],
                        "passes": True,
                    },
                    {
                        "id": "US-002",
                        "title": "项目结构设计",
                        "role": "engineer",
                        "acceptanceCriteria": ["输出目录树", "Typecheck passes"],
                        "passes": False,
                    },
                ]
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )

    async def _complete_story_two():
        await asyncio.sleep(0.05)
        (project_dir / "structure.md").write_text("# structure", encoding="utf-8")
        prd = json.loads(prd_path.read_text(encoding="utf-8"))
        prd["stories"][1]["passes"] = True
        prd_path.write_text(json.dumps(prd, ensure_ascii=False, indent=2), encoding="utf-8")
        with open(progress_path, "a", encoding="utf-8") as f:
            f.write("\n## 2026-03-15 00:00 - US-002\n- 完成项目结构设计\n---\n")

    fake_client = _FakeRalphClient(
        responses=[
            {"response": ACPResponse(content="开始项目结构设计。", error=None), "after_prompt": _complete_story_two},
        ]
    )
    adapter = _FakeRalphAdapter(workspace, _FakeRalphStdio(fake_client))
    loop = AgentLoop(
        bus=bus,
        adapter=adapter,
        model="glm-5",
        streaming=False,
        channel_manager=_FakeChannelManager(channel),
    )
    loop._get_ralph_stdio_adapter = lambda: asyncio.sleep(0, result=adapter._stdio)  # type: ignore[method-assign]

    loop._ralph_set_current(chat_id, run_id)
    loop._ralph_save_state(
        run_dir,
        {
            "run_id": run_id,
            "status": "approved",
            "channel": "feishu",
            "story_index": 0,
            "pass_index": 0,
            "project_dir": str(project_dir),
            "current_started_at": time.time() - 10,
        },
    )

    await loop._ralph_run_loop("feishu", chat_id, run_dir)

    assert len(fake_client.prompts) == 1
    assert "项目结构设计" in fake_client.prompts[0]
    assert "技术栈选型调研" not in fake_client.prompts[0]


@pytest.mark.asyncio
async def test_ralph_run_loop_empty_response_still_retries_once(tmp_path: Path):
    workspace = tmp_path / "workspace"
    workspace.mkdir(parents=True, exist_ok=True)
    bus = MessageBus()
    channel = _FakeChannel()

    run_id = "run-4"
    chat_id = "ou_test"
    run_dir = workspace / "ralph" / chat_id / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    project_dir = workspace / "project" / "demo"
    project_dir.mkdir(parents=True, exist_ok=True)

    prd_path = run_dir / "prd.json"
    progress_path = run_dir / "progress.txt"
    progress_path.write_text("# Ralph Progress\n", encoding="utf-8")
    prd_path.write_text(
        json.dumps(
            {
                "stories": [
                    {
                        "id": "US-001",
                        "title": "项目结构设计",
                        "role": "engineer",
                        "acceptanceCriteria": ["输出目录树", "Typecheck passes"],
                        "passes": False,
                    }
                ]
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )

    async def _complete_on_retry():
        await asyncio.sleep(0.05)
        (project_dir / "structure.md").write_text("# structure", encoding="utf-8")
        prd = json.loads(prd_path.read_text(encoding="utf-8"))
        prd["stories"][0]["passes"] = True
        prd_path.write_text(json.dumps(prd, ensure_ascii=False, indent=2), encoding="utf-8")
        with open(progress_path, "a", encoding="utf-8") as f:
            f.write("\n## 2026-03-15 00:00 - US-001\n- 完成项目结构设计\n---\n")

    fake_client = _FakeRalphClient(
        responses=[
            {"response": ACPResponse(content="", error="Tool shell failed")},
            {"response": ACPResponse(content="已补齐项目结构设计。", error=None), "after_prompt": _complete_on_retry},
        ]
    )
    adapter = _FakeRalphAdapter(workspace, _FakeRalphStdio(fake_client))
    loop = AgentLoop(
        bus=bus,
        adapter=adapter,
        model="glm-5",
        streaming=False,
        channel_manager=_FakeChannelManager(channel),
    )
    loop._get_ralph_stdio_adapter = lambda: asyncio.sleep(0, result=adapter._stdio)  # type: ignore[method-assign]

    loop._ralph_set_current(chat_id, run_id)
    loop._ralph_save_state(
        run_dir,
        {
            "run_id": run_id,
            "status": "approved",
            "channel": "feishu",
            "story_index": 0,
            "pass_index": 0,
            "project_dir": str(project_dir),
        },
    )

    await loop._ralph_run_loop("feishu", chat_id, run_dir)

    state = json.loads((run_dir / "state.json").read_text(encoding="utf-8"))
    assert state["status"] != "failed"
    assert len(fake_client.prompts) == 2


@pytest.mark.asyncio
async def test_ralph_run_loop_retries_when_prompt_raises_timeout(tmp_path: Path):
    workspace = tmp_path / "workspace"
    workspace.mkdir(parents=True, exist_ok=True)
    bus = MessageBus()
    channel = _FakeChannel()

    run_id = "run-timeout"
    chat_id = "ou_test"
    run_dir = workspace / "ralph" / chat_id / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    project_dir = workspace / "project" / "demo"
    project_dir.mkdir(parents=True, exist_ok=True)

    prd_path = run_dir / "prd.json"
    progress_path = run_dir / "progress.txt"
    progress_path.write_text("# Ralph Progress\n", encoding="utf-8")
    prd_path.write_text(
        json.dumps(
            {
                "stories": [
                    {
                        "id": "US-001",
                        "title": "项目结构设计",
                        "role": "engineer",
                        "acceptanceCriteria": ["输出目录树", "单元测试通过"],
                        "passes": False,
                    }
                ]
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )

    async def _complete_on_retry():
        await asyncio.sleep(0.05)
        (project_dir / "structure.md").write_text("# structure", encoding="utf-8")
        prd = json.loads(prd_path.read_text(encoding="utf-8"))
        prd["stories"][0]["passes"] = True
        prd_path.write_text(json.dumps(prd, ensure_ascii=False, indent=2), encoding="utf-8")
        with open(progress_path, "a", encoding="utf-8") as f:
            f.write("\n## 2026-03-15 00:00 - US-001\n- 完成项目结构设计\n---\n")

    fake_client = _FakeRalphClient(
        responses=[
            {"exception": StdioACPTimeoutError("Prompt timeout (idle)")},
            {"response": ACPResponse(content="已补齐项目结构设计。", error=None), "after_prompt": _complete_on_retry},
        ]
    )
    adapter = _FakeRalphAdapter(workspace, _FakeRalphStdio(fake_client))
    loop = AgentLoop(
        bus=bus,
        adapter=adapter,
        model="glm-5",
        streaming=False,
        channel_manager=_FakeChannelManager(channel),
    )
    loop._get_ralph_stdio_adapter = lambda: asyncio.sleep(0, result=adapter._stdio)  # type: ignore[method-assign]

    loop._ralph_set_current(chat_id, run_id)
    loop._ralph_save_state(
        run_dir,
        {
            "run_id": run_id,
            "status": "approved",
            "channel": "feishu",
            "story_index": 0,
            "pass_index": 0,
            "project_dir": str(project_dir),
        },
    )

    await loop._ralph_run_loop("feishu", chat_id, run_dir)

    state = json.loads((run_dir / "state.json").read_text(encoding="utf-8"))
    assert state["status"] == "done"
    assert len(fake_client.prompts) == 2


@pytest.mark.asyncio
async def test_ralph_run_loop_silently_recovers_when_recovery_prompt_times_out(tmp_path: Path):
    workspace = tmp_path / "workspace"
    workspace.mkdir(parents=True, exist_ok=True)
    bus = MessageBus()
    channel = _FakeChannel()

    run_id = "run-recovery-timeout"
    chat_id = "ou_test"
    run_dir = workspace / "ralph" / chat_id / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    project_dir = workspace / "project" / "demo"
    project_dir.mkdir(parents=True, exist_ok=True)

    prd_path = run_dir / "prd.json"
    progress_path = run_dir / "progress.txt"
    progress_path.write_text("# Ralph Progress\n", encoding="utf-8")
    prd_path.write_text(
        json.dumps(
            {
                "stories": [
                    {
                        "id": "US-001",
                        "title": "项目结构设计",
                        "role": "engineer",
                        "acceptanceCriteria": ["输出目录树", "单元测试通过"],
                        "passes": False,
                    }
                ]
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )

    async def _complete_after_second_recovery():
        await asyncio.sleep(0.05)
        (project_dir / "structure.md").write_text("# structure", encoding="utf-8")
        prd = json.loads(prd_path.read_text(encoding="utf-8"))
        prd["stories"][0]["passes"] = True
        prd_path.write_text(json.dumps(prd, ensure_ascii=False, indent=2), encoding="utf-8")
        with open(progress_path, "a", encoding="utf-8") as f:
            f.write("\n## 2026-03-15 00:00 - US-001\n- 完成项目结构设计\n---\n")

    fake_client = _FakeRalphClient(
        responses=[
            {"response": ACPResponse(content="我先检查当前项目。", error="Tool shell failed")},
            {"exception": StdioACPTimeoutError("Prompt timeout (idle)")},
            {"response": ACPResponse(content="已补齐项目结构设计。", error=None), "after_prompt": _complete_after_second_recovery},
        ]
    )
    adapter = _FakeRalphAdapter(workspace, _FakeRalphStdio(fake_client))
    loop = AgentLoop(
        bus=bus,
        adapter=adapter,
        model="glm-5",
        streaming=False,
        channel_manager=_FakeChannelManager(channel),
    )
    loop._get_ralph_stdio_adapter = lambda: asyncio.sleep(0, result=adapter._stdio)  # type: ignore[method-assign]

    loop._ralph_set_current(chat_id, run_id)
    loop._ralph_save_state(
        run_dir,
        {
            "run_id": run_id,
            "status": "approved",
            "channel": "feishu",
            "story_index": 0,
            "pass_index": 0,
            "project_dir": str(project_dir),
        },
    )

    await loop._ralph_run_loop("feishu", chat_id, run_dir)

    state = json.loads((run_dir / "state.json").read_text(encoding="utf-8"))
    contents = [getattr(msg, "content", "") for msg in channel.messages]

    assert state["status"] == "done"
    assert len(fake_client.prompts) == 3


@pytest.mark.asyncio
async def test_ralph_run_loop_retries_when_active_prompt_never_goes_idle(tmp_path: Path):
    workspace = tmp_path / "workspace"
    workspace.mkdir(parents=True, exist_ok=True)
    bus = MessageBus()
    channel = _FakeChannel()

    run_id = "run-execution-watchdog"
    chat_id = "ou_test"
    run_dir = workspace / "ralph" / chat_id / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    project_dir = workspace / "project" / "demo"
    project_dir.mkdir(parents=True, exist_ok=True)

    prd_path = run_dir / "prd.json"
    progress_path = run_dir / "progress.txt"
    progress_path.write_text("# Ralph Progress\n", encoding="utf-8")
    prd_path.write_text(
        json.dumps(
            {
                "stories": [
                    {
                        "id": "US-001",
                        "title": "查看任务列表",
                        "role": "engineer",
                        "acceptanceCriteria": ["访问首页显示所有任务", "Typecheck passes", "Tests pass"],
                        "passes": False,
                    }
                ]
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )

    fake_client = _BusyHeartbeatRalphClient()
    adapter = _FakeRalphAdapter(workspace, _FakeRalphStdio(fake_client))
    loop = AgentLoop(
        bus=bus,
        adapter=adapter,
        model="glm-5",
        streaming=False,
        channel_manager=_FakeChannelManager(channel),
    )
    loop._get_ralph_stdio_adapter = lambda: asyncio.sleep(0, result=adapter._stdio)  # type: ignore[method-assign]
    loop._ralph_prompt_poll_seconds = 0.01
    loop._ralph_story_idle_watchdog_seconds = 999
    loop._ralph_story_execution_watchdog_seconds = 0.06
    loop._ralph_story_settle_timeout_seconds = 0.02

    recovery_calls = {"count": 0}

    async def _fake_retry_incomplete_story(**kwargs):
        recovery_calls["count"] += 1
        (project_dir / "app.py").write_text("print('ok')\n", encoding="utf-8")
        prd = json.loads(prd_path.read_text(encoding="utf-8"))
        prd["stories"][0]["passes"] = True
        prd_path.write_text(json.dumps(prd, ensure_ascii=False, indent=2), encoding="utf-8")
        with open(progress_path, "a", encoding="utf-8") as f:
            f.write("\n## 2026-03-15 00:00 - US-001\n- 完成查看任务列表\n---\n")
        return ACPResponse(content="已完成查看任务列表。", error=None)

    loop._ralph_set_current(chat_id, run_id)
    loop._ralph_save_state(
        run_dir,
        {
            "run_id": run_id,
            "status": "approved",
            "channel": "feishu",
            "story_index": 0,
            "pass_index": 0,
            "project_dir": str(project_dir),
        },
    )

    loop._ralph_retry_incomplete_story = _fake_retry_incomplete_story  # type: ignore[method-assign]

    await asyncio.wait_for(loop._ralph_run_loop("feishu", chat_id, run_dir), timeout=2)

    state = json.loads((run_dir / "state.json").read_text(encoding="utf-8"))
    assert state["status"] == "done"
    assert recovery_calls["count"] == 1
    assert fake_client.cancel_calls == ["ralph-session-1"]


@pytest.mark.asyncio
async def test_ralph_run_loop_keeps_recovering_after_three_rounds_until_story_completes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    workspace = tmp_path / "workspace"
    workspace.mkdir(parents=True, exist_ok=True)
    bus = MessageBus()
    channel = _FakeChannel()

    run_id = "run-recovery-beyond-three"
    chat_id = "ou_test"
    run_dir = workspace / "ralph" / chat_id / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    project_dir = workspace / "project" / "demo"
    project_dir.mkdir(parents=True, exist_ok=True)

    prd_path = run_dir / "prd.json"
    progress_path = run_dir / "progress.txt"
    progress_path.write_text("# Ralph Progress\n", encoding="utf-8")
    prd_path.write_text(
        json.dumps(
            {
                "stories": [
                    {
                        "id": "US-001",
                        "title": "项目结构设计",
                        "role": "engineer",
                        "acceptanceCriteria": ["输出 structure.md"],
                        "passes": False,
                    }
                ]
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )

    fake_client = _FakeRalphClient(
        responses=[
            {"response": ACPResponse(content="先检查项目目录。", error="Tool shell failed")},
        ]
    )
    adapter = _FakeRalphAdapter(workspace, _FakeRalphStdio(fake_client))
    loop = AgentLoop(
        bus=bus,
        adapter=adapter,
        model="glm-5",
        streaming=False,
        channel_manager=_FakeChannelManager(channel),
    )
    loop._ralph_prompt_poll_seconds = 0.02
    loop._ralph_story_settle_timeout_seconds = 0.05
    loop._ralph_recovery_backoff_seconds = [0, 0, 0]
    loop._get_ralph_stdio_adapter = lambda: asyncio.sleep(0, result=adapter._stdio)  # type: ignore[method-assign]

    recovery_calls = {"count": 0}

    async def _fake_retry_incomplete_story(**kwargs):
        recovery_calls["count"] += 1
        if recovery_calls["count"] < 4:
            return ACPResponse(content="", error="Prompt timeout (idle)")
        (project_dir / "structure.md").write_text("# structure\n", encoding="utf-8")
        prd = json.loads(prd_path.read_text(encoding="utf-8"))
        prd["stories"][0]["passes"] = True
        prd_path.write_text(json.dumps(prd, ensure_ascii=False, indent=2), encoding="utf-8")
        with open(progress_path, "a", encoding="utf-8") as f:
            f.write("\n## 2026-03-15 00:00 - US-001\n- 完成项目结构设计\n---\n")
        return ACPResponse(content="已补齐项目结构设计。", error=None)

    monkeypatch.setattr(loop, "_ralph_retry_incomplete_story", _fake_retry_incomplete_story)

    loop._ralph_set_current(chat_id, run_id)
    loop._ralph_save_state(
        run_dir,
        {
            "run_id": run_id,
            "status": "approved",
            "channel": "feishu",
            "story_index": 0,
            "pass_index": 0,
            "project_dir": str(project_dir),
        },
    )

    await asyncio.wait_for(loop._ralph_run_loop("feishu", chat_id, run_dir), timeout=2)

    state = json.loads((run_dir / "state.json").read_text(encoding="utf-8"))
    contents = [getattr(msg, "content", "") for msg in channel.messages]

    assert recovery_calls["count"] == 4
    assert state["status"] == "done"
    assert not any("已暂停" in content or "paused" in content.lower() for content in contents)
    assert not any("内部错误" in content or "paused" in content.lower() for content in contents)


@pytest.mark.asyncio
async def test_ralph_run_loop_keeps_recovering_until_story_is_completed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    workspace = tmp_path / "workspace"
    workspace.mkdir(parents=True, exist_ok=True)
    bus = MessageBus()
    channel = _FakeChannel()

    run_id = "run-multi-recovery"
    chat_id = "ou_test"
    run_dir = workspace / "ralph" / chat_id / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    project_dir = workspace / "project" / "demo"
    project_dir.mkdir(parents=True, exist_ok=True)

    prd_path = run_dir / "prd.json"
    progress_path = run_dir / "progress.txt"
    progress_path.write_text("# Ralph Progress\n", encoding="utf-8")
    prd_path.write_text(
        json.dumps(
            {
                "stories": [
                    {
                        "id": "US-001",
                        "title": "实现 list 命令",
                        "role": "engineer",
                        "acceptanceCriteria": ["实现 list 命令", "Tests pass"],
                        "passes": False,
                    }
                ]
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )

    async def _complete_after_second_recovery():
        await asyncio.sleep(0.05)
        (project_dir / "cli.py").write_text("def cmd_list():\n    return 0\n", encoding="utf-8")
        prd = json.loads(prd_path.read_text(encoding="utf-8"))
        prd["stories"][0]["passes"] = True
        prd_path.write_text(json.dumps(prd, ensure_ascii=False, indent=2), encoding="utf-8")
        with open(progress_path, "a", encoding="utf-8") as f:
            f.write("\n## 2026-03-15 00:00 - US-001\n- 已补齐 list 命令实现\n---\n")

    fake_client = _FakeRalphClient(
        responses=[
            {"response": ACPResponse(content="我先补测试。", error="Tool shell failed")},
            {"response": ACPResponse(content="已补了测试，继续实现。", error=None)},
            {"response": ACPResponse(content="已补齐 list 命令。", error=None), "after_prompt": _complete_after_second_recovery},
        ]
    )
    adapter = _FakeRalphAdapter(workspace, _FakeRalphStdio(fake_client))
    loop = AgentLoop(
        bus=bus,
        adapter=adapter,
        model="glm-5",
        streaming=False,
        channel_manager=_FakeChannelManager(channel),
    )
    loop._ralph_story_settle_timeout_seconds = 0.05
    monkeypatch.setattr(AgentLoop, "_ralph_progress_marks_story_complete", lambda self, progress, story: False)
    monkeypatch.setattr(AgentLoop, "_ralph_can_supervisor_autofinalize", lambda self, story, artifacts, verification: False)

    loop._ralph_set_current(chat_id, run_id)
    loop._ralph_save_state(
        run_dir,
        {
            "run_id": run_id,
            "status": "approved",
            "channel": "feishu",
            "story_index": 0,
            "pass_index": 0,
            "project_dir": str(project_dir),
        },
    )

    await loop._ralph_run_loop("feishu", chat_id, run_dir)

    state = json.loads((run_dir / "state.json").read_text(encoding="utf-8"))
    contents = [getattr(msg, "content", "") for msg in channel.messages]

    assert state["status"] == "done"
    assert len(fake_client.prompts) == 3
    assert not any("暂停" in content or "paused" in content.lower() for content in contents)


@pytest.mark.asyncio
async def test_ralph_retry_incomplete_story_uses_short_idle_watchdog(tmp_path: Path):
    workspace = tmp_path / "workspace"
    workspace.mkdir(parents=True, exist_ok=True)
    fake_client = _HangingRalphClient()
    stdio = _FakeRalphStdio(fake_client)
    adapter = _FakeRalphAdapter(workspace, stdio)
    loop = AgentLoop(bus=MessageBus(), adapter=adapter, model="glm-5", streaming=False)
    loop._ralph_recovery_idle_watchdog_seconds = 0.05

    run_dir = workspace / "ralph" / "chat" / "run-idle-recovery"
    project_dir = workspace / "project" / "demo"
    run_dir.mkdir(parents=True, exist_ok=True)
    project_dir.mkdir(parents=True, exist_ok=True)

    start = time.perf_counter()
    response = await loop._ralph_retry_incomplete_story(
        stdio=stdio,
        run_dir=run_dir,
        project_dir=project_dir,
        story={"id": "US-001", "title": "实现 list 命令", "role": "engineer", "acceptanceCriteria": ["Tests pass"]},
        chat_id="ou_test",
        latest_output="",
        failure_reason="",
    )
    elapsed = time.perf_counter() - start

    assert "Prompt timeout" in str(response.error)
    assert elapsed < 0.5
    assert len(fake_client.prompts) == 3


@pytest.mark.asyncio
async def test_ralph_retry_incomplete_story_treats_prompt_callbacks_as_activity(tmp_path: Path):
    workspace = tmp_path / "workspace"
    workspace.mkdir(parents=True, exist_ok=True)
    fake_client = _RecoveryHeartbeatClient()
    stdio = _FakeRalphStdio(fake_client)
    adapter = _FakeRalphAdapter(workspace, stdio)
    loop = AgentLoop(bus=MessageBus(), adapter=adapter, model="glm-5", streaming=False)
    loop._ralph_recovery_idle_watchdog_seconds = 0.05
    loop._ralph_prompt_poll_seconds = 0.01

    run_dir = workspace / "ralph" / "chat" / "run-recovery-heartbeat"
    project_dir = workspace / "project" / "demo"
    run_dir.mkdir(parents=True, exist_ok=True)
    project_dir.mkdir(parents=True, exist_ok=True)

    response = await loop._ralph_retry_incomplete_story(
        stdio=stdio,
        run_dir=run_dir,
        project_dir=project_dir,
        story={"id": "US-001", "title": "实现 list 命令", "role": "engineer", "acceptanceCriteria": ["Tests pass"]},
        chat_id="ou_test",
        latest_output="",
        failure_reason="",
    )

    assert response.error is None
    assert response.content == "恢复完成"
    assert len(fake_client.prompts) == 1


@pytest.mark.asyncio
async def test_ralph_recovery_hanging_engineer_prompt_auto_finalizes_once_fix_is_written(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    workspace = tmp_path / "workspace"
    workspace.mkdir(parents=True, exist_ok=True)
    bus = MessageBus()
    channel = _FakeChannel()

    run_id = "run-recovery-autofinalize"
    chat_id = "ou_test"
    run_dir = workspace / "ralph" / chat_id / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    project_dir = workspace / "project" / "demo"
    project_dir.mkdir(parents=True, exist_ok=True)
    app_dir = project_dir / "app"
    app_dir.mkdir(parents=True, exist_ok=True)

    prd_path = run_dir / "prd.json"
    progress_path = run_dir / "progress.txt"
    progress_path.write_text("# Ralph Progress\n", encoding="utf-8")
    prd_path.write_text(
        json.dumps(
            {
                "stories": [
                    {
                        "id": "US-001",
                        "title": "项目初始化与数据库设计",
                        "role": "engineer",
                        "acceptanceCriteria": ["创建 SQLite 数据库模型", "Typecheck passes"],
                        "passes": False,
                    }
                ]
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )

    async def _write_broken_files():
        await asyncio.sleep(0.02)
        (app_dir / "database.py").write_text(
            "from sqlalchemy.orm import sessionmaker\n\n"
            "SessionLocal = sessionmaker()\n\n"
            "def get_db() -> sessionmaker[SessionLocal]:\n"
            "    yield SessionLocal()\n",
            encoding="utf-8",
        )
        (app_dir / "main.py").write_text(
            "from contextlib import asynccontextmanager\n"
            "from fastapi import FastAPI\n\n"
            "@asynccontextmanager\n"
            "async def lifespan(app: FastAPI):\n"
            "    yield\n",
            encoding="utf-8",
        )

    async def _write_fixed_files():
        await asyncio.sleep(0.02)
        (app_dir / "database.py").write_text(
            "from collections.abc import Generator\n\n"
            "from sqlalchemy.orm import Session, sessionmaker\n\n"
            "SessionLocal = sessionmaker()\n\n"
            "def get_db() -> Generator[Session, None, None]:\n"
            "    yield SessionLocal()\n",
            encoding="utf-8",
        )
        (app_dir / "main.py").write_text(
            "from collections.abc import AsyncIterator\n"
            "from contextlib import asynccontextmanager\n"
            "from fastapi import FastAPI\n\n"
            "@asynccontextmanager\n"
            "async def lifespan(app: FastAPI) -> AsyncIterator[None]:\n"
            "    yield\n",
            encoding="utf-8",
        )

    class _RecoveringClient(_FakeRalphClient):
        def __init__(self):
            super().__init__()
            self._calls = 0

        async def prompt(self, session_id, message, timeout):
            self.prompts.append(message)
            self._calls += 1
            if self._calls == 1:
                asyncio.create_task(_write_broken_files())
                return ACPResponse(content="我先初始化项目骨架。", error="Tool shell failed")
            asyncio.create_task(_write_fixed_files())
            await asyncio.Future()

    monkeypatch.setattr(
        AgentLoop,
        "_ralph_prepare_typecheck_environment",
        lambda self, project_dir, story: asyncio.sleep(0, result=""),
    )
    monkeypatch.setattr(
        AgentLoop,
        "_ralph_collect_verification_evidence",
        lambda self, project_dir, story: asyncio.sleep(0, result=""),
    )
    monkeypatch.setattr(
        AgentLoop,
        "_ralph_verification_passed",
        lambda self, project_dir, story: asyncio.sleep(
            0,
            result=(
                (project_dir / "app" / "database.py").exists()
                and (project_dir / "app" / "main.py").exists()
                and "Generator[Session, None, None]"
                in (project_dir / "app" / "database.py").read_text(encoding="utf-8")
                and "AsyncIterator[None]"
                in (project_dir / "app" / "main.py").read_text(encoding="utf-8")
            ),
        ),
    )

    fake_client = _RecoveringClient()
    adapter = _FakeRalphAdapter(workspace, _FakeRalphStdio(fake_client))
    loop = AgentLoop(
        bus=bus,
        adapter=adapter,
        model="glm-5",
        streaming=False,
        channel_manager=_FakeChannelManager(channel),
    )
    loop._ralph_prompt_poll_seconds = 0.02
    loop._ralph_artifact_watchdog_seconds = 0.05
    loop._ralph_story_idle_watchdog_seconds = 0.2
    loop._ralph_recovery_idle_watchdog_seconds = 0.2
    loop._ralph_story_settle_timeout_seconds = 0.05

    loop._ralph_set_current(chat_id, run_id)
    loop._ralph_save_state(
        run_dir,
        {
            "run_id": run_id,
            "status": "approved",
            "channel": "feishu",
            "story_index": 0,
            "pass_index": 0,
            "project_dir": str(project_dir),
        },
    )

    await asyncio.wait_for(loop._ralph_run_loop("feishu", chat_id, run_dir), timeout=2)

    state = json.loads((run_dir / "state.json").read_text(encoding="utf-8"))
    story = json.loads(prd_path.read_text(encoding="utf-8"))["stories"][0]
    progress = progress_path.read_text(encoding="utf-8")

    assert state["status"] == "done"
    assert story["passes"] is True
    assert "app/main.py" in progress or str(app_dir / "main.py") in progress
    assert len(fake_client.prompts) == 2


@pytest.mark.asyncio
async def test_ralph_run_loop_cancels_idle_prompt_and_retries(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    workspace = tmp_path / "workspace"
    workspace.mkdir(parents=True, exist_ok=True)
    bus = MessageBus()
    channel = _FakeChannel()

    run_id = "run-idle"
    chat_id = "ou_test"
    run_dir = workspace / "ralph" / chat_id / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    project_dir = workspace / "project" / "demo"
    project_dir.mkdir(parents=True, exist_ok=True)

    prd_path = run_dir / "prd.json"
    progress_path = run_dir / "progress.txt"
    progress_path.write_text("# Ralph Progress\n", encoding="utf-8")
    prd_path.write_text(
        json.dumps(
            {
                "stories": [
                    {
                        "id": "US-001",
                        "title": "项目结构设计",
                        "role": "engineer",
                        "acceptanceCriteria": ["输出目录树", "单元测试通过"],
                        "passes": False,
                    }
                ]
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )

    fake_client = _HangingRalphClient()
    adapter = _FakeRalphAdapter(workspace, _FakeRalphStdio(fake_client))
    loop = AgentLoop(
        bus=bus,
        adapter=adapter,
        model="glm-5",
        streaming=False,
        channel_manager=_FakeChannelManager(channel),
    )
    loop._ralph_prompt_poll_seconds = 0.02
    loop._ralph_story_idle_watchdog_seconds = 0.05

    captured = {}

    async def _fake_retry_incomplete_story(**kwargs):
        captured["failure_reason"] = kwargs.get("failure_reason", "")
        (project_dir / "structure.md").write_text("# structure", encoding="utf-8")
        prd = json.loads(prd_path.read_text(encoding="utf-8"))
        prd["stories"][0]["passes"] = True
        prd_path.write_text(json.dumps(prd, ensure_ascii=False, indent=2), encoding="utf-8")
        with open(progress_path, "a", encoding="utf-8") as f:
            f.write("\n## 2026-03-15 00:00 - US-001\n- 完成项目结构设计\n---\n")
        return ACPResponse(content="已补齐项目结构设计。", error=None)

    monkeypatch.setattr(loop, "_ralph_retry_incomplete_story", _fake_retry_incomplete_story)

    loop._ralph_set_current(chat_id, run_id)
    loop._ralph_save_state(
        run_dir,
        {
            "run_id": run_id,
            "status": "approved",
            "channel": "feishu",
            "story_index": 0,
            "pass_index": 0,
            "project_dir": str(project_dir),
        },
    )

    await asyncio.wait_for(loop._ralph_run_loop("feishu", chat_id, run_dir), timeout=5)

    state = json.loads((run_dir / "state.json").read_text(encoding="utf-8"))
    assert state["status"] == "done"
    assert "idle watchdog" in captured["failure_reason"].lower()


@pytest.mark.asyncio
async def test_ralph_run_loop_stops_instead_of_leaving_stale_running_state_when_retry_crashes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    workspace = tmp_path / "workspace"
    workspace.mkdir(parents=True, exist_ok=True)
    bus = MessageBus()
    channel = _FakeChannel()

    run_id = "run-retry-crash"
    chat_id = "ou_test"
    run_dir = workspace / "ralph" / chat_id / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    project_dir = workspace / "project" / "demo"
    project_dir.mkdir(parents=True, exist_ok=True)

    prd_path = run_dir / "prd.json"
    progress_path = run_dir / "progress.txt"
    progress_path.write_text("# Ralph Progress\n", encoding="utf-8")
    prd_path.write_text(
        json.dumps(
            {
                "stories": [
                    {
                        "id": "US-001",
                        "title": "项目结构设计",
                        "role": "engineer",
                        "acceptanceCriteria": ["输出目录树", "单元测试通过"],
                        "passes": False,
                    }
                ]
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )

    fake_client = _FakeRalphClient(
        responses=[
            {"response": ACPResponse(content="我先检查当前项目。", error="Tool shell failed")},
        ]
    )
    adapter = _FakeRalphAdapter(workspace, _FakeRalphStdio(fake_client))
    loop = AgentLoop(
        bus=bus,
        adapter=adapter,
        model="glm-5",
        streaming=False,
        channel_manager=_FakeChannelManager(channel),
    )

    async def _boom_retry(**kwargs):
        raise RuntimeError("retry crashed")

    monkeypatch.setattr(loop, "_ralph_retry_incomplete_story", _boom_retry)

    loop._ralph_tasks[chat_id] = asyncio.get_running_loop().create_future()
    loop._ralph_set_current(chat_id, run_id)
    loop._ralph_save_state(
        run_dir,
        {
            "run_id": run_id,
            "status": "approved",
            "channel": "feishu",
            "story_index": 0,
            "pass_index": 0,
            "project_dir": str(project_dir),
        },
    )

    await loop._ralph_run_loop("feishu", chat_id, run_dir)

    state = json.loads((run_dir / "state.json").read_text(encoding="utf-8"))
    assert state["status"] == "stopped"
    assert "retry crashed" in state["last_progress"]


@pytest.mark.asyncio
async def test_ralph_subagent_session_workspace_must_cover_project_dir(tmp_path: Path):
    workspace = tmp_path / "workspace"
    workspace.mkdir(parents=True, exist_ok=True)
    bus = MessageBus()
    channel = _FakeChannel()

    run_id = "run-5"
    chat_id = "ou_test"
    run_dir = workspace / "ralph" / chat_id / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    project_dir = workspace / "project" / "demo"
    project_dir.mkdir(parents=True, exist_ok=True)

    prd_path = run_dir / "prd.json"
    progress_path = run_dir / "progress.txt"
    progress_path.write_text("# Ralph Progress\n", encoding="utf-8")
    prd_path.write_text(
        json.dumps(
            {
                "stories": [
                    {
                        "id": "US-001",
                        "title": "技术栈选型调研",
                        "role": "researcher",
                        "acceptanceCriteria": ["输出 docs/research.md"],
                        "passes": False,
                    }
                ]
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )

    async def _complete_on_retry():
        await asyncio.sleep(0.05)
        docs = project_dir / "docs"
        docs.mkdir(parents=True, exist_ok=True)
        (docs / "research.md").write_text("# research", encoding="utf-8")
        prd = json.loads(prd_path.read_text(encoding="utf-8"))
        prd["stories"][0]["passes"] = True
        prd_path.write_text(json.dumps(prd, ensure_ascii=False, indent=2), encoding="utf-8")
        with open(progress_path, "a", encoding="utf-8") as f:
            f.write("\n## 2026-03-15 00:00 - US-001\n- 完成技术调研文档\n---\n")

    fake_client = _FakeRalphClient(
        responses=[
            {"response": ACPResponse(content="先读取上下文。", error="Tool shell failed")},
            {"response": ACPResponse(content="已补齐文档。", error=None), "after_prompt": _complete_on_retry},
        ]
    )
    adapter = _FakeRalphAdapter(workspace, _FakeRalphStdio(fake_client))
    loop = AgentLoop(
        bus=bus,
        adapter=adapter,
        model="glm-5",
        streaming=False,
        channel_manager=_FakeChannelManager(channel),
    )

    loop._ralph_set_current(chat_id, run_id)
    loop._ralph_save_state(
        run_dir,
        {
            "run_id": run_id,
            "status": "approved",
            "channel": "feishu",
            "story_index": 0,
            "pass_index": 0,
            "project_dir": str(project_dir),
        },
    )

    await loop._ralph_run_loop("feishu", chat_id, run_dir)

    workspaces = [call["workspace"] for call in fake_client.create_session_calls]
    assert workspaces == [workspace, workspace]


@pytest.mark.asyncio
async def test_ralph_subagent_session_workspace_expands_for_repo_context(tmp_path: Path, monkeypatch):
    workspace = tmp_path / "workspace"
    workspace.mkdir(parents=True, exist_ok=True)
    repo_root = tmp_path / "repo"
    (repo_root / ".git").mkdir(parents=True, exist_ok=True)
    monkeypatch.chdir(repo_root)

    bus = MessageBus()
    channel = _FakeChannel()

    run_id = "run-repo"
    chat_id = "ou_test"
    run_dir = workspace / "ralph" / chat_id / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    project_dir = workspace / "project" / "demo"
    project_dir.mkdir(parents=True, exist_ok=True)

    prd_path = run_dir / "prd.json"
    progress_path = run_dir / "progress.txt"
    progress_path.write_text("# Ralph Progress\n", encoding="utf-8")
    prd_path.write_text(
        json.dumps(
            {
                "stories": [
                    {
                        "id": "US-001",
                        "title": "本地实现调研",
                        "role": "researcher",
                        "acceptanceCriteria": ["输出 docs/report.md"],
                        "passes": False,
                    }
                ]
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    async def _complete_after_prompt():
        await asyncio.sleep(0.01)
        docs = project_dir / "docs"
        docs.mkdir(parents=True, exist_ok=True)
        (docs / "report.md").write_text("# report", encoding="utf-8")
        prd = json.loads(prd_path.read_text(encoding="utf-8"))
        prd["stories"][0]["passes"] = True
        prd_path.write_text(json.dumps(prd, ensure_ascii=False, indent=2), encoding="utf-8")
        with open(progress_path, "a", encoding="utf-8") as f:
            f.write("## 2026-03-15 00:00 - US-001\n- 完成\n---\n[RALPH_DONE]\n")

    fake_client = _FakeRalphClient(
        responses=[
            {"response": ACPResponse(content="已完成。", error=None), "after_prompt": _complete_after_prompt},
        ]
    )
    adapter = _FakeRalphAdapter(workspace, _FakeRalphStdio(fake_client))
    loop = AgentLoop(
        bus=bus,
        adapter=adapter,
        model="glm-5",
        streaming=False,
        channel_manager=_FakeChannelManager(channel),
    )

    loop._ralph_set_current(chat_id, run_id)
    loop._ralph_save_state(
        run_dir,
        {
            "run_id": run_id,
            "status": "approved",
            "channel": "feishu",
            "story_index": 0,
            "pass_index": 0,
            "project_dir": str(project_dir),
            "prompt": "比较当前仓库与 workspace 中的本地实现差异，只使用本地资料。",
        },
    )

    await loop._ralph_run_loop("feishu", chat_id, run_dir)

    workspaces = [Path(call["workspace"]) for call in fake_client.create_session_calls]
    assert workspaces == [tmp_path]


@pytest.mark.asyncio
async def test_ralph_auto_finalizes_research_story_when_artifact_written_but_prompt_hangs(tmp_path: Path):
    workspace = tmp_path / "workspace"
    workspace.mkdir(parents=True, exist_ok=True)
    bus = MessageBus()
    channel = _FakeChannel()

    run_id = "run-6"
    chat_id = "ou_test"
    run_dir = workspace / "ralph" / chat_id / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    project_dir = workspace / "project" / "demo"
    project_dir.mkdir(parents=True, exist_ok=True)

    prd_path = run_dir / "prd.json"
    progress_path = run_dir / "progress.txt"
    progress_path.write_text("# Ralph Progress\n", encoding="utf-8")
    prd_path.write_text(
        json.dumps(
            {
                "stories": [
                    {
                        "id": "US-001",
                        "title": "技术栈选型调研",
                        "role": "researcher",
                        "acceptanceCriteria": [
                            "完成至少 3 个主流 Python Web 框架的对比分析",
                            "明确推荐框架及理由",
                        ],
                        "passes": False,
                    }
                ]
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )

    async def _write_artifact_only():
        await asyncio.sleep(0.05)
        docs = project_dir / "docs"
        docs.mkdir(parents=True, exist_ok=True)
        (docs / "us-001-researcher-notes.md").write_text("# research", encoding="utf-8")

    fake_client = _HangingRalphClient(after_prompt=_write_artifact_only)
    adapter = _FakeRalphAdapter(workspace, _FakeRalphStdio(fake_client))
    loop = AgentLoop(
        bus=bus,
        adapter=adapter,
        model="glm-5",
        streaming=False,
        channel_manager=_FakeChannelManager(channel),
    )
    loop._ralph_artifact_watchdog_seconds = 0.05
    loop._ralph_prompt_poll_seconds = 0.02

    loop._ralph_set_current(chat_id, run_id)
    loop._ralph_save_state(
        run_dir,
        {
            "run_id": run_id,
            "status": "approved",
            "channel": "feishu",
            "story_index": 0,
            "pass_index": 0,
            "project_dir": str(project_dir),
        },
    )

    await asyncio.wait_for(loop._ralph_run_loop("feishu", chat_id, run_dir), timeout=1)

    state = json.loads((run_dir / "state.json").read_text(encoding="utf-8"))
    story = json.loads(prd_path.read_text(encoding="utf-8"))["stories"][0]
    progress = progress_path.read_text(encoding="utf-8")

    assert state["status"] != "running"
    assert story["passes"] is True
    assert "us-001-researcher-notes.md" in progress


@pytest.mark.asyncio
async def test_ralph_auto_finalizes_docs_only_story_when_output_directory_receives_markdown(tmp_path: Path):
    workspace = tmp_path / "workspace"
    workspace.mkdir(parents=True, exist_ok=True)
    bus = MessageBus()
    channel = _FakeChannel()

    run_id = "run-7"
    chat_id = "ou_test"
    run_dir = workspace / "ralph" / chat_id / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    project_dir = workspace / "project" / "docs-only"
    project_dir.mkdir(parents=True, exist_ok=True)

    prd_path = run_dir / "prd.json"
    progress_path = run_dir / "progress.txt"
    progress_path.write_text("# Ralph Progress\n", encoding="utf-8")
    prd_path.write_text(
        json.dumps(
            {
                "stories": [
                    {
                        "id": "US-001",
                        "title": "Web 框架对比分析文档",
                        "role": "researcher",
                        "acceptanceCriteria": [
                            "文档包含至少 4 个维度的框架对比",
                            f"文档以 Markdown 格式输出至 {project_dir} 目录",
                        ],
                        "passes": False,
                    }
                ]
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )

    async def _write_artifact_only():
        await asyncio.sleep(0.05)
        docs = project_dir / "docs"
        docs.mkdir(parents=True, exist_ok=True)
        (docs / "web-framework-analysis.md").write_text("# framework analysis", encoding="utf-8")

    fake_client = _HangingRalphClient(after_prompt=_write_artifact_only)
    adapter = _FakeRalphAdapter(workspace, _FakeRalphStdio(fake_client))
    loop = AgentLoop(
        bus=bus,
        adapter=adapter,
        model="glm-5",
        streaming=False,
        channel_manager=_FakeChannelManager(channel),
    )
    loop._ralph_artifact_watchdog_seconds = 0.05
    loop._ralph_prompt_poll_seconds = 0.02

    loop._ralph_set_current(chat_id, run_id)
    loop._ralph_save_state(
        run_dir,
        {
            "run_id": run_id,
            "status": "approved",
            "channel": "feishu",
            "story_index": 0,
            "pass_index": 0,
            "project_dir": str(project_dir),
        },
    )

    await asyncio.wait_for(loop._ralph_run_loop("feishu", chat_id, run_dir), timeout=1)

    state = json.loads((run_dir / "state.json").read_text(encoding="utf-8"))
    story = json.loads(prd_path.read_text(encoding="utf-8"))["stories"][0]
    progress = progress_path.read_text(encoding="utf-8")

    assert state["status"] == "done"
    assert story["passes"] is True
    assert "web-framework-analysis.md" in progress

def test_ralph_expected_artifact_paths_fallback_to_project_dir_for_impl_story_without_explicit_paths(tmp_path: Path):
    loop = AgentLoop(bus=MessageBus(), adapter=_FakeAdapter(tmp_path / 'workspace'), model='glm-5', streaming=False)
    project_dir = tmp_path / 'workspace' / 'project' / 'demo'

    artifact_paths = loop._ralph_expected_artifact_paths(
        {
            'id': 'US-001',
            'title': '项目初始化与基础架构搭建',
            'role': 'engineer',
            'acceptanceCriteria': ['初始化 FastAPI 项目结构', 'Typecheck passes'],
        },
        project_dir,
    )

    assert artifact_paths == [project_dir]


def test_ralph_expected_artifact_paths_ignores_malformed_chinese_list_separator_paths(tmp_path: Path):
    loop = AgentLoop(bus=MessageBus(), adapter=_FakeAdapter(tmp_path / "workspace"), model="glm-5", streaming=False)
    project_dir = tmp_path / "workspace" / "project" / "demo"

    artifact_paths = loop._ralph_expected_artifact_paths(
        {
            "id": "US-001",
            "title": "项目初始化与基础架构搭建",
            "role": "engineer",
            "acceptanceCriteria": [
                "配置 pyproject.toml 包含 FastAPI、Jinja2、aiosqlite 等依赖",
                "创建清晰的目录结构：app/、tests/、templates/、static/",
                "Typecheck passes",
            ],
        },
        project_dir,
    )

    assert artifact_paths == [project_dir]


@pytest.mark.asyncio
async def test_ralph_auto_finalizes_engineer_story_when_project_output_exists_and_typecheck_passes(tmp_path: Path, monkeypatch):
    workspace = tmp_path / 'workspace'
    workspace.mkdir(parents=True, exist_ok=True)
    bus = MessageBus()
    channel = _FakeChannel()

    run_id = 'run-eng-1'
    chat_id = 'ou_test'
    run_dir = workspace / 'ralph' / chat_id / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    project_dir = workspace / 'project' / 'demo'
    project_dir.mkdir(parents=True, exist_ok=True)

    prd_path = run_dir / 'prd.json'
    progress_path = run_dir / 'progress.txt'
    progress_path.write_text('# Ralph Progress\n', encoding='utf-8')
    prd_path.write_text(
        json.dumps(
            {
                'stories': [
                    {
                        'id': 'US-001',
                        'title': '项目初始化与基础架构搭建',
                        'role': 'engineer',
                        'acceptanceCriteria': ['初始化 FastAPI 项目结构', 'Typecheck passes'],
                        'passes': False,
                    }
                ]
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding='utf-8',
    )

    async def _write_project_files():
        await asyncio.sleep(0.01)
        app_dir = project_dir / 'app'
        app_dir.mkdir(parents=True, exist_ok=True)
        (app_dir / 'main.py').write_text('def app() -> None:\n    return None\n', encoding='utf-8')
        (project_dir / 'pyproject.toml').write_text('[project]\nname="demo"\nversion="0.1.0"\n', encoding='utf-8')
        venv_bin = project_dir / '.venv' / 'bin'
        venv_bin.mkdir(parents=True, exist_ok=True)
        mypy_bin = venv_bin / 'mypy'
        mypy_bin.write_text('#!/bin/sh\necho "Success: no issues found in 1 source file"\nexit 0\n', encoding='utf-8')
        mypy_bin.chmod(0o755)

    monkeypatch.setattr(AgentLoop, '_ralph_prepare_typecheck_environment', lambda self, project_dir, story: asyncio.sleep(0, result=''))

    fake_client = _FakeRalphClient(after_prompt=_write_project_files)
    adapter = _FakeRalphAdapter(workspace, _FakeRalphStdio(fake_client))
    loop = AgentLoop(
        bus=bus,
        adapter=adapter,
        model='glm-5',
        streaming=False,
        channel_manager=_FakeChannelManager(channel),
    )
    loop._ralph_prompt_poll_seconds = 0.02
    loop._ralph_artifact_watchdog_seconds = 0.05
    loop._ralph_story_idle_watchdog_seconds = 0.5
    loop._ralph_story_settle_timeout_seconds = 0.05
    loop._get_ralph_stdio_adapter = lambda: asyncio.sleep(0, result=adapter._stdio)  # type: ignore[method-assign]

    loop._ralph_set_current(chat_id, run_id)
    loop._ralph_save_state(
        run_dir,
        {
            'run_id': run_id,
            'status': 'approved',
            'channel': 'feishu',
            'story_index': 0,
            'pass_index': 0,
            'project_dir': str(project_dir),
        },
    )

    await asyncio.wait_for(loop._ralph_run_loop('feishu', chat_id, run_dir), timeout=5)

    state = json.loads((run_dir / 'state.json').read_text(encoding='utf-8'))
    story = json.loads(prd_path.read_text(encoding='utf-8'))['stories'][0]
    progress = progress_path.read_text(encoding='utf-8')

    assert state['status'] == 'done'
    assert story['passes'] is True
    assert 'app/main.py' in progress or str(project_dir / 'app' / 'main.py') in progress


@pytest.mark.asyncio
async def test_ralph_auto_finalizes_hanging_engineer_story_once_artifacts_and_typecheck_are_ready(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    workspace = tmp_path / "workspace"
    workspace.mkdir(parents=True, exist_ok=True)
    bus = MessageBus()
    channel = _FakeChannel()

    run_id = "run-eng-hanging-1"
    chat_id = "ou_test"
    run_dir = workspace / "ralph" / chat_id / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    project_dir = workspace / "project" / "demo"
    project_dir.mkdir(parents=True, exist_ok=True)

    prd_path = run_dir / "prd.json"
    progress_path = run_dir / "progress.txt"
    progress_path.write_text("# Ralph Progress\n", encoding="utf-8")
    prd_path.write_text(
        json.dumps(
            {
                "stories": [
                    {
                        "id": "US-001",
                        "title": "项目初始化与基础架构搭建",
                        "role": "engineer",
                        "acceptanceCriteria": ["初始化 FastAPI 项目结构", "Typecheck passes"],
                        "passes": False,
                    }
                ]
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )

    async def _write_project_files():
        await asyncio.sleep(0.02)
        app_dir = project_dir / "app"
        app_dir.mkdir(parents=True, exist_ok=True)
        (app_dir / "main.py").write_text("def app() -> None:\n    return None\n", encoding="utf-8")
        (project_dir / "pyproject.toml").write_text('[project]\nname="demo"\nversion="0.1.0"\n', encoding="utf-8")
        venv_bin = project_dir / ".venv" / "bin"
        venv_bin.mkdir(parents=True, exist_ok=True)
        mypy_bin = venv_bin / "mypy"
        mypy_bin.write_text(
            '#!/bin/sh\necho "Success: no issues found in 1 source file"\nexit 0\n',
            encoding="utf-8",
        )
        mypy_bin.chmod(0o755)

    monkeypatch.setattr(
        AgentLoop,
        "_ralph_prepare_typecheck_environment",
        lambda self, project_dir, story: asyncio.sleep(0, result=""),
    )
    monkeypatch.setattr(
        AgentLoop,
        "_ralph_collect_verification_evidence",
        lambda self, project_dir, story: asyncio.sleep(0, result=""),
    )
    monkeypatch.setattr(
        AgentLoop,
        "_ralph_verification_passed",
        lambda self, project_dir, story: asyncio.sleep(
            0,
            result=(project_dir / "app" / "main.py").exists(),
        ),
    )

    fake_client = _HangingRalphClient(after_prompt=_write_project_files)
    adapter = _FakeRalphAdapter(workspace, _FakeRalphStdio(fake_client))
    loop = AgentLoop(
        bus=bus,
        adapter=adapter,
        model="glm-5",
        streaming=False,
        channel_manager=_FakeChannelManager(channel),
    )
    loop._ralph_prompt_poll_seconds = 0.02
    loop._ralph_artifact_watchdog_seconds = 0.05
    loop._ralph_story_idle_watchdog_seconds = 0.5
    loop._ralph_story_settle_timeout_seconds = 0.05
    loop._get_ralph_stdio_adapter = lambda: asyncio.sleep(0, result=adapter._stdio)  # type: ignore[method-assign]

    loop._ralph_set_current(chat_id, run_id)
    loop._ralph_save_state(
        run_dir,
        {
            "run_id": run_id,
            "status": "approved",
            "channel": "feishu",
            "story_index": 0,
            "pass_index": 0,
            "project_dir": str(project_dir),
        },
    )

    await asyncio.wait_for(loop._ralph_run_loop("feishu", chat_id, run_dir), timeout=1)

    state = json.loads((run_dir / "state.json").read_text(encoding="utf-8"))
    story = json.loads(prd_path.read_text(encoding="utf-8"))["stories"][0]
    progress = progress_path.read_text(encoding="utf-8")

    assert state["status"] == "done"
    assert story["passes"] is True
    assert "app/main.py" in progress or str(project_dir / "app" / "main.py") in progress


@pytest.mark.asyncio
async def test_ralph_resume_auto_finalizes_engineer_story_when_valid_artifacts_already_exist(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    workspace = tmp_path / "workspace"
    workspace.mkdir(parents=True, exist_ok=True)
    bus = MessageBus()
    channel = _FakeChannel()

    run_id = "run-eng-resume-1"
    chat_id = "ou_test"
    run_dir = workspace / "ralph" / chat_id / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    project_dir = workspace / "project" / "demo"
    app_dir = project_dir / "app"
    app_dir.mkdir(parents=True, exist_ok=True)
    (app_dir / "main.py").write_text("def app() -> None:\n    return None\n", encoding="utf-8")
    (project_dir / "pyproject.toml").write_text('[project]\nname="demo"\nversion="0.1.0"\n', encoding="utf-8")
    venv_bin = project_dir / ".venv" / "bin"
    venv_bin.mkdir(parents=True, exist_ok=True)
    mypy_bin = venv_bin / "mypy"
    mypy_bin.write_text(
        '#!/bin/sh\necho "Success: no issues found in 1 source file"\nexit 0\n',
        encoding="utf-8",
    )
    mypy_bin.chmod(0o755)

    prd_path = run_dir / "prd.json"
    progress_path = run_dir / "progress.txt"
    progress_path.write_text("# Ralph Progress\nRESTART_DETECTED\nAUTO_RESUMED\n", encoding="utf-8")
    prd_path.write_text(
        json.dumps(
            {
                "stories": [
                    {
                        "id": "US-001",
                        "title": "项目初始化与基础架构搭建",
                        "role": "engineer",
                        "acceptanceCriteria": ["初始化 FastAPI 项目结构", "Typecheck passes"],
                        "passes": False,
                    }
                ]
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )

    monkeypatch.setattr(
        AgentLoop,
        "_ralph_prepare_typecheck_environment",
        lambda self, project_dir, story: asyncio.sleep(0, result=""),
    )
    monkeypatch.setattr(
        AgentLoop,
        "_ralph_collect_verification_evidence",
        lambda self, project_dir, story: asyncio.sleep(0, result=""),
    )
    monkeypatch.setattr(
        AgentLoop,
        "_ralph_verification_passed",
        lambda self, project_dir, story: asyncio.sleep(
            0,
            result=(project_dir / "app" / "main.py").exists(),
        ),
    )

    fake_client = _HangingRalphClient()
    adapter = _FakeRalphAdapter(workspace, _FakeRalphStdio(fake_client))
    loop = AgentLoop(
        bus=bus,
        adapter=adapter,
        model="glm-5",
        streaming=False,
        channel_manager=_FakeChannelManager(channel),
    )
    loop._ralph_prompt_poll_seconds = 0.02
    loop._ralph_artifact_watchdog_seconds = 0.05
    loop._ralph_story_idle_watchdog_seconds = 0.5
    loop._ralph_story_settle_timeout_seconds = 0.05
    loop._get_ralph_stdio_adapter = lambda: asyncio.sleep(0, result=adapter._stdio)  # type: ignore[method-assign]

    loop._ralph_set_current(chat_id, run_id)
    loop._ralph_save_state(
        run_dir,
        {
            "run_id": run_id,
            "status": "approved",
            "channel": "feishu",
            "story_index": 0,
            "pass_index": 0,
            "project_dir": str(project_dir),
            "current_started_at": time.time() - 10,
            "current_story_index": 1,
            "current_story_total": 1,
            "current_pass_index": 1,
            "current_pass_total": 1,
            "current_story_title": "项目初始化与基础架构搭建",
        },
    )

    await asyncio.wait_for(loop._ralph_run_loop("feishu", chat_id, run_dir), timeout=1)

    state = json.loads((run_dir / "state.json").read_text(encoding="utf-8"))
    story = json.loads(prd_path.read_text(encoding="utf-8"))["stories"][0]
    progress = progress_path.read_text(encoding="utf-8")

    assert state["status"] == "done"
    assert story["passes"] is True
    assert "app/main.py" in progress or str(project_dir / "app" / "main.py") in progress
    assert fake_client.prompts == []


@pytest.mark.asyncio
async def test_ralph_resume_auto_finalizes_when_artifacts_predate_current_started_at(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    workspace = tmp_path / "workspace"
    workspace.mkdir(parents=True, exist_ok=True)
    bus = MessageBus()
    channel = _FakeChannel()

    run_id = "run-eng-resume-older-artifacts"
    chat_id = "ou_test"
    run_dir = workspace / "ralph" / chat_id / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    project_dir = workspace / "project" / "demo"
    src_dir = project_dir / "src" / "todo_cli"
    tests_dir = project_dir / "tests"
    src_dir.mkdir(parents=True, exist_ok=True)
    tests_dir.mkdir(parents=True, exist_ok=True)

    (src_dir / "__init__.py").write_text("", encoding="utf-8")
    models_path = src_dir / "models.py"
    store_path = src_dir / "store.py"
    test_path = tests_dir / "test_store.py"
    models_path.write_text("class Todo: ...\n", encoding="utf-8")
    store_path.write_text("class TodoStore: ...\n", encoding="utf-8")
    test_path.write_text("def test_store() -> None:\n    assert True\n", encoding="utf-8")
    (project_dir / "pyproject.toml").write_text('[project]\nname=\"demo\"\nversion=\"0.1.0\"\n', encoding="utf-8")

    old_mtime = time.time() - 600
    for path in [models_path, store_path, test_path]:
        path.touch()
        os.utime(path, (old_mtime, old_mtime))

    prd_path = run_dir / "prd.json"
    progress_path = run_dir / "progress.txt"
    progress_path.write_text("# Ralph Progress\nRESUMED\n", encoding="utf-8")
    prd_path.write_text(
        json.dumps(
            {
                "stories": [
                    {
                        "id": "US-002",
                        "title": "数据模型与存储层",
                        "role": "engineer",
                        "acceptanceCriteria": [
                            "定义 Todo 数据类，包含 id、title、done、created_at 字段",
                            "实现 TodoStore 类，支持 load/save 操作",
                            "JSON 文件存储在项目目录下的 data/todos.json",
                            "Typecheck passes",
                            "Tests pass",
                        ],
                        "passes": False,
                    }
                ]
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )

    monkeypatch.setattr(
        AgentLoop,
        "_ralph_prepare_typecheck_environment",
        lambda self, project_dir, story: asyncio.sleep(0, result=""),
    )
    monkeypatch.setattr(
        AgentLoop,
        "_ralph_verification_passed",
        lambda self, project_dir, story: asyncio.sleep(0, result=True),
    )

    fake_client = _HangingRalphClient()
    adapter = _FakeRalphAdapter(workspace, _FakeRalphStdio(fake_client))
    loop = AgentLoop(
        bus=bus,
        adapter=adapter,
        model="glm-5",
        streaming=False,
        channel_manager=_FakeChannelManager(channel),
    )
    loop._ralph_prompt_poll_seconds = 0.02
    loop._ralph_artifact_watchdog_seconds = 0.05
    loop._ralph_story_idle_watchdog_seconds = 0.5
    loop._ralph_story_settle_timeout_seconds = 0.05
    loop._get_ralph_stdio_adapter = lambda: asyncio.sleep(0, result=adapter._stdio)  # type: ignore[method-assign]

    loop._ralph_set_current(chat_id, run_id)
    loop._ralph_save_state(
        run_dir,
        {
            "run_id": run_id,
            "status": "approved",
            "channel": "feishu",
            "story_index": 0,
            "pass_index": 0,
            "project_dir": str(project_dir),
            "current_started_at": time.time(),
            "current_story_index": 1,
            "current_story_total": 1,
            "current_pass_index": 1,
            "current_pass_total": 1,
            "current_story_title": "数据模型与存储层",
        },
    )

    await asyncio.wait_for(loop._ralph_run_loop("feishu", chat_id, run_dir), timeout=1)

    state = json.loads((run_dir / "state.json").read_text(encoding="utf-8"))
    story = json.loads(prd_path.read_text(encoding="utf-8"))["stories"][0]
    progress = progress_path.read_text(encoding="utf-8")

    assert state["status"] == "done"
    assert story["passes"] is True
    assert "models.py" in progress or str(models_path) in progress
    assert fake_client.prompts == []
