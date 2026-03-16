from __future__ import annotations

import asyncio
import re

from iflow_bot.bus.events import InboundMessage
from iflow_bot.engine.commands.base import CommandContext
from .skills_support import ensure_skillhub_cli, format_skillhub_install_output, format_skillhub_list_output, format_skillhub_search_output
from iflow_bot.utils.platform import prepare_subprocess_command


class SkillsCommand:
    name = "/skills"
    aliases = ()

    async def handle(self, ctx: CommandContext, msg: InboundMessage, args: list[str]) -> bool:
        loop = ctx.loop
        if not args:
            await ctx.reply(loop._msg("skills_usage"))
            return True

        def _strip_ansi(text: str) -> str:
            if not text:
                return ""
            text = re.sub(r"\x1b\[[0-9;?]*[A-Za-z]", "", text)
            text = re.sub(r"\x1b\][^\x07]*(?:\x07|\x1b\\)", "", text)
            return text.strip()

        try:
            skillhub, install_err, auto_installed = await ensure_skillhub_cli()
            if not skillhub:
                await ctx.reply(loop._msg("skills_auto_install_fail", error=install_err or "unknown error", cmd="curl -fsSL https://skillhub-1388575217.cos.ap-guangzhou.myqcloud.com/install/install.sh | bash -s -- --cli-only"))
                return True
            sub = args[0].lower()
            if sub in {"find", "search"}:
                subcmd = "search"
                passthrough = args[1:]
            elif sub in {"add", "install"}:
                subcmd = "install"
                passthrough = args[1:]
            elif sub in {"list", "ls"}:
                subcmd = "list"
                passthrough = []
            elif sub in {"update", "upgrade"}:
                subcmd = "upgrade"
                passthrough = []
            elif sub in {"remove", "rm", "uninstall"}:
                if len(args) < 2:
                    await ctx.reply(loop._msg("skills_missing_slug"))
                    return True
                slug = args[1]
                skills_dir = loop.workspace / "skills"
                target = skills_dir / slug
                removed = False
                try:
                    if target.exists():
                        if target.is_dir():
                            import shutil as _shutil
                            _shutil.rmtree(target)
                        else:
                            target.unlink()
                        removed = True
                except Exception as e:
                    await ctx.reply(loop._msg("skills_remove_failed", error=e))
                    return True
                try:
                    from iflow_bot.utils.helpers import get_iflow_config_dir
                    iflow_target = get_iflow_config_dir() / "skills" / slug
                    if iflow_target.exists() and not iflow_target.is_symlink():
                        if iflow_target.is_dir():
                            import shutil as _shutil
                            _shutil.rmtree(iflow_target)
                        else:
                            iflow_target.unlink()
                except Exception:
                    pass
                await ctx.reply(loop._msg("skills_removed") if removed else loop._msg("skills_not_found"))
                return True
            else:
                subcmd = sub
                passthrough = args[1:]
            skills_dir = loop.workspace / "skills"
            skills_dir.mkdir(parents=True, exist_ok=True)
            cmdline = [skillhub, "--dir", str(skills_dir), "--skip-self-upgrade", subcmd] + passthrough
            prepared = prepare_subprocess_command(cmdline)
            proc = await asyncio.create_subprocess_exec(*prepared, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE, cwd=str(loop.workspace))
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=120)
            code = proc.returncode
            output = (stdout or b"").decode("utf-8", errors="replace").strip()
            err = (stderr or b"").decode("utf-8", errors="replace").strip()
            text = _strip_ansi(output or err or loop._msg("skills_no_output"))
            if subcmd == "search":
                text = format_skillhub_search_output(text)
            elif subcmd == "list":
                text = format_skillhub_list_output(text)
            elif subcmd == "install":
                slug = passthrough[0] if passthrough else ""
                text = format_skillhub_install_output(text, slug)
            if auto_installed:
                text = loop._msg("skills_auto_installed", text=text)
            if len(text) > 4000:
                text = text[:4000] + "\n... (truncated)"
            await ctx.reply(text)
            if code == 0 and subcmd in {"install", "upgrade"}:
                try:
                    from iflow_bot.utils.helpers import sync_iflow_skills_dir
                    sync_iflow_skills_dir(loop.workspace)
                except Exception:
                    pass
        except Exception as e:
            await ctx.reply(loop._msg("skills_exec_failed", error=e))
        return True
