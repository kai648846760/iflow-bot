"""配置 schema for iflow-bot。

参考: https://platform.iflow.cn/cli/configuration/settings
"""

from pathlib import Path
from typing import Any, Literal, Optional
from pydantic import BaseModel, Field
from pydantic_settings import BaseSettings


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
    
    enabled: bool = False
    bot_token: str = ""
    app_token: str = ""
    allow_from: list[str] = Field(default_factory=list)
    group_policy: Literal["mention", "open", "allowlist"] = "mention"


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
    timeout: int = 300
    workspace: str = ""  # 关键：iflow 工作目录
    extra_args: list[str] = Field(default_factory=list)
    
    # ACP 模式配置
    acp_port: int = 8090
    """ACP 模式下的端口号"""
    
    acp_host: str = "localhost"
    """ACP 模式下的主机地址"""


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
        return 300