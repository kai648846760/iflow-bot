"""iflow-bot - Multi-channel AI Assistant powered by iflow CLI."""

__version__ = "0.4.5"
__logo__ = "🤖"

from iflow_bot.engine.adapter import IFlowAdapter
from iflow_bot.bus.queue import MessageBus
from iflow_bot.bus.events import InboundMessage, OutboundMessage

__all__ = [
    "__version__",
    "__logo__",
    "IFlowAdapter",
    "MessageBus",
    "InboundMessage",
    "OutboundMessage",
]
