"""FastAPI-based web console for iflow-bot."""

from __future__ import annotations

import json
import os
import platform
import uuid
import asyncio
import logging
import re
from datetime import datetime
from collections import deque
from pathlib import Path
from typing import Any, Optional

from fastapi import FastAPI, Form, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from iflow_bot.config.loader import get_config_path
from iflow_bot.config.schema import Config
from iflow_bot.engine.adapter import IFlowAdapter
from iflow_bot.utils.helpers import get_channel_dir, get_home_dir

REFERENCE_MODELS = [
    "GLM-4.7",
    "GLM-5",
    "DeepSeek-V3.2",
    "iFlow-ROME-30BA3B(Preview)",
    "Qwen3-Coder-Plus",
    "Kimi-K2-Thinking",
    "MiniMax-M2.5",
    "MiniMax-M2.1",
    "Kimi-K2-0905",
    "Kimi-K2.5",
]

MODEL_ID_MAP: dict[str, str] = {
    "GLM-4.7": "glm-4.7",
    "GLM-5": "glm-5",
    "DeepSeek-V3.2": "deepseek-v3.2-chat",
    "iFlow-ROME-30BA3B(Preview)": "iFlow-ROME-30BA3B",
    "Qwen3-Coder-Plus": "qwen3-coder-plus",
    "Kimi-K2-Thinking": "kimi-k2-thinking",
    "MiniMax-M2.5": "minimax-m2.5",
    "MiniMax-M2.1": "minimax-m2.1",
    "Kimi-K2-0905": "kimi-k2-0905",
    "Kimi-K2.5": "kimi-k2.5",
}

MODEL_CONTEXT_SIZES: dict[str, int] = {
    "GLM-4.7": 200000,
    "GLM-5": 200000,
    "DeepSeek-V3.2": 128000,
    "iFlow-ROME-30BA3B(Preview)": 256000,
    "Qwen3-Coder-Plus": 256000,
    "Kimi-K2-Thinking": 256000,
    "MiniMax-M2.5": 128000,
    "MiniMax-M2.1": 128000,
    "Kimi-K2-0905": 256000,
    "Kimi-K2.5": 262144,
}

MODEL_LABEL_MAP: dict[str, str] = {model_id: label for label, model_id in MODEL_ID_MAP.items()}

RUNTIME_MODES = ["default", "yolo", "plan", "smart"]
CHANNEL_NAMES = ["telegram", "discord", "slack", "feishu", "dingtalk", "qq", "whatsapp", "email", "mochat"]
RUNTIME_CAPABILITY_LABELS: dict[str, str] = {
    "session/set_mode": "运行模式动态切换",
    "session/set_model": "模型动态切换",
    "session/set_think": "思考开关动态切换",
    "model_runtime_config": "运行时模型配置能力",
}


def _model_id_candidates(model_input: str) -> list[str]:
    model_input = (model_input or "").strip()
    if not model_input:
        return []
    mapped = MODEL_ID_MAP.get(model_input, model_input)
    if mapped == model_input:
        return [mapped]
    return [mapped, model_input]


def _display_model_name(model_input: str) -> str:
    model_input = (model_input or "").strip()
    if not model_input:
        return model_input
    return MODEL_LABEL_MAP.get(model_input, model_input)


def _extract_text_content(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, dict):
        if content.get("type") == "text":
            return str(content.get("text", ""))
        if "text" in content:
            return str(content.get("text", ""))
        try:
            return json.dumps(content, ensure_ascii=False)
        except Exception:
            return str(content)
    if isinstance(content, list):
        parts: list[str] = []
        for item in content:
            if isinstance(item, dict):
                if item.get("type") == "text":
                    parts.append(str(item.get("text", "")))
                elif "text" in item:
                    parts.append(str(item.get("text", "")))
                else:
                    try:
                        parts.append(json.dumps(item, ensure_ascii=False))
                    except Exception:
                        parts.append(str(item))
            elif isinstance(item, str):
                parts.append(item)
            else:
                parts.append(str(item))
        return "".join(parts)
    return ""


def _normalize_plan_entries(raw: Any) -> list[dict[str, str]]:
    candidates: list[Any] = []
    if isinstance(raw, list):
        candidates = raw
    elif isinstance(raw, dict):
        for key in ("entries", "plan", "items", "steps"):
            value = raw.get(key)
            if isinstance(value, list):
                candidates = value
                break
        if not candidates and raw.get("content"):
            candidates = [raw]

    result: list[dict[str, str]] = []
    for item in candidates:
        if isinstance(item, str):
            result.append({"content": item, "status": "pending", "priority": "normal"})
            continue
        if not isinstance(item, dict):
            continue
        content = str(item.get("content") or item.get("text") or item.get("title") or "").strip()
        if not content:
            continue
        result.append(
            {
                "content": content,
                "status": str(item.get("status", "pending")),
                "priority": str(item.get("priority", "normal")),
            }
        )
    return result


def _is_invalid_request_error(error: Exception) -> bool:
    text = str(error).lower()
    return "invalid request" in text


def _is_unsupported_method_error(error: Exception) -> bool:
    text = str(error).lower()
    return "method not found" in text or "未找到方法" in text


def _is_runtime_capability_error(error: Exception) -> bool:
    return _is_invalid_request_error(error) or _is_unsupported_method_error(error)


def _runtime_capability_hint(capability: str) -> str:
    return RUNTIME_CAPABILITY_LABELS.get(capability, capability)


def _process_exists(pid: int) -> bool:
    if platform.system().lower() == "windows":
        import subprocess

        try:
            result = subprocess.run(
                ["tasklist", "/FI", f"PID eq {pid}"],
                capture_output=True,
                text=True,
                timeout=3,
                shell=True,
            )
            return str(pid) in result.stdout
        except Exception:
            return False

    try:
        os.kill(pid, 0)
        return True
    except (ProcessLookupError, PermissionError):
        return False


def _read_json_file(path: Path) -> dict[str, Any]:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _safe_timestamp(value: str | None) -> str:
    if not value:
        return ""
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return dt.strftime("%Y-%m-%d %H:%M:%S")
    except Exception:
        return value


def _clean_history_text(text: str) -> str:
    cleaned = text or ""
    patterns = [
        r"<system-reminder>[\s\S]*?</system-reminder>",
        r"<history_context>[\s\S]*?</history_context>",
        r"\[message_source\][\s\S]*?\[/message_source\]",
        r"<Execution Info>[\s\S]*?</Execution Info>",
    ]
    for pattern in patterns:
        cleaned = re.sub(pattern, "", cleaned, flags=re.IGNORECASE)
    if "用户消息:" in cleaned:
        cleaned = cleaned.split("用户消息:")[-1]
    cleaned = cleaned.replace("</system-reminder>", "")
    lines = [line.rstrip() for line in cleaned.splitlines()]
    compact: list[str] = []
    blank_count = 0
    for line in lines:
        if not line.strip():
            blank_count += 1
            if blank_count <= 1:
                compact.append("")
            continue
        blank_count = 0
        compact.append(line)
    result = "\n".join(compact).strip()
    return result


