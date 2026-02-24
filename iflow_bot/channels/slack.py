"""Slack Channel 实现。

使用 slack-sdk 库实现 Slack Bot 功能，采用 Socket Mode（无需公网 IP）。
支持私聊和频道消息，支持 @mention 触发。
"""

import asyncio
import logging
import re
from typing import Any, Optional

from slack_sdk.socket_mode.aiohttp import SocketModeClient
from slack_sdk.socket_mode.request import SocketModeRequest
from slack_sdk.socket_mode.response import SocketModeResponse
from slack_sdk.web.async_client import AsyncWebClient

from iflow_bot.bus.events import InboundMessage, OutboundMessage
from iflow_bot.bus.queue import MessageBus
from iflow_bot.channels.base import BaseChannel
from iflow_bot.channels.manager import register_channel
from iflow_bot.config.schema import SlackConfig


logger = logging.getLogger(__name__)


# Slack 消息字符限制
SLACK_MAX_MESSAGE_LENGTH = 40000


def _markdown_to_slack(text: str) -> str:
    """
    Convert markdown to Slack-compatible format.

    Slack uses its own formatting:
    - *bold* for bold
    - _italic_ for italic
    - ~strikethrough~ for strikethrough
    - `code` for inline code
    - ```code block``` for code blocks
    - <url|text> for links
    """
    if not text:
        return ""

    # 1. Extract and protect code blocks
    code_blocks: list[str] = []

    def save_code_block(m: re.Match) -> str:
        code_blocks.append(m.group(1))
        return f"\x00CB{len(code_blocks) - 1}\x00"

    text = re.sub(r"```[\w]*\n?([\s\S]*?)```", save_code_block, text)

    # 2. Extract and protect inline code
    inline_codes: list[str] = []

    def save_inline_code(m: re.Match) -> str:
        inline_codes.append(m.group(1))
        return f"\x00IC{len(inline_codes) - 1}\x00"

    text = re.sub(r"`([^`]+)`", save_inline_code, text)

    # 3. Headers # Title -> *Title* (bold)
    text = re.sub(r"^#{1,6}\s+(.+)$", r"*\1*", text, flags=re.MULTILINE)

    # 4. Links [text](url) -> <url|text>
    text = re.sub(r"\[([^\]]+)\]\(([^)]+)\)", r"<\2|\1>", text)

    # 5. Bold **text** -> *text*
    text = re.sub(r"\*\*(.+?)\*\*", r"*\1*", text)

    # 6. Italic _text_ (Slack uses same syntax)
    # Keep as is, but avoid matching inside words

    # 7. Strikethrough ~~text~~ -> ~text~
    text = re.sub(r"~~(.+?)~~", r"~\1~", text)

    # 8. Bullet lists - keep as is
    # Slack supports - and * for bullets

    # 9. Restore inline code
    for i, code in enumerate(inline_codes):
        text = text.replace(f"\x00IC{i}\x00", f"`{code}`")

    # 10. Restore code blocks
    for i, code in enumerate(code_blocks):
        text = text.replace(f"\x00CB{i}\x00", f"```\n{code}\n```")

    return text


def _split_message(content: str, max_len: int = SLACK_MAX_MESSAGE_LENGTH) -> list[str]:
    """Split content into chunks within max_len, preferring line breaks."""
    if len(content) <= max_len:
        return [content]

    chunks: list[str] = []
    while content:
        if len(content) <= max_len:
            chunks.append(content)
            break
        cut = content[:max_len]
        pos = cut.rfind("\n")
        if pos == -1:
            pos = cut.rfind(" ")
        if pos == -1:
            pos = max_len
        chunks.append(content[:pos])
        content = content[pos:].lstrip()

    return chunks


