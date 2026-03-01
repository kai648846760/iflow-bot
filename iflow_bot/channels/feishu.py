"""Feishu/Lark channel implementation using lark-oapi SDK with WebSocket long connection.

使用 lark-oapi SDK 实现飞书 Channel，通过 WebSocket 长连接接收消息。
无需公网 IP 或 webhook 配置。
"""

import asyncio
import json
import logging
import os
import re
import threading
from collections import OrderedDict
from pathlib import Path
from typing import Any, Optional

from iflow_bot.bus.events import OutboundMessage
from iflow_bot.bus.queue import MessageBus
from iflow_bot.channels.base import BaseChannel
from iflow_bot.channels.manager import register_channel
from iflow_bot.config.schema import FeishuConfig

try:
    import lark_oapi as lark
    from lark_oapi.api.im.v1 import (
        CreateFileRequest,
        CreateFileRequestBody,
        CreateImageRequest,
        CreateImageRequestBody,
        DeleteMessageReactionRequest,
        CreateMessageRequest,
        CreateMessageRequestBody,
        CreateMessageReactionRequest,
        CreateMessageReactionRequestBody,
        Emoji,
        GetMessageResourceRequest,
        PatchMessageRequest,
        PatchMessageRequestBody,
        P2ImMessageReceiveV1,
    )
    FEISHU_AVAILABLE = True
except ImportError:
    FEISHU_AVAILABLE = False
    lark = None  # type: ignore
    Emoji = None  # type: ignore


logger = logging.getLogger(__name__)


# Message type display mapping
MSG_TYPE_MAP = {
    "image": "[image]",
    "audio": "[audio]",
    "file": "[file]",
    "sticker": "[sticker]",
}


def _extract_share_card_content(content_json: dict, msg_type: str) -> str:
    """Extract text representation from share cards and interactive messages."""
    parts = []

    if msg_type == "share_chat":
        parts.append(f"[shared chat: {content_json.get('chat_id', '')}]")
    elif msg_type == "share_user":
        parts.append(f"[shared user: {content_json.get('user_id', '')}]")
    elif msg_type == "interactive":
        parts.extend(_extract_interactive_content(content_json))
    elif msg_type == "share_calendar_event":
        parts.append(f"[shared calendar event: {content_json.get('event_key', '')}]")
    elif msg_type == "system":
        parts.append("[system message]")
    elif msg_type == "merge_forward":
        parts.append("[merged forward messages]")

    return "\n".join(parts) if parts else f"[{msg_type}]"


def _extract_interactive_content(content: dict) -> list[str]:
    """Recursively extract text and links from interactive card content."""
    parts = []

    if isinstance(content, str):
        try:
            content = json.loads(content)
        except (json.JSONDecodeError, TypeError):
            return [content] if content.strip() else []

    if not isinstance(content, dict):
        return parts

    if "title" in content:
        title = content["title"]
        if isinstance(title, dict):
            title_content = title.get("content", "") or title.get("text", "")
            if title_content:
                parts.append(f"title: {title_content}")
        elif isinstance(title, str):
            parts.append(f"title: {title}")

    elements = content.get("elements")
    if isinstance(elements, list):
        for element in elements:
            parts.extend(_extract_element_content(element))

    card = content.get("card", {})
    if card:
        parts.extend(_extract_interactive_content(card))

    header = content.get("header", {})
    if header:
        header_title = header.get("title", {})
        if isinstance(header_title, dict):
            header_text = header_title.get("content", "") or header_title.get("text", "")
            if header_text:
                parts.append(f"title: {header_text}")

    return parts


