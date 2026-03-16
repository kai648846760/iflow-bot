from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path

from iflow_bot.utils.platform import prepare_subprocess_command, resolve_command


async def ensure_skillhub_cli() -> tuple[str | None, str | None, bool]:
    existing = resolve_command("skillhub")
    if existing:
        return existing, None, False

    install_cmd = [
        "bash",
        "-lc",
        "curl -fsSL https://skillhub-1388575217.cos.ap-guangzhou.myqcloud.com/install/install.sh | bash -s -- --cli-only",
    ]
    try:
        proc = await asyncio_subprocess_exec(*install_cmd)
        stdout, stderr = await proc.communicate()
        if proc.returncode != 0:
            err = (stderr or stdout or b"").decode("utf-8", errors="replace").strip()
            return None, err or "SkillHub CLI install failed", False
    except Exception as e:
        return None, str(e), False

    installed = resolve_command("skillhub")
    if installed:
        return installed, None, True

    home_bin = Path.home() / ".local" / "bin" / "skillhub"
    if home_bin.exists():
        return str(home_bin), None, True

    return None, "SkillHub CLI installed but executable not found", False


async def asyncio_subprocess_exec(*cmd: str):
    import asyncio
    prepared = prepare_subprocess_command(cmd)
    return await asyncio.create_subprocess_exec(
        *prepared,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env={**os.environ, "PATH": os.environ.get("PATH", "")},
    )


def format_skillhub_search_output(text: str) -> str:
    cleaned = text.strip()
    if not cleaned:
        return cleaned
    lines = [line.rstrip() for line in cleaned.splitlines() if line.strip()]
    if not lines:
        return cleaned
    if all(line.startswith(("-", "*")) or re.search(r"\bslug\b", line, re.I) for line in lines[:2]):
        return cleaned
    return cleaned


def format_skillhub_list_output(text: str) -> str:
    return text.strip()


def format_skillhub_install_output(text: str, slug: str) -> str:
    cleaned = text.strip()
    if cleaned:
        return cleaned
    return f"Installed {slug}" if slug else "Installed"
