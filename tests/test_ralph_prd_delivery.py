from pathlib import Path

from iflow_bot.bus.queue import MessageBus
from iflow_bot.engine.loop import AgentLoop


class _DummyAdapter:
    def __init__(self):
        self.workspace = Path('/tmp')
        self.mode = 'cli'
        self.timeout = 60
        self.session_mappings = type('SM', (), {'clear_session': lambda self, c, ch: True})()


def test_ralph_prd_delivery_contains_full_markdown():
    loop = AgentLoop(bus=MessageBus(), adapter=_DummyAdapter(), model='glm-5', streaming=False)
    loop._load_language_setting = lambda: 'en-US'
    prd = {
        'project': 'Cron vs Ralph Loop Comparison',
        'userStories': [{'id': 'US-001', 'title': 'Research technical architecture differences'}],
    }
    prd_md = '# Title\n\n## Goals\n- Goal A\n\n## User Stories\n- Story A\n\n## Open Questions\n- Q1'
    content = loop._ralph_build_prd_ready_content(Path('/tmp/prd.md'), Path('/tmp/prd.json'), prd, prd_md)

    assert 'Project: Cron vs Ralph Loop Comparison' in content
    assert '## Goals' in content
    assert '## User Stories' in content
    assert '## Open Questions' in content
    assert '/tmp/prd.md' in content
    assert '/tmp/prd.json' in content


def test_ralph_prd_delivery_uses_normalized_story_criteria():
    loop = AgentLoop(bus=MessageBus(), adapter=_DummyAdapter(), model='glm-5', streaming=False)
    loop._load_language_setting = lambda: 'zh-CN'
    prd = {
        'project': 'Todo List Web 应用技术调研',
        'stories': [
            {
                'id': 'US-001',
                'title': '技术框架选型分析',
                'description': '作为研究员，我需要对比分析 FastAPI、Flask、Django 三大主流框架。',
                'role': 'researcher',
                'acceptanceCriteria': [
                    '完成三大框架在开发效率、性能、学习曲线等维度的对比表',
                    '基于项目需求（基础 CRUD）给出明确推荐',
                ],
            }
        ],
    }
    prd_md = """# Todo List Web 应用技术调研与方案文档

## 用户故事

### 用户故事 1：技术框架选型分析

**描述**：作为研究员，我需要对比分析 FastAPI、Flask、Django 三大主流框架。

**角色**：researcher

**验收标准**：
- 完成三大框架在开发效率、性能、学习曲线等维度的对比表
- 基于项目需求（基础 CRUD）给出明确推荐
- Typecheck passes
"""

    content = loop._ralph_build_prd_ready_content(Path('/tmp/prd.md'), Path('/tmp/prd.json'), prd, prd_md)

    assert '### 用户故事 1：技术框架选型分析' in content
    assert '基于项目需求（基础 CRUD）给出明确推荐' in content
    assert 'Typecheck passes' not in content


def test_ralph_prd_delivery_sanitizes_bullet_story_format():
    loop = AgentLoop(bus=MessageBus(), adapter=_DummyAdapter(), model='glm-5', streaming=False)
    loop._load_language_setting = lambda: 'zh-CN'
    prd = {
        'project': 'Todo List Web 应用',
        'stories': [
            {
                'id': 'US-001',
                'title': '技术选型调研',
                'description': '作为研究员，我希望完成技术栈选型调研。',
                'role': 'researcher',
                'acceptanceCriteria': [
                    '输出包含 Web 框架、数据库、前端方案的对比分析',
                    '给出明确的推荐方案及理由',
                ],
            }
        ],
    }
    prd_md = """# Todo List Web 应用产品需求文档（PRD）

## 用户故事

- **US-01：技术选型调研**
  - 作为研究员，我希望完成技术栈选型调研。
  - **角色**：researcher
  - **验收标准**：
    - 类型检查通过（Typecheck passes）
    - 输出包含 Web 框架、数据库、前端方案的对比分析
    - 给出明确的推荐方案及理由
"""

    content = loop._ralph_build_prd_ready_content(Path('/tmp/prd.md'), Path('/tmp/prd.json'), prd, prd_md)

    assert '- **US-01：技术选型调研**' in content
    assert '给出明确的推荐方案及理由' in content
    assert '类型检查通过（Typecheck passes）' not in content


