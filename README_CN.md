# 🤖 iflow-bot

```
/$$ /$$$$$$$$ /$$                                 /$$$$$$$              /$$    
|__/| $$_____/| $$                                | $$__  $$            | $$    
 /$$| $$      | $$  /$$$$$$  /$$  /$$  /$$        | $$  \ $$  /$$$$$$  /$$$$$$  
| $$| $$$$$   | $$ /$$__  $$| $$ | $$ | $$ /$$$$$$| $$$$$$$  /$$__  $$|_  $$_/  
| $$| $$__/   | $$| $$  \ $$| $$ | $$ | $$|______/| $$__  $$| $$  \ $$  | $$    
| $$| $$      | $$| $$  | $$| $$ | $$ | $$        | $$  \ $$| $$  \ $$  | $$ /$$
| $$| $$      | $$|  $$$$$$/|  $$$$$/$$$$/        | $$$$$$$/|  $$$$$$/  |  $$$$/
|__/|__/      |__/ \______/  \_____/\___/         |_______/  \______/    \___/     
```

[English](README.md)|**中文**

**多渠道 AI 助手** - 基于 iflow CLI 构建的多平台消息机器人。

将 iflow 的强大 AI 能力扩展到多个通讯平台，让 AI 助手无处不在。

## ✨ 特性

- 🔌 **多渠道支持** - Telegram、Discord、Slack、飞书、钉钉、QQ、WhatsApp、Email、Mochat
- 🧠 **AI 驱动** - 基于 iflow CLI，支持多种模型（GLM-5、Kimi K2.5、MiniMax M2.5 等）
- 💾 **会话管理** - 自动管理多用户会话，支持对话上下文
- 📁 **工作空间** - 每个机器人实例拥有独立的工作空间和记忆系统
- 🔐 **权限控制** - 支持白名单、提及触发等多种策略
- 🔄 **思考模式** - 可选启用 AI 思考过程展示
- ⚡ **流式输出** - 支持 Telegram、钉钉 AI Card 实时流式输出
- 🚀 **Stdio 模式** - 直接通过 stdin/stdout 与 iflow 通信，响应更快

## 🎬 演示

### Telegram 流式输出

