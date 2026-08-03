"""Best-effort hourly runtime metrics for dashboard charts."""

from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timedelta
import hashlib
import json
import os
import threading
import time
from pathlib import Path
from typing import Any

from core.atomic_storage import replace_with_retry


SCHEMA_VERSION = 1
DEFAULT_RETENTION_DAYS = 365
FILE_LOCK_TIMEOUT_SECONDS = 5


class RuntimeMetricsStorageError(RuntimeError):
    pass


_path_locks = {}
_path_locks_lock = threading.Lock()

METRIC_KEYS = (
    "received_messages",
    "api_calls",
    "chat_api_calls",
    "image_api_calls",
    "material_preface_api_calls",
    "ai_outreach_decision_api_calls",
    "ai_outreach_preface_api_calls",
    "reply_count",
    "scheduled_fixed_runs",
    "scheduled_random_runs",
    "scheduled_fixed_success_targets",
    "scheduled_random_success_targets",
    "keyword_reply_messages",
    "keyword_reply_triggers",
    "material_success_count",
    "ai_material_success_count",
    "relationship_blocked_today",
    "relationship_deleted_today",
    "friend_request_sent_count",
    "new_friend_accepted_count",
)

SET_KEYS = (
    "active_private_chats",
    "active_group_chats",
    "keyword_private_targets",
    "keyword_group_targets",
    "material_success_targets",
    "ai_material_success_targets",
)


def _coerce_now(now: Any = None) -> datetime:
    if isinstance(now, datetime):
        return now
    if isinstance(now, str):
        text = now.strip()
        if text:
            return datetime.fromisoformat(text)
    return datetime.now()


def _hour_key(now: Any = None) -> str:
    current = _coerce_now(now)
    return current.replace(minute=0, second=0, microsecond=0).isoformat(timespec="hours")


def _day_key(now: Any = None) -> str:
    return _coerce_now(now).date().isoformat()


def _path_lock(path: Path):
    key = os.path.normcase(os.path.abspath(str(path)))
    with _path_locks_lock:
        return _path_locks.setdefault(key, threading.RLock())


def _hash_identity(value: Any) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


def _empty_bucket() -> dict[str, Any]:
    bucket = {key: 0 for key in METRIC_KEYS}
    for key in SET_KEYS:
        bucket[key] = []
    return bucket


def _normalize_bucket(raw: Any) -> dict[str, Any]:
    source = raw if isinstance(raw, dict) else {}
    bucket = _empty_bucket()
    for key in METRIC_KEYS:
        try:
            bucket[key] = max(0, int(source.get(key) or 0))
        except (TypeError, ValueError):
            bucket[key] = 0
    for key in SET_KEYS:
        values = source.get(key) if isinstance(source.get(key), list) else []
        normalized = []
        seen = set()
        for value in values:
            text = str(value or "").strip()
            if text and text not in seen:
                normalized.append(text)
                seen.add(text)
        bucket[key] = normalized
    return bucket


def _merge_bucket(target: dict[str, Any], raw_bucket: Any) -> None:
    bucket = _normalize_bucket(raw_bucket)
    for metric in METRIC_KEYS:
        target[metric] = int(target.get(metric, 0) or 0) + int(bucket.get(metric, 0) or 0)
    for set_key in SET_KEYS:
        values = target.setdefault(set_key, [])
        seen = set(values)
        for value in bucket.get(set_key) or []:
            if value not in seen:
                values.append(value)
                seen.add(value)


def _normalize_payload(raw: Any) -> dict[str, Any]:
    source = raw if isinstance(raw, dict) else {}
    hours = {}
    raw_hours = source.get("hours") if isinstance(source.get("hours"), dict) else {}
    for key, bucket in raw_hours.items():
        hour = str(key or "").strip()
        if hour:
            hours[hour] = _normalize_bucket(bucket)
    return {
        "schema_version": SCHEMA_VERSION,
        "updated_at": str(source.get("updated_at") or "").strip(),
        "hours": hours,
    }


