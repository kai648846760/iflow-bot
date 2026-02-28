"""Agent Loop - 核心消息处理循环。

BOOTSTRAP 引导机制：
- 每次处理消息前检查 workspace/BOOTSTRAP.md 是否存在
- 如果存在，将内容作为系统前缀注入到消息中
- AI 会自动执行引导流程
- 引导完成后 AI 会删除 BOOTSTRAP.md

流式输出支持：
- ACP 模式下支持实时流式输出到渠道
- 消息块会实时发送到支持流式的渠道（如 Telegram）

文件回传支持 (from feishu-iflow-bridge)：
- 使用 ResultAnalyzer 分析 iflow 输出
- 自动检测输出中生成的文件路径（图片/音频/视频/文档）
- 通过 OutboundMessage.media 字段将文件附加到响应中
- 支持文件回传的渠道（如飞书）会自动上传并发送这些文件
"""

from __future__ import annotations

import asyncio
import random
from pathlib import Path
from typing import TYPE_CHECKING, Optional

from loguru import logger

from iflow_bot.bus import MessageBus, InboundMessage, OutboundMessage
from iflow_bot.engine.adapter import IFlowAdapter
from iflow_bot.engine.analyzer import result_analyzer, AnalysisResult

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
    5. 使用 ResultAnalyzer 分析响应（检测文件、状态等）
    6. 将响应和检测到的文件发布到消息总线
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
        
        # P3: 每用户并发锁，确保同一用户的消息串行处理，避免会话状态混乱
        self._user_locks: dict[str, asyncio.Lock] = {}

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
        """将引导内容注入到消息中。"""
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
        """Build channel context for the agent."""
        from datetime import datetime
        
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        
        context = f"""[message_source]
channel: {msg.channel}
chat_id: {msg.chat_id}
session: {msg.channel}:{msg.chat_id}
time: {now}
[/message_source]"""
        
        return context

    def _analyze_and_build_outbound(
        self,
        response: str,
        channel: str,
        chat_id: str,
        metadata: Optional[dict] = None,
    ) -> OutboundMessage:
        """Analyze response with ResultAnalyzer and build OutboundMessage with media.

        Ported from feishu-iflow-bridge FeishuSender.sendExecutionResult():
        - Scans iflow output for generated file paths
        - Categorizes files (image/audio/video/doc)
        - Attaches detected files via OutboundMessage.media

        Args:
            response: Raw response text from iflow
            channel: Target channel name
            chat_id: Target chat ID
            metadata: Additional metadata

        Returns:
            OutboundMessage with content and media attachments
        """
        # Analyze the response
        analysis = result_analyzer.analyze({"output": response, "success": True})

        # Collect all detected files for media attachment
        media_files: list[str] = []
        if analysis.image_files:
            media_files.extend(analysis.image_files)
            logger.info(f"Detected {len(analysis.image_files)} image(s) in response")
        if analysis.audio_files:
            media_files.extend(analysis.audio_files)
            logger.info(f"Detected {len(analysis.audio_files)} audio file(s) in response")
        if analysis.video_files:
            media_files.extend(analysis.video_files)
            logger.info(f"Detected {len(analysis.video_files)} video file(s) in response")
        if analysis.doc_files:
            media_files.extend(analysis.doc_files)
            logger.info(f"Detected {len(analysis.doc_files)} document(s) in response")

        if media_files:
            logger.info(f"File callback: attaching {len(media_files)} file(s) to outbound message")

        return OutboundMessage(
            channel=channel,
            chat_id=chat_id,
            content=response,
            media=media_files,
            metadata=metadata or {},
        )

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

    def _get_user_lock(self, channel: str, chat_id: str) -> asyncio.Lock:
        """获取指定用户的处理锁（P3：防止同一用户并发处理）。"""
        key = f"{channel}:{chat_id}"
        if key not in self._user_locks:
            self._user_locks[key] = asyncio.Lock()
        return self._user_locks[key]

    async def _process_message(self, msg: InboundMessage) -> None:
        """处理单条消息（每用户串行）。"""
        lock = self._get_user_lock(msg.channel, msg.chat_id)
        async with lock:
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
                    # 🆕 使用 ResultAnalyzer 分析响应并提取文件
                    outbound = self._analyze_and_build_outbound(
                        response=response,
                        channel=msg.channel,
                        chat_id=msg.chat_id,
                        metadata={"reply_to_id": msg.metadata.get("message_id")},
                    )
                    await self.bus.publish_outbound(outbound)
                    logger.info(f"Response sent to {msg.channel}:{msg.chat_id}")

            except Exception as e:
                logger.exception(f"Error processing message for {msg.channel}:{msg.chat_id}")  # B6
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
            """处理流式消息块。"""
            nonlocal unflushed_count, current_threshold, qq_segment_buffer, qq_line_buffer, qq_newline_count, qq_in_code_block

            key = f"{channel}:{chat_id}"

            # 更新累积缓冲区（所有渠道，用于记录完整内容与日志）
            self._stream_buffers[key] = self._stream_buffers.get(key, "") + chunk_text

            # QQ 渠道：按换行符分段直接发送，不走字符缓冲逻辑
            if channel == "qq" and qq_channel:
                threshold = getattr(qq_channel.config, "split_threshold", 0)
                if threshold > 0:
                    qq_line_buffer += chunk_text
                    while "\n" in qq_line_buffer:
                        idx = qq_line_buffer.index("\n")
                        complete_line = qq_line_buffer[:idx]

                        qq_line_buffer = qq_line_buffer[idx + 1:]

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

            # QQ 渠道：发送遗留的buffer
            if msg.channel == "qq" and qq_channel:
                threshold = getattr(qq_channel.config, "split_threshold", 0)
                from iflow_bot.session.recorder import get_recorder
                recorder = get_recorder()
                if threshold <= 0:
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
                # 🆕 流式结束后，也用 ResultAnalyzer 分析并附加检测到的文件
                analysis = result_analyzer.analyze({"output": final_content, "success": True})
                media_files = analysis.image_files + analysis.audio_files + analysis.video_files + analysis.doc_files

                if media_files:
                    logger.info(f"Stream completed: detected {len(media_files)} file(s) for callback")

                # 钉钉：直接调用最终更新
                if msg.channel == "dingtalk" and dingtalk_channel and hasattr(dingtalk_channel, 'handle_streaming_chunk'):
                    await dingtalk_channel.handle_streaming_chunk(msg.chat_id, final_content, is_final=True)
                    # 钉钉流式结束后，单独发送检测到的文件
                    if media_files:
                        await self.bus.publish_outbound(OutboundMessage(
                            channel=msg.channel,
                            chat_id=msg.chat_id,
                            content="",
                            media=media_files,
                        ))
                elif msg.channel != "qq":
                    # 其他渠道（非 QQ、非钉钉）：通过消息总线
                    await self.bus.publish_outbound(OutboundMessage(
                        channel=msg.channel,
                        chat_id=msg.chat_id,
                        content=final_content,
                        media=media_files,
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
        """直接处理消息（CLI 模式 / Cron / Heartbeat）。"""
        # 检查引导文件（优先 BOOTSTRAP.md，否则 AGENTS.md）
        bootstrap_content, is_bootstrap = self._get_bootstrap_content()
        
        message_content = message
        if bootstrap_content:
            message_content = self._inject_bootstrap(message, bootstrap_content, is_bootstrap)
            mode = "BOOTSTRAP" if is_bootstrap else "AGENTS"
            logger.info(f"Injected {mode} for {channel}:{chat_id} (direct mode)")
        
        effective_channel = channel
        effective_chat_id = chat_id
        
        if session_key:
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
