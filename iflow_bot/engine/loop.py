"""Agent Loop - 核心消息处理循环。

BOOTSTRAP 引导机制：
- 每次处理消息前检查 workspace/BOOTSTRAP.md 是否存在
- 如果存在，将内容作为系统前缀注入到消息中
- AI 会自动执行引导流程
- 引导完成后 AI 会删除 BOOTSTRAP.md
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Optional

from loguru import logger

from iflow_bot.bus import MessageBus, InboundMessage, OutboundMessage
from iflow_bot.engine.adapter import IFlowAdapter


class AgentLoop:
    """Agent 主循环 - 处理来自各渠道的消息。

    工作流程:
    1. 检查 BOOTSTRAP.md 是否存在（首次启动引导）
    2. 从消息总线获取入站消息
    3. 通过 SessionMappingManager 获取/创建会话 ID
    4. 调用 IFlowAdapter 发送消息到 iflow
    5. 将响应发布到消息总线
    """

    def __init__(
        self,
        bus: MessageBus,
        adapter: IFlowAdapter,
        model: str = "glm-5",
    ):
        self.bus = bus
        self.adapter = adapter
        self.model = model
        self.workspace = adapter.workspace

        self._running = False
        self._task: Optional[asyncio.Task] = None

        logger.info(f"AgentLoop initialized with model={model}, workspace={self.workspace}")

    def _get_bootstrap_content(self) -> Optional[str]:
        """读取 BOOTSTRAP.md 内容。"""
        bootstrap_file = self.workspace / "BOOTSTRAP.md"
        if bootstrap_file.exists():
            try:
                content = bootstrap_file.read_text(encoding="utf-8")
                logger.info("BOOTSTRAP.md detected - will inject bootstrap instructions")
                return content
            except Exception as e:
                logger.error(f"Error reading BOOTSTRAP.md: {e}")
        return None

    def _inject_bootstrap(self, message: str, bootstrap_content: str) -> str:
        """将 BOOTSTRAP 内容注入到消息中。
        
        格式：
        [BOOTSTRAP - 首次启动引导 - 必须执行]
        {bootstrap_content}
        [/BOOTSTRAP]
        
        用户消息: {message}
        """
        return f"""[BOOTSTRAP - 首次启动引导 - 必须执行]
以下是首次启动引导文件，你必须按照其中的指示完成身份设置。
完成引导后，请删除 workspace/BOOTSTRAP.md 文件。

{bootstrap_content}
[/BOOTSTRAP]

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
            
            # 检查 BOOTSTRAP.md 是否存在（每次都检查文件，不依赖内存状态）
            bootstrap_content = self._get_bootstrap_content()
            
            # 如果 BOOTSTRAP.md 存在，注入引导内容
            if bootstrap_content:
                message_content = self._inject_bootstrap(message_content, bootstrap_content)
                logger.info(f"Injected BOOTSTRAP for {msg.channel}:{msg.chat_id}")

            # 调用 iflow
            response = await self.adapter.chat(
                message=message_content,
                channel=msg.channel,
                chat_id=msg.chat_id,
                model=self.model,
            )

            # 发送最终响应
            if response:
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
        # 检查 BOOTSTRAP（每次都检查文件是否存在）
        bootstrap_content = self._get_bootstrap_content()
        
        message_content = message
        if bootstrap_content:
            message_content = self._inject_bootstrap(message, bootstrap_content)
            logger.info(f"Injected BOOTSTRAP for {channel}:{chat_id} (direct mode)")
        
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
