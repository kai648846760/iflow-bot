from __future__ import annotations

from iflow_bot.bus.events import InboundMessage
from iflow_bot.engine.commands.base import CommandContext


class RalphCommand:
    name = "/ralph"
    aliases = ()

    async def handle(self, ctx: CommandContext, msg: InboundMessage, args: list[str]) -> bool:
        loop = ctx.loop
        if not args:
            await ctx.reply(loop._msg("ralph_usage"))
            return True
        sub = args[0].lower()
        if sub == "approve":
            await loop._ralph_approve(msg)
            return True
        if sub == "stop":
            await loop._ralph_stop(msg)
            return True
        if sub == "status":
            await loop._ralph_status(msg)
            return True
        if sub in {"resume", "continue"}:
            await loop._ralph_resume(msg)
            return True
        if sub == "answer":
            answer_text = " ".join(args[1:]).strip()
            if not answer_text:
                await ctx.reply(loop._msg("ralph_missing_answer"))
                return True
            await loop._ralph_handle_answers(msg, answer_text)
            return True
        prompt = " ".join(args).strip()
        if not prompt:
            await ctx.reply(loop._msg("ralph_missing_prompt"))
            return True
        await loop._ralph_create(msg, prompt)
        return True
