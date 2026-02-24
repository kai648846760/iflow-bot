"""Discord Channel 实现。

使用 discord.py 库实现 Discord Bot 功能，支持私聊和频道消息。
"""

import asyncio
import logging
from typing import Any, Optional

import discord

from iflow_bot.bus.events import InboundMessage, OutboundMessage
from iflow_bot.bus.queue import MessageBus
from iflow_bot.channels.base import BaseChannel
from iflow_bot.channels.manager import register_channel
from iflow_bot.config.schema import DiscordConfig


logger = logging.getLogger(__name__)


# Discord 消息字符限制
DISCORD_MAX_MESSAGE_LENGTH = 2000


@register_channel("discord")
class DiscordChannel(BaseChannel):
    """Discord Channel 实现。

    使用 discord.py 库连接 Discord Gateway，支持：
    - 私聊 (DM) 和频道消息
    - 权限检查 (allow_from 白名单)
    - 长消息分片 (超过 2000 字符自动分割)
    - Embed 消息支持

    Attributes:
        name: 渠道名称 ("discord")
        config: Discord 配置对象
        bus: 消息总线实例
        client: discord.Client 实例
    """

    name = "discord"

    def __init__(self, config: DiscordConfig, bus: MessageBus):
        """初始化 Discord Channel。

        Args:
            config: Discord 配置对象
            bus: 消息总线实例
        """
        super().__init__(config, bus)
        self.client: Optional[discord.Client] = None
        self._ready_event = asyncio.Event()

    async def start(self) -> None:
        """启动 Discord Bot。

        创建 discord.Client 并连接到 Discord Gateway。
        配置必要的 intents 以接收消息事件。
        """
        # 配置 intents
        intents = discord.Intents.default()
        intents.message_content = True  # 需要读取消息内容
        intents.messages = True  # 接收消息事件
        intents.dm_messages = True  # 接收私聊消息
        intents.guild_messages = True  # 接收服务器消息

        # 创建客户端
        self.client = discord.Client(intents=intents)

        # 注册事件处理器
        self._setup_event_handlers()

        logger.info(f"[{self.name}] Starting Discord bot...")

        # 启动 Bot (非阻塞)
        asyncio.create_task(self._run_bot())

        # 等待就绪
        try:
            await asyncio.wait_for(self._ready_event.wait(), timeout=30.0)
            self._running = True
            logger.info(f"[{self.name}] Discord bot started successfully")
        except asyncio.TimeoutError:
            logger.error(f"[{self.name}] Discord bot failed to start within timeout")
            raise

    async def _run_bot(self) -> None:
        """运行 Discord Bot 的后台任务。"""
        try:
            await self.client.start(self.config.token)
        except discord.LoginFailure as e:
            logger.error(f"[{self.name}] Discord login failed: {e}")
        except Exception as e:
            logger.error(f"[{self.name}] Discord bot error: {e}")
        finally:
            self._running = False

    def _setup_event_handlers(self) -> None:
        """设置 Discord 事件处理器。"""

        @self.client.event
        async def on_ready():
            """Bot 就绪事件。"""
            logger.info(
                f"[{self.name}] Logged in as {self.client.user} "
                f"(ID: {self.client.user.id})"
            )
            self._ready_event.set()

        @self.client.event
        async def on_message(message: discord.Message):
            """消息接收事件。"""
            await self._handle_discord_message(message)

        @self.client.event
        async def on_error(event: str, *args, **kwargs):
            """错误处理事件。"""
            logger.error(f"[{self.name}] Discord error in event '{event}'")

        @self.client.event
        async def on_disconnect():
            """断开连接事件。"""
            logger.warning(f"[{self.name}] Disconnected from Discord Gateway")

        @self.client.event
        async def on_resumed():
            """重新连接事件。"""
            logger.info(f"[{self.name}] Resumed connection to Discord Gateway")

    async def stop(self) -> None:
        """停止 Discord Bot。

        关闭 Discord 客户端连接并清理资源。
        """
        logger.info(f"[{self.name}] Stopping Discord bot...")
        self._running = False

        if self.client:
            await self.client.close()
            self.client = None

        logger.info(f"[{self.name}] Discord bot stopped")

    async def send(self, msg: OutboundMessage) -> None:
        """发送消息到 Discord。

        支持私聊 (DM) 和频道消息，自动处理长消息分片。
        
        Args:
            msg: 出站消息对象
                - chat_id: Discord 用户 ID (私聊) 或 频道 ID
                - content: 消息内容
                - metadata: 可包含 embed 配置
        """
        if not self.client or not self._running:
            logger.warning(f"[{self.name}] Client not ready, cannot send message")
            return

        try:
            # 获取目标 (用户或频道)
            target = await self._get_send_target(msg.chat_id, msg.metadata)
            if target is None:
                logger.warning(f"[{self.name}] Cannot find target: {msg.chat_id}")
                return

            # 检查是否有 embed 配置
            embed = self._build_embed(msg.metadata)

            # 发送消息 (处理分片)
            if embed:
                # Embed 消息不支持分片，直接发送
                await target.send(content=msg.content[:DISCORD_MAX_MESSAGE_LENGTH], embed=embed)
            else:
                # 普通消息，支持分片
                await self._send_chunked(target, msg.content)

            logger.debug(f"[{self.name}] Message sent to {msg.chat_id}")

        except discord.Forbidden:
            logger.error(f"[{self.name}] No permission to send to {msg.chat_id}")
        except discord.NotFound:
            logger.error(f"[{self.name}] Target not found: {msg.chat_id}")
        except Exception as e:
            logger.error(f"[{self.name}] Failed to send message: {e}")

    async def _get_send_target(
        self, chat_id: str, metadata: dict[str, Any]
    ) -> Optional[discord.abc.Messageable]:
        """获取消息发送目标。

        根据 chat_id 和 metadata 判断发送目标类型：
        - 私聊 (DM): 通过用户 ID 获取 DM 频道
        - 频道消息: 通过频道 ID 获取频道对象

        Args:
            chat_id: 目标 ID (用户 ID 或 频道 ID)
            metadata: 元数据，可包含 "is_dm" 标志

        Returns:
            可发送消息的目标对象，或 None
        """
        try:
            # 尝试解析为整数 ID
            target_id = int(chat_id)
        except ValueError:
            logger.warning(f"[{self.name}] Invalid chat_id: {chat_id}")
            return None

        # 检查是否明确指定为 DM
        is_dm = metadata.get("is_dm", False)

        if is_dm:
            # 私聊: 获取用户并创建 DM 频道
            user = self.client.get_user(target_id)
            if user:
                return user.dm_channel or await user.create_dm()
            # 尝试通过 ID 获取现有的 DM 频道
            channel = self.client.get_channel(target_id)
            if isinstance(channel, discord.DMChannel):
                return channel
        else:
            # 频道消息: 获取频道
            channel = self.client.get_channel(target_id)
            if isinstance(channel, discord.abc.Messageable):
                return channel

            # 如果频道获取失败，尝试作为用户 ID 处理 (私聊)
            user = self.client.get_user(target_id)
            if user:
                return user.dm_channel or await user.create_dm()

        return None

    def _build_embed(self, metadata: dict[str, Any]) -> Optional[discord.Embed]:
        """构建 Discord Embed 对象。

        从 metadata 中提取 embed 配置并构建 Embed 对象。

        Args:
            metadata: 元数据，可包含 "embed" 键

        Returns:
            Embed 对象，如果未配置则返回 None
        """
        embed_config = metadata.get("embed")
        if not embed_config or not isinstance(embed_config, dict):
            return None

        try:
            embed = discord.Embed()

            # 标题
            if title := embed_config.get("title"):
                embed.title = str(title)

            # 描述
            if description := embed_config.get("description"):
                embed.description = str(description)

            # 颜色
            if color := embed_config.get("color"):
                embed.colour = discord.Color(int(color))

            # URL
            if url := embed_config.get("url"):
                embed.url = str(url)

            # 字段
            for field in embed_config.get("fields", []):
                embed.add_field(
                    name=field.get("name", ""),
                    value=field.get("value", ""),
                    inline=field.get("inline", False),
                )

            # Footer
            if footer := embed_config.get("footer"):
                embed.set_footer(text=str(footer))

            # 作者
            if author := embed_config.get("author"):
                embed.set_author(name=str(author))

            # 缩略图
            if thumbnail := embed_config.get("thumbnail"):
                embed.set_thumbnail(url=str(thumbnail))

            # 图片
            if image := embed_config.get("image"):
                embed.set_image(url=str(image))

            return embed

        except Exception as e:
            logger.warning(f"[{self.name}] Failed to build embed: {e}")
            return None

    async def _send_chunked(
        self, target: discord.abc.Messageable, content: str
    ) -> None:
        """发送消息，自动处理分片。

        Discord 消息限制为 2000 字符，超过限制的消息会被分割发送。

        Args:
            target: 消息发送目标
            content: 消息内容
        """
        if len(content) <= DISCORD_MAX_MESSAGE_LENGTH:
            await target.send(content=content)
            return

        # 分片发送
        chunks = self._split_message(content, DISCORD_MAX_MESSAGE_LENGTH)
        for i, chunk in enumerate(chunks):
            await target.send(content=chunk)
            logger.debug(f"[{self.name}] Sent chunk {i + 1}/{len(chunks)}")

    def _split_message(self, content: str, max_length: int) -> list[str]:
        """将长消息分割为多个片段。

        尝试在换行符或空格处分割，避免截断单词。

        Args:
            content: 原始消息内容
            max_length: 每段最大长度

        Returns:
            分割后的消息列表
        """
        if len(content) <= max_length:
            return [content]

        chunks = []
        remaining = content

        while remaining:
            if len(remaining) <= max_length:
                chunks.append(remaining)
                break

            # 尝试在换行符处分割
            chunk = remaining[:max_length]
            last_newline = chunk.rfind("\n")
            last_space = chunk.rfind(" ")

            # 选择最佳分割点
            split_pos = max(last_newline, last_space)
            if split_pos > max_length // 2:
                # 在分割点后分割
                chunk = remaining[: split_pos + 1]
            else:
                # 无法找到好的分割点，强制分割
                chunk = remaining[:max_length]

            chunks.append(chunk)
            remaining = remaining[len(chunk) :]

        return chunks

    async def _handle_discord_message(self, message: discord.Message) -> None:
        """处理 Discord 消息事件。

        过滤 Bot 消息，提取消息信息，调用基类的消息处理方法。

        Args:
            message: Discord 消息对象
        """
        # 忽略 Bot 自己的消息
        if message.author == self.client.user:
            return

        # 忽略其他 Bot 的消息
        if message.author.bot:
            return

        # 提取发送者 ID
        sender_id = str(message.author.id)

        # 确定 chat_id
        # - 私聊: 使用用户 ID
        # - 频道: 使用频道 ID
        if isinstance(message.channel, discord.DMChannel):
            chat_id = str(message.author.id)  # 私聊使用用户 ID
            is_dm = True
        else:
            chat_id = str(message.channel.id)  # 频道使用频道 ID
            is_dm = False

        # 提取消息内容
        content = message.content or ""

        # 提取媒体附件
        media = []
        if message.attachments:
            for attachment in message.attachments:
                media.append(attachment.url)

        # 提取消息中的图片链接
        if message.embeds:
            for embed in message.embeds:
                if embed.thumbnail and embed.thumbnail.url:
                    media.append(embed.thumbnail.url)
                if embed.image and embed.image.url:
                    media.append(embed.image.url)

        # 构建元数据
        metadata: dict[str, Any] = {
            "message_id": str(message.id),
            "author_name": message.author.display_name,
            "author_display_name": message.author.global_name or message.author.name,
            "is_dm": is_dm,
            "guild_id": str(message.guild.id) if message.guild else None,
            "channel_name": message.channel.name if hasattr(message.channel, "name") else None,
            "timestamp": message.created_at.isoformat(),
            "reference_message_id": str(message.reference.message_id) if message.reference else None,
        }

        # 调用基类的消息处理方法 (会进行权限检查)
        await self._handle_message(
            sender_id=sender_id,
            chat_id=chat_id,
            content=content,
            media=media,
            metadata=metadata,
        )
