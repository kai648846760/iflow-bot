"""WhatsApp channel implementation using Node.js bridge.

使用 Node.js bridge 连接 WhatsApp，通过 WebSocket 进行通信。
Bridge 使用 @whiskeysockets/baileys 处理 WhatsApp Web 协议。
"""

import asyncio
import json
import logging
from typing import Any, Optional

from iflow_bot.bus.events import OutboundMessage
from iflow_bot.bus.queue import MessageBus
from iflow_bot.channels.base import BaseChannel
from iflow_bot.channels.manager import register_channel
from iflow_bot.config.schema import WhatsAppConfig

try:
    import websockets
    WEBSOCKETS_AVAILABLE = True
except ImportError:
    WEBSOCKETS_AVAILABLE = False
    websockets = None  # type: ignore


logger = logging.getLogger(__name__)


@register_channel("whatsapp")
class WhatsAppChannel(BaseChannel):
    """WhatsApp Channel - 通过 Node.js bridge 连接 WhatsApp。

    使用 WebSocket 连接到 Node.js bridge，bridge 负责处理 WhatsApp Web 协议。
    无需公网 IP 或 webhook 配置。

    要求:
    - Node.js bridge 运行在 bridge_url
    - bridge_token (可选) 用于认证

    Attributes:
        name: 渠道名称 ("whatsapp")
        config: WhatsApp 配置对象
        bus: 消息总线实例
        _ws: WebSocket 连接实例
        _connected: 连接状态标志
    """

    name = "whatsapp"

    def __init__(self, config: WhatsAppConfig, bus: MessageBus):
        """初始化 WhatsApp Channel。

        Args:
            config: WhatsApp 配置对象
            bus: 消息总线实例
        """
        super().__init__(config, bus)
        self.config: WhatsAppConfig = config
        self._ws: Any = None
        self._connected = False

    async def start(self) -> None:
        """启动 WhatsApp Channel，连接到 bridge。"""
        if not WEBSOCKETS_AVAILABLE:
            logger.error("websockets library not installed. Run: pip install websockets")
            return

        if not self.config.bridge_url:
            logger.error("WhatsApp bridge_url not configured")
            return

        bridge_url = self.config.bridge_url
        logger.info(f"[{self.name}] Connecting to WhatsApp bridge at {bridge_url}...")

        self._running = True

        while self._running:
            try:
                async with websockets.connect(bridge_url) as ws:
                    self._ws = ws
                    # 发送认证 token (如果配置)
                    if self.config.bridge_token:
                        await ws.send(json.dumps({
                            "type": "auth",
                            "token": self.config.bridge_token
                        }))

                    self._connected = True
                    logger.info(f"[{self.name}] Connected to WhatsApp bridge")

                    # 监听消息
                    async for message in ws:
                        try:
                            await self._handle_bridge_message(message)
                        except Exception as e:
                            logger.error(f"[{self.name}] Error handling bridge message: {e}")

            except asyncio.CancelledError:
                break
            except Exception as e:
                self._connected = False
                self._ws = None
                logger.warning(f"[{self.name}] WhatsApp bridge connection error: {e}")

                if self._running:
                    logger.info(f"[{self.name}] Reconnecting in 5 seconds...")
                    await asyncio.sleep(5)

        logger.info(f"[{self.name}] WhatsApp channel stopped")

    async def stop(self) -> None:
        """停止 WhatsApp Channel。"""
        self._running = False
        self._connected = False

        if self._ws:
            try:
                await self._ws.close()
            except Exception:
                pass
            self._ws = None

        logger.info(f"[{self.name}] WhatsApp channel stopped")

    async def send(self, msg: OutboundMessage) -> None:
        """通过 WhatsApp 发送消息。

        Args:
            msg: 出站消息对象
                - chat_id: WhatsApp 用户 ID (格式: <phone>@s.whatsapp.net 或 LID)
                - content: 消息内容
        """
        if not self._ws or not self._connected:
            logger.warning(f"[{self.name}] WhatsApp bridge not connected")
            return

        try:
            payload = {
                "type": "send",
                "to": msg.chat_id,
                "text": msg.content
            }
            await self._ws.send(json.dumps(payload, ensure_ascii=False))
            logger.debug(f"[{self.name}] Message sent to {msg.chat_id}")
        except Exception as e:
            logger.error(f"[{self.name}] Error sending WhatsApp message: {e}")

    async def _handle_bridge_message(self, raw: str) -> None:
        """处理来自 bridge 的消息。

        Args:
            raw: 原始 JSON 消息字符串
        """
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            logger.warning(f"[{self.name}] Invalid JSON from bridge: {raw[:100]}")
            return

        msg_type = data.get("type")

        if msg_type == "message":
            # 来自 WhatsApp 的入站消息
            # 旧格式: pn (phone number) - <phone>@s.whatsapp.net
            # 新格式: LID 格式
            pn = data.get("pn", "")
            sender = data.get("sender", "")
            content = data.get("content", "")

            # 提取用户 ID 作为 sender_id
            user_id = pn if pn else sender
            sender_id = user_id.split("@")[0] if "@" in user_id else user_id

            # 处理语音消息
            if content == "[Voice Message]":
                logger.info(
                    f"[{self.name}] Voice message received from {sender_id}, "
                    "but direct download from bridge is not yet supported."
                )
                content = "[Voice Message: Transcription not available for WhatsApp yet]"

            await self._handle_message(
                sender_id=sender_id,
                chat_id=sender,  # 使用完整的 LID 用于回复
                content=content,
                metadata={
                    "message_id": data.get("id"),
                    "timestamp": data.get("timestamp"),
                    "is_group": data.get("isGroup", False)
                }
            )

        elif msg_type == "status":
            # 连接状态更新
            status = data.get("status")
            logger.info(f"[{self.name}] WhatsApp status: {status}")

            if status == "connected":
                self._connected = True
            elif status == "disconnected":
                self._connected = False

        elif msg_type == "qr":
            # QR 码用于认证
            logger.info(f"[{self.name}] Scan QR code in the bridge terminal to connect WhatsApp")

        elif msg_type == "error":
            logger.error(f"[{self.name}] WhatsApp bridge error: {data.get('error')}")
