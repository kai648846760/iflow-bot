"""Message bus for inter-component communication."""

import asyncio
from typing import Optional

from loguru import logger

from iflow_bot.bus.events import InboundMessage, OutboundMessage
from iflow_bot.session.recorder import ChannelRecorder, get_recorder


class MessageBus:
    """
    Message bus for communication between Channels and Agent.
    
    The bus provides two queues:
    - Inbound: Messages from chat platforms → Agent
    - Outbound: Messages from Agent → chat platforms
    
    Usage:
        bus = MessageBus()
        
        # Channel publishes inbound message
        await bus.publish_inbound(InboundMessage(...))
        
        # Agent consumes inbound message
        msg = await bus.consume_inbound()
        
        # Agent publishes outbound message
        await bus.publish_outbound(OutboundMessage(...))
        
        # Channel consumes outbound message
        msg = await bus.consume_outbound()
    """
    
    def __init__(self, max_size: int = 100, recorder: Optional[ChannelRecorder] = None):
        """
        Initialize the message bus.
        
        Args:
            max_size: Maximum queue size for each direction.
            recorder: Optional ChannelRecorder instance. If not provided, uses global recorder.
        """
        self._inbound: asyncio.Queue[InboundMessage] = asyncio.Queue(maxsize=max_size)
        self._outbound: asyncio.Queue[OutboundMessage] = asyncio.Queue(maxsize=max_size)
        self._running = True
        self._recorder = recorder
    
    def _get_recorder(self) -> Optional[ChannelRecorder]:
        """Get the recorder instance."""
        if self._recorder is not None:
            return self._recorder
        return get_recorder()
    
    async def publish_inbound(self, msg: InboundMessage) -> None:
        """
        Publish an inbound message.
        
        Args:
            msg: The inbound message to publish.
        """
        if not self._running:
            logger.warning("Bus is stopped, dropping inbound message")
            return
        
        try:
            self._inbound.put_nowait(msg)
            logger.debug(f"Published inbound message from {msg.channel}:{msg.chat_id}")
            
            # Record the inbound message
            recorder = self._get_recorder()
            if recorder:
                recorder.record_inbound(msg)
        except asyncio.QueueFull:
            logger.warning("Inbound queue full, dropping message")
    
    async def consume_inbound(self, timeout: Optional[float] = None) -> InboundMessage:
        """
        Consume an inbound message.
        
        Args:
            timeout: Optional timeout in seconds.
        
        Returns:
            The next inbound message.
        
        Raises:
            asyncio.TimeoutError: If timeout is reached.
        """
        if timeout is not None:
            return await asyncio.wait_for(self._inbound.get(), timeout=timeout)
        return await self._inbound.get()
    
    async def publish_outbound(self, msg: OutboundMessage) -> None:
        """
        Publish an outbound message.
        
        Args:
            msg: The outbound message to publish.
        """
        if not self._running:
            logger.warning("Bus is stopped, dropping outbound message")
            return
        
        try:
            self._outbound.put_nowait(msg)
            logger.debug(f"Published outbound message to {msg.channel}:{msg.chat_id}")
            
            # Record the outbound message
            recorder = self._get_recorder()
            if recorder:
                recorder.record_outbound(msg)
        except asyncio.QueueFull:
            logger.warning("Outbound queue full, dropping message")
    
    async def consume_outbound(self, timeout: Optional[float] = None) -> OutboundMessage:
        """
        Consume an outbound message.
        
        Args:
            timeout: Optional timeout in seconds.
        
        Returns:
            The next outbound message.
        
        Raises:
            asyncio.TimeoutError: If timeout is reached.
        """
        if timeout is not None:
            return await asyncio.wait_for(self._outbound.get(), timeout=timeout)
        return await self._outbound.get()
    
    def stop(self) -> None:
        """Stop the bus and reject new messages."""
        self._running = False
        logger.info("Message bus stopped")
    
    def start(self) -> None:
        """Start the bus."""
        self._running = True
        logger.info("Message bus started")
    
    @property
    def is_running(self) -> bool:
        """Check if the bus is running."""
        return self._running
    
    @property
    def inbound_size(self) -> int:
        """Get the current size of the inbound queue."""
        return self._inbound.qsize()
    
    @property
    def outbound_size(self) -> int:
        """Get the current size of the outbound queue."""
        return self._outbound.qsize()
    
    def clear(self) -> None:
        """Clear all pending messages."""
        while not self._inbound.empty():
            try:
                self._inbound.get_nowait()
            except asyncio.QueueEmpty:
                break
        
        while not self._outbound.empty():
            try:
                self._outbound.get_nowait()
            except asyncio.QueueEmpty:
                break
        
        logger.info("Message bus cleared")

    def task_done_inbound(self) -> None:
        """标记入站消息处理完成。"""
        try:
            self._inbound.task_done()
        except ValueError:
            pass  # 如果队列为空则忽略

    def task_done_outbound(self) -> None:
        """标记出站消息处理完成。"""
        try:
            self._outbound.task_done()
        except ValueError:
            pass  # 如果队列为空则忽略