"""Email channel implementation using IMAP polling + SMTP replies.

使用 IMAP 轮询接收邮件，使用 SMTP 发送回复。
支持自动回复、主题前缀、邮件去重等功能。
"""

import asyncio
import html
import imaplib
import logging
import re
import smtplib
import ssl
from datetime import date
from email import policy
from email.header import decode_header, make_header
from email.message import EmailMessage
from email.parser import BytesParser
from email.utils import parseaddr
from typing import Any, List, Optional, Set

from iflow_bot.bus.events import OutboundMessage
from iflow_bot.bus.queue import MessageBus
from iflow_bot.channels.base import BaseChannel
from iflow_bot.channels.manager import register_channel
from iflow_bot.config.schema import EmailConfig


logger = logging.getLogger(__name__)


# IMAP 月份缩写 (英文)
_IMAP_MONTHS = (
    "Jan", "Feb", "Mar", "Apr", "May", "Jun",
    "Jul", "Aug", "Sep", "Oct", "Nov", "Dec",
)


@register_channel("email")
class EmailChannel(BaseChannel):
    """Email Channel - 使用 IMAP 轮询接收邮件，SMTP 发送回复。

    入站:
    - 轮询 IMAP 邮箱获取未读消息
    - 将每封邮件转换为入站事件

    出站:
    - 通过 SMTP 发送回复到发送者地址

    要求:
    - consent_granted: 必须为 True (需要用户明确授权)
    - IMAP 配置: host, port, username, password
    - SMTP 配置: host, port, username, password

    Attributes:
        name: 渠道名称 ("email")
        config: Email 配置对象
        bus: 消息总线实例
        _last_subject_by_chat: 每个聊天的最后主题
        _last_message_id_by_chat: 每个聊天的最后消息 ID
        _processed_uids: 已处理的 UID 集合
    """

    name = "email"

    # 配置默认值
    POLL_INTERVAL_SECONDS = 30
    MAX_BODY_CHARS = 10000
    MAX_PROCESSED_UIDS = 100000
    MARK_SEEN = True
    IMAP_MAILBOX = "INBOX"
    SUBJECT_PREFIX = "Re: "
    SMTP_USE_SSL = False
    SMTP_USE_TLS = True

    def __init__(self, config: EmailConfig, bus: MessageBus):
        """初始化 Email Channel。

        Args:
            config: Email 配置对象
            bus: 消息总线实例
        """
        super().__init__(config, bus)
        self.config: EmailConfig = config
        self._last_subject_by_chat: dict[str, str] = {}
        self._last_message_id_by_chat: dict[str, str] = {}
        self._processed_uids: Set[str] = set()

    async def start(self) -> None:
        """启动 IMAP 轮询。"""
        # 检查用户授权
        if not self.config.consent_granted:
            logger.warning(
                f"[{self.name}] Email channel disabled: consent_granted is false. "
                "Set channels.email.consent_granted=true after explicit user permission."
            )
            return

        if not self._validate_config():
            return

        self._running = True
        logger.info(f"[{self.name}] Starting Email channel (IMAP polling mode)...")

        # 从配置获取轮询间隔，默认 30 秒
        poll_seconds = getattr(self.config, 'poll_interval_seconds', self.POLL_INTERVAL_SECONDS)
        poll_seconds = max(5, int(poll_seconds))

        while self._running:
            try:
                inbound_items = await asyncio.to_thread(self._fetch_new_messages)
                for item in inbound_items:
                    sender = item["sender"]
                    subject = item.get("subject", "")
                    message_id = item.get("message_id", "")

                    if subject:
                        self._last_subject_by_chat[sender] = subject
                    if message_id:
                        self._last_message_id_by_chat[sender] = message_id

                    await self._handle_message(
                        sender_id=sender,
                        chat_id=sender,
                        content=item["content"],
                        metadata=item.get("metadata", {}),
                    )
            except Exception as e:
                logger.error(f"[{self.name}] Email polling error: {e}")

            await asyncio.sleep(poll_seconds)

    async def stop(self) -> None:
        """停止轮询循环。"""
        self._running = False
        logger.info(f"[{self.name}] Email channel stopped")

    async def send(self, msg: OutboundMessage) -> None:
        """通过 SMTP 发送邮件。

        Args:
            msg: 出站消息对象
                - chat_id: 收件人邮箱地址
                - content: 邮件内容
                - metadata: 可包含 subject, force_send 等
        """
        if not self.config.consent_granted:
            logger.warning(f"[{self.name}] Skip email send: consent_granted is false")
            return

        # 检查是否强制发送或允许自动回复
        force_send = bool((msg.metadata or {}).get("force_send"))
        auto_reply = getattr(self.config, 'auto_reply_enabled', True)

        if not auto_reply and not force_send:
            logger.info(f"[{self.name}] Skip automatic email reply: auto_reply_enabled is false")
            return

        if not self.config.smtp_host:
            logger.warning(f"[{self.name}] SMTP host not configured")
            return

        to_addr = msg.chat_id.strip()
        if not to_addr:
            logger.warning(f"[{self.name}] Missing recipient address")
            return

        # 构建邮件
        base_subject = self._last_subject_by_chat.get(to_addr, "iFlow Bot Reply")
        subject = self._reply_subject(base_subject)

        # 允许通过 metadata 覆盖主题
        if msg.metadata and isinstance(msg.metadata.get("subject"), str):
            override = msg.metadata["subject"].strip()
            if override:
                subject = override

        email_msg = EmailMessage()
        from_addr = self.config.from_address or self.config.smtp_username or self.config.imap_username
        email_msg["From"] = from_addr
        email_msg["To"] = to_addr
        email_msg["Subject"] = subject
        email_msg.set_content(msg.content or "")

        # 添加 In-Reply-To 和 References 头
        in_reply_to = self._last_message_id_by_chat.get(to_addr)
        if in_reply_to:
            email_msg["In-Reply-To"] = in_reply_to
            email_msg["References"] = in_reply_to

        try:
            await asyncio.to_thread(self._smtp_send, email_msg)
            logger.debug(f"[{self.name}] Email sent to {to_addr}")
        except Exception as e:
            logger.error(f"[{self.name}] Error sending email to {to_addr}: {e}")
            raise

    def _validate_config(self) -> bool:
        """验证配置完整性。"""
        missing = []
        if not self.config.imap_host:
            missing.append("imap_host")
        if not self.config.imap_username:
            missing.append("imap_username")
        if not self.config.imap_password:
            missing.append("imap_password")
        if not self.config.smtp_host:
            missing.append("smtp_host")
        if not self.config.smtp_username:
            missing.append("smtp_username")
        if not self.config.smtp_password:
            missing.append("smtp_password")

        if missing:
            logger.error(
                f"[{self.name}] Email channel not configured, missing: {', '.join(missing)}"
            )
            return False
        return True

    def _smtp_send(self, msg: EmailMessage) -> None:
        """同步发送邮件。"""
        timeout = 30
        use_ssl = getattr(self.config, 'smtp_use_ssl', self.SMTP_USE_SSL)
        use_tls = getattr(self.config, 'smtp_use_tls', self.SMTP_USE_TLS)

        if use_ssl:
            with smtplib.SMTP_SSL(
                self.config.smtp_host,
                self.config.smtp_port,
                timeout=timeout,
            ) as smtp:
                smtp.login(self.config.smtp_username, self.config.smtp_password)
                smtp.send_message(msg)
            return

        with smtplib.SMTP(
            self.config.smtp_host,
            self.config.smtp_port,
            timeout=timeout
        ) as smtp:
            if use_tls:
                smtp.starttls(context=ssl.create_default_context())
            smtp.login(self.config.smtp_username, self.config.smtp_password)
            smtp.send_message(msg)

    def _fetch_new_messages(self) -> List[dict[str, Any]]:
        """轮询 IMAP 获取未读消息。"""
        return self._fetch_messages(
            search_criteria=("UNSEEN",),
            mark_seen=getattr(self.config, 'mark_seen', self.MARK_SEEN),
            dedupe=True,
            limit=0,
        )

    def fetch_messages_between_dates(
        self,
        start_date: date,
        end_date: date,
        limit: int = 20,
    ) -> List[dict[str, Any]]:
        """获取指定日期范围内的消息 (用于历史摘要)。

        Args:
            start_date: 开始日期
            end_date: 结束日期
            limit: 最大消息数

        Returns:
            消息列表
        """
        if end_date <= start_date:
            return []

        return self._fetch_messages(
            search_criteria=(
                "SINCE",
                self._format_imap_date(start_date),
                "BEFORE",
                self._format_imap_date(end_date),
            ),
            mark_seen=False,
            dedupe=False,
            limit=max(1, int(limit)),
        )

    def _fetch_messages(
        self,
        search_criteria: tuple,
        mark_seen: bool,
        dedupe: bool,
        limit: int,
    ) -> List[dict[str, Any]]:
        """根据 IMAP 搜索条件获取消息。"""
        messages: List[dict[str, Any]] = []
        mailbox = getattr(self.config, 'imap_mailbox', self.IMAP_MAILBOX)
        use_ssl = getattr(self.config, 'imap_use_ssl', True)

        if use_ssl:
            client = imaplib.IMAP4_SSL(
                self.config.imap_host,
                self.config.imap_port
            )
        else:
            client = imaplib.IMAP4(
                self.config.imap_host,
                self.config.imap_port
            )

        try:
            client.login(self.config.imap_username, self.config.imap_password)
            status, _ = client.select(mailbox)
            if status != "OK":
                return messages

            status, data = client.search(None, *search_criteria)
            if status != "OK" or not data:
                return messages

            ids = data[0].split()
            if limit > 0 and len(ids) > limit:
                ids = ids[-limit:]

            for imap_id in ids:
                status, fetched = client.fetch(imap_id, "(BODY.PEEK[] UID)")
                if status != "OK" or not fetched:
                    continue

                raw_bytes = self._extract_message_bytes(fetched)
                if raw_bytes is None:
                    continue

                uid = self._extract_uid(fetched)
                if dedupe and uid and uid in self._processed_uids:
                    continue

                parsed = BytesParser(policy=policy.default).parsebytes(raw_bytes)
                sender = parseaddr(parsed.get("From", ""))[1].strip().lower()
                if not sender:
                    continue

                subject = self._decode_header_value(parsed.get("Subject", ""))
                date_value = parsed.get("Date", "")
                message_id = parsed.get("Message-ID", "").strip()
                body = self._extract_text_body(parsed)

                if not body:
                    body = "(empty email body)"

                max_body_chars = getattr(
                    self.config, 'max_body_chars', self.MAX_BODY_CHARS
                )
                body = body[:max_body_chars]

                content = (
                    f"Email received.\n"
                    f"From: {sender}\n"
                    f"Subject: {subject}\n"
                    f"Date: {date_value}\n\n"
                    f"{body}"
                )

                metadata = {
                    "message_id": message_id,
                    "subject": subject,
                    "date": date_value,
                    "sender_email": sender,
                    "uid": uid,
                }

                messages.append({
                    "sender": sender,
                    "subject": subject,
                    "message_id": message_id,
                    "content": content,
                    "metadata": metadata,
                })

                if dedupe and uid:
                    self._processed_uids.add(uid)
                    # 限制内存使用
                    if len(self._processed_uids) > self.MAX_PROCESSED_UIDS:
                        # 移除一半
                        self._processed_uids = set(
                            list(self._processed_uids)[len(self._processed_uids) // 2:]
                        )

                if mark_seen:
                    client.store(imap_id, "+FLAGS", "\\Seen")

        finally:
            try:
                client.logout()
            except Exception:
                pass

        return messages

    @classmethod
    def _format_imap_date(cls, value: date) -> str:
        """格式化日期为 IMAP 搜索格式 (英文月份缩写)。"""
        month = cls._IMAP_MONTHS[value.month - 1]
        return f"{value.day:02d}-{month}-{value.year}"

    @staticmethod
    def _extract_message_bytes(fetched: List[Any]) -> Optional[bytes]:
        """从 IMAP 获取结果中提取消息字节。"""
        for item in fetched:
            if isinstance(item, tuple) and len(item) >= 2:
                if isinstance(item[1], (bytes, bytearray)):
                    return bytes(item[1])
        return None

    @staticmethod
    def _extract_uid(fetched: List[Any]) -> str:
        """从 IMAP 获取结果中提取 UID。"""
        for item in fetched:
            if isinstance(item, tuple) and item:
                if isinstance(item[0], (bytes, bytearray)):
                    head = bytes(item[0]).decode("utf-8", errors="ignore")
                    m = re.search(r"UID\s+(\d+)", head)
                    if m:
                        return m.group(1)
        return ""

    @staticmethod
    def _decode_header_value(value: str) -> str:
        """解码邮件头部值。"""
        if not value:
            return ""
        try:
            return str(make_header(decode_header(value)))
        except Exception:
            return value

    @classmethod
    def _extract_text_body(cls, msg: Any) -> str:
        """提取邮件正文文本 (尽力而为)。"""
        if msg.is_multipart():
            plain_parts: List[str] = []
            html_parts: List[str] = []

            for part in msg.walk():
                if part.get_content_disposition() == "attachment":
                    continue

                content_type = part.get_content_type()
                try:
                    payload = part.get_content()
                except Exception:
                    payload_bytes = part.get_payload(decode=True) or b""
                    charset = part.get_content_charset() or "utf-8"
                    payload = payload_bytes.decode(charset, errors="replace")

                if not isinstance(payload, str):
                    continue

                if content_type == "text/plain":
                    plain_parts.append(payload)
                elif content_type == "text/html":
                    html_parts.append(payload)

            if plain_parts:
                return "\n\n".join(plain_parts).strip()
            if html_parts:
                return cls._html_to_text("\n\n".join(html_parts)).strip()
            return ""

        # 非 multipart
        try:
            payload = msg.get_content()
        except Exception:
            payload_bytes = msg.get_payload(decode=True) or b""
            charset = msg.get_content_charset() or "utf-8"
            payload = payload_bytes.decode(charset, errors="replace")

        if not isinstance(payload, str):
            return ""

        if msg.get_content_type() == "text/html":
            return cls._html_to_text(payload).strip()
        return payload.strip()

    @staticmethod
    def _html_to_text(raw_html: str) -> str:
        """将 HTML 转换为纯文本。"""
        text = re.sub(r"<\s*br\s*/?>", "\n", raw_html, flags=re.IGNORECASE)
        text = re.sub(r"<\s*/\s*p\s*>", "\n", text, flags=re.IGNORECASE)
        text = re.sub(r"<[^>]+>", "", text)
        return html.unescape(text)

    def _reply_subject(self, base_subject: str) -> str:
        """生成回复主题。"""
        subject = (base_subject or "").strip() or "iFlow Bot Reply"
        prefix = getattr(self.config, 'subject_prefix', self.SUBJECT_PREFIX)
        if subject.lower().startswith("re:"):
            return subject
        return f"{prefix}{subject}"
