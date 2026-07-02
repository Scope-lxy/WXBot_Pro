"""Helpers for converting stored chat history into UI/model-visible context."""

import re

from core.message_pipeline import (
    format_message_semantic_text,
    readable_emotion_text,
    split_quoted_image_message,
)
from core.vision_bridge import VisionNote


_EMPTY_VISIBLE_TEXT = "未提取到明确文字。"
_EMPTY_KEY_DETAILS = "未提取到稳定细节。"
_IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp", ".heic", ".heif"}
_VIDEO_EXTS = {".mp4", ".mov", ".avi", ".mkv", ".wmv", ".flv", ".webm", ".m4v"}
DEFAULT_MODEL_HISTORY_MEDIA_LIMIT = 3
_SEMANTIC_MESSAGE_TYPES = {"voice", "emotion", "link", "miniapp", "personal_card", "note", "video"}
_TIME_SEPARATOR_RE = re.compile(
    r"^(?:"
    r"\d{1,2}:\d{2}"
    r"|(?:今天|昨天|前天|星期[一二三四五六日天]|周[一二三四五六日天])\s*(?:上午|下午|晚上|凌晨|早上)?\s*\d{1,2}:\d{2}"
    r"|\d{4}[/-]\d{1,2}[/-]\d{1,2}\s+\d{1,2}:\d{2}"
    r"|\d{4}年\d{1,2}月\d{1,2}日\s*(?:上午|下午|晚上|凌晨|早上)?\s*\d{1,2}:\d{2}"
    r")$"
)


def _clean_text(value):
    return str(value or "").replace("\r\n", "\n").replace("\r", "\n").strip()


def _normalized_visual_notes(item):
    item = item if isinstance(item, dict) else {}
    notes = []
    raw_notes = item.get("visual_notes")
    if isinstance(raw_notes, list):
        notes.extend(_clean_text(note) for note in raw_notes if _clean_text(note))
    single = _clean_text(item.get("visual_note"))
    if single and single not in notes:
        notes.insert(0, single)
    return notes


def _normalized_image_paths(item):
    item = item if isinstance(item, dict) else {}
    paths = item.get("image_paths")
    if isinstance(paths, list):
        return [_clean_text(path) for path in paths if _clean_text(path)]
    raw = _clean_text(item.get("content"))
    if not raw or raw == "[图片]":
        return []
    _text_part, quoted_paths = split_quoted_image_message(raw)
    return [_clean_text(path) for path in quoted_paths if _clean_text(path)]


def _render_visual_note_summary(note_text, *, compact=False):
    note = VisionNote.from_recognition_text(note_text)
    parts = [note.overview]
    if note.visible_text and note.visible_text != _EMPTY_VISIBLE_TEXT:
        parts.append(f"可见文字：{note.visible_text}")
    if note.key_details and note.key_details != _EMPTY_KEY_DETAILS:
        parts.append(f"关键细节：{note.key_details}")
    return (" " if compact else "\n").join(parts)


def _render_visual_summaries(notes, *, compact=False):
    rendered = [
        _render_visual_note_summary(note_text, compact=compact)
        for note_text in (notes or [])
        if _clean_text(note_text)
    ]
    if not rendered:
        return ""
    return (" " if compact else "\n").join(rendered)


def _path_media_kind(path):
    raw = _clean_text(path).lower()
    if "." not in raw:
        return "file"
    suffix = "." + raw.rsplit(".", 1)[-1]
    if suffix in _IMAGE_EXTS:
        return "image"
    if suffix in _VIDEO_EXTS:
        return "video"
    return "file"


def _media_kind_from_record(item, image_paths=None):
    item = item if isinstance(item, dict) else {}
    msg_type = str(item.get("type", "") or "").strip().lower()
    if msg_type in {"image", "video", "file"}:
        return msg_type
    paths = [path for path in (image_paths or []) if _clean_text(path)]
    if not paths:
        return "file"
    kinds = {_path_media_kind(path) for path in paths}
    return kinds.pop() if len(kinds) == 1 else "file"


