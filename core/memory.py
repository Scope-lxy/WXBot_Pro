"""Compatibility facade for account-scoped SQLite chat history."""

from __future__ import annotations

import hashlib
import json
import os
import re
import time
import uuid
from datetime import datetime
from pathlib import Path

from core.account_storage import account_area_dir
from core.memory_context_repair import build_repair_plan, unique_message_key
from core.message_pipeline import split_quoted_image_message
from core.message_store import MessageStore


WINDOWS_RESERVED_NAMES = {
    "CON", "PRN", "AUX", "NUL",
    "COM1", "COM2", "COM3", "COM4", "COM5", "COM6", "COM7", "COM8", "COM9",
    "LPT1", "LPT2", "LPT3", "LPT4", "LPT5", "LPT6", "LPT7", "LPT8", "LPT9",
}
INVALID_FILENAME_CHARS_RE = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
LEGACY_MEMORY_MIGRATION_KEY = "legacy-memory-json-v1"


def normalize_memory_chat_name(chat_name):
    return str(chat_name or "").strip()


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


def read_memory_original_name(chat_path, fallback=""):
    name_path = os.path.join(chat_path, "name.json")
    if not os.path.exists(name_path):
        return fallback
    try:
        with open(name_path, "r", encoding="utf-8") as file:
            data = json.load(file)
        name = str(data.get("name", "") if isinstance(data, dict) else "").strip()
        return name or fallback
    except Exception:
        return fallback


def find_memory_chat_dir(base_path, wx_id, chat_name):
    raw_name = normalize_memory_chat_name(chat_name)
    storage_name = resolve_memory_storage_name(raw_name)
    wx_path = account_area_dir(base_path, wx_id, "memory")
    return storage_name, os.path.join(wx_path, storage_name)


