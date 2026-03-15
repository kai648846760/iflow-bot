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
