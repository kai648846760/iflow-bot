from __future__ import annotations

from iflow_bot.bus.events import InboundMessage
from iflow_bot.engine.commands.base import CommandContext


class NewSessionCommand:
    name = "/new"
    aliases = ("/start",)

    async def handle(self, ctx: CommandContext, msg: InboundMessage, args: list[str]) -> bool:
        return False


class CompactCommand:
    name = "/compact"
    aliases = ()

    async def handle(self, ctx: CommandContext, msg: InboundMessage, args: list[str]) -> bool:
        loop = ctx.loop
        try:
            if getattr(loop.adapter, "mode", "cli") == "stdio":
                stdio = await loop.adapter._get_stdio_adapter()
                ok, reason = await stdio.compact_session(msg.channel, msg.chat_id)
                if ok:
                    await ctx.reply(loop._msg("compact_ok"))
                else:
                    await ctx.reply(loop._msg("compact_fail", reason=reason))
            else:
                await ctx.reply(loop._msg("compact_unsupported"))
        except Exception as e:
            await ctx.reply(loop._msg("compact_error", error=e))
        return True
