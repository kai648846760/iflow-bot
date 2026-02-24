"""Channel implementations for iflow-bot."""

from iflow_bot.channels.base import BaseChannel
from iflow_bot.channels.manager import ChannelManager, register_channel, get_channel_class

# Channel implementations
from iflow_bot.channels.telegram import TelegramChannel
from iflow_bot.channels.discord import DiscordChannel
from iflow_bot.channels.feishu import FeishuChannel
from iflow_bot.channels.slack import SlackChannel
from iflow_bot.channels.whatsapp import WhatsAppChannel
from iflow_bot.channels.dingtalk import DingTalkChannel
from iflow_bot.channels.qq import QQChannel
from iflow_bot.channels.email import EmailChannel
from iflow_bot.channels.mochat import MochatChannel

__all__ = [
    # Base
    "BaseChannel",
    "ChannelManager",
    "register_channel",
    "get_channel_class",
    # Implementations
    "TelegramChannel",
    "DiscordChannel",
    "FeishuChannel",
    "SlackChannel",
    "WhatsAppChannel",
    "DingTalkChannel",
    "QQChannel",
    "EmailChannel",
    "MochatChannel",
]