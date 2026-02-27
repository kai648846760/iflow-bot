"""Agent Loop - 核心消息处理循环。

BOOTSTRAP 引导机制：
- 每次处理消息前检查 workspace/BOOTSTRAP.md 是否存在
- 如果存在，将内容作为系统前缀注入到消息中
- AI 会自动执行引导流程
- 引导完成后 AI 会删除 BOOTSTRAP.md

流式输出支持：
- ACP 模式下支持实时流式输出到渠道
- 消息块会实时发送到支持流式的渠道（如 Telegram）
"""

from __future__ import annotations

import asyncio
import random
from pathlib import Path
from typing import TYPE_CHECKING, Optional

from loguru import logger

from iflow_bot.bus import MessageBus, InboundMessage, OutboundMessage
from iflow_bot.engine.adapter import IFlowAdapter

if TYPE_CHECKING:
    from iflow_bot.channels.manager import ChannelManager


# 支持流式输出的渠道列表
STREAMING_CHANNELS = {"telegram", "discord", "slack", "dingtalk", "qq"}

# 流式输出缓冲区大小范围（字符数）
STREAM_BUFFER_MIN = 10
STREAM_BUFFER_MAX = 25


class AgentLoop:
    """Agent 主循环 - 处理来自各渠道的消息。

    工作流程:
    1. 检查 BOOTSTRAP.md 是否存在（首次启动引导）
    2. 从消息总线获取入站消息
    3. 通过 SessionMappingManager 获取/创建会话 ID
    4. 调用 IFlowAdapter 发送消息到 iflow（支持流式）
    5. 将响应发布到消息总线
    """

    def __init__(
        self,
        bus: MessageBus,
        adapter: IFlowAdapter,
        model: str = "kimi-k2.5",
        streaming: bool = True,
        channel_manager: Optional["ChannelManager"] = None,
    ):
        self.bus = bus
        self.adapter = adapter
        self.model = model
        self.streaming = streaming
        self.workspace = adapter.workspace
        self.channel_manager = channel_manager

        self._running = False
        self._task: Optional[asyncio.Task] = None
        
        # 流式消息缓冲区
        self._stream_buffers: dict[str, str] = {}
        self._stream_tasks: dict[str, asyncio.Task] = {}

        logger.info(f"AgentLoop initialized with model={model}, workspace={self.workspace}, streaming={streaming}")

    def _get_bootstrap_content(self) -> tuple[Optional[str], bool]:
        """读取引导内容。
        
        Returns:
            tuple: (内容, 是否是 BOOTSTRAP)
            - 如果 BOOTSTRAP.md 存在，返回 (BOOTSTRAP内容, True)
            - 否则如果 AGENTS.md 存在，返回 (AGENTS内容, False)
            - 都不存在，返回 (None, False)
        """
        # 优先检查 BOOTSTRAP.md
        bootstrap_file = self.workspace / "BOOTSTRAP.md"
        if bootstrap_file.exists():
            try:
                content = bootstrap_file.read_text(encoding="utf-8")
                logger.info("BOOTSTRAP.md detected - will inject bootstrap instructions")
                return content, True
            except Exception as e:
                logger.error(f"Error reading BOOTSTRAP.md: {e}")
        
        # 否则注入 AGENTS.md
        agents_file = self.workspace / "AGENTS.md"
        if agents_file.exists():
            try:
                content = agents_file.read_text(encoding="utf-8")
                logger.debug("AGENTS.md detected - will inject agents context")
                return content, False
            except Exception as e:
                logger.error(f"Error reading AGENTS.md: {e}")
        
        return None, False

    def _inject_bootstrap(self, message: str, bootstrap_content: str, is_bootstrap: bool = True) -> str:
        """将引导内容注入到消息中。
        
        Args:
            message: 用户消息
            bootstrap_content: 引导内容
            is_bootstrap: 是否是 BOOTSTRAP 模式（首次引导）
        
        格式（BOOTSTRAP 模式）：
        [BOOTSTRAP - 首次启动引导 - 必须执行]
        {bootstrap_content}
        [/BOOTSTRAP]
        
        用户消息: {message}
        
        格式（AGENTS 模式）：
        [AGENTS - 工作空间指南]
        {agents_content}
        [/AGENTS]
        
        用户消息: {message}
        """
        if is_bootstrap:
            return f"""[BOOTSTRAP - 首次启动引导 - 必须执行]
以下是首次启动引导文件，你必须按照其中的指示完成身份设置。
完成引导后，删除 workspace/BOOTSTRAP.md 文件,删除后只需要告诉用户已完成身份设置即可，无需告诉用户关于 BOOTSTRAP.md 文件的任何信息。

{bootstrap_content}
[/BOOTSTRAP]

用户消息: {message}"""
        else:
            return f"""[AGENTS - 工作空间指南]
以下是当前工作空间的行为指南，请严格遵循。

{bootstrap_content}
[/AGENTS]

SOUL.md - Who You Are（你的灵魂）定义了你是谁，你的性格、特点、行为准则等核心信息。
IDENTITY.md - Your Identity（你的身份）定义了你的具体身份信息，如名字、年龄、职业、兴趣爱好等。
USERY.md - User Identity（用户身份）定义了用户的具体身份信息，如名字、年龄、职业、兴趣爱好等。
TOOLS.md - Your Tools（你的工具）定义了你可以使用的工具列表，包括每个工具的名称、功能描述、使用方法等, 每次学会一个工具，你便要主动更新该文件。

用户消息: {message}"""

    def _build_channel_context(self, msg) -> str:
        """Build channel context for the agent.
        
        Args:
            msg: InboundMessage object
            
        Returns:
            Channel context string
        """
        from datetime import datetime
        
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        
        context = f"""[message_source]
channel: {msg.channel}
chat_id: {msg.chat_id}
session: {msg.channel}:{msg.chat_id}
time: {now}
[/message_source]"""
        
        return context

    async def run(self) -> None:
        """启动主循环。"""
        self._running = True
        logger.info("AgentLoop started, listening for inbound messages...")

        while self._running:
            try:
                msg = await self.bus.consume_inbound()
                # 异步处理消息
                asyncio.create_task(self._process_message(msg))
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Error in main loop: {e}")
                await asyncio.sleep(0.1)

    async def _process_message(self, msg: InboundMessage) -> None:
        """处理单条消息。"""
        try:
            logger.info(f"Processing: {msg.channel}:{msg.chat_id}")

            # 检查是否是新会话请求（如 /new 命令）
            if msg.content.strip().lower() in ["/new", "/start"]:
                # 清除会话映射，开始新对话
                self.adapter.session_mappings.clear_session(msg.channel, msg.chat_id)
                await self.bus.publish_outbound(OutboundMessage(
                    channel=msg.channel,
                    chat_id=msg.chat_id,
                    content="✨ 已开始新对话，之前的上下文已清除。",
                ))
                return

            # 准备消息内容
            message_content = msg.content
            
            # 注入渠道上下文
            channel_context = self._build_channel_context(msg)
            if channel_context:
                message_content = channel_context + "\n\n" + message_content
            
            # 检查引导文件（优先 BOOTSTRAP.md，否则 AGENTS.md）
            bootstrap_content, is_bootstrap = self._get_bootstrap_content()
            
            # 如果有引导内容，注入到消息中
            if bootstrap_content:
                message_content = self._inject_bootstrap(message_content, bootstrap_content, is_bootstrap)
                mode = "BOOTSTRAP" if is_bootstrap else "AGENTS"
                logger.info(f"Injected {mode} for {msg.channel}:{msg.chat_id}")

            # 检查是否支持流式输出
            supports_streaming = self.streaming and msg.channel in STREAMING_CHANNELS
            
            if supports_streaming:
                # 流式模式
                response = await self._process_with_streaming(msg, message_content)
            else:
                # 非流式模式
                response = await self.adapter.chat(
                    message=message_content,
                    channel=msg.channel,
                    chat_id=msg.chat_id,
                    model=self.model,
                )

            # 发送最终响应（如果有内容且不是流式模式）
            if response and not supports_streaming:
                await self.bus.publish_outbound(OutboundMessage(
                    channel=msg.channel,
                    chat_id=msg.chat_id,
                    content=response,
                    metadata={"reply_to_id": msg.metadata.get("message_id")},
                ))
                logger.info(f"Response sent to {msg.channel}:{msg.chat_id}")

        except Exception as e:
            logger.error(f"Error processing message: {e}")
            await self.bus.publish_outbound(OutboundMessage(
                channel=msg.channel,
                chat_id=msg.chat_id,
                content=f"❌ 处理消息时出错: {e}",
            ))

    async def _process_with_streaming(
        self,
        msg: InboundMessage,
        message_content: str,
    ) -> str:
        """
        流式处理消息并发送实时更新到渠道。
        
        使用内容缓冲机制，每满 N 个字符推送一次（随机范围）。
        
        Args:
            msg: 入站消息
            message_content: 准备好的消息内容
        
        Returns:
            最终响应文本
        """
        session_key = f"{msg.channel}:{msg.chat_id}"
        
        # 初始化缓冲区
        self._stream_buffers[session_key] = ""
        
        # 未发送的字符计数和当前阈值
        unflushed_count = 0
        current_threshold = random.randint(STREAM_BUFFER_MIN, STREAM_BUFFER_MAX)
        
        # 钉钉使用直接调用方式（AI Card）
        dingtalk_channel = None
        if msg.channel == "dingtalk" and self.channel_manager:
            dingtalk_channel = self.channel_manager.get_channel("dingtalk")
            # 立即创建 AI Card，实现秒回卡片
            if dingtalk_channel and hasattr(dingtalk_channel, 'start_streaming'):
                await dingtalk_channel.start_streaming(msg.chat_id)
            dingtalk_channel = self.channel_manager.get_channel("dingtalk")

        # QQ 使用直接调用方式（流式分段发送）
        qq_channel = None
        qq_segment_buffer = ""  # 当前正在累积的段内容
        qq_line_buffer = ""      # 还没收到 \n 的不完整行（用于正确检测 ```）
        qq_newline_count = 0
        qq_in_code_block = False  # 是否在代码块内（代码块内换行符不计入阈值）
        if msg.channel == "qq" and self.channel_manager:
            qq_channel = self.channel_manager.get_channel("qq")
        
        async def on_chunk(channel: str, chat_id: str, chunk_text: str):
            """处理流式消息块。

            - QQ 渠道：按换行符精确分段，达到 split_threshold 时立即直接推送一条消息
            - 其他渠道：基于内容长度缓冲
            """
            nonlocal unflushed_count, current_threshold, qq_segment_buffer, qq_line_buffer, qq_newline_count, qq_in_code_block

            key = f"{channel}:{chat_id}"

            # 更新累积缓冲区（所有渠道，用于记录完整内容与日志）
            self._stream_buffers[key] = self._stream_buffers.get(key, "") + chunk_text

            # QQ 渠道：按换行符分段直接发送，不走字符缓冲逻辑
            if channel == "qq" and qq_channel:
                threshold = getattr(qq_channel.config, "split_threshold", 0)
                if threshold > 0:
                    # 使用真正的行级缓冲：把 chunk 先添入 line_buffer
                    # 再循环提取完整行（以 \n 为的）进行处理
                    # 这样即使 ``` 被分割到多个 chunk，也能正确检测
                    qq_line_buffer += chunk_text
                    while "\n" in qq_line_buffer:
                        idx = qq_line_buffer.index("\n")
                        complete_line = qq_line_buffer[:idx]       # 不含 \n
                        qq_line_buffer = qq_line_buffer[idx + 1:]  # \n 后的剩余

                        # 检测代码块分隔符
                        if complete_line.strip().startswith("```"):
                            qq_in_code_block = not qq_in_code_block

                        # 将完整行加入当前段
                        qq_segment_buffer += complete_line + "\n"

                        # 代码块内的换行符不计入阈值
                        if not qq_in_code_block:
                            qq_newline_count += 1
                            if qq_newline_count >= threshold:
                                segment = qq_segment_buffer.strip()
                                qq_segment_buffer = ""
                                qq_newline_count = 0
                                if segment:
                                    await qq_channel.send(OutboundMessage(
                                        channel=channel,
                                        chat_id=chat_id,
                                        content=segment,
                                        metadata={"reply_to_id": msg.metadata.get("message_id")},
                                    ))
                                    from iflow_bot.session.recorder import get_recorder
                                    recorder = get_recorder()
                                    if recorder:
                                        recorder.record_outbound(OutboundMessage(
                                            channel=channel,
                                            chat_id=chat_id,
                                            content=segment,
                                            metadata={"reply_to_id": msg.metadata.get("message_id")},
                                        ))
                return  # 不走字符缓冲逻辑

            unflushed_count += len(chunk_text)

            # 当累积足够字符时发送更新
            if unflushed_count >= current_threshold:
                unflushed_count = 0
                current_threshold = random.randint(STREAM_BUFFER_MIN, STREAM_BUFFER_MAX)

                # 钉钉：直接调用渠道的流式方法
                if channel == "dingtalk" and dingtalk_channel and hasattr(dingtalk_channel, 'handle_streaming_chunk'):
                    await dingtalk_channel.handle_streaming_chunk(chat_id, self._stream_buffers[key], is_final=False)
                else:
                    # 其他渠道：通过消息总线
                    await self.bus.publish_outbound(OutboundMessage(
                        channel=channel,
                        chat_id=chat_id,
                        content=self._stream_buffers[key],
                        metadata={
                            "_progress": True,
                            "_streaming": True,
                            "reply_to_id": msg.metadata.get("message_id"),
                        },
                    ))
        
        try:
            # 使用流式 chat
            response = await self.adapter.chat_stream(
                message=message_content,
                channel=msg.channel,
                chat_id=msg.chat_id,
                model=self.model,
                on_chunk=on_chunk,
            )
            
            # 清理缓冲区并发送最终内容
            final_content = self._stream_buffers.pop(session_key, "")

            # QQ 渠道：这里必须在 final_content 判断之外呼叫
            # 原因：如果 _stream_buffers.pop 返回空字符串则 final_content 为 ""，
            # 但 qq_segment_buffer/qq_line_buffer 里已经有内容，不能被忽略（问题2: 消息卡幻）
            if msg.channel == "qq" and qq_channel:
                threshold = getattr(qq_channel.config, "split_threshold", 0)
                from iflow_bot.session.recorder import get_recorder
                recorder = get_recorder()
                if threshold <= 0:
                    # 不分段：发送完整内容
                    content_to_send = final_content.strip()
                    if content_to_send:
                        await qq_channel.send(OutboundMessage(
                            channel=msg.channel,
                            chat_id=msg.chat_id,
                            content=content_to_send,
                            metadata={"reply_to_id": msg.metadata.get("message_id")},
                        ))
                        if recorder:
                            recorder.record_outbound(OutboundMessage(
                                channel=msg.channel,
                                chat_id=msg.chat_id,
                                content=content_to_send,
                                metadata={"reply_to_id": msg.metadata.get("message_id")},
                            ))
                else:
                    # 分段：发送流式结束时遗留的buffer
                    # qq_segment_buffer: 已解析的完整行内容
                    # qq_line_buffer: 最后一行尚未收到 \n 的不完整内容
                    remainder_to_send = (qq_segment_buffer + qq_line_buffer).strip()
                    if remainder_to_send:
                        await qq_channel.send(OutboundMessage(
                            channel=msg.channel,
                            chat_id=msg.chat_id,
                            content=remainder_to_send,
                            metadata={"reply_to_id": msg.metadata.get("message_id")},
                        ))
                        if recorder:
                            recorder.record_outbound(OutboundMessage(
                                channel=msg.channel,
                                chat_id=msg.chat_id,
                                content=remainder_to_send,
                                metadata={"reply_to_id": msg.metadata.get("message_id")},
                            ))

            if final_content:
                # 钉钉：直接调用最终更新
                if msg.channel == "dingtalk" and dingtalk_channel and hasattr(dingtalk_channel, 'handle_streaming_chunk'):
                    await dingtalk_channel.handle_streaming_chunk(msg.chat_id, final_content, is_final=True)
                elif msg.channel != "qq":
                    # 其他渠道（非 QQ、非钉钉）：通过消息总线
                    # QQ 已在上方直接通过 qq_channel.send() 处理，不走 bus，避免重复发送
                    await self.bus.publish_outbound(OutboundMessage(
                        channel=msg.channel,
                        chat_id=msg.chat_id,
                        content=final_content,
                        metadata={
                            "_progress": True,
                            "_streaming": True,
                            "reply_to_id": msg.metadata.get("message_id"),
                        },
                    ))
                    # 再发送流式结束标记
                    await self.bus.publish_outbound(OutboundMessage(
                        channel=msg.channel,
                        chat_id=msg.chat_id,
                        content="",
                        metadata={
                            "_streaming_end": True,
                            "reply_to_id": msg.metadata.get("message_id"),
                        },
                    ))
                logger.info(f"Streaming response completed for {msg.channel}:{msg.chat_id}")
            
            return final_content or response
            
        except Exception as e:
            # 清理缓冲区
            self._stream_buffers.pop(session_key, None)
            raise e

    async def process_direct(
        self,
        message: str,
        session_key: Optional[str] = None,
        channel: str = "cli",
        chat_id: str = "direct",
        on_progress: Optional[callable] = None,
    ) -> str:
        """直接处理消息（CLI 模式 / Cron / Heartbeat）。
        
        Args:
            message: 消息内容
            session_key: 可选的会话标识（如 "cron:abc123" 或 "heartbeat"）
            channel: 渠道名称
            chat_id: 聊天 ID
            on_progress: 进度回调（可选）
        """
        # 检查引导文件（优先 BOOTSTRAP.md，否则 AGENTS.md）
        bootstrap_content, is_bootstrap = self._get_bootstrap_content()
        
        message_content = message
        if bootstrap_content:
            message_content = self._inject_bootstrap(message, bootstrap_content, is_bootstrap)
            mode = "BOOTSTRAP" if is_bootstrap else "AGENTS"
            logger.info(f"Injected {mode} for {channel}:{chat_id} (direct mode)")
        
        # 如果提供了 session_key，使用它作为会话标识
        # 否则使用 channel:chat_id 格式
        effective_channel = channel
        effective_chat_id = chat_id
        
        if session_key:
            # 解析 session_key 格式（如 "cron:abc123"）
            parts = session_key.split(":", 1)
            if len(parts) == 2:
                effective_channel = parts[0]
                effective_chat_id = parts[1]
        
        return await self.adapter.chat(
            message=message_content,
            channel=effective_channel,
            chat_id=effective_chat_id,
            model=self.model,
        )

    async def start_background(self) -> None:
        """后台启动。"""
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self.run())
            logger.info("AgentLoop started in background")

    def stop(self) -> None:
        """停止。"""
        self._running = False
        if self._task and not self._task.done():
            self._task.cancel()
        logger.info("AgentLoop stopped")