def test_ralph_prd_delivery_rewrites_story_role_and_criteria_from_normalized_prd():
    loop = AgentLoop(bus=MessageBus(), adapter=_DummyAdapter(), model='glm-5', streaming=False)
    loop._load_language_setting = lambda: 'zh-CN'
    prd = {
        'project': 'Todo List Web 应用技术调研与架构设计',
        'stories': [
            {
                'id': 'US-003',
                'title': '系统架构设计文档输出',
                'description': '作为文档作者，我希望输出系统架构设计文档，以便团队后续实现。',
                'role': 'writer',
                'acceptanceCriteria': [
                    '架构图清晰展示前后端分层结构',
                    '说明数据流和请求处理流程',
                ],
            }
        ],
    }
    prd_md = """# Todo List Web 应用技术调研与架构设计文档

## 用户故事

### 故事 3：系统架构设计文档输出

**描述**：作为工程师，我希望获得完整的系统架构设计文档，以便理解系统各组件的职责和交互方式。

**角色**：engineer

**验收标准**：
- Typecheck passes
- 架构图清晰展示前后端分层结构
- 说明数据流和请求处理流程
"""

    content = loop._ralph_build_prd_ready_content(Path('/tmp/prd.md'), Path('/tmp/prd.json'), prd, prd_md)

    assert '**角色**：writer' in content
    assert '作为文档作者，我希望输出系统架构设计文档，以便团队后续实现。' in content
    assert 'Typecheck passes' not in content


def test_ralph_prd_delivery_uses_story_order_when_markdown_titles_differ():
    loop = AgentLoop(bus=MessageBus(), adapter=_DummyAdapter(), model='glm-5', streaming=False)
    loop._load_language_setting = lambda: 'zh-CN'
    prd = {
        'project': 'Todo List Web 应用技术调研与架构设计',
        'stories': [
            {
                'id': 'US-001',
                'title': 'Python Web 框架技术选型调研',
                'description': '作为研究员，我希望输出《Python Web 框架技术选型调研》相关调研文档，以便团队后续决策与实现。',
                'role': 'researcher',
                'acceptanceCriteria': [
                    '至少对比 3 个主流 Python Web 框架',
                    '输出技术选型对比表格',
                ],
            },
            {
                'id': 'US-002',
                'title': '系统架构设计文档',
                'description': '作为文档作者，我希望输出《系统架构设计文档》相关设计文档，以便团队后续实现。',
                'role': 'writer',
                'acceptanceCriteria': [
                    '包含系统架构 Mermaid 图',
                    '明确前端、后端、数据库各层职责',
                ],
            },
        ],
    }
    prd_md = """# Todo List Web 应用技术调研与架构设计 PRD

## 用户故事

### 故事 1：技术选型调研

- **描述**：作为研究员，我希望完成 Python Web 框架的对比分析，以便为项目选择最合适的技术栈。
- **角色**：researcher
- **验收标准**：
  - 至少对比 3 个主流 Python Web 框架
  - 输出技术选型对比表格
  - Typecheck passes

### 故事 2：架构设计文档

- **描述**：作为工程师，我希望获得清晰的系统架构设计文档，以便理解系统各组件的职责与交互方式。
- **角色**：engineer
- **验收标准**：
  - 包含系统架构 Mermaid 图
  - 明确前端、后端、数据库各层职责
  - Typecheck passes
"""

    content = loop._ralph_build_prd_ready_content(Path('/tmp/prd.md'), Path('/tmp/prd.json'), prd, prd_md)

    assert '- **角色**：researcher' in content
    assert '- **角色**：writer' in content
    assert '作为文档作者，我希望输出《系统架构设计文档》相关设计文档，以便团队后续实现。' in content
    assert 'Typecheck passes' not in content


