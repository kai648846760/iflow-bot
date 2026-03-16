from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol

from iflow_bot.bus.events import InboundMessage

if TYPE_CHECKING:
    from iflow_bot.engine.loop import AgentLoop


@dataclass(slots=True)
class CommandContext:
    loop: "AgentLoop"

    async def reply(self, content: str, *, streaming: bool | None = None) -> None:
        await self.loop._send_command_reply(self.current_message, content, streaming=streaming)

    async def dispatch_ralph_status(self, msg: InboundMessage) -> None:
        await self.loop._ralph_status(msg)

    @property
    def language(self) -> str:
        return self.loop._load_language_setting()

    def msg(self, key: str, **kwargs: object) -> str:
        return self.loop._msg(key, **kwargs)

    def build_help_text(self) -> str:
        return self.loop._build_help_text()

    def peek_command(self, content: str) -> tuple[str, list[str]]:
        return self.loop._peek_command(content)

    def ralph_status_text(self, chat_id: str) -> str:
        return self.loop._ralph_current_status_text(chat_id)

    def has_active_ralph(self, chat_id: str) -> bool:
        return self.loop._has_active_ralph(chat_id)

    def is_ralph_running_state(self, chat_id: str) -> bool:
        return self.loop._ralph_is_running_state(chat_id)

    def looks_like_status_query(self, content: str) -> bool:
        return self.loop._ralph_looks_like_status_query(content)

    current_message: InboundMessage


class ChatCommand(Protocol):
    name: str
    aliases: tuple[str, ...]

    async def handle(self, ctx: CommandContext, msg: InboundMessage, args: list[str]) -> bool: ...