class MemoryManager:
    """Keep the legacy memory API while SQLite owns all message facts."""

    def __init__(self, wx_id, base_path, chat_name_resolver=None, message_store=None):
        self.wx_id = wx_id
        self.base_path = base_path
        self.chat_name_resolver = chat_name_resolver
        self.message_store = message_store or MessageStore(base_path, wx_id)
        self._migrate_legacy_json_once()

    def resolve_chat_name(self, chat_name):
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

    @staticmethod
    def _parse_message_time(message_time):
        if not message_time:
            return None
        for fmt in ("%Y/%m/%d %H:%M:%S", "%Y-%m-%d %H:%M:%S"):
            try:
                return datetime.strptime(str(message_time), fmt)
            except ValueError:
                pass
        return None

    @classmethod
    def _received_at(cls, message_time, fallback=None):
        parsed = cls._parse_message_time(message_time)
        return parsed.timestamp() if parsed else float(time.time() if fallback is None else fallback)

    @staticmethod
    def _message_image_paths(entry):
        entry = entry if isinstance(entry, dict) else {}
        image_paths = entry.get("image_paths")
        if isinstance(image_paths, list):
            paths = [str(path or "").strip() for path in image_paths if str(path or "").strip()]
            if paths:
                return paths
        raw = str(entry.get("content", "") or "").strip()
        if str(entry.get("type", "") or "").strip().lower() == "image":
            return [raw] if raw and raw != "[图片]" else []
        if not raw:
            return []
        _text, paths = split_quoted_image_message(raw)
        return [str(path).strip() for path in paths if str(path or "").strip()]

    @classmethod
    def _normalize_entry(cls, entry):
        entry = entry if isinstance(entry, dict) else {}
        msg_type = str(entry.get("type", "text") or "text").strip() or "text"
        raw_content = str(entry.get("content", "") or "")
        image_paths = cls._message_image_paths(entry)
        content = "[图片]" if msg_type.lower() == "image" else raw_content
        normalized = {
            "time": cls._normalize_message_time(entry.get("time")),
            "type": msg_type,
            "attr": str(entry.get("attr", "") or ""),
            "sender": str(entry.get("sender", "") or ""),
            "content": content,
        }
        source = str(entry.get("source", "") or "").strip()
        if source:
            normalized["source"] = source
        if image_paths:
            normalized["image_paths"] = image_paths
        raw_notes = entry.get("visual_notes")
        raw_notes = raw_notes if isinstance(raw_notes, list) else []
        raw_notes = [str(note or "").strip() for note in raw_notes]
        if image_paths:
            notes = [
                raw_notes[index] if index < len(raw_notes) else ""
                for index in range(len(image_paths))
            ]
        else:
            notes = [note for note in raw_notes if note]
        fallback_note = str(entry.get("visual_note", "") or "").strip()
        if fallback_note and not any(notes):
            notes = [fallback_note]
        if any(notes):
            normalized["visual_notes"] = notes
            normalized["visual_note"] = next(note for note in notes if note)
        return normalized

    @staticmethod
    def _direction(attr):
        attr = str(attr or "").strip().lower()
        if attr == "self":
            return "manual_self"
        if attr in {"friend", "system"}:
            return attr
        return "unknown"

    def _store_entry(self, conversation, entry, *, chat_type, event_id, fallback_time=None):
        metadata = {
            key: entry[key]
            for key in ("source", "image_paths", "visual_notes", "visual_note")
            if key in entry
        }
        return {
            "event_id": event_id,
            "conversation": conversation,
            "chat_type": str(chat_type or "private"),
            "direction": self._direction(entry.get("attr")),
            "sender": entry.get("sender", ""),
            "content": entry.get("content", ""),
            "original_content": entry.get("content", ""),
            "message_type": entry.get("type", "text"),
            "native_attr": entry.get("attr", ""),
            "native_time": entry.get("time", ""),
            "received_at": self._received_at(entry.get("time"), fallback_time),
            "metadata": metadata,
        }

    @staticmethod
    def _event_id(prefix, value=None):
        if value is None:
            return f"evt_{prefix}_{uuid.uuid4().hex}"
        digest = hashlib.sha256(str(value).encode("utf-8")).hexdigest()
        return f"evt_{prefix}_{digest}"

    def _legacy_memory_files(self):
        base = Path(account_area_dir(self.base_path, self.wx_id, "memory"))
        if not base.is_dir():
            return []
        result = []
        directories = (path for path in base.iterdir() if path.is_dir())
        for directory in sorted(directories, key=lambda path: path.name):
            preferred = directory / f"{directory.name}_memory.json"
            candidates = sorted(directory.glob("*_memory.json"))
            path = preferred if preferred.is_file() else (candidates[0] if candidates else None)
            if path is not None:
                result.append((read_memory_original_name(str(directory), directory.name), path))
        return result

    def _migrate_legacy_json_once(self):
        if self.message_store.migration_completed(LEGACY_MEMORY_MIGRATION_KEY):
            return
        imported = []
        fallback = time.time()
        sequence = 0
        for raw_conversation, path in self._legacy_memory_files():
            conversation = self.resolve_chat_name(raw_conversation)
            if not conversation:
                continue
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, UnicodeError, json.JSONDecodeError):
                continue
            if not isinstance(payload, list):
                continue
            for index, raw_entry in enumerate(payload):
                if not isinstance(raw_entry, dict):
                    continue
                entry = self._normalize_entry(raw_entry)
                identity = json.dumps(
                    [conversation, path.name, index, entry],
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                )
                imported.append(
                    self._store_entry(
                        conversation,
                        entry,
                        chat_type="private",
                        event_id=self._event_id("legacy", identity),
                        fallback_time=fallback + sequence / 1000000,
                    )
                )
                sequence += 1
        self.message_store.import_history_once(
            LEGACY_MEMORY_MIGRATION_KEY,
            imported,
            now=fallback,
        )

    def save_message(
        self,
        chat_name,
        sender,
        content,
        msg_type,
        msg_attr,
        max_count,
        message_time=None,
        image_paths=None,
        visual_notes=None,
    ):
        del max_count
        conversation = self.resolve_chat_name(chat_name)
        entry = self._normalize_entry(
            {
                "time": message_time,
                "type": msg_type,
                "attr": msg_attr,
                "sender": sender,
                "content": content,
                "image_paths": image_paths or [],
                "visual_notes": visual_notes or [],
            }
        )
        self.message_store.import_history_once(
            None,
            [
                self._store_entry(
                    conversation,
                    entry,
                    chat_type="private",
                    event_id=self._event_id("memory"),
                )
            ],
        )

    def append_missing_messages(
        self,
        chat_name,
        entries,
        max_count,
        *,
        reconcile_visible_snapshot=False,
        require_anchor=False,
        chat_type="private",
        anchor_recent_count=5,
    ):
        conversation = self.resolve_chat_name(chat_name)
        normalized_entries = []
        for entry in entries or []:
            if not isinstance(entry, dict):
                continue
            normalized = self._normalize_entry(entry)
            if unique_message_key(normalized):
                normalized_entries.append(normalized)

        history_limit = self._positive_count(max_count, fallback=5000)
        messages = self.get_messages(conversation, history_limit)
        if not normalized_entries:
            return {"added": 0, "total": len(messages)}

        repair_plan = None
        if reconcile_visible_snapshot:
            repair_plan = build_repair_plan(
                messages,
                normalized_entries,
                anchor_recent_count=anchor_recent_count,
                chat_type=chat_type,
            )
            normalized_entries = (
                repair_plan.messages_to_append
                if repair_plan.anchor_found or not require_anchor
                else []
            )

        existing_keys = {
            unique_message_key(item)
            for item in messages
            if isinstance(item, dict) and unique_message_key(item)
        }
        indexed = list(enumerate(normalized_entries))
        indexed.sort(
            key=lambda pair: (
                self._parse_message_time(pair[1].get("time")) or datetime.max,
                pair[0],
            )
        )
        to_append = []
        for _index, entry in indexed:
            key = unique_message_key(entry)
            if not key or (not reconcile_visible_snapshot and key in existing_keys):
                continue
            if not reconcile_visible_snapshot:
                existing_keys.add(key)
            to_append.append(
                self._store_entry(
                    conversation,
                    entry,
                    chat_type=chat_type,
                    event_id=self._event_id("repair"),
                )
            )
        added = self.message_store.import_history_once(None, to_append)
        result = {
            "added": added,
            "total": len(self.get_messages(conversation, history_limit)),
        }
        if repair_plan is not None:
            result.update(
                anchor_found=repair_plan.anchor_found,
                anchor_index=repair_plan.anchor_index,
            )
        return result

    def attach_visual_notes(self, chat_name, image_paths, visual_notes):
        conversation = self.resolve_chat_name(chat_name)
        return self.message_store.attach_visual_notes(conversation, image_paths, visual_notes)

    @staticmethod
    def _positive_count(value, *, fallback=0):
        try:
            value = int(value)
        except (TypeError, ValueError):
            return fallback
        return value if value > 0 else fallback

    @classmethod
    def _legacy_message(cls, event):
        metadata = event.get("metadata") if isinstance(event.get("metadata"), dict) else {}
        native_time = str(event.get("native_time", "") or "").strip()
        if native_time:
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
        return item

    def get_messages(self, chat_name, count):
        count = self._positive_count(count)
        if not count:
            return []
        events = self.message_store.history(
            self.resolve_chat_name(chat_name),
            count,
            chat_type=None,
        )
        return [self._legacy_message(event) for event in events]

    def list_chat_names(self):
        return self.message_store.list_conversations()

    def clear_messages(self, chat_name):
        self.message_store.delete_conversation(self.resolve_chat_name(chat_name))

    def clear_all_messages(self):
        count = len(self.list_chat_names())
        self.message_store.clear_history()
        return count
