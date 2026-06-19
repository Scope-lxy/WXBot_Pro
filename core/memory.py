"""Chat memory persistence helpers."""

import json
import hashlib
import os
import re
import threading
from datetime import datetime

from core.account_storage import account_area_dir
from core.message_pipeline import split_quoted_image_message


WINDOWS_RESERVED_NAMES = {
    "CON", "PRN", "AUX", "NUL",
    "COM1", "COM2", "COM3", "COM4", "COM5", "COM6", "COM7", "COM8", "COM9",
    "LPT1", "LPT2", "LPT3", "LPT4", "LPT5", "LPT6", "LPT7", "LPT8", "LPT9",
}
INVALID_FILENAME_CHARS_RE = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


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
    """
    Store chat messages per window and provide recent context for AI requests.
    Storage path: {base_path}/accounts/{wx_id}/memory/{storage_name}/{storage_name}_memory.json
    """

    def __init__(self, wx_id, base_path, chat_name_resolver=None):
        self.wx_id = wx_id
        self.base_path = base_path
        self.chat_name_resolver = chat_name_resolver
        self._locks = {}

    def resolve_chat_name(self, chat_name):
        resolver = getattr(self, "chat_name_resolver", None)
        if callable(resolver):
            try:
                resolved = normalize_memory_chat_name(resolver(chat_name))
                if resolved:
                    return resolved
            except Exception:
                pass
        return normalize_memory_chat_name(chat_name)

    def _get_lock(self, chat_name):
        key = self.resolve_chat_name(chat_name)
        if key not in self._locks:
            self._locks[key] = threading.Lock()
        return self._locks[key]

    def _write_original_name(self, dir_path, chat_name):
        name_path = os.path.join(dir_path, "name.json")
        raw_name = normalize_memory_chat_name(chat_name)
        try:
            current = read_memory_original_name(dir_path, "")
            if current == raw_name:
                return
            with open(name_path, "w", encoding="utf-8") as file:
                json.dump({"name": raw_name}, file, ensure_ascii=False, indent=2)
        except Exception:
            pass

    def _get_memory_path(self, chat_name, create=True):
        chat_name = self.resolve_chat_name(chat_name)
        storage_name = resolve_memory_storage_name(chat_name)
        dir_path = os.path.join(account_area_dir(self.base_path, self.wx_id, "memory", create=create), storage_name)
        if not create:
            found_storage_name, found_dir_path = find_memory_chat_dir(self.base_path, self.wx_id, chat_name)
            return os.path.join(found_dir_path, f"{found_storage_name}_memory.json")
        os.makedirs(dir_path, exist_ok=True)
        self._write_original_name(dir_path, chat_name)
        return os.path.join(dir_path, f"{storage_name}_memory.json")

    def _find_existing_memory_file(self, chat_name):
        chat_name = self.resolve_chat_name(chat_name)
        storage_name, dir_path = find_memory_chat_dir(self.base_path, self.wx_id, chat_name)
        preferred = os.path.join(dir_path, f"{storage_name}_memory.json")
        if os.path.exists(preferred):
            return preferred
        try:
            for filename in os.listdir(dir_path):
                if filename.endswith("_memory.json"):
                    return os.path.join(dir_path, filename)
        except OSError:
            pass
        return preferred

    @staticmethod
    def _normalize_message_time(message_time=None):
        if isinstance(message_time, datetime):
            return message_time.strftime("%Y/%m/%d %H:%M:%S")
        if isinstance(message_time, str):
            message_time = message_time.strip()
            if message_time:
                return message_time
        return datetime.now().strftime("%Y/%m/%d %H:%M:%S")

    @staticmethod
    def _parse_message_time(message_time):
        if not message_time:
            return None
        try:
            return datetime.strptime(str(message_time), "%Y/%m/%d %H:%M:%S")
        except Exception:
            return None

    def _append_message_in_order(self, messages, entry, recent_count=5):
        current_dt = self._parse_message_time(entry.get("time"))
        if current_dt is None or not messages:
            messages.append(entry)
            return messages

        recent_start = max(0, len(messages) - recent_count)
        recent_messages = messages[recent_start:]
        has_later_recent = False
        for item in recent_messages:
            item_dt = self._parse_message_time(item.get("time"))
            if item_dt and item_dt > current_dt:
                has_later_recent = True
                break

        if not has_later_recent:
            messages.append(entry)
            return messages

        sortable_recent = []
        for idx, item in enumerate(recent_messages):
            item_dt = self._parse_message_time(item.get("time")) or datetime.max
            sortable_recent.append((item_dt, idx, item))
        sortable_recent.append((current_dt, len(recent_messages), entry))
        sortable_recent.sort(key=lambda item: (item[0], item[1]))
        messages[recent_start:] = [item for _, _, item in sortable_recent]
        return messages

    def save_message(self, chat_name, sender, content, msg_type, msg_attr, max_count, message_time=None):
        path = self._get_memory_path(chat_name)
        entry = {
            "time": self._normalize_message_time(message_time),
            "type": str(msg_type),
            "attr": str(msg_attr),
            "sender": str(sender),
            "content": str(content),
        }
        with self._get_lock(chat_name):
            if os.path.exists(path):
                try:
                    with open(path, "r", encoding="utf-8") as file:
                        messages = json.load(file)
                    if not isinstance(messages, list):
                        messages = []
                except Exception:
                    messages = []
            else:
                messages = []
            messages = self._append_message_in_order(messages, entry, recent_count=5)
            if len(messages) > max_count:
                messages = messages[-max_count:]
            with open(path, "w", encoding="utf-8") as file:
                json.dump(messages, file, ensure_ascii=False, indent=2)

    @staticmethod
    def _message_image_paths(entry):
        entry = entry if isinstance(entry, dict) else {}
        msg_type = str(entry.get("type", "") or "").strip().lower()
        raw = str(entry.get("content", "") or "").strip()
        if msg_type == "image":
            return [raw] if raw else []
        if not raw:
            return []
        _text_part, image_paths = split_quoted_image_message(raw)
        return [path for path in image_paths if str(path or "").strip()]

    def attach_visual_notes(self, chat_name, image_paths, visual_notes):
        normalized_paths = [str(path or "").strip() for path in (image_paths or []) if str(path or "").strip()]
        normalized_notes = [str(note or "").strip() for note in (visual_notes or [])]
        if not normalized_paths or not any(normalized_notes):
            return False
        note_by_path = {
            path: normalized_notes[index]
            for index, path in enumerate(normalized_paths)
            if index < len(normalized_notes) and normalized_notes[index]
        }
        if not note_by_path:
            return False
        path = self._find_existing_memory_file(chat_name)
        if not os.path.exists(path):
            return False
        with self._get_lock(chat_name):
            try:
                with open(path, "r", encoding="utf-8") as file:
                    messages = json.load(file)
                if not isinstance(messages, list):
                    return False
            except Exception:
                return False

            updated = False
            matched_paths = set()
            for entry in reversed(messages):
                entry_paths = self._message_image_paths(entry)
                if not entry_paths:
                    continue
                entry_notes = list(entry.get("visual_notes") or [])
                merged_notes = []
                changed = False
                for index, entry_path in enumerate(entry_paths):
                    existing = str(entry_notes[index] or "").strip() if index < len(entry_notes) else ""
                    note = note_by_path.get(entry_path, existing)
                    if note:
                        matched_paths.add(entry_path)
                    merged_notes.append(note)
                    if note != existing:
                        changed = True
                if not any(str(note or "").strip() for note in merged_notes):
                    continue
                primary_note = next((note for note in merged_notes if str(note or "").strip()), "")
                if entry.get("visual_note") != primary_note:
                    entry["visual_note"] = primary_note
                    changed = True
                if entry.get("visual_notes") != merged_notes:
                    entry["visual_notes"] = merged_notes
                    changed = True
                if changed:
                    updated = True
                if matched_paths >= set(note_by_path):
                    break
            if not updated:
                return False
            with open(path, "w", encoding="utf-8") as file:
                json.dump(messages, file, ensure_ascii=False, indent=2)
            return True

    def get_messages(self, chat_name, count):
        path = self._find_existing_memory_file(chat_name)
        if not os.path.exists(path):
            return []
        try:
            count = int(count)
        except (TypeError, ValueError):
            count = 0
        if count <= 0:
            return []
        try:
            with open(path, "r", encoding="utf-8") as file:
                messages = json.load(file)
            if isinstance(messages, list):
                return messages[-count:]
        except Exception:
            pass
        return []

    def list_chat_names(self):
        base = account_area_dir(self.base_path, self.wx_id, "memory")
        if not os.path.isdir(base):
            return []
        names = []
        for chat_dir in os.listdir(base):
            chat_path = os.path.join(base, chat_dir)
            if not os.path.isdir(chat_path):
                continue
            try:
                has_memory_file = any(filename.endswith("_memory.json") for filename in os.listdir(chat_path))
            except OSError:
                has_memory_file = False
            if has_memory_file:
                names.append(read_memory_original_name(chat_path, chat_dir))
        return sorted(set(name for name in names if str(name or "").strip()))

    def clear_messages(self, chat_name):
        path = self._find_existing_memory_file(chat_name)
        if not os.path.exists(path):
            path = self._get_memory_path(chat_name)
        with self._get_lock(chat_name):
            try:
                with open(path, "w", encoding="utf-8") as file:
                    json.dump([], file, ensure_ascii=False)
            except Exception:
                pass

    def clear_all_messages(self):
        count = 0
        base = account_area_dir(self.base_path, self.wx_id, "memory")
        if not os.path.exists(base):
            return count
        for chat_dir in os.listdir(base):
            memory_file = os.path.join(base, chat_dir, f"{chat_dir}_memory.json")
            if os.path.exists(memory_file):
                try:
                    with open(memory_file, "w", encoding="utf-8") as file:
                        json.dump([], file, ensure_ascii=False)
                    count += 1
                except Exception:
                    pass
        return count
