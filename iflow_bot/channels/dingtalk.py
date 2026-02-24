"""DingTalk (钉钉) channel implementation using Stream Mode.

使用 dingtalk-stream SDK 通过 WebSocket 接收消息，
使用 HTTP API 发送消息。无需公网 IP 或 webhook 配置。
"""

import asyncio
import json
import logging
import time
from typing import Any, Optional, Set

from iflow_bot.bus.events import OutboundMessage
from iflow_bot.bus.queue import MessageBus
from iflow_bot.channels.base import BaseChannel
from iflow_bot.channels.manager import register_channel
from iflow_bot.config.schema import DingTalkConfig

try:
    import httpx
    HTTPX_AVAILABLE = True
except ImportError:
    HTTPX_AVAILABLE = False
    httpx = None  # type: ignore

try:
    from dingtalk_stream import (
        DingTalkStreamClient,
        Credential,
        CallbackHandler,
        CallbackMessage,
        AckMessage,
    )
    from dingtalk_stream.chatbot import ChatbotMessage
    DINGTALK_AVAILABLE = True
except ImportError:
    DINGTALK_AVAILABLE = False
    # Fallback 避免模块级别定义崩溃
    CallbackHandler = object  # type: ignore
    CallbackMessage = None  # type: ignore
    AckMessage = None  # type: ignore
    ChatbotMessage = None  # type: ignore


logger = logging.getLogger(__name__)


class DingTalkHandler(CallbackHandler):
    """钉钉 Stream SDK 回调处理器。

    解析入站消息并转发给 DingTalkChannel。
    """

    def __init__(self, channel: "DingTalkChannel"):
        super().__init__()
        self.channel = channel

    async def process(self, message: CallbackMessage):
        """处理入站的 stream 消息。"""
        try:
            # 使用 SDK 的 ChatbotMessage 进行解析
            chatbot_msg = ChatbotMessage.from_dict(message.data)

            # 提取文本内容
            content = ""
            if chatbot_msg.text:
                content = chatbot_msg.text.content.strip()
            if not content:
                content = message.data.get("text", {}).get("content", "").strip()

            if not content:
                logger.warning(
                    f"[{self.channel.name}] Received empty or unsupported message type: "
                    f"{chatbot_msg.message_type}"
                )
                return AckMessage.STATUS_OK, "OK"

            sender_id = chatbot_msg.sender_staff_id or chatbot_msg.sender_id
            sender_name = chatbot_msg.sender_nick or "Unknown"

            logger.info(
                f"[{self.channel.name}] Received message from {sender_name} "
                f"({sender_id}): {content}"
            )

            # 转发到 Channel (非阻塞)
            task = asyncio.create_task(
                self.channel._on_message(content, sender_id, sender_name)
            )
            self.channel._background_tasks.add(task)
            task.add_done_callback(self.channel._background_tasks.discard)

            return AckMessage.STATUS_OK, "OK"

        except Exception as e:
            logger.error(f"[{self.channel.name}] Error processing message: {e}")
            # 返回 OK 避免钉钉服务器重试
            return AckMessage.STATUS_OK, "Error"