class RuntimeMetricsStore:
    def __init__(self, path, *, retention_days: int = DEFAULT_RETENTION_DAYS):
        self.path = Path(path)
        self.retention_days = max(1, int(retention_days or DEFAULT_RETENTION_DAYS))
        self._lock = _path_lock(self.path)

    def increment(self, key: str, *, amount: int = 1, now: Any = None) -> dict[str, Any]:
        key = str(key or "").strip()
        if key not in METRIC_KEYS:
            raise KeyError(key)
        try:
            step = int(amount or 0)
        except (TypeError, ValueError):
            step = 0
        if step <= 0:
            return self.load()
        current = _coerce_now(now)
        with self._lock, self._file_lock():
            payload = self._load_normalized()
            bucket = payload["hours"].setdefault(_hour_key(current), _empty_bucket())
            bucket[key] = int(bucket.get(key, 0) or 0) + step
            payload["updated_at"] = current.replace(microsecond=0).isoformat()
            self._prune(payload, current)
            self._write(payload)
            return json.loads(json.dumps(payload, ensure_ascii=False))

    def add_unique(self, key: str, identity: Any, *, now: Any = None) -> dict[str, Any]:
        key = str(key or "").strip()
        if key not in SET_KEYS:
            raise KeyError(key)
        digest = _hash_identity(identity)
        if not digest:
            return self.load()
        current = _coerce_now(now)
        with self._lock, self._file_lock():
            payload = self._load_normalized()
            bucket = payload["hours"].setdefault(_hour_key(current), _empty_bucket())
            values = bucket.setdefault(key, [])
            if digest not in values:
                values.append(digest)
            payload["updated_at"] = current.replace(microsecond=0).isoformat()
            self._prune(payload, current)
            self._write(payload)
            return json.loads(json.dumps(payload, ensure_ascii=False))

    def set_today_counts(self, counts: dict[str, Any], *, now: Any = None) -> dict[str, Any]:
        current = _coerce_now(now)
        hour = _hour_key(current)
        today = _day_key(current)
        updates = {}
        for key in ("relationship_blocked_today", "relationship_deleted_today"):
            if key in counts:
                try:
                    updates[key] = max(0, int(counts.get(key) or 0))
                except (TypeError, ValueError):
                    updates[key] = 0
        if not updates:
            return self.load()
        with self._lock, self._file_lock():
            payload = self._load_normalized()
            for existing_hour, bucket in payload["hours"].items():
                if str(existing_hour).startswith(today):
                    for key in updates:
                        bucket[key] = 0
            bucket = payload["hours"].setdefault(hour, _empty_bucket())
            bucket.update(updates)
            payload["updated_at"] = current.replace(microsecond=0).isoformat()
            self._prune(payload, current)
            self._write(payload)
            return json.loads(json.dumps(payload, ensure_ascii=False))

    def load(self) -> dict[str, Any]:
        with self._lock, self._file_lock():
            return json.loads(json.dumps(self._load_normalized(), ensure_ascii=False))

    def series_payload(
        self,
        *,
        now: Any = None,
        days: int = 7,
        hourly_bucket_hours: int = 1,
    ) -> dict[str, Any]:
        current = _coerce_now(now)
        days = max(1, min(self.retention_days, int(days or 7)))
        hourly_bucket_hours = max(1, min(24, int(hourly_bucket_hours or 1)))
        start = (current.replace(minute=0, second=0, microsecond=0) - timedelta(hours=(days * 24) - 1))
        with self._lock, self._file_lock():
            payload = self._load_normalized()
        hourly = []
        for index in range(0, days * 24, hourly_bucket_hours):
            hour_dt = start + timedelta(hours=index)
            key = hour_dt.isoformat(timespec="hours")
            bucket = _empty_bucket()
            for offset in range(min(hourly_bucket_hours, (days * 24) - index)):
                source_key = (hour_dt + timedelta(hours=offset)).isoformat(timespec="hours")
                _merge_bucket(bucket, payload["hours"].get(source_key))
            hourly.append(self._point_from_bucket(key, bucket))
        day_start = current.date() - timedelta(days=days - 1)
        daily_buckets = {
            (day_start + timedelta(days=index)).isoformat(): _empty_bucket()
            for index in range(days)
        }
        for hour_key, raw_bucket in payload["hours"].items():
            day = str(hour_key)[:10]
            if day not in daily_buckets:
                continue
            target = daily_buckets[day]
            _merge_bucket(target, raw_bucket)
        daily = [self._point_from_bucket(key, daily_buckets.get(key)) for key in sorted(daily_buckets)]
        today_key = current.date().isoformat()
        today = next((point for point in daily if point["key"] == today_key), self._point_from_bucket(today_key, {}))
        return {
            "status": "success",
            "updated_at": payload.get("updated_at", ""),
            "range_days": days,
            "hourly": hourly,
            "daily": daily,
            "today": today,
        }

    def _point_from_bucket(self, key: str, raw_bucket: Any) -> dict[str, Any]:
        bucket = _normalize_bucket(raw_bucket)
        point = {"key": key}
        point.update({metric: int(bucket.get(metric, 0) or 0) for metric in METRIC_KEYS})
        point["private_active_count"] = len(bucket.get("active_private_chats") or [])
        point["group_active_count"] = len(bucket.get("active_group_chats") or [])
        point["keyword_private_target_count"] = len(bucket.get("keyword_private_targets") or [])
        point["keyword_group_target_count"] = len(bucket.get("keyword_group_targets") or [])
        point["material_success_target_count"] = len(bucket.get("material_success_targets") or [])
        point["ai_material_success_target_count"] = len(bucket.get("ai_material_success_targets") or [])
        return point

    def _load_normalized(self) -> dict[str, Any]:
        if not self.path.exists():
            return _normalize_payload({})
        try:
            return _normalize_payload(json.loads(self.path.read_text(encoding="utf-8")))
        except (OSError, json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise RuntimeMetricsStorageError(
                f"运行统计文件无法读取，已阻止覆盖：{self.path}"
            ) from exc

    @contextmanager
    def _file_lock(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        lock_path = self.path.with_name(f"{self.path.name}.lock")
        deadline = time.monotonic() + FILE_LOCK_TIMEOUT_SECONDS
        handle = None
        locked = False
        try:
            while handle is None:
                try:
                    if not lock_path.exists() or lock_path.stat().st_size == 0:
                        with open(lock_path, "ab") as seed:
                            if seed.tell() == 0:
                                seed.write(b"0")
                                seed.flush()
                    handle = open(lock_path, "a+b")
                except OSError as exc:
                    if time.monotonic() >= deadline:
                        raise RuntimeMetricsStorageError(
                            f"运行统计锁文件无法打开：{self.path}"
                        ) from exc
                    time.sleep(0.05)
            handle.seek(0)
            while True:
                try:
                    if os.name == "nt":
                        import msvcrt

                        msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                    else:
                        import fcntl

                        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    locked = True
                    break
                except OSError as exc:
                    if time.monotonic() >= deadline:
                        raise RuntimeMetricsStorageError(
                            f"运行统计文件正被其他进程使用：{self.path}"
                        ) from exc
                    time.sleep(0.05)
            yield
        finally:
            try:
                if locked and handle is not None:
                    handle.seek(0)
                    if os.name == "nt":
                        import msvcrt

                        msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
                    else:
                        import fcntl

                        fcntl.flock(handle, fcntl.LOCK_UN)
            finally:
                if handle is not None:
                    handle.close()

    def _write(self, payload: dict[str, Any]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temp_path = self.path.with_name(
            f"{self.path.name}.{os.getpid()}.{threading.get_ident()}.tmp"
        )
        try:
            temp_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
            replace_with_retry(temp_path, self.path)
        finally:
            try:
                temp_path.unlink()
            except FileNotFoundError:
                pass

    def _prune(self, payload: dict[str, Any], current: datetime) -> None:
        cutoff = current.replace(minute=0, second=0, microsecond=0) - timedelta(days=self.retention_days)
        kept = {}
        for key, bucket in payload.get("hours", {}).items():
            try:
                hour_dt = datetime.fromisoformat(str(key))
            except ValueError:
                continue
            if hour_dt >= cutoff:
                kept[str(key)] = bucket
        payload["hours"] = kept
