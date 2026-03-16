from __future__ import annotations

from datetime import datetime

from iflow_bot.bus.events import InboundMessage
from iflow_bot.cron.service import CronService
from iflow_bot.cron.types import CronSchedule
from iflow_bot.engine.commands.base import CommandContext
from iflow_bot.utils.helpers import get_data_dir


class CronCommand:
    name = "/cron"
    aliases = ()

    async def handle(self, ctx: CommandContext, msg: InboundMessage, args: list[str]) -> bool:
        loop = ctx.loop
        store_path = get_data_dir() / "cron" / "jobs.json"
        cron = CronService(store_path)
        sub = (args[0].lower() if args else "help")
        if sub == "list":
            jobs = cron.list_jobs(include_disabled=True)
            if not jobs:
                await ctx.reply(loop._msg("cron_none"))
            else:
                lines = []
                for job in jobs:
                    sched = job.schedule.kind
                    if sched == "every":
                        sched = f"every {int(job.schedule.every_ms/1000)}s"
                    elif sched == "cron":
                        sched = f"cron {job.schedule.expr}"
                    elif sched == "at":
                        ts = job.schedule.at_ms / 1000 if job.schedule.at_ms else 0
                        sched = f"at {datetime.fromtimestamp(ts).isoformat(sep=' ')}"
                    lines.append(f"{job.id} | {job.name} | {sched} | enabled={job.enabled}")
                await ctx.reply(loop._msg("cron_list", jobs="\n".join(lines)))
            return True
        if sub == "delete" and len(args) >= 2:
            job_id = args[1]
            removed = cron.remove_job(job_id)
            await ctx.reply(loop._msg("cron_deleted") if removed else loop._msg("cron_not_found"))
            return True
        if sub == "add":
            opts = {}
            key = None
            for token in args[1:]:
                if token.startswith("--"):
                    key = token[2:]
                    opts[key] = ""
                elif key:
                    opts[key] = f"{opts[key]} {token}".strip()
            name = opts.get("name", "cron")
            message = opts.get("message")
            if not message:
                await ctx.reply(loop._msg("cron_missing_message"))
                return True
            tz = opts.get("tz")
            every = opts.get("every")
            cron_expr = opts.get("cron")
            at = opts.get("at")
            if sum(1 for x in [every, cron_expr, at] if x) != 1:
                await ctx.reply(loop._msg("cron_missing_schedule"))
                return True
            if every:
                schedule = CronSchedule(kind="every", every_ms=int(every) * 1000)
            elif cron_expr:
                schedule = CronSchedule(kind="cron", expr=cron_expr, tz=tz)
            else:
                when = datetime.fromisoformat(at)
                schedule = CronSchedule(kind="at", at_ms=int(when.timestamp() * 1000))
            channel = opts.get("channel")
            to = opts.get("to")
            deliver_raw = opts.get("deliver")
            if not channel and not to:
                channel = msg.channel
                to = msg.chat_id
                deliver = True if deliver_raw in {None, ""} else deliver_raw.lower() in {"1", "true", "yes"}
            else:
                deliver = True if deliver_raw is None else deliver_raw.lower() in {"1", "true", "yes"}
            job = cron.add_job(name=name, schedule=schedule, message=message, deliver=deliver, channel=channel, to=to, delete_after_run=False)
            await ctx.reply(loop._msg("cron_added", id=job.id))
            return True
        await ctx.reply(loop._msg("cron_usage"))
        return True
