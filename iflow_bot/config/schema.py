"""配置 schema for iflow-bot。

参考: https://platform.iflow.cn/cli/configuration/settings
"""

from pathlib import Path
from typing import Any, Literal, Optional
from pydantic import BaseModel, Field
from pydantic_settings import BaseSettings

# 导入统一的超时常量，避免循环导入问题
def _get_default_timeout() -> int:
    """延迟导入避免循环依赖。"""
    from iflow_bot.config.loader import DEFAULT_TIMEOUT
    return DEFAULT_TIMEOUT


# ============================================================================
# 渠道配置
# ============================================================================

class TelegramConfig(BaseModel):
    model_config = {"extra": "ignore"}
    
    enabled: bool = False
    token: str = ""
    allow_from: list[str] = Field(default_factory=list)


class DiscordConfig(BaseModel):
    model_config = {"extra": "ignore"}
    
    enabled: bool = False
    token: str = ""
    allow_from: list[str] = Field(default_factory=list)


class WhatsAppConfig(BaseModel):
    model_config = {"extra": "ignore"}
    
    enabled: bool = False
    bridge_url: str = "http://localhost:3001"
    bridge_token: str = ""
    allow_from: list[str] = Field(default_factory=list)


class FeishuConfig(BaseModel):
    model_config = {"extra": "ignore"}
    
    enabled: bool = False
    app_id: str = ""
    app_secret: str = ""
    encrypt_key: str = ""
    verification_token: str = ""
    allow_from: list[str] = Field(default_factory=list)


class SlackConfig(BaseModel):
    model_config = {"extra": "ignore"}

    class DMConfig(BaseModel):
        model_config = {"extra": "ignore"}

        enabled: bool = True
        policy: Literal["open", "allowlist"] = "open"
        allow_from: list[str] = Field(default_factory=list)

    enabled: bool = False
    bot_token: str = ""
    app_token: str = ""
    allow_from: list[str] = Field(default_factory=list)
    group_policy: Literal["mention", "open", "allowlist"] = "mention"
    group_allow_from: list[str] = Field(default_factory=list)
    reply_in_thread: bool = True
    react_emoji: str = "eyes"
    dm: DMConfig = Field(default_factory=DMConfig)


class DingTalkConfig(BaseModel):
    model_config = {"extra": "ignore"}
    
    enabled: bool = False
    client_id: str = ""
    client_secret: str = ""
    robot_code: str = ""  # 机器人代码（群聊需要）
    card_template_id: str = ""  # AI Card 模板 ID（流式输出需要）
    card_template_key: str = "content"  # AI Card 内容字段名
    allow_from: list[str] = Field(default_factory=list)


class QQConfig(BaseModel):
    model_config = {"extra": "ignore"}

    enabled: bool = False
    app_id: str = ""
    secret: str = ""
    allow_from: list[str] = Field(default_factory=list)
    split_threshold: int = 3
    """流式分段发送阈值（基于换行符数量）。

    - 0: 不分段，等 AI 全部输出完后一次性发送
    - N > 0: 流式接收时每累积 N 个换行符立即推送一条新 QQ 消息，剩余内容在结束时补发
    """
    groups: list[str] = Field(default_factory=list)
    """允许加入的 QQ 群号列表（guild_id）"""
    markdown_support: bool = False
    """是否启用 Markdown 消息格式（默认 False）。

    启用前需在 QQ 开放平台申请 Markdown 消息权限。
    启用后消息格式为 { markdown: { content }, msg_type: 2 }。
    禁用时使用纯文本格式 { content, msg_type: 0 }。
    """


class EmailConfig(BaseModel):
    model_config = {"extra": "ignore"}
    
    enabled: bool = False
    consent_granted: bool = False
    imap_host: str = ""
    imap_port: int = 993
    imap_username: str = ""
    imap_password: str = ""
    smtp_host: str = ""
    smtp_port: int = 587
    smtp_username: str = ""
    smtp_password: str = ""
    from_address: str = ""
    allow_from: list[str] = Field(default_factory=list)
    auto_reply_enabled: bool = True


class MochatConfig(BaseModel):
    model_config = {"extra": "ignore"}
    
    enabled: bool = False
    base_url: str = "https://mochat.io"
    socket_url: str = "https://mochat.io"
    socket_path: str = "/socket.io"
    claw_token: str = ""
    agent_user_id: str = ""
    sessions: list[str] = Field(default_factory=lambda: ["*"])
    panels: list[str] = Field(default_factory=lambda: ["*"])


