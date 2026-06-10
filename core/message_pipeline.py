"""Pure message pipeline helpers."""

import hashlib
from types import SimpleNamespace

QUOTE_IMAGE_MARKER = "+引用的图片:"
SINGLE_EMOTION_TEXT = "对方发来了一个微信表情（无法识别具体表情内容）"
MULTI_EMOTION_TEXT_TEMPLATE = "对方连续发来了 {count} 个微信表情（无法识别具体表情内容）"
MAX_MERGED_PRIVATE_IMAGES = 9


def message_unique_id(chat_name, message):
    """Build a stable unique id for de-duplicating incoming messages."""
    msg_id = getattr(message, "id", None)
    if msg_id:
        return f"id:{chat_name}:{msg_id}"
    raw = "|".join([
        str(chat_name),
        str(getattr(message, "sender", "")),
        str(getattr(message, "type", "")),
        str(getattr(message, "attr", "")),
        str(getattr(message, "content", "")),
        str(getattr(message, "time", "")),
    ])
    return "hash:" + hashlib.sha1(raw.encode("utf-8", errors="ignore")).hexdigest()


def split_quoted_image_message(content):
    """Split merged text+image content into text and ordered image paths."""
    raw = str(content or "")
    if QUOTE_IMAGE_MARKER not in raw:
        return raw.strip(), []
    text_part, image_block = raw.split(QUOTE_IMAGE_MARKER, 1)
    image_paths = [
        line.strip()
        for line in image_block.replace("\r\n", "\n").replace("\r", "\n").split("\n")
        if line.strip()
    ]
    return text_part.strip(), image_paths


def build_quoted_image_message(text, image_paths):
    """Rebuild merged content from text and one or more image paths."""
    normalized_paths = [str(path or "").strip() for path in image_paths or [] if str(path or "").strip()]
    if not normalized_paths:
        return str(text or "").strip()
    return f"{str(text or '').strip()}{QUOTE_IMAGE_MARKER}" + "\n".join(normalized_paths)


def build_merged_private_message(messages, *, on_extra_image=None):
    """Merge short consecutive private messages into one AI-facing message."""
    text_parts = []
    image_paths = []
    sender = ""
    attr = "friend"
    pending_emotion_count = 0
    contains_voice_message = False
    on_extra_image = on_extra_image or (lambda image_path: None)

    def flush_emotions():
        nonlocal pending_emotion_count
        if pending_emotion_count <= 0:
            return
        if pending_emotion_count == 1:
            text_parts.append(SINGLE_EMOTION_TEXT)
        else:
            text_parts.append(MULTI_EMOTION_TEXT_TEMPLATE.format(count=pending_emotion_count))
        pending_emotion_count = 0

    for msg in messages:
        sender = sender or getattr(msg, "sender", "")
        attr = getattr(msg, "attr", attr)
        msg_type = getattr(msg, "type", "")
        content = str(getattr(msg, "content", "") or "").strip()
        if msg_type == "voice":
            contains_voice_message = True
        if msg_type == "emotion":
            pending_emotion_count += 1
            continue
        if not content:
            continue
        if msg_type == "image":
            flush_emotions()
            if len(image_paths) < MAX_MERGED_PRIVATE_IMAGES:
                image_paths.append(content)
            else:
                on_extra_image(content)
        elif QUOTE_IMAGE_MARKER in content:
            flush_emotions()
            text_part, quoted_paths = split_quoted_image_message(content)
            if text_part.strip():
                text_parts.append(text_part.strip())
            for image_path in quoted_paths:
                if len(image_paths) < MAX_MERGED_PRIVATE_IMAGES:
                    image_paths.append(image_path)
                else:
                    on_extra_image(image_path)
        else:
            flush_emotions()
            text_parts.append(content)
    flush_emotions()

    merged_content = "\n".join(text_parts).strip()
    if image_paths:
        if not merged_content:
            if len(image_paths) == 1:
                return SimpleNamespace(
                    type="image",
                    content=image_paths[0],
                    sender=sender,
                    attr=attr,
                    _contains_voice_message=contains_voice_message,
                )
            merged_content = build_quoted_image_message("", image_paths)
        else:
            merged_content = build_quoted_image_message(merged_content, image_paths)
    return SimpleNamespace(
        type="text",
        content=merged_content,
        sender=sender,
        attr=attr,
        _contains_voice_message=contains_voice_message,
    )
