"""Persistent commit journal for non-idempotent WeChat UI deliveries."""

from __future__ import annotations

import json
import os
import tempfile
from datetime import datetime

from core.account_storage import account_area_file
from core.contact_profiles import directory_lock


class UIDeliveryJournal:
    def __init__(self, base_dir, wx_id):
        self.path = account_area_file(
            base_dir,
            str(wx_id or "default").strip() or "default",
            "ui_delivery",
            "journal.json",
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
        payload = json.dumps(records, ensure_ascii=False, indent=2)
        fd, temp_name = tempfile.mkstemp(
            prefix=f".{self.path.name}.",
            suffix=".tmp",
            dir=str(self.path.parent),
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temp_name, self.path)
        finally:
            if os.path.exists(temp_name):
                os.unlink(temp_name)

    def begin(self, delivery_id, kind, payload):
        delivery_id = str(delivery_id or "").strip()
        if not delivery_id:
            return False
        with directory_lock(self.path):
            records = self._load_unlocked()
            if any(str(item.get("delivery_id") or "") == delivery_id for item in records):
                return False
            payload = payload if isinstance(payload, dict) else {}
            records.append({
                "delivery_id": delivery_id,
                "kind": str(kind or ""),
                "conversation": str(payload.get("conversation") or ""),
                "request_id": str(payload.get("request_id") or ""),
                "run_id": str(payload.get("run_id") or ""),
                "batch_id": str(payload.get("batch_id") or ""),
                "contact_key": str(payload.get("contact_key") or ""),
                "targets": [str(item or "") for item in (payload.get("targets") or []) if str(item or "")],
                "status": "inflight",
                "started_at": self._now(),
                "finished_at": "",
                "error": "",
                "details": {},
            })
            self._save_unlocked(records)
        return True

    def finish(self, delivery_id, status, error="", details=None):
        delivery_id = str(delivery_id or "").strip()
        with directory_lock(self.path):
            records = self._load_unlocked()
            for item in reversed(records):
                if str(item.get("delivery_id") or "") == delivery_id:
                    item["status"] = str(status or "")
                    item["finished_at"] = self._now()
                    item["error"] = str(error or "")
                    item["details"] = dict(details or {})
                    break
            self._save_unlocked(records)

    def freeze_interrupted(self):
        recovered = []
        with directory_lock(self.path):
            records = self._load_unlocked()
            for item in records:
                if str(item.get("status") or "") != "inflight":
                    continue
                item["status"] = "uncertain"
                item["finished_at"] = self._now()
                item["error"] = "进程在微信调用完成前退出，禁止自动重发"
                recovered.append(dict(item))
            if recovered:
                self._save_unlocked(records)
        return recovered

    def records(self):
        with directory_lock(self.path):
            return self._load_unlocked()
