from __future__ import annotations

from iflow_bot.bus.events import InboundMessage
from iflow_bot.engine.commands.base import CommandContext


class StatusCommand:
    name = "/status"
    aliases = ()

    async def handle(self, ctx: CommandContext, msg: InboundMessage, args: list[str]) -> bool:
        loop = ctx.loop
        adapter = loop.adapter
        mode = getattr(adapter, "mode", "cli")
        model = loop.model
        workspace = str(loop.workspace) if loop.workspace else "unknown"
        language = loop._load_language_setting()
        streaming = loop._msg("status_on", _lang=language) if loop.streaming else loop._msg("status_off", _lang=language)
        compression_count = "-"
        session_id = "-"
        est_tokens = "-"
        try:
            if mode == "stdio":
                stdio = getattr(adapter, "_stdio_adapter", None)
                if stdio is None and hasattr(adapter, "_get_stdio_adapter"):
                    stdio = await adapter._get_stdio_adapter()
                if stdio is not None:
                    info = stdio.get_session_status(msg.channel, msg.chat_id)
                    compression_count = str(info.get("compression_count", "-"))
                    session_id = info.get("session_id", "-")
                    est_tokens = info.get("estimated_tokens", "-")
        except Exception:
            pass
        lines = [
            loop._msg("status_mode", value=mode, _lang=language),
            loop._msg("status_model", value=model, _lang=language),
            loop._msg("status_streaming", value=streaming, _lang=language),
            loop._msg("status_language", value=language, _lang=language),
            loop._msg("status_workspace", value=workspace, _lang=language),
            loop._msg("status_session", value=session_id, _lang=language),
            loop._msg("status_est_tokens", value=est_tokens, _lang=language),
            loop._msg("status_compaction", value=compression_count, _lang=language),
        ]
        await ctx.reply("\n".join(lines))
        return True
