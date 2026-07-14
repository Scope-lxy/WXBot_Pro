"""Chat context repair helpers for aligning WeChat-visible messages with memory."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

from core.message_pipeline import (
    UNRECOGNIZED_VOICE_TEXT,
    is_failed_voice_transcription_text,
    is_unrecognized_voice_placeholder,
    voice_message_body,
)


DEFAULT_CONTEXT_REPAIR_COOLDOWN_SECONDS = 300
DEFAULT_ANCHOR_RECENT_COUNT = 5
DEFAULT_VISIBLE_LIMIT = 30
DEFAULT_LOCAL_HISTORY_LIMIT = 50
NEARBY_DUPLICATE_WINDOW_SECONDS = 600


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


def normalize_group_self_content(content) -> str:
    text = clean_text(content)
    if text.startswith("@") and "\u2005" in text:
        _mention, _separator, body = text.partition("\u2005")
        body = body.lstrip()
        if body:
            return body
    return text


def message_content_candidates(content, msg_type="", *, allow_voice_shell=False) -> tuple[str, ...]:
    msg_type = normalize_message_type(msg_type)
    text = normalize_message_content(content, msg_type)
    candidates = [text] if text else []
    if msg_type == "voice" and allow_voice_shell:
        body = voice_message_body(text)
        if body and body != text:
            candidates.append(body)
    return tuple(candidates)


def is_unrecognized_voice(item) -> bool:
    if not isinstance(item, dict):
        return False
    if normalize_message_type(item.get("type")) != "voice":
        return False
    content = normalize_message_content(item.get("content"), "voice")
    return (
        not voice_message_body(content)
        or content == UNRECOGNIZED_VOICE_TEXT
        or is_failed_voice_transcription_text(content)
        or is_unrecognized_voice_placeholder(content)
    )


def _anchor_direction(item) -> str:
    attr = clean_text(item.get("attr")).lower() if isinstance(item, dict) else ""
    sender = clean_text(item.get("sender")).lower() if isinstance(item, dict) else ""
    return "self" if attr == "self" or sender in {"self", "me"} else "other"


def relaxed_duplicate_keys(item, *, chat_type="private", allow_voice_shell=False) -> set[str]:
    if not isinstance(item, dict):
        return set()
    msg_type = normalize_message_type(item.get("type"))
    direction = _anchor_direction(item)
    normalized_chat_type = clean_text(chat_type).lower()
    parts = [direction]
    if normalized_chat_type == "group" and direction != "self":
        sender = clean_text(item.get("sender"))
        if not sender:
            return set()
        parts.append(sender)
    parts.append(msg_type)
    contents = message_content_candidates(
        item.get("content"),
        msg_type,
        allow_voice_shell=allow_voice_shell,
    )
    if normalized_chat_type == "group" and direction == "self":
        contents = tuple(dict.fromkeys(
            [*contents, *(normalize_group_self_content(content) for content in contents)]
        ))
    return {
        "|".join(parts + [content])
        for content in contents
    }


def parse_message_time(value):
    text = clean_text(value)
    if not text:
        return None
    for fmt in ("%Y/%m/%d %H:%M:%S", "%Y-%m-%d %H:%M:%S"):
        try:
            return datetime.strptime(text, fmt)
        except Exception:
            pass
    return None


def message_fingerprints(item, *, allow_voice_shell=False) -> tuple[str, ...]:
    if not isinstance(item, dict):
        return ()
    msg_type = normalize_message_type(item.get("type"))
    contents = message_content_candidates(
        item.get("content"),
        msg_type,
        allow_voice_shell=allow_voice_shell,
    )
    if not contents and msg_type in {"image", "emotion", "voice", "video", "file"}:
        contents = ("",)
    parts = [
        clean_text(item.get("attr")).lower(),
        clean_text(item.get("sender")),
        msg_type,
    ]
    return tuple(
        "|".join(parts + [content])
        for content in contents
    )


def message_fingerprint(item) -> str:
    fingerprints = message_fingerprints(item)
    return fingerprints[0] if fingerprints else ""


def repair_anchor_fingerprint(item, *, chat_type="private") -> str:
    if not isinstance(item, dict):
        return ""
    msg_type = normalize_message_type(item.get("type"))
    if msg_type == "voice":
        return ""
    content = normalize_message_content(item.get("content"), msg_type)
    if not content and msg_type not in {"image", "emotion", "video", "file"}:
        return ""
    direction = _anchor_direction(item)
    if clean_text(chat_type).lower() == "group" and direction == "self":
        content = normalize_group_self_content(content)
    parts = [direction]
    if clean_text(chat_type).lower() == "group" and direction != "self":
        sender = clean_text(item.get("sender"))
        if not sender:
            return ""
        parts.append(sender)
    parts.extend([msg_type, content])
    return "|".join(parts)


def unique_message_key(item) -> str:
    if not isinstance(item, dict):
        return ""
    fp = message_fingerprint(item)
    if not fp:
        return ""
    message_time = clean_text(item.get("time"))
    return f"{message_time}|{fp}" if message_time else fp


def unique_message_keys(item, *, allow_voice_shell=False) -> set[str]:
    if not isinstance(item, dict):
        return set()
    message_time = clean_text(item.get("time"))
    return {
        f"{message_time}|{fingerprint}" if message_time else fingerprint
        for fingerprint in message_fingerprints(
            item,
            allow_voice_shell=allow_voice_shell,
        )
    }


def message_for_storage(item) -> dict:
    entry = dict(item)
    if normalize_message_type(entry.get("type")) == "voice":
        content = clean_text(entry.get("content"))
        entry["content"] = voice_message_body(content) or content
    return entry


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


def _assign_snapshot_times_before(entries, indexes, anchor_time, *, include_anchor=False):
    anchor_dt = parse_message_time(anchor_time)
    if not anchor_dt or not indexes:
        return
    use_dash = "-" in clean_text(anchor_time)
    fmt = "%Y-%m-%d %H:%M:%S" if use_dash else "%Y/%m/%d %H:%M:%S"
    last_offset = len(indexes) - 1 if include_anchor else len(indexes)
    for position, index in enumerate(indexes):
        entries[index]["time"] = (anchor_dt - timedelta(seconds=last_offset - position)).strftime(fmt)
        entries[index]["time_inferred"] = True


def normalize_wechat_snapshot(
    messages,
    *,
    source="wechat_context_repair",
    fallback_tail_time="",
) -> list[dict]:
    entries = []
    latest_time_marker = ""
    leading_without_time = []
    for message in messages or []:
        msg_type = normalize_message_type(getattr(message, "type", "text"))
        raw_time = clean_text(getattr(message, "time", ""))
        if msg_type == "time":
            if not latest_time_marker and leading_without_time:
                _assign_snapshot_times_before(entries, leading_without_time, raw_time)
                leading_without_time = []
            latest_time_marker = raw_time or latest_time_marker
            continue
        entry = normalize_wechat_message(message, source=source)
        entry["time"] = raw_time or latest_time_marker
        entries.append(entry)
        if not entry["time"]:
            leading_without_time.append(len(entries) - 1)
    if leading_without_time:
        tail_time = clean_text(fallback_tail_time) or datetime.now().strftime("%Y/%m/%d %H:%M:%S")
        _assign_snapshot_times_before(
            entries,
            leading_without_time,
            tail_time,
            include_anchor=True,
        )
    return entries


def snapshot_messages_through_current(messages, current_message):
    source = list(messages or [])
    current_id = clean_text(getattr(current_message, "id", ""))
    current_hash_text = clean_text(getattr(current_message, "hash_text", ""))
    current_entry = normalize_wechat_message(current_message, source="current_snapshot_tail")
    current_fps = set(message_fingerprints(current_entry))
    matched_raw_index = None
    for index, item in enumerate(source):
        if normalize_message_type(getattr(item, "type", "text")) == "time":
            continue
        item_id = clean_text(getattr(item, "id", ""))
        if current_id and item_id and item_id == current_id:
            return source[:index + 1]
        item_hash_text = clean_text(getattr(item, "hash_text", ""))
        if current_hash_text and item_hash_text and item_hash_text == current_hash_text:
            return source[:index + 1]
        item_fps = set(
            message_fingerprints(
                normalize_wechat_message(item),
                allow_voice_shell=True,
            )
        )
        if current_fps and current_fps.intersection(item_fps):
            matched_raw_index = index
    if matched_raw_index is None:
        return source
    return source[:matched_raw_index + 1]


def filter_model_repair_messages(messages) -> list[dict]:
    result = []
    for item in messages or []:
        if not isinstance(item, dict):
            continue
        if is_unrecognized_voice(item):
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


def recent_repair_anchor_fingerprints(messages, count, *, chat_type="private") -> list[str]:
    try:
        count = max(1, int(count or DEFAULT_ANCHOR_RECENT_COUNT))
    except Exception:
        count = DEFAULT_ANCHOR_RECENT_COUNT
    fingerprints = []
    for item in filter_model_repair_messages(messages):
        fp = repair_anchor_fingerprint(item, chat_type=chat_type)
        if fp:
            fingerprints.append(fp)
    return fingerprints[-count:]


def find_anchor_index(
    local_history,
    remote_history,
    *,
    anchor_recent_count=DEFAULT_ANCHOR_RECENT_COUNT,
    chat_type="private",
) -> int | None:
    remote = filter_model_repair_messages(remote_history)
    if not remote:
        return None
    remote_anchor_pairs = [
        (index, repair_anchor_fingerprint(item, chat_type=chat_type))
        for index, item in enumerate(remote)
    ]
    remote_anchor_pairs = [(index, fp) for index, fp in remote_anchor_pairs if fp]
    remote_fps = [fp for _, fp in remote_anchor_pairs]
    local_fps = recent_repair_anchor_fingerprints(
        local_history,
        anchor_recent_count,
        chat_type=chat_type,
    )
    max_sequence = min(len(local_fps), len(remote_fps))
    for size in range(max_sequence, 1, -1):
        sequence = local_fps[-size:]
        for start in range(len(remote_fps) - size, -1, -1):
            if _tail_sequence_match(remote_fps, sequence, start):
                return remote_anchor_pairs[start + size - 1][0]

    if local_fps:
        last_fp = local_fps[-1]
        matches = [index for index, fp in remote_anchor_pairs if fp == last_fp]
        if len(matches) == 1:
            return matches[0]
    return None


def build_repair_plan(
    local_history,
    remote_history,
    *,
    anchor_recent_count=DEFAULT_ANCHOR_RECENT_COUNT,
    chat_type="private",
) -> RepairPlan:
    remote = filter_model_repair_messages(remote_history)
    anchor = find_anchor_index(
        local_history,
        remote,
        anchor_recent_count=anchor_recent_count,
        chat_type=chat_type,
    )
    local_messages = filter_model_repair_messages(local_history)
    unmatched_local = set(range(len(local_messages)))
    messages_to_append = []
    for remote_item in remote:
        exact_keys = unique_message_keys(remote_item, allow_voice_shell=True)
        exact_match = next(
            (
                index
                for index in unmatched_local
                if exact_keys.intersection(unique_message_keys(local_messages[index]))
            ),
            None,
        )
        if exact_match is not None:
            unmatched_local.remove(exact_match)
            continue

        relaxed_keys = relaxed_duplicate_keys(
            remote_item,
            chat_type=chat_type,
            allow_voice_shell=True,
        )
        remote_time = parse_message_time(remote_item.get("time"))
        nearby_matches = []
        if relaxed_keys and remote_time:
            for index in unmatched_local:
                local_item = local_messages[index]
                local_time = parse_message_time(local_item.get("time"))
                local_keys = relaxed_duplicate_keys(local_item, chat_type=chat_type)
                if not local_time or not relaxed_keys.intersection(local_keys):
                    continue
                delta = abs((remote_time - local_time).total_seconds())
                if delta <= NEARBY_DUPLICATE_WINDOW_SECONDS:
                    nearby_matches.append((delta, index))
        if nearby_matches:
            _, nearby_match = min(nearby_matches)
            unmatched_local.remove(nearby_match)
            continue
        if remote_item.get("time_inferred") and relaxed_keys:
            inferred_match = next(
                (
                    index
                    for index in sorted(unmatched_local, reverse=True)
                    if relaxed_keys.intersection(
                        relaxed_duplicate_keys(local_messages[index], chat_type=chat_type)
                    )
                ),
                None,
            )
            if inferred_match is not None:
                unmatched_local.remove(inferred_match)
                continue
        messages_to_append.append(message_for_storage(remote_item))
    return RepairPlan(
        anchor_index=anchor,
        messages_to_append=messages_to_append,
        anchor_found=anchor is not None,
    )


def current_message_found_near_tail(local_history, current_message, *, tail_count=5) -> bool:
    source_messages = getattr(current_message, "_merged_source_messages", None)
    candidates = []
    if source_messages:
        try:
            source_iter = list(source_messages)
        except TypeError:
            source_iter = []
        for source_message in source_iter:
            entry = normalize_wechat_message(source_message, source="current_source")
            if message_fingerprint(entry):
                candidates.append(entry)
    if not candidates:
        current_entry = normalize_wechat_message(current_message, source="current")
        if message_fingerprint(current_entry):
            candidates.append(current_entry)
    if not candidates:
        return False
    tail = recent_effective_messages(local_history, max(tail_count, len(candidates)))
    unmatched_tail = set(range(len(tail)))
    for current_entry in candidates:
        current_keys = unique_message_keys(current_entry)
        current_fps = set(message_fingerprints(current_entry))
        matched_index = next(
            (
                index
                for index in unmatched_tail
                if (
                    current_keys.intersection(unique_message_keys(tail[index]))
                    or current_fps.intersection(message_fingerprints(tail[index]))
                )
            ),
            None,
        )
        if matched_index is None:
            return False
        unmatched_tail.remove(matched_index)
    return True