def _extract_element_content(element: dict) -> list[str]:
    """Extract content from a single card element."""
    parts = []

    if not isinstance(element, dict):
        return parts

    tag = element.get("tag", "")

    if tag in ("markdown", "lark_md"):
        content = element.get("content", "")
        if content:
            parts.append(content)

    elif tag == "div":
        text = element.get("text", {})
        if isinstance(text, dict):
            text_content = text.get("content", "") or text.get("text", "")
            if text_content:
                parts.append(text_content)
        elif isinstance(text, str):
            parts.append(text)
        for field in element.get("fields", []):
            if isinstance(field, dict):
                field_text = field.get("text", {})
                if isinstance(field_text, dict):
                    c = field_text.get("content", "")
                    if c:
                        parts.append(c)

    elif tag == "a":
        href = element.get("href", "")
        text = element.get("text", "")
        if href:
            parts.append(f"link: {href}")
        if text:
            parts.append(text)

    elif tag == "button":
        text = element.get("text", {})
        if isinstance(text, dict):
            c = text.get("content", "")
            if c:
                parts.append(c)
        url = element.get("url", "") or element.get("multi_url", {}).get("url", "")
        if url:
            parts.append(f"link: {url}")

    elif tag == "img":
        alt = element.get("alt", {})
        parts.append(alt.get("content", "[image]") if isinstance(alt, dict) else "[image]")

    elif tag == "note":
        for ne in element.get("elements", []):
            parts.extend(_extract_element_content(ne))

    elif tag == "column_set":
        for col in element.get("columns", []):
            for ce in col.get("elements", []):
                parts.extend(_extract_element_content(ce))

    elif tag == "plain_text":
        content = element.get("content", "")
        if content:
            parts.append(content)

    else:
        for ne in element.get("elements", []):
            parts.extend(_extract_element_content(ne))

    return parts


def _extract_post_text(content_json: dict) -> str:
    """Extract plain text from Feishu post (rich text) message content.

    Supports two formats:
    1. Direct format: {"title": "...", "content": [...]}
    2. Localized format: {"zh_cn": {"title": "...", "content": [...]}}
    """
    def extract_from_lang(lang_content: dict) -> Optional[str]:
        if not isinstance(lang_content, dict):
            return None
        title = lang_content.get("title", "")
        content_blocks = lang_content.get("content", [])
        if not isinstance(content_blocks, list):
            return None
        text_parts = []
        if title:
            text_parts.append(title)
        for block in content_blocks:
            if not isinstance(block, list):
                continue
            for element in block:
                if isinstance(element, dict):
                    tag = element.get("tag")
                    if tag == "text":
                        text_parts.append(element.get("text", ""))
                    elif tag == "a":
                        text_parts.append(element.get("text", ""))
                    elif tag == "at":
                        text_parts.append(f"@{element.get('user_name', 'user')}")
        return " ".join(text_parts).strip() if text_parts else None

    # Try direct format first
    if "content" in content_json:
        result = extract_from_lang(content_json)
        if result:
            return result

    # Try localized format
    for lang_key in ("zh_cn", "en_us", "ja_jp"):
        lang_content = content_json.get(lang_key)
        result = extract_from_lang(lang_content)
        if result:
            return result

    return ""