class ChannelsConfig(BaseModel):
    model_config = {"extra": "ignore"}
    
    telegram: TelegramConfig = Field(default_factory=TelegramConfig)
    discord: DiscordConfig = Field(default_factory=DiscordConfig)
    whatsapp: WhatsAppConfig = Field(default_factory=WhatsAppConfig)
    feishu: FeishuConfig = Field(default_factory=FeishuConfig)
    slack: SlackConfig = Field(default_factory=SlackConfig)
    dingtalk: DingTalkConfig = Field(default_factory=DingTalkConfig)
    qq: QQConfig = Field(default_factory=QQConfig)
    email: EmailConfig = Field(default_factory=EmailConfig)
    mochat: MochatConfig = Field(default_factory=MochatConfig)
    
    send_progress: bool = True
    send_tool_hints: bool = True


# ============================================================================
# Driver 配置（iflow 设置）
# ============================================================================

class DriverConfig(BaseModel):
    """IFlow driver 配置。
    
    参考: https://platform.iflow.cn/cli/configuration/settings
    """
    model_config = {"extra": "ignore"}
    
    mode: Literal["cli", "acp", "stdio"] = "stdio"
    """通信模式: cli (子进程调用), acp (WebSocket), 或 stdio (直接通过 stdin/stdout)"""
    
    iflow_path: str = "iflow"
    model: str = "minimax-m2.5"
    yolo: bool = True
    thinking: bool = False
    max_turns: int = 40
    timeout: int = Field(default_factory=_get_default_timeout)
    compression_trigger_tokens: int = 60000
    """活跃会话自动压缩触发阈值（估算 token）"""
    workspace: str = ""  # 关键：iflow 工作目录
    extra_args: list[str] = Field(default_factory=list)
    
    # ACP 模式配置
    acp_port: int = 8090
    """ACP 模式下的端口号"""
    
    acp_host: str = "localhost"
    """ACP 模式下的主机地址"""
    
    disable_mcp: bool = False
    """是否禁用 MCP 服务器（减少资源消耗）"""
    
    # MCP 代理配置
    mcp_proxy_enabled: bool = True
    """是否启用 MCP 代理（共享 MCP 服务器以减少资源消耗）"""

    mcp_proxy_port: int = 8888
    """MCP 代理服务器的端口号"""

    mcp_proxy_auto_start: bool = True
    """是否在启动网关时自动启动 MCP 代理"""

    mcp_servers_auto_discover: bool = True
    """是否自动从 MCP 代理发现启用的服务器（替代硬编码）"""

    mcp_servers_max: int = 10
    """单个 iflow 实例最多连接的 MCP 服务器数量（防止资源耗尽）"""

    mcp_servers_allowlist: list[str] = Field(default_factory=list)
    """允许使用的 MCP 服务器名称列表（空表示使用代理中的所有服务器）"""

    mcp_servers_blocklist: list[str] = Field(default_factory=list)
    """禁用的 MCP 服务器名称列表（优先级高于 allowlist）"""


# ============================================================================
# 主配置
# ============================================================================

class Config(BaseSettings):
    """iflow-bot 主配置。

    配置统一放在 driver 下，避免重复字段。
    """

    model_config = {
        "env_prefix": "IFLOW_BOT_",
        "env_nested_delimiter": "__",
        "extra": "ignore",
    }

    # Driver 配置（包含 model, workspace, timeout 等）
    driver: DriverConfig = Field(default_factory=DriverConfig)

    # 渠道配置
    channels: ChannelsConfig = Field(default_factory=ChannelsConfig)

    # 日志
    log_level: str = "INFO"
    log_file: str = ""

    def get_enabled_channels(self) -> list[str]:
        """获取已启用的渠道列表。"""
        enabled = []
        for name in ["telegram", "discord", "whatsapp", "feishu", "slack",
                     "dingtalk", "qq", "email", "mochat"]:
            channel = getattr(self.channels, name, None)
            if channel and getattr(channel, "enabled", False):
                enabled.append(name)
        return enabled

    def get_workspace(self) -> str:
        """获取 workspace 路径。

        优先使用 driver.workspace，默认为 ~/.iflow-bot/workspace
        """
        if self.driver and self.driver.workspace:
            return self.driver.workspace
        return str(Path.home() / ".iflow-bot" / "workspace")

    def get_model(self) -> str:
        """获取模型名称。

        优先使用 driver.model，默认为 glm-5
        """
        if self.driver and self.driver.model:
            return self.driver.model
        return "glm-5"

    def get_timeout(self) -> int:
        """获取超时时间。"""
        if self.driver and self.driver.timeout:
            return self.driver.timeout
        return _get_default_timeout()
