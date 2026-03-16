from __future__ import annotations

from iflow_bot.bus.events import InboundMessage
from iflow_bot.config.loader import load_config, save_config
from iflow_bot.engine.commands.base import CommandContext


class ModelCommand:
    name = "/model"
    aliases = ()

    async def handle(self, ctx: CommandContext, msg: InboundMessage, args: list[str]) -> bool:
        if len(args) < 2 or args[0].lower() != "set":
            return False
        loop = ctx.loop
        new_model = args[1]
        cfg = load_config()
        if hasattr(cfg, "driver") and cfg.driver:
            cfg.driver.model = new_model
        save_config(cfg)
        loop.model = new_model
        try:
            loop.adapter.default_model = new_model
            if getattr(loop.adapter, "_stdio_adapter", None):
                loop.adapter._stdio_adapter.default_model = new_model
        except Exception:
            pass
        await ctx.reply(loop._msg("model_set", model=new_model))
        return True