def _flatten_dict(data: dict[str, Any], prefix: str = "") -> list[tuple[str, Any]]:
    items: list[tuple[str, Any]] = []
    for key, value in data.items():
        path = f"{prefix}.{key}" if prefix else key
        if isinstance(value, dict):
            items.extend(_flatten_dict(value, path))
        else:
            items.append((path, value))
    return items


def _set_nested_value(data: dict[str, Any], path: str, value: Any) -> None:
    parts = path.split(".")
    cur = data
    for part in parts[:-1]:
        nxt = cur.get(part)
        if not isinstance(nxt, dict):
            nxt = {}
            cur[part] = nxt
        cur = nxt
    cur[parts[-1]] = value


def _field_input_type(path: str, value: Any) -> str:
    key = path.split(".")[-1].lower()
    if isinstance(value, bool):
        return "bool"
    if isinstance(value, int) and not isinstance(value, bool):
        return "int"
    if isinstance(value, float):
        return "float"
    if isinstance(value, list):
        return "list"
    if any(token in key for token in ("secret", "token", "password", "key")):
        return "password"
    return "text"


def _field_label(path: str) -> str:
    parts = path.split(".")
    words = []
    for part in parts:
        words.append(part.replace("_", " ").strip())
    return " / ".join(words)


def _coerce_field_value(raw: str, sample: Any) -> Any:
    text = (raw or "").strip()
    if isinstance(sample, bool):
        return text.lower() in {"1", "true", "on", "yes"}
    if isinstance(sample, int) and not isinstance(sample, bool):
        return int(text or "0")
    if isinstance(sample, float):
        return float(text or "0")
    if isinstance(sample, list):
        return [line.strip() for line in (raw or "").replace("\r\n", "\n").split("\n") if line.strip()]
    return raw or ""


