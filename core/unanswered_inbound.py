"""Persistent inbound records interrupted during private or group AI generation."""

from __future__ import annotations

import json
import os
import tempfile
import uuid
from datetime import datetime

from core.account_storage import account_area_file
from core.contact_profiles import directory_lock


class UnansweredInboundStore:
    def __init__(self, base_dir, wx_id):
        self.path = account_area_file(
            base_dir,
            str(wx_id or "default").strip() or "default",
            "unanswered_inbound",
            "records.json",
            create_parent=True,
        )

    @staticmethod
    def _now():
        return datetime.now().replace(microsecond=0).isoformat()

    def _load_unlocked(self):
        if not self.path.exists():
            return []
        try:
            value = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            return []
        return [dict(item) for item in value if isinstance(item, dict)] if isinstance(value, list) else []

    def _save_unlocked(self, records):
        payload = json.dumps(records[-500:], ensure_ascii=False, indent=2)
        fd, temp_name = tempfile.mkstemp(prefix=f".{self.path.name}.", suffix=".tmp", dir=str(self.path.parent))
        try:
            with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temp_name, self.path)
        finally:
            if os.path.exists(temp_name):
                os.unlink(temp_name)

    @staticmethod
    def _received_at(message):
        value = getattr(message, "_wxbot_received_at", 0.0)
        if isinstance(value, datetime):
            return value.timestamp()
        try:
            return float(value or 0.0)
        except (TypeError, ValueError):
            return 0.0

    def begin(self, conversation, message, *, chat_type="private"):
        record_id = str(uuid.uuid4())
        record = {
            "record_id": record_id,
            "conversation": str(conversation or "").strip(),
            "chat_type": str(chat_type or "private").strip().lower() or "private",
            "status": "routing",
            "created_at": self._now(),
            "updated_at": self._now(),
            "message": {
                key: str(getattr(message, key, "") or "")
                for key in ("content", "original_content", "type", "sender", "attr", "id", "hash", "hash_text", "time")
            },
            "received_at": self._received_at(message),
        }
        with directory_lock(self.path):
            records = self._load_unlocked()
            records.append(record)
            self._save_unlocked(records)
        return record_id

    def set_status(self, record_id, status):
        with directory_lock(self.path):
            records = self._load_unlocked()
            for item in reversed(records):
                if str(item.get("record_id") or "") == str(record_id or ""):
                    item["status"] = str(status or "")
                    item["updated_at"] = self._now()
                    break
            self._save_unlocked(records)

    def resolve(self, record_id):
        self.set_status(record_id, "resolved")

    def recover_for_replay(self):
        replay = []
        with directory_lock(self.path):
            records = self._load_unlocked()
            for item in records:
                status = str(item.get("status") or "")
                if status in {"ai_started", "replay_pending", "replaying"}:
                    item["status"] = "replay_pending"
                    item["updated_at"] = self._now()
                    replay.append(dict(item))
                elif status in {"routing", "send_started"}:
                    item["status"] = "uncertain"
                    item["updated_at"] = self._now()
            if replay or any(str(item.get("status") or "") == "uncertain" for item in records):
                self._save_unlocked(records)
        return replay

    def records(self):
        with directory_lock(self.path):
            return self._load_unlocked()
