"""Repair a visible tail gap without reconstructing deep chat history."""

from __future__ import annotations

from dataclasses import dataclass

from core.message_pipeline import (
    is_failed_voice_transcription_text,
    is_unrecognized_voice_placeholder,
    voice_message_body,
)


DEFAULT_CONTEXT_REPAIR_RETRY_SECONDS = 300
DEFAULT_VISIBLE_LIMIT = 30
DEFAULT_LOCAL_HISTORY_LIMIT = 50
DEFAULT_ANCHOR_TAIL_COUNT = 5


@dataclass(frozen=True)
class SnapshotBoundary:
    found: bool
    messages: list


@dataclass(frozen=True)
class TailRepairPlan:
    anchor_found: bool
    anchor_index: int | None
    messages_to_append: list[dict]


def clean_text(value) -> str:
    return str(value or "").replace("\r\n", "\n").replace("\r", "\n").strip()


def normalize_message_type(value) -> str:
    msg_type = clean_text(value).lower()
    return "text" if msg_type == "other" else (msg_type or "text")


def _message_value(item, dict_name, object_name=None, default=""):
    if isinstance(item, dict):
        if dict_name in item:
            return item[dict_name]
        return item.get(object_name or dict_name, default)
    return getattr(item, object_name or dict_name, default)


def _message_type(item) -> str:
    return normalize_message_type(
        _message_value(item, "message_type", "type", "text")
    )


def _native_id(item) -> str:
    return clean_text(_message_value(item, "native_id", "id", "")) or clean_text(
        _message_value(item, "message_id", "id", "")
    )


def _native_hash_text(item) -> str:
    return clean_text(_message_value(item, "native_hash_text", "hash_text", ""))


def _direction(item) -> str:
    direction = clean_text(_message_value(item, "direction", default="")).lower()
    if direction in {"manual_self", "bot_echo", "self"}:
        return "self"
    if direction == "friend":
        return "friend"
    attr = clean_text(_message_value(item, "native_attr", "attr", "")).lower()
    return "self" if attr == "self" else attr


def _normalized_content(item) -> str:
    msg_type = _message_type(item)
    content = clean_text(_message_value(item, "content", default=""))
    if msg_type == "image":
        return "[图片]"
    if msg_type == "voice":
        if (
            not content
            or is_failed_voice_transcription_text(content)
            or is_unrecognized_voice_placeholder(content)
        ):
            return ""
        return clean_text(voice_message_body(content))
    return content


def _semantic_key(item, *, chat_type) -> tuple[str, ...] | None:
    msg_type = _message_type(item)
    direction = _direction(item)
    content = _normalized_content(item)
    if not direction or direction == "system" or not content:
        return None
    if chat_type == "group" and direction == "self" and content.startswith("@") and "\u2005" in content:
        _mention, _separator, body = content.partition("\u2005")
        content = body.lstrip() or content
    parts = [direction]
    if chat_type == "group" and direction != "self":
        sender = clean_text(_message_value(item, "sender", default=""))
        if not sender:
            return None
        parts.append(sender)
    parts.extend((msg_type, content))
    return tuple(parts)


def normalize_wechat_message(message, *, source="wechat_context_repair") -> dict:
    msg_type = _message_type(message)
    raw_content = clean_text(getattr(message, "content", ""))
    content = _normalized_content(message)
    entry = {
        "time": clean_text(getattr(message, "time", "")),
        "type": msg_type,
        "attr": clean_text(getattr(message, "attr", "")),
        "sender": clean_text(getattr(message, "sender", "")),
        "content": content,
        "message_id": clean_text(getattr(message, "id", "")),
        "native_hash": clean_text(getattr(message, "hash", "")),
        "native_hash_text": clean_text(getattr(message, "hash_text", "")),
        "source": source,
    }
    if msg_type == "image" and raw_content and raw_content != "[图片]":
        entry["image_paths"] = [raw_content]
    return entry


def normalize_wechat_snapshot(messages, *, source="wechat_context_repair") -> list[dict]:
    entries = []
    latest_time_marker = ""
    bubble_order = 0
    for message in messages or []:
        msg_type = _message_type(message)
        if msg_type == "time":
            latest_time_marker = clean_text(getattr(message, "time", "")) or clean_text(
                getattr(message, "content", "")
            ) or latest_time_marker
            continue
        entry = normalize_wechat_message(message, source=source)
        entry["window_order"] = bubble_order
        bubble_order += 1
        if not entry["content"] or entry["attr"].lower() == "system":
            continue
        entry["time"] = entry["time"] or latest_time_marker
        entries.append(entry)
    return entries


def _object_semantic_key(message, *, chat_type):
    return _semantic_key(normalize_wechat_message(message), chat_type=chat_type)


def _unique_match_index(messages, expected, value_getter):
    expected_value = value_getter(expected)
    if not expected_value:
        return None
    matches = [
        index
        for index, message in enumerate(messages)
        if value_getter(message) == expected_value
    ]
    return matches[0] if len(matches) == 1 else None


