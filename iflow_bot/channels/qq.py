"""QQ channel implementation using qq-botpy SDK.

使用 qq-botpy SDK 通过 WebSocket 连接 QQ 频道机器人。
支持 C2C 私聊消息和频道消息。
"""

import asyncio
import logging
from collections import deque
from typing import Any, Optional, TYPE_CHECKING

from iflow_bot.bus.events import OutboundMessage
from iflow_bot.bus.queue import MessageBus
from iflow_bot.channels.base import BaseChannel
from iflow_bot.channels.manager import register_channel
from iflow_bot.config.schema import QQConfig

try:
    import botpy
    from botpy.message import C2CMessage
    QQ_AVAILABLE = True
except ImportError:
    QQ_AVAILABLE = False
    botpy = None  # type: ignore
    C2CMessage = None  # type: ignore

if TYPE_CHECKING:
    from botpy.message import C2CMessage


logger = logging.getLogger(__name__)


def _make_bot_class(channel: "QQChannel") -> Any:
    """创建绑定到指定 Channel 的 botpy.Client 子类。"""
    intents = botpy.Intents(public_messages=True, direct_message=True)

    class _Bot(botpy.Client):
        def __init__(self):
            super().__init__(intents=intents)

        async def on_ready(self):
            logger.info(f"[{channel.name}] QQ bot ready: {self.robot.name}")

        async def on_c2c_message_create(self, message: "C2CMessage"):
            await channel._on_message(message)

        async def on_direct_message_create(self, message):
            await channel._on_message(message)

    return _Bot


@register_channel("qq")
class QQChannel(BaseChannel):
    """QQ Channel - 使用 qq-botpy SDK 通过 WebSocket 连接。

    支持:
    - C2C 私聊消息
    - 频道消息 (通过 public_intents)

    要求:
    - app_id: QQ 机器人 AppID
    - secret: QQ 机器人 Secret

    Attributes:
        name: 渠道名称 ("qq")
        config: QQ 配置对象
        bus: 消息总线实例
        _client: botpy Client 实例
        _processed_ids: 已处理消息 ID 队列 (去重)
    """

    name = "qq"

    def __init__(self, config: QQConfig, bus: MessageBus):
        """初始化 QQ Channel。

        Args:
            config: QQ 配置对象
            bus: 消息总线实例
        """
        super().__init__(config, bus)
        self.config: QQConfig = config
        self._client: Any = None
        self._processed_ids: deque = deque(maxlen=1000)

    async def start(self) -> None:
        """启动 QQ Bot。"""
        if not QQ_AVAILABLE:
            logger.error(
                f"[{self.name}] QQ SDK not installed. Run: pip install qq-botpy"
            )
            return

        if not self.config.app_id or not self.config.secret:
            logger.error(f"[{self.name}] app_id and secret not configured")
            return

        self._running = True
        BotClass = _make_bot_class(self)
        self._client = BotClass()

        logger.info(f"[{self.name}] QQ bot started (C2C private message)")
        await self._run_bot()

    async def _run_bot(self) -> None:
        """运行 Bot 连接，支持自动重连。"""
        while self._running:
            try:
                await self._client.start(
                    appid=self.config.app_id,
                    secret=self.config.secret
                )
            except Exception as e:
                logger.warning(f"[{self.name}] QQ bot error: {e}")
            if self._running:
                logger.info(f"[{self.name}] Reconnecting in 5 seconds...")
                await asyncio.sleep(5)

    async def stop(self) -> None:
        """停止 QQ Bot。"""
        self._running = False
        if self._client:
            try:
                await self._client.close()
            except Exception:
                pass
        logger.info(f"[{self.name}] QQ bot stopped")

    async def send(self, msg: OutboundMessage) -> None:
        """通过 QQ 发送消息。

        Args:
            msg: 出站消息对象
                - chat_id: 用户 openid
                - content: 消息内容
        """
        if not self._client:
            logger.warning(f"[{self.name}] QQ client not initialized")
            return

        try:
            await self._client.api.post_c2c_message(
                openid=msg.chat_id,
                msg_type=0,  # 文本消息
                content=msg.content,
            )
            logger.debug(f"[{self.name}] Message sent to {msg.chat_id}")
        except Exception as e:
            logger.error(f"[{self.name}] Error sending message: {e}")

    async def _on_message(self, data: "C2CMessage") -> None:
        """处理来自 QQ 的入站消息。

        Args:
            data: QQ 消息对象
        """
        try:
            # 消息 ID 去重
            if data.id in self._processed_ids:
                return
            self._processed_ids.append(data.id)

            # 提取用户信息
            author = data.author
            user_id = str(
                getattr(author, 'id', None) or
                getattr(author, 'user_openid', 'unknown')
            )

            # 提取消息内容
            content = (data.content or "").strip()
            if not content:
                return

            # 先发送 "Thinking..." 提示（非阻塞，不影响主流程）
            try:
                if self._client:
                    await self._client.api.post_c2c_message(
                        openid=user_id,
                        msg_type=0,  # 文本消息
                        content="🤔 Thinking...",
                    )
            except Exception as e:
                logger.debug(f"[{self.name}] Failed to send thinking: {e}")

            # 转发到消息总线
            await self._handle_message(
                sender_id=user_id,
                chat_id=user_id,  # 私聊: chat_id == user_id
                content=content,
                metadata={"message_id": data.id},
            )

        except Exception:
            logger.exception(f"[{self.name}] Error handling QQ message")