def test_ralph_prd_delivery_sanitizes_ascii_colon_story_fields():
    loop = AgentLoop(bus=MessageBus(), adapter=_DummyAdapter(), model='glm-5', streaming=False)
    loop._load_language_setting = lambda: 'zh-CN'
    prd = {
        'project': 'Todo List Web 应用技术调研与架构文档',
        'stories': [
            {
                'id': 'US-003',
                'title': '应用目录结构设计',
                'description': '作为文档作者，我需要设计符合 Python 最佳实践的目录结构，以便后续开发有清晰的代码组织规范。',
                'role': 'writer',
                'acceptanceCriteria': [
                    '定义完整的目录树结构',
                    '说明各目录和核心文件的职责',
                ],
            }
        ],
    }
    prd_md = """# Todo List Web 应用技术调研与架构文档

## 用户故事

### 故事 3: 应用目录结构设计

**描述**: 作为工程师，我需要设计符合 Python 最佳实践的目录结构，以便后续开发有清晰的代码组织规范。

**角色**: engineer

**验收标准**:
- 定义完整的目录树结构
- 说明各目录和核心文件的职责
- Typecheck passes
"""

    content = loop._ralph_build_prd_ready_content(Path('/tmp/prd.md'), Path('/tmp/prd.json'), prd, prd_md)

    assert '**角色**: writer' in content
    assert '作为文档作者，我需要设计符合 Python 最佳实践的目录结构，以便后续开发有清晰的代码组织规范。' in content
    assert 'Typecheck passes' not in content


def test_ralph_prd_delivery_rewrites_incomplete_story_sections_from_normalized_prd():
    loop = AgentLoop(bus=MessageBus(), adapter=_DummyAdapter(), model='glm-5', streaming=False)
    loop._load_language_setting = lambda: 'zh-CN'
    prd = {
        'project': 'Todo List Web 应用',
        'stories': [
            {
                'id': 'US-003',
                'title': '任务列表展示页面',
                'description': '作为用户，我希望看到所有任务的列表，以便一目了然地了解待办事项。',
                'role': 'engineer',
                'acceptanceCriteria': [
                    '页面加载时从数据库获取所有任务',
                    '按创建时间倒序排列显示',
                    '清晰展示任务内容和完成状态',
                    'Typecheck passes',
                ],
            },
            {
                'id': 'US-004',
                'title': '新增任务功能',
                'description': '作为用户，我希望能够新增任务，以便记录我需要完成的事项。',
                'role': 'engineer',
                'acceptanceCriteria': [
                    '提供任务输入表单',
                    '提交后将任务保存到数据库',
                    '页面显示新增的任务',
                    'Typecheck passes',
                ],
            },
        ],
    }
    prd_md = """# Todo List Web 应用 PRD

## 用户故事

### 用户故事 3：新增任务

- **描述**：作为用户，我希望看到所有任务的列表，以便一目了然地了解待办事项。
- **角色**：engineer
- **验收标准**：

### 用户故事 4：完成任务

- **描述**：作为用户，我希望能够新增任务，以便记录我需要完成的事项。
- **角色**：engineer
- **验收标准**：
"""

    content = loop._ralph_build_prd_ready_content(Path('/tmp/prd.md'), Path('/tmp/prd.json'), prd, prd_md)

    assert '### 用户故事 1：任务列表展示页面' in content
    assert '### 用户故事 2：新增任务功能' in content
    assert '页面加载时从数据库获取所有任务' in content
    assert '提供任务输入表单' in content


