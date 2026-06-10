"""Helpers for converting stored chat history into UI/model-visible context."""

from core.message_pipeline import split_quoted_image_message
from core.vision_bridge import VisionNote


_EMPTY_VISIBLE_TEXT = "未提取到明确文字。"
_EMPTY_KEY_DETAILS = "未提取到稳定细节。"
_IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp", ".heic", ".heif"}
_VIDEO_EXTS = {".mp4", ".mov", ".avi", ".mkv", ".wmv", ".flv", ".webm", ".m4v"}
DEFAULT_MODEL_HISTORY_MEDIA_LIMIT = 3


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


def _render_visual_note_summary(note_text, *, compact=False):
    note = VisionNote.from_recognition_text(note_text)
    parts = [f"图片概览：{note.overview}"]
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
    return "文件"


def _media_message_text(kind, count, *, speaker_role="user", compact=False):
    word = _media_word(kind)
    if compact:
        return f"[{word}]" if count <= 1 else f"[{word} x{count}]"
    if kind == "image":
        count_text = "一张" if count <= 1 else f"{count}张"
    else:
        count_text = "一个" if count <= 1 else f"{count}个"
    action = "发出" if speaker_role == "assistant" else "发来"
    return f"{action}{count_text}{word}。"


def _render_media_message(item, raw, notes, *, speaker_role="user", compact=False):
    kind = _media_kind_from_record(item)
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
    if msg_type in {"image", "video", "file"}:
        return _render_media_message(item, raw, notes, speaker_role=speaker_role, compact=compact)
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
    visible_history = limit_recent_media_records(history, media_limit=media_limit)
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
    sender = h.get("sender", "")
    if role == "user" and sender:
        content = f"{sender}: {raw}"
    else:
        content = str(raw)
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