@register_channel("dingtalk")
class DingTalkChannel(BaseChannel):
    """钉钉 Channel - 使用 Stream Mode。

    通过 dingtalk-stream SDK 使用 WebSocket 接收事件。
    使用直接 HTTP API 发送消息。

    注意: 当前仅支持私聊 (1:1)。群聊消息会接收，但回复会作为私聊发送给发送者。

    要求:
    - client_id (AppKey)
    - client_secret (AppSecret)

    Attributes:
        name: 渠道名称 ("dingtalk")
        config: DingTalk 配置对象
        bus: 消息总线实例
        _client: DingTalk Stream 客户端
        _http: HTTP 客户端
        _access_token: Access Token 缓存
        _token_expiry: Token 过期时间
        _background_tasks: 后台任务集合
    """

    name = "dingtalk"

    def __init__(self, config: DingTalkConfig, bus: MessageBus):
        """初始化钉钉 Channel。

        Args:
            config: DingTalk 配置对象
            bus: 消息总线实例
        """
        super().__init__(config, bus)
        self.config: DingTalkConfig = config
        self._client: Any = None
        self._http: Optional[httpx.AsyncClient] = None

        # Access Token 管理
        self._access_token: Optional[str] = None
        self._token_expiry: float = 0

        # 后台任务引用 (防止 GC)
        self._background_tasks: Set[asyncio.Task] = set()

    async def start(self) -> None:
        """启动钉钉 Bot (Stream Mode)。"""
        if not DINGTALK_AVAILABLE:
            logger.error(
                f"[{self.name}] DingTalk Stream SDK not installed. "
                "Run: pip install dingtalk-stream"
            )
            return

        if not HTTPX_AVAILABLE:
            logger.error(
                f"[{self.name}] httpx not installed. Run: pip install httpx"
            )
            return

        if not self.config.client_id or not self.config.client_secret:
            logger.error(f"[{self.name}] client_id and client_secret not configured")
            return

        self._running = True
        self._http = httpx.AsyncClient()

        logger.info(
            f"[{self.name}] Initializing DingTalk Stream Client with Client ID: "
            f"{self.config.client_id[:8]}..."
        )

        credential = Credential(self.config.client_id, self.config.client_secret)
        self._client = DingTalkStreamClient(credential)

        # 注册回调处理器
        handler = DingTalkHandler(self)
        self._client.register_callback_handler(ChatbotMessage.TOPIC, handler)

        logger.info(f"[{self.name}] DingTalk bot started with Stream Mode")

        # 重连循环
        while self._running:
            try:
                await self._client.start()
            except Exception as e:
                logger.warning(f"[{self.name}] DingTalk stream error: {e}")
            if self._running:
                logger.info(f"[{self.name}] Reconnecting in 5 seconds...")
                await asyncio.sleep(5)

    async def stop(self) -> None:
        """停止钉钉 Bot。"""
        self._running = False

        # 关闭 HTTP 客户端
        if self._http:
            await self._http.aclose()
            self._http = None

        # 取消后台任务
        for task in self._background_tasks:
            task.cancel()
        self._background_tasks.clear()

        logger.info(f"[{self.name}] DingTalk bot stopped")

    async def _get_access_token(self) -> Optional[str]:
        """获取或刷新 Access Token。"""
        if self._access_token and time.time() < self._token_expiry:
            return self._access_token

        url = "https://api.dingtalk.com/v1.0/oauth2/accessToken"
        data = {
            "appKey": self.config.client_id,
            "appSecret": self.config.client_secret,
        }

        if not self._http:
            logger.warning(f"[{self.name}] HTTP client not initialized")
            return None

        try:
            resp = await self._http.post(url, json=data)
            resp.raise_for_status()
            res_data = resp.json()
            self._access_token = res_data.get("accessToken")
            # 提前 60 秒过期以确保安全
            self._token_expiry = time.time() + int(res_data.get("expireIn", 7200)) - 60
            return self._access_token
        except Exception as e:
            logger.error(f"[{self.name}] Failed to get access token: {e}")
            return None

    async def send(self, msg: OutboundMessage) -> None:
        """通过钉钉发送消息。

        Args:
            msg: 出站消息对象
                - chat_id: 用户 staffId
                - content: 消息内容 (支持 Markdown)
        """
        token = await self._get_access_token()
        if not token:
            return

        # oToMessages/batchSend: 发送私聊消息
        url = "https://api.dingtalk.com/v1.0/robot/oToMessages/batchSend"

        headers = {"x-acs-dingtalk-access-token": token}

        data = {
            "robotCode": self.config.client_id,
            "userIds": [msg.chat_id],  # chat_id 是用户的 staffId
            "msgKey": "sampleMarkdown",
            "msgParam": json.dumps({
                "text": msg.content,
                "title": "iFlow Bot Reply",
            }, ensure_ascii=False),
        }

        if not self._http:
            logger.warning(f"[{self.name}] HTTP client not initialized")
            return

        try:
            resp = await self._http.post(url, json=data, headers=headers)
            if resp.status_code != 200:
                logger.error(f"[{self.name}] Send failed: {resp.text}")
            else:
                logger.debug(f"[{self.name}] Message sent to {msg.chat_id}")
        except Exception as e:
            logger.error(f"[{self.name}] Error sending message: {e}")

    async def _on_message(
        self, content: str, sender_id: str, sender_name: str
    ) -> None:
        """处理入站消息 (由 DingTalkHandler 调用)。

        委托给 BaseChannel._handle_message()，会执行权限检查后发布到总线。

        Args:
            content: 消息内容
            sender_id: 发送者 ID
            sender_name: 发送者名称
        """
        try:
            logger.debug(f"[{self.name}] Inbound: {content} from {sender_name}")
            await self._handle_message(
                sender_id=sender_id,
                chat_id=sender_id,  # 私聊: chat_id == sender_id
                content=str(content),
                metadata={
                    "sender_name": sender_name,
                    "platform": "dingtalk",
                },
            )
        except Exception as e:
            logger.error(f"[{self.name}] Error publishing message: {e}")
