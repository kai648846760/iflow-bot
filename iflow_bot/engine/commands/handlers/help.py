from __future__ import annotations

from iflow_bot.bus.events import InboundMessage
from iflow_bot.engine.commands.base import CommandContext


class HelpCommand:
    name = "/help"
    aliases = ("/h",)

    async def handle(self, ctx: CommandContext, msg: InboundMessage, args: list[str]) -> bool:
        await ctx.reply(ctx.build_help_text())
        return True
