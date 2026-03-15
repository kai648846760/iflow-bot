"""Agent Loop - 核心消息处理循环。

BOOTSTRAP 引导机制：
- 每次处理消息前检查 workspace/BOOTSTRAP.md 是否存在
- 如果存在，将内容作为系统前缀注入到消息中
- AI 会自动执行引导流程
- 引导完成后 AI 会删除 BOOTSTRAP.md

流式输出支持：
- ACP 模式下支持实时流式输出到渠道
- 消息块会实时发送到支持流式的渠道（如 Telegram）

文件回传支持 (from feishu-iflow-bridge)：
- 使用 ResultAnalyzer 分析 iflow 输出
- 自动检测输出中生成的文件路径（图片/音频/视频/文档）
- 通过 OutboundMessage.media 字段将文件附加到响应中
- 支持文件回传的渠道（如飞书）会自动上传并发送这些文件
"""

from __future__ import annotations

import asyncio
import contextlib
import inspect
import json
import os
import random
import re
import shutil
import time
import uuid
from pathlib import Path
from typing import TYPE_CHECKING, Optional

from loguru import logger

from iflow_bot.bus import MessageBus, InboundMessage, OutboundMessage
from iflow_bot.engine.adapter import IFlowAdapter
from iflow_bot.engine.analyzer import result_analyzer, AnalysisResult
from iflow_bot.engine.stdio_acp import ACPResponse, StdioACPTimeoutError
from iflow_bot.session.recorder import get_recorder

if TYPE_CHECKING:
    from iflow_bot.channels.manager import ChannelManager


# 支持流式输出的渠道列表
STREAMING_CHANNELS = {"telegram", "discord", "slack", "dingtalk", "qq", "feishu"}

# 流式输出缓冲区大小范围（字符数）
STREAM_BUFFER_MIN = 10
STREAM_BUFFER_MAX = 25


