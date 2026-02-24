"""Mochat channel implementation using Socket.IO with HTTP polling fallback.

使用 Socket.IO 连接 Mochat 平台，支持 WebSocket 实时通信和 HTTP 轮询回退。
支持会话 和面板 消息。
"""

import asyncio
import json
import logging
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Set

from iflow_bot.bus.events import OutboundMessage
from iflow_bot.bus.queue import MessageBus
from iflow_bot.channels.base import BaseChannel
from iflow_bot.channels.manager import register_channel
from iflow_bot.config.schema import MochatConfig

try:
    import httpx
    HTTPX_AVAILABLE = True
except ImportError:
    HTTPX_AVAILABLE = False
    httpx = None  # type: ignore

try:
    import socketio
    SOCKETIO_AVAILABLE = True
except ImportError:
    SOCKETIO_AVAILABLE = False
    socketio = None  # type: ignore

try:
    import msgpack  # noqa: F401
    MSGPACK_AVAILABLE = True
except ImportError:
    MSGPACK_AVAILABLE = False


logger = logging.getLogger(__name__)


# 常量
MAX_SEEN_MESSAGE_IDS = 2000
CURSOR_SAVE_DEBOUNCE_S = 0.5


# ---------------------------------------------------------------------------
# 数据类
# ---------------------------------------------------------------------------

@dataclass
class MochatBufferedEntry:
    """缓冲的入站消息条目 (用于延迟发送)。"""
    raw_body: str
    author: str
    sender_name: str = ""
    sender_username: str = ""
    timestamp: Optional[int] = None
    message_id: str = ""
    group_id: str = ""


@dataclass
class DelayState:
    """每个目标的延迟消息状态。"""
    entries: List[MochatBufferedEntry] = field(default_factory=list)
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    timer: Optional[asyncio.Task] = None


@dataclass
class MochatTarget:
    """出站目标解析结果。"""
    id: str
    is_panel: bool


# ---------------------------------------------------------------------------
# 辅助函数
# ---------------------------------------------------------------------------

def _safe_dict(value: Any) -> dict:
    """返回字典，否则返回空字典。"""
    return value if isinstance(value, dict) else {}


def _str_field(src: dict, *keys: str) -> str:
    """返回第一个非空字符串值。"""
    for k in keys:
        v = src.get(k)
        if isinstance(v, str) and v.strip():
            return v.strip()
    return ""


def normalize_mochat_content(content: Any) -> str:
    """标准化内容为文本。"""
    if isinstance(content, str):
        return content.strip()
    if content is None:
        return ""
    try:
        return json.dumps(content, ensure_ascii=False)
    except TypeError:
        return str(content)


def resolve_mochat_target(raw: str) -> MochatTarget:
    """解析目标 ID 和类型。"""
    trimmed = (raw or "").strip()
    if not trimmed:
        return MochatTarget(id="", is_panel=False)

    lowered = trimmed.lower()
    cleaned, forced_panel = trimmed, False

    for prefix in ("mochat:", "group:", "channel:", "panel:"):
        if lowered.startswith(prefix):
            cleaned = trimmed[len(prefix):].strip()
            forced_panel = prefix in {"group:", "channel:", "panel:"}
            break

    if not cleaned:
        return MochatTarget(id="", is_panel=False)

    return MochatTarget(
        id=cleaned,
        is_panel=forced_panel or not cleaned.startswith("session_")
    )


def extract_mention_ids(value: Any) -> List[str]:
    """从提及数据中提取 ID。"""
    if not isinstance(value, list):
        return []
    ids: List[str] = []
    for item in value:
        if isinstance(item, str):
            if item.strip():
                ids.append(item.strip())
        elif isinstance(item, dict):
            for key in ("id", "userId", "_id"):
                candidate = item.get(key)
                if isinstance(candidate, str) and candidate.strip():
                    ids.append(candidate.strip())
                    break
    return ids


def resolve_was_mentioned(payload: dict, agent_user_id: str) -> bool:
    """判断是否被提及。"""
    meta = payload.get("meta")
    if isinstance(meta, dict):
        if meta.get("mentioned") is True or meta.get("wasMentioned") is True:
            return True
        for f in ("mentions", "mentionIds", "mentionedUserIds", "mentionedUsers"):
            if agent_user_id and agent_user_id in extract_mention_ids(meta.get(f)):
                return True

    if not agent_user_id:
        return False

    content = payload.get("content")
    if not isinstance(content, str) or not content:
        return False

    return f"<@{agent_user_id}>" in content or f"@{agent_user_id}" in content