def test_ralph_prompt_constraints_enforce_required_roles_from_prompt():
    loop = AgentLoop(bus=MessageBus(), adapter=_DummyAdapter(), model='glm-5', streaming=False)
    prd = {
        'project': 'Docs',
        'stories': [
            {
                'id': 'US-001',
                'title': '源码调研',
                'description': '调研命令实现',
                'acceptanceCriteria': ['定位实现入口'],
                'role': 'researcher',
                'priority': 1,
                'passes': False,
                'notes': '',
            },
            {
                'id': 'US-002',
                'title': '编写使用指南',
                'description': '输出文档',
                'acceptanceCriteria': ['输出指南文档'],
                'role': 'writer',
                'priority': 2,
                'passes': False,
                'notes': '',
            },
            {
                'id': 'US-003',
                'title': '验证文档准确性与完整性',
                'description': '做最终检查',
                'acceptanceCriteria': ['验证命令说明准确', '检查状态流转覆盖完整'],
                'role': 'writer',
                'priority': 3,
                'passes': False,
                'notes': '',
            },
        ],
    }
    prd['userStories'] = list(prd['stories'])

    constrained = loop._ralph_apply_prompt_constraints_to_prd(
        prd,
        prompt='故事必须覆盖 researcher、writer、qa 三种角色；只输出文档。',
    )

    roles = [story['role'] for story in constrained['stories']]
    assert 'researcher' in roles
    assert 'writer' in roles
    assert 'qa' in roles
    assert constrained['stories'][2]['role'] == 'qa'


def test_ralph_prompt_constraints_concretize_flask_json_todo_stories():
    loop = AgentLoop(bus=MessageBus(), adapter=_DummyAdapter(), model='glm-5', streaming=False)
    prd = {
        'project': 'Todo List Web Application',
        'stories': [
            {'id': 'US-001', 'title': '项目初始化与基础架构', 'description': '初始化项目', 'acceptanceCriteria': ['Typecheck passes'], 'role': 'engineer'},
            {'id': 'US-002', 'title': '新增任务功能', 'description': '新增任务', 'acceptanceCriteria': ['Typecheck passes'], 'role': 'engineer'},
            {'id': 'US-003', 'title': '完成任务功能', 'description': '完成任务', 'acceptanceCriteria': ['Typecheck passes'], 'role': 'engineer'},
            {'id': 'US-004', 'title': '删除任务功能', 'description': '删除任务', 'acceptanceCriteria': ['Typecheck passes'], 'role': 'engineer'},
        ],
    }
    prd['userStories'] = list(prd['stories'])

    constrained = loop._ralph_apply_prompt_constraints_to_prd(
        prd,
        prompt='做一个 Todo List Web 应用，使用 Python 3 + uv，输出到 /Users/LokiTina/.iflow-bot/workspace/project/todolist，支持新增、完成、删除任务。',
        qa_block='必须使用 Flask + JSON 文件持久化；不要注册登录；界面极简。',
    )

    story1 = constrained['stories'][0]
    assert story1['title'] == '项目初始化与基础架构'
    assert '输出目录: /Users/LokiTina/.iflow-bot/workspace/project/todolist' in story1['acceptanceCriteria']
    assert '必须包含文件: pyproject.toml、app.py、templates/index.html、todos.json' in story1['acceptanceCriteria']
    assert '启动命令: uv run python app.py' in story1['acceptanceCriteria']
    assert 'Tests pass' in story1['acceptanceCriteria']

    story2 = constrained['stories'][1]
    assert '提交表单到路由 POST /add' in story2['acceptanceCriteria']
    assert 'Tests pass' in story2['acceptanceCriteria']

    story3 = constrained['stories'][2]
    assert '提交到路由 POST /complete/<id>' in story3['acceptanceCriteria']
    assert 'Tests pass' in story3['acceptanceCriteria']

    story4 = constrained['stories'][3]
    assert '提交到路由 POST /delete/<id>' in story4['acceptanceCriteria']
    assert 'Tests pass' in story4['acceptanceCriteria']


