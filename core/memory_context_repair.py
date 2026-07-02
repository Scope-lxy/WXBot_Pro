"""Private-chat context repair helpers for aligning WeChat-visible messages with memory."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime


DEFAULT_LOW_RISK_COOLDOWN_SECONDS = 300
DEFAULT_HIGH_RISK_COOLDOWN_SECONDS = 1800
DEFAULT_ANCHOR_RECENT_COUNT = 5
DEFAULT_VISIBLE_LIMIT = 30
DEFAULT_HISTORY_LIMIT = 50


@dataclass(frozen=True)
class RepairPlan:
    anchor_index: int | None
    messages_to_append: list[dict]
    anchor_found: bool


def clean_text(value) -> str:
    return str(value or "").replace("\r\n", "\n").replace("\r", "\n").strip()


def normalize_message_type(value) -> str:
    msg_type = clean_text(value).lower()
    if msg_type == "other":
        return "text"
    return msg_type or "text"


def normalize_message_content(content, msg_type="") -> str:
    msg_type = normalize_message_type(msg_type)
    text = clean_text(content)
    if msg_type == "image":
        return "[图片]"
    return text


def message_fingerprint(item) -> str:
    if not isinstance(item, dict):
        return ""
    msg_type = normalize_message_type(item.get("type"))
    content = normalize_message_content(item.get("content"), msg_type)
    if not content and msg_type not in {"image", "emotion", "voice", "video", "file"}:
        return ""
    parts = [
        clean_text(item.get("attr")).lower(),
        clean_text(item.get("sender")),
        msg_type,
        content,
    ]
    return "|".join(parts)


def unique_message_key(item) -> str:
    if not isinstance(item, dict):
        return ""
    fp = message_fingerprint(item)
    if not fp:
        return ""
    message_time = clean_text(item.get("time"))
    return f"{message_time}|{fp}" if message_time else fp


def normalize_wechat_message(message, *, source="wechat_context_repair") -> dict:
    msg_type = normalize_message_type(getattr(message, "type", "text"))
    entry = {
        "time": clean_text(getattr(message, "time", "")) or datetime.now().strftime("%Y/%m/%d %H:%M:%S"),
        "type": msg_type,
        "attr": clean_text(getattr(message, "attr", "")),
        "sender": clean_text(getattr(message, "sender", "")),
        "content": normalize_message_content(getattr(message, "content", ""), msg_type),
        "source": source,
    }
    return entry


def filter_model_repair_messages(messages) -> list[dict]:
    result = []
    for item in messages or []:
        if not isinstance(item, dict):
            continue
        attr = clean_text(item.get("attr")).lower()
        if attr == "system":
            continue
        if not message_fingerprint(item):
            continue
        result.append(item)
    return result


def recent_effective_messages(messages, count) -> list[dict]:
    try:
        count = max(1, int(count or DEFAULT_ANCHOR_RECENT_COUNT))
    except Exception:
        count = DEFAULT_ANCHOR_RECENT_COUNT
    return filter_model_repair_messages(messages)[-count:]


def _tail_sequence_match(remote_fps, local_fps, start) -> bool:
    if start < 0:
        return False
    if start + len(local_fps) > len(remote_fps):
        return False
    return remote_fps[start:start + len(local_fps)] == local_fps


def find_anchor_index(local_history, remote_history, *, anchor_recent_count=DEFAULT_ANCHOR_RECENT_COUNT) -> int | None:
    remote = filter_model_repair_messages(remote_history)
    if not remote:
        return None
    remote_fps = [message_fingerprint(item) for item in remote]
    local_tail = recent_effective_messages(local_history, anchor_recent_count)
    local_fps = [message_fingerprint(item) for item in local_tail if message_fingerprint(item)]
    if not local_fps:
        return None

    max_sequence = min(len(local_fps), len(remote_fps))
    for size in range(max_sequence, 1, -1):
        sequence = local_fps[-size:]
        for start in range(len(remote_fps) - size, -1, -1):
            if _tail_sequence_match(remote_fps, sequence, start):
                return start + size - 1

    last_fp = local_fps[-1]
    if not last_fp:
        return None
    matches = [index for index, fp in enumerate(remote_fps) if fp == last_fp]
    if len(matches) == 1:
        return matches[0]
    return None


def build_repair_plan(local_history, remote_history, *, anchor_recent_count=DEFAULT_ANCHOR_RECENT_COUNT) -> RepairPlan:
    remote = filter_model_repair_messages(remote_history)
    anchor = find_anchor_index(local_history, remote, anchor_recent_count=anchor_recent_count)
    existing_keys = {
        unique_message_key(item)
        for item in filter_model_repair_messages(local_history)
        if unique_message_key(item)
    }
    messages_to_append = [
        dict(item)
        for item in remote
        if unique_message_key(item) and unique_message_key(item) not in existing_keys
    ]
    return RepairPlan(
        anchor_index=anchor,
        messages_to_append=messages_to_append,
        anchor_found=anchor is not None,
    )


def current_message_found_near_tail(local_history, current_message, *, tail_count=5) -> bool:
    current_entry = normalize_wechat_message(current_message, source="current")
    current_key = unique_message_key(current_entry)
    current_fp = message_fingerprint(current_entry)
    if not current_fp:
        return False
    for item in recent_effective_messages(local_history, tail_count):
        if current_key and unique_message_key(item) == current_key:
            return True
        if message_fingerprint(item) == current_fp:
            return True
    return False
