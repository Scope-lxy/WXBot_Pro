"""Persistent reply-count state for per-user AI reply limits and notifications."""

from __future__ import annotations

from datetime import datetime, timedelta
import json
import os
import threading

from core.logger import log


class ReplyCountStore:
    """
    私聊回复计数器管理类。
    负责持久化每个用户的 AI 回复次数、超限通知状态和 API 错误通知状态。
    """

    DEFAULT_DATA = {"users": {}}

    def __init__(self, file_path):
        self.file_path = file_path
        self._lock = threading.RLock()
        self.data = self._load()

    @classmethod
    def _empty_data(cls):
        return {"users": {}}

    @classmethod
    def _normalize_user_data(cls, user_data):
        if not isinstance(user_data, dict):
            user_data = {}
        try:
            count = int(user_data.get("count", 0))
        except Exception:
            count = 0
        return {
            "count": max(0, count),
            "window_started_at": str(user_data.get("window_started_at", "") or ""),
            "api_err_notified": bool(user_data.get("api_err_notified", False)),
            "limit_notified": bool(user_data.get("limit_notified", False)),
        }

    @classmethod
    def _normalize_data(cls, raw_data):
        if not isinstance(raw_data, dict):
            return cls._empty_data()
        users = raw_data.get("users", {})
        if not isinstance(users, dict):
            users = {}
        normalized_users = {}
        for user, user_data in users.items():
            user = str(user).strip()
            if user:
                normalized_users[user] = cls._normalize_user_data(user_data)
        return {
            "users": normalized_users,
        }

    def _load(self):
        if not os.path.exists(self.file_path):
            return self._empty_data()
        try:
            with open(self.file_path, "r", encoding="utf-8") as f:
                return self._normalize_data(json.load(f))
        except Exception as e:
            log(level="WARNING", message=f"加载 reply_count.json 失败: {e}")
            return self._empty_data()

    def _save_locked(self):
        os.makedirs(os.path.dirname(self.file_path), exist_ok=True)
        tmp_path = self.file_path + ".tmp"
        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump(self.data, f, ensure_ascii=False, indent=4)
        os.replace(tmp_path, self.file_path)

    def save(self):
        with self._lock:
            self._save_locked()

    def _get_or_init_user_locked(self, user_key):
        user_key = str(user_key).strip()
        users = self.data.setdefault("users", {})
        if user_key not in users:
            users[user_key] = self._normalize_user_data({})
        else:
            users[user_key] = self._normalize_user_data(users[user_key])
        return users[user_key]

    def _refresh_user_window_locked(self, user_key, *, now, limit_hours):
        user_data = self._get_or_init_user_locked(user_key)
        try:
            limit_hours = int(limit_hours or 0)
        except Exception:
            limit_hours = 0
        if limit_hours <= 0:
            return user_data
        started_at_raw = str(user_data.get("window_started_at", "") or "")
        try:
            started_at = datetime.fromisoformat(started_at_raw)
        except Exception:
            started_at = None
        if started_at is None or now - started_at >= timedelta(hours=limit_hours):
            user_data["count"] = 0
            user_data["window_started_at"] = now.isoformat(timespec="seconds")
            user_data["limit_notified"] = False
            user_data["api_err_notified"] = False
        return user_data

    def get_user(self, user_key, *, now=None, limit_hours=0):
        with self._lock:
            if now is not None:
                return self._refresh_user_window_locked(user_key, now=now, limit_hours=limit_hours)
            return self._get_or_init_user_locked(user_key)

    def can_consume(self, user_key, *, limit_count, limit_hours, now=None):
        try:
            limit_count = int(limit_count or 0)
        except Exception:
            limit_count = 0
        try:
            limit_hours = int(limit_hours or 0)
        except Exception:
            limit_hours = 0
        if limit_count <= 0 or limit_hours <= 0:
            return True
        now = now or datetime.now()
        with self._lock:
            user_data = self._refresh_user_window_locked(user_key, now=now, limit_hours=limit_hours)
            return int(user_data.get("count", 0) or 0) < limit_count

    def increment_ai_count(self, user_key, *, now=None, limit_hours=0):
        now = now or datetime.now()
        with self._lock:
            user_data = self._refresh_user_window_locked(user_key, now=now, limit_hours=limit_hours)
            if not str(user_data.get("window_started_at", "") or ""):
                user_data["window_started_at"] = now.isoformat(timespec="seconds")
            user_data["count"] = int(user_data.get("count", 0) or 0) + 1
            self._save_locked()
            return user_data["count"]

    def mark_limit_notified(self, user_key):
        with self._lock:
            user_data = self.get_user(user_key)
            if user_data.get("limit_notified"):
                return False
            user_data["limit_notified"] = True
            self._save_locked()
            return True

    def mark_api_err_notified(self, user_key):
        with self._lock:
            user_data = self.get_user(user_key)
            if user_data.get("api_err_notified"):
                return False
            user_data["api_err_notified"] = True
            self._save_locked()
            return True

    def clear_user(self, user_key):
        user_key = str(user_key).strip()
        with self._lock:
            users = self.data.setdefault("users", {})
            if user_key not in users:
                return False
            del users[user_key]
            self._save_locked()
            return True

    @staticmethod
    def was_send_success(result):
        if result is True:
            return True
        if result is False or result is None:
            return False
        if isinstance(result, dict):
            status = str(result.get("status", "")).lower()
            if status in ("success", "ok", "true", "成功"):
                return True
            if status in ("queued", "pending", "deferred", "延后", "待发送"):
                return False
            if status in ("error", "fail", "failed", "false", "失败", "错误"):
                return False
            if result.get("code") == 0:
                return True
            if result.get("success") is True:
                return True
            if result.get("success") is False:
                return False
        return bool(result)