def _media_word(kind):
    if kind == "image":
        return "图片"
    if kind == "video":
        return "视频"
    if kind == "emotion":
        return "微信表情"
    return "文件"


def _media_message_text(kind, count, *, speaker_role="user", compact=False):
    word = _media_word(kind)
    if compact:
        return f"[{word}]" if count <= 1 else f"[{word} x{count}]"
    if kind == "emotion":
        action = "发出" if speaker_role == "assistant" else "发来"
        return f"{action}一个微信表情（无法识别具体内容）。"
    if kind == "image":
        count_text = "一张" if count <= 1 else f"{count}张"
    else:
        count_text = "一个" if count <= 1 else f"{count}个"
    action = "发出" if speaker_role == "assistant" else "发来"
    return f"{action}{count_text}{word}。"


def _render_emotion_message(raw, *, speaker_role="user", compact=False):
    text = readable_emotion_text(raw)
    if compact:
        return f"表情：{text}" if text else "[微信表情]"
    action = "发出" if speaker_role == "assistant" else "发来"
    if text:
        return f"{action}一个微信表情：{text}"
    return f"{action}一个微信表情（无法识别具体内容）。"


def _render_media_message(item, raw, notes, *, speaker_role="user", compact=False):
    kind = _media_kind_from_record(item)
    if kind == "image":
        summary = _render_visual_summaries(notes, compact=compact)
        if compact:
            return f"[图片] {summary}" if summary else "[图片]"
        return f"[图片]\n{summary}".strip() if summary else "[图片]"
    content = _media_message_text(kind, 1, speaker_role=speaker_role, compact=compact)
    summary = _render_visual_summaries(notes, compact=compact)
    if summary:
        return f"{content} {summary}".strip() if compact else f"{content}\n{summary}".strip()
    return content


def _render_quoted_media_message(item, raw, notes, *, speaker_role="user", compact=False):
    text_part, image_paths = split_quoted_image_message(raw)
    image_count = len(image_paths) or 1
    kind = _media_kind_from_record(item, image_paths=image_paths)
    word = _media_word(kind)
    image_text = _media_message_text(kind, image_count, speaker_role=speaker_role, compact=compact)
    prefix = text_part.strip()
    if not compact:
        if prefix:
            unit = "一张" if kind == "image" and image_count <= 1 else (
                f"{image_count}张" if kind == "image" else ("一个" if image_count <= 1 else f"{image_count}个")
            )
            prefix = f"{prefix}\n附带{unit}{word}。"
        else:
            prefix = _media_message_text(kind, image_count, speaker_role=speaker_role, compact=False)
    else:
        prefix = " ".join(part for part in (prefix, image_text) if part).strip()
    summary = _render_visual_summaries(notes, compact=compact)
    if summary:
        return f"{prefix} {summary}".strip() if compact else f"{prefix}\n{summary}".strip()
    return prefix or image_text


def _render_record_content(item, *, speaker_role="user", compact=False):
    item = item if isinstance(item, dict) else {}
    msg_type = str(item.get("type", "") or "").strip().lower()
    raw = _clean_text(item.get("content"))
    notes = _normalized_visual_notes(item)
    image_paths = _normalized_image_paths(item)
    if msg_type in _SEMANTIC_MESSAGE_TYPES:
        return format_message_semantic_text(item)
    if msg_type in {"image", "video", "file"}:
        return _render_media_message(item, raw, notes, speaker_role=speaker_role, compact=compact)
    if image_paths:
        return _render_media_message({**item, "type": "image", "image_paths": image_paths}, raw, notes, speaker_role=speaker_role, compact=compact)
    if raw:
        _text_part, image_paths = split_quoted_image_message(raw)
        if image_paths:
            return _render_quoted_media_message(item, raw, notes, speaker_role=speaker_role, compact=compact)
    return raw


def _is_media_record(item):
    if not isinstance(item, dict):
        return False
    msg_type = str(item.get("type", "") or "").strip().lower()
    if msg_type in {"image", "video", "file"}:
        return True
    raw = _clean_text(item.get("content"))
    if not raw:
        return False
    _text_part, image_paths = split_quoted_image_message(raw)
    return bool(image_paths)