def test_ralph_prompt_constraints_canonicalize_flask_json_todo_story_list():
    loop = AgentLoop(bus=MessageBus(), adapter=_DummyAdapter(), model='glm-5', streaming=False)
    prd = {
        'project': 'Todo List Web Application',
        'stories': [
            {'id': 'US-001', 'title': '项目初始化与基础架构', 'description': '初始化项目', 'acceptanceCriteria': ['Typecheck passes'], 'role': 'engineer'},
            {'id': 'US-002', 'title': '新增任务功能', 'description': '新增任务', 'acceptanceCriteria': ['Typecheck passes'], 'role': 'engineer'},
            {'id': 'US-003', 'title': '新增任务功能', 'description': '重复的新增任务', 'acceptanceCriteria': ['Typecheck passes'], 'role': 'engineer'},
            {'id': 'US-004', 'title': '完成任务功能', 'description': '完成任务', 'acceptanceCriteria': ['Typecheck passes'], 'role': 'engineer'},
            {'id': 'US-005', 'title': '删除任务功能', 'description': '删除任务', 'acceptanceCriteria': ['Typecheck passes'], 'role': 'engineer'},
            {'id': 'US-006', 'title': 'Web 界面集成与整体测试', 'description': '泛化故事', 'acceptanceCriteria': ['Typecheck passes'], 'role': 'engineer'},
        ],
    }
    prd['userStories'] = list(prd['stories'])

    constrained = loop._ralph_apply_prompt_constraints_to_prd(
        prd,
        prompt='做一个 Todo List Web 应用，使用 Python 3 + uv，输出到 /Users/LokiTina/.iflow-bot/workspace/project/todolist，支持新增、完成、删除任务。',
        qa_block='必须使用 Flask + JSON 文件持久化；不要注册登录；界面极简。',
    )

    titles = [story['title'] for story in constrained['stories']]
    assert titles == [
        '项目初始化与基础架构',
        '新增任务功能',
        '完成任务功能',
        '删除任务功能',
    ]
    assert len(constrained['stories']) == 4
    assert constrained['stories'] == constrained['userStories']


def test_ralph_prompt_constraints_default_todo_stack_to_flask_json_when_unspecified():
    loop = AgentLoop(bus=MessageBus(), adapter=_DummyAdapter(), model='glm-5', streaming=False)
    prd = {
        'project': 'Todo List Web 应用',
        'stories': [
            {'id': 'US-001', 'title': '项目初始化与基础结构', 'description': '初始化项目', 'acceptanceCriteria': ['Typecheck passes'], 'role': 'engineer'},
            {'id': 'US-002', 'title': '数据模型与存储层', 'description': '建立 SQLite 存储层', 'acceptanceCriteria': ['Typecheck passes'], 'role': 'engineer'},
            {'id': 'US-003', 'title': '后端 API 路由', 'description': '实现 CRUD API', 'acceptanceCriteria': ['Typecheck passes'], 'role': 'engineer'},
            {'id': 'US-004', 'title': 'Web 界面 - 任务列表展示', 'description': '展示列表', 'acceptanceCriteria': ['Typecheck passes'], 'role': 'engineer'},
            {'id': 'US-005', 'title': 'Web 界面 - 新增任务', 'description': '新增任务', 'acceptanceCriteria': ['Typecheck passes'], 'role': 'engineer'},
            {'id': 'US-006', 'title': 'Web 界面 - 完成任务', 'description': '完成任务', 'acceptanceCriteria': ['Typecheck passes'], 'role': 'engineer'},
            {'id': 'US-007', 'title': 'Web 界面 - 删除任务', 'description': '删除任务', 'acceptanceCriteria': ['Typecheck passes'], 'role': 'engineer'},
            {'id': 'US-008', 'title': '应用集成验证', 'description': '整体验证', 'acceptanceCriteria': ['Typecheck passes'], 'role': 'engineer'},
        ],
    }
    prd['userStories'] = list(prd['stories'])

    constrained = loop._ralph_apply_prompt_constraints_to_prd(
        prd,
        prompt='做一个 Todo List Web 应用，使用 Python 3 + uv，输出到 /Users/LokiTina/.iflow-bot/workspace/project/todolist，支持新增、完成、删除任务。',
        qa_block='目标是产出一个可运行的 Todo List Web 应用；交付物是完整项目代码，输出到 /Users/LokiTina/.iflow-bot/workspace/project/todolist；约束：使用 Python 3 + uv，允许修改该输出目录内文件，不要改其他环境或无关目录。',
    )

    titles = [story['title'] for story in constrained['stories']]
    assert titles == [
        '项目初始化与基础架构',
        '新增任务功能',
        '完成任务功能',
        '删除任务功能',
    ]
    assert len(constrained['stories']) == 4
    assert '提交表单到路由 POST /add' in constrained['stories'][1]['acceptanceCriteria']
    assert '提交到路由 POST /complete/<id>' in constrained['stories'][2]['acceptanceCriteria']
    assert '提交到路由 POST /delete/<id>' in constrained['stories'][3]['acceptanceCriteria']


