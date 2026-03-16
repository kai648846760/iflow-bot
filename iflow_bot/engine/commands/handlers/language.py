from __future__ import annotations

import json

from iflow_bot.bus.events import InboundMessage
from iflow_bot.engine.commands.base import CommandContext


class LanguageCommand:
    name = "/language"
    aliases = ()

    async def handle(self, ctx: CommandContext, msg: InboundMessage, args: list[str]) -> bool:
        if not args:
            return False
        loop = ctx.loop
        lang = loop._normalize_language_setting(args[0])
        if not lang:
            await ctx.reply(loop._msg("language_set_failed", error="unsupported language"))
            return True
        settings_path = loop.workspace / ".iflow" / "settings.json"
        try:
            if settings_path.exists():
                data = json.loads(settings_path.read_text(encoding="utf-8"))
            else:
                data = {}
            data["language"] = lang
            settings_path.parent.mkdir(parents=True, exist_ok=True)
            settings_path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
            await ctx.reply(loop._msg("language_set_ok", lang=lang))
        except Exception as e:
            await ctx.reply(loop._msg("language_set_failed", error=e))
        return True