@register_channel("feishu")
class FeishuChannel(BaseChannel):
    """Feishu/Lark channel using WebSocket long connection.

    Uses WebSocket to receive events - no public IP or webhook required.

    Requires:
    - App ID and App Secret from Feishu Open Platform
    - Bot capability enabled
    - Event subscription enabled (im.message.receive_v1)

    Attributes:
        name: Channel name identifier
        config: FeishuConfig configuration object
        bus: MessageBus instance
        _client: Lark client for API calls
        _ws_client: WebSocket client for long connection
        _ws_thread: Thread running WebSocket client
        _processed_message_ids: Ordered deduplication cache
        _loop: Async event loop reference
    """

    name = "feishu"

    def __init__(self, config: FeishuConfig, bus: MessageBus):
        """Initialize Feishu Channel.

        Args:
            config: FeishuConfig configuration object
            bus: MessageBus instance
        """
        super().__init__(config, bus)
        self.config: FeishuConfig = config
        self._client: Any = None
        self._ws_client: Any = None
        self._ws_thread: Optional[threading.Thread] = None
        self._processed_message_ids: OrderedDict[str, None] = OrderedDict()
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._streaming_message_ids: dict[str, str] = {}
        self._streaming_last_content: dict[str, str] = {}
        self._typing_reaction_ids: dict[str, str] = {}

    async def start(self) -> None:
        """Start the Feishu bot with WebSocket long connection."""
        if not FEISHU_AVAILABLE:
            logger.error("Feishu SDK not installed. Run: pip install lark-oapi")
            return

        if not self.config.app_id or not self.config.app_secret:
            logger.error("Feishu app_id and app_secret not configured")
            return

        self._running = True
        self._loop = asyncio.get_running_loop()

        # Create Lark client for sending messages
        self._client = lark.Client.builder() \
            .app_id(self.config.app_id) \
            .app_secret(self.config.app_secret) \
            .log_level(lark.LogLevel.ERROR) \
            .build()

        # Create event handler (only register message receive)
        event_handler = lark.EventDispatcherHandler.builder(
            self.config.encrypt_key or "",
            self.config.verification_token or "",
        ).register_p2_im_message_receive_v1(
            self._on_message_sync
        ).build()

        # Create WebSocket client for long connection
        self._ws_client = lark.ws.Client(
            self.config.app_id,
            self.config.app_secret,
            event_handler=event_handler,
            log_level=lark.LogLevel.ERROR
        )

        # Start WebSocket client in a separate thread with reconnect loop
        def run_ws() -> None:
            import time
            import asyncio
            while self._running:
                try:
                    # 关键修复：lark_oapi/ws/client.py 在模块级别调用 asyncio.get_event_loop()
                    # 并保存在模块变量中。我们需要替换这个变量为新线程的事件循环
                    import lark_oapi.ws.client as ws_client_module
                    
                    # 创建新的事件循环并替换模块变量
                    new_loop = asyncio.new_event_loop()
                    asyncio.set_event_loop(new_loop)
                    ws_client_module.loop = new_loop
                    
                    # 现在可以安全地调用 start()
                    self._ws_client.start()
                except Exception as e:
                    logger.warning(f"Feishu WebSocket error: {e}")
                if self._running:
                    time.sleep(5)

        self._ws_thread = threading.Thread(target=run_ws, daemon=True)
        self._ws_thread.start()

        logger.info("Feishu bot started with WebSocket long connection")
        logger.info("No public IP required - using WebSocket to receive events")

        # Keep running until stopped
        while self._running:
            await asyncio.sleep(1)

    async def stop(self) -> None:
        """Stop the Feishu bot."""
        self._running = False
        if self._ws_client:
            try:
                self._ws_client.stop()
            except Exception as e:
                logger.warning(f"Error stopping WebSocket client: {e}")
        logger.info("Feishu bot stopped")

    # Regex patterns for markdown conversion
    _TABLE_RE = re.compile(
        r"((?:^[ \t]*\|.+\|[ \t]*\n)(?:^[ \t]*\|[-:\s|]+\|[ \t]*\n)(?:^[ \t]*\|.+\|[ \t]*\n?)+)",
        re.MULTILINE,
    )
    _HEADING_RE = re.compile(r"^(#{1,6})\s+(.+)$", re.MULTILINE)
    _CODE_BLOCK_RE = re.compile(r"(```[\s\S]*?```)", re.MULTILINE)

    @staticmethod
    def _parse_md_table(table_text: str) -> Optional[dict]:
        """Parse a markdown table into a Feishu table element."""
        lines = [l.strip() for l in table_text.strip().split("\n") if l.strip()]
        if len(lines) < 3:
            return None

        def split_line(l: str) -> list[str]:
            return [c.strip() for c in l.strip("|").split("|")]

        headers = split_line(lines[0])
        rows = [split_line(l) for l in lines[2:]]
        columns = [
            {"tag": "column", "name": f"c{i}", "display_name": h, "width": "auto"}
            for i, h in enumerate(headers)
        ]
        return {
            "tag": "table",
            "page_size": len(rows) + 1,
            "columns": columns,
            "rows": [
                {f"c{i}": r[i] if i < len(r) else "" for i in range(len(headers))}
                for r in rows
            ],
        }

    def _build_card_elements(self, content: str) -> list[dict]:
        """Split content into div/markdown + table elements for Feishu card."""
        elements: list[dict] = []
        last_end = 0

        for m in self._TABLE_RE.finditer(content):
            before = content[last_end:m.start()]
            if before.strip():
                elements.extend(self._split_headings(before))
            table = self._parse_md_table(m.group(1))
            elements.append(table or {"tag": "markdown", "content": m.group(1)})
            last_end = m.end()

        remaining = content[last_end:]
        if remaining.strip():
            elements.extend(self._split_headings(remaining))

        return elements or [{"tag": "markdown", "content": content}]

    def _split_headings(self, content: str) -> list[dict]:
        """Split content by headings, converting headings to div elements."""
        protected = content
        code_blocks: list[str] = []

        for m in self._CODE_BLOCK_RE.finditer(content):
            code_blocks.append(m.group(1))
            protected = protected.replace(m.group(1), f"\x00CODE{len(code_blocks)-1}\x00", 1)

        elements: list[dict] = []
        last_end = 0

        for m in self._HEADING_RE.finditer(protected):
            before = protected[last_end:m.start()].strip()
            if before:
                elements.append({"tag": "markdown", "content": before})
            text = m.group(2).strip()
            elements.append({
                "tag": "div",
                "text": {
                    "tag": "lark_md",
                    "content": f"**{text}**",
                },
            })
            last_end = m.end()

        remaining = protected[last_end:].strip()
        if remaining:
            elements.append({"tag": "markdown", "content": remaining})

        # Restore code blocks
        for i, cb in enumerate(code_blocks):
            for el in elements:
                if el.get("tag") == "markdown":
                    el["content"] = el["content"].replace(f"\x00CODE{i}\x00", cb)

        return elements or [{"tag": "markdown", "content": content}]

    # File type mappings
    _IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".gif", ".bmp", ".webp", ".ico", ".tiff", ".tif"}
    _AUDIO_EXTS = {".opus"}
    _FILE_TYPE_MAP = {
        ".opus": "opus", ".mp4": "mp4", ".pdf": "pdf", ".doc": "doc", ".docx": "doc",
        ".xls": "xls", ".xlsx": "xls", ".ppt": "ppt", ".pptx": "ppt",
    }

    def _upload_image_sync(self, file_path: str) -> Optional[str]:
        """Upload an image to Feishu and return the image_key."""
        try:
            with open(file_path, "rb") as f:
                request = CreateImageRequest.builder() \
                    .request_body(
                        CreateImageRequestBody.builder()
                        .image_type("message")
                        .image(f)
                        .build()
                    ).build()
                response = self._client.im.v1.image.create(request)
                if response.success():
                    image_key = response.data.image_key
                    logger.debug(f"Uploaded image {os.path.basename(file_path)}: {image_key}")
                    return image_key
                else:
                    logger.error(f"Failed to upload image: code={response.code}, msg={response.msg}")
                    return None
        except Exception as e:
            logger.error(f"Error uploading image {file_path}: {e}")
            return None

    def _upload_file_sync(self, file_path: str) -> Optional[str]:
        """Upload a file to Feishu and return the file_key."""
        ext = os.path.splitext(file_path)[1].lower()
        file_type = self._FILE_TYPE_MAP.get(ext, "stream")
        file_name = os.path.basename(file_path)
        try:
            with open(file_path, "rb") as f:
                request = CreateFileRequest.builder() \
                    .request_body(
                        CreateFileRequestBody.builder()
                        .file_type(file_type)
                        .file_name(file_name)
                        .file(f)
                        .build()
                    ).build()
                response = self._client.im.v1.file.create(request)
                if response.success():
                    file_key = response.data.file_key
                    logger.debug(f"Uploaded file {file_name}: {file_key}")
                    return file_key
                else:
                    logger.error(f"Failed to upload file: code={response.code}, msg={response.msg}")
                    return None
        except Exception as e:
            logger.error(f"Error uploading file {file_path}: {e}")
            return None

    def _download_image_sync(self, message_id: str, image_key: str) -> tuple[Optional[bytes], Optional[str]]:
        """Download an image from Feishu message."""
        try:
            request = GetMessageResourceRequest.builder() \
                .message_id(message_id) \
                .file_key(image_key) \
                .type("image") \
                .build()
            response = self._client.im.v1.message_resource.get(request)
            if response.success():
                file_data = response.file
                if hasattr(file_data, 'read'):
                    file_data = file_data.read()
                return file_data, response.file_name
            else:
                logger.error(f"Failed to download image: code={response.code}, msg={response.msg}")
                return None, None
        except Exception as e:
            logger.error(f"Error downloading image {image_key}: {e}")
            return None, None

    def _download_file_sync(
        self, message_id: str, file_key: str, resource_type: str = "file"
    ) -> tuple[Optional[bytes], Optional[str]]:
        """Download a file/audio/media from a Feishu message."""
        try:
            request = GetMessageResourceRequest.builder() \
                .message_id(message_id) \
                .file_key(file_key) \
                .type(resource_type) \
                .build()
            response = self._client.im.v1.message_resource.get(request)
            if response.success():
                file_data = response.file
                if hasattr(file_data, "read"):
                    file_data = file_data.read()
                return file_data, response.file_name
            else:
                logger.error(f"Failed to download {resource_type}: code={response.code}, msg={response.msg}")
                return None, None
        except Exception as e:
            logger.error(f"Error downloading {resource_type} {file_key}: {e}")
            return None, None

    async def _download_and_save_media(
        self,
        msg_type: str,
        content_json: dict,
        message_id: Optional[str] = None
    ) -> tuple[Optional[str], str]:
        """Download media from Feishu and save to local disk.

        Args:
            msg_type: Message type (image, audio, file, media)
            content_json: Parsed message content
            message_id: Feishu message ID

        Returns:
            (file_path, content_text) - file_path is None if download failed
        """
        loop = asyncio.get_running_loop()
        media_dir = Path.home() / ".iflow-bot" / "media"
        media_dir.mkdir(parents=True, exist_ok=True)

        data: Optional[bytes] = None
        filename: Optional[str] = None

        if msg_type == "image":
            image_key = content_json.get("image_key")
            if image_key and message_id:
                data, filename = await loop.run_in_executor(
                    None, self._download_image_sync, message_id, image_key
                )
                if not filename:
                    filename = f"{image_key[:16]}.jpg"

        elif msg_type in ("audio", "file", "media"):
            file_key = content_json.get("file_key")
            if file_key and message_id:
                data, filename = await loop.run_in_executor(
                    None, self._download_file_sync, message_id, file_key, msg_type
                )
                if not filename:
                    ext = {"audio": ".opus", "media": ".mp4"}.get(msg_type, "")
                    filename = f"{file_key[:16]}{ext}"

        if data and filename:
            file_path = media_dir / filename
            file_path.write_bytes(data)
            logger.debug(f"Downloaded {msg_type} to {file_path}")
            return str(file_path), f"[{msg_type}: {filename}]"

        return None, f"[{msg_type}: download failed]"

    def _send_message_sync(
        self, receive_id_type: str, receive_id: str, msg_type: str, content: str
    ) -> Optional[str]:
        """Send a single message (text/image/file/interactive) synchronously."""
        try:
            request = CreateMessageRequest.builder() \
                .receive_id_type(receive_id_type) \
                .request_body(
                    CreateMessageRequestBody.builder()
                    .receive_id(receive_id)
                    .msg_type(msg_type)
                    .content(content)
                    .build()
                ).build()
            response = self._client.im.v1.message.create(request)
            if not response.success():
                logger.error(
                    f"Failed to send Feishu {msg_type} message: code={response.code}, "
                    f"msg={response.msg}, log_id={response.get_log_id()}"
                )
                return None

            message_id = None
            data = getattr(response, "data", None)
            if data is not None:
                message_id = getattr(data, "message_id", None)
                if message_id is None and isinstance(data, dict):
                    message_id = data.get("message_id")

            logger.debug(f"Feishu {msg_type} message sent to {receive_id}")
            return message_id
        except Exception as e:
            logger.error(f"Error sending Feishu {msg_type} message: {e}")
            return None

    def _patch_message_sync(self, message_id: str, content: str) -> bool:
        """Patch an existing Feishu message content synchronously."""
        try:
            request = PatchMessageRequest.builder() \
                .message_id(message_id) \
                .request_body(
                    PatchMessageRequestBody.builder()
                    .content(content)
                    .build()
                ).build()
            response = self._client.im.v1.message.patch(request)
            if not response.success():
                logger.error(
                    f"Failed to patch Feishu message: code={response.code}, "
                    f"msg={response.msg}, log_id={response.get_log_id()}"
                )
                return False
            return True
        except Exception as e:
            logger.error(f"Error patching Feishu message {message_id}: {e}")
            return False

    async def _handle_streaming_message(
        self, msg: OutboundMessage, receive_id_type: str
    ) -> None:
        """Handle Feishu streaming message by create+patch interactive card."""
        stream_key = msg.chat_id
        loop = asyncio.get_running_loop()

        if msg.metadata.get("_streaming_end"):
            source_message_id = msg.metadata.get("reply_to_id")
            if source_message_id:
                await self._remove_typing_reaction(source_message_id)
            self._streaming_message_ids.pop(stream_key, None)
            self._streaming_last_content.pop(stream_key, None)
            logger.debug(f"Feishu streaming ended: {stream_key}")
            return

        content = (msg.content or "").strip()
        if not content:
            return

        if self._streaming_last_content.get(stream_key) == content:
            return

        source_message_id = msg.metadata.get("reply_to_id")
        if source_message_id:
            await self._remove_typing_reaction(source_message_id)

        card_content = json.dumps({
            "config": {"wide_screen_mode": True},
            "elements": self._build_card_elements(content),
        }, ensure_ascii=False)

        message_id = self._streaming_message_ids.get(stream_key)
        if message_id:
            patched = await loop.run_in_executor(
                None, self._patch_message_sync, message_id, card_content
            )
            if patched:
                self._streaming_last_content[stream_key] = content
                logger.debug(f"Feishu streaming patched: {stream_key}")
                return
            self._streaming_message_ids.pop(stream_key, None)
            logger.warning(f"Feishu streaming patch failed, recreating: {stream_key}")

        created_id = await loop.run_in_executor(
            None, self._send_message_sync,
            receive_id_type, msg.chat_id, "interactive", card_content
        )
        if created_id:
            self._streaming_message_ids[stream_key] = created_id
            self._streaming_last_content[stream_key] = content
            logger.info(f"Feishu streaming placeholder sent: {stream_key}")
        else:
            logger.error(f"Feishu streaming placeholder send failed: {stream_key}")

    def _add_reaction_sync(self, message_id: str, emoji_type: str) -> Optional[str]:
        """Sync helper for adding reaction (runs in thread pool)."""
        try:
            request = CreateMessageReactionRequest.builder() \
                .message_id(message_id) \
                .request_body(
                    CreateMessageReactionRequestBody.builder()
                    .reaction_type(Emoji.builder().emoji_type(emoji_type).build())
                    .build()
                ).build()

            response = self._client.im.v1.message_reaction.create(request)

            if not response.success():
                logger.warning(f"Failed to add reaction: code={response.code}, msg={response.msg}")
                return None

            reaction_id = None
            data = getattr(response, "data", None)
            if data is not None:
                reaction_id = getattr(data, "reaction_id", None)
                if reaction_id is None and isinstance(data, dict):
                    reaction_id = data.get("reaction_id")
            logger.debug(f"Added {emoji_type} reaction to message {message_id}")
            return reaction_id
        except Exception as e:
            logger.warning(f"Error adding reaction: {e}")
            return None

    def _delete_reaction_sync(self, message_id: str, reaction_id: str) -> bool:
        """Sync helper for deleting reaction (runs in thread pool)."""
        try:
            request = DeleteMessageReactionRequest.builder() \
                .message_id(message_id) \
                .reaction_id(reaction_id) \
                .build()
            response = self._client.im.v1.message_reaction.delete(request)
            if not response.success():
                logger.warning(
                    f"Failed to delete reaction: code={response.code}, msg={response.msg}"
                )
                return False
            logger.debug(f"Deleted reaction from message {message_id}")
            return True
        except Exception as e:
            logger.warning(f"Error deleting reaction: {e}")
            return False

    async def _add_reaction(self, message_id: str, emoji_type: str = "THUMBSUP") -> Optional[str]:
        """Add a reaction emoji to a message (non-blocking).

        Common emoji types: THUMBSUP, OK, EYES, DONE, OnIt, HEART
        """
        if not self._client or not Emoji:
            return None

        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(None, self._add_reaction_sync, message_id, emoji_type)

    async def _remove_typing_reaction(self, source_message_id: str) -> None:
        """Remove the pending typing reaction from a source message."""
        reaction_id = self._typing_reaction_ids.pop(source_message_id, None)
        if not reaction_id:
            return

        loop = asyncio.get_running_loop()
        deleted = await loop.run_in_executor(
            None, self._delete_reaction_sync, source_message_id, reaction_id
        )
        if deleted:
            logger.debug(f"Typing reaction cleared: {source_message_id}")

    async def send(self, msg: OutboundMessage) -> None:
        """Send a message through Feishu, including media (images/files) if present.

        Supports:
        - Text messages (converted to interactive cards)
        - Image messages
        - File messages
        - Audio messages

        Args:
            msg: OutboundMessage to send
        """
        if not self._client:
            logger.warning("Feishu client not initialized")
            return

        try:
            # Determine receive_id_type based on chat_id prefix
            receive_id_type = "chat_id" if msg.chat_id.startswith("oc_") else "open_id"
            loop = asyncio.get_running_loop()

            if msg.metadata.get("_streaming") or msg.metadata.get("_streaming_end"):
                await self._handle_streaming_message(msg, receive_id_type)
                return

            if msg.metadata.get("_progress"):
                return

            source_message_id = msg.metadata.get("reply_to_id")
            if source_message_id:
                await self._remove_typing_reaction(source_message_id)

            self._streaming_message_ids.pop(msg.chat_id, None)
            self._streaming_last_content.pop(msg.chat_id, None)

            # Handle media files first
            media = getattr(msg, 'media', None) or []
            for file_path in media:
                if not os.path.isfile(file_path):
                    logger.warning(f"Media file not found: {file_path}")
                    continue
                ext = os.path.splitext(file_path)[1].lower()
                if ext in self._IMAGE_EXTS:
                    key = await loop.run_in_executor(None, self._upload_image_sync, file_path)
                    if key:
                        await loop.run_in_executor(
                            None, self._send_message_sync,
                            receive_id_type, msg.chat_id, "image",
                            json.dumps({"image_key": key}, ensure_ascii=False),
                        )
                else:
                    key = await loop.run_in_executor(None, self._upload_file_sync, file_path)
                    if key:
                        media_type = "audio" if ext in self._AUDIO_EXTS else "file"
                        await loop.run_in_executor(
                            None, self._send_message_sync,
                            receive_id_type, msg.chat_id, media_type,
                            json.dumps({"file_key": key}, ensure_ascii=False),
                        )

            # Send text content as interactive card
            if msg.content and msg.content.strip():
                card = {
                    "config": {"wide_screen_mode": True},
                    "elements": self._build_card_elements(msg.content)
                }
                await loop.run_in_executor(
                    None, self._send_message_sync,
                    receive_id_type, msg.chat_id, "interactive",
                    json.dumps(card, ensure_ascii=False),
                )

        except Exception as e:
            logger.error(f"Error sending Feishu message: {e}")

    def _on_message_sync(self, data: "P2ImMessageReceiveV1") -> None:
        """Sync handler for incoming messages (called from WebSocket thread).

        Schedules async handling in the main event loop.
        """
        if self._loop and self._loop.is_running():
            asyncio.run_coroutine_threadsafe(self._on_message(data), self._loop)

    async def _on_message(self, data: "P2ImMessageReceiveV1") -> None:
        """Handle incoming message from Feishu.

        Processes message content, downloads media if present,
        and publishes to message bus.
        """
        try:
            event = data.event
            message = event.message
            sender = event.sender

            # Deduplication check
            message_id = message.message_id
            if message_id in self._processed_message_ids:
                return
            self._processed_message_ids[message_id] = None

            # Trim cache (keep last 1000 messages)
            while len(self._processed_message_ids) > 1000:
                self._processed_message_ids.popitem(last=False)

            # Skip bot messages
            if sender.sender_type == "bot":
                return

            sender_id = sender.sender_id.open_id if sender.sender_id else "unknown"
            chat_id = message.chat_id
            chat_type = message.chat_type
            msg_type = message.message_type

            # 秒回“敲键盘”反应，作为处理中提示
            typing_reaction_id = await self._add_reaction(message_id, "OnIt")
            if typing_reaction_id:
                self._typing_reaction_ids[message_id] = typing_reaction_id

            # Parse content
            content_parts: list[str] = []
            media_paths: list[str] = []

            try:
                content_json = json.loads(message.content) if message.content else {}
            except json.JSONDecodeError:
                content_json = {}

            if msg_type == "text":
                text = content_json.get("text", "")
                if text:
                    content_parts.append(text)

            elif msg_type == "post":
                text = _extract_post_text(content_json)
                if text:
                    content_parts.append(text)

            elif msg_type in ("image", "audio", "file", "media"):
                file_path, content_text = await self._download_and_save_media(
                    msg_type, content_json, message_id
                )
                if file_path:
                    media_paths.append(file_path)
                content_parts.append(content_text)

            elif msg_type in ("share_chat", "share_user", "interactive",
                            "share_calendar_event", "system", "merge_forward"):
                text = _extract_share_card_content(content_json, msg_type)
                if text:
                    content_parts.append(text)

            else:
                content_parts.append(MSG_TYPE_MAP.get(msg_type, f"[{msg_type}]"))

            content = "\n".join(content_parts) if content_parts else ""

            if not content and not media_paths:
                return

            # Determine reply target: group chat uses chat_id, private uses sender_id
            reply_to = chat_id if chat_type == "group" else sender_id

            logger.info(
                "Feishu inbound accepted: message_id=%s chat_type=%s msg_type=%s sender=%s reply_to=%s content_len=%d media=%d",
                message_id,
                chat_type,
                msg_type,
                sender_id,
                reply_to,
                len(content),
                len(media_paths),
            )

            # Forward to message bus
            await self._handle_message(
                sender_id=sender_id,
                chat_id=reply_to,
                content=content,
                media=media_paths,
                metadata={
                    "message_id": message_id,
                    "chat_type": chat_type,
                    "msg_type": msg_type,
                }
            )

        except Exception as e:
            logger.error(f"Error processing Feishu message: {e}")