def test_ralph_prompt_constraints_respect_explicit_fastapi_sqlite_choices():
    loop = AgentLoop(bus=MessageBus(), adapter=_DummyAdapter(), model='glm-5', streaming=False)
    prd = {
        'project': 'Todo List Web 应用',
        'stories': [
            {'id': 'US-001', 'title': '项目初始化与基础结构', 'description': '初始化项目', 'acceptanceCriteria': ['Typecheck passes'], 'role': 'engineer'},
            {'id': 'US-002', 'title': '数据模型与存储层', 'description': '建立 SQLite 存储层', 'acceptanceCriteria': ['Typecheck passes'], 'role': 'engineer'},
        ],
    }
    prd['userStories'] = list(prd['stories'])

    constrained = loop._ralph_apply_prompt_constraints_to_prd(
        prd,
        prompt='做一个 Todo List Web 应用，使用 Python 3 + uv，支持新增、完成、删除任务。',
        qa_block='必须使用 FastAPI + SQLite；前后端分离不是必须。',
    )

    assert [story['title'] for story in constrained['stories']] == [
        '项目初始化与基础结构',
        '数据模型与存储层',
    ]


def test_ralph_prompt_constraints_respect_explicit_flask_sqlite_choices():
    loop = AgentLoop(bus=MessageBus(), adapter=_DummyAdapter(), model='glm-5', streaming=False)
    prd = {
        'project': 'Todo List Web 应用',
        'stories': [
            {'id': 'US-001', 'title': '项目初始化与基础架构', 'description': '初始化项目', 'acceptanceCriteria': ['Typecheck passes'], 'role': 'engineer'},
            {'id': 'US-002', 'title': '新增任务功能', 'description': '新增任务', 'acceptanceCriteria': ['Typecheck passes'], 'role': 'engineer'},
            {'id': 'US-003', 'title': '完成任务功能', 'description': '完成任务', 'acceptanceCriteria': ['Typecheck passes'], 'role': 'engineer'},
            {'id': 'US-004', 'title': '删除任务功能', 'description': '删除任务', 'acceptanceCriteria': ['Typecheck passes'], 'role': 'engineer'},
        ],
    }
    prd['userStories'] = list(prd['stories'])

    constrained = loop._ralph_apply_prompt_constraints_to_prd(
        prd,
        prompt='做一个 Todo List Web 应用，使用 Python 3 + uv，输出到 /Users/LokiTina/.iflow-bot/workspace/project/todolist，支持新增、完成、删除任务。',
        qa_block='必须使用 Flask + SQLite；不要注册登录；界面极简。',
    )

    story1 = constrained['stories'][0]
    joined1 = '\n'.join(story1['acceptanceCriteria'])
    assert 'templates/index.html' in joined1
    assert 'todo.db' in joined1 or 'SQLite' in joined1 or '数据库' in joined1
    assert 'todos.json' not in joined1

    story2 = constrained['stories'][1]
    joined2 = '\n'.join(story2['acceptanceCriteria'])
    assert '提交表单到路由 POST /add' in joined2
    assert '数据库' in joined2 or 'SQLite' in joined2
    assert 'todos.json' not in joined2

    story3 = constrained['stories'][2]
    joined3 = '\n'.join(story3['acceptanceCriteria'])
    assert '提交到路由 POST /complete/<id>' in joined3
    assert 'todos.json' not in joined3

    story4 = constrained['stories'][3]
    joined4 = '\n'.join(story4['acceptanceCriteria'])
    assert '提交到路由 POST /delete/<id>' in joined4
    assert 'todos.json' not in joined4