class ConsoleService:
    """Provides data for the web console."""

    def __init__(self) -> None:
        self.home_dir = get_home_dir()
        self.config_path = get_config_path()
        self.channel_dir = get_channel_dir()
        self.pid_file = self.home_dir / "gateway.pid"
        self.log_file = self.home_dir / "gateway.log"
        self._chat_adapter: Optional[IFlowAdapter] = None
        self._chat_lock = asyncio.Lock()
        self._web_chat_messages: dict[str, list[dict[str, str]]] = {}
        self._web_log_seq = 0
        self._web_logs: deque[tuple[int, str]] = deque(maxlen=3000)

    def add_web_log(self, line: str) -> None:
        self._web_log_seq += 1
        ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        self._web_logs.append((self._web_log_seq, f"{ts} | {line}"))

    def read_web_logs_tail(self, limit: int = 300, since: int = 0) -> tuple[list[str], int]:
        rows = [(seq, line) for (seq, line) in self._web_logs if seq > since]
        if not rows:
            return [], self._web_log_seq
        if len(rows) > limit:
            rows = rows[-limit:]
        return [line for _, line in rows], (rows[-1][0] if rows else self._web_log_seq)

    def read_gateway_logs_tail(self, limit: int = 300, since: int = 0) -> tuple[list[str], int]:
        if not self.log_file.exists():
            return [], 0
        try:
            lines = self.log_file.read_text(encoding="utf-8", errors="ignore").splitlines()
            total = len(lines)
            if since > 0:
                if since >= total:
                    return [], total
                new_lines = lines[since:]
                if len(new_lines) > limit:
                    new_lines = new_lines[-limit:]
                return new_lines, total
            return lines[-limit:], total
        except Exception:
            return [], 0

    def read_logs(self, source: str = "gateway", limit: int = 300, since: int = 0) -> tuple[list[str], int]:
        if source == "gateway":
            return self.read_gateway_logs_tail(limit=limit, since=since)
        if source == "web":
            return self.read_web_logs_tail(limit=limit, since=since)
        if source == "all":
            gateway_lines, gateway_cursor = self.read_gateway_logs_tail(limit=limit, since=since)
            web_lines, web_cursor = self.read_web_logs_tail(limit=limit, since=since)
            merged = gateway_lines + web_lines
            if len(merged) > limit:
                merged = merged[-limit:]
            return merged, max(gateway_cursor, web_cursor)
        return self.read_gateway_logs_tail(limit=limit, since=since)

    def get_gateway_status(self) -> dict[str, Any]:
        pid: Optional[int] = None
        running = False
        if self.pid_file.exists():
            try:
                pid = int(self.pid_file.read_text(encoding="utf-8").strip())
                running = _process_exists(pid)
            except Exception:
                pid = None
                running = False
        return {"running": running, "pid": pid}

    def list_conversations(self, channel: str = "", keyword: str = "") -> list[dict[str, Any]]:
        records: list[dict[str, Any]] = []
        if not self.channel_dir.exists():
            return records

        for channel_dir in sorted(self.channel_dir.iterdir()):
            if not channel_dir.is_dir():
                continue
            channel_name = channel_dir.name
            if channel and channel_name != channel:
                continue

            for json_file in sorted(channel_dir.glob("*.json"), reverse=True):
                data = _read_json_file(json_file)
                messages = data.get("messages") or []
                if not isinstance(messages, list):
                    messages = []

                last_msg = messages[-1] if messages else {}
                last_content = str(last_msg.get("content", "")).strip()
                last_content = (last_content[:120] + "...") if len(last_content) > 120 else last_content
                chat_id = str(last_msg.get("chat_id", "")) or data.get("chat_id", "") or json_file.stem

                record = {
                    "channel": channel_name,
                    "file_name": json_file.name,
                    "chat_id": chat_id,
                    "message_count": len(messages),
                    "last_timestamp": _safe_timestamp(last_msg.get("timestamp")),
                    "last_content": last_content or "(empty)",
                }
                if keyword:
                    haystack = f"{record['chat_id']} {record['last_content']}".lower()
                    if keyword.lower() not in haystack:
                        continue
                records.append(record)

        records.sort(key=lambda item: item["last_timestamp"], reverse=True)
        return records

    def get_conversation_detail(self, channel: str, file_name: str) -> dict[str, Any]:
        file_path = self.channel_dir / channel / file_name
        if not file_path.exists():
            raise FileNotFoundError("Conversation file not found")

        data = _read_json_file(file_path)
        messages = data.get("messages") or []
        normalized_messages: list[dict[str, Any]] = []
        for msg in messages:
            if not isinstance(msg, dict):
                continue
            normalized_messages.append(
                {
                    "timestamp": _safe_timestamp(msg.get("timestamp")),
                    "role": msg.get("role", "assistant"),
                    "direction": msg.get("direction", "outbound"),
                    "content": str(msg.get("content", "")),
                }
            )

        chat_id = ""
        if normalized_messages:
            raw = messages[-1] if isinstance(messages[-1], dict) else {}
            chat_id = str(raw.get("chat_id", ""))

        return {
            "channel": channel,
            "file_name": file_name,
            "chat_id": chat_id or data.get("chat_id", ""),
            "messages": normalized_messages,
        }

    def get_channels_summary(self) -> list[dict[str, Any]]:
        summary: list[dict[str, Any]] = []
        if not self.channel_dir.exists():
            return summary

        for channel_dir in sorted(self.channel_dir.iterdir()):
            if not channel_dir.is_dir():
                continue
            files = list(channel_dir.glob("*.json"))
            total_messages = 0
            for f in files:
                total_messages += len((_read_json_file(f).get("messages") or []))
            summary.append(
                {
                    "name": channel_dir.name,
                    "conversation_count": len(files),
                    "message_count": total_messages,
                }
            )
        return summary

    def read_config_text(self) -> str:
        if not self.config_path.exists():
            default = Config().model_dump()
            return json.dumps(default, indent=2, ensure_ascii=False)
        return self.config_path.read_text(encoding="utf-8")

    def get_config_obj(self) -> Config:
        if not self.config_path.exists():
            return Config()
        try:
            return Config(**json.loads(self.config_path.read_text(encoding="utf-8")))
        except Exception:
            return Config()

    def validate_and_save_config(self, content: str) -> tuple[bool, str]:
        try:
            raw = json.loads(content)
        except json.JSONDecodeError as e:
            return False, f"JSON 解析失败: {e}"

        try:
            config = Config(**raw)
        except Exception as e:
            return False, f"配置校验失败: {e}"

        pretty = json.dumps(config.model_dump(), indent=2, ensure_ascii=False)
        self.config_path.parent.mkdir(parents=True, exist_ok=True)
        self.config_path.write_text(pretty, encoding="utf-8")
        return True, "配置已保存"

    def set_channel_enabled(self, channel: str, enabled: bool) -> tuple[bool, str]:
        cfg = self.get_config_obj()
        if not hasattr(cfg.channels, channel):
            return False, f"未知渠道: {channel}"
        setattr(getattr(cfg.channels, channel), "enabled", enabled)
        pretty = json.dumps(cfg.model_dump(), indent=2, ensure_ascii=False)
        self.config_path.write_text(pretty, encoding="utf-8")
        return True, f"{channel} 已{'启用' if enabled else '禁用'}"

    def update_channel_config(self, channel: str, updates: dict[str, Any]) -> tuple[bool, str]:
        cfg = self.get_config_obj()
        channel_obj = getattr(cfg.channels, channel, None)
        if channel_obj is None:
            return False, f"未知渠道: {channel}"

        merged = channel_obj.model_dump()
        for path, value in updates.items():
            _set_nested_value(merged, path, value)
        try:
            validated = channel_obj.__class__(**merged)
        except Exception as e:
            return False, f"{channel} 配置校验失败: {e}"

        setattr(cfg.channels, channel, validated)
        pretty = json.dumps(cfg.model_dump(), indent=2, ensure_ascii=False)
        self.config_path.parent.mkdir(parents=True, exist_ok=True)
        self.config_path.write_text(pretty, encoding="utf-8")
        return True, f"{channel} 配置已更新"

    def get_channel_states(self, cfg: Config | None = None) -> list[dict[str, Any]]:
        cfg = cfg or self.get_config_obj()
        states: list[dict[str, Any]] = []
        for name in CHANNEL_NAMES:
            ch = getattr(cfg.channels, name, None)
            if ch is None:
                continue
            ch_dict = ch.model_dump()
            flat_fields = _flatten_dict(ch_dict)
            fields: list[dict[str, Any]] = []
            for path, value in flat_fields:
                input_type = _field_input_type(path, value)
                field_value = "\n".join([str(v) for v in value]) if isinstance(value, list) else str(value)
                fields.append(
                    {
                        "path": path,
                        "name": path.replace(".", "__"),
                        "label": _field_label(path),
                        "input_type": input_type,
                        "value": field_value,
                    }
                )
            states.append(
                {
                    "name": name,
                    "enabled": bool(ch_dict.get("enabled", False)),
                    "fields": fields,
                    "config_json": json.dumps(ch_dict, indent=2, ensure_ascii=False),
                }
            )
        states.sort(key=lambda item: (not item["enabled"], item["name"]))
        return states

    def read_log_tail(self, limit: int = 300) -> list[str]:
        if not self.log_file.exists():
            return []
        try:
            lines = self.log_file.read_text(encoding="utf-8", errors="ignore").splitlines()
            return lines[-limit:]
        except Exception:
            return []

    async def _get_chat_adapter(self) -> IFlowAdapter:
        if self._chat_adapter is not None:
            return self._chat_adapter
        async with self._chat_lock:
            if self._chat_adapter is not None:
                return self._chat_adapter
            cfg = self.get_config_obj()
            self._chat_adapter = IFlowAdapter(
                default_model=cfg.get_model(),
                workspace=cfg.get_workspace(),
                iflow_path=cfg.driver.iflow_path,
                mode=cfg.driver.mode,
                thinking=cfg.driver.thinking,
                timeout=cfg.driver.timeout,
                acp_host=cfg.driver.acp_host,
                acp_port=cfg.driver.acp_port,
            )
            return self._chat_adapter

    async def close_chat_adapter(self) -> None:
        if self._chat_adapter is not None:
            await self._chat_adapter.close()
            self._chat_adapter = None

    def get_web_chat_messages(self, session_id: str) -> list[dict[str, str]]:
        return self._web_chat_messages.get(session_id, [])

    def _conversation_key(self, channel: str, chat_id: str) -> str:
        return f"{channel}:{chat_id}"

    def _session_mapping_file(self) -> Path:
        return Path.home() / ".iflow-bot" / "session_mappings.json"

    def _chat_targets_meta_file(self) -> Path:
        return Path.home() / ".iflow-bot" / "chat_targets_meta.json"

    def _acp_session_file(self, session_id: str) -> Path:
        return Path.home() / ".iflow" / "acp" / "sessions" / f"{session_id}.json"

    def _read_session_mappings(self) -> dict[str, str]:
        path = self._session_mapping_file()
        if not path.exists():
            return {}
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                return {str(k): str(v) for k, v in data.items()}
        except Exception:
            return {}
        return {}

    def _write_session_mappings(self, mappings: dict[str, str]) -> None:
        path = self._session_mapping_file()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(mappings, ensure_ascii=False, indent=2), encoding="utf-8")

    def _read_chat_targets_meta(self) -> dict[str, dict[str, Any]]:
        path = self._chat_targets_meta_file()
        if not path.exists():
            return {}
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            return {}
        if not isinstance(payload, dict):
            return {}
        rows: dict[str, dict[str, Any]] = {}
        for key, value in payload.items():
            if not isinstance(value, dict):
                continue
            rows[str(key)] = {
                "pinned": bool(value.get("pinned", False)),
                "pinned_at": str(value.get("pinned_at", "")),
            }
        return rows

    def _write_chat_targets_meta(self, data: dict[str, dict[str, Any]]) -> None:
        path = self._chat_targets_meta_file()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")

    def set_chat_target_pinned(self, channel: str, chat_id: str, pinned: bool) -> None:
        key = self._conversation_key(channel, chat_id)
        meta = self._read_chat_targets_meta()
        if pinned:
            meta[key] = {"pinned": True, "pinned_at": datetime.now().isoformat()}
        else:
            if key in meta:
                meta.pop(key, None)
        self._write_chat_targets_meta(meta)

    def delete_chat_target(self, channel: str, chat_id: str) -> dict[str, Any]:
        key = self._conversation_key(channel, chat_id)
        mappings = self._read_session_mappings()
        session_id = mappings.pop(key, "")
        self._write_session_mappings(mappings)

        removed_session_file = False
        if session_id:
            session_file = self._acp_session_file(session_id)
            if session_file.exists():
                try:
                    session_file.unlink()
                    removed_session_file = True
                except Exception:
                    removed_session_file = False

        removed_recorder_files = 0
        channel_path = self.channel_dir / channel
        if channel_path.exists():
            for file_path in channel_path.glob(f"{chat_id}*.json"):
                try:
                    file_path.unlink()
                    removed_recorder_files += 1
                except Exception:
                    continue

        meta = self._read_chat_targets_meta()
        meta.pop(key, None)
        self._write_chat_targets_meta(meta)
        self._web_chat_messages.pop(key, None)

        return {
            "session_id": session_id,
            "removed_session_file": removed_session_file,
            "removed_recorder_files": removed_recorder_files,
        }

    def _extract_part_text(self, parts: Any) -> str:
        if not isinstance(parts, list):
            return ""
        rows: list[str] = []
        for part in parts:
            if isinstance(part, dict):
                text = str(part.get("text", "")).strip()
                if text:
                    rows.append(text)
        return _clean_history_text("\n".join(rows).strip())

    def _load_history_from_session_file(self, session_id: str, limit: int = 120) -> list[dict[str, str]]:
        session_file = self._acp_session_file(session_id)
        if not session_file.exists():
            return []
        try:
            payload = json.loads(session_file.read_text(encoding="utf-8"))
        except Exception:
            return []
        history = payload.get("chatHistory") or []
        if not isinstance(history, list):
            return []
        rows: list[dict[str, str]] = []
        for item in history[-limit:]:
            if not isinstance(item, dict):
                continue
            role = str(item.get("role", "")).lower()
            text = self._extract_part_text(item.get("parts"))
            if not text:
                continue
            if len(text) > 4000:
                text = text[:4000].rstrip() + "..."
            normalized_role = "user" if role == "user" else "assistant"
            rows.append({"role": normalized_role, "content": text})
        return rows

    def list_chat_targets(self, limit: int = 120) -> list[dict[str, Any]]:
        mappings = self._read_session_mappings()
        meta = self._read_chat_targets_meta()
        rows: list[dict[str, Any]] = []
        for key, session_id in mappings.items():
            if ":" not in key:
                continue
            channel, chat_id = key.split(":", 1)
            messages = self._load_history_from_session_file(session_id, limit=50)
            preview = ""
            if messages:
                preview = messages[-1]["content"].replace("\n", " ").strip()
            session_file = self._acp_session_file(session_id)
            updated_at = ""
            if session_file.exists():
                updated_at = datetime.fromtimestamp(session_file.stat().st_mtime).strftime("%Y-%m-%d %H:%M:%S")
            target_meta = meta.get(key, {})
            pinned = bool(target_meta.get("pinned", False))
            pinned_at = str(target_meta.get("pinned_at", ""))
            rows.append(
                {
                    "channel": channel,
                    "chat_id": chat_id,
                    "session_id": session_id,
                    "updated_at": updated_at,
                    "preview": (preview[:220] + "...") if len(preview) > 220 else (preview or "(empty)"),
                    "pinned": pinned,
                    "pinned_at": pinned_at,
                }
            )
        rows.sort(
            key=lambda item: (
                0 if item.get("pinned") else 1,
                item.get("pinned_at", ""),
                item.get("updated_at", ""),
            ),
            reverse=False,
        )
        pinned_rows = [item for item in rows if item.get("pinned")]
        normal_rows = [item for item in rows if not item.get("pinned")]
        pinned_rows.sort(key=lambda item: item.get("pinned_at", ""), reverse=True)
        normal_rows.sort(key=lambda item: item.get("updated_at", ""), reverse=True)
        rows = [*pinned_rows, *normal_rows]
        return rows[:limit]

    def _load_conversation_messages(self, channel: str, chat_id: str, limit: int = 120) -> list[dict[str, str]]:
        if not channel or not chat_id:
            return []
        mappings = self._read_session_mappings()
        session_id = mappings.get(f"{channel}:{chat_id}")
        if session_id:
            from_session = self._load_history_from_session_file(session_id, limit=limit)
            if from_session:
                return from_session
        channel_path = self.channel_dir / channel
        if not channel_path.exists():
            return []
        candidates = sorted(channel_path.glob(f"{chat_id}*.json"), reverse=True)
        if not candidates:
            return []
        data = _read_json_file(candidates[0])
        raw_messages = data.get("messages") or []
        if not isinstance(raw_messages, list):
            return []
        rows: list[dict[str, str]] = []
        for item in raw_messages[-limit:]:
            if not isinstance(item, dict):
                continue
            direction = str(item.get("direction", "")).lower()
            role = str(item.get("role", "")).lower()
            content = str(item.get("content", ""))
            if not content.strip():
                continue
            if direction == "inbound":
                normalized_role = "user"
            elif role == "user":
                normalized_role = "user"
            else:
                normalized_role = "assistant"
            rows.append({"role": normalized_role, "content": content})
        return rows

    def get_or_load_chat_messages(self, channel: str, chat_id: str) -> list[dict[str, str]]:
        key = self._conversation_key(channel, chat_id)
        if key not in self._web_chat_messages:
            self._web_chat_messages[key] = self._load_conversation_messages(channel, chat_id)
        return self._web_chat_messages[key]

    async def send_web_chat_message(self, session_id: str, message: str) -> str:
        adapter = await self._get_chat_adapter()
        self._web_chat_messages.setdefault(session_id, [])
        self._web_chat_messages[session_id].append({"role": "user", "content": message})
        reply = await adapter.chat(
            message=message,
            channel="web",
            chat_id=session_id,
        )
        self._web_chat_messages[session_id].append({"role": "assistant", "content": reply})
        return reply

    async def stream_web_chat(
        self,
        session_id: str,
        message: str,
        channel: str = "web",
        chat_id: str = "",
        model: str = "",
        think_enabled: bool = False,
        mode: str = "yolo",
    ):
        adapter = await self._get_chat_adapter()
        effective_channel = (channel or "web").strip()
        effective_chat_id = (chat_id or session_id).strip()
        conversation_key = self._conversation_key(effective_channel, effective_chat_id)
        self.get_or_load_chat_messages(effective_channel, effective_chat_id)
        selected_model = (model or adapter.default_model).strip()
        model_candidates = _model_id_candidates(selected_model)
        selected_model_id = model_candidates[0] if model_candidates else selected_model
        selected_mode = mode if mode in RUNTIME_MODES else "yolo"
        self._web_chat_messages.setdefault(conversation_key, [])
        self._web_chat_messages[conversation_key].append({"role": "user", "content": message})

        queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        assistant_parts: list[str] = []
        thought_parts: list[str] = []
        seen_first_chunk = False

        async def emit(event: str, data: dict[str, Any]) -> None:
            await queue.put({"event": event, "data": data})

        async def on_chunk(chunk: Any) -> None:
            nonlocal seen_first_chunk
            text = getattr(chunk, "text", "") or ""
            if not text:
                return
            if not seen_first_chunk and not getattr(chunk, "is_thought", False):
                seen_first_chunk = True
                await emit("phase", {"phase": "waiting_first_chunk", "status": "completed"})
                await emit("phase", {"phase": "streaming", "status": "in_progress"})
            if getattr(chunk, "is_thought", False):
                thought_parts.append(text)
                await emit("thought_chunk", {"text": text})
            else:
                assistant_parts.append(text)
                await emit("assistant_chunk", {"text": text})

        async def on_tool_call(tool_call: Any) -> None:
            await emit(
                "tool_call",
                {
                    "tool_call_id": getattr(tool_call, "tool_call_id", ""),
                    "tool_name": getattr(tool_call, "tool_name", ""),
                    "status": getattr(tool_call, "status", ""),
                    "args": getattr(tool_call, "args", {}) or {},
                    "output": getattr(tool_call, "output", "") or "",
                },
            )

        async def on_runtime_event(event: dict[str, Any]) -> None:
            update_type = str(event.get("update_type", "")).strip()
            update = event.get("update", {}) or {}
            if not update_type or update_type in {"agent_message_chunk", "agent_thought_chunk", "tool_call"}:
                return

            if update_type == "tool_call_update":
                await emit(
                    "tool_output",
                    {
                        "tool_call_id": update.get("toolCallId", ""),
                        "tool_name": update.get("name", ""),
                        "args": update.get("args", {}) or {},
                        "status": update.get("status", ""),
                        "content": _extract_text_content(update.get("content", "")),
                    },
                )
                return

            if update_type in {"plan", "todo", "task_plan", "session_plan"}:
                entries = _normalize_plan_entries(update)
                if entries:
                    await emit("plan", {"entries": entries})
                return

            if update_type in {"usage", "token_usage"}:
                usage = update.get("usage", update)
                if isinstance(usage, dict):
                    await emit(
                        "usage",
                        {
                            "prompt_tokens": usage.get("promptTokens", usage.get("prompt_tokens")),
                            "completion_tokens": usage.get("completionTokens", usage.get("completion_tokens")),
                            "total_tokens": usage.get("totalTokens", usage.get("total_tokens")),
                        },
                    )
                return

            if update_type in {"file_ref", "file_reference"}:
                await emit(
                    "file_ref",
                    {
                        "path": str(update.get("path", "")),
                        "line_start": update.get("lineStart", update.get("line_start")),
                        "line_end": update.get("lineEnd", update.get("line_end")),
                    },
                )
                return

            if update_type in {"warning", "agent_warning"}:
                await emit("warning", {"message": str(update.get("message", ""))})
                return

            if update_type in {"error", "agent_error"}:
                await emit("warning", {"message": str(update.get("message", "")) or "runtime error chunk"})
                return

            if update_type in {"tool_confirmation", "user_question", "plan_approval"}:
                await emit(
                    "approval",
                    {
                        "update_type": update_type,
                        "payload": update,
                    },
                )
                return

            if update_type in {"available_commands_update"}:
                return

            inferred_entries = _normalize_plan_entries(update)
            if inferred_entries:
                await emit("plan", {"entries": inferred_entries})
                return

            await emit(
                "raw_event",
                {
                    "update_type": update_type,
                    "payload": update,
                },
            )

        async def runner() -> None:
            try:
                await emit(
                    "status",
                    {
                        "mode": adapter.mode,
                        "model": selected_model_id,
                        "think": think_enabled,
                        "runtime_mode": selected_mode,
                        "message": f"mode={adapter.mode}, model={selected_model_id}, think={think_enabled}, runtime={selected_mode}",
                    },
                )
                self.add_web_log(
                    f"chat.start sid={session_id} channel={effective_channel} chat_id={effective_chat_id} mode={adapter.mode} runtime={selected_mode} model={selected_model_id} think={think_enabled}"
                )
                await emit("phase", {"phase": "preparing", "status": "completed"})
                await emit("phase", {"phase": "runtime_config", "status": "in_progress"})

                if adapter.mode == "stdio":
                    low = await adapter._get_stdio_adapter()
                    runtime_session_id = await low._get_or_create_session(effective_channel, effective_chat_id, selected_model_id)
                    runtime_result = await self._apply_runtime_settings(
                        low._client,
                        runtime_session_id,
                        model_candidates,
                        think_enabled,
                        selected_mode,
                    )
                    await emit("runtime_update", runtime_result)
                    await emit("phase", {"phase": "runtime_config", "status": "completed"})
                    await emit("phase", {"phase": "waiting_first_chunk", "status": "in_progress"})
                    final_content = await low.chat_stream(
                        message=message,
                        channel=effective_channel,
                        chat_id=effective_chat_id,
                        model=selected_model_id,
                        timeout=adapter.timeout,
                        on_chunk=on_chunk,
                        on_tool_call=on_tool_call,
                        on_event=on_runtime_event,
                    )
                elif adapter.mode == "acp":
                    low = await adapter._get_acp_adapter()
                    runtime_session_id = await low._get_or_create_session(effective_channel, effective_chat_id, selected_model_id)
                    runtime_result = await self._apply_runtime_settings(
                        low._client,
                        runtime_session_id,
                        model_candidates,
                        think_enabled,
                        selected_mode,
                    )
                    await emit("runtime_update", runtime_result)
                    await emit("phase", {"phase": "runtime_config", "status": "completed"})
                    await emit("phase", {"phase": "waiting_first_chunk", "status": "in_progress"})
                    final_content = await low.chat_stream(
                        message=message,
                        channel=effective_channel,
                        chat_id=effective_chat_id,
                        model=selected_model_id,
                        timeout=adapter.timeout,
                        on_chunk=on_chunk,
                        on_tool_call=on_tool_call,
                        on_event=on_runtime_event,
                    )
                else:
                    adapter.thinking = think_enabled
                    await emit("phase", {"phase": "runtime_config", "status": "completed"})
                    await emit("phase", {"phase": "streaming", "status": "in_progress"})
                    final_content = await adapter.chat(
                        message=message,
                        channel=effective_channel,
                        chat_id=effective_chat_id,
                        model=selected_model_id,
                    )
                    assistant_parts.append(final_content)
                    await emit("assistant_chunk", {"text": final_content})

                merged = "".join(assistant_parts).strip() or final_content
                self._web_chat_messages[conversation_key].append({"role": "assistant", "content": merged})
                await emit("phase", {"phase": "streaming", "status": "completed"})
                await emit("phase", {"phase": "finalizing", "status": "in_progress"})
                await emit("done", {"content": merged, "thought": "".join(thought_parts)})
                self.add_web_log(
                    f"chat.done sid={session_id} channel={effective_channel} chat_id={effective_chat_id} content_len={len(merged)} thought_len={len(''.join(thought_parts))}"
                )
                await emit("phase", {"phase": "finalizing", "status": "completed"})
            except Exception as e:
                self.add_web_log(f"chat.error sid={session_id} channel={effective_channel} chat_id={effective_chat_id} err={e}")
                await emit("phase", {"phase": "failed", "status": "failed"})
                await emit("error", {"message": str(e)})
            finally:
                await emit("_end", {})

        task = asyncio.create_task(runner())
        try:
            while True:
                event = await queue.get()
                if event["event"] == "_end":
                    break
                yield event
        finally:
            if not task.done():
                task.cancel()

    async def _apply_runtime_settings(
        self,
        client: Any,
        session_id: str,
        model_candidates: list[str],
        think_enabled: bool,
        mode: str,
    ) -> dict[str, Any]:
        result: dict[str, Any] = {
            "ok": True,
            "warnings": [],
            "unsupported": [],
            "unsupported_details": [],
            "model_applied": "",
        }
        if client is None:
            return result
        send_request = getattr(client, "_send_request", None)
        if not callable(send_request):
            return result

        unsupported: set[str] = set()
        unsupported_details: dict[str, str] = {}

        try:
            await send_request("session/set_mode", {"sessionId": session_id, "modeId": mode})
        except Exception as e:
            if _is_runtime_capability_error(e):
                unsupported.add("session/set_mode")
                unsupported_details.setdefault("session/set_mode", str(e))
            else:
                result["ok"] = False
                result["warnings"].append(f"session/set_mode 失败: {e}")

        model_set = False
        for model_id in model_candidates:
            try:
                await send_request("session/set_model", {"sessionId": session_id, "modelId": model_id})
                model_set = True
                result["model_applied"] = model_id
                break
            except Exception as e:
                if _is_runtime_capability_error(e):
                    try:
                        await send_request(
                            "session/set_config_option",
                            {
                                "sessionId": session_id,
                                "configId": "model",
                                "value": model_id,
                            },
                        )
                        model_set = True
                        result["model_applied"] = model_id
                        break
                    except Exception as e2:
                        if _is_runtime_capability_error(e2):
                            unsupported.add("session/set_model")
                            unsupported_details.setdefault("session/set_model", str(e2))
                        else:
                            result["warnings"].append(f"session/set_model({model_id}) 失败: {e2}")
                else:
                    result["warnings"].append(f"session/set_model({model_id}) 失败: {e}")
        if not model_set:
            unsupported.add("model_runtime_config")
            unsupported_details.setdefault("model_runtime_config", "未发现可用的运行时模型配置接口")

        think_payload: dict[str, Any] = {"sessionId": session_id, "thinkEnabled": think_enabled}
        if think_enabled:
            think_payload["thinkConfig"] = "think"
        try:
            await send_request("session/set_think", think_payload)
        except Exception as e:
            if _is_runtime_capability_error(e):
                unsupported.add("session/set_think")
                unsupported_details.setdefault("session/set_think", str(e))
            else:
                result["ok"] = False
                result["warnings"].append(f"session/set_think 失败: {e}")

        result["unsupported"] = sorted(unsupported)
        result["unsupported_details"] = [
            {
                "key": key,
                "label": _runtime_capability_hint(key),
                "reason": unsupported_details.get(key, ""),
            }
            for key in sorted(unsupported)
        ]
        if result["warnings"]:
            result["ok"] = False
        return result

    async def reset_web_chat(self, session_id: str) -> None:
        self._web_chat_messages[session_id] = []
        adapter = await self._get_chat_adapter()
        adapter.session_mappings.clear_session("web", session_id)


