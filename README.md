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

**English** | [中文](README_CN.md)

**Multi-Channel AI Assistant** - A multi-platform messaging bot built on iflow CLI.

Extend the powerful AI capabilities of iflow to multiple communication platforms, making AI assistants accessible everywhere.

## ✨ Features

- 🔌 **Multi-Channel Support** - Telegram, Discord, Slack, Feishu, DingTalk, QQ, WhatsApp, Email, Mochat
- 🧠 **AI-Powered** - Built on iflow CLI, supporting multiple models (GLM-5, Kimi K2.5, MiniMax M2.5, etc.)
- 💾 **Session Management** - Automatic multi-user session management with conversation context support
- 📁 **Workspace** - Each bot instance has its own independent workspace and memory system
- 🔐 **Access Control** - Supports whitelist, mention trigger, and various other policies
- 🔄 **Thinking Mode** - Optional AI thinking process display
- ⚡ **Streaming Output** - Real-time streaming output support for Telegram and DingTalk AI Card
- 🚀 **Stdio Mode** - Direct communication with iflow via stdin/stdout for faster response

## 🎬 Demo

### Telegram Streaming Output

![Telegram Streaming Output Demo](https://github.com/kai648846760/iflow-bot/raw/master/testcase/Lark20260225-200437.gif)

### DingTalk AI Card Streaming Output

![DingTalk AI Card Streaming Output Demo](https://github.com/kai648846760/iflow-bot/raw/master/testcase/Lark20260225-200423.gif)

## 📋 Prerequisites

### 1. Install iflow CLI

iflow-bot depends on iflow CLI. Please install it first:

```bash
# With Node.js 22+
npm i -g @iflow-ai/iflow-cli@latest
```

### 2. Login to iflow

```bash
iflow
```
1. After running iflow, select "Login with iFlow"
2. CLI will automatically open browser to iFlow platform
3. Complete registration/login and authorize iFlow CLI
4. Return to terminal automatically and start using

Follow the prompts to complete the login process.

## 🚀 Quick Start

### Installation

**Option 1: pip install (Recommended)**

```bash
pip install iflow-bot
```

After installation, you can use it directly:

```bash
iflow-bot --help
iflow-bot onboard
iflow-bot gateway start
```

**Option 2: Install from Source**

```bash
# Clone repository
git clone https://github.com/your-repo/iflow-bot.git
cd iflow-bot

# Install dependencies (using uv)
uv sync
```

### Initialize Configuration

```bash
# Create default config file (pip install)
iflow-bot onboard

# Or from source
uv run iflow-bot onboard

# Or manually
mkdir -p ~/.iflow-bot
cp config.example.json ~/.iflow-bot/config.json
```

### Start Service

**After pip install:**

```bash
# Foreground (debug mode)
iflow-bot gateway run

# Background
iflow-bot gateway start

# Check status
iflow-bot status

# Stop service
iflow-bot gateway stop
```

**After source install:**

```bash
# Foreground (debug mode)
uv run iflow-bot gateway run

# Background
uv run iflow-bot gateway start
```

## 🐳 Docker Deployment

```bash
# Build image
docker build -t iflow-bot:latest .

# Prepare host config file
mkdir -p ./config
cp config/config.example.json ./config/config.json
```

Then edit `./config/config.json` to enable channels and tokens.

```bash
# Start with docker compose
docker compose up -d

# View logs
docker compose logs -f iflow-bot
```

## ⚙️ Configuration

Configuration file located at `~/.iflow-bot/config.json`

### Full Configuration Example

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

### Driver Configuration

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `mode` | string | `"stdio"` | Communication mode: `stdio` (recommended), `acp` (WebSocket), `cli` (subprocess) |
| `iflow_path` | string | `"iflow"` | iflow CLI path [keep default] |
| `model` | string | `"minimax-m2.5"` | Default model (glm-5, kimi-k2.5, minimax-m2.5, etc.) |
| `yolo` | bool | `true` | Auto-confirm mode |
| `thinking` | bool | `false` | Show AI thinking process |
| `max_turns` | int | `40` | Maximum conversation turns per session |
| `timeout` | int | `600` | Timeout in seconds |
| `workspace` | string | `~/.iflow-bot/workspace` | Workspace path |
| `extra_args` | list | `[]` | Additional iflow arguments |
| `acp_port` | int | `8090` | Port for ACP mode |
| `acp_host` | string | `"localhost"` | Host for ACP mode |

#### Communication Modes

**Stdio Mode (⭐ Recommended)**:
- Direct communication with iflow via stdin/stdout
- No need to start WebSocket service, faster startup
- Real-time streaming output, typewriter effect
- Lower response latency, close to native experience
- Gateway automatically executes `iflow --experimental-acp --stream` on startup

**ACP Mode (WebSocket)**:
- Communication with iflow via WebSocket
- Requires starting WebSocket server [auto-started]
- Real-time streaming output support
- Suitable for scenarios requiring remote connection

**CLI Mode**:
- Call iflow CLI via subprocess
- Each conversation starts independent process
- Suitable for simple scenarios or debugging

#### Recommended Configuration

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

### Channel Configuration

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

1. Create bot at [@BotFather](https://t.me/BotFather) to get Token
2. Empty `allow_from` allows all users

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

1. Create application at [Discord Developer Portal](https://discord.com/developers/applications)
2. Create Bot user and get Token
3. Enable Message Content Intent

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

1. Create application at [Slack API](https://api.slack.com/apps)
2. Create Bot and get Bot Token (`xoxb-xxx`)
3. Enable Socket Mode to get App Token (`xapp-xxx`)
4. `group_policy` controls channel message response strategy:
   - `mention`: Only respond to @mentions
   - `open`: Respond to all messages
   - `allowlist`: Only respond to whitelisted channels

#### Feishu/Lark

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

1. Create enterprise self-built app at [Feishu Open Platform](https://open.feishu.cn/)
2. Enable bot capability
3. Configure event subscription (uses WebSocket, no public IP required)

#### DingTalk

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

1. Create bot at [DingTalk Open Platform](https://open.dingtalk.com/)
2. Get Client ID and Client Secret
3. Enable Stream Mode (no public IP required)

**AI Card Streaming Output Configuration** (optional, for typewriter effect):

| Parameter | Description |
|-----------|-------------|
| `robot_code` | Robot code, required for group chats |
| `card_template_id` | AI Card template ID, create in DingTalk developer console |
| `card_template_key` | Template content field name, default `content` |

**Create AI Card Template**:
1. Login to [DingTalk Developer Console](https://open.dingtalk.com/)
2. Go to "Card Platform" → "Card Templates"
3. Create template, add a "Text" type field
4. Record template ID and field name, configure in `card_template_id` and `card_template_key`

**Streaming Output Effect**:
- Bot immediately replies with a blank card after user sends message
- Card content updates in real-time, typewriter effect
- No need to wait for complete response, smoother experience

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

1. Create bot at [QQ Open Platform](https://q.qq.com/)
2. Get App ID and Secret

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

Requires deploying [WhatsApp Bridge](https://github.com/your-repo/whatsapp-bridge) (based on baileys)

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

**Important**: Using Gmail requires creating an App Password

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

## 🎮 CLI Commands

### Basic Commands

```bash
# Show version
iflow-bot version
iflow-bot -v

# Show help
iflow-bot --help

# Check status
iflow-bot status

# Initialize config
iflow-bot onboard [--force]

# Start web console
iflow-bot console --host 127.0.0.1 --port 8787
# Optional access token
iflow-bot console --token your_token
```

### Gateway Service Management

```bash
# Start service in background
iflow-bot gateway start

# Run in foreground (debug mode)
iflow-bot gateway run

# Stop service
iflow-bot gateway stop

# Restart service
iflow-bot gateway restart
```

### Configuration Management

```bash
# Show config
iflow-bot config --show

# Edit config
iflow-bot config -e

# Switch model
iflow-bot model glm-5
iflow-bot model kimi-k2.5
iflow-bot model minimax-m2.5

# Thinking mode
iflow-bot thinking on
iflow-bot thinking off
```

### Session Management

```bash
# List all sessions
iflow-bot sessions

# Filter by channel
iflow-bot sessions --channel telegram

# Filter by chat ID
iflow-bot sessions --chat-id 123456

# Clear session mappings
iflow-bot sessions --clear
```

### Scheduled Tasks (Cron)

```bash
# List tasks
iflow-bot cron list [-a]

# Add interval task
iflow-bot cron add -n "Water reminder" -m "Time to drink water!" -e 300 -d --channel telegram --to "123456"

# Add one-time task
iflow-bot cron add -n "Meeting reminder" -m "Meeting time!" -a "2024-12-25T10:00:00" -d --channel telegram --to "123456"

# Add cron expression task
iflow-bot cron add -n "Morning report" -m "Send morning report" -c "0 9 * * *" -d --channel telegram --to "123456"

# Enable/disable task
iflow-bot cron enable <id>
iflow-bot cron disable <id>

# Run task immediately
iflow-bot cron run <id>

# Remove task
iflow-bot cron remove <id>
```

### iflow Command Passthrough

```bash
# iflow basic passthrough
iflow-bot iflow --help
iflow-bot iflow -p "hello"

# MCP commands
iflow-bot mcp --help

# Agent commands
iflow-bot agent --help

# Workflow commands
iflow-bot workflow --help

# Skill commands
iflow-bot skill --help

# Commands
iflow-bot commands --help
```

### Chat Slash Commands

These commands are sent inside your chat app (Telegram/Feishu/DingTalk/etc.):

```
/status                 Show status (model, session, compression count)
/new                    Start a new conversation
/compact                Manually compact current conversation
/help                   Show help
/cron list              List scheduled tasks
/cron add ...           Add a task (see /help for args)
/cron delete <id>       Delete a task
/model set <name>       Switch model for new sessions
/language <en-US|zh-CN> Set language
/skills find <keyword>  Search skills (SkillHub)
/skills add <slug>      Install skill to workspace/skills
/skills list            List installed skills
/skills remove <slug>   Uninstall skill
/skills update          Upgrade installed skills
```

### SkillHub CLI (for /skills)

`/skills` uses SkillHub CLI. If it is missing, the bot will auto-install it.
You can also install it manually:

```bash
curl -fsSL https://skillhub-1388575217.cos.ap-guangzhou.myqcloud.com/install/install.sh | bash -s -- --cli-only
```

## 📁 Directory Structure

```
~/.iflow-bot/
├── botpy.log                # QQ bot log
├── config.json              # Configuration file
├── gateway.pid              # PID file (background mode)
├── gateway.log              # Gateway log
├── session_mappings.json    # Session mappings
├── workspace/               # iflow workspace
│   ├── AGENTS.md            # Agent behavior guide
│   ├── BOOT.md              # Boot configuration
│   ├── HEARTBEAT.md         # Heartbeat tasks
│   ├── IDENTITY.md          # Identity
│   ├── SOUL.md              # AI personality definition
│   ├── TOOLS.md             # Tool configuration
│   ├── USER.md              # User info
│   └── memory/              # Memory directory
│       └── MEMORY.md        # Long-term memory
└── data/                    # Data directory
    └── cron/                # Scheduled tasks
        └── jobs.json        # Task data
```

## 🔧 Development

### Streaming Output Support

iflow-bot supports real-time streaming output, allowing users to see AI "typing".

**Channels with Streaming Support**:
| Channel | Method | Description |
|---------|--------|-------------|
| Telegram | Edit message | Real-time message content editing |
| DingTalk | AI Card | Streaming update using DingTalk card template |
| Discord | Edit message | Real-time message content editing (planned) |
| Slack | Edit message | Real-time message content editing (planned) |

**Configuration Requirements**:
- Use Stdio mode (`driver.mode = "stdio"`) or ACP mode (`driver.mode = "acp"`)
- DingTalk requires additional AI Card template configuration

**Streaming Output Buffer Mechanism**:
- Push update when content accumulates 10-25 characters (random)
- Avoid overly frequent API calls
- Ensure final message contains all content

### Session Management

iflow-bot automatically manages multi-user sessions with cross-channel conversation context support.

**Session Mapping Storage**:
- Location: `~/.iflow-bot/session_mappings.json`
- Format: `{channel}:{chat_id} -> {sessionId}`

**Session Recovery Mechanism**:
- Automatically restore sessions after Gateway restart
- Create new session when session expires
- Support session management via CLI

```bash
# View all sessions
iflow-bot sessions

# Clear session mappings
iflow-bot sessions --clear
```

### Project Structure

```
iflow-bot/
├── iflow_bot/
│   ├── __init__.py
│   ├── __main__.py          # Entry point
│   ├── bus/                 # Message bus
│   │   ├── events.py        # Event definitions
│   │   └── queue.py         # Message queue
│   ├── channels/            # Channel implementations
│   │   ├── base.py          # Base class
│   │   ├── telegram.py
│   │   ├── discord.py
│   │   ├── slack.py
│   │   ├── feishu.py
│   │   ├── dingtalk.py
│   │   ├── qq.py
│   │   ├── whatsapp.py
│   │   ├── email.py
│   │   ├── mochat.py
│   │   └── manager.py       # Channel manager
│   ├── cli/                 # CLI commands
│   │   └── commands.py
│   ├── config/              # Configuration management
│   │   ├── schema.py        # Configuration model
│   │   └── loader.py
│   ├── cron/                # Scheduled tasks
│   │   ├── service.py
│   │   └── types.py
│   ├── engine/              # Core engine
│   │   ├── adapter.py       # iflow adapter
│   │   ├── acp.py          # ACP mode (WebSocket)
│   │   ├── stdio_acp.py    # Stdio mode
│   │   └── loop.py          # Message loop
│   ├── heartbeat/           # Heartbeat service
│   │   └── service.py
│   ├── session/             # Session management
│   │   └── manager.py
│   ├── templates/           # Template files
│   │   ├── AGENTS.md
│   │   ├── SOUL.md
│   │   └── ...
│   └── utils/               # Utility functions
│       └── helpers.py
├── tests/
├── pyproject.toml
└── README.md
```

## 📝 Workspace Templates

Workspace contains AI's "personality" and memory:

- **SOUL.md** - Defines AI's core personality and behavior guidelines
- **USER.md** - User information and preferences
- **AGENTS.md** - Workspace behavior guide
- **TOOLS.md** - Available tools and configuration
- **MEMORY.md** - Long-term memory (important events, decisions)
- **memory/YYYY-MM-DD.md** - Daily memory logs

## 🤝 Contributing

Issues and Pull Requests are welcome!

## 📄 License

MIT

## 🙏 Acknowledgments

- [iflow CLI](https://cli.iflow.cn/) - Powerful AI Agent CLI
- [nanobot](https://github.com/HKUDS/nanobot) - Lightweight AI bot framework
