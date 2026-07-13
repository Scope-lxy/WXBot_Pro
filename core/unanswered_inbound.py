"""Persistent inbound records interrupted during private or group AI generation."""

from __future__ import annotations

import json
import os
import tempfile
import uuid
from datetime import datetime, timedelta

from core.account_storage import account_area_file
from core.atomic_storage import replace_with_retry
from core.contact_profiles import directory_lock


RESOLVED_HISTORY_LIMIT = 100
VOICE_PENDING_MAX_AGE = timedelta(hours=24)
VOICE_PENDING_MAX_STARTUP_RECOVERIES = 2
VOICE_HISTORY_UNAVAILABLE_STATUS = "voice_history_unavailable"


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
        terminal_history = [
            item
            for item in records
            if str(item.get("status") or "") in {"resolved", VOICE_HISTORY_UNAVAILABLE_STATUS}
        ][-RESOLVED_HISTORY_LIMIT:]
        retained_ids = {id(item) for item in terminal_history}
        retained = [
            item
            for item in records
            if str(item.get("status") or "") not in {"resolved", VOICE_HISTORY_UNAVAILABLE_STATUS}
            or id(item) in retained_ids
        ]
        payload = json.dumps(retained, ensure_ascii=False, indent=2)
        fd, temp_name = tempfile.mkstemp(prefix=f".{self.path.name}.", suffix=".tmp", dir=str(self.path.parent))
        try:
            with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            replace_with_retry(temp_name, self.path)
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

    def begin(self, conversation, message, *, chat_type="private", status="routing"):
        record_id = str(uuid.uuid4())
        record = {
            "record_id": record_id,
            "conversation": str(conversation or "").strip(),
            "chat_type": str(chat_type or "private").strip().lower() or "private",
            "status": str(status or "routing"),
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
        wanted_status = str(status or "")
        with directory_lock(self.path):
            records = self._load_unlocked()
            for item in reversed(records):
                if str(item.get("record_id") or "") == str(record_id or ""):
                    if str(item.get("status") or "") == wanted_status:
                        return False
                    item["status"] = wanted_status
                    item["updated_at"] = self._now()
                    self._save_unlocked(records)
                    return True
            return False

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

    def pending(self, status):
        wanted = str(status or "").strip()
        if not wanted:
            return []
        with directory_lock(self.path):
            return [dict(item) for item in self._load_unlocked() if str(item.get("status") or "") == wanted]

    @staticmethod
    def _parse_timestamp(value):
        try:
            return datetime.fromisoformat(str(value or ""))
        except (TypeError, ValueError):
            return None

    def prepare_voice_pending_recovery(
        self,
        *,
        now=None,
        max_age=VOICE_PENDING_MAX_AGE,
        max_startup_recoveries=VOICE_PENDING_MAX_STARTUP_RECOVERIES,
    ):
        """Return bounded history-only voice recovery work and terminalize stale records."""
        current = now if isinstance(now, datetime) else datetime.now()
        max_attempts = max(0, int(max_startup_recoveries or 0))
        recovered = []
        changed = False
        with directory_lock(self.path):
            records = self._load_unlocked()
            for item in records:
                if str(item.get("status") or "") != "voice_pending":
                    continue
                created_at = self._parse_timestamp(item.get("created_at"))
                try:
                    attempts = max(0, int(item.get("voice_recovery_attempts", 0) or 0))
                except (TypeError, ValueError):
                    attempts = 0
                too_old = created_at is None or current - created_at >= max_age
                if too_old or attempts >= max_attempts:
                    item["status"] = VOICE_HISTORY_UNAVAILABLE_STATUS
                    item["updated_at"] = self._now()
                    item["terminal_reason"] = "expired" if too_old else "startup_recovery_limit"
                    changed = True
                    continue
                attempts += 1
                item["voice_recovery_attempts"] = attempts
                item["updated_at"] = self._now()
                recovered.append(dict(item))
                changed = True
            if changed:
                self._save_unlocked(records)
        return recovered

    def mark_voice_history_unavailable(self, record_id, *, reason="runtime_recovery_limit"):
        with directory_lock(self.path):
            records = self._load_unlocked()
            for item in reversed(records):
                if str(item.get("record_id") or "") != str(record_id or ""):
                    continue
                if str(item.get("status") or "") == VOICE_HISTORY_UNAVAILABLE_STATUS:
                    return False
                item["status"] = VOICE_HISTORY_UNAVAILABLE_STATUS
                item["updated_at"] = self._now()
                item["terminal_reason"] = str(reason or "runtime_recovery_limit")
                self._save_unlocked(records)
                return True
            return False
