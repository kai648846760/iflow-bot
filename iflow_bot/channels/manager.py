"""Channel 管理器模块。

负责管理所有 Channel 的生命周期，包括注册、启动、停止和消息路由。
"""

import asyncio
import logging
from typing import Optional, Type

from iflow_bot.bus.events import OutboundMessage
from iflow_bot.bus.queue import MessageBus
from iflow_bot.config.schema import Config

from .base import BaseChannel


logger = logging.getLogger(__name__)


# Channel 注册表 - 存储渠道名称到 Channel 类的映射
_CHANNEL_REGISTRY: dict[str, Type[BaseChannel]] = {}


def register_channel(name: str):
    """Channel 注册装饰器。

    用于将 Channel 类注册到全局注册表中。

    Args:
        name: 渠道名称

    Example:
        @register_channel("telegram")
        class TelegramChannel(BaseChannel):
            ...
    """
    def decorator(cls: Type[BaseChannel]) -> Type[BaseChannel]:
        _CHANNEL_REGISTRY[name] = cls
        cls.name = name
        return cls
    return decorator


def get_channel_class(name: str) -> Optional[Type[BaseChannel]]:
    """获取已注册的 Channel 类。

    Args:
        name: 渠道名称

    Returns:
        Channel 类，如果未注册则返回 None
    """
    return _CHANNEL_REGISTRY.get(name)


class ChannelManager:
    """Channel 管理器 - 管理所有 Channel 的生命周期。

    主要职责：
    1. 根据配置创建和注册 Channel 实例
    2. 启动/停止所有已启用的 Channel
    3. 路由出站消息到对应的 Channel
    4. 监听出站消息队列并分发

    Attributes:
        config: 全局配置对象
        bus: 消息总线实例
        _channels: 已创建的 Channel 实例字典
        _outbound_task: 出站消息监听任务
    """

    def __init__(self, config: Config, bus: MessageBus):
        """初始化 Channel 管理器。

        Args:
            config: 全局配置对象
            bus: 消息总线实例
        """
        self.config = config
        self.bus = bus
        self._channels: dict[str, BaseChannel] = {}
        self._outbound_task: Optional[asyncio.Task] = None

    @property
    def enabled_channels(self) -> list[str]:
        """获取已启用的渠道列表。

        Returns:
            已启用的渠道名称列表
        """
        return self.config.get_enabled_channels()

    @property
    def channels(self) -> dict[str, BaseChannel]:
        """获取所有已创建的 Channel 实例。"""
        return self._channels.copy()

    def get_channel(self, name: str) -> Optional[BaseChannel]:
        """获取指定名称的 Channel 实例。

        Args:
            name: 渠道名称

        Returns:
            Channel 实例，如果不存在则返回 None
        """
        return self._channels.get(name)

    def _create_channel(self, name: str) -> Optional[BaseChannel]:
        """创建指定名称的 Channel 实例。

        Args:
            name: 渠道名称

        Returns:
            Channel 实例，如果渠道未注册或配置不存在则返回 None
        """
        channel_cls = get_channel_class(name)
        if channel_cls is None:
            logger.warning(f"Channel '{name}' is not registered")
            return None

        # 获取渠道配置
        channel_config = getattr(self.config.channels, name, None)
        if channel_config is None:
            logger.warning(f"Channel '{name}' has no configuration")
            return None

        # 创建实例
        channel = channel_cls(config=channel_config, bus=self.bus)
        return channel

    async def start_all(self) -> None:
        """启动所有已启用的 Channel。

        使用 asyncio.create_task() 并发启动所有 channel，
        然后启动出站消息监听任务。
        """
        enabled = self.enabled_channels
        logger.info(f"Starting channels: {enabled}")

        tasks = []
        for name in enabled:
            try:
                channel = self._create_channel(name)
                if channel is None:
                    continue

                # 使用 create_task 非阻塞启动
                task = asyncio.create_task(channel.start())
                self._channels[name] = channel
                tasks.append((name, task, channel))
                logger.info(f"Channel '{name}' start task created")

            except Exception as e:
                logger.error(f"Failed to create channel '{name}': {e}")

        # 等待一小段时间让 channel 初始化
        await asyncio.sleep(1)

        # 检查是否有 channel 启动失败
        for name, task, channel in tasks:
            if task.done():
                try:
                    task.result()  # 如果有异常会在这里抛出
                except Exception as e:
                    logger.error(f"Channel '{name}' failed to start: {e}")
                    self._channels.pop(name, None)

        # 启动出站消息监听任务
        if self._channels:
            self._outbound_task = asyncio.create_task(self._listen_outbound())
            logger.debug("Outbound message listener started")

    async def stop_all(self) -> None:
        """停止所有 Channel。

        先停止出站消息监听任务，然后依次停止每个 Channel。
        """
        # 停止出站消息监听
        if self._outbound_task:
            self._outbound_task.cancel()
            try:
                await self._outbound_task
            except asyncio.CancelledError:
                pass
            self._outbound_task = None
            logger.debug("Outbound message listener stopped")

        # 停止所有 Channel
        for name, channel in self._channels.items():
            try:
                await channel.stop()
                logger.info(f"Channel '{name}' stopped")
            except Exception as e:
                logger.error(f"Error stopping channel '{name}': {e}")

        self._channels.clear()

    async def send_to(self, channel: str, msg: OutboundMessage) -> None:
        """发送消息到指定渠道。

        Args:
            channel: 目标渠道名称
            msg: 出站消息对象

        Raises:
            ValueError: 指定的渠道不存在或未运行
        """
        ch = self._channels.get(channel)
        if ch is None:
            raise ValueError(f"Channel '{channel}' not found")
        if not ch.is_running:
            raise ValueError(f"Channel '{channel}' is not running")

        await ch.send(msg)

    async def _listen_outbound(self) -> None:
        """监听出站消息队列并分发到对应的 Channel。

        这是一个后台任务，持续从消息总线的出站队列消费消息，
        然后根据消息的 channel 字段路由到对应的 Channel。
        """
        logger.debug("Outbound listener started")

        while True:
            try:
                # 从出站队列获取消息
                msg = await self.bus.consume_outbound()

                # 获取目标 Channel
                channel = self._channels.get(msg.channel)
                if channel is None:
                    logger.warning(
                        f"Outbound message for unknown channel '{msg.channel}'"
                    )
                    self.bus.task_done_outbound()
                    continue

                if not channel.is_running:
                    logger.warning(
                        f"Channel '{msg.channel}' is not running, message dropped"
                    )
                    self.bus.task_done_outbound()
                    continue

                # 发送消息
                try:
                    await channel.send(msg)
                    logger.debug(
                        f"Message sent to channel '{msg.channel}' chat '{msg.chat_id}'"
                    )
                except Exception as e:
                    logger.error(
                        f"Failed to send message to channel '{msg.channel}': {e}"
                    )

                self.bus.task_done_outbound()

            except asyncio.CancelledError:
                logger.debug("Outbound listener cancelled")
                break
            except Exception as e:
                logger.error(f"Error in outbound listener: {e}")

    def __repr__(self) -> str:
        """返回管理器的字符串表示。"""
        channels_status = {
            name: "running" if ch.is_running else "stopped"
            for name, ch in self._channels.items()
        }
        return f"<ChannelManager channels={channels_status}>"