def build_buffered_body(entries: List[MochatBufferedEntry], is_group: bool) -> str:
    """从缓冲条目构建消息体。"""
    if not entries:
        return ""
    if len(entries) == 1:
        return entries[0].raw_body

    lines: List[str] = []
    for entry in entries:
        if not entry.raw_body:
            continue
        if is_group:
            label = entry.sender_name.strip() or entry.sender_username.strip() or entry.author
            if label:
                lines.append(f"{label}: {entry.raw_body}")
                continue
        lines.append(entry.raw_body)

    return "\n".join(lines).strip()


def parse_timestamp(value: Any) -> Optional[int]:
    """解析时间戳为毫秒。"""
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        return int(
            datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp() * 1000
        )
    except ValueError:
        return None


# ---------------------------------------------------------------------------
# Channel
# ---------------------------------------------------------------------------

@register_channel("mochat")
class MochatChannel(BaseChannel):
    """Mochat Channel - 使用 Socket.IO 连接，支持 HTTP 轮询回退。

    支持功能:
    - 会话 消息
    - 面板 消息
    - 自动发现会话和面板
    - 延迟消息发送 (非提及消息)
    - 消息去重

    要求:
    - claw_token: 认证 Token
    - agent_user_id (可选): Agent 用户 ID，用于提及检测

    Attributes:
        name: 渠道名称 ("mochat")
        config: Mochat 配置对象
        bus: 消息总线实例
        _http: HTTP 客户端
        _socket: Socket.IO 客户端
        _ws_connected: WebSocket 连接状态
        _ws_ready: WebSocket 就绪状态
    """

    name = "mochat"

    def __init__(self, config: MochatConfig, bus: MessageBus):
        """初始化 Mochat Channel。

        Args:
            config: Mochat 配置对象
            bus: 消息总线实例
        """
        super().__init__(config, bus)
        self.config: MochatConfig = config
        self._http: Optional[httpx.AsyncClient] = None
        self._socket: Any = None
        self._ws_connected = False
        self._ws_ready = False

        # 状态存储
        self._state_dir = Path.home() / ".iflow-bot" / "mochat"
        self._cursor_path = self._state_dir / "session_cursors.json"
        self._session_cursor: Dict[str, int] = {}
        self._cursor_save_task: Optional[asyncio.Task] = None

        # 目标管理
        self._session_set: Set[str] = set()
        self._panel_set: Set[str] = set()
        self._auto_discover_sessions = False
        self._auto_discover_panels = False

        # 会话映射
        self._cold_sessions: Set[str] = set()
        self._session_by_converse: Dict[str, str] = {}

        # 去重
        self._seen_set: Dict[str, Set[str]] = {}
        self._seen_queue: Dict[str, deque] = {}

        # 延迟状态
        self._delay_states: Dict[str, DelayState] = {}

        # 回退模式
        self._fallback_mode = False
        self._session_fallback_tasks: Dict[str, asyncio.Task] = {}
        self._panel_fallback_tasks: Dict[str, asyncio.Task] = {}
        self._refresh_task: Optional[asyncio.Task] = None
        self._target_locks: Dict[str, asyncio.Lock] = {}

    # ---- 生命周期 ---------------------------------------------------------

    async def start(self) -> None:
        """启动 Mochat Channel。"""
        if not HTTPX_AVAILABLE:
            logger.error(f"[{self.name}] httpx not installed. Run: pip install httpx")
            return

        if not self.config.claw_token:
            logger.error(f"[{self.name}] claw_token not configured")
            return

        self._running = True
        self._http = httpx.AsyncClient(timeout=30.0)
        self._state_dir.mkdir(parents=True, exist_ok=True)
        await self._load_session_cursors()
        self._seed_targets_from_config()
        await self._refresh_targets(subscribe_new=False)

        if not await self._start_socket_client():
            await self._ensure_fallback_workers()

        self._refresh_task = asyncio.create_task(self._refresh_loop())

        logger.info(f"[{self.name}] Mochat channel started")

        # 保持运行
        while self._running:
            await asyncio.sleep(1)

    async def stop(self) -> None:
        """停止所有工作线程并清理资源。"""
        self._running = False

        if self._refresh_task:
            self._refresh_task.cancel()
            self._refresh_task = None

        await self._stop_fallback_workers()
        await self._cancel_delay_timers()

        if self._socket:
            try:
                await self._socket.disconnect()
            except Exception:
                pass
            self._socket = None

        if self._cursor_save_task:
            self._cursor_save_task.cancel()
            self._cursor_save_task = None
        await self._save_session_cursors()

        if self._http:
            await self._http.aclose()
            self._http = None

        self._ws_connected = False
        self._ws_ready = False

        logger.info(f"[{self.name}] Mochat channel stopped")

    async def send(self, msg: OutboundMessage) -> None:
        """发送消息到会话或面板。

        Args:
            msg: 出站消息对象
                - chat_id: 目标 ID (session_xxx 或 panel ID)
                - content: 消息内容
                - reply_to: 回复的消息 ID (可选)
                - metadata: 可包含 group_id (面板消息)
        """
        if not self.config.claw_token:
            logger.warning(f"[{self.name}] claw_token missing, skip send")
            return

        parts = [msg.content.strip()] if msg.content and msg.content.strip() else []
        if msg.media:
            parts.extend(m for m in msg.media if isinstance(m, str) and m.strip())
        content = "\n".join(parts).strip()

        if not content:
            return

        target = resolve_mochat_target(msg.chat_id)
        if not target.id:
            logger.warning(f"[{self.name}] Outbound target is empty")
            return

        is_panel = (
            target.is_panel or target.id in self._panel_set
        ) and not target.id.startswith("session_")

        try:
            if is_panel:
                await self._api_send_panel(
                    target.id, content, msg.reply_to,
                    self._read_group_id(msg.metadata)
                )
            else:
                await self._api_send_session(target.id, content, msg.reply_to)
        except Exception as e:
            logger.error(f"[{self.name}] Failed to send message: {e}")

    # ---- 配置 / 初始化辅助 -------------------------------------------------

    def _seed_targets_from_config(self) -> None:
        """从配置初始化目标集合。"""
        sessions, self._auto_discover_sessions = self._normalize_id_list(
            self.config.sessions
        )
        panels, self._auto_discover_panels = self._normalize_id_list(
            self.config.panels
        )
        self._session_set.update(sessions)
        self._panel_set.update(panels)

        for sid in sessions:
            if sid not in self._session_cursor:
                self._cold_sessions.add(sid)

    @staticmethod
    def _normalize_id_list(values: List[str]) -> tuple:
        """规范化 ID 列表。"""
        cleaned = [str(v).strip() for v in values if str(v).strip()]
        return (
            sorted({v for v in cleaned if v != "*"}),
            "*" in cleaned
        )

    # ---- WebSocket ---------------------------------------------------------

    async def _start_socket_client(self) -> bool:
        """启动 Socket.IO 客户端。"""
        if not SOCKETIO_AVAILABLE:
            logger.warning(
                f"[{self.name}] python-socketio not installed, using polling fallback"
            )
            return False

        serializer = "default"
        if MSGPACK_AVAILABLE:
            serializer = "msgpack"

        client = socketio.AsyncClient(
            reconnection=True,
            reconnection_attempts=5,
            reconnection_delay=1.0,
            reconnection_delay_max=5.0,
            logger=False,
            engineio_logger=False,
            serializer=serializer,
        )

        @client.event
        async def connect() -> None:
            self._ws_connected = True
            self._ws_ready = False
            logger.info(f"[{self.name}] WebSocket connected")
            subscribed = await self._subscribe_all()
            self._ws_ready = subscribed
            if subscribed:
                await self._stop_fallback_workers()
            else:
                await self._ensure_fallback_workers()

        @client.event
        async def disconnect() -> None:
            if not self._running:
                return
            self._ws_connected = False
            self._ws_ready = False
            logger.warning(f"[{self.name}] WebSocket disconnected")
            await self._ensure_fallback_workers()

        @client.event
        async def connect_error(data: Any) -> None:
            logger.error(f"[{self.name}] WebSocket connect error: {data}")

        @client.on("claw.session.events")
        async def on_session_events(payload: dict) -> None:
            await self._handle_watch_payload(payload, "session")

        @client.on("claw.panel.events")
        async def on_panel_events(payload: dict) -> None:
            await self._handle_watch_payload(payload, "panel")

        socket_url = (self.config.socket_url or self.config.base_url).strip().rstrip("/")
        socket_path = (self.config.socket_path or "/socket.io").strip().lstrip("/")

        try:
            self._socket = client
            await client.connect(
                socket_url,
                transports=["websocket"],
                socketio_path=socket_path,
                auth={"token": self.config.claw_token},
                wait_timeout=10.0,
            )
            return True
        except Exception as e:
            logger.error(f"[{self.name}] Failed to connect WebSocket: {e}")
            try:
                await client.disconnect()
            except Exception:
                pass
            self._socket = None
            return False

    # ---- 订阅 --------------------------------------------------------------

    async def _subscribe_all(self) -> bool:
        """订阅所有会话和面板。"""
        ok = await self._subscribe_sessions(sorted(self._session_set))
        ok = await self._subscribe_panels(sorted(self._panel_set)) and ok

        if self._auto_discover_sessions or self._auto_discover_panels:
            await self._refresh_targets(subscribe_new=True)

        return ok

    async def _subscribe_sessions(self, session_ids: List[str]) -> bool:
        """订阅会话。"""
        if not session_ids:
            return True

        for sid in session_ids:
            if sid not in self._session_cursor:
                self._cold_sessions.add(sid)

        ack = await self._socket_call("com.claw.im.subscribeSessions", {
            "sessionIds": session_ids,
            "cursors": self._session_cursor,
            "limit": 50,
        })

        if not ack.get("result"):
            logger.error(
                f"[{self.name}] subscribeSessions failed: "
                f"{ack.get('message', 'unknown error')}"
            )
            return False

        data = ack.get("data")
        items: List[dict] = []
        if isinstance(data, list):
            items = [i for i in data if isinstance(i, dict)]
        elif isinstance(data, dict):
            sessions = data.get("sessions")
            if isinstance(sessions, list):
                items = [i for i in sessions if isinstance(i, dict)]

        for p in items:
            await self._handle_watch_payload(p, "session")

        return True

    async def _subscribe_panels(self, panel_ids: List[str]) -> bool:
        """订阅面板。"""
        if not self._auto_discover_panels and not panel_ids:
            return True

        ack = await self._socket_call("com.claw.im.subscribePanels", {
            "panelIds": panel_ids
        })

        if not ack.get("result"):
            logger.error(
                f"[{self.name}] subscribePanels failed: "
                f"{ack.get('message', 'unknown error')}"
            )
            return False

        return True

    async def _socket_call(
        self, event_name: str, payload: dict
    ) -> dict:
        """调用 Socket.IO 方法。"""
        if not self._socket:
            return {"result": False, "message": "socket not connected"}
        try:
            raw = await self._socket.call(event_name, payload, timeout=10)
        except Exception as e:
            return {"result": False, "message": str(e)}
        return raw if isinstance(raw, dict) else {"result": True, "data": raw}

    # ---- 刷新 / 发现 -------------------------------------------------------

    async def _refresh_loop(self) -> None:
        """定时刷新目标。"""
        interval_s = 60.0  # 每 60 秒刷新一次
        while self._running:
            await asyncio.sleep(interval_s)
            try:
                await self._refresh_targets(subscribe_new=self._ws_ready)
            except Exception as e:
                logger.warning(f"[{self.name}] Refresh failed: {e}")

            if self._fallback_mode:
                await self._ensure_fallback_workers()

    async def _refresh_targets(self, subscribe_new: bool) -> None:
        """刷新目标列表。"""
        if self._auto_discover_sessions:
            await self._refresh_sessions_directory(subscribe_new)
        if self._auto_discover_panels:
            await self._refresh_panels(subscribe_new)

    async def _refresh_sessions_directory(self, subscribe_new: bool) -> None:
        """刷新会话目录。"""
        try:
            response = await self._post_json("/api/claw/sessions/list", {})
        except Exception as e:
            logger.warning(f"[{self.name}] listSessions failed: {e}")
            return

        sessions = response.get("sessions")
        if not isinstance(sessions, list):
            return

        new_ids: List[str] = []
        for s in sessions:
            if not isinstance(s, dict):
                continue
            sid = _str_field(s, "sessionId")
            if not sid:
                continue
            if sid not in self._session_set:
                self._session_set.add(sid)
                new_ids.append(sid)
                if sid not in self._session_cursor:
                    self._cold_sessions.add(sid)
            cid = _str_field(s, "converseId")
            if cid:
                self._session_by_converse[cid] = sid

        if new_ids:
            if self._ws_ready and subscribe_new:
                await self._subscribe_sessions(new_ids)
            if self._fallback_mode:
                await self._ensure_fallback_workers()

    async def _refresh_panels(self, subscribe_new: bool) -> None:
        """刷新面板列表。"""
        try:
            response = await self._post_json("/api/claw/groups/get", {})
        except Exception as e:
            logger.warning(f"[{self.name}] getWorkspaceGroup failed: {e}")
            return

        raw_panels = response.get("panels")
        if not isinstance(raw_panels, list):
            return

        new_ids: List[str] = []
        for p in raw_panels:
            if not isinstance(p, dict):
                continue
            pid = _str_field(p, "id", "_id")
            if pid and pid not in self._panel_set:
                self._panel_set.add(pid)
                new_ids.append(pid)

        if new_ids:
            if self._ws_ready and subscribe_new:
                await self._subscribe_panels(new_ids)
            if self._fallback_mode:
                await self._ensure_fallback_workers()

    # ---- 回退工作线程 -------------------------------------------------------

    async def _ensure_fallback_workers(self) -> None:
        """确保回退工作线程运行。"""
        if not self._running:
            return
        self._fallback_mode = True

        for sid in sorted(self._session_set):
            t = self._session_fallback_tasks.get(sid)
            if not t or t.done():
                self._session_fallback_tasks[sid] = asyncio.create_task(
                    self._session_watch_worker(sid)
                )

        for pid in sorted(self._panel_set):
            t = self._panel_fallback_tasks.get(pid)
            if not t or t.done():
                self._panel_fallback_tasks[pid] = asyncio.create_task(
                    self._panel_poll_worker(pid)
                )

    async def _stop_fallback_workers(self) -> None:
        """停止回退工作线程。"""
        self._fallback_mode = False
        tasks = [
            *self._session_fallback_tasks.values(),
            *self._panel_fallback_tasks.values()
        ]
        for t in tasks:
            t.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._session_fallback_tasks.clear()
        self._panel_fallback_tasks.clear()

    async def _session_watch_worker(self, session_id: str) -> None:
        """会话轮询工作线程。"""
        while self._running and self._fallback_mode:
            try:
                payload = await self._post_json("/api/claw/sessions/watch", {
                    "sessionId": session_id,
                    "cursor": self._session_cursor.get(session_id, 0),
                    "timeoutMs": 30000,
                    "limit": 50,
                })
                await self._handle_watch_payload(payload, "session")
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.warning(
                    f"[{self.name}] Watch fallback error ({session_id}): {e}"
                )
                await asyncio.sleep(5)

    async def _panel_poll_worker(self, panel_id: str) -> None:
        """面板轮询工作线程。"""
        sleep_s = 60.0
        while self._running and self._fallback_mode:
            try:
                resp = await self._post_json(
                    "/api/claw/groups/panels/messages",
                    {"panelId": panel_id, "limit": 50}
                )
                msgs = resp.get("messages")
                if isinstance(msgs, list):
                    for m in reversed(msgs):
                        if not isinstance(m, dict):
                            continue
                        evt = {
                            "type": "message.add",
                            "payload": {
                                "messageId": str(m.get("messageId") or ""),
                                "author": str(m.get("author") or ""),
                                "content": m.get("content"),
                                "meta": m.get("meta"),
                                "groupId": str(resp.get("groupId") or ""),
                            }
                        }
                        await self._process_inbound_event(panel_id, evt, "panel")
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.warning(f"[{self.name}] Panel polling error ({panel_id}): {e}")
            await asyncio.sleep(sleep_s)

    # ---- 入站事件处理 -------------------------------------------------------

    async def _handle_watch_payload(
        self, payload: dict, target_kind: str
    ) -> None:
        """处理 watch payload。"""
        if not isinstance(payload, dict):
            return

        target_id = _str_field(payload, "sessionId")
        if not target_id:
            return

        lock = self._target_locks.setdefault(
            f"{target_kind}:{target_id}", asyncio.Lock()
        )
        async with lock:
            # 更新游标
            pc = payload.get("cursor")
            if target_kind == "session" and isinstance(pc, int) and pc >= 0:
                self._mark_session_cursor(target_id, pc)

            raw_events = payload.get("events")
            if not isinstance(raw_events, list):
                return

            # 处理冷启动
            if target_kind == "session" and target_id in self._cold_sessions:
                self._cold_sessions.discard(target_id)
                return

            for event in raw_events:
                if not isinstance(event, dict):
                    continue
                if event.get("type") == "message.add":
                    await self._process_inbound_event(target_id, event, target_kind)

    async def _process_inbound_event(
        self, target_id: str, event: dict, target_kind: str
    ) -> None:
        """处理入站事件。"""
        payload = event.get("payload")
        if not isinstance(payload, dict):
            return

        author = _str_field(payload, "author")
        if not author or (
            self.config.agent_user_id and author == self.config.agent_user_id
        ):
            return

        if not self.is_allowed(author):
            return

        # 去重
        message_id = _str_field(payload, "messageId")
        seen_key = f"{target_kind}:{target_id}"
        if message_id and self._remember_message_id(seen_key, message_id):
            return

        raw_body = normalize_mochat_content(payload.get("content")) or "[empty message]"
        ai = _safe_dict(payload.get("authorInfo"))
        sender_name = _str_field(ai, "nickname", "email")
        sender_username = _str_field(ai, "agentId")

        group_id = _str_field(payload, "groupId")
        is_group = bool(group_id)
        was_mentioned = resolve_was_mentioned(payload, self.config.agent_user_id)

        # 处理延迟发送
        use_delay = (
            target_kind == "panel" and
            self.config.reply_delay_mode == "non-mention"
        )

        entry = MochatBufferedEntry(
            raw_body=raw_body,
            author=author,
            sender_name=sender_name,
            sender_username=sender_username,
            timestamp=parse_timestamp(event.get("timestamp")),
            message_id=message_id,
            group_id=group_id,
        )

        if use_delay:
            delay_key = seen_key
            if was_mentioned:
                await self._flush_delayed_entries(
                    delay_key, target_id, target_kind, "mention", entry
                )
            else:
                await self._enqueue_delayed_entry(
                    delay_key, target_id, target_kind, entry
                )
            return

        await self._dispatch_entries(target_id, target_kind, [entry], was_mentioned)

    # ---- 去重 / 缓冲 -------------------------------------------------------

    def _remember_message_id(self, key: str, message_id: str) -> bool:
        """记录已见消息 ID。"""
        seen_set = self._seen_set.setdefault(key, set())
        seen_queue = self._seen_queue.setdefault(key, deque())

        if message_id in seen_set:
            return True

        seen_set.add(message_id)
        seen_queue.append(message_id)

        while len(seen_queue) > MAX_SEEN_MESSAGE_IDS:
            seen_set.discard(seen_queue.popleft())

        return False

    async def _enqueue_delayed_entry(
        self, key: str, target_id: str, target_kind: str,
        entry: MochatBufferedEntry
    ) -> None:
        """入队延迟条目。"""
        state = self._delay_states.setdefault(key, DelayState())
        async with state.lock:
            state.entries.append(entry)
            if state.timer:
                state.timer.cancel()
            state.timer = asyncio.create_task(
                self._delay_flush_after(key, target_id, target_kind)
            )

    async def _delay_flush_after(
        self, key: str, target_id: str, target_kind: str
    ) -> None:
        """延迟刷新。"""
        delay_ms = getattr(self.config, 'reply_delay_ms', 120000)
        await asyncio.sleep(max(0, delay_ms) / 1000.0)
        await self._flush_delayed_entries(key, target_id, target_kind, "timer", None)

    async def _flush_delayed_entries(
        self, key: str, target_id: str, target_kind: str,
        reason: str, entry: Optional[MochatBufferedEntry]
    ) -> None:
        """刷新延迟条目。"""
        state = self._delay_states.setdefault(key, DelayState())
        async with state.lock:
            if entry:
                state.entries.append(entry)
            current = asyncio.current_task()
            if state.timer and state.timer is not current:
                state.timer.cancel()
            state.timer = None
            entries = state.entries[:]
            state.entries.clear()

        if entries:
            await self._dispatch_entries(
                target_id, target_kind, entries, reason == "mention"
            )

    async def _dispatch_entries(
        self, target_id: str, target_kind: str,
        entries: List[MochatBufferedEntry], was_mentioned: bool
    ) -> None:
        """分发条目到消息总线。"""
        if not entries:
            return

        last = entries[-1]
        is_group = bool(last.group_id)
        body = build_buffered_body(entries, is_group) or "[empty message]"

        await self._handle_message(
            sender_id=last.author,
            chat_id=target_id,
            content=body,
            metadata={
                "message_id": last.message_id,
                "timestamp": last.timestamp,
                "is_group": is_group,
                "group_id": last.group_id,
                "was_mentioned": was_mentioned,
                "target_kind": target_kind,
            }
        )

    async def _cancel_delay_timers(self) -> None:
        """取消所有延迟计时器。"""
        for state in self._delay_states.values():
            if state.timer and not state.timer.done():
                state.timer.cancel()

    # ---- API 调用 ----------------------------------------------------------

    async def _api_send_session(
        self, session_id: str, content: str, reply_to: Optional[str]
    ) -> None:
        """发送会话消息。"""
        payload = {
            "sessionId": session_id,
            "content": content,
        }
        if reply_to:
            payload["replyTo"] = reply_to

        await self._post_json("/api/claw/sessions/send", payload)

    async def _api_send_panel(
        self, panel_id: str, content: str,
        reply_to: Optional[str], group_id: Optional[str]
    ) -> None:
        """发送面板消息。"""
        payload = {
            "panelId": panel_id,
            "content": content,
        }
        if reply_to:
            payload["replyTo"] = reply_to
        if group_id:
            payload["groupId"] = group_id

        await self._post_json("/api/claw/groups/panels/send", payload)

    async def _post_json(self, path: str, data: dict) -> dict:
        """发送 POST 请求。"""
        if not self._http:
            return {}

        url = f"{self.config.base_url.rstrip('/')}{path}"
        headers = {"Authorization": f"Bearer {self.config.claw_token}"}

        try:
            resp = await self._http.post(url, json=data, headers=headers)
            resp.raise_for_status()
            return resp.json()
        except Exception as e:
            logger.error(f"[{self.name}] API error {path}: {e}")
            return {}

    def _read_group_id(self, metadata: Optional[dict]) -> Optional[str]:
        """从 metadata 读取 group_id。"""
        if not metadata:
            return None
        return metadata.get("group_id")

    # ---- 游标持久化 --------------------------------------------------------

    def _mark_session_cursor(self, session_id: str, cursor: int) -> None:
        """标记会话游标。"""
        self._session_cursor[session_id] = cursor
        self._schedule_cursor_save()

    def _schedule_cursor_save(self) -> None:
        """调度游标保存。"""
        if self._cursor_save_task and not self._cursor_save_task.done():
            return
        self._cursor_save_task = asyncio.create_task(self._delayed_cursor_save())

    async def _delayed_cursor_save(self) -> None:
        """延迟保存游标。"""
        await asyncio.sleep(CURSOR_SAVE_DEBOUNCE_S)
        await self._save_session_cursors()

    async def _load_session_cursors(self) -> None:
        """加载会话游标。"""
        if not self._cursor_path.exists():
            return
        try:
            data = json.loads(self._cursor_path.read_text())
            if isinstance(data, dict):
                self._session_cursor = {k: int(v) for k, v in data.items()}
        except Exception as e:
            logger.warning(f"[{self.name}] Failed to load cursors: {e}")

    async def _save_session_cursors(self) -> None:
        """保存会话游标。"""
        try:
            self._cursor_path.write_text(
                json.dumps(self._session_cursor, ensure_ascii=False, indent=2)
            )
        except Exception as e:
            logger.warning(f"[{self.name}] Failed to save cursors: {e}")
