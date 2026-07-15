"""Application-facing access to account-scoped SQLite chat history."""

from __future__ import annotations

import hashlib
import re
import time
from datetime import datetime

from core.memory_context_repair import build_repair_plan, unique_message_key
from core.message_pipeline import split_quoted_image_message
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
        message_id = str(entry.get("message_id", "") or "").strip()
        if message_id:
            normalized["message_id"] = message_id
        if entry.get("time_inferred") is True:
            normalized["time_inferred"] = True
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
        time_inferred = entry.get("time_inferred") is True
        if time_inferred:
            metadata["time_inferred"] = True
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
            "native_time": "" if time_inferred else entry.get("time", ""),
            "received_at": self._received_at(entry.get("time"), fallback_time),
            "metadata": metadata,
        }

    @staticmethod
    def _event_id(prefix, value=None):
        if value is None:
            raise ValueError("deterministic event identity is required")
        digest = hashlib.sha256(str(value).encode("utf-8")).hexdigest()
        return f"evt_{prefix}_{digest}"

    def append_missing_messages(
        self,
        chat_name,
        entries,
        max_count,
        *,
        reconcile_visible_snapshot=False,
        require_anchor=False,
        chat_type,
        anchor_recent_count=5,
        not_after=None,
    ):
        chat_type = require_memory_chat_type(chat_type)
        conversation = self.resolve_chat_name(chat_name, chat_type=chat_type)
        normalized_entries = []
        for entry in entries or []:
            if not isinstance(entry, dict):
                continue
            normalized = self._normalize_entry(entry)
            if unique_message_key(normalized):
                normalized_entries.append(normalized)

        if reconcile_visible_snapshot:
            snapshot_occurrences = {}
            for entry in normalized_entries:
                key = unique_message_key(entry)
                entry["_repair_occurrence"] = snapshot_occurrences.get(key, 0)
                snapshot_occurrences[key] = entry["_repair_occurrence"] + 1

        if not_after is not None:
            boundary = self._parse_message_time(self._normalize_message_time(not_after))
            if boundary is None:
                raise ValueError("context repair boundary time is invalid")
            for entry in normalized_entries:
                message_time = self._parse_message_time(entry.get("time"))
                if message_time is None or message_time > boundary:
                    raise ValueError("context repair message exceeds current inbound boundary")

        history_limit = self._positive_count(max_count, fallback=5000)
        messages = self.get_messages(conversation, history_limit, chat_type=chat_type)
        deletion_boundary = (
            self.message_store.latest_history_deletion_at(
                conversation,
                chat_type=chat_type,
            )
            if reconcile_visible_snapshot
            else None
        )
        if not normalized_entries:
            return {"added": 0, "total": len(messages), "deleted_boundary_skipped": 0}

        repair_plan = None
        deleted_boundary_skipped = 0
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
            if deletion_boundary is not None:
                kept_entries = []
                for entry in normalized_entries:
                    parsed_time = self._parse_message_time(entry.get("time"))
                    if (
                        entry.get("time_inferred") is True
                        or parsed_time is None
                        or parsed_time.timestamp() <= deletion_boundary
                    ):
                        deleted_boundary_skipped += 1
                        continue
                    kept_entries.append(entry)
                normalized_entries = kept_entries

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
        occurrence_counts = {}
        for _index, entry in indexed:
            key = unique_message_key(entry)
            if not key or (not reconcile_visible_snapshot and key in existing_keys):
                continue
            if not reconcile_visible_snapshot:
                existing_keys.add(key)
            occurrence = entry.get("_repair_occurrence")
            if occurrence is None:
                occurrence = occurrence_counts.get(key, 0)
                occurrence_counts[key] = occurrence + 1
            repair_identity = "\x1f".join((
                conversation,
                chat_type,
                key,
                str(occurrence),
            ))
            to_append.append(
                self._store_entry(
                    conversation,
                    entry,
                    chat_type=chat_type,
                    event_id=self._event_id("repair", repair_identity),
                )
            )
        added = self.message_store.append_history(to_append)
        result = {
            "added": added,
            "total": len(self.get_messages(conversation, history_limit, chat_type=chat_type)),
            "deleted_boundary_skipped": deleted_boundary_skipped,
        }
        if repair_plan is not None:
            result.update(
                anchor_found=repair_plan.anchor_found,
                anchor_index=repair_plan.anchor_index,
            )
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
