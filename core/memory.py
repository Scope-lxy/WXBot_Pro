"""Application-facing access to account-scoped SQLite chat history."""

from __future__ import annotations

import hashlib
import re
from datetime import datetime

from core.message_store import MessageStore


WINDOWS_RESERVED_NAMES = {
    "CON", "PRN", "AUX", "NUL",
    "COM1", "COM2", "COM3", "COM4", "COM5", "COM6", "COM7", "COM8", "COM9",
    "LPT1", "LPT2", "LPT3", "LPT4", "LPT5", "LPT6", "LPT7", "LPT8", "LPT9",
}
INVALID_FILENAME_CHARS_RE = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


def normalize_memory_chat_name(chat_name):
    return str(chat_name or "").strip()


def require_memory_chat_type(chat_type):
    value = str(chat_type or "").strip().lower()
    if value not in {"private", "group"}:
        raise ValueError("chat_type must be private or group")
    return value


def is_windows_reserved_storage_name(name):
    stem = str(name or "").split(".", 1)[0].upper()
    return stem in WINDOWS_RESERVED_NAMES


def hash_memory_storage_name(chat_name):
    raw_name = normalize_memory_chat_name(chat_name)
    return "hash" + hashlib.sha256(raw_name.encode("utf-8")).hexdigest()


def resolve_memory_storage_name(chat_name):
    raw_name = normalize_memory_chat_name(chat_name)
    if (
        not raw_name
        or raw_name in (".", "..")
        or raw_name.endswith(".")
        or len(raw_name) > 120
        or INVALID_FILENAME_CHARS_RE.search(raw_name)
        or is_windows_reserved_storage_name(raw_name)
    ):
        return hash_memory_storage_name(raw_name)
    return raw_name


class MemoryManager:
    """Read chat history and append explicitly repaired history gaps."""

    def __init__(self, wx_id, base_path, chat_name_resolver=None, message_store=None):
        self.wx_id = wx_id
        self.base_path = base_path
        self.chat_name_resolver = chat_name_resolver
        self.message_store = message_store or MessageStore(base_path, wx_id)

    def resolve_chat_name(self, chat_name, *, chat_type):
        chat_type = require_memory_chat_type(chat_type)
        if chat_type == "group":
            return normalize_memory_chat_name(chat_name)
        resolver = self.chat_name_resolver
        if callable(resolver):
            try:
                resolved = normalize_memory_chat_name(resolver(chat_name))
                if resolved:
                    return resolved
            except Exception:
                pass
        return normalize_memory_chat_name(chat_name)

    @staticmethod
    def _normalize_message_time(message_time=None):
        if isinstance(message_time, datetime):
            return message_time.strftime("%Y/%m/%d %H:%M:%S")
        if isinstance(message_time, (int, float)):
            try:
                return datetime.fromtimestamp(float(message_time)).strftime("%Y/%m/%d %H:%M:%S")
            except (OSError, OverflowError, ValueError):
                pass
        if isinstance(message_time, str):
            message_time = message_time.strip()
            if message_time:
                for fmt in ("%Y/%m/%d %H:%M:%S", "%Y-%m-%d %H:%M:%S"):
                    try:
                        return datetime.strptime(message_time, fmt).strftime("%Y/%m/%d %H:%M:%S")
                    except ValueError:
                        pass
                return message_time
        return datetime.now().strftime("%Y/%m/%d %H:%M:%S")

    def reconcile_visible_tail(
        self,
        chat_name,
        entries,
        *,
        current_event_ids,
        chat_type,
        history_limit=50,
    ):
        chat_type = require_memory_chat_type(chat_type)
        conversation = self.resolve_chat_name(chat_name, chat_type=chat_type)
        result = self.message_store.reconcile_visible_tail(
            conversation,
            entries,
            current_event_ids=current_event_ids,
            chat_type=chat_type,
            history_limit=history_limit,
        )
        result["history_messages"] = [
            self._history_message(event) for event in result.pop("events", [])
        ]
        return result

    def attach_visual_notes(self, chat_name, image_paths, visual_notes, *, chat_type):
        chat_type = require_memory_chat_type(chat_type)
        conversation = self.resolve_chat_name(chat_name, chat_type=chat_type)
        return self.message_store.attach_visual_notes(
            conversation,
            image_paths,
            visual_notes,
            chat_type=chat_type,
        )

    @staticmethod
    def _positive_count(value, *, fallback=0):
        try:
            value = int(value)
        except (TypeError, ValueError):
            return fallback
        return value if value > 0 else fallback

    @classmethod
    def _history_message(cls, event):
        metadata = event.get("metadata") if isinstance(event.get("metadata"), dict) else {}
        time_inferred = metadata.get("time_inferred") is True
        native_time = str(event.get("native_time", "") or "").strip()
        if time_inferred:
            message_time = ""
        elif native_time:
            message_time = cls._normalize_message_time(native_time)
        else:
            message_time = datetime.fromtimestamp(float(event["received_at"])).strftime("%Y/%m/%d %H:%M:%S")
        attr = str(event.get("native_attr", "") or "")
        if not attr:
            attr = "self" if event.get("direction") in {"manual_self", "bot_echo"} else str(event.get("direction", "") or "")
        item = {
            "event_id": str(event.get("event_id", "") or ""),
            "time": message_time,
            "type": str(event.get("message_type", "text") or "text"),
            "attr": attr,
            "sender": str(event.get("sender", "") or ""),
            "content": str(event.get("content", "") or ""),
        }
        for key in ("source", "image_paths", "visual_notes", "visual_note"):
            if key in metadata:
                item[key] = metadata[key]
        if time_inferred:
            item["time_inferred"] = True
        native_id = str(event.get("native_id", "") or "").strip()
        if native_id:
            item["message_id"] = native_id
        return item

    def get_messages(self, chat_name, count, *, chat_type):
        chat_type = require_memory_chat_type(chat_type)
        count = self._positive_count(count)
        if not count:
            return []
        events = self.message_store.history(
            self.resolve_chat_name(chat_name, chat_type=chat_type),
            count,
            chat_type=chat_type,
        )
        return [self._history_message(event) for event in events]

    def list_chat_names(self, *, chat_type):
        chat_type = require_memory_chat_type(chat_type)
        return self.message_store.list_conversations(chat_type=chat_type)

    def clear_messages(self, chat_name, *, chat_type):
        chat_type = require_memory_chat_type(chat_type)
        self.message_store.delete_conversation(
            self.resolve_chat_name(chat_name, chat_type=chat_type),
            chat_type=chat_type,
        )

    def clear_all_messages(self):
        count = sum(
            len(self.list_chat_names(chat_type=chat_type))
            for chat_type in ("private", "group")
        )
        self.message_store.clear_history()
        return count