![Telegram 流式输出演示](https://github.com/kai648846760/iflow-bot/raw/master/testcase/Lark20260225-200437.gif)

### 钉钉 AI Card 流式输出

![钉钉 AI Card 流式输出演示](https://github.com/kai648846760/iflow-bot/raw/master/testcase/Lark20260225-200423.gif)

## 📋 前置要求

### 1. 安装 iflow CLI

iflow-bot 依赖 iflow CLI 运行，请先安装：

```bash
# 已有Node.js 22+
npm i -g @iflow-ai/iflow-cli@latest
```

### 2. 登录 iflow

```bash
iflow
```
1. 运行 iflow 后选择 OLogin with iFlow 登录
2. CLI 会自动打开浏览器跳转到心流平台
3. 完成注册/登录后授权 iFlow CLI
4. 自动返回终端，开始使用

按照提示完成登录流程。

## 🚀 快速开始

### 安装

**方式一：pip 安装（推荐）**

```bash
pip install iflow-bot
```

安装完成后即可直接使用：

```bash
iflow-bot --help
iflow-bot onboard
iflow-bot gateway start
```

**方式二：从源码安装**

```bash
# 克隆仓库
git clone https://github.com/your-repo/iflow-bot.git
cd iflow-bot

# 安装依赖（使用 uv）
uv sync
```

### 初始化配置

```bash
# 创建默认配置文件（pip安装）
iflow-bot onboard

# 或源码安装
uv run iflow-bot onboard

# 或手动创建
mkdir -p ~/.iflow-bot
cp config.example.json ~/.iflow-bot/config.json
```

### 启动服务

**pip 安装后：**

```bash
# 前台运行（调试模式）
iflow-bot gateway run

# 后台运行
iflow-bot gateway start

# 查看状态
iflow-bot status

# 停止服务
iflow-bot gateway stop
```

**源码安装后：**

```bash
# 前台运行（调试模式）
uv run iflow-bot gateway run

# 后台运行
uv run iflow-bot gateway start
```

## 🐳 Docker 部署

```bash
# 构建镜像
docker build -t iflow-bot:latest .

# 准备宿主机配置文件
mkdir -p ./config
cp config/config.example.json ./config/config.json
```

然后编辑 `./config/config.json`，按需启用渠道并填写 token。

```bash
# 使用 docker compose 启动
docker compose up -d

# 查看日志
docker compose logs -f iflow-bot
```

## ⚙️ 配置说明

配置文件位于 `~/.iflow-bot/config.json`

### 完整配置示例

```json
{
  "driver": {
    "mode": "stdio",
    "iflow_path": "iflow",
    "model": "minimax-m2.5",
    "yolo": true,
    "thinking": false,
    "max_turns": 40,
    "timeout": 600,
    "workspace": "~/.iflow-bot/workspace",
    "extra_args": []
  },
  "channels": {
    "telegram": {
      "enabled": true,
      "token": "YOUR_BOT_TOKEN",
      "allow_from": []
    },
    "discord": {
      "enabled": false,
      "token": "YOUR_BOT_TOKEN",
      "allow_from": []
    },
    "slack": {
      "enabled": false,
      "bot_token": "xoxb-xxx",
      "app_token": "xapp-xxx",
      "allow_from": [],
      "group_policy": "mention"
    },
    "feishu": {
      "enabled": false,
      "app_id": "cli_xxx",
      "app_secret": "xxx",
      "encrypt_key": "",
      "verification_token": "",
      "allow_from": []
    },
    "dingtalk": {
      "enabled": false,
      "client_id": "xxx",
      "client_secret": "xxx",
      "robot_code": "xxx",
      "card_template_id": "xxx-xxx-xxx",
      "card_template_key": "content",
      "allow_from": []
    },
    "qq": {
      "enabled": false,
      "app_id": "xxx",
      "secret": "xxx",
      "allow_from": []
    },
    "whatsapp": {
      "enabled": false,
      "bridge_url": "http://localhost:3001",
      "bridge_token": "",
      "allow_from": []
    },
    "email": {
      "enabled": false,
      "consent_granted": false,
      "imap_host": "imap.gmail.com",
      "imap_port": 993,
      "imap_username": "your@email.com",
      "imap_password": "app_password",
      "smtp_host": "smtp.gmail.com",
      "smtp_port": 587,
      "smtp_username": "your@email.com",
      "smtp_password": "app_password",
      "from_address": "your@email.com",
      "allow_from": [],
      "auto_reply_enabled": true
    },
    "mochat": {
      "enabled": false,
      "base_url": "https://mochat.io",
      "socket_url": "https://mochat.io",
      "socket_path": "/socket.io",
      "claw_token": "xxx",
      "agent_user_id": "",
      "sessions": ["*"],
      "panels": ["*"]
    }
  },
  "messages": {
    "new_conversation": "✨ New conversation started, previous context has been cleared."
  },
  "log_level": "INFO",
  "log_file": ""
}
```

### Driver 配置

| 参数 | 类型 | 默认值 | 说明 |
|------|------|--------|------|
| `mode` | string | `"stdio"` | 通信模式：`stdio`（推荐）、`acp`（WebSocket）、`cli`（子进程） |
| `iflow_path` | string | `"iflow"` | iflow CLI 路径【保持默认即可】 |
| `model` | string | `"minimax-m2.5"` | 默认模型（glm-5、kimi-k2.5、minimax-m2.5 等） |
| `yolo` | bool | `true` | 自动确认模式 |
| `thinking` | bool | `false` | 显示 AI 思考过程 |
| `max_turns` | int | `40` | 单次最大对话轮次 |
| `timeout` | int | `600` | 超时时间（秒） |
| `workspace` | string | `~/.iflow-bot/workspace` | 工作空间路径 |
| `extra_args` | list | `[]` | 额外的 iflow 参数 |
| `acp_port` | int | `8090` | ACP 模式下的端口号 |
| `acp_host` | string | `"localhost"` | ACP 模式下的主机地址 |

#### 通信模式说明

**Stdio 模式（⭐ 推荐）**：
- 直接通过 stdin/stdout 与 iflow 通信
- 无需启动 WebSocket 服务，启动更快
- 支持实时流式输出，打字机效果
- 响应延迟更低，接近原生体验
- Gateway 启动时自动执行 `iflow --experimental-acp --stream`

**ACP 模式（WebSocket）**：
- 通过 WebSocket 与 iflow 通信
- 需要先启动 WebSocket 服务器[自动启动]
- 支持实时流式输出
- 适合需要远程连接的场景

**CLI 模式**：
- 通过子进程调用 iflow CLI
- 每次对话独立启动进程
- 适合简单场景或调试

#### 推荐配置

```json
{
  "driver": {
    "mode": "stdio",
    "model": "minimax-m2.5",
    "thinking": false,
    "yolo": true
  }
}
```

### 渠道配置

#### Telegram

```json
{
  "telegram": {
    "enabled": true,
    "token": "YOUR_BOT_TOKEN",
    "allow_from": ["user_id_1", "user_id_2"]
  }
}
```

1. 在 [@BotFather](https://t.me/BotFather) 创建机器人获取 Token
2. `allow_from` 为空表示允许所有用户

#### Discord

```json
{
  "discord": {
    "enabled": true,
    "token": "YOUR_BOT_TOKEN",
    "allow_from": ["user_id_1"]
  }
}
```

1. 在 [Discord Developer Portal](https://discord.com/developers/applications) 创建应用
2. 创建 Bot 用户并获取 Token
3. 启用 Message Content Intent

#### Slack

```json
{
  "slack": {
    "enabled": true,
    "bot_token": "xoxb-xxx",
    "app_token": "xapp-xxx",
    "allow_from": [],
    "group_policy": "mention"
  }
}
```

1. 在 [Slack API](https://api.slack.com/apps) 创建应用
2. 创建 Bot 并获取 Bot Token (`xoxb-xxx`)
3. 启用 Socket Mode 获取 App Token (`xapp-xxx`)
4. `group_policy` 控制频道消息响应策略：
   - `mention`: 只响应 @提及
   - `open`: 响应所有消息
   - `allowlist`: 只响应白名单频道

#### 飞书 (Feishu/Lark)

```json
{
  "feishu": {
    "enabled": true,
    "app_id": "cli_xxx",
    "app_secret": "xxx",
    "encrypt_key": "",
    "verification_token": "",
    "allow_from": []
  }
}
```

1. 在 [飞书开放平台](https://open.feishu.cn/) 创建企业自建应用
2. 启用机器人能力
3. 配置事件订阅（使用 WebSocket，无需公网 IP）

#### 钉钉 (DingTalk)

```json
{
  "dingtalk": {
    "enabled": true,
    "client_id": "xxx",
    "client_secret": "xxx",
    "robot_code": "xxx",
    "card_template_id": "xxx-xxx-xxx",
    "card_template_key": "content",
    "allow_from": []
  }
}
```

1. 在 [钉钉开放平台](https://open.dingtalk.com/) 创建机器人
2. 获取 Client ID 和 Client Secret
3. 启用 Stream Mode（无需公网 IP）

**AI Card 流式输出配置**（可选，实现打字机效果）：

| 参数 | 说明 |
|------|------|
| `robot_code` | 机器人代码，群聊时需要配置 |
| `card_template_id` | AI Card 模板 ID，在钉钉开发者后台创建 |
| `card_template_key` | 模板内容字段名，默认 `content` |

**创建 AI Card 模板**：
1. 登录 [钉钉开发者后台](https://open.dingtalk.com/)
2. 进入「卡片平台」→「卡片模板」
3. 创建模板，添加一个「文本」类型的字段
4. 记录模板 ID 和字段名，配置到 `card_template_id` 和 `card_template_key`

**流式输出效果**：
- 用户发送消息后，机器人立即回复一张空白卡片
- 卡片内容实时更新，呈现打字机效果
- 无需等待完整响应，体验更流畅

#### QQ

```json
{
  "qq": {
    "enabled": true,
    "app_id": "xxx",
    "secret": "xxx",
    "allow_from": []
  }
}
```

1. 在 [QQ 开放平台](https://q.qq.com/) 创建机器人
2. 获取 App ID 和 Secret

#### WhatsApp

```json
{
  "whatsapp": {
    "enabled": true,
    "bridge_url": "http://localhost:3001",
    "bridge_token": "",
    "allow_from": []
  }
}
```

需要部署 [WhatsApp Bridge](https://github.com/your-repo/whatsapp-bridge)（基于 baileys）

#### Email

```json
{
  "email": {
    "enabled": true,
    "consent_granted": true,
    "imap_host": "imap.gmail.com",
    "imap_port": 993,
    "imap_username": "your@email.com",
    "imap_password": "app_password",
    "smtp_host": "smtp.gmail.com",
    "smtp_port": 587,
    "smtp_username": "your@email.com",
    "smtp_password": "app_password",
    "from_address": "your@email.com",
    "allow_from": ["sender@example.com"],
    "auto_reply_enabled": true
  }
}
```

**重要**: 使用 Gmail 需要创建应用专用密码

#### Mochat

```json
{
  "mochat": {
    "enabled": true,
    "base_url": "https://mochat.io",
    "socket_url": "https://mochat.io",
    "socket_path": "/socket.io",
    "claw_token": "xxx",
    "agent_user_id": "",
    "sessions": ["*"],
    "panels": ["*"]
  }
}
```

## 🎮 CLI 命令

### 基础命令

```bash
# 查看版本
iflow-bot version
iflow-bot -v

# 查看帮助
iflow-bot --help

# 查看状态
iflow-bot status

# 初始化配置
iflow-bot onboard [--force]

# 启动 Web 控制台
iflow-bot console --host 127.0.0.1 --port 8787
# 可选访问令牌
iflow-bot console --token your_token
```

### Gateway 服务管理

```bash
# 后台启动服务
iflow-bot gateway start

# 前台运行（调试模式）
iflow-bot gateway run

# 停止服务
iflow-bot gateway stop

# 重启服务
iflow-bot gateway restart
```

### 配置管理

```bash
# 显示配置
iflow-bot config --show

# 编辑配置
iflow-bot config -e

# 切换模型
iflow-bot model glm-5
iflow-bot model kimi-k2.5
iflow-bot model minimax-m2.5

# 思考模式
iflow-bot thinking on
iflow-bot thinking off
```

### 会话管理

```bash
# 列出所有会话
iflow-bot sessions

# 过滤渠道
iflow-bot sessions --channel telegram

# 过滤聊天ID
iflow-bot sessions --chat-id 123456

# 清除会话映射
iflow-bot sessions --clear
```

### 定时任务 (Cron)

```bash
# 列出任务
iflow-bot cron list [-a]

# 添加间隔任务
iflow-bot cron add -n "喝水提醒" -m "该喝水了！" -e 300 -d --channel telegram --to "123456"

# 添加一次性任务
iflow-bot cron add -n "会议提醒" -m "开会！" -a "2024-12-25T10:00:00" -d --channel telegram --to "123456"

# 添加 cron 表达式任务
iflow-bot cron add -n "早报" -m "发送早报" -c "0 9 * * *" -d --channel telegram --to "123456"

# 启用/禁用任务
iflow-bot cron enable <id>
iflow-bot cron disable <id>

# 立即执行任务
iflow-bot cron run <id>

# 移除任务
iflow-bot cron remove <id>
```

### iflow 命令透传

```bash
# iflow 基础透传
iflow-bot iflow --help
iflow-bot iflow -p "hello"

# MCP 命令
iflow-bot mcp --help

# Agent 命令
iflow-bot agent --help

# Workflow 命令
iflow-bot workflow --help

# Skill 命令
iflow-bot skill --help

# Commands 命令
iflow-bot commands --help
```

## 💬 聊天内指令

这些命令在聊天窗口内发送（Telegram/飞书/钉钉等）：

```text
/status
/new
/compact
/help
/cron list
/cron add --name <name> --message <text> (--every <秒> | --cron "<表达式>" | --at "<ISO时间>") [--tz <时区>] [--channel <渠道>] [--to <chat_id>] [--deliver true|false]
/cron delete <id>
/model set <name>
/language <en-US|zh-CN>
/skills find <关键词>
/skills add <slug>
/skills list
/skills remove <slug>
/skills update
/ralph "<prompt>"
/ralph answer <回复>
/ralph approve
/ralph status
/ralph stop
/ralph resume
```

`/status`
- 展示当前适配器模式、模型、流式开关、语言、workspace 路径、stdio session id、上下文 token 估算、压缩次数。

`/new`
- 开启新会话，并清空上一轮运行态会话上下文。

`/compact`
- 手动压缩当前 stdio 会话。

`/cron`
- `list`：查看全部任务。
- `add`：如果聊天里未显式传 `--channel` 和 `--to`，机器人会自动补齐当前渠道和 chat_id，且默认 `--deliver true`。
- `delete`：按 id 删除任务。

`/model set <name>`
- 持久化新的默认模型，对新会话生效。

`/language <en-US|zh-CN>`
- 将语言设置写入 `workspace/.iflow/settings.json`。
- 系统回复和命令输出都会遵循该语言。

`/skills`
- 基于 SkillHub CLI。
- `add` 会安装到 `workspace/skills`，随后自动同步到 `~/.iflow/skills`。
- `remove` 会从这两个位置一起移除。
- 如果本机没有 SkillHub CLI，机器人会先尝试自动安装。

## 🧠 Ralph 使用流程

`/ralph` 是一个带人工审核的长任务 loop，不会在你发出 prompt 后立刻执行。

1. `/ralph "<prompt>"`
   - 创建 Ralph run。
   - 如有需要会先提出澄清问题。
2. `/ralph answer <回复>`
   - 提交澄清问题答案。
   - 机器人会生成 `prd.json` 和 markdown 版 PRD。
3. 先在聊天里审核完整 PRD 输出，必要时也可直接查看保存下来的文件。
4. `/ralph approve`
   - 仅在审批通过后才正式开始执行。
5. `/ralph status`
   - 展示当前状态、run id、当前 story/pass、story id、子角色、当前阶段。
6. `/ralph stop`
   - 停止当前 run，并取消正在运行的 Ralph 子会话。
7. `/ralph resume`
   - 从上次保存的位置恢复最近一个未完成的 run。

Ralph 行为说明：
- 同一个聊天内同一时间只允许一个 Ralph run 执行。
- Gateway 重启后，会自动继续该聊天中未完成的 Ralph run。
- Ralph 使用独立的 stdio adapter，因此执行长任务时，`/help`、`/status` 这类普通命令仍可响应。
- PRD 预览过长时会自动分段发送，而不是直接截断。

## 🧰 SkillHub CLI（/skills 依赖）

`/skills` 使用 SkillHub CLI，如果缺失会自动安装。
也可手动安装：

```bash
curl -fsSL https://skillhub-1388575217.cos.ap-guangzhou.myqcloud.com/install/install.sh | bash -s -- --cli-only
```

## 📁 目录结构

```
~/.iflow-bot/
├── config.json                    # 机器人配置文件
├── gateway.log                    # Gateway 日志
├── gateway.pid                    # 后台运行 PID 文件
├── botpy.log                      # QQ SDK 日志（启用 QQ 时）
├── data/
│   ├── cron/
│   │   └── jobs.json              # 定时任务存储
│   ├── media/                     # 下载的媒体缓存
│   └── sessions/                  # 会话元数据缓存
└── workspace/
    ├── .iflow/
    │   └── settings.json          # 每个 workspace 的语言与 iflow 设置
    ├── AGENTS.md                  # Agent 指令
    ├── BOOTSTRAP.md               # 首次启动引导文件（可选）
    ├── HEARTBEAT.md               # 心跳提示词（可选）
    ├── IDENTITY.md                # 身份提示词（可选）
    ├── SOUL.md                    # 人格提示词（可选）
    ├── TOOLS.md                   # 工具策略提示词（可选）
    ├── USER.md                    # 用户资料提示词（可选）
    ├── channel/
    │   └── <channel>/<chat>-<date>.json  # 聊天记录归档
    ├── images/                    # 调用 iflow 前下载的入站图片
    ├── memory/
    │   └── MEMORY.md              # 长期记忆
    ├── project/                   # Ralph 任务产出的目标项目目录
    ├── ralph/
    │   └── <chat_id>/<run_id>/    # Ralph 的 PRD、状态、进度文件
    └── skills/                    # 已安装技能，并同步到 ~/.iflow/skills
```

`workspace/skills` 是技能安装的真实来源目录。
机器人会把它同步到 iflow CLI 的技能目录（Linux 下一般是 `~/.iflow/skills`），这样 Ralph 子会话也能加载同一批技能。

## 🔧 开发

### 流式输出支持

iflow-bot 支持实时流式输出，让用户看到 AI "打字"的过程。

**支持流式输出的渠道**：
| 渠道 | 流式方式 | 说明 |
|------|----------|------|
| Telegram | 编辑消息 | 实时编辑消息内容 |
| 钉钉 | AI Card | 使用钉钉卡片模板流式更新 |
| Discord | 编辑消息 | 实时编辑消息内容（计划中） |
| Slack | 编辑消息 | 实时编辑消息内容（计划中） |

**配置要求**：
- 需要使用 Stdio 模式（`driver.mode = "stdio"`）或 ACP 模式（`driver.mode = "acp"`）
- 钉钉需要额外配置 AI Card 模板

**流式输出缓冲机制**：
- 内容累积到 10-25 个字符（随机）时推送一次更新
- 避免过于频繁的 API 调用
- 确保最终消息包含所有内容

### Session 会话管理

iflow-bot 自动管理多用户会话，支持跨渠道对话上下文。

**Session 映射存储**：
- 存储位置：`~/.iflow-bot/session_mappings.json`
- 格式：`{渠道}:{聊天ID} -> {sessionId}`

**Session 恢复机制**：
- Gateway 重启后自动恢复会话
- Session 失效时自动创建新会话
- 支持通过 CLI 管理会话

```bash
# 查看所有会话
iflow-bot sessions

# 清除会话映射
iflow-bot sessions --clear
```

### 项目结构

```
iflow-bot/
├── iflow_bot/
│   ├── __init__.py
│   ├── __main__.py          # 入口点
│   ├── bus/                 # 消息总线
│   │   ├── events.py        # 事件定义
│   │   └── queue.py         # 消息队列
│   ├── channels/            # 渠道实现
│   │   ├── base.py          # 基类
│   │   ├── telegram.py
│   │   ├── discord.py
│   │   ├── slack.py
│   │   ├── feishu.py
│   │   ├── dingtalk.py
│   │   ├── qq.py
│   │   ├── whatsapp.py
│   │   ├── email.py
│   │   ├── mochat.py
│   │   └── manager.py       # 渠道管理器
│   ├── cli/                 # CLI 命令
│   │   └── commands.py
│   ├── config/              # 配置管理
│   │   ├── schema.py        # 配置模型
│   │   └── loader.py
│   ├── cron/                # 定时任务
│   │   ├── service.py
│   │   └── types.py
│   ├── engine/              # 核心引擎
│   │   ├── adapter.py       # iflow 适配器
│   │   ├── acp.py          # ACP 模式（WebSocket）
│   │   ├── stdio_acp.py    # Stdio 模式
│   │   └── loop.py          # 消息循环
│   ├── heartbeat/           # 心跳服务
│   │   └── service.py
│   ├── session/             # 会话管理
│   │   └── manager.py
│   ├── templates/           # 模板文件
│   │   ├── AGENTS.md
│   │   ├── SOUL.md
│   │   └── ...
│   └── utils/               # 工具函数
│       └── helpers.py
├── tests/
├── pyproject.toml
└── README.md
```


## 📝 工作空间模板

工作空间包含 AI 的"人格"和记忆：

- **SOUL.md** - 定义 AI 的核心人格和行为准则
- **USER.md** - 用户信息和偏好
- **AGENTS.md** - 工作空间行为指南
- **TOOLS.md** - 可用工具和配置
- **MEMORY.md** - 长期记忆（重要事件、决策）
- **memory/YYYY-MM-DD.md** - 日常记忆日志

## 🤝 贡献

欢迎提交 Issue 和 Pull Request！

## 📄 License

MIT

## 🙏 致谢

- [iflow CLI](https://cli.iflow.cn/) - 强大的 AI Agent CLI
- [nanobot](https://github.com/HKUDS/nanobot) - 轻量级 AI 机器人框架
