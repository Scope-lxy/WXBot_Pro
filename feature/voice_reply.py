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
    "声音",
    "语音",
    "别打字",
    "说给我听",
]
DEFAULT_GROUP_VOICE_REPLY_KEYWORDS = [
    "声音",
    "语音",
    "别打字",
    "说给我听",
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


def _selected_voice_trigger_modes(config, trigger_modes_attr, keywords_attr):
    raw_modes = getattr(config, trigger_modes_attr, None)
    if isinstance(raw_modes, list):
        return [
            mode for mode in (str(item or "").strip() for item in raw_modes)
            if mode in {"incoming_voice", "keyword"}
        ]
    modes = []
    if getattr(config, keywords_attr, []):
        modes.append("keyword")
    return modes


def selected_private_voice_trigger_modes(config):
    return _selected_voice_trigger_modes(
        config,
        "chat_voice_reply_trigger_modes",
        "chat_voice_reply_request_keywords",
    )


def selected_group_voice_trigger_modes(config):
    return _selected_voice_trigger_modes(
        config,
        "group_voice_reply_trigger_modes",
        "group_voice_reply_request_keywords",
    )


@dataclass
class VoiceReplyState:
    limits: dict = field(default_factory=dict)


class VoiceReplyLimiter:
    def __init__(self, state):
        self.state = state

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

    def can_send(self, key, *, now, limit_count, limit_hours):
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


def private_voice_candidate(config, message):
    if not bool(getattr(config, "chat_voice_reply_switch", False)):
        return False
    trigger_modes = set(selected_private_voice_trigger_modes(config))
    content = str(getattr(message, "content", "") or "")
    if "keyword" in trigger_modes and contains_any_keyword(content, getattr(config, "chat_voice_reply_request_keywords", [])):
        return True
    if "incoming_voice" in trigger_modes and (
        getattr(message, "type", "") == "voice"
        or bool(getattr(message, "_contains_voice_message", False))
    ):
        return True
    return False


def group_voice_candidate(config, message):
    if not bool(getattr(config, "group_voice_reply_switch", False)):
        return False
    trigger_modes = set(selected_group_voice_trigger_modes(config))
    content = str(getattr(message, "content", "") or "")
    if "keyword" in trigger_modes and contains_any_keyword(content, getattr(config, "group_voice_reply_request_keywords", [])):
        return True
    if "incoming_voice" in trigger_modes and (
        getattr(message, "type", "") == "voice"
        or bool(getattr(message, "_contains_voice_message", False))
    ):
        return True
    return False


def load_voice_reply_state(path):
    try:
        with Path(path).open("r", encoding="utf-8") as handle:
            data = json.load(handle)
    except Exception:
        data = {}
    return VoiceReplyState(limits=data.get("limits", {}) or {})


def save_voice_reply_state(path, state):
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    with target.open("w", encoding="utf-8") as handle:
        json.dump(
            {"limits": dict(getattr(state, "limits", {}) or {})},
            handle,
            ensure_ascii=False,
            indent=2,
        )
