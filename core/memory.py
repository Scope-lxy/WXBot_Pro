"""Chat memory persistence helpers."""

import json
import hashlib
import os
import re
import tempfile
import threading
from datetime import datetime

from core.account_storage import account_area_dir
from core.message_pipeline import split_quoted_image_message
from core.memory_context_repair import build_repair_plan, unique_message_key


WINDOWS_RESERVED_NAMES = {
    "CON", "PRN", "AUX", "NUL",
    "COM1", "COM2", "COM3", "COM4", "COM5", "COM6", "COM7", "COM8", "COM9",
    "LPT1", "LPT2", "LPT3", "LPT4", "LPT5", "LPT6", "LPT7", "LPT8", "LPT9",
}
INVALID_FILENAME_CHARS_RE = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


def _atomic_write_json(path, value, *, indent=2):
    directory = os.path.dirname(path)
    os.makedirs(directory, exist_ok=True)
    fd, temp_path = tempfile.mkstemp(prefix=f".{os.path.basename(path)}.", suffix=".tmp", dir=directory)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as file:
            json.dump(value, file, ensure_ascii=False, indent=indent)
            file.flush()
            os.fsync(file.fileno())
        os.replace(temp_path, path)
    finally:
        if os.path.exists(temp_path):
            os.unlink(temp_path)


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
        self._locks_guard = threading.Lock()

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
        with self._locks_guard:
            lock = self._locks.get(key)
            if lock is None:
                lock = threading.Lock()
                self._locks[key] = lock
            return lock

    def _write_original_name(self, dir_path, chat_name):
        name_path = os.path.join(dir_path, "name.json")
        raw_name = normalize_memory_chat_name(chat_name)
        try:
            current = read_memory_original_name(dir_path, "")
            if current == raw_name:
                return
            _atomic_write_json(name_path, {"name": raw_name})
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
        path = self._get_memory_path(chat_name)
        normalized_msg_type = str(msg_type).strip().lower()
        normalized_image_paths = [
            str(path or "").strip()
            for path in (image_paths or [])
            if str(path or "").strip()
        ]
        raw_visual_notes = [str(note or "").strip() for note in (visual_notes or [])]
        if normalized_image_paths:
            normalized_visual_notes = [
                raw_visual_notes[index] if index < len(raw_visual_notes) else ""
                for index, _path in enumerate(normalized_image_paths)
            ]
        else:
            normalized_visual_notes = [note for note in raw_visual_notes if note]
        normalized_content = str(content)
        if normalized_msg_type == "image":
            normalized_content = "[图片]"
        entry = {
            "time": self._normalize_message_time(message_time),
            "type": str(msg_type),
            "attr": str(msg_attr),
            "sender": str(sender),
            "content": normalized_content,
        }
        if normalized_image_paths:
            entry["image_paths"] = normalized_image_paths
        if any(normalized_visual_notes):
            entry["visual_notes"] = normalized_visual_notes
            entry["visual_note"] = next((note for note in normalized_visual_notes if note), "")
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
            _atomic_write_json(path, messages)

    def append_missing_messages(
        self,
        chat_name,
        entries,
        max_count,
        *,
        reconcile_visible_snapshot=False,
        chat_type="private",
        anchor_recent_count=5,
    ):
        path = self._get_memory_path(chat_name)
        normalized_entries = []
        for entry in entries or []:
            if not isinstance(entry, dict):
                continue
            msg_type = str(entry.get("type", "text") or "text").strip() or "text"
            content = str(entry.get("content", "") or "")
            if msg_type.lower() == "image":
                content = "[图片]"
            normalized = {
                "time": self._normalize_message_time(entry.get("time")),
                "type": msg_type,
                "attr": str(entry.get("attr", "") or ""),
                "sender": str(entry.get("sender", "") or ""),
                "content": content,
            }
            source = str(entry.get("source", "") or "").strip()
            if source:
                normalized["source"] = source
            if bool(entry.get("time_inferred")):
                normalized["time_inferred"] = True
            if isinstance(entry.get("image_paths"), list):
                image_paths = [str(path or "").strip() for path in entry.get("image_paths") if str(path or "").strip()]
                if image_paths:
                    normalized["image_paths"] = image_paths
            if isinstance(entry.get("visual_notes"), list):
                visual_notes = [str(note or "").strip() for note in entry.get("visual_notes")]
                if any(visual_notes):
                    normalized["visual_notes"] = visual_notes
                    normalized["visual_note"] = next((note for note in visual_notes if note), "")
            if unique_message_key(normalized):
                normalized_entries.append(normalized)
        if not normalized_entries:
            return {"added": 0, "total": len(self.get_messages(chat_name, max_count))}

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

            repair_plan = None
            if reconcile_visible_snapshot:
                repair_plan = build_repair_plan(
                    messages,
                    normalized_entries,
                    anchor_recent_count=anchor_recent_count,
                    chat_type=chat_type,
                )
                normalized_entries = repair_plan.messages_to_append

            existing_keys = {
                unique_message_key(item)
                for item in messages
                if isinstance(item, dict) and unique_message_key(item)
            }
            added = 0
            indexed_entries = list(enumerate(normalized_entries))
            indexed_entries.sort(
                key=lambda pair: (
                    self._parse_message_time(pair[1].get("time")) or datetime.max,
                    pair[0],
                )
            )
            normalized_entries = [entry for _, entry in indexed_entries]
            for entry in normalized_entries:
                entry.pop("time_inferred", None)
                key = unique_message_key(entry)
                if not key or (not reconcile_visible_snapshot and key in existing_keys):
                    continue
                messages = self._append_message_in_order(messages, entry, recent_count=10)
                if not reconcile_visible_snapshot:
                    existing_keys.add(key)
                added += 1
            try:
                max_count = int(max_count)
            except (TypeError, ValueError):
                max_count = 0
            if max_count > 0 and len(messages) > max_count:
                messages = messages[-max_count:]
            if added:
                _atomic_write_json(path, messages)
            result = {"added": added, "total": len(messages)}
            if repair_plan is not None:
                result.update(
                    anchor_found=repair_plan.anchor_found,
                    anchor_index=repair_plan.anchor_index,
                )
            return result

    @staticmethod
    def _message_image_paths(entry):
        entry = entry if isinstance(entry, dict) else {}
        msg_type = str(entry.get("type", "") or "").strip().lower()
        image_paths = entry.get("image_paths")
        if isinstance(image_paths, list):
            normalized_paths = [str(path or "").strip() for path in image_paths if str(path or "").strip()]
            if normalized_paths:
                return normalized_paths
        raw = str(entry.get("content", "") or "").strip()
        if msg_type == "image":
            return [raw] if raw and raw != "[图片]" else []
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
                if entry.get("type") == "image" and entry.get("content") != "[图片]":
                    entry["content"] = "[图片]"
                    changed = True
                if entry.get("image_paths") != entry_paths:
                    entry["image_paths"] = entry_paths
                    changed = True
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
            _atomic_write_json(path, messages)
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
                _atomic_write_json(path, [], indent=None)
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
                    _atomic_write_json(memory_file, [], indent=None)
                    count += 1
                except Exception:
                    pass
        return count