def _is_time_separator_record(item):
    if not isinstance(item, dict):
        return False
    attr = str(item.get("attr", "") or "").strip().lower()
    msg_type = str(item.get("type", "") or "").strip().lower()
    content = _clean_text(item.get("content"))
    if attr != "system" or not content:
        return False
    return msg_type == "time" or bool(_TIME_SEPARATOR_RE.match(content))


def _is_model_visible_record(item):
    if not isinstance(item, dict):
        return True
    attr = str(item.get("attr", "") or "").strip().lower()
    if attr == "system":
        return False
    return True


def _attach_time_separators(history):
    visible = []
    pending_time = ""
    for item in history or []:
        if _is_time_separator_record(item):
            pending_time = _clean_text(item.get("content"))
            continue
        if not _is_model_visible_record(item):
            continue
        if pending_time and isinstance(item, dict):
            item = dict(item)
            item["_history_time"] = pending_time
            pending_time = ""
        visible.append(item)
    return visible


def limit_recent_media_records(history, media_limit=DEFAULT_MODEL_HISTORY_MEDIA_LIMIT):
    try:
        media_limit = int(media_limit)
    except (TypeError, ValueError):
        media_limit = DEFAULT_MODEL_HISTORY_MEDIA_LIMIT
    if media_limit < 0:
        return list(history or [])
    kept_reversed = []
    media_seen = 0
    for item in reversed(list(history or [])):
        if _is_media_record(item):
            if media_seen >= media_limit:
                continue
            media_seen += 1
        kept_reversed.append(item)
    return list(reversed(kept_reversed))


def build_model_visible_history(history, *, assistant_limit=None, media_limit=DEFAULT_MODEL_HISTORY_MEDIA_LIMIT):
    visible_history = _attach_time_separators(history)
    visible_history = limit_recent_media_records(visible_history, media_limit=media_limit)
    skipped_assistant = 0
    if assistant_limit is not None:
        visible_history, skipped_assistant = filter_model_visible_history(
            visible_history,
            assistant_limit=assistant_limit,
        )
    return [format_history_message(item) for item in visible_history], skipped_assistant


def format_memory_record_for_display(item):
    item = dict(item or {})
    speaker_role = "assistant" if item.get("attr") == "self" else "user"
    raw_content = _clean_text(item.get("content"))
    display_content = _render_record_content(item, speaker_role=speaker_role, compact=True)
    item["raw_content"] = raw_content
    item["content"] = display_content
    return item


def format_history_message(h):
    """Format stored history into model context without leaking storage-only timestamps."""
    explicit_role = str(h.get("role", "") or "").strip().lower()
    if explicit_role in {"system", "user", "assistant"} and "attr" not in h:
        role = explicit_role
    else:
        role = "assistant" if h.get("attr") == "self" else "user"
    raw = _render_record_content(h, speaker_role=role, compact=False)
    history_time = _clean_text(h.get("_history_time")) if isinstance(h, dict) else ""
    sender = h.get("sender", "")
    if role == "user" and sender:
        content = f"{sender}: {raw}"
    else:
        content = str(raw)
    if history_time:
        content = f"发送时间：{history_time}\n{content}"
    return {"role": role, "content": content}


def filter_model_visible_history(history, assistant_limit=3):
    """Keep user history intact while limiting old assistant replies used as examples."""
    try:
        assistant_limit = max(0, int(assistant_limit))
    except Exception:
        assistant_limit = 3
    messages = list(history or [])
    kept_reversed = []
    assistant_seen = 0
    skipped_assistant = 0
    for item in reversed(messages):
        if not isinstance(item, dict):
            kept_reversed.append(item)
            continue
        is_assistant = item.get("attr") == "self" or item.get("role") == "assistant"
        if is_assistant:
            if assistant_seen < assistant_limit:
                kept_reversed.append(item)
                assistant_seen += 1
            else:
                skipped_assistant += 1
            continue
        kept_reversed.append(item)
    return list(reversed(kept_reversed)), skipped_assistant
