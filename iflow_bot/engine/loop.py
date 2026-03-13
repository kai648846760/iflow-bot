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
import random
from pathlib import Path
from typing import TYPE_CHECKING, Optional

from loguru import logger

from iflow_bot.bus import MessageBus, InboundMessage, OutboundMessage
from iflow_bot.engine.adapter import IFlowAdapter
from iflow_bot.engine.analyzer import result_analyzer, AnalysisResult

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
        
        # P3: 每用户并发锁，确保同一用户的消息串行处理，避免会话状态混乱
        self._user_locks: dict[str, asyncio.Lock] = {}

        logger.info(f"AgentLoop initialized with model={model}, workspace={self.workspace}, streaming={streaming}")

    def _get_new_conversation_message(self) -> str:
        """获取新会话提示文案（可配置）。"""
        if self.channel_manager and getattr(self.channel_manager, "config", None):
            try:
                messages = getattr(self.channel_manager.config, "messages", None)
                if messages and getattr(messages, "new_conversation", None):
                    return str(messages.new_conversation)
            except Exception:
                pass
        return "✨ New conversation started, previous context has been cleared."

    async def _send_command_reply(self, msg: InboundMessage, content: str) -> None:
        await self.bus.publish_outbound(OutboundMessage(
            channel=msg.channel,
            chat_id=msg.chat_id,
            content=content,
            metadata=self._build_reply_metadata(msg),
        ))

    def _build_help_text(self) -> str:
        return (
            "可用命令：\n"
            "/status  查看当前状态\n"
            "/new     开启新会话\n"
            "/compact 手动压缩会话\n"
            "/model set <name>  修改模型\n"
            "/cron list | /cron add | /cron delete <id>\n"
            "/skills find|add|list|remove|update  管理 Skills（SkillHub）\n"
            "/language <en-US|zh-CN>  设置语言\n"
            "/help    查看帮助\n"
        )

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
                    return None, err or "SkillHub CLI 安装失败", False
            except Exception as e:
                return None, str(e), False

            path = _find_skillhub_binary()
            if not path:
                return None, "SkillHub CLI 安装完成但未找到可执行文件", False
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
            formatted = ["技能搜索结果（用 /skills add <slug> 安装）："]
            for idx, entry in enumerate(entries, start=1):
                title = f"（{entry['title']}）" if entry["title"] else ""
                ver = f" v{entry['version']}" if entry["version"] else ""
                formatted.append(f"[{idx}] {entry['slug']}{title}{ver}")
                if entry["desc"]:
                    formatted.append(f"    {entry['desc']}")
                formatted.append(f"    /skills add {entry['slug']}")
            return "\n".join(formatted)

        def _format_skillhub_list_output(text: str) -> str:
            if not text:
                return "暂无已安装技能"
            if "No installed skills." in text:
                return "暂无已安装技能"
            lines = [line.strip() for line in text.splitlines() if line.strip()]
            formatted = ["已安装技能："]
            for line in lines:
                formatted.append(f"- {line}")
            return "\n".join(formatted)

        def _format_skillhub_install_output(text: str, slug: str) -> str:
            m = re.search(r"Installed:\\s*([^\\s]+)\\s*->\\s*(.+)$", text, re.M)
            if m:
                installed = m.group(1).strip()
                path = m.group(2).strip()
                return f"✅ 已安装 {installed}\n路径: {path}"
            if text:
                return text
            return f"✅ 已安装 {slug}"

        raw = (msg.content or "").strip()
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
            streaming = "on" if self.streaming else "off"
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

            await self._send_command_reply(
                msg,
                "\n".join([
                    f"状态: {mode}",
                    f"模型: {model}",
                    f"流式: {streaming}",
                    f"workspace: {workspace}",
                    f"session: {session_id}",
                    f"上下文估算(tokens): {est_tokens}",
                    f"压缩次数: {compression_count}",
                ]),
            )
            return True

        if cmd == "/compact":
            try:
                if getattr(self.adapter, "mode", "cli") == "stdio":
                    stdio = await self.adapter._get_stdio_adapter()
                    ok, reason = await stdio.compact_session(msg.channel, msg.chat_id)
                    if ok:
                        await self._send_command_reply(msg, "✅ 已触发会话压缩")
                    else:
                        await self._send_command_reply(msg, f"⚠️ 无法压缩：{reason}")
                else:
                    await self._send_command_reply(msg, "⚠️ 当前模式不支持手动压缩")
            except Exception as e:
                await self._send_command_reply(msg, f"❌ 压缩失败: {e}")
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
            await self._send_command_reply(msg, f"✅ 已切换模型为 {new_model}（新会话生效）")
            return True

        if cmd == "/cron":
            from iflow_bot.utils.helpers import get_data_dir
            store_path = get_data_dir() / "cron" / "jobs.json"
            cron = CronService(store_path)
            sub = (args[0].lower() if args else "help")

            if sub == "list":
                jobs = cron.list_jobs(include_disabled=True)
                if not jobs:
                    await self._send_command_reply(msg, "暂无定时任务")
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
                    await self._send_command_reply(msg, "定时任务：\n" + "\n".join(lines))
                return True

            if sub == "delete" and len(args) >= 2:
                job_id = args[1]
                removed = cron.remove_job(job_id)
                await self._send_command_reply(msg, "✅ 已删除" if removed else "⚠️ 未找到该任务")
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
                    await self._send_command_reply(msg, "⚠️ 缺少 --message")
                    return True
                tz = opts.get("tz")
                every = opts.get("every")
                cron_expr = opts.get("cron")
                at = opts.get("at")
                if sum(1 for x in [every, cron_expr, at] if x) != 1:
                    await self._send_command_reply(msg, "⚠️ 需要指定 --every 或 --cron 或 --at 其中之一")
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
                await self._send_command_reply(msg, f"✅ 已添加任务 {job.id}")
                return True

            await self._send_command_reply(
                msg,
                "用法：/cron list | /cron add --name xxx --message xxx --every 60 | /cron delete <id>",
            )
            return True

        if cmd == "/skills":
            try:
                if not args:
                    await self._send_command_reply(
                        msg,
                        "用法：/skills find <关键词> | /skills add <slug> | /skills list | /skills remove <slug> | /skills update",
                    )
                    return True

                skillhub, install_err, auto_installed = await _ensure_skillhub_cli()
                if not skillhub:
                    await self._send_command_reply(
                        msg,
                        "❌ 自动安装 SkillHub CLI 失败："
                        f"{install_err or 'unknown error'}\n"
                        "请手动执行：\n"
                        "curl -fsSL https://skillhub-1388575217.cos.ap-guangzhou.myqcloud.com/install/install.sh | bash -s -- --cli-only",
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
                        await self._send_command_reply(msg, "⚠️ 缺少技能 slug：/skills remove <slug>")
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
                        await self._send_command_reply(msg, f"❌ 删除失败: {e}")
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
                    await self._send_command_reply(msg, "✅ 已卸载" if removed else "⚠️ 未找到该技能")
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
                text = _strip_ansi(output or err or "无输出")

                if subcmd == "search":
                    text = _format_skillhub_search_output(text)
                elif subcmd == "list":
                    text = _format_skillhub_list_output(text)
                elif subcmd == "install":
                    slug = passthrough[0] if passthrough else ""
                    text = _format_skillhub_install_output(text, slug)

                if auto_installed:
                    text = f"✅ SkillHub CLI 已自动安装\n{text}"

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
                await self._send_command_reply(msg, f"❌ skills 执行失败: {e}")
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
                await self._send_command_reply(msg, f"✅ 已设置 language = {lang}")
            except Exception as e:
                await self._send_command_reply(msg, f"❌ 设置语言失败: {e}")
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
        
        return context

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

    async def run(self) -> None:
        """启动主循环。"""
        self._running = True
        logger.info("AgentLoop started, listening for inbound messages...")

        while self._running:
            try:
                msg = await self.bus.consume_inbound()
                # 异步处理消息
                asyncio.create_task(self._process_message(msg))
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Error in main loop: {e}")
                await asyncio.sleep(0.1)

    def _get_user_lock(self, channel: str, chat_id: str) -> asyncio.Lock:
        """获取指定用户的处理锁（P3：防止同一用户并发处理）。"""
        key = f"{channel}:{chat_id}"
        if key not in self._user_locks:
            self._user_locks[key] = asyncio.Lock()
        return self._user_locks[key]

    async def _process_message(self, msg: InboundMessage) -> None:
        """处理单条消息（每用户串行）。"""
        lock = self._get_user_lock(msg.channel, msg.chat_id)
        async with lock:
            try:
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
                if await self._handle_slash_command(msg):
                    return

                # 准备消息内容
                message_content = msg.content
                
                # 注入渠道上下文
                channel_context = self._build_channel_context(msg)
                if channel_context:
                    message_content = channel_context + "\n\n" + message_content
                
                # 检查引导文件（优先 BOOTSTRAP.md，否则 AGENTS.md）
                bootstrap_content, is_bootstrap = self._get_bootstrap_content()
                
                # 如果有引导内容，注入到消息中
                if bootstrap_content:
                    message_content = self._inject_bootstrap(message_content, bootstrap_content, is_bootstrap)
                    mode = "BOOTSTRAP" if is_bootstrap else "AGENTS"
                    logger.info(f"Injected {mode} for {msg.channel}:{msg.chat_id}")

                # 检查是否支持流式输出
                supports_streaming = self.streaming and msg.channel in STREAMING_CHANNELS
                
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
                    content=f"❌ 处理消息时出错: {e}",
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
                fallback = (
                    "⚠️ 本轮未产出可见文本（可能会话上下文过长）。"
                    "我已自动尝试恢复，如仍失败请发送 /new 开启新会话。"
                )
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
            logger.info("AgentLoop started in background")

    def stop(self) -> None:
        """停止。"""
        self._running = False
        if self._task and not self._task.done():
            self._task.cancel()
        logger.info("AgentLoop stopped")
