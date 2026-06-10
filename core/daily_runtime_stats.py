"""Persistent day-scoped runtime stats for admin status surfaces."""

from __future__ import annotations

from datetime import datetime
import json
import threading
from pathlib import Path


STAT_KEYS = (
    "received_messages",
    "replied_messages",
    "scheduled_messages_sent",
    "material_forwards_sent",
    "ai_material_forwards_sent",
    "moments_published",
)


def _coerce_now(now=None) -> datetime:
    if isinstance(now, datetime):
        return now
    if isinstance(now, str):
        text = now.strip()
        if text:
            return datetime.fromisoformat(text)
    return datetime.now()


def build_empty_daily_runtime_stats(date_text: str) -> dict:
    payload = {"date": str(date_text or "").strip()}
    for key in STAT_KEYS:
        payload[key] = 0
    return payload


def normalize_daily_runtime_stats(payload, *, date_text: str) -> dict:
    raw = payload if isinstance(payload, dict) else {}
    normalized = build_empty_daily_runtime_stats(date_text)
    payload_date = str(raw.get("date") or "").strip()
    if payload_date == date_text:
        normalized["date"] = payload_date
        for key in STAT_KEYS:
            try:
                normalized[key] = max(0, int(raw.get(key) or 0))
            except (TypeError, ValueError):
                normalized[key] = 0
    return normalized


class DailyRuntimeStatsStore:
    def __init__(self, path):
        self.path = Path(path)
        self._lock = threading.RLock()

    def load(self, now=None):
        current = _coerce_now(now)
        date_text = current.date().isoformat()
        with self._lock:
            return normalize_daily_runtime_stats(self._read_raw(), date_text=date_text)

    def increment(self, key, *, amount=1, now=None):
        key = str(key or "").strip()
        if key not in STAT_KEYS:
            raise KeyError(key)
        current = _coerce_now(now)
        date_text = current.date().isoformat()
        with self._lock:
            payload = normalize_daily_runtime_stats(self._read_raw(), date_text=date_text)
            try:
                step = int(amount or 0)
            except (TypeError, ValueError):
                step = 0
            if step > 0:
                payload[key] += step
                self._write_raw(payload)
            return json.loads(json.dumps(payload, ensure_ascii=False))

    def _read_raw(self):
        if not self.path.exists():
            return {}
        try:
            return json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError, UnicodeDecodeError):
            return {}

    def _write_raw(self, payload):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temp_path = self.path.with_name(f"{self.path.name}.tmp")
        temp_path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        temp_path.replace(self.path)