@register_channel("slack")
class SlackChannel(BaseChannel):
    """Slack Channel 实现。

    使用 slack-sdk 库的 Socket Mode 连接 Slack，无需公网 IP。
    支持：
    - 私聊 (DM) 和频道消息
    - @mention 触发（频道中需要 @ 机器人）
    - 权限检查 (allow_from 白名单)
    - group_policy: mention/open/allowlist 控制频道消息响应策略

    Attributes:
        name: 渠道名称 ("slack")
        config: Slack 配置对象
        bus: 消息总线实例
        client: Socket Mode 客户端
        web_client: Web API 客户端
    """

    name = "slack"

    def __init__(self, config: SlackConfig, bus: MessageBus):
        """初始化 Slack Channel。

        Args:
            config: Slack 配置对象
            bus: 消息总线实例
        """
        super().__init__(config, bus)
        self.config: SlackConfig = config
        self.client: Optional[SocketModeClient] = None
        self.web_client: Optional[AsyncWebClient] = None
        self._bot_user_id: Optional[str] = None
        self._ready_event = asyncio.Event()

    async def start(self) -> None:
        """启动 Slack Bot。

        创建 Socket Mode 客户端并连接到 Slack。
        Socket Mode 不需要公网 IP，通过 WebSocket 接收事件。
        """
        if not self.config.bot_token or not self.config.app_token:
            logger.error("Slack bot_token or app_token not configured")
            return

        # 创建 Web API 客户端
        self.web_client = AsyncWebClient(token=self.config.bot_token)

        # 创建 Socket Mode 客户端
        self.client = SocketModeClient(
            app_token=self.config.app_token,
            web_client=self.web_client,
        )

        logger.info(f"[{self.name}] Starting Slack bot (Socket Mode)...")

        # 注册事件处理器
        self.client.socket_mode_request_listeners.append(self._on_socket_mode_request)

        # 启动客户端
        try:
            await self.client.connect()
        except Exception as e:
            logger.error(f"[{self.name}] Failed to connect to Slack: {e}")
            return

        # 获取 Bot 用户 ID
        try:
            auth_result = await self.web_client.auth_test()
            self._bot_user_id = auth_result["user_id"]
            logger.info(
                f"[{self.name}] Logged in as {auth_result['user']} "
                f"(Bot ID: {self._bot_user_id})"
            )
        except Exception as e:
            logger.error(f"[{self.name}] Failed to get bot info: {e}")
            await self.client.close()
            return

        self._running = True
        logger.info(f"[{self.name}] Slack bot started successfully")

    async def stop(self) -> None:
        """停止 Slack Bot。

        关闭 Socket Mode 客户端连接。
        """
        logger.info(f"[{self.name}] Stopping Slack bot...")
        self._running = False

        if self.client:
            await self.client.close()
            self.client = None

        self.web_client = None
        logger.info(f"[{self.name}] Slack bot stopped")

    async def send(self, msg: OutboundMessage) -> None:
        """发送消息到 Slack。

        支持私聊和频道消息，自动处理长消息分片。
        支持 text 和 blocks 格式。

        Args:
            msg: 出站消息对象
                - chat_id: Slack 用户 ID (私聊) 或 频道 ID
                - content: 消息内容
                - metadata: 可包含 blocks、thread_ts 等
        """
        if not self.web_client or not self._running:
            logger.warning(f"[{self.name}] Client not ready, cannot send message")
            return

        try:
            # 构建消息参数
            kwargs: dict[str, Any] = {
                "channel": msg.chat_id,
            }

            # 处理回复线程
            thread_ts = msg.reply_to_id or msg.metadata.get("thread_ts")
            if thread_ts:
                kwargs["thread_ts"] = thread_ts

            # 检查是否有 blocks 配置
            blocks = msg.metadata.get("blocks")
            if blocks:
                kwargs["blocks"] = blocks
                # 同时设置 text 作为后备
                kwargs["text"] = msg.content[:3000] if msg.content else ""
            else:
                # 转换 Markdown 为 Slack 格式
                slack_text = _markdown_to_slack(msg.content)
                kwargs["text"] = slack_text

            # 检查是否需要 unfurl_links
            if msg.metadata.get("unfurl_links") is False:
                kwargs["unfurl_links"] = False
                kwargs["unfurl_media"] = False

            # 发送消息 (处理分片)
            if blocks or len(kwargs.get("text", "")) <= SLACK_MAX_MESSAGE_LENGTH:
                await self.web_client.chat_postMessage(**kwargs)
            else:
                # 分片发送纯文本
                for chunk in _split_message(kwargs["text"]):
                    await self.web_client.chat_postMessage(
                        channel=msg.chat_id,
                        text=chunk,
                        thread_ts=thread_ts,
                    )

            logger.debug(f"[{self.name}] Message sent to {msg.chat_id}")

        except Exception as e:
            logger.error(f"[{self.name}] Failed to send message: {e}")

    async def _on_socket_mode_request(
        self, client: SocketModeClient, request: SocketModeRequest
    ) -> None:
        """处理 Socket Mode 请求。

        处理各类 Slack 事件，主要是消息事件。

        Args:
            client: Socket Mode 客户端
            request: Socket Mode 请求对象
        """
        # 先确认请求
        response = SocketModeResponse(envelope_id=request.envelope_id)
        await client.send_socket_mode_response(response)

        # 处理事件
        if request.type == "events_api":
            event = request.payload.get("event", {})
            event_type = event.get("type")

            if event_type == "message":
                await self._handle_message_event(event)
            elif event_type == "app_mention":
                await self._handle_mention_event(event)

    async def _handle_message_event(self, event: dict[str, Any]) -> None:
        """处理消息事件。

        处理私聊消息和频道消息，根据 group_policy 决定是否响应。

        Args:
            event: Slack 消息事件
        """
        # 忽略 Bot 消息
        if event.get("bot_id") or event.get("subtype") == "bot_message":
            return

        # 忽略消息编辑、删除等子类型
        subtype = event.get("subtype")
        if subtype and subtype not in ("message_changed", "thread_broadcast"):
            return

        # 获取用户信息
        user_id = event.get("user")
        if not user_id:
            return

        # 获取频道信息
        channel_id = event.get("channel")
        channel_type = event.get("channel_type")

        # 判断是否为私聊
        is_dm = channel_type == "im"

        # 获取消息内容
        text = event.get("text", "")

        # 移除消息中的 @mention（如果是自己）
        if self._bot_user_id:
            text = re.sub(
                rf"<@{self._bot_user_id}>",
                "",
                text
            ).strip()

        # 检查是否应该响应
        if not is_dm:
            # 频道消息，检查 group_policy
            if not self._should_respond_in_channel(event, text):
                return

        # 提取发送者 ID
        sender_id = await self._build_sender_id(user_id)

        # 提取媒体附件
        media = []
        files = event.get("files", [])
        for file_info in files:
            if url := file_info.get("url_private"):
                media.append(url)

        # 构建元数据
        metadata: dict[str, Any] = {
            "message_ts": event.get("ts"),
            "thread_ts": event.get("thread_ts"),
            "channel_id": channel_id,
            "channel_type": channel_type,
            "user_id": user_id,
            "is_dm": is_dm,
        }

        # 调用基类的消息处理方法
        await self._handle_message(
            sender_id=sender_id,
            chat_id=channel_id,
            content=text,
            media=media,
            metadata=metadata,
        )

    async def _handle_mention_event(self, event: dict[str, Any]) -> None:
        """处理 @mention 事件。

        当用户在频道中 @机器人时触发此事件。

        Args:
            event: Slack app_mention 事件
        """
        user_id = event.get("user")
        channel_id = event.get("channel")
        text = event.get("text", "")

        if not user_id or not channel_id:
            return

        # 移除 @mention
        if self._bot_user_id:
            text = re.sub(
                rf"<@{self._bot_user_id}>",
                "",
                text
            ).strip()

        # 提取发送者 ID
        sender_id = await self._build_sender_id(user_id)

        # 构建元数据
        metadata: dict[str, Any] = {
            "message_ts": event.get("ts"),
            "thread_ts": event.get("thread_ts"),
            "channel_id": channel_id,
            "user_id": user_id,
            "is_dm": False,
            "is_mention": True,
        }

        # 调用基类的消息处理方法
        await self._handle_message(
            sender_id=sender_id,
            chat_id=channel_id,
            content=text,
            metadata=metadata,
        )

    def _should_respond_in_channel(self, event: dict[str, Any], text: str) -> bool:
        """检查是否应该在频道中响应消息。

        根据 group_policy 配置决定响应策略：
        - "mention": 只响应 @mention 消息（默认）
        - "open": 响应所有消息
        - "allowlist": 只响应 allow_from 中允许的频道

        Args:
            event: 消息事件
            text: 消息文本（已移除 @mention）

        Returns:
            是否应该响应
        """
        policy = self.config.group_policy
        channel_id = event.get("channel", "")

        if policy == "mention":
            # 只响应包含 @mention 的消息
            # 如果 text 为空（可能是因为移除了 @mention），说明是 mention
            # 检查原始消息是否有 @bot
            original_text = event.get("text", "")
            if self._bot_user_id and f"<@{self._bot_user_id}>" in original_text:
                return True
            return False

        elif policy == "open":
            # 响应所有消息
            return True

        elif policy == "allowlist":
            # 检查频道是否在白名单中
            allow_from = self.config.allow_from
            if not allow_from:
                return True  # 未配置白名单则允许所有
            return channel_id in allow_from

        return False

    async def _build_sender_id(self, user_id: str) -> str:
        """构建发送者 ID，包含用户名以便权限匹配。

        格式: "user_id|username" 或 "user_id"

        Args:
            user_id: Slack 用户 ID

        Returns:
            发送者 ID 字符串
        """
        try:
            if self.web_client:
                user_info = await self.web_client.users_info(user=user_id)
                user = user_info.get("user", {})
                username = user.get("name", "")
                if username:
                    return f"{user_id}|{username}"
        except Exception as e:
            logger.debug(f"Failed to get user info for {user_id}: {e}")

        return user_id