def _source_indexes_through_boundary(messages, sources, boundary_index, *, chat_type):
    source_keys = [_object_semantic_key(item, chat_type=chat_type) for item in sources]
    if all(source_keys) and boundary_index + 1 >= len(source_keys):
        start = boundary_index + 1 - len(source_keys)
        visible_keys = [
            _object_semantic_key(item, chat_type=chat_type)
            for item in messages[start:boundary_index + 1]
        ]
        if visible_keys == source_keys:
            return set(range(start, boundary_index + 1))

    indexes = []
    search_start = 0
    for source in sources:
        source_id = _native_id(source)
        source_hash = _native_hash_text(source)
        matches = [
            index
            for index in range(search_start, boundary_index + 1)
            if source_id and _native_id(messages[index]) == source_id
        ]
        if len(matches) != 1:
            matches = [
                index
                for index in range(search_start, boundary_index + 1)
                if source_hash and _native_hash_text(messages[index]) == source_hash
            ]
        if len(matches) != 1:
            return set()
        indexes.append(matches[0])
        search_start = matches[0] + 1
    return set(indexes)


def snapshot_before_current(messages, current_message, *, chat_type="private") -> SnapshotBoundary:
    source = list(messages or [])
    effective_pairs = [
        (raw_index, item)
        for raw_index, item in enumerate(source)
        if _message_type(item) != "time"
    ]
    effective = [item for _raw_index, item in effective_pairs]
    source_messages = list(getattr(current_message, "_merged_source_messages", None) or [current_message])
    source_messages = [item for item in source_messages if _message_type(item) != "time"]
    if not effective or not source_messages:
        return SnapshotBoundary(False, [])

    current = source_messages[-1]
    boundary_index = _unique_match_index(effective, current, _native_id)
    if boundary_index is None:
        boundary_index = _unique_match_index(effective, current, _native_hash_text)
    if boundary_index is None:
        current_key = _object_semantic_key(current, chat_type=chat_type)
        matches = [
            index
            for index, item in enumerate(effective)
            if current_key and _object_semantic_key(item, chat_type=chat_type) == current_key
        ]
        if len(matches) == 1:
            boundary_index = matches[0]
    if boundary_index is None:
        return SnapshotBoundary(False, [])

    source_indexes = _source_indexes_through_boundary(
        effective,
        source_messages,
        boundary_index,
        chat_type=chat_type,
    )
    if len(source_indexes) != len(source_messages):
        return SnapshotBoundary(False, [])
    raw_boundary_index = effective_pairs[boundary_index][0]
    excluded_raw_indexes = {effective_pairs[index][0] for index in source_indexes}
    return SnapshotBoundary(
        True,
        [
            item
            for raw_index, item in enumerate(source[:raw_boundary_index + 1])
            if raw_index not in excluded_raw_indexes
        ],
    )


def _semantic_anchor_index(local, visible, *, chat_type, tail_count):
    max_size = min(tail_count, len(local), len(visible))
    visible_keys = [_semantic_key(item, chat_type=chat_type) for item in visible]
    local_keys = [_semantic_key(item, chat_type=chat_type) for item in local]
    for size in range(max_size, 1, -1):
        sequence = local_keys[-size:]
        if not all(sequence):
            continue
        matches = [
            start
            for start in range(0, len(visible_keys) - size + 1)
            if visible_keys[start:start + size] == sequence
        ]
        if len(matches) == 1:
            return matches[0] + size - 1
    return None


def build_tail_repair_plan(
    local_history,
    visible_tail,
    *,
    chat_type,
    anchor_tail_count=DEFAULT_ANCHOR_TAIL_COUNT,
) -> TailRepairPlan:
    local = [item for item in local_history or [] if _semantic_key(item, chat_type=chat_type)]
    visible = [item for item in visible_tail or [] if _semantic_key(item, chat_type=chat_type)]
    if not visible:
        return TailRepairPlan(False, None, [])

    anchor_index = None
    if local:
        last_native_id = _native_id(local[-1])
        if last_native_id:
            matches = [
                index for index, item in enumerate(visible) if _native_id(item) == last_native_id
            ]
            if len(matches) == 1:
                anchor_index = matches[0]
        if anchor_index is None:
            anchor_index = _semantic_anchor_index(
                local,
                visible,
                chat_type=chat_type,
                tail_count=max(2, int(anchor_tail_count or DEFAULT_ANCHOR_TAIL_COUNT)),
            )

    selected = visible[anchor_index + 1:] if anchor_index is not None else visible
    existing_native_ids = {_native_id(item) for item in local if _native_id(item)}
    selected = [
        item for item in selected if not _native_id(item) or _native_id(item) not in existing_native_ids
    ]
    return TailRepairPlan(anchor_index is not None, anchor_index, selected)
