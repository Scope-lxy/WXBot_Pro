"""Voice reply decision helpers."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path

HTML_BREAK_RE = re.compile(r"<br\s*/?>", re.IGNORECASE)
URL_RE = re.compile(r"https?://", re.IGNORECASE)
CODE_BLOCK_RE = re.compile(r"```")
PATH_RE = re.compile(r"\b[A-Za-z]:\\|/Users/|/home/")
LIST_RE = re.compile(r"^\s*(?:\d+[.)、]|[-*+])\s+", re.MULTILINE)
HTML_TAG_RE = re.compile(r"<[^>]+>")
REFUSAL_RE = re.compile(r"(抱歉|对不起).{0,12}(不能|无法)|作为\s*AI|我无法提供|不能提供")
DEFAULT_CHAT_VOICE_REPLY_KEYWORDS = [
    "发语音",
    "语音回我",
    "语音回复",
    "回语音",
    "用语音",
    "用语音说",
    "用语音回",
    "说给我听",
    "讲给我听",
    "念给我听",
    "读给我听",
    "说一下",
    "讲一下",
    "说句话",
    "发条语音",
    "来条语音",
    "来段语音",
    "回我语音",
    "发个语音",
    "语音说",
    "语音讲",
    "别打字",
    "不要打字",
    "别文字",
    "发微信语音",
    "微信语音回",
]
DEFAULT_GROUP_VOICE_REPLY_KEYWORDS = [
    "发语音",
    "发条语音",
    "发个语音",
    "语音说一下",
    "语音讲一下",
    "用语音说",
    "用语音讲",
    "说给我们听",
    "讲给我们听",
    "读给我们听",
    "念给我们听",
    "说给大家听",
    "讲给大家听",
    "语音回复一下",
    "回条语音",
    "来条语音",
    "来段语音",
    "别打字",
    "不要打字",
    "发微信语音",
]


def normalize_text_for_tts(text):
    value = str(text or "")
    value = HTML_BREAK_RE.sub("。", value)
    value = value.replace("/br", "。")
    value = re.sub(r"\s*\n+\s*", "。", value)
    value = re.sub(r"\s+", " ", value)
    value = re.sub(r"[。]{2,}", "。", value)
    value = re.sub(r"[，,]{2,}", "，", value)
    value = value.strip()
    return value.strip("。") if value == "。" else value.strip(" 。")


def build_tts_context_text(text):
    return normalize_text_for_tts(text)


def classify_voice_reply_text(text):
    value = str(text or "").strip()
    if not value:
        return "empty"
    if "API返回错误" in value:
        return "api_error"
    if REFUSAL_RE.search(value):
        return "refusal"
    return "normal"


def is_text_suitable_for_voice(text, *, max_chars=100):
    value = str(text or "").strip()
    if not value or len(value) > max_chars:
        return False
    if URL_RE.search(value) or CODE_BLOCK_RE.search(value) or PATH_RE.search(value) or LIST_RE.search(value):
        return False
    if HTML_TAG_RE.search(HTML_BREAK_RE.sub("", value)):
        return False
    return classify_voice_reply_text(value) == "normal"


def contains_any_keyword(text, keywords):
    value = str(text or "")
    return any(
        keyword and keyword in value
        for keyword in (str(item or "").strip() for item in (keywords or []))
    )


def selected_private_voice_trigger_modes(config):
    raw_modes = getattr(config, "chat_voice_reply_trigger_modes", None)
    if isinstance(raw_modes, list):
        modes = [
            mode for mode in (str(item or "").strip() for item in raw_modes)
            if mode in {"incoming_voice", "keyword"}
        ]
        if modes:
            return modes
    modes = []
    if getattr(config, "chat_voice_reply_request_keywords", []):
        modes.append("keyword")
    return modes


@dataclass
class VoiceReplyState:
    limits: dict = field(default_factory=dict)
    private_sessions: dict = field(default_factory=dict)


class VoiceReplyLimiter:
    def __init__(self, state):
        self.state = state

    def _passes_cooldown(self, key, *, now, cooldown_minutes):
        item = self.state.limits.get(key, {})
        last_raw = item.get("last_sent_at")
        if last_raw and int(cooldown_minutes or 0) > 0:
            try:
                last = datetime.fromisoformat(last_raw)
                if now - last < timedelta(minutes=cooldown_minutes):
                    return False
            except ValueError:
                pass
        return True

    def _refresh_limit_window(self, key, *, now, limit_hours, create=True):
        if create:
            item = self.state.limits.setdefault(key, {})
        else:
            item = self.state.limits.get(key, {})
        try:
            limit_hours = int(limit_hours or 0)
        except Exception:
            limit_hours = 0
        if limit_hours <= 0:
            return item
        started_at_raw = str(item.get("window_started_at", "") or "")
        try:
            started_at = datetime.fromisoformat(started_at_raw)
        except ValueError:
            started_at = None
        if started_at is None or now - started_at >= timedelta(hours=limit_hours):
            if create:
                item["count"] = 0
                item["window_started_at"] = now.isoformat(timespec="seconds")
            else:
                item = dict(item)
                item["count"] = 0
                item["window_started_at"] = now.isoformat(timespec="seconds")
        return item

    def can_send(self, key, *, now, cooldown_minutes, limit_count, limit_hours):
        if not self._passes_cooldown(key, now=now, cooldown_minutes=cooldown_minutes):
            return False
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
        item = self._refresh_limit_window(key, now=now, limit_hours=limit_hours, create=False)
        return int(item.get("count", 0) or 0) < limit_count

    def mark_sent(self, key, *, now, limit_hours):
        item = self._refresh_limit_window(key, now=now, limit_hours=limit_hours)
        if not str(item.get("window_started_at", "") or ""):
            item["window_started_at"] = now.isoformat(timespec="seconds")
        item["count"] = int(item.get("count", 0) or 0) + 1
        item["last_sent_at"] = now.isoformat(timespec="seconds")


class VoiceSessionManager:
    def __init__(self, state):
        self.state = state

    def start_private_session(self, chat_who, *, now, minutes, turns, section_id=""):
        self.state.private_sessions[str(chat_who)] = {
            "enabled_until": (now + timedelta(minutes=minutes)).isoformat(timespec="seconds"),
            "remaining_turns": max(0, int(turns)),
            "section_id": str(section_id or "").strip(),
        }

    def get_private_session_section_id(self, chat_who):
        item = self.state.private_sessions.get(str(chat_who), {})
        return str(item.get("section_id", "") or "").strip()

    def set_private_session_section_id(self, chat_who, section_id):
        item = self.state.private_sessions.get(str(chat_who))
        if not item:
            return
        item["section_id"] = str(section_id or "").strip()

    def stop_private_session(self, chat_who):
        self.state.private_sessions.pop(str(chat_who), None)

    def is_private_session_active(self, chat_who, *, now):
        item = self.state.private_sessions.get(str(chat_who), {})
        if int(item.get("remaining_turns", 0) or 0) <= 0:
            self.stop_private_session(chat_who)
            return False
        try:
            enabled_until = datetime.fromisoformat(item.get("enabled_until", ""))
        except ValueError:
            self.stop_private_session(chat_who)
            return False
        if now > enabled_until:
            self.stop_private_session(chat_who)
            return False
        return True

    def consume_private_turn(self, chat_who):
        item = self.state.private_sessions.get(str(chat_who))
        if not item:
            return
        item["remaining_turns"] = max(0, int(item.get("remaining_turns", 0) or 0) - 1)
        if item["remaining_turns"] <= 0:
            self.stop_private_session(chat_who)


def private_voice_candidate(config, chat_who, message, session_manager, *, now):
    if not bool(getattr(config, "chat_voice_reply_switch", False)):
        return False, False
    trigger_modes = set(selected_private_voice_trigger_modes(config))
    content = str(getattr(message, "content", "") or "")
    if "keyword" in trigger_modes and contains_any_keyword(content, getattr(config, "chat_voice_reply_request_keywords", [])):
        return True, True
    if session_manager and session_manager.is_private_session_active(chat_who, now=now):
        return True, False
    if "incoming_voice" in trigger_modes and (
        getattr(message, "type", "") == "voice"
        or bool(getattr(message, "_contains_voice_message", False))
    ):
        return True, True
    return False, False


def group_voice_candidate(config, message):
    if not bool(getattr(config, "group_voice_reply_switch", False)):
        return False
    return contains_any_keyword(
        getattr(message, "content", ""),
        getattr(config, "group_voice_reply_request_keywords", []),
    )


def load_voice_reply_state(path):
    try:
        with Path(path).open("r", encoding="utf-8") as handle:
            data = json.load(handle)
    except Exception:
        data = {}
    return VoiceReplyState(
        limits=data.get("limits", {}) or {},
        private_sessions=data.get("private_sessions", {}) or {},
    )


def save_voice_reply_state(path, state):
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    with target.open("w", encoding="utf-8") as handle:
        json.dump(
            {
                "limits": dict(getattr(state, "limits", {}) or {}),
                "private_sessions": dict(getattr(state, "private_sessions", {}) or {}),
            },
            handle,
            ensure_ascii=False,
            indent=2,
        )
