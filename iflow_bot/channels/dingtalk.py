"""DingTalk (钉钉) channel implementation using Stream Mode.

使用 dingtalk-stream SDK 通过 WebSocket 接收消息，
使用 HTTP API 发送消息。无需公网 IP 或 webhook 配置。

支持 AI Card 流式输出，实现打字机效果。
"""

import asyncio
import json
import logging
import time
import uuid
from typing import Any, Dict, Optional, Set

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
    CallbackHandler = object  # type: ignore
    CallbackMessage = None  # type: ignore
    AckMessage = None  # type: ignore
    ChatbotMessage = None  # type: ignore


logger = logging.getLogger(__name__)

# AI Card 状态常量
class AICardStatus:
    PROCESSING = "1"
    INPUTING = "2"
    FINISHED = "3"
    FAILED = "5"


class AICardInstance:
    """AI Card 实例，用于跟踪流式输出状态。"""
    
    def __init__(
        self,
        card_instance_id: str,
        access_token: str,
        conversation_id: str,
        config: DingTalkConfig,
    ):
        self.card_instance_id = card_instance_id
        self.access_token = access_token
        self.conversation_id = conversation_id
        self.config = config
        self.created_at = time.time()
        self.last_updated = time.time()
        self.state = AICardStatus.PROCESSING


def is_card_in_terminal_state(state: str) -> bool:
    """检查 Card 是否处于终态。"""
    return state in (AICardStatus.FINISHED, AICardStatus.FAILED)


class DingTalkHandler(CallbackHandler):
    """钉钉 Stream SDK 回调处理器。"""

    def __init__(self, channel: "DingTalkChannel"):
        super().__init__()
        self.channel = channel

    async def process(self, message: CallbackMessage):
        """处理入站的 stream 消息。"""
        try:
            chatbot_msg = ChatbotMessage.from_dict(message.data)

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
            
            # 判断是否群聊
            is_group = chatbot_msg.conversation_type == "2"
            chat_id = chatbot_msg.conversation_id if is_group else sender_id

            logger.info(
                f"[{self.channel.name}] Received message from {sender_name} "
                f"({sender_id}) in {'group' if is_group else 'private'}: {content[:50]}..."
            )

            task = asyncio.create_task(
                self.channel._on_message(content, sender_id, sender_name, chat_id, is_group)
            )
            self.channel._background_tasks.add(task)
            task.add_done_callback(self.channel._background_tasks.discard)

            return AckMessage.STATUS_OK, "OK"

        except Exception as e:
            logger.error(f"[{self.channel.name}] Error processing message: {e}")
            return AckMessage.STATUS_OK, "Error"