class AgentLoop:
    """Agent 主循环 - 处理来自各渠道的消息。

    工作流程:
    1. 检查 BOOTSTRAP.md 是否存在（首次启动引导）
    2. 从消息总线获取入站消息
    3. 通过 SessionMappingManager 获取/创建会话 ID
    4. 调用 IFlowAdapter 发送消息到 iflow（支持流式）
    5. 使用 ResultAnalyzer 分析响应（检测文件、状态等）
    6. 将响应和检测到的文件发布到消息总线
    """

    def __init__(
        self,
        bus: MessageBus,
        adapter: IFlowAdapter,
        model: str = "kimi-k2.5",
        streaming: bool = True,
        channel_manager: Optional["ChannelManager"] = None,
    ):
        self.bus = bus
        self.adapter = adapter
        self.model = model
        self.streaming = streaming
        self.workspace = adapter.workspace
        self.channel_manager = channel_manager

        self._running = False
        self._task: Optional[asyncio.Task] = None
        
        # 流式消息缓冲区
        self._stream_buffers: dict[str, str] = {}
        self._stream_tasks: dict[str, asyncio.Task] = {}
        self._ralph_tasks: dict[str, asyncio.Task] = {}
        self._ralph_active_sessions: dict[str, str] = {}
        
        # P3: 每用户并发锁，确保同一用户的消息串行处理，避免会话状态混乱
        self._user_locks: dict[str, asyncio.Lock] = {}

        logger.info(f"AgentLoop initialized with model={model}, workspace={self.workspace}, streaming={streaming}")

    def _get_new_conversation_message(self) -> str:
        """获取新会话提示文案（可配置）。"""
        if self.channel_manager and getattr(self.channel_manager, "config", None):
            try:
                messages = getattr(self.channel_manager.config, "messages", None)
                if messages and getattr(messages, "new_conversation", None):
                    configured = str(messages.new_conversation).strip()
                    default_messages = {
                        self._msg("new_conversation", _lang="zh-CN"),
                        self._msg("new_conversation", _lang="en-US"),
                    }
                    if configured and configured not in default_messages:
                        return configured
            except Exception:
                pass
        return self._msg("new_conversation")

    def _split_command_message(self, content: str, max_len: int = 1800) -> list[str]:
        if not content:
            return [""]
        if len(content) <= max_len:
            return [content]

        parts: list[str] = []
        buffer = ""
        blocks = content.split("\n\n")
        for block in blocks:
            if not block:
                continue
            sep = "\n\n" if buffer else ""
            candidate = f"{buffer}{sep}{block}" if buffer else block
            if len(candidate) <= max_len:
                buffer = candidate
                continue
            if buffer:
                parts.append(buffer)
                buffer = ""
            if len(block) <= max_len:
                buffer = block
                continue
            # Split very long block by line boundaries first to preserve readability.
            line_buffer = ""
            for line in block.splitlines():
                sep = "\n" if line_buffer else ""
                candidate_line = f"{line_buffer}{sep}{line}" if line_buffer else line
                if len(candidate_line) <= max_len:
                    line_buffer = candidate_line
                    continue
                if line_buffer:
                    parts.append(line_buffer)
                    line_buffer = ""
                if len(line) <= max_len:
                    line_buffer = line
                    continue
                for idx in range(0, len(line), max_len):
                    parts.append(line[idx:idx + max_len])
            if line_buffer:
                parts.append(line_buffer)
        if buffer:
            parts.append(buffer)
        return parts or [content]

    async def _send_streaming_content(
        self,
        channel: str,
        chat_id: str,
        content: str,
        metadata: Optional[dict] = None,
    ) -> None:
        if not content:
            return
        meta = dict(metadata or {})
        chunks = self._split_command_message(content, max_len=800)
        buffer = ""

        dingtalk_channel = None
        if channel == "dingtalk" and self.channel_manager:
            dingtalk_channel = self.channel_manager.get_channel("dingtalk")
            if dingtalk_channel and hasattr(dingtalk_channel, "start_streaming"):
                await dingtalk_channel.start_streaming(chat_id)

        for idx, chunk in enumerate(chunks):
            buffer += chunk
            if channel == "dingtalk" and dingtalk_channel and hasattr(dingtalk_channel, "handle_streaming_chunk"):
                await dingtalk_channel.handle_streaming_chunk(chat_id, buffer, is_final=False)
            else:
                await self._emit_outbound(OutboundMessage(
                    channel=channel,
                    chat_id=chat_id,
                    content=buffer,
                    metadata={
                        "_progress": True,
                        "_streaming": True,
                        **meta,
                    },
                ), prefer_direct=True)
            if idx < len(chunks) - 1:
                await asyncio.sleep(0.2)

        if channel == "dingtalk" and dingtalk_channel and hasattr(dingtalk_channel, "handle_streaming_chunk"):
            await dingtalk_channel.handle_streaming_chunk(chat_id, buffer, is_final=True)
        else:
            await self._emit_outbound(OutboundMessage(
                channel=channel,
                chat_id=chat_id,
                content="",
                metadata={
                    "_streaming_end": True,
                    **meta,
                },
            ), prefer_direct=True)

    async def _emit_outbound(self, msg: OutboundMessage, prefer_direct: bool = False) -> None:
        logger.debug(
            "Emit outbound: channel={} chat_id={} prefer_direct={} content_len={} flags={}",
            msg.channel,
            msg.chat_id,
            prefer_direct,
            len(msg.content or ""),
            sorted((msg.metadata or {}).keys()),
        )
        if prefer_direct and self.channel_manager:
            channel = self.channel_manager.get_channel(msg.channel)
            if channel and channel.is_running:
                logger.debug("Emit outbound direct send: {}:{}", msg.channel, msg.chat_id)
                await self.channel_manager.send_to(msg.channel, msg)
                recorder = get_recorder()
                if recorder:
                    recorder.record_outbound(msg)
                logger.debug("Emit outbound direct send complete: {}:{}", msg.channel, msg.chat_id)
                return
            logger.debug("Emit outbound fell back to bus: channel_found={} running={}", bool(channel), bool(channel and channel.is_running))
        await self.bus.publish_outbound(msg)

    async def _send_command_reply(
        self,
        msg: InboundMessage,
        content: str,
        streaming: Optional[bool] = None,
    ) -> None:
        supports_streaming = self.streaming and msg.channel in STREAMING_CHANNELS and msg.channel != "qq"
        if streaming is None:
            streaming = supports_streaming and len(content) > 600

        if streaming and supports_streaming:
            await self._send_streaming_content(
                msg.channel,
                msg.chat_id,
                content,
                self._build_reply_metadata(msg),
            )
            return

        chunks = self._split_command_message(content)
        for idx, chunk in enumerate(chunks):
            await self._emit_outbound(OutboundMessage(
                channel=msg.channel,
                chat_id=msg.chat_id,
                content=chunk,
                metadata=self._build_reply_metadata(msg),
            ), prefer_direct=True)
            if idx < len(chunks) - 1:
                await asyncio.sleep(0.2)

    def _build_help_text(self) -> str:
        return self._msg("help_text")

    async def _handle_slash_command(self, msg: InboundMessage) -> bool:
        import shlex
        from datetime import datetime
        from iflow_bot.config.loader import load_config, save_config
        from iflow_bot.cron.service import CronService
        from iflow_bot.cron.types import CronSchedule
        from iflow_bot.utils.platform import prepare_subprocess_command
        import re

        def _strip_ansi(text: str) -> str:
            if not text:
                return ""
            # Remove ANSI escape sequences and other terminal control codes
            text = re.sub(r"\x1b\[[0-9;?]*[A-Za-z]", "", text)
            text = re.sub(r"\x1b\][^\x07]*\x07", "", text)  # OSC
            text = re.sub(r"\x1b\\", "", text)
            # Remove other non-printable control chars except \n and \t
            text = "".join(ch for ch in text if ch == "\n" or ch == "\t" or ord(ch) >= 32)
            return text.strip()

        def _find_skillhub_binary() -> Optional[str]:
            import shutil

            candidates = [
                shutil.which("skillhub"),
                str(Path.home() / ".local" / "bin" / "skillhub"),
            ]
            for item in candidates:
                if item and Path(item).exists():
                    return item
            return None

        async def _ensure_skillhub_cli() -> tuple[Optional[str], Optional[str], bool]:
            path = _find_skillhub_binary()
            if path:
                return path, None, False

            install_cmd = (
                "curl -fsSL https://skillhub-1388575217.cos.ap-guangzhou.myqcloud.com/install/install.sh "
                "| bash -s -- --cli-only"
            )
            try:
                proc = await asyncio.create_subprocess_exec(
                    "bash",
                    "-lc",
                    install_cmd,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                )
                stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=180)
                if proc.returncode != 0:
                    err = (stderr or stdout or b"").decode("utf-8", errors="replace").strip()
                    return None, err or self._msg("skillhub_install_failed"), False
            except Exception as e:
                return None, str(e), False

            path = _find_skillhub_binary()
            if not path:
                return None, self._msg("skillhub_executable_missing"), False
            return path, None, True

        def _format_skillhub_search_output(text: str) -> str:
            if not text:
                return text
            lines = [line.rstrip() for line in text.splitlines() if line.strip()]
            entries: list[dict[str, str]] = []
            current: dict[str, str] | None = None
            for line in lines:
                if line.startswith("You can use"):
                    continue
                if not line.startswith(" "):
                    if current:
                        entries.append(current)
                    parts = line.split(None, 1)
                    slug = parts[0].strip()
                    title = parts[1].strip() if len(parts) > 1 else ""
                    current = {"slug": slug, "title": title, "desc": "", "version": ""}
                else:
                    if not current:
                        continue
                    item = line.strip()
                    if item.startswith("- "):
                        content = item[2:].strip()
                        if content.startswith("version:"):
                            current["version"] = content.replace("version:", "").strip()
                        elif not current["desc"]:
                            current["desc"] = content
            if current:
                entries.append(current)
            if not entries:
                return text
            formatted = [self._msg("skills_search_header")]
            for idx, entry in enumerate(entries, start=1):
                if entry["title"]:
                    title = (
                        f" ({entry['title']})"
                        if self._is_english(self._load_language_setting())
                        else f"（{entry['title']}）"
                    )
                else:
                    title = ""
                ver = f" v{entry['version']}" if entry["version"] else ""
                formatted.append(f"[{idx}] {entry['slug']}{title}{ver}")
                if entry["desc"]:
                    formatted.append(f"    {entry['desc']}")
                formatted.append(f"    /skills add {entry['slug']}")
            return "\n".join(formatted)

        def _format_skillhub_list_output(text: str) -> str:
            if not text:
                return self._msg("skills_none_installed")
            if "No installed skills." in text:
                return self._msg("skills_none_installed")
            lines = [line.strip() for line in text.splitlines() if line.strip()]
            formatted = [self._msg("skills_installed_header")]
            for line in lines:
                formatted.append(f"- {line}")
            return "\n".join(formatted)

        def _format_skillhub_install_output(text: str, slug: str) -> str:
            m = re.search(r"Installed:\\s*([^\\s]+)\\s*->\\s*(.+)$", text, re.M)
            if m:
                installed = m.group(1).strip()
                path = m.group(2).strip()
                label = self._msg("skills_install_path")
                return self._msg("skills_installed_path", slug=installed, label=label, path=path)
            if text:
                return text
            return self._msg("skills_installed_single", slug=slug)

        raw = (msg.content or "").strip()
        if not raw.startswith("/") and raw.lstrip().startswith("@") and "/" in raw:
            raw = raw[raw.find("/"):]
        if not raw.startswith("/"):
            return False

        try:
            parts = shlex.split(raw)
        except Exception:
            parts = raw.split()

        if not parts:
            return False

        cmd = parts[0].lower()
        if "@" in cmd:
            cmd = cmd.split("@", 1)[0]
        args = parts[1:]

        if cmd in {"/help"}:
            await self._send_command_reply(msg, self._build_help_text())
            return True

        if cmd in {"/new", "/start"}:
            return False

        if cmd == "/status":
            adapter = self.adapter
            mode = getattr(adapter, "mode", "cli")
            model = self.model
            workspace = str(self.workspace) if self.workspace else "unknown"
            language = self._load_language_setting()
            streaming = self._msg("status_on", _lang=language) if self.streaming else self._msg("status_off", _lang=language)
            compression_count = "-"
            session_id = "-"
            est_tokens = "-"

            try:
                if mode == "stdio":
                    stdio = await adapter._get_stdio_adapter()
                    info = stdio.get_session_status(msg.channel, msg.chat_id)
                    compression_count = str(info.get("compression_count", "-"))
                    session_id = info.get("session_id", "-")
                    est_tokens = info.get("estimated_tokens", "-")
            except Exception:
                pass

            lines = [
                self._msg("status_mode", value=mode, _lang=language),
                self._msg("status_model", value=model, _lang=language),
                self._msg("status_streaming", value=streaming, _lang=language),
                self._msg("status_language", value=language, _lang=language),
                self._msg("status_workspace", value=workspace, _lang=language),
                self._msg("status_session", value=session_id, _lang=language),
                self._msg("status_est_tokens", value=est_tokens, _lang=language),
                self._msg("status_compaction", value=compression_count, _lang=language),
            ]

            await self._send_command_reply(msg, "\n".join(lines))
            return True

        if cmd == "/compact":
            try:
                if getattr(self.adapter, "mode", "cli") == "stdio":
                    stdio = await self.adapter._get_stdio_adapter()
                    ok, reason = await stdio.compact_session(msg.channel, msg.chat_id)
                    if ok:
                        await self._send_command_reply(msg, self._msg("compact_ok"))
                    else:
                        await self._send_command_reply(msg, self._msg("compact_fail", reason=reason))
                else:
                    await self._send_command_reply(msg, self._msg("compact_unsupported"))
            except Exception as e:
                await self._send_command_reply(msg, self._msg("compact_error", error=e))
            return True

        if cmd == "/model" and len(args) >= 2 and args[0].lower() == "set":
            new_model = args[1]
            cfg = load_config()
            if hasattr(cfg, "driver") and cfg.driver:
                cfg.driver.model = new_model
            save_config(cfg)
            self.model = new_model
            try:
                self.adapter.default_model = new_model
                if getattr(self.adapter, "_stdio_adapter", None):
                    self.adapter._stdio_adapter.default_model = new_model
            except Exception:
                pass
            await self._send_command_reply(msg, self._msg("model_set", model=new_model))
            return True

        if cmd == "/cron":
            from iflow_bot.utils.helpers import get_data_dir
            store_path = get_data_dir() / "cron" / "jobs.json"
            cron = CronService(store_path)
            sub = (args[0].lower() if args else "help")

            if sub == "list":
                jobs = cron.list_jobs(include_disabled=True)
                if not jobs:
                    await self._send_command_reply(msg, self._msg("cron_none"))
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
                    await self._send_command_reply(msg, self._msg("cron_list", jobs="\n".join(lines)))
                return True

            if sub == "delete" and len(args) >= 2:
                job_id = args[1]
                removed = cron.remove_job(job_id)
                await self._send_command_reply(
                    msg,
                    self._msg("cron_deleted") if removed else self._msg("cron_not_found"),
                )
                return True

            if sub == "add":
                # 简易参数解析：--name/--message/--every/--cron/--at/--tz/--channel/--to/--deliver
                opts = {}
                key = None
                for token in args[1:]:
                    if token.startswith("--"):
                        key = token[2:]
                        opts[key] = ""
                    else:
                        if key:
                            if opts[key]:
                                opts[key] += " " + token
                            else:
                                opts[key] = token
                name = opts.get("name", "cron")
                message = opts.get("message")
                if not message:
                    await self._send_command_reply(msg, self._msg("cron_missing_message"))
                    return True
                tz = opts.get("tz")
                every = opts.get("every")
                cron_expr = opts.get("cron")
                at = opts.get("at")
                if sum(1 for x in [every, cron_expr, at] if x) != 1:
                    await self._send_command_reply(msg, self._msg("cron_missing_schedule"))
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
                job = cron.add_job(
                    name=name,
                    schedule=schedule,
                    message=message,
                    deliver=deliver,
                    channel=channel,
                    to=to,
                    delete_after_run=False,
                )
                await self._send_command_reply(msg, self._msg("cron_added", id=job.id))
                return True

            await self._send_command_reply(msg, self._msg("cron_usage"))
            return True

        if cmd == "/ralph":
            if not args:
                await self._send_command_reply(msg, self._msg("ralph_usage"))
                return True

            sub = args[0].lower()
            if sub == "approve":
                await self._ralph_approve(msg)
                return True
            if sub == "stop":
                await self._ralph_stop(msg)
                return True
            if sub == "status":
                await self._ralph_status(msg)
                return True
            if sub in {"resume", "continue"}:
                await self._ralph_resume(msg)
                return True
            if sub == "answer":
                answer_text = " ".join(args[1:]).strip()
                if not answer_text:
                    await self._send_command_reply(msg, self._msg("ralph_missing_answer"))
                    return True
                await self._ralph_handle_answers(msg, answer_text)
                return True

            prompt = " ".join(args).strip()
            if not prompt:
                await self._send_command_reply(msg, self._msg("ralph_missing_prompt"))
                return True
            await self._ralph_create(msg, prompt)
            return True

        if cmd == "/skills":
            try:
                if not args:
                    await self._send_command_reply(msg, self._msg("skills_usage"))
                    return True

                skillhub, install_err, auto_installed = await _ensure_skillhub_cli()
                if not skillhub:
                    await self._send_command_reply(
                        msg,
                        self._msg(
                            "skills_auto_install_fail",
                            error=install_err or "unknown error",
                            cmd="curl -fsSL https://skillhub-1388575217.cos.ap-guangzhou.myqcloud.com/install/install.sh | bash -s -- --cli-only",
                        ),
                    )
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
                        await self._send_command_reply(msg, self._msg("skills_missing_slug"))
                        return True
                    slug = args[1]
                    skills_dir = self.workspace / "skills"
                    target = skills_dir / slug
                    removed = False
                    try:
                        if target.exists():
                            if target.is_dir():
                                import shutil
                                shutil.rmtree(target)
                            else:
                                target.unlink()
                            removed = True
                    except Exception as e:
                        await self._send_command_reply(msg, self._msg("skills_remove_failed", error=e))
                        return True
                    try:
                        from iflow_bot.utils.helpers import get_iflow_config_dir
                        iflow_target = get_iflow_config_dir() / "skills" / slug
                        if iflow_target.exists() and not iflow_target.is_symlink():
                            if iflow_target.is_dir():
                                import shutil
                                shutil.rmtree(iflow_target)
                            else:
                                iflow_target.unlink()
                    except Exception:
                        pass
                    await self._send_command_reply(
                        msg,
                        self._msg("skills_removed") if removed else self._msg("skills_not_found"),
                    )
                    return True
                else:
                    subcmd = sub
                    passthrough = args[1:]

                skills_dir = self.workspace / "skills"
                skills_dir.mkdir(parents=True, exist_ok=True)
                cmdline = [
                    skillhub,
                    "--dir",
                    str(skills_dir),
                    "--skip-self-upgrade",
                    subcmd,
                ] + passthrough
                prepared = prepare_subprocess_command(cmdline)
                proc = await asyncio.create_subprocess_exec(
                    *prepared,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                    cwd=str(self.workspace),
                )
                stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=120)
                code = proc.returncode
                output = (stdout or b"").decode("utf-8", errors="replace").strip()
                err = (stderr or b"").decode("utf-8", errors="replace").strip()
                text = _strip_ansi(output or err or self._msg("skills_no_output"))

                if subcmd == "search":
                    text = _format_skillhub_search_output(text)
                elif subcmd == "list":
                    text = _format_skillhub_list_output(text)
                elif subcmd == "install":
                    slug = passthrough[0] if passthrough else ""
                    text = _format_skillhub_install_output(text, slug)

                if auto_installed:
                    text = self._msg("skills_auto_installed", text=text)

                if len(text) > 4000:
                    text = text[:4000] + "\n... (truncated)"
                await self._send_command_reply(msg, text)

                if code == 0 and subcmd in {"install", "upgrade"}:
                    try:
                        from iflow_bot.utils.helpers import sync_iflow_skills_dir
                        sync_iflow_skills_dir(self.workspace)
                    except Exception:
                        pass
            except Exception as e:
                await self._send_command_reply(msg, self._msg("skills_exec_failed", error=e))
            return True

        if cmd == "/language" and args:
            lang = args[0]
            settings_path = self.workspace / ".iflow" / "settings.json"
            try:
                if settings_path.exists():
                    import json
                    data = json.loads(settings_path.read_text(encoding="utf-8"))
                else:
                    data = {}
                data["language"] = lang
                settings_path.parent.mkdir(parents=True, exist_ok=True)
                settings_path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
                await self._send_command_reply(msg, self._msg("language_set_ok", lang=lang))
            except Exception as e:
                await self._send_command_reply(msg, self._msg("language_set_failed", error=e))
            return True

        return False

    def _get_bootstrap_content(self) -> tuple[Optional[str], bool]:
        """读取引导内容。
        
        Returns:
            tuple: (内容, 是否是 BOOTSTRAP)
            - 如果 BOOTSTRAP.md 存在，返回 (BOOTSTRAP内容, True)
            - 否则如果 AGENTS.md 存在，返回 (AGENTS内容, False)
            - 都不存在，返回 (None, False)
        """
        # stdio/acp 模式下，AGENTS 通过 session system_prompt 注入，避免每条消息重复注入
        inline_agents = getattr(self.adapter, "mode", "cli") == "cli"

        # 优先检查 BOOTSTRAP.md
        bootstrap_file = self.workspace / "BOOTSTRAP.md"
        if bootstrap_file.exists():
            try:
                content = bootstrap_file.read_text(encoding="utf-8")
                logger.info("BOOTSTRAP.md detected - will inject bootstrap instructions")
                return content, True
            except Exception as e:
                logger.error(f"Error reading BOOTSTRAP.md: {e}")
        
        # 否则注入 AGENTS.md
        if inline_agents:
            agents_file = self.workspace / "AGENTS.md"
            if agents_file.exists():
                try:
                    content = agents_file.read_text(encoding="utf-8")
                    logger.debug("AGENTS.md detected - will inject agents context")
                    return content, False
                except Exception as e:
                    logger.error(f"Error reading AGENTS.md: {e}")
        
        return None, False

    def _inject_bootstrap(self, message: str, bootstrap_content: str, is_bootstrap: bool = True) -> str:
        """将引导内容注入到消息中。"""
        if is_bootstrap:
            return f"""[BOOTSTRAP - 首次启动引导 - 必须执行]
以下是首次启动引导文件，你必须按照其中的指示完成身份设置。
完成引导后，删除 workspace/BOOTSTRAP.md 文件,删除后只需要告诉用户已完成身份设置即可，无需告诉用户关于 BOOTSTRAP.md 文件的任何信息。

{bootstrap_content}
[/BOOTSTRAP]

用户消息: {message}"""
        else:
            return f"""[AGENTS - 工作空间指南]
以下是当前工作空间的行为指南，请严格遵循。

{bootstrap_content}
[/AGENTS]

SOUL.md - Who You Are（你的灵魂）定义了你是谁，你的性格、特点、行为准则等核心信息。
IDENTITY.md - Your Identity（你的身份）定义了你的具体身份信息，如名字、年龄、职业、兴趣爱好等。
USERY.md - User Identity（用户身份）定义了用户的具体身份信息，如名字、年龄、职业、兴趣爱好等。
TOOLS.md - Your Tools（你的工具）定义了你可以使用的工具列表，包括每个工具的名称、功能描述、使用方法等, 每次学会一个工具，你便要主动更新该文件。

用户消息: {message}"""

    def _load_language_setting(self) -> str:
        settings_path = self.workspace / ".iflow" / "settings.json"
        if settings_path.exists():
            try:
                data = json.loads(settings_path.read_text(encoding="utf-8"))
                lang = (data.get("language") or "").strip()
                if lang:
                    return lang
            except Exception:
                pass
        return "zh-CN"

    def _format_language_policy(self, lang: str) -> str:
        key = (lang or "").lower()
        if key.startswith("zh"):
            return "回复必须严格使用中文（简体），不要混用其他语言。"
        if key.startswith("en"):
            return "Respond strictly in English only. Do not mix other languages."
        return f"Respond strictly in {lang} only. Do not mix other languages."

    def _is_english(self, lang: str) -> bool:
        return (lang or "").lower().startswith("en")

    def _msg(self, key: str, **kwargs: object) -> str:
        lang = kwargs.pop("_lang", None) or self._load_language_setting()
        is_en = self._is_english(lang)
        table = {
            "help_text": {
                "zh": (
                    "可用命令：\n"
                    "/status  查看当前状态\n"
                    "/new     开启新会话\n"
                    "/compact 手动压缩会话\n"
                    "/model set <name>  修改模型\n"
                    "/cron list | /cron add | /cron delete <id>\n"
                    "/skills find|add|list|remove|update  管理 Skills（SkillHub）\n"
                    "/ralph \"prompt\"  生成 PRD（会先提澄清问题）\n"
                    "/ralph answer <回复> | /ralph approve | /ralph stop | /ralph resume | /ralph status\n"
                    "/language <en-US|zh-CN>  设置语言\n"
                    "/help    查看帮助\n"
                ),
                "en": (
                    "Available commands:\n"
                    "/status  Show status\n"
                    "/new     Start a new session\n"
                    "/compact Compact session\n"
                    "/model set <name>  Set model\n"
                    "/cron list | /cron add | /cron delete <id>\n"
                    "/skills find|add|list|remove|update  Manage Skills (SkillHub)\n"
                    "/ralph \"prompt\"  Generate PRD (asks clarifying questions first)\n"
                    "/ralph answer <text> | /ralph approve | /ralph stop | /ralph resume | /ralph status\n"
                    "/language <en-US|zh-CN>  Set language\n"
                    "/help    Show help\n"
                ),
            },
            "status_on": {"zh": "开启", "en": "on"},
            "status_off": {"zh": "关闭", "en": "off"},
            "status_mode": {"zh": "状态: {value}", "en": "Mode: {value}"},
            "status_model": {"zh": "模型: {value}", "en": "Model: {value}"},
            "status_streaming": {"zh": "流式: {value}", "en": "Streaming: {value}"},
            "status_language": {"zh": "语言: {value}", "en": "Language: {value}"},
            "status_workspace": {"zh": "workspace: {value}", "en": "Workspace: {value}"},
            "status_session": {"zh": "session: {value}", "en": "Session: {value}"},
            "status_est_tokens": {"zh": "上下文估算(tokens): {value}", "en": "Estimated tokens: {value}"},
            "status_compaction": {"zh": "压缩次数: {value}", "en": "Compression count: {value}"},
            "language_set_ok": {"zh": "✅ 已设置 language = {lang}", "en": "✅ Language set to {lang}"},
            "language_set_failed": {"zh": "❌ 设置语言失败: {error}", "en": "❌ Failed to set language: {error}"},
            "compact_ok": {"zh": "✅ 已触发会话压缩", "en": "✅ Session compaction triggered"},
            "compact_fail": {"zh": "⚠️ 无法压缩：{reason}", "en": "⚠️ Unable to compact: {reason}"},
            "compact_unsupported": {"zh": "⚠️ 当前模式不支持手动压缩", "en": "⚠️ Manual compaction not supported in current mode"},
            "compact_error": {"zh": "❌ 压缩失败: {error}", "en": "❌ Compaction failed: {error}"},
            "model_set": {"zh": "✅ 已切换模型为 {model}（新会话生效）", "en": "✅ Model set to {model} (applies to new sessions)"},
            "cron_none": {"zh": "暂无定时任务", "en": "No cron jobs"},
            "cron_list": {"zh": "定时任务：\n{jobs}", "en": "Cron jobs:\n{jobs}"},
            "cron_deleted": {"zh": "✅ 已删除", "en": "✅ Deleted"},
            "cron_not_found": {"zh": "⚠️ 未找到该任务", "en": "⚠️ Job not found"},
            "cron_missing_message": {"zh": "⚠️ 缺少 --message", "en": "⚠️ Missing --message"},
            "cron_missing_schedule": {"zh": "⚠️ 需要指定 --every 或 --cron 或 --at 其中之一", "en": "⚠️ Must provide one of --every, --cron, or --at"},
            "cron_added": {"zh": "✅ 已添加任务 {id}", "en": "✅ Added job {id}"},
            "cron_usage": {
                "zh": "用法：/cron list | /cron add --name xxx --message xxx --every 60 | /cron delete <id>",
                "en": "Usage: /cron list | /cron add --name xxx --message xxx --every 60 | /cron delete <id>",
            },
            "ralph_usage": {
                "zh": "用法：/ralph \"prompt\" | /ralph answer <回复> | /ralph approve | /ralph stop | /ralph resume | /ralph status",
                "en": "Usage: /ralph \"prompt\" | /ralph answer <text> | /ralph approve | /ralph stop | /ralph resume | /ralph status",
            },
            "ralph_missing_answer": {"zh": "⚠️ 缺少回答内容：/ralph answer <内容>", "en": "⚠️ Missing answer: /ralph answer <text>"},
            "ralph_missing_prompt": {"zh": "⚠️ 缺少 prompt：/ralph \"你的任务\"", "en": "⚠️ Missing prompt: /ralph \"your task\""},
            "skills_usage": {
                "zh": "用法：/skills find <关键词> | /skills add <slug> | /skills list | /skills remove <slug> | /skills update",
                "en": "Usage: /skills find <query> | /skills add <slug> | /skills list | /skills remove <slug> | /skills update",
            },
            "skills_auto_install_fail": {
                "zh": "❌ 自动安装 SkillHub CLI 失败：{error}\n请手动执行：\n{cmd}",
                "en": "❌ Failed to auto-install SkillHub CLI: {error}\nPlease run manually:\n{cmd}",
            },
            "skills_missing_slug": {"zh": "⚠️ 缺少技能 slug：/skills remove <slug>", "en": "⚠️ Missing skill slug: /skills remove <slug>"},
            "skills_remove_failed": {"zh": "❌ 删除失败: {error}", "en": "❌ Remove failed: {error}"},
            "skills_removed": {"zh": "✅ 已卸载", "en": "✅ Uninstalled"},
            "skills_not_found": {"zh": "⚠️ 未找到该技能", "en": "⚠️ Skill not found"},
            "skills_auto_installed": {"zh": "✅ SkillHub CLI 已自动安装\n{text}", "en": "✅ SkillHub CLI auto-installed\n{text}"},
            "skills_exec_failed": {"zh": "❌ skills 执行失败: {error}", "en": "❌ skills failed: {error}"},
            "skills_no_output": {"zh": "无输出", "en": "No output"},
            "skillhub_install_failed": {"zh": "SkillHub CLI 安装失败", "en": "SkillHub CLI install failed"},
            "skillhub_executable_missing": {"zh": "SkillHub CLI 安装完成但未找到可执行文件", "en": "SkillHub CLI installed but executable not found"},
            "skills_search_header": {"zh": "技能搜索结果（用 /skills add <slug> 安装）：", "en": "Skill search results (install with /skills add <slug>):"},
            "skills_installed_header": {"zh": "已安装技能：", "en": "Installed skills:"},
            "skills_none_installed": {"zh": "暂无已安装技能", "en": "No installed skills"},
            "skills_install_path": {"zh": "路径", "en": "Path"},
            "skills_installed_path": {"zh": "✅ 已安装 {slug}\n{label}: {path}", "en": "✅ Installed {slug}\n{label}: {path}"},
            "skills_installed_single": {"zh": "✅ 已安装 {slug}", "en": "✅ Installed {slug}"},
            "ralph_running_stop": {"zh": "⚠️ 已有 Ralph 任务在运行，请先 /ralph stop", "en": "⚠️ A Ralph task is running. Please /ralph stop first"},
            "ralph_pending_stop": {"zh": "⚠️ 还有未完成的 Ralph 任务，请先 /ralph stop", "en": "⚠️ There is an unfinished Ralph task. Please /ralph stop first"},
            "ralph_generating_questions": {"zh": "⏳ 正在生成澄清问题...", "en": "⏳ Generating clarifying questions..."},
            "ralph_generating_questions_ping": {"zh": "⏳ 澄清问题生成中（{elapsed}s）", "en": "⏳ Generating clarifying questions ({elapsed}s)"},
            "ralph_questions_prompt": {
                "zh": "请先回答以下澄清问题（可用 1A 2C 形式，或直接输入文字回答）：\n\n{questions}\n\n回答后我会生成 PRD，再由你 /ralph approve 执行。",
                "en": "Please answer the following clarifying questions (e.g., 1A 2C or free text):\n\n{questions}\n\nAfter you answer, I will generate the PRD, then you can /ralph approve to execute.",
            },
            "ralph_no_pending_task": {"zh": "⚠️ 未找到待处理的 Ralph 任务。", "en": "⚠️ No pending Ralph task found."},
            "ralph_no_questions": {"zh": "⚠️ 当前没有需要回答的问题。", "en": "⚠️ No questions to answer right now."},
            "ralph_generating_prd": {"zh": "⏳ 正在生成 PRD，请稍候...", "en": "⏳ Generating PRD, please wait..."},
            "ralph_generating_prd_ping": {"zh": "⏳ PRD 生成中（{elapsed}s）", "en": "⏳ PRD generation in progress ({elapsed}s)"},
            "ralph_prd_feedback_regenerating": {
                "zh": "⏳ 已收到 PRD 调整反馈，正在重新生成 PRD...",
                "en": "⏳ PRD feedback received. Regenerating PRD...",
            },
            "ralph_prd_generation_busy": {
                "zh": "⏳ Ralph 正在生成 PRD，请等待当前轮完成后再补充反馈。",
                "en": "⏳ Ralph is still generating the PRD. Wait for this round to finish before sending more feedback.",
            },
            "ralph_prd_failed": {"zh": "❌ PRD 生成失败，请重试 /ralph \"prompt\"。", "en": "❌ PRD generation failed. Please retry /ralph \"prompt\"."},
            "ralph_generating_prd_json": {"zh": "⏳ 正在生成 PRD JSON...", "en": "⏳ Generating PRD JSON..."},
            "ralph_generating_prd_json_ping": {"zh": "⏳ PRD JSON 生成中（{elapsed}s）", "en": "⏳ PRD JSON generation in progress ({elapsed}s)"},
            "ralph_prd_ready": {
                "zh": "✅ PRD 已生成，请审核后执行 /ralph approve\n\nPRD Markdown: {prd_md}\nPRD JSON: {prd_json}\n\n{prd_preview}",
                "en": "✅ PRD generated. Review then /ralph approve\n\nPRD Markdown: {prd_md}\nPRD JSON: {prd_json}\n\n{prd_preview}",
            },
            "ralph_running_status": {"zh": "⚠️ Ralph 任务已在运行，可用 /ralph status 查看", "en": "⚠️ Ralph task is running. Use /ralph status"},
            "ralph_no_prd_to_approve": {"zh": "⚠️ 未找到待审核 PRD，请先 /ralph \"prompt\"", "en": "⚠️ No PRD to approve. Please /ralph \"prompt\" first"},
            "ralph_waiting_answers": {"zh": "⚠️ 仍在等待澄清问题回答，请先回复答案。", "en": "⚠️ Still waiting for clarifying answers. Please reply first."},
            "ralph_invalid_status_execute": {"zh": "⚠️ 当前状态不可执行：{status}", "en": "⚠️ Cannot execute in current status: {status}"},
            "ralph_no_prd_yet": {"zh": "⚠️ 未找到 PRD，请先完成澄清问题回答。", "en": "⚠️ PRD not found. Please answer clarifying questions first."},
            "ralph_approved_start": {"zh": "✅ 已批准，开始执行 Ralph loop", "en": "✅ Approved. Starting Ralph loop"},
            "ralph_stopped": {"zh": "✅ 已停止 Ralph 任务", "en": "✅ Ralph task stopped"},
            "ralph_no_task": {"zh": "暂无 Ralph 任务", "en": "No active Ralph task"},
            "ralph_resume_none": {"zh": "⚠️ 未找到可恢复的 Ralph 任务。", "en": "⚠️ No Ralph task to resume."},
            "ralph_resume_waiting_answers": {"zh": "⚠️ 仍在等待澄清问题回答，请先 /ralph answer。", "en": "⚠️ Still waiting for answers. Please /ralph answer."},
            "ralph_resume_waiting_approve": {"zh": "⚠️ 仍待审批，请先 /ralph approve。", "en": "⚠️ Still awaiting approval. Please /ralph approve."},
            "ralph_archived": {"zh": "✅ 该任务已完成，已归档。", "en": "✅ Task completed and archived."},
            "ralph_resume_running": {"zh": "⚠️ Ralph 任务已在运行，可用 /ralph status 查看", "en": "⚠️ Ralph task is running. Use /ralph status"},
            "ralph_busy_chat": {
                "zh": "⚠️ Ralph 正在执行中。当前不走普通对话模型，请先用 /ralph status 查看进度。\n\n{status}",
                "en": "⚠️ Ralph is currently running. Regular chat is temporarily paused; use /ralph status for progress.\n\n{status}",
            },
            "ralph_resume_no_prd": {"zh": "⚠️ 未找到 PRD，无法恢复执行。", "en": "⚠️ PRD not found. Cannot resume."},
            "ralph_resumed": {"zh": "✅ 已恢复执行 Ralph loop", "en": "✅ Ralph loop resumed"},
            "ralph_auto_resumed": {
                "zh": "♻️ 检测到服务重启前有进行中的 Ralph 任务，已自动继续执行。",
                "en": "♻️ Detected a Ralph task that was running before restart. It has been resumed automatically.",
            },
            "ralph_resume_invalid_status": {"zh": "⚠️ 当前状态不可恢复：{status}", "en": "⚠️ Cannot resume from status: {status}"},
            "ralph_prd_missing_execute": {"zh": "❌ PRD 不存在，无法执行。请重新 /ralph \"prompt\"。", "en": "❌ PRD missing. Please /ralph \"prompt\" again."},
            "ralph_prd_json_invalid": {"zh": "❌ PRD JSON 无效，请手动修正后 /ralph approve。", "en": "❌ PRD JSON invalid. Fix it then /ralph approve."},
            "ralph_prd_no_stories": {"zh": "❌ PRD 缺少 stories，无法执行。", "en": "❌ PRD has no stories. Cannot execute."},
            "ralph_story_pass_completed": {
                "zh": "✅ Story {story}/{total} Pass {pass_index}/{passes} 完成",
                "en": "✅ Story {story}/{total} Pass {pass_index}/{passes} completed",
            },
            "ralph_story_incomplete_paused": {
                "zh": "⚠️ 当前任务未真正完成，Ralph 已暂停。请修正后使用 /ralph resume 继续。",
                "en": "⚠️ The current story did not complete. Ralph has been paused. Fix it and use /ralph resume.",
            },
            "ralph_internal_paused": {
                "zh": "⚠️ Ralph 执行时发生内部错误，已暂停当前任务：{error}",
                "en": "⚠️ Ralph paused because of an internal error: {error}",
            },
            "ralph_no_progress": {"zh": "❌ Ralph 未产出进度内容，请重试 /ralph resume。", "en": "❌ Ralph produced no progress output. Please /ralph resume."},
            "ralph_final_summary_title": {"zh": "📌 最终结果/结论\n\n{summary}", "en": "📌 Final Result / Conclusion\n\n{summary}"},
            "ralph_task_done": {"zh": "✅ Ralph 任务完成", "en": "✅ Ralph task completed"},
            "ralph_subagent_connect_failed": {"zh": "❌ Ralph 子任务无法连接 iflow。", "en": "❌ Ralph subagent failed to connect to iflow."},
            "ralph_summary_fallback": {"zh": "未检测到结果摘要，请查看 {path}", "en": "No summary detected. Please check {path}"},
            "media_prompt_header": {"zh": "[用户发送了图片/文件，请读取以下本地路径进行识别，勿编造内容]", "en": "[User sent image/file. Please read these local paths for analysis. Do not fabricate content.]"},
            "media_prompt_footer": {"zh": "如无法读取，请说明原因并提示用户重新发送。", "en": "If you cannot access them, explain why and ask the user to resend."},
            "stream_empty_fallback": {
                "zh": "⚠️ 本轮未产出可见文本（可能会话上下文过长）。我已自动尝试恢复，如仍失败请发送 /new 开启新会话。",
                "en": "⚠️ No visible output this turn (context may be too long). I attempted auto-recovery; if it still fails, send /new to start a new session.",
            },
            "process_error": {"zh": "❌ 处理消息时出错: {error}", "en": "❌ Error processing message: {error}"},
            "new_conversation": {"zh": "✨ 已开启新会话，上一轮上下文已清空。", "en": "✨ New conversation started, previous context has been cleared."},
        }
        entry = table.get(key, {})
        text = entry.get("en") if is_en else entry.get("zh")
        if text is None:
            text = entry.get("zh") or key
        return text.format(**kwargs)

    def _build_channel_context(self, msg) -> str:
        """Build channel context for the agent."""
        from datetime import datetime
        
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        
        context = f"""[message_source]
channel: {msg.channel}
chat_id: {msg.chat_id}
session: {msg.channel}:{msg.chat_id}
time: {now}
[/message_source]"""

        lang = self._load_language_setting()
        if lang:
            policy = self._format_language_policy(lang)
            context += f"""

[language]
locale: {lang}
policy: {policy}
[/language]"""

        return context

    def _append_media_prompt(self, message: str, media: list[str]) -> str:
        """Append media file hints so the agent can load images/files reliably."""
        if not media:
            return message

        lines = [
            self._msg("media_prompt_header"),
        ]
        for item in media:
            lines.append(f"- {item}")
        lines.append(self._msg("media_prompt_footer"))

        prompt = "\n".join(lines)
        if message:
            return f"{message}\n\n{prompt}"
        return prompt

    async def _resolve_media_paths(self, media: list[str]) -> list[str]:
        """Normalize media to local files (download remote URLs into workspace)."""
        resolved: list[str] = []
        if not media:
            return resolved

        workspace = self.workspace or Path.home() / ".iflow-bot" / "workspace"
        media_dir = workspace / "images"
        media_dir.mkdir(parents=True, exist_ok=True)

        def _is_url(value: str) -> bool:
            return value.startswith("http://") or value.startswith("https://")

        async def _download(url: str) -> Optional[str]:
            import hashlib
            import mimetypes
            import aiohttp

            suffix = ""
            try:
                suffix = Path(url.split("?")[0]).suffix
            except Exception:
                suffix = ""

            name_hash = hashlib.sha1(url.encode("utf-8")).hexdigest()[:16]
            file_path = media_dir / f"remote_{name_hash}{suffix or ''}"
            try:
                timeout = aiohttp.ClientTimeout(total=30)
                async with aiohttp.ClientSession(timeout=timeout) as session:
                    async with session.get(url) as resp:
                        if resp.status != 200:
                            logger.warning("Failed to download media: {} -> HTTP {}", url, resp.status)
                            return None
                        content_type = resp.headers.get("Content-Type", "")
                        if not suffix:
                            ext = mimetypes.guess_extension(content_type.split(";")[0].strip()) or ""
                            if ext:
                                file_path = media_dir / f"remote_{name_hash}{ext}"
                        data = await resp.read()
                        file_path.write_bytes(data)
                        return str(file_path)
            except Exception as e:
                logger.warning("Failed to download media {}: {}", url, e)
                return None

        for item in media:
            if not item:
                continue
            if _is_url(item):
                downloaded = await _download(item)
                if downloaded:
                    resolved.append(downloaded)
                else:
                    resolved.append(item)
                continue
            path = Path(item)
            if not path.is_absolute():
                candidate = media_dir / path
                if candidate.exists():
                    resolved.append(str(candidate))
                else:
                    resolved.append(str(path))
            else:
                resolved.append(str(path))

        return resolved

    async def _resolve_media_paths(self, media: list[str]) -> list[str]:
        """Normalize media to local files (download remote URLs into workspace)."""
        resolved: list[str] = []
        if not media:
            return resolved

        workspace = self.workspace or Path.home() / ".iflow-bot" / "workspace"
        media_dir = workspace / "images"
        media_dir.mkdir(parents=True, exist_ok=True)

        def _is_url(value: str) -> bool:
            return value.startswith("http://") or value.startswith("https://")

        async def _download(url: str) -> Optional[str]:
            import hashlib
            import mimetypes
            import aiohttp

            suffix = ""
            try:
                suffix = Path(url.split("?")[0]).suffix
            except Exception:
                suffix = ""

            name_hash = hashlib.sha1(url.encode("utf-8")).hexdigest()[:16]
            file_path = media_dir / f"remote_{name_hash}{suffix or ''}"
            try:
                timeout = aiohttp.ClientTimeout(total=30)
                async with aiohttp.ClientSession(timeout=timeout) as session:
                    async with session.get(url) as resp:
                        if resp.status != 200:
                            logger.warning("Failed to download media: {} -> HTTP {}", url, resp.status)
                            return None
                        content_type = resp.headers.get("Content-Type", "")
                        if not suffix:
                            ext = mimetypes.guess_extension(content_type.split(";")[0].strip()) or ""
                            if ext:
                                file_path = media_dir / f"remote_{name_hash}{ext}"
                        data = await resp.read()
                        file_path.write_bytes(data)
                        return str(file_path)
            except Exception as e:
                logger.warning("Failed to download media {}: {}", url, e)
                return None

        for item in media:
            if not item:
                continue
            if _is_url(item):
                downloaded = await _download(item)
                if downloaded:
                    resolved.append(downloaded)
                else:
                    resolved.append(item)
                continue
            path = Path(item)
            if not path.is_absolute():
                candidate = media_dir / path
                if candidate.exists():
                    resolved.append(str(candidate))
                else:
                    resolved.append(str(path))
            else:
                resolved.append(str(path))

        return resolved

    def _analyze_and_build_outbound(
        self,
        response: str,
        channel: str,
        chat_id: str,
        metadata: Optional[dict] = None,
    ) -> OutboundMessage:
        """Analyze response with ResultAnalyzer and build OutboundMessage with media.

        Ported from feishu-iflow-bridge FeishuSender.sendExecutionResult():
        - Scans iflow output for generated file paths
        - Categorizes files (image/audio/video/doc)
        - Attaches detected files via OutboundMessage.media

        Args:
            response: Raw response text from iflow
            channel: Target channel name
            chat_id: Target chat ID
            metadata: Additional metadata

        Returns:
            OutboundMessage with content and media attachments
        """
        # Analyze the response
        analysis = result_analyzer.analyze({"output": response, "success": True})

        # Collect all detected files for media attachment
        media_files: list[str] = []
        if analysis.image_files:
            media_files.extend(analysis.image_files)
            logger.info(f"Detected {len(analysis.image_files)} image(s) in response")
        if analysis.audio_files:
            media_files.extend(analysis.audio_files)
            logger.info(f"Detected {len(analysis.audio_files)} audio file(s) in response")
        if analysis.video_files:
            media_files.extend(analysis.video_files)
            logger.info(f"Detected {len(analysis.video_files)} video file(s) in response")
        if analysis.doc_files:
            media_files.extend(analysis.doc_files)
            logger.info(f"Detected {len(analysis.doc_files)} document(s) in response")

        if media_files:
            logger.info(f"File callback: attaching {len(media_files)} file(s) to outbound message")

        return OutboundMessage(
            channel=channel,
            chat_id=chat_id,
            content=response,
            media=media_files,
            metadata=metadata or {},
        )

    def _build_reply_metadata(self, msg: InboundMessage, extra: Optional[dict] = None) -> dict:
        metadata = dict(extra or {})
        if msg.metadata:
            if "is_group" in msg.metadata:
                metadata["is_group"] = msg.metadata.get("is_group")
            if "group_id" in msg.metadata:
                metadata["group_id"] = msg.metadata.get("group_id")
            if "reply_to_id" not in metadata:
                metadata["reply_to_id"] = msg.metadata.get("message_id")
        return metadata

    def _ralph_base_dir(self, chat_id: str) -> Path:
        workspace = self.workspace or Path.home() / ".iflow-bot" / "workspace"
        base = workspace / "ralph" / chat_id
        base.mkdir(parents=True, exist_ok=True)
        return base

    def _ralph_current_file(self, chat_id: str) -> Path:
        return self._ralph_base_dir(chat_id) / "current.json"

    def _ralph_state_path(self, run_dir: Path) -> Path:
        return run_dir / "state.json"

    def _ralph_progress_path(self, run_dir: Path) -> Path:
        return run_dir / "progress.txt"

    def _ralph_prd_path(self, run_dir: Path) -> Path:
        return run_dir / "prd.json"

    def _ralph_load_state(self, run_dir: Path) -> dict:
        path = self._ralph_state_path(run_dir)
        if path.exists():
            try:
                return json.loads(path.read_text(encoding="utf-8"))
            except Exception:
                return {}
        return {}

    def _ralph_save_state(self, run_dir: Path, state: dict) -> None:
        path = self._ralph_state_path(run_dir)
        path.write_text(json.dumps(state, indent=2, ensure_ascii=False), encoding="utf-8")

    def _ralph_get_effective_state(self, chat_id: str, run_dir: Path) -> dict:
        return self._ralph_load_state(run_dir)

    def _ralph_append_progress_marker(self, run_dir: Path, marker: str) -> None:
        progress_path = self._ralph_progress_path(run_dir)
        if not progress_path.exists():
            progress_path.write_text("# Ralph Progress\n", encoding="utf-8")
        try:
            with open(progress_path, "a", encoding="utf-8") as f:
                f.write(f"\n{marker}\n")
        except Exception:
            pass

    def _ralph_infer_channel(self, chat_id: str, state: dict) -> Optional[str]:
        channel = str(state.get("channel") or "").strip()
        if channel:
            return channel

        workspace = self.workspace or Path.home() / ".iflow-bot" / "workspace"
        channel_root = workspace / "channel"
        if not channel_root.exists():
            return None

        candidates: list[tuple[float, str]] = []
        for channel_dir in channel_root.iterdir():
            if not channel_dir.is_dir():
                continue
            for path in channel_dir.glob(f"{chat_id}-*.json"):
                try:
                    candidates.append((path.stat().st_mtime, channel_dir.name))
                except Exception:
                    continue
        if not candidates:
            return None
        candidates.sort(reverse=True)
        return candidates[0][1]

    async def _ralph_auto_resume_pending_runs(self) -> None:
        workspace = self.workspace or Path.home() / ".iflow-bot" / "workspace"
        ralph_root = workspace / "ralph"
        if not ralph_root.exists():
            return

        for chat_dir in ralph_root.iterdir():
            if not chat_dir.is_dir():
                continue
            chat_id = chat_dir.name
            run_id = self._ralph_get_current(chat_id)
            if not run_id or self._has_active_ralph(chat_id):
                continue

            run_dir = self._ralph_run_dir(chat_id, run_id)
            state = self._ralph_load_state(run_dir)
            status = str(state.get("status") or "").strip().lower()
            if status not in {"running", "approved"}:
                continue
            if not self._ralph_prd_path(run_dir).exists():
                continue

            channel = self._ralph_infer_channel(chat_id, state)
            if not channel:
                logger.warning("Skipping Ralph auto-resume for {}: channel unknown", chat_id)
                continue

            state["channel"] = channel
            state["status"] = "approved"
            state["updated_at"] = time.time()
            self._ralph_save_state(run_dir, state)
            self._ralph_append_progress_marker(run_dir, "RESTART_DETECTED")
            self._ralph_append_progress_marker(run_dir, "AUTO_RESUMED")

            task = asyncio.create_task(self._ralph_run_loop(channel, chat_id, run_dir))
            self._ralph_tasks[chat_id] = task
            await self._ralph_send_update(channel, chat_id, self._msg("ralph_auto_resumed"))

    def _ralph_set_current(self, chat_id: str, run_id: str) -> None:
        current = {"run_id": run_id}
        self._ralph_current_file(chat_id).write_text(
            json.dumps(current, ensure_ascii=False), encoding="utf-8"
        )

    def _ralph_clear_current(self, chat_id: str) -> None:
        current_file = self._ralph_current_file(chat_id)
        if current_file.exists():
            current_file.unlink()

    def _ralph_get_current(self, chat_id: str) -> Optional[str]:
        current_file = self._ralph_current_file(chat_id)
        if not current_file.exists():
            return None
        try:
            data = json.loads(current_file.read_text(encoding="utf-8"))
            return data.get("run_id")
        except Exception:
            return None

    def _ralph_run_dir(self, chat_id: str, run_id: str) -> Path:
        return self._ralph_base_dir(chat_id) / run_id

    def _ralph_questions_path(self, run_dir: Path) -> Path:
        return run_dir / "questions.json"

    def _ralph_answers_path(self, run_dir: Path) -> Path:
        return run_dir / "answers.txt"

    def _ralph_tasks_dir(self, run_dir: Path) -> Path:
        return run_dir / "tasks"

    def _ralph_prd_md_path(self, run_dir: Path, slug: str) -> Path:
        return self._ralph_tasks_dir(run_dir) / f"prd-{slug}.md"

    def _ralph_latest_prd_md_path(self, run_dir: Path) -> Optional[Path]:
        tasks_dir = self._ralph_tasks_dir(run_dir)
        if not tasks_dir.exists():
            return None
        candidates = sorted(
            tasks_dir.glob("prd-*.md"),
            key=lambda path: path.stat().st_mtime if path.exists() else 0,
            reverse=True,
        )
        return candidates[0] if candidates else None

    def _ralph_slugify(self, text: str) -> str:
        raw = (text or "").lower()
        slug = re.sub(r"[^a-z0-9]+", "-", raw).strip("-")
        if not slug:
            return "ralph"
        return slug[:40]

    def _ralph_extract_json(self, text: str) -> Optional[dict]:
        if not text:
            return None
        raw = text.strip()
        if raw.startswith("```"):
            parts = raw.split("```")
            if len(parts) >= 3:
                raw = parts[1].strip()
                if raw.startswith("json"):
                    raw = raw[4:].strip()
        # Try to isolate JSON object
        if "{" in raw and "}" in raw:
            start = raw.find("{")
            end = raw.rfind("}")
            raw = raw[start:end + 1]
        try:
            return json.loads(raw)
        except Exception:
            return None

    def _ralph_build_prd_fallback(self, prd_md: str) -> Optional[dict]:
        if not prd_md.strip():
            return None

        title = ""
        intro_lines: list[str] = []
        description = ""
        stories: list[dict] = []
        current_story: Optional[dict] = None
        in_intro = False
        in_user_stories = False
        in_acceptance = False
        story_index = 0

        title_headers = {"## Title", "## 标题"}
        intro_headers = {"## Introduction", "## 简介"}
        user_story_headers = {"## User Stories", "## 用户故事"}

        for raw_line in prd_md.splitlines():
            line = raw_line.rstrip()
            stripped = line.strip()
            if not stripped:
                continue

            if stripped in title_headers:
                in_intro = False
                in_user_stories = False
                in_acceptance = False
                continue
            if stripped in intro_headers:
                in_intro = True
                in_user_stories = False
                in_acceptance = False
                continue
            if stripped in user_story_headers:
                in_intro = False
                in_user_stories = True
                in_acceptance = False
                continue
            if stripped.startswith("## ") and stripped not in (title_headers | intro_headers | user_story_headers):
                in_intro = False
                in_user_stories = False
                in_acceptance = False
                if current_story:
                    stories.append(current_story)
                    current_story = None
                continue

            if not title and not stripped.startswith("#") and not stripped.startswith("-") and not stripped.startswith("### "):
                title = stripped
                continue

            if in_intro:
                intro_lines.append(stripped)
                continue

            if in_user_stories and stripped.startswith("### "):
                if current_story:
                    stories.append(current_story)
                story_index += 1
                heading = stripped[4:].strip()
                current_story = {
                    "id": f"US-{story_index:03d}",
                    "title": heading.split(":", 1)[-1].strip() if ":" in heading else heading,
                    "description": "",
                    "acceptanceCriteria": [],
                    "role": "engineer",
                    "priority": story_index,
                    "passes": False,
                    "notes": "",
                }
                in_acceptance = False
                continue

            if not current_story:
                continue

            if stripped.startswith("- **Description:**"):
                current_story["description"] = stripped.split(":", 1)[1].strip()
                in_acceptance = False
                continue
            if stripped.startswith("- **描述**：") or stripped.startswith("- **描述**:"):
                current_story["description"] = re.split(r"[:：]", stripped, maxsplit=1)[1].strip()
                in_acceptance = False
                continue

            if stripped.startswith("- **Role:**"):
                role = stripped.split(":", 1)[1].strip().lower()
                role = role.strip("*`_ ")
                current_story["role"] = role or "engineer"
                in_acceptance = False
                continue
            if stripped.startswith("- **角色**：") or stripped.startswith("- **角色**:"):
                role = re.split(r"[:：]", stripped, maxsplit=1)[1].strip().lower()
                role = role.strip("*`_ ")
                current_story["role"] = role or "engineer"
                in_acceptance = False
                continue

            if stripped.startswith("- **Acceptance Criteria:**"):
                in_acceptance = True
                continue
            if stripped.startswith("- **验收标准**：") or stripped.startswith("- **验收标准**:"):
                in_acceptance = True
                continue

            if in_acceptance and stripped.startswith("- "):
                current_story["acceptanceCriteria"].append(stripped[2:].strip())
                continue

        if current_story:
            stories.append(current_story)

        normalized_stories: list[dict] = []
        for idx, story in enumerate(stories, start=1):
            normalized = self._ralph_normalize_story(story, idx)
            normalized["passes"] = False
            normalized_stories.append(normalized)

        description = " ".join(intro_lines).strip()
        if not title:
            title = "Ralph Task"
        if not description:
            description = title
        if not normalized_stories:
            return None

        branch_slug = self._ralph_slugify(title)
        return {
            "project": title,
            "branchName": f"ralph/{branch_slug}",
            "description": description,
            "userStories": normalized_stories,
            "stories": normalized_stories,
        }

    def _ralph_extract_project_dir(self, prompt: str) -> Optional[str]:
        if not prompt:
            return None
        patterns = [
            r"请在\s*([~/A-Za-z0-9._/-]+)\s*创建",
            r"请在\s*([~/A-Za-z0-9._/-]+)\s*(?:输出|生成|产出|撰写|写入|保存)",
            r"create(?:\s+\w+)*\s+in\s*[:：]?\s*([~/A-Za-z0-9._/-]+)",
            r"(?:输出目录固定为|输出目录为|输出到)\s*[:：]?\s*([A-Za-z0-9_./~-]+)",
            r"(?:写入到|写入|保存到|保存至|放到|产出到|生成到)\s*[:：]?\s*([A-Za-z0-9_./~-]+)",
            r"(?:output\s+to|output\s+path|output\s+dir(?:ectory)?|output\s+folder|project\s+dir(?:ectory)?|project\s+path)(?:\s+is)?\s*[:=]?\s*([A-Za-z0-9_./~-]+)",
        ]
        for pattern in patterns:
            match = re.search(pattern, prompt, re.IGNORECASE)
            if match:
                candidate = match.group(1).strip().strip("\"“”")
                candidate = candidate.rstrip("。.,，;；:：")
                if candidate:
                    return candidate
        for match in re.findall(r"([~/A-Za-z0-9._/-]+)", prompt):
            candidate = match.strip().strip("\"“”").rstrip("。.,，;；:：")
            if not candidate:
                continue
            if candidate.startswith("project/") or "/workspace/project/" in candidate:
                return candidate
        return None

    def _ralph_prompt_requires_local_only(self, prompt: str) -> bool:
        text = (prompt or "").lower()
        keywords = (
            "不要访问互联网",
            "不要上网",
            "仅使用本地",
            "只使用本地",
            "只使用当前仓库",
            "只使用当前仓库与workspace",
            "只使用当前仓库和workspace",
            "本地代码/文档作为资料来源",
            "本地代码/文档",
            "no internet",
            "local only",
            "only use local",
            "do not access the internet",
            "do not use the internet",
            "use only local",
        )
        return any(keyword in text for keyword in keywords)

    def _ralph_prompt_requests_repo_context(self, prompt: str) -> bool:
        text = (prompt or "").lower()
        keywords = (
            "当前仓库",
            "本仓库",
            "当前代码库",
            "当前repo",
            "current repo",
            "current repository",
            "local repo",
            "local repository",
            "current codebase",
        )
        return any(keyword in text for keyword in keywords)

    def _ralph_current_repo_root(self) -> Optional[Path]:
        with contextlib.suppress(Exception):
            cwd = Path.cwd().resolve()
            for candidate in (cwd, *cwd.parents):
                if (candidate / ".git").exists():
                    return candidate
            return cwd
        return None

    def _ralph_allowed_read_roots(self, run_dir: Path, project_dir: Path, task_prompt: str = "") -> list[Path]:
        roots: list[Path] = []
        for candidate in (run_dir, project_dir):
            with contextlib.suppress(Exception):
                resolved = candidate.expanduser().resolve()
                if resolved not in roots:
                    roots.append(resolved)
        if self._ralph_prompt_requests_repo_context(task_prompt):
            repo_root = self._ralph_current_repo_root()
            if repo_root is not None:
                with contextlib.suppress(Exception):
                    resolved_repo = repo_root.expanduser().resolve()
                    if resolved_repo not in roots:
                        roots.append(resolved_repo)
        return roots

    def _ralph_resolve_project_dir(self, project_dir: str) -> Path:
        raw = (project_dir or "").strip()
        if not raw:
            raise ValueError("project_dir is required")

        candidate = Path(raw).expanduser()
        if candidate.is_absolute():
            return candidate

        workspace = self.workspace or Path.home() / ".iflow-bot" / "workspace"
        return workspace / candidate

    def _ralph_normalize_story(self, story: dict, idx: int) -> dict:
        role = self._ralph_pick_role(story)
        criteria = [str(item).strip() for item in story.get("acceptanceCriteria", []) if str(item).strip()]

        if role in {"researcher", "writer"}:
            criteria = [
                item
                for item in criteria
                if item.lower() not in {"typecheck passes", "tests pass"}
            ]
        elif "Typecheck passes" not in criteria:
            criteria.append("Typecheck passes")

        return {
            "id": story.get("id") or f"US-{idx:03d}",
            "title": story.get("title") or f"Story {idx}",
            "description": story.get("description") or (story.get("title") or f"Story {idx}"),
            "acceptanceCriteria": criteria,
            "role": role,
            "priority": story.get("priority") or idx,
            "passes": story.get("passes", False),
            "notes": story.get("notes") or "",
        }

    def _ralph_story_completed(self, story: dict) -> bool:
        raw_passes = story.get("passes", 1)
        if isinstance(raw_passes, bool):
            return raw_passes
        try:
            required = max(1, int(raw_passes))
        except Exception:
            return False
        try:
            completed = int(story.get("completed_passes", 0))
        except Exception:
            completed = 0
        return completed >= required

    def _ralph_mark_story_complete(self, story: dict) -> None:
        raw_passes = story.get("passes", 1)
        if isinstance(raw_passes, bool):
            story["passes"] = True
            return
        try:
            story["completed_passes"] = max(1, int(raw_passes))
        except Exception:
            story["passes"] = True

    def _ralph_sync_story_completion_from_progress(
        self,
        prd_path: Path,
        progress_path: Path,
        story_index: int,
        artifact_paths: list[Path] | None = None,
        progress_text: str | None = None,
    ) -> tuple[dict, bool]:
        try:
            prd = json.loads(prd_path.read_text(encoding="utf-8"))
        except Exception:
            return {}, False

        stories = prd.get("stories") or prd.get("userStories") or []
        if story_index >= len(stories):
            return prd, False
        story = stories[story_index]
        if not isinstance(story, dict):
            return prd, False
        if self._ralph_story_completed(story):
            return prd, True

        if progress_text is None:
            try:
                progress_text = progress_path.read_text(encoding="utf-8")
            except Exception:
                progress_text = ""

        if not self._ralph_progress_marks_story_complete(progress_text, story):
            return prd, False
        if artifact_paths and not self._ralph_has_story_artifact_output(artifact_paths):
            return prd, False

        self._ralph_mark_story_complete(story)
        try:
            prd_path.write_text(json.dumps(prd, ensure_ascii=False, indent=2), encoding="utf-8")
        except Exception:
            return prd, False
        return prd, True

    def _ralph_progress_marks_story_complete(self, progress_text: str, story: dict) -> bool:
        if not progress_text.strip():
            return False
        story_id = str(story.get("id") or "").strip().lower()
        title = str(story.get("title") or story.get("name") or "").strip().lower()
        if not story_id and not title:
            return False

        sections = re.split(r"(?m)^##\s+", progress_text)
        candidates = [f"## {section}".strip() for section in sections[1:]] or [progress_text.strip()]
        status_patterns = (
            r"\*\*状态[：:]\*\*\s*已完成(?:\s*\(passes:\s*true\))?",
            r"\*\*status[：:]\*\*\s*completed(?:\s*\(passes:\s*true\))?",
            r"passes:\s*true",
        )
        header_patterns = (
            r"^## .*?\b(?:done|completed)\b",
            r"^## .*?完成\b",
        )

        for section in reversed(candidates):
            normalized = section.strip()
            lowered = normalized.lower()
            if story_id and story_id not in lowered and (not title or title not in lowered):
                continue
            if title and title not in lowered and (not story_id or story_id not in lowered):
                continue
            if any(re.search(pattern, normalized, flags=re.IGNORECASE | re.MULTILINE) for pattern in status_patterns):
                return True
            if any(re.search(pattern, normalized, flags=re.IGNORECASE | re.MULTILINE) for pattern in header_patterns):
                return True
        return False

    def _ralph_has_story_artifact_output(self, artifact_paths: list[Path]) -> bool:
        for path in artifact_paths:
            try:
                if not path.exists():
                    continue
                if path.is_dir():
                    if any(child.is_file() and child.stat().st_size >= 0 for child in path.rglob("*")):
                        return True
                    continue
                if path.is_file():
                    return True
            except Exception:
                continue
        return False

    async def _ralph_wait_for_story_settle(
        self,
        prd_path: Path,
        progress_path: Path,
        story_index: int,
        initial_progress: str,
        artifact_paths: list[Path] | None = None,
        settle_timeout: float = 3.0,
        poll_interval: float = 0.2,
    ) -> tuple[dict, str]:
        deadline = asyncio.get_running_loop().time() + settle_timeout
        latest_prd: dict = {}
        latest_progress = initial_progress

        while asyncio.get_running_loop().time() < deadline:
            await asyncio.sleep(poll_interval)
            try:
                latest_prd = json.loads(prd_path.read_text(encoding="utf-8"))
            except Exception:
                latest_prd = {}
            try:
                latest_progress = progress_path.read_text(encoding="utf-8")
            except Exception:
                latest_progress = initial_progress

            stories = latest_prd.get("stories") or latest_prd.get("userStories") or []
            current_story = stories[story_index] if story_index < len(stories) else {}
            if self._ralph_story_completed(current_story):
                return latest_prd, latest_progress
            latest_prd, synced = self._ralph_sync_story_completion_from_progress(
                prd_path=prd_path,
                progress_path=progress_path,
                story_index=story_index,
                artifact_paths=artifact_paths,
                progress_text=latest_progress,
            )
            if synced:
                try:
                    latest_progress = progress_path.read_text(encoding="utf-8")
                except Exception:
                    latest_progress = initial_progress
                return latest_prd, latest_progress

        return latest_prd, latest_progress

    def _ralph_prime_current_story(self, run_dir: Path, state: dict) -> dict:
        prd_path = self._ralph_prd_path(run_dir)
        if not prd_path.exists():
            return state
        try:
            prd = json.loads(prd_path.read_text(encoding="utf-8"))
        except Exception:
            return state

        stories = prd.get("stories") or prd.get("userStories") or []
        if not stories:
            return state

        story_index = int(state.get("story_index", 0) or 0)
        if story_index >= len(stories):
            story_index = max(0, len(stories) - 1)
        story = stories[story_index]
        raw_passes = story.get("passes", 1) if isinstance(story, dict) else 1
        passes = 1 if isinstance(raw_passes, bool) else max(1, int(raw_passes))
        pass_index = int(state.get("pass_index", 0) or 0)
        if pass_index >= passes:
            pass_index = max(0, passes - 1)

        title = ""
        role = ""
        if isinstance(story, dict):
            title = str(story.get("title") or story.get("name") or "").strip()
            role = self._ralph_pick_role(story)

        previous_story_index = state.get("current_story_index")
        previous_pass_index = state.get("current_pass_index")
        previous_started_at = state.get("current_started_at")
        state["current_story_index"] = story_index + 1
        state["current_story_total"] = len(stories)
        state["current_pass_index"] = pass_index + 1
        state["current_pass_total"] = passes
        state["current_story_title"] = title
        state["current_story_role"] = role
        if (
            previous_story_index == story_index + 1
            and previous_pass_index == pass_index + 1
            and previous_started_at is not None
        ):
            state["current_started_at"] = previous_started_at
        else:
            state["current_started_at"] = time.time()
        return state

    def _ralph_default_questions(self, prompt: str) -> list[dict]:
        lang = self._load_language_setting()
        if self._is_english(lang):
            return [
                {"question": "What is the goal and scope of this task?"},
                {"question": "What output or deliverable do you expect?"},
                {"question": "What constraints or prohibitions apply (for example, no code changes or no environment changes)?"},
            ]
        return [
            {"question": "这次任务的目标与范围是什么？"},
            {"question": "期望的输出形式/交付物是什么？"},
            {"question": "有什么约束或禁止项（如不改代码/不改环境）？"},
        ]

    def _load_ralph_template(self, name: str) -> str:
        base = Path(__file__).resolve().parent.parent / "templates" / "ralph"
        path = base / name
        return path.read_text(encoding="utf-8")

    def _render_ralph_template(self, template: str, **kwargs: object) -> str:
        def replace(match: re.Match[str]) -> str:
            key = match.group(1)
            return str(kwargs.get(key, match.group(0)))

        return re.sub(r"\{([a-zA-Z0-9_]+)\}", replace, template)

    def _load_role_focus(self, role: str) -> str:
        role_name = (role or "default").lower()
        candidates = [
            f"role/{role_name}.md",
            "role/default.md",
        ]
        for candidate in candidates:
            try:
                return self._load_ralph_template(candidate).strip()
            except Exception:
                continue
        return ""

    def _ralph_build_subagent_prompt(
        self,
        run_dir: Path,
        project_dir: Path,
        story: dict,
        story_index: int,
        story_total: int,
        pass_index: int,
        passes: int,
        progress_before: str,
        task_prompt: str = "",
    ) -> str:
        lang = self._load_language_setting()
        policy = self._format_language_policy(lang)
        role = self._ralph_pick_role(story)
        role_focus = self._ralph_role_focus(role)
        story_json = json.dumps(story, ensure_ascii=False, indent=2)
        targeted_hints = self._ralph_targeted_story_hints(story=story, project_dir=project_dir)
        allowed_roots = self._ralph_allowed_read_roots(run_dir, project_dir, task_prompt=task_prompt)
        path_lines = ["Only read files inside these paths:"]
        run_dir_resolved = run_dir.resolve()
        project_dir_resolved = project_dir.resolve()
        path_lines.append(f"- Ralph Run Directory: {run_dir_resolved}")
        path_lines.append(f"- Project Directory: {project_dir_resolved}")
        for candidate in allowed_roots:
            if candidate in {run_dir_resolved, project_dir_resolved}:
                continue
            path_lines.append(f"- Additional Allowed Root: {candidate}")
        path_lines.extend(
            [
                "Do not read outside the allowed roots above.",
                "The runtime workspace may cover a broader ancestor directory; ignore that and stay within the allowed roots above.",
                "Do not scan sibling directories unless they are descendants of an allowed root.",
            ]
        )
        if self._ralph_prompt_requires_local_only(task_prompt):
            path_lines.extend(
                [
                    "Use only local files under the allowed roots above as sources.",
                    "Do not access the internet, web search, WebFetch, remote APIs, or external websites.",
                    "If required evidence is not available locally, record the gap instead of using remote sources.",
                ]
            )
        path_rules = "\n".join(path_lines)
        subagent_template = self._load_ralph_template("rules/ralph_prompt.md")
        return self._render_ralph_template(
            subagent_template,
            workspace=run_dir,
            project_dir=project_dir,
            role=role,
            role_focus=role_focus,
            language=lang,
            language_policy=policy,
            story_index=story_index,
            story_total=story_total,
            pass_index=pass_index,
            passes=passes,
            story_json=story_json,
            progress_before=progress_before,
            path_rules=path_rules,
            targeted_hints=targeted_hints,
        )

    async def _ralph_call_model(self, message: str, run_dir: Path, system_prompt: str) -> str:
        lang = self._load_language_setting()
        if lang:
            policy = self._format_language_policy(lang)
            system_prompt = f"{system_prompt}\n\n[LANGUAGE]\n{policy}\n[/LANGUAGE]"
            message = f"[LANGUAGE]\n{policy}\n[/LANGUAGE]\n\n{message}"
        if getattr(self.adapter, "mode", "cli") == "stdio":
            stdio = await self.adapter._get_stdio_adapter()
            await stdio.connect()
            if not stdio._client:
                return ""
            session_id = await stdio._client.create_session(
                workspace=run_dir,
                model=self.model,
                system_prompt=system_prompt,
                approval_mode="yolo",
            )
            response = await stdio._client.prompt(session_id, message=message, timeout=self.adapter.timeout)
            return response.content or ""

        return await self.adapter.chat(
            message=message,
            channel="ralph",
            chat_id=str(run_dir),
            model=self.model,
        )

    async def _ralph_generate_questions(self, prompt: str, run_dir: Path) -> tuple[list[dict], str]:
        try:
            template = self._load_ralph_template("prd/clarify_questions.md")
            system_prompt = self._load_ralph_template("prd/clarify_system.md")
        except Exception:
            return [], ""
        message = self._render_ralph_template(template, user_prompt=prompt)
        text = await self._ralph_call_model(message, run_dir, system_prompt)
        data = self._ralph_extract_json(text) or {}
        questions = data.get("questions") if isinstance(data, dict) else None
        if not isinstance(questions, list):
            return [], text
        return questions, text

    def _text_matches_language(self, text: str, lang: str) -> bool:
        content = (text or "").strip()
        if not content:
            return True
        has_cjk = bool(re.search(r"[\u4e00-\u9fff]", content))
        has_latin = bool(re.search(r"[A-Za-z]", content))
        if self._is_english(lang):
            return has_latin and not has_cjk
        if (lang or "").lower().startswith("zh"):
            return has_cjk
        return True

    def _ralph_questions_match_language(self, questions: list[dict], lang: str) -> bool:
        if not questions:
            return True
        for item in questions:
            if not isinstance(item, dict):
                return False
            if not self._text_matches_language(str(item.get("question") or ""), lang):
                return False
            options = item.get("options")
            if isinstance(options, dict):
                for value in options.values():
                    if not self._text_matches_language(str(value or ""), lang):
                        return False
        return True

    async def _ralph_send_update(
        self,
        channel: str,
        chat_id: str,
        content: str,
        force_streaming: bool = False,
    ) -> None:
        if not content:
            return
        supports_streaming = self.streaming and channel in STREAMING_CHANNELS and channel != "qq"
        if (force_streaming or len(content) > 600) and supports_streaming:
            await self._send_streaming_content(channel, chat_id, content, {})
            return
        chunks = self._split_command_message(content)
        for idx, chunk in enumerate(chunks):
            await self._emit_outbound(OutboundMessage(
                channel=channel,
                chat_id=chat_id,
                content=chunk,
            ), prefer_direct=True)
            if idx < len(chunks) - 1:
                await asyncio.sleep(0.2)

    async def _ralph_generate_prd_md(self, prompt: str, qa_block: str, run_dir: Path) -> str:
        try:
            template = self._load_ralph_template("prd/prd_writer.md")
            system_prompt = self._load_ralph_template("prd/prd_writer_system.md")
        except Exception:
            return ""
        message = self._render_ralph_template(template, user_prompt=prompt, qa_block=qa_block)
        return await self._ralph_call_model(message, run_dir, system_prompt)

    def _ralph_requires_docs_only(self, prompt: str, qa_block: str = "") -> bool:
        text = f"{prompt}\n{qa_block}".lower()
        has_docs_only = any(
            needle in text
            for needle in (
                "只输出调研",
                "只做调研",
                "只输出文档",
                "只输出调研与架构文档",
                "只输出架构文档",
                "只做架构设计",
                "documentation only",
                "docs only",
                "research only",
                "architecture only",
            )
        )
        has_no_code = any(
            needle in text
            for needle in (
                "不写代码",
                "不要写代码",
                "no code",
                "do not write code",
                "without code",
            )
        )
        has_no_impl = any(
            needle in text
            for needle in (
                "不做实现",
                "不要实现",
                "不做开发",
                "no implementation",
                "do not implement",
                "without implementation",
            )
        )
        return has_docs_only or (has_no_code and has_no_impl)

    def _ralph_apply_docs_only_constraints(self, story: dict, idx: int) -> dict:
        normalized = self._ralph_normalize_story(story, idx)
        if normalized.get("role") not in {"researcher", "writer"}:
            normalized["role"] = "writer"

        filtered: list[str] = []
        for item in normalized.get("acceptanceCriteria", []):
            lowered = str(item).strip().lower()
            if not lowered:
                continue
            if any(
                needle in lowered
                for needle in (
                    "typecheck passes",
                    "tests pass",
                    "test passes",
                    "lint passes",
                    "build passes",
                    "实现",
                    "编写代码",
                    "可运行",
                    "运行通过",
                    "启动服务",
                    "implement ",
                    "implementation",
                    "write code",
                    "run the app",
                    "runnable",
                )
            ):
                continue
            filtered.append(str(item).strip())

        if not filtered:
            if normalized["role"] == "researcher":
                filtered = ["输出调研结论文档", "给出清晰推荐方案"]
            else:
                filtered = ["输出设计文档", "说明目录/模块职责与后续实现建议"]

        normalized["acceptanceCriteria"] = filtered
        title = str(normalized.get("title") or f"Story {idx}").strip()
        if normalized["role"] == "researcher":
            normalized["description"] = (
                f"作为研究员，我希望输出《{title}》相关调研文档，以便团队后续决策与实现。"
            )
        else:
            normalized["description"] = (
                f"作为文档作者，我希望输出《{title}》相关设计文档，以便团队后续实现。"
            )
        return normalized

    def _ralph_required_roles(self, prompt: str, qa_block: str = "") -> list[str]:
        text = f"{prompt}\n{qa_block}".lower()
        aliases: dict[str, tuple[re.Pattern[str], ...]] = {
            "researcher": (
                re.compile(r"\bresearcher\b"),
                re.compile(r"研究员"),
            ),
            "writer": (
                re.compile(r"\bwriter\b"),
                re.compile(r"文档作者"),
            ),
            "qa": (
                re.compile(r"\bqa\b"),
                re.compile(r"测试角色"),
                re.compile(r"测试人员"),
                re.compile(r"质量保证"),
            ),
            "engineer": (
                re.compile(r"\bengineer\b"),
                re.compile(r"工程师"),
            ),
            "devops": (
                re.compile(r"\bdevops\b"),
                re.compile(r"运维角色"),
            ),
            "debugger": (
                re.compile(r"\bdebugger\b"),
                re.compile(r"调试者"),
            ),
        }
        required: list[str] = []
        for role, patterns in aliases.items():
            if any(pattern.search(text) for pattern in patterns):
                required.append(role)
        return required

    def _ralph_story_role_match_score(self, story: dict, target_role: str) -> int:
        text = " ".join(
            [
                str(story.get("title") or ""),
                str(story.get("description") or ""),
                " ".join(str(item) for item in story.get("acceptanceCriteria", []) or []),
            ]
        ).lower()
        score = 0
        keyword_map = {
            "researcher": ("research", "analysis", "analyze", "investigate", "调研", "分析", "收集"),
            "writer": ("write", "document", "readme", "guide", "文档", "编写", "说明", "指南"),
            "qa": ("qa", "verify", "validation", "test", "check", "review", "验收", "验证", "检查", "测试"),
            "engineer": ("implement", "build", "develop", "code", "实现", "开发", "编码"),
            "devops": ("deploy", "release", "ops", "infra", "运维", "部署"),
            "debugger": ("debug", "fix", "investigate bug", "排查", "修复", "调试"),
        }
        for needle in keyword_map.get(target_role, ()):
            if needle in text:
                score += 3
        current_role = str(story.get("role") or "").strip().lower()
        if current_role == target_role:
            score += 10
        if target_role == "qa" and current_role == "writer":
            score += 2
        if target_role == "writer" and current_role == "qa":
            score += 1
        return score

    def _ralph_enforce_required_roles(self, stories: list[dict], required_roles: list[str]) -> list[dict]:
        if not stories or not required_roles:
            return stories
        normalized_roles = {str(story.get("role") or "").strip().lower() for story in stories}
        missing = [role for role in required_roles if role not in normalized_roles]
        if not missing:
            return stories

        for role in missing:
            best_idx = -1
            best_score = -1
            for idx, story in enumerate(stories):
                current_role = str(story.get("role") or "").strip().lower()
                if current_role == role:
                    best_idx = idx
                    best_score = 999
                    break
                score = self._ralph_story_role_match_score(story, role)
                if role == "qa" and current_role not in {"qa", "engineer", "writer"}:
                    score -= 2
                if score > best_score:
                    best_score = score
                    best_idx = idx
            if best_idx >= 0:
                stories[best_idx]["role"] = role
        return stories

    def _ralph_apply_prompt_constraints_to_prd(
        self,
        prd: dict,
        prompt: str,
        qa_block: str = "",
    ) -> dict:
        stories = prd.get("stories") or prd.get("userStories") or []
        if not stories:
            return prd

        docs_only = self._ralph_requires_docs_only(prompt, qa_block)
        normalized_stories: list[dict] = []
        for idx, story in enumerate(stories, start=1):
            if docs_only:
                normalized_stories.append(self._ralph_apply_docs_only_constraints(story, idx))
            else:
                normalized_stories.append(self._ralph_normalize_story(story, idx))

        normalized_stories = self._ralph_enforce_required_roles(
            normalized_stories,
            self._ralph_required_roles(prompt, qa_block),
        )

        prd["stories"] = normalized_stories
        prd["userStories"] = normalized_stories
        return prd

    async def _ralph_generate_prd_json(
        self,
        prd_md: str,
        run_dir: Path,
        prompt: str = "",
        qa_block: str = "",
    ) -> tuple[Optional[dict], str]:
        try:
            template = self._load_ralph_template("prd/prd_rule.md")
            system_prompt = self._load_ralph_template("prd/prd_system.md")
        except Exception:
            return None, ""
        message = self._render_ralph_template(template, prd_md=prd_md)
        text = await self._ralph_call_model(message, run_dir, system_prompt)
        parsed = self._ralph_extract_json(text)
        if not parsed or (not parsed.get("stories") and not parsed.get("userStories")):
            parsed = self._ralph_build_prd_fallback(prd_md)
        if parsed:
            parsed = self._ralph_apply_prompt_constraints_to_prd(parsed, prompt=prompt, qa_block=qa_block)
        return parsed, text

    def _ralph_format_questions(self, questions: list[dict]) -> str:
        lines: list[str] = []
        for idx, q in enumerate(questions, start=1):
            question = str(q.get("question") or "").strip()
            options = q.get("options") if isinstance(q, dict) else None
            lines.append(f"{idx}. {question}")
            if isinstance(options, dict):
                for key in sorted(options.keys()):
                    lines.append(f"   {key}. {options[key]}")
        return "\n".join(lines).strip()

    def _ralph_build_qa_block(self, questions: list[dict], answers_text: str) -> str:
        if not questions:
            return f"Answers:\n{answers_text}".strip()
        formatted = self._ralph_format_questions(questions)
        return f"Questions:\n{formatted}\n\nAnswers:\n{answers_text}".strip()

    def _ralph_build_prd_preview(self, prd: dict) -> str:
        stories = prd.get("stories") or prd.get("userStories") or []
        project = str(prd.get("project") or "Ralph Task").strip()
        is_en = self._is_english(self._load_language_setting())

        lines = [
            f"Project: {project}" if is_en else f"项目: {project}",
            f"Stories: {len(stories)}",
        ]
        if stories:
            lines.append("Top stories:" if is_en else "主要 stories:")
            for story in stories[:3]:
                if not isinstance(story, dict):
                    continue
                story_id = str(story.get("id") or "-").strip()
                title = str(story.get("title") or story.get("name") or "").strip()
                if title:
                    lines.append(f"- {story_id}: {title}")
        if len(stories) > 3:
            remaining = len(stories) - 3
            lines.append(
                f"... and {remaining} more"
                if is_en else
                f"... 以及另外 {remaining} 个"
            )
        return "\n".join(lines).strip()

    def _ralph_sanitize_prd_markdown(self, prd_md: str, prd: dict) -> str:
        stories = prd.get("stories") or prd.get("userStories") or []
        if not prd_md.strip() or not stories:
            return prd_md

        ordered_story_meta: list[dict[str, object]] = []
        for story in stories:
            if not isinstance(story, dict):
                continue
            ordered_story_meta.append(
                {
                    "role": str(story.get("role") or "").strip().lower(),
                    "description": str(story.get("description") or "").strip(),
                    "acceptanceCriteria": [
                        str(item).strip()
                        for item in story.get("acceptanceCriteria", [])
                        if str(item).strip()
                    ],
                }
            )

        story_heading = re.compile(r"^###\s+(?:User Story \d+[:：]|用户故事\s*\d+[:：])\s*(.+?)\s*$")
        generic_story_heading = re.compile(r"^###\s+(?:故事|Story)\s*\d+[:：]\s*(.+?)\s*$")
        bullet_story_heading = re.compile(r"^-\s+\*\*US-\d+[:：](.+?)\*\*\s*$")
        lines = prd_md.splitlines()
        sanitized: list[str] = []
        story_cursor = -1
        current_title = ""
        current_role = ""
        current_description = ""
        current_criteria: list[str] = []
        in_acceptance = False
        for line in lines:
            stripped = line.strip()
            match = story_heading.match(stripped)
            generic_match = generic_story_heading.match(stripped)
            bullet_match = bullet_story_heading.match(stripped)
            if match:
                current_title = match.group(1).strip()
                story_cursor += 1
                meta = ordered_story_meta[story_cursor] if story_cursor < len(ordered_story_meta) else {}
                current_role = str(meta.get("role") or "")
                current_description = str(meta.get("description") or "")
                current_criteria = list(meta.get("acceptanceCriteria") or [])
                in_acceptance = False
                sanitized.append(line)
                continue
            if generic_match:
                current_title = generic_match.group(1).strip()
                story_cursor += 1
                meta = ordered_story_meta[story_cursor] if story_cursor < len(ordered_story_meta) else {}
                current_role = str(meta.get("role") or "")
                current_description = str(meta.get("description") or "")
                current_criteria = list(meta.get("acceptanceCriteria") or [])
                in_acceptance = False
                sanitized.append(line)
                continue
            if bullet_match:
                current_title = bullet_match.group(1).strip()
                story_cursor += 1
                meta = ordered_story_meta[story_cursor] if story_cursor < len(ordered_story_meta) else {}
                current_role = str(meta.get("role") or "")
                current_description = str(meta.get("description") or "")
                current_criteria = list(meta.get("acceptanceCriteria") or [])
                in_acceptance = False
                sanitized.append(line)
                continue
            if (stripped.startswith("**描述**：") or stripped.startswith("**描述**:")) and current_description:
                prefix = "**描述**：" if "：" in stripped else "**描述**: "
                sanitized.append(f"{prefix}{current_description}")
                continue
            if stripped.startswith("- **Description:**") and current_description:
                sanitized.append(f"- **Description:** {current_description}")
                continue
            if stripped.startswith("- **描述**：") and current_description:
                sanitized.append(f"- **描述**：{current_description}")
                continue
            if stripped.startswith("**验收标准**") or stripped.startswith("**Acceptance Criteria**"):
                in_acceptance = True
                sanitized.append(line)
                continue
            if stripped.startswith("- **验收标准**") or stripped.startswith("- **Acceptance Criteria**"):
                in_acceptance = True
                sanitized.append(line)
                continue
            if (stripped.startswith("**角色**：") or stripped.startswith("**角色**:")) and current_role:
                prefix = "**角色**：" if "：" in stripped else "**角色**: "
                sanitized.append(f"{prefix}{current_role}")
                continue
            if stripped.startswith("- **Role:**") and current_role:
                sanitized.append(f"- **Role:** {current_role}")
                continue
            if stripped.startswith("- **角色**：") and current_role:
                sanitized.append(f"- **角色**：{current_role}")
                continue
            if in_acceptance and stripped.startswith("### "):
                in_acceptance = False
                current_criteria = []
            if in_acceptance and bullet_story_heading.match(stripped):
                in_acceptance = False
                current_criteria = []
            if in_acceptance and stripped.startswith("- "):
                item_text = stripped[2:].strip()
                if current_criteria:
                    if item_text in current_criteria:
                        sanitized.append(line)
                    continue
                if current_role in {"researcher", "writer"}:
                    lowered = item_text.lower()
                    if (
                        lowered in {"typecheck passes", "tests pass"}
                        or "typecheck passes" in lowered
                        or "tests pass" in lowered
                    ):
                        continue
            sanitized.append(line)

        return "\n".join(sanitized).strip()

    def _ralph_build_prd_ready_content(
        self,
        prd_md_path: Path,
        prd_json_path: Path,
        prd: dict,
        prd_md: str,
    ) -> str:
        preview = self._ralph_build_prd_preview(prd)
        is_en = self._is_english(self._load_language_setting())
        sanitized_prd_md = self._ralph_sanitize_prd_markdown(prd_md, prd)
        intro = (
            "✅ PRD generated. Review then /ralph approve"
            if is_en else
            "✅ PRD 已生成，请审核后执行 /ralph approve"
        )
        full_label = "Full PRD:" if is_en else "完整 PRD："
        return (
            f"{intro}\n\n"
            f"PRD Markdown: {prd_md_path}\n"
            f"PRD JSON: {prd_json_path}\n\n"
            f"{preview}\n\n"
            f"{full_label}\n\n"
            f"{sanitized_prd_md}"
        ).strip()

    def _ralph_pick_role(self, story: dict) -> str:
        criteria_text = ""
        if isinstance(story, dict):
            role = str(story.get("role") or "").strip().lower().strip("*`_ ")
            criteria = story.get("acceptanceCriteria", [])
            criteria_text = " ".join(str(item).strip() for item in criteria if str(item).strip()).lower()
            title = str(story.get("title") or "").lower()
            description = str(story.get("description") or "").lower()
            combined = " ".join(part for part in (title, description, criteria_text) if part)
            implementation_actions = (
                "implement",
                "build",
                "develop",
                "create",
                "define",
                "code",
                "write code",
                "实现",
                "开发",
                "构建",
                "搭建",
                "创建",
                "定义",
                "编写代码",
                "初始化",
                "启动服务",
            )
            implementation_targets = (
                "api",
                "endpoint",
                "route",
                "crud",
                "database",
                "migration",
                "schema",
                "model",
                "script",
                "app/",
                "src/",
                "main.py",
                "pyproject.toml",
                "接口",
                "端点",
                "数据库",
                "数据模型",
                "模型",
                "初始化脚本",
                "单元测试",
                "集成测试",
                "服务",
            )
            documentation_signals = (
                "文档",
                "文案",
                "说明",
                "指南",
                "readme",
                "guide",
                "copy",
                "提示信息",
                "按钮文案",
            )
            research_signals = (
                "research",
                "investigate",
                "analyze",
                "analysis",
                "调研",
                "分析",
                "选型",
                "可行性",
                "依据",
                "对比",
                "记录",
            )
            if role in {"researcher", "writer"}:
                has_impl_action = any(signal in combined for signal in implementation_actions)
                has_impl_target = any(signal in combined for signal in implementation_targets)
                has_doc_signal = any(signal in combined for signal in documentation_signals)
                has_research_signal = any(signal in combined for signal in research_signals)
                if has_impl_action and has_impl_target and not (has_doc_signal or has_research_signal):
                    return "engineer"
            if role:
                return role
        else:
            title = ""
            combined = ""

        if any(k in combined for k in ["doc", "readme", "guide", "说明", "文档"]):
            return "writer"
        if any(k in combined for k in ["test", "qa", "quality", "lint", "覆盖"]):
            return "qa"
        if any(k in combined for k in ["deploy", "release", "ci", "pipeline", "docker"]):
            return "devops"
        if any(k in combined for k in ["bug", "fix", "error", "crash", "异常"]):
            return "debugger"
        if any(k in combined for k in ["research", "investigate", "analyze", "分析", "调研"]):
            return "researcher"
        return "engineer"

    def _ralph_role_focus(self, role: str) -> str:
        return self._load_role_focus(role)

    def _ralph_project_tree_summary(self, project_dir: Path, limit: int = 20) -> str:
        if not project_dir.exists():
            return "- (project directory does not exist yet)"

        entries: list[str] = []
        ignored_roots = {".git", ".venv", ".pytest_cache", ".mypy_cache", "__pycache__"}
        try:
            for path in sorted(project_dir.rglob("*")):
                if any(part in ignored_roots for part in path.parts):
                    continue
                rel = path.relative_to(project_dir)
                entries.append(str(rel))
                if len(entries) >= limit:
                    break
        except Exception:
            return "- (failed to enumerate project directory)"

        if not entries:
            return "- (project directory is currently empty)"
        return "\n".join(f"- {entry}" for entry in entries)

    def _ralph_story_requires_typecheck(self, story: dict) -> bool:
        criteria = [str(item).strip() for item in story.get("acceptanceCriteria", []) if str(item).strip()]
        return any("typecheck passes" in item.lower() for item in criteria)

    def _ralph_story_requires_tests(self, story: dict) -> bool:
        criteria = [str(item).strip() for item in story.get("acceptanceCriteria", []) if str(item).strip()]
        return any("tests pass" in item.lower() for item in criteria)

    def _ralph_ensure_hatchling_wheel_packages(self, project_dir: Path) -> bool:
        pyproject_path = project_dir / "pyproject.toml"
        app_dir = project_dir / "app"
        if not pyproject_path.exists() or not app_dir.is_dir():
            return False
        try:
            pyproject_text = pyproject_path.read_text(encoding="utf-8")
        except Exception:
            return False
        if "[tool.hatch.build.targets.wheel]" in pyproject_text:
            return False
        build_backend_match = re.search(r'build-backend\s*=\s*["\']hatchling\.build["\']', pyproject_text)
        if not build_backend_match:
            return False
        patched = pyproject_text.rstrip() + '\n\n[tool.hatch.build.targets.wheel]\npackages = ["app"]\n'
        try:
            pyproject_path.write_text(patched, encoding="utf-8")
        except Exception:
            return False
        return True

    async def _ralph_prepare_typecheck_environment(self, project_dir: Path, story: dict) -> str:
        if not self._ralph_story_requires_typecheck(story):
            return ""
        if not project_dir.exists():
            return ""

        notes: list[str] = []
        if self._ralph_ensure_hatchling_wheel_packages(project_dir):
            notes.append("Patched pyproject.toml with [tool.hatch.build.targets.wheel] packages = [\"app\"]")

        sync_commands = [
            ["uv", "sync", "--extra", "dev"],
            ["uv", "sync"],
        ]
        seen: set[tuple[str, ...]] = set()
        for command in sync_commands:
            key = tuple(command)
            if key in seen:
                continue
            seen.add(key)
            try:
                proc = await asyncio.create_subprocess_exec(
                    *command,
                    cwd=str(project_dir),
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.STDOUT,
                )
                stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=60)
            except Exception as exc:
                notes.append(f"$ {' '.join(command)}\n{exc}")
                continue

            output = stdout.decode("utf-8", errors="replace").strip()
            if proc.returncode == 0:
                if output:
                    trimmed = output[-1200:] if len(output) > 1200 else output
                    notes.append(f"$ {' '.join(command)}\n{trimmed}")
                return "\n\n".join(notes)
            trimmed = output[-1200:] if len(output) > 1200 else output
            notes.append(f"$ {' '.join(command)}\n{trimmed}")
        return "\n\n".join(notes)

    async def _ralph_collect_verification_evidence(self, project_dir: Path, story: dict) -> str:
        if not self._ralph_story_requires_typecheck(story) and not self._ralph_story_requires_tests(story):
            return ""
        if not project_dir.exists():
            return ""

        target = "."
        if (project_dir / "app").is_dir():
            target = "app"
        elif (project_dir / "src").is_dir():
            target = "src"

        async def _collect_group(commands: list[list[str]]) -> tuple[bool | None, str]:
            seen: set[tuple[str, ...]] = set()
            first_success = ""
            for command in commands:
                key = tuple(command)
                if key in seen:
                    continue
                seen.add(key)
                try:
                    proc = await asyncio.create_subprocess_exec(
                        *command,
                        cwd=str(project_dir),
                        stdout=asyncio.subprocess.PIPE,
                        stderr=asyncio.subprocess.STDOUT,
                    )
                    stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=15)
                except Exception:
                    continue

                output = stdout.decode("utf-8", errors="replace").strip()
                if len(output) > 2000:
                    output = output[-2000:]
                prefix = f"$ {' '.join(command)}"
                evidence = f"{prefix}\n{output}" if output else prefix
                if proc.returncode != 0:
                    return False, evidence
                if output and not first_success:
                    first_success = evidence
                else:
                    first_success = first_success or prefix
                return True, first_success
            return None, ""

        success_evidence: list[str] = []

        if self._ralph_story_requires_typecheck(story):
            typecheck_commands: list[list[str]] = []
            venv_mypy = project_dir / ".venv" / "bin" / "mypy"
            if venv_mypy.is_file():
                typecheck_commands.append([str(venv_mypy), target])
            typecheck_commands.append(["uv", "run", "--no-project", "mypy", target])
            ok, evidence = await _collect_group(typecheck_commands)
            if ok is False:
                return evidence
            if evidence:
                success_evidence.append(evidence)

        if self._ralph_story_requires_tests(story):
            test_commands: list[list[str]] = []
            venv_pytest = project_dir / ".venv" / "bin" / "pytest"
            if venv_pytest.is_file():
                test_commands.append([str(venv_pytest), "-q"])
            test_commands.append(["uv", "run", "--no-project", "pytest", "-q"])
            ok, evidence = await _collect_group(test_commands)
            if ok is False:
                return evidence
            if evidence:
                success_evidence.append(evidence)

        return "\n\n".join(part for part in success_evidence if part).strip()

    async def _ralph_verification_passed(self, project_dir: Path, story: dict) -> bool:
        require_typecheck = self._ralph_story_requires_typecheck(story)
        require_tests = self._ralph_story_requires_tests(story)
        if not require_typecheck and not require_tests:
            return True
        if not project_dir.exists():
            return False

        target = "."
        if (project_dir / "app").is_dir():
            target = "app"
        elif (project_dir / "src").is_dir():
            target = "src"

        async def _group_passes(commands: list[list[str]]) -> bool:
            seen: set[tuple[str, ...]] = set()
            for command in commands:
                key = tuple(command)
                if key in seen:
                    continue
                seen.add(key)
                try:
                    proc = await asyncio.create_subprocess_exec(
                        *command,
                        cwd=str(project_dir),
                        stdout=asyncio.subprocess.PIPE,
                        stderr=asyncio.subprocess.STDOUT,
                    )
                    await asyncio.wait_for(proc.communicate(), timeout=15)
                except Exception:
                    continue
                return proc.returncode == 0
            return False

        if require_typecheck:
            typecheck_commands: list[list[str]] = []
            venv_mypy = project_dir / ".venv" / "bin" / "mypy"
            if venv_mypy.is_file():
                typecheck_commands.append([str(venv_mypy), target])
            typecheck_commands.append(["uv", "run", "--no-project", "mypy", target])
            if not await _group_passes(typecheck_commands):
                return False

        if require_tests:
            test_commands: list[list[str]] = []
            venv_pytest = project_dir / ".venv" / "bin" / "pytest"
            if venv_pytest.is_file():
                test_commands.append([str(venv_pytest), "-q"])
            test_commands.append(["uv", "run", "--no-project", "pytest", "-q"])
            if not await _group_passes(test_commands):
                return False

        return True

    def _ralph_control_roots(self) -> set[str]:
        return {".git", ".venv", ".pytest_cache", ".mypy_cache", "__pycache__", "tasks"}

    def _ralph_control_files(self) -> set[str]:
        return {
            "prd.json",
            "progress.txt",
            "state.json",
            "answers.txt",
            "questions.json",
            "revision_feedback.txt",
        }

    def _ralph_should_ignore_artifact(self, path: Path, base: Path | None = None) -> bool:
        try:
            rel_parts = path.relative_to(base).parts if base is not None else path.parts
        except Exception:
            rel_parts = path.parts
        if any(part in self._ralph_control_roots() for part in rel_parts):
            return True
        if path.name in self._ralph_control_files():
            return True
        return False

    def _ralph_materialized_artifacts(self, paths: list[Path]) -> list[Path]:
        materialized: list[Path] = []
        for path in paths:
            try:
                if not path.exists():
                    continue
                if path.is_dir():
                    for child in sorted(path.rglob("*")):
                        if not child.is_file() or child.stat().st_size <= 0:
                            continue
                        if self._ralph_should_ignore_artifact(child, path):
                            continue
                        materialized.append(child)
                    continue
                if (
                    path.is_file()
                    and path.stat().st_size > 0
                    and not self._ralph_should_ignore_artifact(path, path.parent)
                ):
                    materialized.append(path)
            except Exception:
                continue
        deduped: list[Path] = []
        seen: set[str] = set()
        for path in materialized:
            key = str(path)
            if key in seen:
                continue
            seen.add(key)
            deduped.append(path)
        return deduped

    def _ralph_sync_run_dir_outputs_to_project_dir(self, run_dir: Path, project_dir: Path) -> list[Path]:
        synced: list[Path] = []
        if not run_dir.exists():
            return synced

        project_dir.mkdir(parents=True, exist_ok=True)
        for path in sorted(run_dir.rglob("*")):
            if not path.is_file():
                continue
            try:
                rel = path.relative_to(run_dir)
            except Exception:
                continue
            if not rel.parts:
                continue
            if self._ralph_should_ignore_artifact(path, run_dir):
                continue
            dest = project_dir / rel
            dest.parent.mkdir(parents=True, exist_ok=True)
            try:
                shutil.copy2(path, dest)
            except Exception:
                continue
            synced.append(dest)
        return synced

    def _ralph_build_recovery_prompt(
        self,
        story: dict,
        run_dir: Path,
        project_dir: Path,
        latest_output: str = "",
        failure_reason: str = "",
        task_prompt: str = "",
    ) -> str:
        role = self._ralph_pick_role(story)
        story_id = str(story.get("id") or "US-001").strip().lower().replace("_", "-")
        criteria = [str(item).strip() for item in story.get("acceptanceCriteria", []) if str(item).strip()]
        artifact_paths: list[str] = []
        for item in criteria:
            for match in re.findall(r"`([^`]+)`", item):
                artifact_paths.append(match)
            for match in re.findall(r"\b(?:docs|src|templates|static|tests)/[A-Za-z0-9_./-]+\b", item):
                artifact_paths.append(match)
        fallback_doc_path = project_dir / "docs" / f"{story_id}-researcher-notes.md"
        if role in {"researcher", "writer"} and not artifact_paths:
            artifact_paths.append(str(fallback_doc_path))
        artifact_paths = list(dict.fromkeys(artifact_paths))
        artifact_block = "\n".join(f"- {path}" for path in artifact_paths) if artifact_paths else "- Follow the acceptance criteria exactly."
        extra_rules = ""
        if role in {"researcher", "writer"}:
            extra_rules = (
                f"\nFor this {role} story, Create the documentation file even if the project directory is empty.\n"
                f"Write the deliverable into: {fallback_doc_path}\n"
                "Do not stop at checking directories or reading files.\n"
            )
        context_block = ""
        if failure_reason.strip():
            context_block += f"Failure reason:\n{failure_reason.strip()}\n"
        if latest_output.strip():
            context_block += f"Latest captured output:\n{latest_output.strip()}\n"
        if context_block:
            context_block = f"{context_block}\n"
        project_tree = self._ralph_project_tree_summary(project_dir)
        targeted_hints = self._ralph_targeted_recovery_hints(
            story=story,
            project_dir=project_dir,
            latest_output=latest_output,
        )
        story_block = json.dumps(
            {
                "id": story.get("id"),
                "title": story.get("title"),
                "description": story.get("description"),
                "acceptanceCriteria": criteria,
                "role": role,
            },
            ensure_ascii=False,
            indent=2,
        )
        completion_guard = (
            "At least one file inside the Project Directory must be created or updated before you mark this story complete.\n"
            "If you only read files or only update progress/prd without changing project artifacts, the story will be rejected.\n"
        )
        packaging_hint = self._ralph_python_packaging_hint(project_dir)
        verification_guidance = (
            "Verification guidance:\n"
            "- If `uv run <tool>` triggers editable build or package selection failures, rerun with `uv run --no-project <tool>` or the existing virtualenv tool binary.\n"
            "- If typecheck or tests fail because imports/dependencies are missing, install or sync the required dependencies first, then rerun verification until it passes.\n"
            "- Do not stop at reporting a verification failure; fix it, rerun it, then update prd.json and progress.txt.\n"
        )
        source_policy = ""
        if self._ralph_prompt_requires_local_only(task_prompt):
            source_policy = (
                "Source policy:\n"
                "- Use only local files from the allowed repo/workspace paths already given.\n"
                "- Do not access the internet, WebFetch, web search, or any remote documentation.\n"
                "- If local evidence is missing, state that gap in the deliverable instead of going online.\n"
            )
        return (
            "The current story is still incomplete.\n"
            "Do not inspect or restate the task. Do not start the next story.\n"
            "Perform the missing deliverables now and finish this story in one pass.\n"
            f"Ralph run directory: {run_dir}\n"
            f"PRD JSON: {run_dir / 'prd.json'}\n"
            f"Progress log: {run_dir / 'progress.txt'}\n"
            f"Project directory: {project_dir}\n"
            f"{context_block}"
            "Current story:\n"
            f"{story_block}\n"
            "Current project directory snapshot:\n"
            f"{project_tree}\n"
            f"{targeted_hints}"
            f"{completion_guard}"
            f"{source_policy}"
            f"{verification_guidance}"
            f"{packaging_hint}"
            "Required artifacts:\n"
            f"{artifact_block}\n"
            f"{extra_rules}"
            "After creating the required artifacts:\n"
            "1. Update prd.json for the current story and mark it complete.\n"
            "2. Append a completion entry to progress.txt.\n"
            "3. Stop after finishing this story."
        )

    def _ralph_story_command_phrase(self, story: dict) -> str:
        if isinstance(story, dict):
            for criterion in story.get("acceptanceCriteria", []) or []:
                matches = re.findall(r"`([^`]+)`", str(criterion))
                if matches:
                    return matches[0].strip()
            title = str(story.get("title") or "").strip()
            if title:
                return title
        return ""

    def _ralph_targeted_story_hints(self, story: dict, project_dir: Path, latest_output: str = "") -> str:
        title = str(story.get("title") or "").strip()
        command_phrase = self._ralph_story_command_phrase(story)
        command_tokens = [token for token in re.findall(r"[A-Za-z0-9_]+", f"{title} {command_phrase}".lower()) if token]
        criteria_text = "\n".join(str(item) for item in (story.get("acceptanceCriteria") or []))
        combined_text = f"{title}\n{criteria_text}".lower()
        pyproject_path = project_dir / "pyproject.toml"
        pyproject_text = ""
        with contextlib.suppress(Exception):
            pyproject_text = pyproject_path.read_text(encoding="utf-8")
        is_scaffold_story = any(
            token in combined_text
            for token in ("脚手架", "初始化", "scaffold", "bootstrap", "uv init", "health")
        )

        source_candidates: list[Path] = []
        if is_scaffold_story and pyproject_path.exists():
            source_candidates.append(pyproject_path)
        app_main = project_dir / "app" / "main.py"
        if is_scaffold_story and (app_main.exists() or (project_dir / "app").is_dir()):
            source_candidates.append(app_main if app_main.exists() else project_dir / "app")
        if (project_dir / "src").is_dir():
            source_candidates.extend(sorted((project_dir / "src").glob("**/cli.py")))
        if (project_dir / "app").is_dir():
            source_candidates.extend(sorted((project_dir / "app").glob("**/cli.py")))

        symbol = ""
        symbol_match = re.search(
            r"cannot import name ['`\"]?([A-Za-z_][A-Za-z0-9_]*)['`\"]?\s+from ['`\"]([A-Za-z0-9_.]+)['`\"]",
            latest_output,
            re.IGNORECASE,
        )
        if symbol_match:
            symbol = symbol_match.group(1).strip()
            module_name = symbol_match.group(2).strip()
            module_rel = Path(*module_name.split(".")).with_suffix(".py")
            for candidate in (project_dir / "src" / module_rel, project_dir / module_rel):
                if candidate.exists():
                    source_candidates.insert(0, candidate)
                    break

        mypy_path = ""
        mypy_message = ""
        mypy_fix_hint = ""
        mypy_match = re.search(
            r"(?P<path>[A-Za-z0-9_./-]+\.py):(?P<line>\d+):\s*error:\s*(?P<message>.+?)(?:\s+\[[^\]]+\])?\s*$",
            latest_output,
            re.IGNORECASE | re.MULTILINE,
        )
        if mypy_match:
            mypy_path = f"{mypy_match.group('path')}:{mypy_match.group('line')}"
            mypy_message = mypy_match.group("message").strip()
            mypy_rel = Path(mypy_match.group("path"))
            for candidate in (project_dir / mypy_rel, project_dir / "src" / mypy_rel, project_dir / "app" / mypy_rel):
                if candidate.exists():
                    source_candidates.insert(0, candidate)
                    break
            lowered_mypy = mypy_message.lower()
            if "missing a return type annotation" in lowered_mypy:
                mypy_fix_hint = "Add an explicit return type annotation to the flagged function."
            elif "missing type parameters" in lowered_mypy:
                mypy_fix_hint = "Add the missing generic type parameters instead of leaving the container type implicit."
            elif "incompatible return value type" in lowered_mypy:
                mypy_fix_hint = "Make the function return value match its declared return type, or update the annotation to the correct type."
            elif "incompatible types in assignment" in lowered_mypy:
                mypy_fix_hint = "Align the assigned value type with the variable annotation instead of relying on implicit coercion."
            else:
                mypy_fix_hint = "Fix the reported type error in the flagged file before rerunning the full verification."

        missing_modules = {match.lower() for match in re.findall(r"No module named ([A-Za-z0-9_.-]+)", latest_output, re.IGNORECASE)}
        dependency_hints: list[str] = []
        if "pytest" in missing_modules or "mypy" in missing_modules:
            dependency_hints.append(
                "Install the missing dev tools in the project environment, then run `uv sync --extra dev` before rerunning verification."
            )
        if is_scaffold_story:
            declared_lower = pyproject_text.lower()
            missing_runtime: list[str] = []
            for package in ("fastapi", "jinja2"):
                if package not in declared_lower:
                    missing_runtime.append(package)
            if missing_runtime:
                packages = " ".join(missing_runtime)
                dependency_hints.append(
                    f"Add the missing runtime dependencies to `pyproject.toml` (for example with `uv add {packages}`), then resync the environment."
                )
            if "pytest" not in declared_lower or "mypy" not in declared_lower:
                dependency_hints.append(
                    "Ensure `pytest` and `mypy` are declared as dev dependencies in `pyproject.toml` so verification commands can run."
                )

        deduped_sources: list[Path] = []
        seen_sources: set[str] = set()
        for candidate in source_candidates:
            key = str(candidate)
            if key in seen_sources:
                continue
            seen_sources.add(key)
            deduped_sources.append(candidate)
        source_candidates = deduped_sources

        related_tests: list[Path] = []
        tests_dir = project_dir / "tests"
        if tests_dir.is_dir():
            for candidate in sorted(tests_dir.rglob("test*.py")):
                stem = candidate.stem.lower()
                text = ""
                with contextlib.suppress(Exception):
                    text = candidate.read_text(encoding="utf-8").lower()
                if symbol and (symbol.lower() in stem or symbol.lower() in text):
                    related_tests.append(candidate)
                    continue
                if any(token in stem or token in text for token in command_tokens):
                    related_tests.append(candidate)
            deduped_tests: list[Path] = []
            seen_tests: set[str] = set()
            for candidate in related_tests:
                key = str(candidate)
                if key in seen_tests:
                    continue
                seen_tests.add(key)
                deduped_tests.append(candidate)
            related_tests = deduped_tests[:3]

        if not source_candidates and not related_tests and not symbol and not mypy_path and not dependency_hints:
            return ""

        def _display(path: Path) -> str:
            try:
                return str(path.relative_to(project_dir))
            except Exception:
                return str(path)

        lines = ["Targeted story hints:", "Start with these files:"]
        for candidate in source_candidates[:3]:
            lines.append(f"- {_display(candidate)}")
        for candidate in related_tests:
            lines.append(f"- {_display(candidate)}")
        if symbol and source_candidates:
            lines.append(f"Implement the missing symbol `{symbol}` in `{_display(source_candidates[0])}`.")
        if mypy_path:
            lines.append(f"Address the mypy failure at `{mypy_path}`: {mypy_message}.")
            if mypy_fix_hint:
                lines.append(mypy_fix_hint)
        for hint in dependency_hints:
            lines.append(hint)
        if command_phrase:
            lines.append(f"Wire the `{command_phrase}` command through the CLI entrypoint so the command path is reachable.")
        if related_tests:
            lines.append(f"Run the focused test first: `pytest -q {_display(related_tests[0])}`.")
        return "\n".join(lines) + "\n"

    def _ralph_targeted_recovery_hints(self, story: dict, project_dir: Path, latest_output: str) -> str:
        hints = self._ralph_targeted_story_hints(story=story, project_dir=project_dir, latest_output=latest_output)
        if not hints:
            return ""
        return f"{hints}After the focused test passes, rerun the full required verification.\n"

    def _ralph_python_packaging_hint(self, project_dir: Path) -> str:
        pyproject_path = project_dir / "pyproject.toml"
        app_dir = project_dir / "app"
        if not pyproject_path.exists() or not app_dir.is_dir():
            return ""
        try:
            pyproject_text = pyproject_path.read_text(encoding="utf-8")
        except Exception:
            return ""
        if "[tool.hatch.build.targets.wheel]" in pyproject_text:
            return ""
        return (
            "Python packaging hint:\n"
            "- If Hatchling says it cannot determine which files to ship and your code is under `app/`, add this to `pyproject.toml`:\n"
            "  [tool.hatch.build.targets.wheel]\n"
            "  packages = [\"app\"]\n"
            "- Then rerun `uv sync` or the verification command.\n"
        )

    def _ralph_subagent_workspace(self, run_dir: Path, project_dir: Path, task_prompt: str = "") -> Path:
        roots = self._ralph_allowed_read_roots(run_dir, project_dir, task_prompt=task_prompt)
        try:
            common = os.path.commonpath([str(root) for root in roots])
        except Exception:
            return run_dir
        workspace = Path(common)
        return workspace if workspace.is_dir() else run_dir

    def _ralph_expected_artifact_paths(self, story: dict, project_dir: Path) -> list[Path]:
        role = self._ralph_pick_role(story)
        story_id = str(story.get("id") or "US-001").strip().lower().replace("_", "-")
        criteria = [str(item).strip() for item in story.get("acceptanceCriteria", []) if str(item).strip()]
        artifact_paths: list[Path] = []
        for item in criteria:
            for match in re.findall(r"`([^`]+)`", item):
                artifact_paths.append(project_dir / match if not Path(match).is_absolute() else Path(match))
            for match in re.findall(r"\b(?:app|data|docs|src|templates|static|tests)/[A-Za-z0-9_./-]+\b", item):
                artifact_paths.append(project_dir / match if not Path(match).is_absolute() else Path(match))
            for match in re.findall(r"(?<![A-Za-z0-9_.-])((?:/|~)[^\s，。,;；:：]+)", item):
                if "、" in match:
                    continue
                candidate = Path(match).expanduser()
                if candidate.suffix:
                    artifact_paths.append(candidate)
                else:
                    artifact_paths.append(candidate)
        if role in {"researcher", "writer"} and not artifact_paths:
            artifact_paths.append(project_dir / "docs" / f"{story_id}-researcher-notes.md")
        if role not in {"researcher", "writer"} and not artifact_paths:
            artifact_paths.append(project_dir)
        deduped: list[Path] = []
        seen: set[str] = set()
        for path in artifact_paths:
            key = str(path)
            if key in seen:
                continue
            seen.add(key)
            deduped.append(path)
        if role not in {"researcher", "writer"}:
            has_file_target = any(path.suffix for path in deduped)
            if not has_file_target and str(project_dir) not in seen:
                deduped.append(project_dir)
        return deduped

    def _ralph_snapshot_artifacts(self, paths: list[Path]) -> dict[str, float | None]:
        snapshot: dict[str, float | None] = {}
        for path in paths:
            try:
                if not path.exists():
                    snapshot[str(path)] = None
                    continue
                if path.is_dir():
                    latest: float | None = path.stat().st_mtime
                    for child in path.rglob("*"):
                        if not child.is_file():
                            continue
                        if self._ralph_should_ignore_artifact(child, path):
                            continue
                        child_mtime = child.stat().st_mtime
                        latest = child_mtime if latest is None else max(latest, child_mtime)
                    snapshot[str(path)] = latest
                    continue
                if self._ralph_should_ignore_artifact(path, path.parent):
                    snapshot[str(path)] = None
                    continue
                snapshot[str(path)] = path.stat().st_mtime
            except Exception:
                snapshot[str(path)] = None
        return snapshot

    def _ralph_changed_artifacts(
        self,
        paths: list[Path],
        snapshot: dict[str, float | None],
        anchor_mtime: float | None = None,
    ) -> list[Path]:
        changed: list[Path] = []
        for path in paths:
            try:
                if not path.exists():
                    continue
                if path.is_dir():
                    previous = snapshot.get(str(path))
                    for child in sorted(path.rglob("*")):
                        if not child.is_file() or child.stat().st_size <= 0:
                            continue
                        if self._ralph_should_ignore_artifact(child, path):
                            continue
                        current = child.stat().st_mtime
                        threshold = previous
                        if anchor_mtime is not None:
                            threshold = anchor_mtime if threshold is None else max(threshold, anchor_mtime)
                        if threshold is None or current > threshold:
                            changed.append(child)
                    continue
                if not path.is_file() or path.stat().st_size <= 0:
                    continue
                if self._ralph_should_ignore_artifact(path, path.parent):
                    continue
                previous = snapshot.get(str(path))
                current = path.stat().st_mtime
                threshold = previous
                if anchor_mtime is not None:
                    threshold = anchor_mtime if threshold is None else max(threshold, anchor_mtime)
                if threshold is None or current > threshold:
                    changed.append(path)
            except Exception:
                continue
        deduped: list[Path] = []
        seen: set[str] = set()
        for path in changed:
            key = str(path)
            if key in seen:
                continue
            seen.add(key)
            deduped.append(path)
        return deduped

    def _ralph_autofinalize_story_from_artifacts(
        self,
        prd_path: Path,
        progress_path: Path,
        story_index: int,
        artifact_paths: list[Path],
    ) -> bool:
        try:
            prd = json.loads(prd_path.read_text(encoding="utf-8"))
        except Exception:
            return False
        stories = prd.get("stories") or prd.get("userStories") or []
        if story_index >= len(stories):
            return False

        story = stories[story_index]
        self._ralph_mark_story_complete(story)
        try:
            prd_path.write_text(json.dumps(prd, ensure_ascii=False, indent=2), encoding="utf-8")
        except Exception:
            return False

        lang = self._load_language_setting()
        is_en = self._is_english(lang)
        ts = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime())
        artifact_lines = "\n".join(f"- {path}" for path in artifact_paths) if artifact_paths else "- (none)"
        if is_en:
            entry = (
                f"\n## {ts} - {story.get('id') or f'Story {story_index + 1}'}\n"
                "- Auto-finalized after detecting completed artifact output.\n"
                "- Files or artifacts touched:\n"
                f"{artifact_lines}\n"
                "- Learnings / gotchas: The subagent produced the deliverable but did not persist progress/prd state; Ralph synchronized it automatically.\n"
                "---\n"
            )
        else:
            entry = (
                f"\n## {ts} - {story.get('id') or f'故事 {story_index + 1}'}\n"
                "- 检测到交付物已生成，Ralph 已自动收口当前 story。\n"
                "- Files or artifacts touched:\n"
                f"{artifact_lines}\n"
                "- Learnings / gotchas: 子 agent 已产出交付物，但未回写 progress/prd 状态，已由 Ralph 自动同步。\n"
                "---\n"
            )
        try:
            with open(progress_path, "a", encoding="utf-8") as f:
                f.write(entry)
        except Exception:
            return False
        return True

    def _ralph_can_supervisor_autofinalize(
        self,
        story: dict,
        changed_artifacts: list[Path],
        verification_passed: bool,
    ) -> bool:
        role = self._ralph_pick_role(story)
        if role in {"researcher", "writer"}:
            return False
        if not changed_artifacts:
            return False
        if self._ralph_story_requires_typecheck(story) and not verification_passed:
            return False
        return True

    async def _ralph_retry_incomplete_story(
        self,
        stdio,
        run_dir: Path,
        project_dir: Path,
        story: dict,
        chat_id: str,
        latest_output: str = "",
        failure_reason: str = "",
        task_prompt: str = "",
        prd_path: Path | None = None,
        progress_path: Path | None = None,
        story_index: int | None = None,
    ) -> ACPResponse | None:
        lang = self._load_language_setting()
        policy = self._format_language_policy(lang)
        system_template = self._load_ralph_template("rules/ralph_system.md")
        subagent_system = self._render_ralph_template(
            system_template,
            language=lang,
            language_policy=policy,
        )
        session_workspace = self._ralph_subagent_workspace(run_dir, project_dir, task_prompt=task_prompt)
        recovery_prompt = self._ralph_build_recovery_prompt(
            story,
            run_dir=run_dir,
            project_dir=project_dir,
            latest_output=latest_output,
            failure_reason=failure_reason,
            task_prompt=task_prompt,
        )
        timeout_budgets = self._ralph_timeout_retry_budgets(getattr(self.adapter, "timeout", None))
        recovery_idle_watchdog = self._ralph_recovery_idle_watchdog_seconds_for_attempt(
            story,
            float(getattr(self, "_ralph_recovery_idle_watchdog_seconds", 60.0)),
            latest_output=latest_output,
        )
        configured_poll_interval = float(getattr(self, "_ralph_prompt_poll_seconds", 2.0))
        poll_interval = configured_poll_interval
        if recovery_idle_watchdog > 0:
            poll_interval = min(poll_interval, max(0.01, recovery_idle_watchdog / 4))
        artifact_watchdog = float(getattr(self, "_ralph_artifact_watchdog_seconds", 8.0))
        artifact_paths = self._ralph_expected_artifact_paths(story, project_dir)
        activity_paths = artifact_paths or [project_dir]
        last_timeout: StdioACPTimeoutError | None = None

        for attempt, budget in enumerate(timeout_budgets, start=1):
            session_id = await stdio._client.create_session(
                workspace=session_workspace,
                model=self.model,
                system_prompt=subagent_system,
                approval_mode="yolo",
            )
            self._ralph_active_sessions[chat_id] = session_id
            attempt_timeout: StdioACPTimeoutError | None = None
            prompt_cancel_reason = ""
            auto_finalized = False
            try:
                last_activity = asyncio.get_running_loop().time()
                activity_clock = {"last": last_activity}
                activity_snapshot = self._ralph_snapshot_artifacts(activity_paths)
                artifact_snapshot = self._ralph_snapshot_artifacts(artifact_paths)
                artifact_watch_started: float | None = None
                observed_artifacts: list[Path] = []

                async def mark_prompt_activity(*_args, **_kwargs):
                    activity_clock["last"] = asyncio.get_running_loop().time()

                prompt_kwargs = {
                    "message": recovery_prompt,
                    "timeout": budget,
                }
                if self._ralph_prompt_supports_callbacks(stdio._client.prompt):
                    prompt_kwargs.update(
                        {
                            "on_chunk": mark_prompt_activity,
                            "on_tool_call": mark_prompt_activity,
                            "on_event": mark_prompt_activity,
                        }
                    )
                prompt_task = asyncio.create_task(
                    stdio._client.prompt(session_id, **prompt_kwargs)
                )
                while not prompt_task.done():
                    await asyncio.sleep(poll_interval)
                    now = asyncio.get_running_loop().time()
                    last_activity = max(last_activity, activity_clock["last"])

                    synced_outputs = self._ralph_sync_run_dir_outputs_to_project_dir(run_dir, project_dir)
                    if synced_outputs:
                        last_activity = now
                        activity_snapshot = self._ralph_snapshot_artifacts(activity_paths)
                        artifact_snapshot = self._ralph_snapshot_artifacts(artifact_paths)

                    activity_changed = self._ralph_changed_artifacts(activity_paths, activity_snapshot)
                    if activity_changed:
                        last_activity = now
                        activity_snapshot = self._ralph_snapshot_artifacts(activity_paths)

                    changed_artifacts = self._ralph_changed_artifacts(artifact_paths, artifact_snapshot)
                    if changed_artifacts:
                        last_activity = now
                        if artifact_watch_started is None:
                            artifact_watch_started = now
                        for path in changed_artifacts:
                            if not any(str(path) == str(item) for item in observed_artifacts):
                                observed_artifacts.append(path)
                    if artifact_watch_started is not None and now - artifact_watch_started >= artifact_watchdog:
                        candidate_artifacts = observed_artifacts or self._ralph_materialized_artifacts(artifact_paths)
                        verification_passed = await self._ralph_verification_passed(
                            project_dir=project_dir,
                            story=story,
                        )
                        if (
                            self._ralph_can_supervisor_autofinalize(
                                story,
                                candidate_artifacts,
                                verification_passed,
                            )
                            and prd_path is not None
                            and progress_path is not None
                            and story_index is not None
                            and self._ralph_autofinalize_story_from_artifacts(
                                prd_path=prd_path,
                                progress_path=progress_path,
                                story_index=story_index,
                                artifact_paths=candidate_artifacts,
                            )
                        ):
                            auto_finalized = True
                            with contextlib.suppress(Exception):
                                await stdio._client.cancel(session_id)
                            prompt_task.cancel()
                            with contextlib.suppress(asyncio.CancelledError):
                                await prompt_task
                            break
                        artifact_watch_started = now
                        observed_artifacts = candidate_artifacts
                    artifact_snapshot = self._ralph_snapshot_artifacts(artifact_paths)

                    if recovery_idle_watchdog > 0 and now - last_activity >= recovery_idle_watchdog:
                        prompt_cancel_reason = f"Prompt timeout (idle watchdog after {int(recovery_idle_watchdog)}s)"
                        with contextlib.suppress(Exception):
                            await stdio._client.cancel(session_id)
                        prompt_task.cancel()
                        with contextlib.suppress(asyncio.CancelledError):
                            await prompt_task
                        break

                self._ralph_sync_run_dir_outputs_to_project_dir(run_dir, project_dir)
                if auto_finalized:
                    return ACPResponse(content="", error=None)
                try:
                    return await prompt_task
                except asyncio.CancelledError:
                    raise StdioACPTimeoutError(prompt_cancel_reason or "Prompt timeout (idle)")
            except StdioACPTimeoutError as exc:
                attempt_timeout = exc
            except asyncio.TimeoutError:
                attempt_timeout = StdioACPTimeoutError("Prompt timeout (idle)")
            finally:
                if attempt_timeout is not None:
                    with contextlib.suppress(Exception):
                        await stdio._client.cancel(session_id)
            if attempt_timeout is not None:
                last_timeout = attempt_timeout
                if attempt >= len(timeout_budgets):
                    break
                logger.warning(
                    "Ralph recovery prompt timed out for {} (attempt {}/{}, next timeout={}s)",
                    chat_id,
                    attempt,
                    len(timeout_budgets),
                    timeout_budgets[attempt],
                )

        return ACPResponse(content="", error=str(last_timeout or "Prompt timeout (idle)"))

    def _ralph_timeout_retry_budgets(self, timeout: Optional[int]) -> list[int]:
        base = int(timeout or getattr(self.adapter, "timeout", 0) or 600)
        budgets = [base]
        for _ in range(2):
            budgets.append(budgets[-1] * 2)
        return budgets

    def _ralph_idle_watchdog_seconds(
        self,
        story: Optional[dict],
        base_seconds: float,
    ) -> float:
        base = float(base_seconds or 0)
        role = self._ralph_pick_role(story or {})
        if role in {"researcher", "writer"}:
            return max(base, 300.0)
        return base

    def _ralph_recovery_idle_watchdog_seconds_for_attempt(
        self,
        story: Optional[dict],
        base_seconds: float,
        latest_output: str = "",
    ) -> float:
        base = self._ralph_idle_watchdog_seconds(story, base_seconds)
        role = self._ralph_pick_role(story or {})
        lowered = (latest_output or "").lower()
        if role in {"engineer", "qa"} and any(
            token in lowered
            for token in (
                "supervisor verification evidence",
                "no module named",
                "error:",
                "traceback",
                "tests failed",
                "typecheck",
            )
        ):
            return max(base, 180.0)
        return base

    def _ralph_prompt_supports_callbacks(self, prompt_fn) -> bool:
        try:
            params = inspect.signature(prompt_fn).parameters
        except (TypeError, ValueError):
            return False
        return {"on_chunk", "on_tool_call", "on_event"}.issubset(params.keys())

    def _ralph_extract_final_summary(self, progress_text: str) -> str:
        if not progress_text:
            return ""
        markers = [
            "## Result",
            "## Summary",
            "## Conclusion",
            "## 结果",
            "## 总结",
            "## 结论",
            "结论",
            "最终结论",
            "最终结果",
        ]
        for marker in markers:
            idx = progress_text.rfind(marker)
            if idx != -1:
                section = progress_text[idx:].strip()
                lines = section.splitlines()
                if len(lines) > 1:
                    trimmed = []
                    for i, line in enumerate(lines):
                        if i > 0 and line.startswith("## "):
                            break
                        trimmed.append(line)
                    section = "\n".join(trimmed).strip()
                return section
        lines = progress_text.strip().splitlines()
        return "\n".join(lines[-40:]).strip()

    def _ralph_project_summary(self, project_dir: Optional[str]) -> str:
        if not project_dir:
            return ""
        try:
            base = Path(project_dir).expanduser()
        except Exception:
            return ""
        if not base.exists():
            return ""

        candidates: list[Path] = []
        direct = base / "report.md"
        if direct.exists():
            candidates.append(direct)
        for pattern in ("*.md", "*.txt"):
            candidates.extend(sorted(base.glob(pattern)))

        seen: set[Path] = set()
        for candidate in candidates:
            if candidate in seen or not candidate.is_file():
                continue
            seen.add(candidate)
            try:
                text = candidate.read_text(encoding="utf-8").strip()
            except Exception:
                continue
            if not text:
                continue
            lines = [line.strip() for line in text.splitlines() if line.strip()]
            if not lines:
                continue
            return "\n".join(lines[:12]).strip()
        return ""

    def _ralph_pick_final_summary(
        self,
        progress_text: str,
        last_progress: str,
        progress_path: Path,
        project_dir: Optional[str] = None,
    ) -> str:
        summary = self._ralph_extract_final_summary(progress_text)
        normalized = summary.strip()
        if normalized in {"", "[RALPH_DONE]", "# Ralph Progress"} or len(normalized) < 40:
            artifact_summary = self._ralph_project_summary(project_dir)
            if len(artifact_summary) >= 40:
                return artifact_summary
            fallback = (last_progress or "").strip()
            if len(fallback) >= 40:
                return fallback
            return self._msg("ralph_summary_fallback", path=progress_path)
        return summary

    def _ralph_strip_markers(self, text: str) -> str:
        if not text:
            return text
        cleaned: list[str] = []
        for line in text.splitlines():
            stripped = line.strip()
            upper = stripped.upper()
            if upper in {"[RALPH_DONE]", "STOPPED", "RESUMED", "RESTART_DETECTED", "AUTO_RESUMED"}:
                continue
            if "执行中" in stripped and re.search(r"story\s*\d+/\d+", stripped, re.IGNORECASE):
                continue
            if re.search(r"\b(executing|running|in progress)\b", stripped, re.IGNORECASE) and re.search(
                r"story\s*\d+/\d+",
                stripped,
                re.IGNORECASE,
            ):
                continue
            cleaned.append(line)
        return "\n".join(cleaned).strip()

    async def _ralph_create(self, msg: InboundMessage, prompt: str) -> None:
        chat_id = msg.chat_id
        if chat_id in self._ralph_tasks:
            await self._send_command_reply(msg, self._msg("ralph_running_stop"))
            return
        current_run = self._ralph_get_current(chat_id)
        if current_run:
            run_dir = self._ralph_run_dir(chat_id, current_run)
            state = self._ralph_get_effective_state(chat_id, run_dir)
            if state.get("status") in {"awaiting_answers", "needs_approval", "running"}:
                await self._send_command_reply(msg, self._msg("ralph_pending_stop"))
                return

        run_id = f"{uuid.uuid4().hex[:8]}-{int(asyncio.get_running_loop().time())}"
        run_dir = self._ralph_run_dir(chat_id, run_id)
        run_dir.mkdir(parents=True, exist_ok=True)

        await self._send_command_reply(msg, self._msg("ralph_generating_questions"))
        questions_task = asyncio.create_task(self._ralph_generate_questions(prompt, run_dir))
        start_ts = asyncio.get_running_loop().time()
        last_ping = start_ts
        while not questions_task.done():
            await asyncio.sleep(2)
            now = asyncio.get_running_loop().time()
            if now - last_ping >= 15:
                elapsed = int(now - start_ts)
                await self._send_command_reply(msg, self._msg("ralph_generating_questions_ping", elapsed=elapsed))
                last_ping = now
        questions, _raw = await questions_task
        lang = self._load_language_setting()
        if not questions or not self._ralph_questions_match_language(questions, lang):
            questions = self._ralph_default_questions(prompt)
        self._ralph_questions_path(run_dir).write_text(
            json.dumps(questions, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

        progress_path = self._ralph_progress_path(run_dir)
        if not progress_path.exists():
            progress_path.write_text("# Ralph Progress\n", encoding="utf-8")

        state = {
            "run_id": run_id,
            "channel": msg.channel,
            "prompt": prompt,
            "status": "awaiting_answers",
            "created_at": asyncio.get_running_loop().time(),
            "updated_at": asyncio.get_running_loop().time(),
            "story_index": 0,
            "pass_index": 0,
            "round": 0,
            "last_progress": "",
        }
        project_dir = self._ralph_extract_project_dir(prompt)
        if project_dir:
            try:
                resolved = self._ralph_resolve_project_dir(project_dir)
                if resolved.suffix and not resolved.is_dir():
                    resolved = resolved.parent
                resolved.mkdir(parents=True, exist_ok=True)
                state["project_dir"] = str(resolved)
            except Exception:
                state["project_dir"] = str(project_dir)
        self._ralph_save_state(run_dir, state)
        self._ralph_set_current(chat_id, run_id)

        questions_text = self._ralph_format_questions(questions)
        await self._send_command_reply(
            msg,
            self._msg("ralph_questions_prompt", questions=questions_text),
            streaming=False,
        )

    async def _ralph_handle_answers(self, msg: InboundMessage, answers_text: str) -> None:
        chat_id = msg.chat_id
        run_id = self._ralph_get_current(chat_id)
        if not run_id:
            await self._send_command_reply(msg, self._msg("ralph_no_pending_task"))
            return

        run_dir = self._ralph_run_dir(chat_id, run_id)
        state = self._ralph_get_effective_state(chat_id, run_dir)
        status = state.get("status")
        if status == "needs_approval":
            await self._ralph_revise_prd(msg, answers_text)
            return
        if status in {"generating_prd", "generating_prd_json"}:
            await self._send_command_reply(msg, self._msg("ralph_prd_generation_busy"))
            return
        if status != "awaiting_answers":
            await self._send_command_reply(msg, self._msg("ralph_no_questions"))
            return

        questions: list[dict] = []
        questions_path = self._ralph_questions_path(run_dir)
        if questions_path.exists():
            try:
                questions = json.loads(questions_path.read_text(encoding="utf-8"))
            except Exception:
                questions = []

        qa_block = self._ralph_build_qa_block(questions, answers_text)
        self._ralph_answers_path(run_dir).write_text(answers_text, encoding="utf-8")
        await self._ralph_generate_prd_artifacts(
            msg=msg,
            run_dir=run_dir,
            state=state,
            qa_block=qa_block,
            intro_message=self._msg("ralph_generating_prd"),
        )

    async def _ralph_generate_prd_artifacts(
        self,
        msg: InboundMessage,
        run_dir: Path,
        state: dict,
        qa_block: str,
        intro_message: str,
    ) -> None:
        state["status"] = "generating_prd"
        state["updated_at"] = asyncio.get_running_loop().time()
        self._ralph_save_state(run_dir, state)

        await self._send_command_reply(msg, intro_message)
        prd_task = asyncio.create_task(self._ralph_generate_prd_md(state.get("prompt", ""), qa_block, run_dir))
        prd_start = asyncio.get_running_loop().time()
        prd_last_ping = prd_start
        while not prd_task.done():
            await asyncio.sleep(2)
            now = asyncio.get_running_loop().time()
            if now - prd_last_ping >= 60:
                elapsed = int(now - prd_start)
                await self._send_command_reply(msg, self._msg("ralph_generating_prd_ping", elapsed=elapsed))
                prd_last_ping = now
        prd_md = await prd_task
        if not prd_md.strip():
            state["status"] = "awaiting_answers"
            state["updated_at"] = asyncio.get_running_loop().time()
            self._ralph_save_state(run_dir, state)
            await self._send_command_reply(msg, self._msg("ralph_prd_failed"))
            return

        self._ralph_tasks_dir(run_dir).mkdir(parents=True, exist_ok=True)
        slug = self._ralph_slugify(state.get("prompt", ""))
        prd_md_path = self._ralph_prd_md_path(run_dir, slug)
        prd_md_path.write_text(prd_md, encoding="utf-8")

        state["status"] = "generating_prd_json"
        state["updated_at"] = asyncio.get_running_loop().time()
        self._ralph_save_state(run_dir, state)
        await self._send_command_reply(msg, self._msg("ralph_generating_prd_json"))
        prd_json_task = asyncio.create_task(
            self._ralph_generate_prd_json(
                prd_md,
                run_dir,
                prompt=state.get("prompt", ""),
                qa_block=qa_block,
            )
        )
        prd_json_start = asyncio.get_running_loop().time()
        prd_json_last_ping = prd_json_start
        while not prd_json_task.done():
            await asyncio.sleep(2)
            now = asyncio.get_running_loop().time()
            if now - prd_json_last_ping >= 60:
                elapsed = int(now - prd_json_start)
                await self._send_command_reply(msg, self._msg("ralph_generating_prd_json_ping", elapsed=elapsed))
                prd_json_last_ping = now
        prd, raw = await prd_json_task
        prd_path = self._ralph_prd_path(run_dir)
        if prd:
            prd_path.write_text(json.dumps(prd, indent=2, ensure_ascii=False), encoding="utf-8")
        else:
            prd_path.write_text(raw or "{}", encoding="utf-8")
        if prd:
            prd_md = self._ralph_sanitize_prd_markdown(prd_md, prd)
            prd_md_path.write_text(prd_md, encoding="utf-8")

        state["status"] = "needs_approval"
        state["updated_at"] = asyncio.get_running_loop().time()
        self._ralph_save_state(run_dir, state)

        await self._send_command_reply(
            msg,
            self._ralph_build_prd_ready_content(prd_md_path, prd_path, prd or {}, prd_md),
            streaming=False,
        )

    async def _ralph_maybe_handle_answer(self, msg: InboundMessage) -> bool:
        return await self._ralph_maybe_handle_followup(msg)

    async def _ralph_revise_prd(self, msg: InboundMessage, feedback_text: str) -> None:
        run_id = self._ralph_get_current(msg.chat_id)
        if not run_id:
            await self._send_command_reply(msg, self._msg("ralph_no_pending_task"))
            return

        run_dir = self._ralph_run_dir(msg.chat_id, run_id)
        state = self._ralph_get_effective_state(msg.chat_id, run_dir)
        if state.get("status") != "needs_approval":
            await self._send_command_reply(msg, self._msg("ralph_no_questions"))
            return

        questions: list[dict] = []
        questions_path = self._ralph_questions_path(run_dir)
        if questions_path.exists():
            try:
                questions = json.loads(questions_path.read_text(encoding="utf-8"))
            except Exception:
                questions = []

        existing_answers = ""
        answers_path = self._ralph_answers_path(run_dir)
        if answers_path.exists():
            existing_answers = answers_path.read_text(encoding="utf-8").strip()

        qa_parts: list[str] = []
        if existing_answers:
            qa_parts.append(self._ralph_build_qa_block(questions, existing_answers))
        previous_prd_path = self._ralph_latest_prd_md_path(run_dir)
        if previous_prd_path and previous_prd_path.exists():
            previous_prd = previous_prd_path.read_text(encoding="utf-8").strip()
            if previous_prd:
                qa_parts.append(
                    "## Existing PRD Markdown\n"
                    "Preserve the current PRD structure, story detail, and acceptance criteria unless the feedback explicitly asks you to change them.\n"
                    "Apply the revision as a targeted update instead of rewriting the PRD from scratch.\n\n"
                    f"{previous_prd}"
                )
        if feedback_text.strip():
            qa_parts.append(f"## PRD Revision Feedback\n{feedback_text.strip()}")
            revision_path = run_dir / "revision_feedback.txt"
            existing_notes = revision_path.read_text(encoding="utf-8").strip() if revision_path.exists() else ""
            joined = f"{existing_notes}\n\n{feedback_text.strip()}".strip() if existing_notes else feedback_text.strip()
            revision_path.write_text(joined, encoding="utf-8")

        await self._ralph_generate_prd_artifacts(
            msg=msg,
            run_dir=run_dir,
            state=state,
            qa_block="\n\n".join(part for part in qa_parts if part.strip()),
            intro_message=self._msg("ralph_prd_feedback_regenerating"),
        )

    async def _ralph_maybe_handle_followup(self, msg: InboundMessage) -> bool:
        content = (msg.content or "").strip()
        if not content or content.startswith("/"):
            return False
        run_id = self._ralph_get_current(msg.chat_id)
        if not run_id:
            return False
        run_dir = self._ralph_run_dir(msg.chat_id, run_id)
        state = self._ralph_get_effective_state(msg.chat_id, run_dir)
        status = state.get("status")
        if status == "awaiting_answers":
            await self._ralph_handle_answers(msg, content)
            return True
        if status in {"generating_prd", "generating_prd_json"}:
            await self._send_command_reply(msg, self._msg("ralph_prd_generation_busy"))
            return True
        if status == "needs_approval":
            await self._ralph_revise_prd(msg, content)
            return True
        return False

    async def _ralph_approve(self, msg: InboundMessage) -> None:
        chat_id = msg.chat_id
        if chat_id in self._ralph_tasks:
            await self._send_command_reply(msg, self._msg("ralph_running_status"))
            return

        run_id = self._ralph_get_current(chat_id)
        if not run_id:
            await self._send_command_reply(msg, self._msg("ralph_no_prd_to_approve"))
            return

        run_dir = self._ralph_run_dir(chat_id, run_id)
        state = self._ralph_get_effective_state(chat_id, run_dir)
        if state.get("status") == "awaiting_answers":
            await self._send_command_reply(msg, self._msg("ralph_waiting_answers"))
            return
        if state.get("status") not in {"needs_approval", "approved"}:
            await self._send_command_reply(msg, self._msg("ralph_invalid_status_execute", status=state.get("status")))
            return

        prd_path = self._ralph_prd_path(run_dir)
        if not prd_path.exists():
            await self._send_command_reply(msg, self._msg("ralph_no_prd_yet"))
            return

        state["status"] = "approved"
        state["updated_at"] = asyncio.get_running_loop().time()
        state = self._ralph_prime_current_story(run_dir, state)
        self._ralph_save_state(run_dir, state)

        task = asyncio.create_task(self._ralph_run_loop(msg.channel, chat_id, run_dir))
        self._ralph_tasks[chat_id] = task
        await self._send_command_reply(msg, self._msg("ralph_approved_start"))

    async def _ralph_stop(self, msg: InboundMessage) -> None:
        chat_id = msg.chat_id
        run_id = self._ralph_get_current(chat_id)
        run_dir = self._ralph_run_dir(chat_id, run_id) if run_id else None

        session_id = self._ralph_active_sessions.pop(chat_id, None)
        if session_id:
            try:
                stdio = await self.adapter._get_stdio_adapter()
                if stdio._client:
                    await stdio._client.cancel(session_id)
            except Exception:
                pass

        task = self._ralph_tasks.pop(chat_id, None)
        if task and not task.done():
            task.cancel()

        if run_dir and run_dir.exists():
            state = self._ralph_load_state(run_dir)
            state["status"] = "stopped"
            state["updated_at"] = asyncio.get_running_loop().time()
            self._ralph_save_state(run_dir, state)
            progress_path = self._ralph_progress_path(run_dir)
            try:
                with open(progress_path, "a", encoding="utf-8") as f:
                    f.write("\nSTOPPED\n")
            except Exception:
                pass

        await self._send_command_reply(msg, self._msg("ralph_stopped"))

    async def _ralph_status(self, msg: InboundMessage) -> None:
        chat_id = msg.chat_id
        run_id = self._ralph_get_current(chat_id)
        if not run_id:
            await self._send_command_reply(msg, self._msg("ralph_no_task"))
            return

        run_dir = self._ralph_run_dir(chat_id, run_id)
        state = self._ralph_get_effective_state(chat_id, run_dir)
        raw_status = state.get("status", "unknown")
        status = self._format_ralph_status(raw_status)
        progress_path = self._ralph_progress_path(run_dir)
        tail = ""
        if progress_path.exists():
            content = progress_path.read_text(encoding="utf-8")
            cleaned = self._ralph_strip_markers(content)
            lines = [line for line in cleaned.strip().splitlines() if line.strip()] if cleaned else []
            tail = "\n".join(lines[-6:])

        current_line = ""
        current_story = state.get("current_story_index")
        current_total = state.get("current_story_total")
        current_pass = state.get("current_pass_index")
        current_pass_total = state.get("current_pass_total")
        current_title = state.get("current_story_title")
        current_role = state.get("current_story_role")
        started_at = state.get("current_started_at")
        if raw_status == "running" and current_story and current_total and current_pass and current_pass_total:
            elapsed = ""
            if started_at:
                try:
                    elapsed = f"({int(time.time() - float(started_at))}s)" if self._is_english(self._load_language_setting()) else f"（{int(time.time() - float(started_at))}s）"
                except Exception:
                    elapsed = ""
            if self._is_english(self._load_language_setting()):
                current_line = (
                    f"Current: Story {current_story}/{current_total} "
                    f"Pass {current_pass}/{current_pass_total}{elapsed}"
                )
            else:
                current_line = (
                    f"当前: 第 {current_story}/{current_total} 个任务，"
                    f"第 {current_pass}/{current_pass_total} 轮{elapsed}"
                )
            if current_title:
                current_line += f": {current_title}" if self._is_english(self._load_language_setting()) else f"：{current_title}"
        role_line = ""
        if raw_status == "running" and current_role:
            formatted_role = self._format_ralph_role(str(current_role))
            role_line = (
                f"Subagent role: {formatted_role}"
                if self._is_english(self._load_language_setting())
                else f"子角色: {formatted_role}"
            )

        await self._send_command_reply(
            msg,
            "\n".join(
                part
                for part in [
                    f"Ralph status: {status}" if self._is_english(self._load_language_setting()) else f"Ralph 状态: {status}",
                    f"run_id: {run_id}",
                    current_line,
                    role_line,
                    "",
                    tail,
                ]
                if part is not None
            ).strip(),
        )

    async def _ralph_resume(self, msg: InboundMessage) -> None:
        chat_id = msg.chat_id
        run_id = self._ralph_get_current(chat_id)
        if not run_id:
            await self._send_command_reply(msg, self._msg("ralph_resume_none"))
            return

        run_dir = self._ralph_run_dir(chat_id, run_id)
        state = self._ralph_get_effective_state(chat_id, run_dir)
        status = state.get("status")

        if status == "awaiting_answers":
            await self._send_command_reply(msg, self._msg("ralph_resume_waiting_answers"))
            return
        if status == "needs_approval":
            await self._send_command_reply(msg, self._msg("ralph_resume_waiting_approve"))
            return
        if status == "done":
            state["status"] = "archived"
            state["updated_at"] = asyncio.get_running_loop().time()
            self._ralph_save_state(run_dir, state)
            self._ralph_clear_current(chat_id)
            await self._send_command_reply(msg, self._msg("ralph_archived"))
            return

        if status in {"running", "approved", "stopped"}:
            if chat_id in self._ralph_tasks and not self._ralph_tasks[chat_id].done():
                await self._send_command_reply(msg, self._msg("ralph_resume_running"))
                return
            prd_path = self._ralph_prd_path(run_dir)
            if not prd_path.exists():
                await self._send_command_reply(msg, self._msg("ralph_resume_no_prd"))
                return
            state["status"] = "approved"
            state["updated_at"] = asyncio.get_running_loop().time()
            state = self._ralph_prime_current_story(run_dir, state)
            self._ralph_save_state(run_dir, state)
            progress_path = self._ralph_progress_path(run_dir)
            try:
                with open(progress_path, "a", encoding="utf-8") as f:
                    f.write("\nRESUMED\n")
            except Exception:
                pass
            task = asyncio.create_task(self._ralph_run_loop(msg.channel, chat_id, run_dir))
            self._ralph_tasks[chat_id] = task
            await self._send_command_reply(msg, self._msg("ralph_resumed"))
            return

        await self._send_command_reply(msg, self._msg("ralph_resume_invalid_status", status=status))

    async def _ralph_handle_run_loop_exception(
        self,
        channel: str,
        chat_id: str,
        run_dir: Path,
        error: Exception,
    ) -> None:
        logger.exception("Ralph run loop crashed for {}:{}", channel, chat_id)
        state = self._ralph_load_state(run_dir)
        if str(state.get("status") or "").strip().lower() in {"done", "failed", "stopped", "archived"}:
            return

        reason = str(error).strip() or error.__class__.__name__
        if len(reason) > 300:
            reason = reason[:297] + "..."
        state["status"] = "stopped"
        state["updated_at"] = asyncio.get_running_loop().time()
        state["last_progress"] = self._msg("ralph_internal_paused", error=reason)
        self._ralph_save_state(run_dir, state)
        await self._ralph_send_update(channel, chat_id, state["last_progress"])
        await self._ralph_send_update(channel, chat_id, self._msg("ralph_story_incomplete_paused"))

    async def _ralph_run_loop(self, channel: str, chat_id: str, run_dir: Path) -> None:
        try:
            await self._ralph_run_loop_impl(channel, chat_id, run_dir)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            await self._ralph_handle_run_loop_exception(channel, chat_id, run_dir, exc)
        finally:
            self._ralph_active_sessions.pop(chat_id, None)
            current_task = asyncio.current_task()
            if self._ralph_tasks.get(chat_id) is current_task:
                self._ralph_tasks.pop(chat_id, None)

    async def _ralph_run_loop_impl(self, channel: str, chat_id: str, run_dir: Path) -> None:
        state = self._ralph_load_state(run_dir)
        state["status"] = "running"
        state["updated_at"] = asyncio.get_running_loop().time()
        self._ralph_save_state(run_dir, state)

        prd_path = self._ralph_prd_path(run_dir)
        if not prd_path.exists():
            state["status"] = "failed"
            state["updated_at"] = asyncio.get_running_loop().time()
            self._ralph_save_state(run_dir, state)
            self._ralph_tasks.pop(chat_id, None)
            await self._ralph_send_update(
                channel,
                chat_id,
                self._msg("ralph_prd_missing_execute"),
            )
            return

        try:
            prd = json.loads(prd_path.read_text(encoding="utf-8"))
        except Exception:
            state["status"] = "failed"
            state["updated_at"] = asyncio.get_running_loop().time()
            self._ralph_save_state(run_dir, state)
            self._ralph_tasks.pop(chat_id, None)
            await self._ralph_send_update(
                channel,
                chat_id,
                self._msg("ralph_prd_json_invalid"),
            )
            return

        stories = prd.get("stories")
        if not stories and prd.get("userStories"):
            stories = prd.get("userStories")
            prd["stories"] = stories
        stories = stories or []
        if not stories:
            state["status"] = "failed"
            state["updated_at"] = asyncio.get_running_loop().time()
            self._ralph_save_state(run_dir, state)
            self._ralph_tasks.pop(chat_id, None)
            await self._ralph_send_update(
                channel,
                chat_id,
                self._msg("ralph_prd_no_stories"),
            )
            return

        progress_path = self._ralph_progress_path(run_dir)
        if not progress_path.exists():
            progress_path.write_text("# Ralph Progress\n", encoding="utf-8")

        stdio = await self.adapter._get_stdio_adapter()
        await stdio.connect()
        if not stdio._client:
            await self.bus.publish_outbound(OutboundMessage(
                channel=channel,
                chat_id=chat_id,
                content=self._msg("ralph_subagent_connect_failed"),
            ))
            return

        for story_index in range(state.get("story_index", 0), len(stories)):
            story = stories[story_index]
            if isinstance(story, dict) and self._ralph_story_completed(story):
                state["pass_index"] = 0
                state["story_index"] = story_index + 1
                state["updated_at"] = asyncio.get_running_loop().time()
                self._ralph_save_state(run_dir, state)
                continue
            raw_passes = story.get("passes", 1)
            if isinstance(raw_passes, bool):
                passes = 1
            else:
                passes = max(1, int(raw_passes))
            start_pass = state.get("pass_index", 0) if story_index == state.get("story_index", 0) else 0

            for pass_index in range(start_pass, passes):
                if state.get("status") in {"stopped", "failed"}:
                    return

                title = ""
                role = ""
                if isinstance(story, dict):
                    title = str(story.get("title") or story.get("name") or "").strip()
                    role = self._ralph_pick_role(story)
                previous_story_index = state.get("current_story_index")
                previous_pass_index = state.get("current_pass_index")
                previous_started_at = state.get("current_started_at")
                resumed_same_story_pass = (
                    previous_story_index == story_index + 1
                    and previous_pass_index == pass_index + 1
                    and previous_started_at is not None
                )
                state["current_story_index"] = story_index + 1
                state["current_story_total"] = len(stories)
                state["current_pass_index"] = pass_index + 1
                state["current_pass_total"] = passes
                state["current_story_title"] = title
                state["current_story_role"] = role
                if resumed_same_story_pass:
                    state["current_started_at"] = previous_started_at
                else:
                    state["current_started_at"] = time.time()
                self._ralph_save_state(run_dir, state)
                lang = self._load_language_setting()
                policy = self._format_language_policy(lang)
                system_template = self._load_ralph_template("rules/ralph_system.md")
                subagent_system = self._render_ralph_template(
                    system_template,
                    language=lang,
                    language_policy=policy,
                )
                project_dir = Path(state.get("project_dir") or str(run_dir)).expanduser()
                task_prompt = str(state.get("prompt") or "")
                session_workspace = self._ralph_subagent_workspace(run_dir, project_dir, task_prompt=task_prompt)
                self._ralph_sync_run_dir_outputs_to_project_dir(run_dir, project_dir)
                progress_before = progress_path.read_text(encoding="utf-8") if progress_path.exists() else ""
                prompt_cancel_reason = ""
                artifact_paths = self._ralph_expected_artifact_paths(story if isinstance(story, dict) else {}, project_dir)
                artifact_snapshot = self._ralph_snapshot_artifacts(artifact_paths)
                activity_paths = [project_dir, prd_path, progress_path]
                activity_snapshot = self._ralph_snapshot_artifacts(activity_paths)
                try:
                    artifact_anchor_mtime = prd_path.stat().st_mtime
                except Exception:
                    artifact_anchor_mtime = None
                artifact_watch_started: float | None = None
                observed_artifacts: list[Path] = []
                auto_finalized = False
                poll_interval = float(getattr(self, "_ralph_prompt_poll_seconds", 2.0))
                artifact_watchdog = float(getattr(self, "_ralph_artifact_watchdog_seconds", 8.0))
                current_story = story if isinstance(story, dict) else {}
                idle_watchdog = self._ralph_idle_watchdog_seconds(
                    current_story,
                    float(getattr(self, "_ralph_story_idle_watchdog_seconds", 60.0)),
                )
                settle_timeout = float(getattr(self, "_ralph_story_settle_timeout_seconds", 3.0))
                last_activity = asyncio.get_running_loop().time()
                activity_clock = {"last": last_activity}
                resume_anchor_mtime = None
                started_at = state.get("current_started_at")
                if started_at is not None:
                    with contextlib.suppress(TypeError, ValueError):
                        resume_anchor_mtime = float(started_at)
                existing_artifacts = self._ralph_changed_artifacts(
                    artifact_paths,
                    {},
                    anchor_mtime=resume_anchor_mtime,
                )
                if not existing_artifacts and resumed_same_story_pass:
                    existing_artifacts = self._ralph_materialized_artifacts(artifact_paths)
                if not existing_artifacts and resumed_same_story_pass:
                    existing_artifacts = self._ralph_materialized_artifacts([project_dir])
                if existing_artifacts:
                    should_autofinalize = False
                    if self._ralph_pick_role(current_story) in {"researcher", "writer"}:
                        should_autofinalize = True
                    else:
                        verification_passed = await self._ralph_verification_passed(
                            project_dir=project_dir,
                            story=current_story,
                        )
                        should_autofinalize = self._ralph_can_supervisor_autofinalize(
                            current_story,
                            existing_artifacts,
                            verification_passed,
                        )
                    if should_autofinalize and self._ralph_autofinalize_story_from_artifacts(
                        prd_path=prd_path,
                        progress_path=progress_path,
                        story_index=story_index,
                        artifact_paths=existing_artifacts,
                    ):
                        auto_finalized = True
                if not auto_finalized:
                    session_id = await stdio._client.create_session(
                        workspace=session_workspace,
                        model=self.model,
                        system_prompt=subagent_system,
                        approval_mode="yolo",
                    )
                    self._ralph_active_sessions[chat_id] = session_id
                    prompt = self._ralph_build_subagent_prompt(
                        run_dir=run_dir,
                        project_dir=project_dir,
                        story=story,
                        story_index=story_index + 1,
                        story_total=len(stories),
                        pass_index=pass_index + 1,
                        passes=passes,
                        progress_before=progress_before,
                        task_prompt=task_prompt,
                    )

                    async def mark_prompt_activity(*_args, **_kwargs):
                        activity_clock["last"] = asyncio.get_running_loop().time()

                    prompt_kwargs = {
                        "message": prompt,
                        "timeout": self.adapter.timeout,
                    }
                    if self._ralph_prompt_supports_callbacks(stdio._client.prompt):
                        prompt_kwargs.update(
                            {
                                "on_chunk": mark_prompt_activity,
                                "on_tool_call": mark_prompt_activity,
                                "on_event": mark_prompt_activity,
                            }
                        )
                    prompt_task = asyncio.create_task(
                        stdio._client.prompt(session_id, **prompt_kwargs)
                    )
                    while not prompt_task.done():
                        await asyncio.sleep(poll_interval)
                        if state.get("status") in {"stopped", "failed"}:
                            return
                        now = asyncio.get_running_loop().time()
                        last_activity = max(last_activity, activity_clock["last"])
                        synced_outputs = self._ralph_sync_run_dir_outputs_to_project_dir(run_dir, project_dir)
                        if synced_outputs:
                            last_activity = now
                            activity_snapshot = self._ralph_snapshot_artifacts(activity_paths)
                        activity_changed = self._ralph_changed_artifacts(
                            activity_paths,
                            activity_snapshot,
                            anchor_mtime=artifact_anchor_mtime,
                        )
                        if activity_changed:
                            last_activity = now
                            activity_snapshot = self._ralph_snapshot_artifacts(activity_paths)
                        changed_artifacts = self._ralph_changed_artifacts(
                            artifact_paths,
                            artifact_snapshot,
                            anchor_mtime=artifact_anchor_mtime,
                        )
                        if changed_artifacts:
                            if artifact_watch_started is None:
                                artifact_watch_started = now
                            for path in changed_artifacts:
                                if not any(str(path) == str(item) for item in observed_artifacts):
                                    observed_artifacts.append(path)
                        if artifact_watch_started is not None and now - artifact_watch_started >= artifact_watchdog:
                            candidate_artifacts = observed_artifacts or self._ralph_materialized_artifacts(artifact_paths)
                            should_autofinalize = False
                            if self._ralph_pick_role(current_story) in {"researcher", "writer"}:
                                should_autofinalize = bool(candidate_artifacts)
                            else:
                                verification_passed = await self._ralph_verification_passed(
                                    project_dir=project_dir,
                                    story=current_story,
                                )
                                should_autofinalize = self._ralph_can_supervisor_autofinalize(
                                    current_story,
                                    candidate_artifacts,
                                    verification_passed,
                                )
                            if should_autofinalize and self._ralph_autofinalize_story_from_artifacts(
                                prd_path=prd_path,
                                progress_path=progress_path,
                                story_index=story_index,
                                artifact_paths=candidate_artifacts,
                            ):
                                auto_finalized = True
                                try:
                                    await stdio._client.cancel(session_id)
                                except Exception:
                                    pass
                                prompt_task.cancel()
                                with contextlib.suppress(asyncio.CancelledError):
                                    await prompt_task
                                break
                            artifact_watch_started = now
                            observed_artifacts = candidate_artifacts
                        artifact_snapshot = self._ralph_snapshot_artifacts(artifact_paths)
                        if not auto_finalized and idle_watchdog > 0 and now - last_activity >= idle_watchdog:
                            prompt_cancel_reason = f"Prompt idle watchdog exceeded ({int(idle_watchdog)}s)"
                            try:
                                await stdio._client.cancel(session_id)
                            except Exception:
                                pass
                            prompt_task.cancel()
                            with contextlib.suppress(asyncio.CancelledError):
                                await prompt_task
                            break
                if auto_finalized:
                    response = ACPResponse(content="", error=None)
                else:
                    try:
                        response = await prompt_task
                    except asyncio.CancelledError:
                        response = ACPResponse(content="", error=prompt_cancel_reason or "Prompt cancelled")
                    except Exception as exc:
                        response = ACPResponse(content="", error=str(exc))
                self._ralph_sync_run_dir_outputs_to_project_dir(run_dir, project_dir)

                state["round"] = int(state.get("round", 0)) + 1
                state["updated_at"] = asyncio.get_running_loop().time()
                self._ralph_save_state(run_dir, state)

                progress_after = progress_path.read_text(encoding="utf-8") if progress_path.exists() else ""
                delta = progress_after[len(progress_before):] if progress_after.startswith(progress_before) else progress_after
                delta = delta.strip()
                fallback_used = False
                if not delta:
                    response_text = ""
                    try:
                        response_text = (response.content or "").strip()
                    except Exception:
                        response_text = ""
                    if response_text:
                        ts = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime())
                        fallback_entry = (
                            f"## {ts} - Story {story_index + 1}/{len(stories)}\n"
                            f"- Auto-captured output (progress.txt was not updated):\n"
                            f"{response_text}\n"
                            "---\n"
                        )
                        try:
                            with open(progress_path, "a", encoding="utf-8") as f:
                                f.write("\n" + fallback_entry)
                            progress_after = progress_path.read_text(encoding="utf-8") if progress_path.exists() else ""
                            delta = fallback_entry.strip()
                            fallback_used = True
                        except Exception:
                            delta = response_text
                if delta and not fallback_used:
                    cleaned = self._ralph_strip_markers(delta)
                    if cleaned:
                        await self._ralph_send_update(channel, chat_id, cleaned, force_streaming=True)
                        state["last_progress"] = cleaned
                        self._ralph_save_state(run_dir, state)

                try:
                    prd_after = json.loads(prd_path.read_text(encoding="utf-8"))
                except Exception:
                    prd_after = prd
                stories_after = prd_after.get("stories") or prd_after.get("userStories") or []
                current_story_after = stories_after[story_index] if story_index < len(stories_after) else story
                if not self._ralph_story_completed(current_story_after):
                    prd_after, progress_after = await self._ralph_wait_for_story_settle(
                        prd_path=prd_path,
                        progress_path=progress_path,
                        story_index=story_index,
                        initial_progress=progress_after,
                        artifact_paths=artifact_paths,
                        settle_timeout=settle_timeout,
                    )
                    delta_after_settle = (
                        progress_after[len(progress_before):]
                        if progress_after.startswith(progress_before)
                        else progress_after
                    )
                    delta_after_settle = delta_after_settle.strip()
                    if delta_after_settle:
                        cleaned = self._ralph_strip_markers(delta_after_settle)
                        if cleaned and cleaned != state.get("last_progress", ""):
                            await self._ralph_send_update(channel, chat_id, cleaned, force_streaming=True)
                            state["last_progress"] = cleaned
                            self._ralph_save_state(run_dir, state)
                    stories_after = prd_after.get("stories") or prd_after.get("userStories") or []
                    current_story_after = stories_after[story_index] if story_index < len(stories_after) else story
                recovery_round = 0
                max_recovery_rounds = 3
                while not self._ralph_story_completed(current_story_after) and recovery_round < max_recovery_rounds:
                    latest_output = self._ralph_strip_markers(delta)
                    if not latest_output:
                        latest_output = self._ralph_strip_markers(progress_after)
                    prep_evidence = await self._ralph_prepare_typecheck_environment(
                        project_dir=project_dir,
                        story=current_story_after if isinstance(current_story_after, dict) else story,
                    )
                    verification_passed = await self._ralph_verification_passed(
                        project_dir=project_dir,
                        story=current_story_after if isinstance(current_story_after, dict) else story,
                    )
                    changed_artifacts = self._ralph_changed_artifacts(
                        artifact_paths,
                        artifact_snapshot,
                        anchor_mtime=artifact_anchor_mtime,
                    )
                    autofinalize_artifacts = changed_artifacts
                    if not autofinalize_artifacts and resumed_same_story_pass:
                        autofinalize_artifacts = self._ralph_materialized_artifacts(artifact_paths)
                    if not autofinalize_artifacts and resumed_same_story_pass:
                        autofinalize_artifacts = self._ralph_materialized_artifacts([project_dir])
                    if self._ralph_can_supervisor_autofinalize(
                        current_story_after if isinstance(current_story_after, dict) else story,
                        autofinalize_artifacts,
                        verification_passed,
                    ):
                        if self._ralph_autofinalize_story_from_artifacts(
                            prd_path=prd_path,
                            progress_path=progress_path,
                            story_index=story_index,
                            artifact_paths=autofinalize_artifacts,
                        ):
                            try:
                                prd_after = json.loads(prd_path.read_text(encoding="utf-8"))
                            except Exception:
                                prd_after = {}
                            progress_after = progress_path.read_text(encoding="utf-8") if progress_path.exists() else progress_after
                            stories_after = prd_after.get("stories") or prd_after.get("userStories") or []
                            current_story_after = stories_after[story_index] if story_index < len(stories_after) else story
                            break
                    verification_evidence = ""
                    if not self._ralph_story_completed(current_story_after):
                        verification_evidence = await self._ralph_collect_verification_evidence(
                            project_dir=project_dir,
                            story=current_story_after if isinstance(current_story_after, dict) else story,
                        )
                    evidence_parts = [part for part in [prep_evidence, verification_evidence] if part]
                    verification_evidence = "\n\n".join(evidence_parts)
                    if verification_evidence:
                        evidence_block = f"Supervisor verification evidence:\n{verification_evidence}"
                        latest_output = (
                            f"{latest_output}\n\n{evidence_block}".strip()
                            if latest_output
                            else evidence_block
                        )
                    failure_reason = ""
                    try:
                        failure_reason = str(response.error or "").strip()
                    except Exception:
                        failure_reason = ""
                    if failure_reason:
                        failure_reason = f"Previous attempt ended with error: {failure_reason}"
                    retry_response = await self._ralph_retry_incomplete_story(
                        stdio=stdio,
                        run_dir=run_dir,
                        project_dir=project_dir,
                        story=current_story_after if isinstance(current_story_after, dict) else story,
                        chat_id=chat_id,
                        latest_output=latest_output,
                        failure_reason=failure_reason,
                        task_prompt=task_prompt,
                        prd_path=prd_path,
                        progress_path=progress_path,
                        story_index=story_index,
                    )
                    retry_content = ""
                    if retry_response is not None:
                        try:
                            retry_content = (retry_response.content or "").strip()
                        except Exception:
                            retry_content = ""
                        try:
                            response = retry_response
                        except Exception:
                            pass
                    prd_after, progress_after = await self._ralph_wait_for_story_settle(
                        prd_path=prd_path,
                        progress_path=progress_path,
                        story_index=story_index,
                        initial_progress=progress_after,
                        artifact_paths=artifact_paths,
                        settle_timeout=settle_timeout,
                    )
                    delta_after_retry = (
                        progress_after[len(progress_before):]
                        if progress_after.startswith(progress_before)
                        else progress_after
                    )
                    delta_after_retry = delta_after_retry.strip()
                    if delta_after_retry:
                        cleaned = self._ralph_strip_markers(delta_after_retry)
                        if cleaned and cleaned != state.get("last_progress", ""):
                            await self._ralph_send_update(channel, chat_id, cleaned, force_streaming=True)
                            state["last_progress"] = cleaned
                            self._ralph_save_state(run_dir, state)
                    elif retry_content:
                        cleaned = self._ralph_strip_markers(retry_content)
                        if cleaned and cleaned != state.get("last_progress", ""):
                            await self._ralph_send_update(channel, chat_id, cleaned, force_streaming=True)
                            state["last_progress"] = cleaned
                            self._ralph_save_state(run_dir, state)
                    stories_after = prd_after.get("stories") or prd_after.get("userStories") or []
                    current_story_after = stories_after[story_index] if story_index < len(stories_after) else story
                    recovery_round += 1
                if not self._ralph_story_completed(current_story_after):
                    state["status"] = "stopped"
                    state["updated_at"] = asyncio.get_running_loop().time()
                    self._ralph_save_state(run_dir, state)
                    self._ralph_active_sessions.pop(chat_id, None)
                    self._ralph_tasks.pop(chat_id, None)
                    await self._ralph_send_update(channel, chat_id, self._msg("ralph_story_incomplete_paused"))
                    return

                state["story_index"] = story_index
                state["pass_index"] = pass_index + 1
                state["updated_at"] = asyncio.get_running_loop().time()
                self._ralph_save_state(run_dir, state)

                title = ""
                if isinstance(story, dict):
                    title = str(story.get("title") or story.get("name") or "").strip()
                pass_msg = self._msg(
                    "ralph_story_pass_completed",
                    story=story_index + 1,
                    total=len(stories),
                    pass_index=pass_index + 1,
                    passes=passes,
                )
                if title:
                    pass_msg += f": {title}" if self._is_english(self._load_language_setting()) else f"：{title}"
                await self._ralph_send_update(channel, chat_id, pass_msg)

                if "[RALPH_DONE]" in progress_after:
                    state["status"] = "done"
                    state["updated_at"] = asyncio.get_running_loop().time()
                    self._ralph_save_state(run_dir, state)
                    self._ralph_active_sessions.pop(chat_id, None)
                    self._ralph_tasks.pop(chat_id, None)
                    final_summary = self._ralph_pick_final_summary(
                        progress_after,
                        state.get("last_progress", ""),
                        progress_path,
                        state.get("project_dir"),
                    )
                    if final_summary:
                        await self._ralph_send_update(
                            channel,
                            chat_id,
                            self._msg("ralph_final_summary_title", summary=final_summary),
                            force_streaming=True,
                        )
                    await self._ralph_send_update(channel, chat_id, self._msg("ralph_task_done"))
                    return

            state["pass_index"] = 0
            state["story_index"] = story_index + 1
            self._ralph_save_state(run_dir, state)
        # All stories finished, enforce done marker
        progress_after = progress_path.read_text(encoding="utf-8") if progress_path.exists() else ""
        if "[RALPH_DONE]" not in progress_after:
            with open(progress_path, "a", encoding="utf-8") as f:
                f.write("\n[RALPH_DONE]\n")
            progress_after = progress_path.read_text(encoding="utf-8") if progress_path.exists() else ""

        final_summary = self._ralph_pick_final_summary(
            progress_after,
            state.get("last_progress", ""),
            progress_path,
            state.get("project_dir"),
        )
        if final_summary:
            await self._ralph_send_update(
                channel,
                chat_id,
                self._msg("ralph_final_summary_title", summary=final_summary),
                force_streaming=True,
            )

        state["status"] = "done"
        state["updated_at"] = asyncio.get_running_loop().time()
        self._ralph_save_state(run_dir, state)
        self._ralph_active_sessions.pop(chat_id, None)
        self._ralph_tasks.pop(chat_id, None)
        await self._ralph_send_update(channel, chat_id, self._msg("ralph_task_done"))

    async def run(self) -> None:
        """启动主循环。"""
        self._running = True
        logger.info("AgentLoop started, listening for inbound messages...")

        while self._running:
            try:
                msg = await self.bus.consume_inbound()
                logger.debug(
                    "AgentLoop consumed inbound: channel={} chat_id={} content={}",
                    msg.channel,
                    msg.chat_id,
                    (msg.content or "")[:200],
                )
                # 异步处理消息
                task = asyncio.create_task(self._process_message(msg))
                task.add_done_callback(self._on_process_message_done)
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Error in main loop: {e}")
                await asyncio.sleep(0.1)

    def _on_process_message_done(self, task: asyncio.Task) -> None:
        if task.cancelled():
            return
        try:
            task.result()
        except Exception:
            logger.exception("Unhandled exception in _process_message task")

    def _get_user_lock(self, channel: str, chat_id: str) -> asyncio.Lock:
        """获取指定用户的处理锁（P3：防止同一用户并发处理）。"""
        key = f"{channel}:{chat_id}"
        if key not in self._user_locks:
            self._user_locks[key] = asyncio.Lock()
        return self._user_locks[key]

    def _peek_command(self, content: str) -> tuple[str, list[str]]:
        import shlex

        raw = (content or "").strip()
        if not raw.startswith("/") and raw.lstrip().startswith("@") and "/" in raw:
            raw = raw[raw.find("/"):]
        if not raw.startswith("/"):
            return "", []
        try:
            parts = shlex.split(raw)
        except Exception:
            parts = raw.split()
        if not parts:
            return "", []
        cmd = parts[0].lower()
        if "@" in cmd:
            cmd = cmd.split("@", 1)[0]
        return cmd, parts[1:]

    def _has_active_ralph(self, chat_id: str) -> bool:
        task = self._ralph_tasks.get(chat_id)
        return bool(task and not task.done())

    def _ralph_current_status_text(self, chat_id: str) -> str:
        run_id = self._ralph_get_current(chat_id)
        if not run_id:
            return self._msg("ralph_no_task")
        run_dir = self._ralph_run_dir(chat_id, run_id)
        state = self._ralph_get_effective_state(chat_id, run_dir)
        raw_status = state.get("status", "unknown")
        status = self._format_ralph_status(raw_status)
        parts = [
            f"Ralph status: {status}" if self._is_english(self._load_language_setting()) else f"Ralph 状态: {status}",
            f"run_id: {run_id}",
        ]
        current_story = state.get("current_story_index")
        current_total = state.get("current_story_total")
        current_pass = state.get("current_pass_index")
        current_pass_total = state.get("current_pass_total")
        current_title = state.get("current_story_title")
        current_role = state.get("current_story_role")
        started_at = state.get("current_started_at")
        if raw_status == "running" and current_story and current_total and current_pass and current_pass_total:
            elapsed = ""
            if started_at:
                try:
                    elapsed_seconds = int(time.time() - float(started_at))
                    elapsed = f" ({elapsed_seconds}s)" if self._is_english(self._load_language_setting()) else f"（{elapsed_seconds}s）"
                except Exception:
                    elapsed = ""
            line = (
                f"Current: Story {current_story}/{current_total} Pass {current_pass}/{current_pass_total}{elapsed}"
                if self._is_english(self._load_language_setting())
                else f"当前: 第 {current_story}/{current_total} 个任务，第 {current_pass}/{current_pass_total} 轮{elapsed}"
            )
            if current_title:
                line += f": {current_title}" if self._is_english(self._load_language_setting()) else f"：{current_title}"
            parts.append(line)
        if raw_status == "running" and current_role:
            formatted_role = self._format_ralph_role(str(current_role))
            parts.append(
                f"Subagent role: {formatted_role}"
                if self._is_english(self._load_language_setting())
                else f"子角色: {formatted_role}"
            )
        return "\n".join(parts)

    def _format_ralph_status(self, status: str) -> str:
        normalized = (status or "unknown").strip().lower()
        if self._is_english(self._load_language_setting()):
            mapping = {
                "generating_prd": "generating_prd",
                "generating_prd_json": "generating_prd_json",
            }
            return mapping.get(normalized, normalized or "unknown")
        mapping = {
            "awaiting_answers": "等待回答",
            "generating_prd": "生成PRD中",
            "generating_prd_json": "生成PRD JSON中",
            "needs_approval": "待审批",
            "approved": "已批准",
            "running": "运行中",
            "stopped": "已停止",
            "failed": "失败",
            "done": "已完成",
            "archived": "已归档",
            "unknown": "未知",
        }
        return mapping.get(normalized, status or "未知")

    def _format_ralph_role(self, role: str) -> str:
        normalized = (role or "").strip().lower()
        if self._is_english(self._load_language_setting()):
            mapping = {
                "researcher": "Researcher",
                "engineer": "Engineer",
                "qa": "QA",
                "writer": "Writer",
            }
            return mapping.get(normalized, normalized or "Unknown")
        mapping = {
            "researcher": "调研",
            "engineer": "工程",
            "qa": "测试",
            "writer": "文档",
        }
        return mapping.get(normalized, normalized or "未知")

    async def _try_fast_path(self, msg: InboundMessage) -> bool:
        cmd, args = self._peek_command(msg.content)
        active_ralph = self._has_active_ralph(msg.chat_id)
        if cmd:
            logger.debug(
                "Fast path check: channel={} chat_id={} cmd={} args={} active_ralph={}",
                msg.channel,
                msg.chat_id,
                cmd,
                args,
                active_ralph,
            )

        if cmd in {"/status", "/help", "/language"}:
            if cmd == "/status":
                adapter = self.adapter
                mode = getattr(adapter, "mode", "cli")
                model = self.model
                workspace = str(self.workspace) if self.workspace else "unknown"
                language = self._load_language_setting()
                streaming = self._msg("status_on", _lang=language) if self.streaming else self._msg("status_off", _lang=language)
                compression_count = "-"
                session_id = "-"
                est_tokens = "-"
                try:
                    if mode == "stdio":
                        stdio = getattr(adapter, "_stdio_adapter", None)
                        if stdio is not None:
                            info = stdio.get_session_status(msg.channel, msg.chat_id)
                            compression_count = str(info.get("compression_count", "-"))
                            session_id = info.get("session_id", "-")
                            est_tokens = info.get("estimated_tokens", "-")
                except Exception:
                    pass
                lines = [
                    self._msg("status_mode", value=mode, _lang=language),
                    self._msg("status_model", value=model, _lang=language),
                    self._msg("status_streaming", value=streaming, _lang=language),
                    self._msg("status_language", value=language, _lang=language),
                    self._msg("status_workspace", value=workspace, _lang=language),
                    self._msg("status_session", value=session_id, _lang=language),
                    self._msg("status_est_tokens", value=est_tokens, _lang=language),
                    self._msg("status_compaction", value=compression_count, _lang=language),
                ]
                await self._send_command_reply(msg, "\n".join(lines))
                return True
            if cmd == "/language" and args:
                lang = args[0]
                settings_path = self.workspace / ".iflow" / "settings.json"
                try:
                    if settings_path.exists():
                        import json
                        data = json.loads(settings_path.read_text(encoding="utf-8"))
                    else:
                        data = {}
                    data["language"] = lang
                    settings_path.parent.mkdir(parents=True, exist_ok=True)
                    settings_path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
                    await self._send_command_reply(msg, self._msg("language_set_ok", lang=lang))
                except Exception as e:
                    await self._send_command_reply(msg, self._msg("language_set_failed", error=e))
                return True
            await self._send_command_reply(msg, self._build_help_text())
            return True

        if cmd == "/ralph" and args and args[0].lower() == "status":
            await self._send_command_reply(msg, self._ralph_current_status_text(msg.chat_id))
            return True

        if cmd == "/ralph" and args and args[0].lower() not in {"status", "stop", "resume", "approve", "answer"} and active_ralph:
            await self._send_command_reply(msg, self._msg("ralph_running_stop"))
            return True

        return False

    async def _process_message(self, msg: InboundMessage) -> None:
        """处理单条消息（每用户串行）。"""
        try:
            if await self._try_fast_path(msg):
                return
            lock = self._get_user_lock(msg.channel, msg.chat_id)
            async with lock:
                logger.info(f"Processing: {msg.channel}:{msg.chat_id}")
                logger.info(
                    "Inbound detail: channel={} chat_id={} sender={} msg_type={}",
                    msg.channel,
                    msg.chat_id,
                    msg.sender_id,
                    msg.metadata.get("msg_type", ""),
                )

                # 检查是否是新会话请求（如 /new 命令）
                if msg.content.strip().lower() in ["/new", "/start"]:
                    cleared = False
                    try:
                        if getattr(self.adapter, "mode", "cli") in {"stdio", "acp"}:
                            # 走底层 adapter，确保真实会话状态（session_map / loaded / rehydrate）一起清理
                            if self.adapter.mode == "stdio":
                                stdio_adapter = await self.adapter._get_stdio_adapter()
                                cleared = stdio_adapter.clear_session(msg.channel, msg.chat_id)
                            else:
                                acp_adapter = await self.adapter._get_acp_adapter()
                                cleared = acp_adapter.clear_session(msg.channel, msg.chat_id)
                        else:
                            cleared = self.adapter.session_mappings.clear_session(msg.channel, msg.chat_id)
                    except Exception as e:
                        logger.warning(f"Failed to clear session for {msg.channel}:{msg.chat_id}: {e}")

                    logger.info(
                        "New chat requested: channel=%s chat_id=%s mode=%s cleared=%s",
                        msg.channel,
                        msg.chat_id,
                        getattr(self.adapter, "mode", "unknown"),
                        cleared,
                    )
                    await self.bus.publish_outbound(OutboundMessage(
                        channel=msg.channel,
                        chat_id=msg.chat_id,
                        content=self._get_new_conversation_message(),
                    ))
                    return

                # 处理斜杠命令
                if await self._ralph_maybe_handle_answer(msg):
                    return

                if await self._handle_slash_command(msg):
                    return

                # 准备消息内容
                message_content = msg.content
                
                # 注入渠道上下文
                channel_context = self._build_channel_context(msg)
                if channel_context:
                    message_content = channel_context + "\n\n" + message_content

                # 注入媒体文件路径（用于图片/文件识别）
                if msg.media:
                    media_paths = await self._resolve_media_paths(msg.media)
                    message_content = self._append_media_prompt(message_content, media_paths)
                
                # 检查引导文件（优先 BOOTSTRAP.md，否则 AGENTS.md）
                bootstrap_content, is_bootstrap = self._get_bootstrap_content()
                
                # 如果有引导内容，注入到消息中
                if bootstrap_content:
                    message_content = self._inject_bootstrap(message_content, bootstrap_content, is_bootstrap)
                    mode = "BOOTSTRAP" if is_bootstrap else "AGENTS"
                    logger.info(f"Injected {mode} for {msg.channel}:{msg.chat_id}")

                # 主会话在支持流式的渠道上保持流式响应；Ralph 子流程本身使用独立子会话，不应拖慢主会话首响。
                supports_streaming = self.streaming and msg.channel in STREAMING_CHANNELS and msg.channel != "qq"
                
                if supports_streaming:
                    # 流式模式
                    response = await self._process_with_streaming(msg, message_content)
                else:
                    # 非流式模式
                    response = await self.adapter.chat(
                        message=message_content,
                        channel=msg.channel,
                        chat_id=msg.chat_id,
                        model=self.model,
                    )

                # 发送最终响应（如果有内容且不是流式模式）
                if response and not supports_streaming:
                    # 🆕 使用 ResultAnalyzer 分析响应并提取文件
                    outbound = self._analyze_and_build_outbound(
                        response=response,
                        channel=msg.channel,
                        chat_id=msg.chat_id,
                        metadata=self._build_reply_metadata(msg),
                    )
                    await self.bus.publish_outbound(outbound)
                    logger.info(f"Response sent to {msg.channel}:{msg.chat_id}")

        except Exception as e:
            logger.exception(f"Error processing message for {msg.channel}:{msg.chat_id}")  # B6
            await self.bus.publish_outbound(OutboundMessage(
                channel=msg.channel,
                chat_id=msg.chat_id,
                content=self._msg("process_error", error=e),
            ))

    async def _process_with_streaming(
        self,
        msg: InboundMessage,
        message_content: str,
    ) -> str:
        """
        流式处理消息并发送实时更新到渠道。
        
        使用内容缓冲机制，每满 N 个字符推送一次（随机范围）。
        
        Args:
            msg: 入站消息
            message_content: 准备好的消息内容
        
        Returns:
            最终响应文本
        """
        session_key = f"{msg.channel}:{msg.chat_id}"
        
        # 初始化缓冲区
        self._stream_buffers[session_key] = ""
        
        # 未发送的字符计数和当前阈值
        unflushed_count = 0
        current_threshold = random.randint(STREAM_BUFFER_MIN, STREAM_BUFFER_MAX)
        
        # 钉钉使用直接调用方式（AI Card）
        dingtalk_channel = None
        if msg.channel == "dingtalk" and self.channel_manager:
            dingtalk_channel = self.channel_manager.get_channel("dingtalk")
            # 立即创建 AI Card，实现秒回卡片
            if dingtalk_channel and hasattr(dingtalk_channel, 'start_streaming'):
                await dingtalk_channel.start_streaming(msg.chat_id)
            dingtalk_channel = self.channel_manager.get_channel("dingtalk")

        # QQ 使用直接调用方式（流式分段发送）
        qq_channel = None
        qq_segment_buffer = ""  # 当前正在累积的段内容
        qq_line_buffer = ""      # 还没收到 \n 的不完整行（用于正确检测 ```）
        qq_newline_count = 0
        qq_in_code_block = False  # 是否在代码块内（代码块内换行符不计入阈值）
        if msg.channel == "qq" and self.channel_manager:
            qq_channel = self.channel_manager.get_channel("qq")

        async def on_chunk(channel: str, chat_id: str, chunk_text: str):
            """处理流式消息块。"""
            nonlocal unflushed_count, current_threshold, qq_segment_buffer, qq_line_buffer, qq_newline_count, qq_in_code_block

            key = f"{channel}:{chat_id}"

            # 更新累积缓冲区（所有渠道，用于记录完整内容与日志）
            self._stream_buffers[key] = self._stream_buffers.get(key, "") + chunk_text

            # QQ 渠道：按换行符分段直接发送，不走字符缓冲逻辑
            if channel == "qq" and qq_channel:
                threshold = getattr(qq_channel.config, "split_threshold", 0)
                if threshold > 0:
                    qq_line_buffer += chunk_text
                    while "\n" in qq_line_buffer:
                        idx = qq_line_buffer.index("\n")
                        complete_line = qq_line_buffer[:idx]

                        qq_line_buffer = qq_line_buffer[idx + 1:]

                        # 检测代码块分隔符
                        if complete_line.strip().startswith("```"):
                            qq_in_code_block = not qq_in_code_block

                        # 将完整行加入当前段
                        qq_segment_buffer += complete_line + "\n"

                        # 代码块内的换行符不计入阈值
                        if not qq_in_code_block:
                            qq_newline_count += 1
                            if qq_newline_count >= threshold:
                                segment = qq_segment_buffer.strip()
                                qq_segment_buffer = ""
                                qq_newline_count = 0
                                if segment:
                                    await qq_channel.send(OutboundMessage(
                                        channel=channel,
                                        chat_id=chat_id,
                                        content=segment,
                                        metadata=self._build_reply_metadata(msg),
                                    ))
                                    from iflow_bot.session.recorder import get_recorder
                                    recorder = get_recorder()
                                    if recorder:
                                        recorder.record_outbound(OutboundMessage(
                                            channel=channel,
                                            chat_id=chat_id,
                                            content=segment,
                                            metadata=self._build_reply_metadata(msg),
                                        ))
                return  # 不走字符缓冲逻辑

            unflushed_count += len(chunk_text)

            # 当累积足够字符时发送更新
            if unflushed_count >= current_threshold:
                unflushed_count = 0
                current_threshold = random.randint(STREAM_BUFFER_MIN, STREAM_BUFFER_MAX)

                # 钉钉：直接调用渠道的流式方法
                if channel == "dingtalk" and dingtalk_channel and hasattr(dingtalk_channel, 'handle_streaming_chunk'):
                    await dingtalk_channel.handle_streaming_chunk(chat_id, self._stream_buffers[key], is_final=False)
                else:
                    # 其他渠道：通过消息总线
                    await self.bus.publish_outbound(OutboundMessage(
                        channel=channel,
                        chat_id=chat_id,
                        content=self._stream_buffers[key],
                        metadata={
                            "_progress": True,
                            "_streaming": True,
                            **self._build_reply_metadata(msg),
                        },
                    ))
        
        try:
            # 使用流式 chat
            response = await self.adapter.chat_stream(
                message=message_content,
                channel=msg.channel,
                chat_id=msg.chat_id,
                model=self.model,
                on_chunk=on_chunk,
            )
            
            # 清理缓冲区并发送最终内容
            final_content = self._stream_buffers.pop(session_key, "")
            effective_content = (final_content or response or "").strip()

            # QQ 渠道：发送遗留的buffer
            if msg.channel == "qq" and qq_channel:
                threshold = getattr(qq_channel.config, "split_threshold", 0)
                from iflow_bot.session.recorder import get_recorder
                recorder = get_recorder()
                if threshold <= 0:
                    content_to_send = final_content.strip()
                    if content_to_send:
                        await qq_channel.send(OutboundMessage(
                            channel=msg.channel,
                            chat_id=msg.chat_id,
                            content=content_to_send,
                            metadata=self._build_reply_metadata(msg),
                        ))
                        if recorder:
                            recorder.record_outbound(OutboundMessage(
                                channel=msg.channel,
                                chat_id=msg.chat_id,
                                content=content_to_send,
                                metadata=self._build_reply_metadata(msg),
                            ))
                else:
                    remainder_to_send = (qq_segment_buffer + qq_line_buffer).strip()
                    if remainder_to_send:
                        await qq_channel.send(OutboundMessage(
                            channel=msg.channel,
                            chat_id=msg.chat_id,
                            content=remainder_to_send,
                            metadata=self._build_reply_metadata(msg),
                        ))
                        if recorder:
                            recorder.record_outbound(OutboundMessage(
                                channel=msg.channel,
                                chat_id=msg.chat_id,
                                content=remainder_to_send,
                                metadata=self._build_reply_metadata(msg),
                            ))

            if not effective_content:
                logger.warning(
                    f"Streaming produced empty output for {msg.channel}:{msg.chat_id}, retrying non-stream chat"
                )
                try:
                    fallback_response = await self.adapter.chat(
                        message=message_content,
                        channel=msg.channel,
                        chat_id=msg.chat_id,
                        model=self.model,
                    )
                    effective_content = (fallback_response or "").strip()
                except Exception as e:
                    logger.warning(
                        f"Non-stream retry failed for {msg.channel}:{msg.chat_id}: {e}"
                    )

            if effective_content:
                # 🆕 流式结束后，也用 ResultAnalyzer 分析并附加检测到的文件
                analysis = result_analyzer.analyze({"output": effective_content, "success": True})
                media_files = analysis.image_files + analysis.audio_files + analysis.video_files + analysis.doc_files

                if media_files:
                    logger.info(f"Stream completed: detected {len(media_files)} file(s) for callback")

                # 钉钉：直接调用最终更新
                if msg.channel == "dingtalk" and dingtalk_channel and hasattr(dingtalk_channel, 'handle_streaming_chunk'):
                    await dingtalk_channel.handle_streaming_chunk(msg.chat_id, effective_content, is_final=True)
                    # 钉钉流式结束后，单独发送检测到的文件
                    if media_files:
                        await self.bus.publish_outbound(OutboundMessage(
                            channel=msg.channel,
                            chat_id=msg.chat_id,
                            content="",
                            media=media_files,
                        ))
                elif msg.channel != "qq":
                    # 其他渠道（非 QQ、非钉钉）：通过消息总线
                    await self.bus.publish_outbound(OutboundMessage(
                        channel=msg.channel,
                        chat_id=msg.chat_id,
                        content=effective_content,
                        media=media_files,
                        metadata={
                            "_progress": True,
                            "_streaming": True,
                            **self._build_reply_metadata(msg),
                        },
                    ))
                    # 再发送流式结束标记
                    await self.bus.publish_outbound(OutboundMessage(
                        channel=msg.channel,
                        chat_id=msg.chat_id,
                        content="",
                        metadata={
                            "_streaming_end": True,
                            **self._build_reply_metadata(msg),
                        },
                    ))
                elif qq_channel and not final_content:
                    # QQ：流式无内容时，补发非流式结果
                    await qq_channel.send(OutboundMessage(
                        channel=msg.channel,
                        chat_id=msg.chat_id,
                        content=effective_content,
                        metadata=self._build_reply_metadata(msg),
                    ))
                    from iflow_bot.session.recorder import get_recorder
                    recorder = get_recorder()
                    if recorder:
                        recorder.record_outbound(OutboundMessage(
                            channel=msg.channel,
                            chat_id=msg.chat_id,
                            content=effective_content,
                            metadata=self._build_reply_metadata(msg),
                        ))
                logger.info(f"Streaming response completed for {msg.channel}:{msg.chat_id}")
            else:
                fallback = self._msg("stream_empty_fallback")
                await self.bus.publish_outbound(OutboundMessage(
                    channel=msg.channel,
                    chat_id=msg.chat_id,
                    content=fallback,
                    metadata=self._build_reply_metadata(msg),
                ))
                logger.warning(f"Streaming produced empty output for {msg.channel}:{msg.chat_id}")
            
            return effective_content or response
            
        except Exception as e:
            # 清理缓冲区
            self._stream_buffers.pop(session_key, None)
            raise e

    async def process_direct(
        self,
        message: str,
        session_key: Optional[str] = None,
        channel: str = "cli",
        chat_id: str = "direct",
        on_progress: Optional[callable] = None,
    ) -> str:
        """直接处理消息（CLI 模式 / Cron / Heartbeat）。"""
        # 检查引导文件（优先 BOOTSTRAP.md，否则 AGENTS.md）
        bootstrap_content, is_bootstrap = self._get_bootstrap_content()
        
        message_content = message
        if bootstrap_content:
            message_content = self._inject_bootstrap(message, bootstrap_content, is_bootstrap)
            mode = "BOOTSTRAP" if is_bootstrap else "AGENTS"
            logger.info(f"Injected {mode} for {channel}:{chat_id} (direct mode)")
        
        effective_channel = channel
        effective_chat_id = chat_id
        
        if session_key:
            parts = session_key.split(":", 1)
            if len(parts) == 2:
                effective_channel = parts[0]
                effective_chat_id = parts[1]
        
        return await self.adapter.chat(
            message=message_content,
            channel=effective_channel,
            chat_id=effective_chat_id,
            model=self.model,
        )

    async def start_background(self) -> None:
        """后台启动。"""
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self.run())
            await self._ralph_auto_resume_pending_runs()
            logger.info("AgentLoop started in background")

    def stop(self) -> None:
        """停止。"""
        self._running = False
        if self._task and not self._task.done():
            self._task.cancel()
        logger.info("AgentLoop stopped")