def create_app(token: str | None = None) -> FastAPI:
    service = ConsoleService()
    app = FastAPI(title="iflow-bot Console", version="1.0.0")

    base_dir = Path(__file__).parent
    templates = Jinja2Templates(directory=str(base_dir / "templates"))
    app.mount("/static", StaticFiles(directory=str(base_dir / "static")), name="static")

    class _WebConsoleLogHandler(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            try:
                message = self.format(record)
                service.add_web_log(message)
            except Exception:
                pass

    log_handler = _WebConsoleLogHandler()
    log_handler.setLevel(logging.DEBUG)
    log_handler.setFormatter(logging.Formatter("%(name)s | %(levelname)s | %(message)s"))
    for logger_name in ("uvicorn", "uvicorn.error", "uvicorn.access", "iflow_bot"):
        logging.getLogger(logger_name).addHandler(log_handler)

    @app.middleware("http")
    async def _request_logger(request: Request, call_next):
        start = datetime.now()
        try:
            response = await call_next(request)
            elapsed_ms = int((datetime.now() - start).total_seconds() * 1000)
            service.add_web_log(f"{request.method} {request.url.path} -> {response.status_code} ({elapsed_ms}ms)")
            return response
        except Exception as e:
            elapsed_ms = int((datetime.now() - start).total_seconds() * 1000)
            service.add_web_log(f"{request.method} {request.url.path} -> 500 ({elapsed_ms}ms) err={e}")
            raise

    @app.on_event("shutdown")
    async def _shutdown() -> None:
        for logger_name in ("uvicorn", "uvicorn.error", "uvicorn.access", "iflow_bot"):
            logger = logging.getLogger(logger_name)
            if log_handler in logger.handlers:
                logger.removeHandler(log_handler)
        await service.close_chat_adapter()

    def _check_token(request: Request) -> None:
        if not token:
            return
        supplied = (
            request.headers.get("x-iflow-console-token")
            or request.query_params.get("token")
            or request.cookies.get("iflow_console_token")
        )
        if supplied != token:
            raise HTTPException(status_code=401, detail="Unauthorized")

    def _resolve_web_session_id(request: Request) -> str:
        sid = request.cookies.get("iflow_web_session")
        if sid:
            return sid
        return uuid.uuid4().hex[:16]

    @app.get("/", response_class=HTMLResponse)
    async def dashboard(request: Request) -> HTMLResponse:
        _check_token(request)
        gateway = service.get_gateway_status()
        channels = service.get_channels_summary()
        logs = service.read_log_tail(limit=120)
        errors = [line for line in logs if "ERROR" in line or "Traceback" in line][-20:]

        conversation_total = sum(item["conversation_count"] for item in channels)
        message_total = sum(item["message_count"] for item in channels)
        return templates.TemplateResponse(
            "dashboard.html",
            {
                "request": request,
                "page": "dashboard",
                "gateway": gateway,
                "channels": channels,
                "conversation_total": conversation_total,
                "message_total": message_total,
                "errors": errors,
                "token": token or "",
            },
        )

    @app.get("/conversations", response_class=HTMLResponse)
    async def conversations(
        request: Request,
        channel: str = Query(default=""),
        keyword: str = Query(default=""),
    ) -> HTMLResponse:
        _check_token(request)
        items = service.list_conversations(channel=channel, keyword=keyword)
        channel_names = sorted({i["channel"] for i in service.list_conversations()})
        return templates.TemplateResponse(
            "conversations.html",
            {
                "request": request,
                "page": "conversations",
                "items": items,
                "channel": channel,
                "keyword": keyword,
                "channel_names": channel_names,
                "token": token or "",
            },
        )

    @app.get("/chat", response_class=HTMLResponse)
    async def web_chat(request: Request) -> HTMLResponse:
        _check_token(request)
        session_id = _resolve_web_session_id(request)
        cfg = service.get_config_obj()
        default_model_raw = cfg.get_model()
        default_model = _display_model_name(default_model_raw)
        model_options = []
        for m in [default_model, *REFERENCE_MODELS]:
            if m not in model_options:
                model_options.append(m)
        enabled_channels = cfg.get_enabled_channels()
        if "web" not in enabled_channels:
            enabled_channels = ["web", *enabled_channels]
        chat_targets = service.list_chat_targets(limit=200)

        response = templates.TemplateResponse(
            "chat.html",
            {
                "request": request,
                "page": "chat",
                "token": token or "",
                "session_id": session_id,
                "messages": service.get_or_load_chat_messages("web", session_id),
                "default_model": default_model,
                "default_model_raw": default_model_raw,
                "driver_mode": cfg.driver.mode,
                "default_runtime_mode": "yolo" if cfg.driver.yolo else "default",
                "default_think_enabled": cfg.driver.thinking,
                "model_options": model_options,
                "model_context_sizes": MODEL_CONTEXT_SIZES,
                "runtime_modes": RUNTIME_MODES,
                "enabled_channels": enabled_channels,
                "chat_targets": chat_targets,
            },
        )
        response.set_cookie("iflow_web_session", session_id, httponly=False, max_age=86400 * 30)
        return response

    @app.post("/api/chat/send")
    async def web_chat_send(
        request: Request,
        message: str = Form(...),
        session_id: str = Form(...),
    ) -> JSONResponse:
        _check_token(request)
        text = message.strip()
        if not text:
            return JSONResponse({"ok": False, "message": "消息不能为空"}, status_code=400)
        try:
            reply = await service.send_web_chat_message(session_id, text)
            return JSONResponse(
                {
                    "ok": True,
                    "reply": reply,
                    "messages": service.get_web_chat_messages(session_id),
                }
            )
        except Exception as e:
            return JSONResponse({"ok": False, "message": str(e)}, status_code=500)

    @app.post("/api/chat/stream")
    async def web_chat_stream(
        request: Request,
        message: str = Form(...),
        session_id: str = Form(...),
        channel: str = Form(default="web"),
        chat_id: str = Form(default=""),
        model: str = Form(default=""),
        think_enabled: int = Form(default=0),
        runtime_mode: str = Form(default="yolo"),
    ) -> StreamingResponse:
        _check_token(request)
        text = message.strip()
        if not text:
            raise HTTPException(status_code=400, detail="消息不能为空")

        async def event_gen():
            async for event in service.stream_web_chat(
                session_id=session_id,
                message=text,
                channel=channel,
                chat_id=chat_id,
                model=model,
                think_enabled=bool(think_enabled),
                mode=runtime_mode,
            ):
                payload = json.dumps(event["data"], ensure_ascii=False)
                yield f"event: {event['event']}\ndata: {payload}\n\n"

        return StreamingResponse(
            event_gen(),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "Connection": "keep-alive",
                "X-Accel-Buffering": "no",
            },
        )

    @app.post("/api/chat/reset")
    async def web_chat_reset(
        request: Request,
        session_id: str = Form(...),
        channel: str = Form(default="web"),
        chat_id: str = Form(default=""),
    ) -> JSONResponse:
        _check_token(request)
        effective_channel = (channel or "web").strip()
        effective_chat_id = (chat_id or session_id).strip()
        key = f"{effective_channel}:{effective_chat_id}"
        service._web_chat_messages[key] = []
        adapter = await service._get_chat_adapter()
        adapter.session_mappings.clear_session(effective_channel, effective_chat_id)
        return JSONResponse({"ok": True})

    @app.get("/api/chat/history")
    async def web_chat_history(
        request: Request,
        channel: str = Query(default="web"),
        chat_id: str = Query(default=""),
        session_id: str = Query(default=""),
    ) -> JSONResponse:
        _check_token(request)
        sid = (session_id or "").strip() or _resolve_web_session_id(request)
        effective_channel = (channel or "web").strip()
        effective_chat_id = (chat_id or sid).strip()
        messages = service.get_or_load_chat_messages(effective_channel, effective_chat_id)
        return JSONResponse(
            {
                "ok": True,
                "channel": effective_channel,
                "chat_id": effective_chat_id,
                "messages": messages,
            }
        )

    @app.post("/api/chat/target/pin")
    async def web_chat_target_pin(
        request: Request,
        channel: str = Form(...),
        chat_id: str = Form(...),
        pinned: int = Form(default=1),
    ) -> JSONResponse:
        _check_token(request)
        effective_channel = (channel or "").strip()
        effective_chat_id = (chat_id or "").strip()
        if not effective_channel or not effective_chat_id:
            return JSONResponse({"ok": False, "message": "channel/chat_id 不能为空"}, status_code=400)
        service.set_chat_target_pinned(effective_channel, effective_chat_id, bool(pinned))
        return JSONResponse({"ok": True, "pinned": bool(pinned)})

    @app.post("/api/chat/target/delete")
    async def web_chat_target_delete(
        request: Request,
        channel: str = Form(...),
        chat_id: str = Form(...),
    ) -> JSONResponse:
        _check_token(request)
        effective_channel = (channel or "").strip()
        effective_chat_id = (chat_id or "").strip()
        if not effective_channel or not effective_chat_id:
            return JSONResponse({"ok": False, "message": "channel/chat_id 不能为空"}, status_code=400)
        deleted = service.delete_chat_target(effective_channel, effective_chat_id)
        adapter = await service._get_chat_adapter()
        adapter.session_mappings.clear_session(effective_channel, effective_chat_id)
        return JSONResponse({"ok": True, "deleted": deleted})

    @app.get("/conversations/{channel}/{file_name}", response_class=HTMLResponse)
    async def conversation_detail(request: Request, channel: str, file_name: str) -> HTMLResponse:
        _check_token(request)
        try:
            detail = service.get_conversation_detail(channel, file_name)
        except FileNotFoundError:
            raise HTTPException(status_code=404, detail="Conversation not found")
        return templates.TemplateResponse(
            "conversation_detail.html",
            {
                "request": request,
                "page": "conversations",
                "detail": detail,
                "token": token or "",
            },
        )

    @app.get("/config", response_class=HTMLResponse)
    async def config_page(request: Request, saved: str = "") -> HTMLResponse:
        _check_token(request)
        cfg = service.get_config_obj()
        channel_states = service.get_channel_states(cfg)
        enabled_count = len([c for c in channel_states if c["enabled"]])
        return templates.TemplateResponse(
            "config.html",
            {
                "request": request,
                "page": "config",
                "config_text": service.read_config_text(),
                "saved": saved,
                "channel_states": channel_states,
                "enabled_count": enabled_count,
                "channel_total": len(channel_states),
                "driver_mode": cfg.driver.mode,
                "driver_model": cfg.driver.model,
                "driver_timeout": cfg.driver.timeout,
                "driver_think": cfg.driver.thinking,
                "driver_yolo": cfg.driver.yolo,
                "config_path": str(service.config_path),
                "token": token or "",
            },
        )

    @app.post("/config/save")
    async def config_save(request: Request, content: str = Form(...)) -> HTMLResponse:
        _check_token(request)
        ok, message = service.validate_and_save_config(content)
        if ok:
            return RedirectResponse(url="/config?saved=1", status_code=303)
        cfg = service.get_config_obj()
        channel_states = service.get_channel_states(cfg)
        return templates.TemplateResponse(
            "config.html",
            {
                "request": request,
                "page": "config",
                "config_text": content,
                "error": message,
                "channel_states": channel_states,
                "enabled_count": len([c for c in channel_states if c["enabled"]]),
                "channel_total": len(channel_states),
                "driver_mode": cfg.driver.mode,
                "driver_model": cfg.driver.model,
                "driver_timeout": cfg.driver.timeout,
                "driver_think": cfg.driver.thinking,
                "driver_yolo": cfg.driver.yolo,
                "config_path": str(service.config_path),
                "token": token or "",
            },
            status_code=400,
        )

    @app.post("/api/config/validate")
    async def config_validate(request: Request, content: str = Form(...)) -> JSONResponse:
        _check_token(request)
        try:
            raw = json.loads(content)
            Config(**raw)
            return JSONResponse({"ok": True, "message": "配置校验通过"})
        except json.JSONDecodeError as e:
            return JSONResponse({"ok": False, "message": f"JSON 解析失败: {e}"}, status_code=400)
        except Exception as e:
            return JSONResponse({"ok": False, "message": f"配置校验失败: {e}"}, status_code=400)

    @app.post("/config/toggle/{channel_name}")
    async def config_toggle(
        request: Request,
        channel_name: str,
        enabled: int = Form(...),
    ) -> RedirectResponse:
        _check_token(request)
        service.set_channel_enabled(channel_name, bool(enabled))
        url = "/config?saved=1"
        if token:
            url += f"&token={token}"
        return RedirectResponse(url=url, status_code=303)

    @app.post("/config/channel/{channel_name}")
    async def config_channel_save(
        request: Request,
        channel_name: str,
    ) -> HTMLResponse:
        _check_token(request)
        cfg = service.get_config_obj()
        channel_obj = getattr(cfg.channels, channel_name, None)
        if channel_obj is None:
            raise HTTPException(status_code=404, detail=f"未知渠道: {channel_name}")

        existing = channel_obj.model_dump()
        flat_fields = _flatten_dict(existing)
        field_index = {path.replace(".", "__"): (path, sample) for path, sample in flat_fields}

        form = await request.form()
        updates: dict[str, Any] = {}
        for form_name, raw_value in form.multi_items():
            if form_name not in field_index:
                continue
            path, sample = field_index[form_name]
            try:
                updates[path] = _coerce_field_value(str(raw_value), sample)
            except Exception as e:
                channel_states = service.get_channel_states(cfg)
                return templates.TemplateResponse(
                    "config.html",
                    {
                        "request": request,
                        "page": "config",
                        "config_text": service.read_config_text(),
                        "saved": "",
                        "error": f"{channel_name} 字段 {path} 值无效: {e}",
                        "channel_states": channel_states,
                        "enabled_count": len([c for c in channel_states if c["enabled"]]),
                        "channel_total": len(channel_states),
                        "driver_mode": cfg.driver.mode,
                        "driver_model": cfg.driver.model,
                        "driver_timeout": cfg.driver.timeout,
                        "driver_think": cfg.driver.thinking,
                        "driver_yolo": cfg.driver.yolo,
                        "config_path": str(service.config_path),
                        "token": token or "",
                    },
                    status_code=400,
                )

        ok, message = service.update_channel_config(channel_name, updates)
        if ok:
            url = f"/config?saved={channel_name}"
            if token:
                url += f"&token={token}"
            return RedirectResponse(url=url, status_code=303)
        channel_states = service.get_channel_states(cfg)
        return templates.TemplateResponse(
            "config.html",
            {
                "request": request,
                "page": "config",
                "config_text": service.read_config_text(),
                "saved": "",
                "error": message,
                "channel_states": channel_states,
                "enabled_count": len([c for c in channel_states if c["enabled"]]),
                "channel_total": len(channel_states),
                "driver_mode": cfg.driver.mode,
                "driver_model": cfg.driver.model,
                "driver_timeout": cfg.driver.timeout,
                "driver_think": cfg.driver.thinking,
                "driver_yolo": cfg.driver.yolo,
                "config_path": str(service.config_path),
                "token": token or "",
            },
            status_code=400,
        )

    @app.get("/logs", response_class=HTMLResponse)
    async def logs_page(
        request: Request,
        keyword: str = Query(default=""),
        source: str = Query(default="gateway"),
        auto: int = Query(default=1),
    ) -> HTMLResponse:
        _check_token(request)
        lines, cursor = service.read_logs(source=source, limit=600, since=0)
        if keyword:
            lines = [line for line in lines if keyword.lower() in line.lower()]
        return templates.TemplateResponse(
            "logs.html",
            {
                "request": request,
                "page": "logs",
                "lines": lines[-400:],
                "keyword": keyword,
                "source": source,
                "auto": bool(auto),
                "cursor": cursor,
                "token": token or "",
            },
        )

    @app.get("/api/logs/tail")
    async def api_logs_tail(
        request: Request,
        source: str = Query(default="gateway"),
        keyword: str = Query(default=""),
        since: int = Query(default=0),
        limit: int = Query(default=200),
    ) -> JSONResponse:
        _check_token(request)
        safe_limit = max(20, min(limit, 800))
        lines, cursor = service.read_logs(source=source, limit=safe_limit, since=since)
        if keyword:
            lines = [line for line in lines if keyword.lower() in line.lower()]
        return JSONResponse({"ok": True, "lines": lines, "cursor": cursor, "source": source})

    return app


def run_console(host: str = "127.0.0.1", port: int = 8787, token: str | None = None) -> None:
    import uvicorn

    app = create_app(token=token)
    uvicorn.run(app, host=host, port=port, log_level="info")