@register_channel("dingtalk")
class DingTalkChannel(BaseChannel):
    """钉钉 Channel - 使用 Stream Mode，支持 AI Card 流式输出。"""

    name = "dingtalk"
    
    # 支持流式输出的渠道标识
    supports_streaming = True

    def __init__(self, config: DingTalkConfig, bus: MessageBus):
        super().__init__(config, bus)
        self.config: DingTalkConfig = config
        self._client: Any = None
        self._http: Optional[httpx.AsyncClient] = None

        # Access Token 管理
        self._access_token: Optional[str] = None
        self._token_expiry: float = 0

        # AI Card 管理
        self._ai_cards: Dict[str, AICardInstance] = {}
        self._active_cards_by_target: Dict[str, str] = {}

        # 流式消息缓冲
        self._streaming_buffers: Dict[str, str] = {}
        self._streaming_card_ids: Dict[str, str] = {}
        self._streaming_end: Set[str] = set()

        # 后台任务
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
            logger.error(f"[{self.name}] httpx not installed. Run: pip install httpx")
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

        handler = DingTalkHandler(self)
        self._client.register_callback_handler(ChatbotMessage.TOPIC, handler)

        logger.info(f"[{self.name}] DingTalk bot started with Stream Mode")

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

        if self._http:
            await self._http.aclose()
            self._http = None

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
            return None

        try:
            resp = await self._http.post(url, json=data)
            resp.raise_for_status()
            res_data = resp.json()
            self._access_token = res_data.get("accessToken")
            self._token_expiry = time.time() + int(res_data.get("expireIn", 7200)) - 60
            return self._access_token
        except Exception as e:
            logger.error(f"[{self.name}] Failed to get access token: {e}")
            return None

    def _get_target_key(self, chat_id: str) -> str:
        """获取目标唯一标识。"""
        return f"{self.config.client_id}:{chat_id}"

    async def _create_ai_card(self, chat_id: str, is_group: bool = False) -> Optional[AICardInstance]:
        """创建 AI Card 实例。"""
        token = await self._get_access_token()
        if not token:
            return None

        card_template_id = getattr(self.config, 'card_template_id', None)
        if not card_template_id:
            logger.debug(f"[{self.name}] No card_template_id configured, using markdown")
            return None

        card_instance_id = f"card_{uuid.uuid4().hex[:24]}"
        
        # 构建 openSpaceId
        if is_group:
            open_space_id = f"dtv1.card//IM_GROUP.{chat_id}"
        else:
            open_space_id = f"dtv1.card//IM_ROBOT.{chat_id}"

        create_body = {
            "cardTemplateId": card_template_id,
            "outTrackId": card_instance_id,
            "cardData": {"cardParamMap": {}},
            "callbackType": "STREAM",
            "imGroupOpenSpaceModel": {"supportForward": True},
            "imRobotOpenSpaceModel": {"supportForward": True},
            "openSpaceId": open_space_id,
            "userIdType": 1,
        }

        if is_group:
            robot_code = getattr(self.config, 'robot_code', None) or self.config.client_id
            create_body["imGroupOpenDeliverModel"] = {"robotCode": robot_code}
        else:
            create_body["imRobotOpenDeliverModel"] = {"spaceType": "IM_ROBOT"}

        try:
            resp = await self._http.post(
                "https://api.dingtalk.com/v1.0/card/instances/createAndDeliver",
                json=create_body,
                headers={
                    "x-acs-dingtalk-access-token": token,
                    "Content-Type": "application/json",
                },
            )
            resp.raise_for_status()

            card = AICardInstance(
                card_instance_id=card_instance_id,
                access_token=token,
                conversation_id=chat_id,
                config=self.config,
            )
            self._ai_cards[card_instance_id] = card
            
            target_key = self._get_target_key(chat_id)
            self._active_cards_by_target[target_key] = card_instance_id

            logger.debug(f"[{self.name}] Created AI Card: {card_instance_id}")
            return card

        except Exception as e:
            logger.error(f"[{self.name}] Failed to create AI Card: {e}")
            return None

    async def _stream_ai_card(self, card: AICardInstance, content: str, finished: bool = False) -> bool:
        """流式更新 AI Card 内容。"""
        if card.state == AICardStatus.FINISHED:
            return False

        # Token 刷新检查 (90分钟阈值)
        token_age = time.time() - card.created_at
        if token_age > 90 * 60:
            new_token = await self._get_access_token()
            if new_token:
                card.access_token = new_token

        card_template_key = getattr(self.config, 'card_template_key', 'content')
        
        stream_body = {
            "outTrackId": card.card_instance_id,
            "guid": uuid.uuid4().hex,
            "key": card_template_key,
            "content": content,
            "isFull": True,
            "isFinalize": finished,
            "isError": False,
        }

        try:
            resp = await self._http.put(
                "https://api.dingtalk.com/v1.0/card/streaming",
                json=stream_body,
                headers={
                    "x-acs-dingtalk-access-token": card.access_token,
                    "Content-Type": "application/json",
                },
            )
            if resp.status_code >= 400:
                logger.error(f"[{self.name}] AI Card streaming error: status={resp.status_code}, body={resp.text[:500]}")
            resp.raise_for_status()

            card.last_updated = time.time()
            if finished:
                card.state = AICardStatus.FINISHED
            elif card.state == AICardStatus.PROCESSING:
                card.state = AICardStatus.INPUTING

            return True

        except Exception as e:
            logger.error(f"[{self.name}] AI Card streaming failed: {e}")
            card.state = AICardStatus.FAILED
            return False

    async def _send_markdown(self, chat_id: str, content: str, is_group: bool = False) -> bool:
        """发送 Markdown 消息（非 Card 模式）。"""
        token = await self._get_access_token()
        if not token:
            return False

        robot_code = getattr(self.config, 'robot_code', None) or self.config.client_id

        if is_group:
            url = "https://api.dingtalk.com/v1.0/robot/groupMessages/send"
            data = {
                "robotCode": robot_code,
                "openConversationId": chat_id,
                "msgKey": "sampleMarkdown",
                "msgParam": json.dumps({
                    "title": "iFlow Bot",
                    "text": content,
                }, ensure_ascii=False),
            }
        else:
            url = "https://api.dingtalk.com/v1.0/robot/oToMessages/batchSend"
            data = {
                "robotCode": robot_code,
                "userIds": [chat_id],
                "msgKey": "sampleMarkdown",
                "msgParam": json.dumps({
                    "title": "iFlow Bot Reply",
                    "text": content,
                }, ensure_ascii=False),
            }

        try:
            resp = await self._http.post(
                url,
                json=data,
                headers={"x-acs-dingtalk-access-token": token},
            )
            return resp.status_code == 200
        except Exception as e:
            logger.error(f"[{self.name}] Send markdown failed: {e}")
            return False

    async def send(self, msg: OutboundMessage) -> None:
        """发送消息（非流式）。"""
        is_group = msg.metadata.get("is_group", False) if msg.metadata else False
        await self._send_markdown(msg.chat_id, msg.content, is_group)

    async def start_streaming(self, chat_id: str) -> None:
        """开始流式输出，立即创建 AI Card。

        在消息处理开始时调用，确保卡片秒回。

        Args:
            chat_id: 聊天 ID
        """
        target_key = self._get_target_key(chat_id)
        
        # 初始化缓冲区
        if target_key not in self._streaming_buffers:
            self._streaming_buffers[target_key] = ""
            
            # 立即创建 AI Card
            is_group = chat_id.startswith("cid")
            card = await self._create_ai_card(chat_id, is_group)
            if card:
                self._streaming_card_ids[target_key] = card.card_instance_id
                logger.debug(f"[{self.name}] Created AI Card for streaming: {card.card_instance_id}")

    async def handle_streaming_chunk(self, chat_id: str, chunk: str, is_final: bool = False) -> None:
        """处理流式输出的 chunk。

        由 AgentLoop 调用，实现打字机效果。

        注意：chunk 参数已经是累积后的完整内容（由 AgentLoop 累积），
        这里直接使用，不再重复累积。

        Args:
            chat_id: 聊天 ID
            chunk: 累积后的完整文本内容
            is_final: 是否是最终消息
        """
        target_key = self._get_target_key(chat_id)
        
        # 确保缓冲区已初始化（兼容没有调用 start_streaming 的情况）
        if target_key not in self._streaming_buffers:
            await self.start_streaming(chat_id)

        # 直接使用传入的内容（已经是累积后的）
        full_content = chunk

        # 获取 Card
        card_id = self._streaming_card_ids.get(target_key)
        card = self._ai_cards.get(card_id) if card_id else None

        if card and not is_card_in_terminal_state(card.state):
            # 使用 AI Card 流式更新
            await self._stream_ai_card(card, full_content, is_final)
        else:
            # 降级为 Markdown 发送（仅在最终消息时发送）
            if is_final:
                is_group = chat_id.startswith("cid")
                await self._send_markdown(chat_id, full_content, is_group)

        # 清理
        if is_final:
            self._streaming_buffers.pop(target_key, None)
            self._streaming_card_ids.pop(target_key, None)
            self._streaming_end.discard(target_key)

    async def _on_message(
        self,
        content: str,
        sender_id: str,
        sender_name: str,
        chat_id: str,
        is_group: bool,
    ) -> None:
        """处理入站消息。"""
        try:
            await self._handle_message(
                sender_id=sender_id,
                chat_id=chat_id,
                content=content,
                metadata={
                    "sender_name": sender_name,
                    "platform": "dingtalk",
                    "is_group": is_group,
                },
            )
        except Exception as e:
            logger.error(f"[{self.name}] Error publishing message: {e}")