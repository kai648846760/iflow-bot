"""Channel 基类定义模块。

提供所有 Channel 的抽象基类，定义了统一的接口和通用功能。
"""

import logging
from abc import ABC, abstractmethod
from typing import Any, Optional

from iflow_bot.bus.events import InboundMessage, OutboundMessage
from iflow_bot.bus.queue import MessageBus


logger = logging.getLogger(__name__)


class BaseChannel(ABC):
    """Channel 基类 - 所有渠道实现的抽象基类。

    定义了 Channel 的核心接口：
    - start(): 启动 Channel，开始监听消息
    - stop(): 停止 Channel
    - send(): 发送消息到渠道

    提供了通用功能：
    - 权限检查 (is_allowed)
    - 消息处理和发布到总线 (_handle_message)

    Attributes:
        name: Channel 名称标识
        config: Channel 配置对象
        bus: 消息总线实例
        _running: 运行状态标志
    """

    name: str = "base"

    def __init__(self, config: Any, bus: MessageBus):
        """初始化 Channel。

        Args:
            config: Channel 配置对象
            bus: 消息总线实例
        """
        self.config = config
        self.bus = bus
        self._running = False

    @abstractmethod
    async def start(self) -> None:
        """启动 Channel，开始监听消息。

        子类需要实现具体的连接和监听逻辑。
        启动成功后应将 _running 设置为 True。
        """
        pass

    @abstractmethod
    async def stop(self) -> None:
        """停止 Channel。

        子类需要实现具体的断开连接和清理逻辑。
        停止后应将 _running 设置为 False。
        """
        pass

    @abstractmethod
    async def send(self, msg: OutboundMessage) -> None:
        """发送消息到渠道。

        Args:
            msg: 出站消息对象

        子类需要实现具体的消息发送逻辑。
        """
        pass

    def is_allowed(self, sender_id: str) -> bool:
        """检查发送者是否有权限。

        如果配置了 allow_from 白名单，则只允许白名单中的发送者。
        如果未配置白名单（空列表），则允许所有发送者。

        Args:
            sender_id: 发送者 ID

        Returns:
            是否有权限
        """
        allow_list = getattr(self.config, "allow_from", [])
        
        # 如果没有配置白名单，允许所有
        if not allow_list:
            return True
        
        sender_str = str(sender_id)
        # 先直接比较完整 sender_id
        if sender_str in allow_list:
            return True
        # 如果 sender_id 包含 |，遍历拆分后的每个部分检查
        if "|" in sender_str:
            for part in sender_str.split("|"):
                if part and part in allow_list:
                    return True
        return False

    async def _handle_message(
        self,
        sender_id: str,
        chat_id: str,
        content: str,
        media: Optional[list[str]] = None,
        metadata: Optional[dict[str, Any]] = None,
    ) -> None:
        """处理入站消息，发布到消息总线。

        这是 Channel 接收到消息后的统一处理方法：
        1. 检查发送者权限
        2. 构造 InboundMessage 对象
        3. 发布到消息总线的入站队列

        Args:
            sender_id: 发送者 ID
            chat_id: 聊天/频道 ID
            content: 消息内容
            media: 媒体文件 URL 列表
            metadata: 元数据 (可包含原始消息 ID、时间戳等)
        """
        # 检查权限
        if not self.is_allowed(sender_id):
            logger.debug(
                f"[{self.name}] Message from {sender_id} blocked by allow_list"
            )
            return

        # 构造入站消息
        msg = InboundMessage(
            channel=self.name,
            sender_id=str(sender_id),
            chat_id=str(chat_id),
            content=content,
            media=media or [],
            metadata=metadata or {},
        )

        # 发布到消息总线
        await self.bus.publish_inbound(msg)
        logger.debug(
            f"[{self.name}] Published inbound message from {sender_id} in {chat_id}"
        )

    @property
    def is_running(self) -> bool:
        """Channel 是否正在运行。"""
        return self._running

    def __repr__(self) -> str:
        """返回 Channel 的字符串表示。"""
        status = "running" if self._running else "stopped"
        return f"<{self.__class__.__name__} name={self.name} status={status}>"
