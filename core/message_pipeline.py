"""Pure message pipeline helpers."""

import hashlib
import re
from types import SimpleNamespace

QUOTE_IMAGE_MARKER = "+引用的图片:"
SINGLE_EMOTION_TEXT = "对方发来了一个微信表情（无法识别具体表情内容）"
MULTI_EMOTION_TEXT_TEMPLATE = "对方连续发来了 {count} 个微信表情（无法识别具体表情内容）"
MAX_MERGED_PRIVATE_IMAGES = 9
VOICE_DURATION_PREFIX_RE = re.compile(r'^\s*语音\s*\d+\s*["”]?\s*秒\s*')
LEADING_VOICE_LABEL_RE = re.compile(r'^\s*\[语音\]\s*')
UNRECOGNIZED_VOICE_TEXT = "一条语音消息（未识别出文字）"
VOICE_TRANSCRIPTION_FAILED_TEXTS = {
    "语音未能转换",
    "语音转换失败",
    "语音识别失败",
}
MESSAGE_TYPE_LABELS = {
    "voice": "语音",
    "emotion": "微信表情",
    "image": "图片",
    "link": "链接",
    "miniapp": "小程序",
    "personal_card": "个人名片",
    "note": "笔记",
    "location": "位置",
    "merge": "聊天记录",
    "video": "视频",
    "file": "文件",
}
MESSAGE_TYPE_PREFIX_PATTERNS = {
    "link": re.compile(r'^\s*(?:\[(?:链接|网页链接)\]|链接|网页链接)\s*'),
    "miniapp": re.compile(r'^\s*(?:\[(?:小程序|小程序卡片)\]|小程序)\s*'),
    "personal_card": re.compile(r'^\s*(?:\[(?:个人名片|名片)\]|个人名片|名片|好友名片)\s*'),
    "note": re.compile(r'^\s*(?:\[(?:笔记)\]|笔记)\s*'),
    "location": re.compile(r'^\s*(?:\[(?:位置)\]|位置)\s*'),
    "merge": re.compile(r'^\s*(?:\[(?:聊天记录|合并转发)\]|聊天记录|合并转发)\s*'),
    "video": re.compile(r'^\s*(?:\[(?:视频|视频号)\]|视频号|视频)\s*'),
}
VIDEO_DURATION_SUFFIX_RE = re.compile(r'\s*(\d+:\d+)\s*$')
LOCAL_PATH_RE = re.compile(r'^(?:[A-Za-z]:[\\/]|\\\\|/|file://)', re.IGNORECASE)


def strip_voice_duration_metadata(content):
    """Remove wxauto's leading voice-duration UI text when transcription text follows."""
    text = str(content or "").replace("\r\n", "\n").replace("\r", "\n").strip()
    if not text:
        return ""
    match = VOICE_DURATION_PREFIX_RE.match(text)
    if not match:
        return text
    tail = text[match.end():].strip()
    return tail or text


def strip_leading_voice_label(content):
    text = str(content or "").replace("\r\n", "\n").replace("\r", "\n").strip()
    if not text:
        return ""
    return LEADING_VOICE_LABEL_RE.sub("", text, count=1).strip()


def voice_message_body(content):
    text = strip_leading_voice_label(content)
    if not text:
        return ""
    match = VOICE_DURATION_PREFIX_RE.match(text)
    if match:
        return strip_leading_voice_label(text[match.end():].strip())
    return text


def is_failed_voice_transcription_text(content):
    return voice_message_body(content) in VOICE_TRANSCRIPTION_FAILED_TEXTS


def is_unrecognized_voice_placeholder(content):
    body = voice_message_body(content)
    if not body:
        return False
    lowered = body.lower()
    return body == UNRECOGNIZED_VOICE_TEXT or lowered.startswith("<") or "voicemsg" in lowered


def readable_emotion_text(content):
    text = str(content or "").replace("\r\n", "\n").replace("\r", "\n").strip()
    if not text or len(text) > 120:
        return ""
    lowered = text.lower()
    if lowered.startswith("<") or "msg>" in lowered or ("emoji" in lowered and "<" in lowered):
        return ""
    text = re.sub(r"^(动画表情|表情)\s*[:：]?\s*", "", text).strip()
    return text


def message_type_label(msg_type):
    msg_type = str(msg_type or "").strip().lower()
    return MESSAGE_TYPE_LABELS.get(msg_type, msg_type)


def strip_message_shell(content, msg_type):
    text = str(content or "").replace("\r\n", "\n").replace("\r", "\n").strip()
    msg_type = str(msg_type or "").strip().lower()
    if not text:
        return ""
    if msg_type == "voice":
        match = VOICE_DURATION_PREFIX_RE.match(text)
        if match:
            tail = text[match.end():].strip()
            return tail
        return text
    if msg_type == "emotion":
        return readable_emotion_text(text)
    pattern = MESSAGE_TYPE_PREFIX_PATTERNS.get(msg_type)
    if pattern:
        text = pattern.sub("", text).strip()
    if msg_type in {"image", "file"} and LOCAL_PATH_RE.match(text):
        return ""
    if msg_type == "video":
        duration = ""
        duration_match = VIDEO_DURATION_SUFFIX_RE.search(text)
        if duration_match:
            duration = duration_match.group(1).strip()
            text = text[:duration_match.start()].strip()
        text = re.sub(r'^\s*下载\s*', '', text).strip()
        if duration:
            text = duration if not text else f"{text} {duration}"
        if text in {"下载", "视频"}:
            text = ""
    return text


def format_message_semantic_text(message, *, compact=False):
    item = message if isinstance(message, dict) else {}
    msg_type = str(item.get("type", "") or getattr(message, "type", "") or "").strip().lower()
    raw = str(item.get("content", "") or getattr(message, "content", "") or "").replace("\r\n", "\n").replace("\r", "\n").strip()
    label = message_type_label(msg_type)
    if msg_type in {"voice", "emotion", "image", "file", "link", "miniapp", "personal_card", "note", "location", "merge", "video"}:
        body = strip_message_shell(raw, msg_type)
        if body:
            return f"[{label}]{body}"
        return f"[{label}]"
    return raw


def format_model_message_text(message):
    item = message if isinstance(message, dict) else {}
    msg_type = str(item.get("type", "") or getattr(message, "type", "") or "").strip().lower()
    raw = str(item.get("content", "") or getattr(message, "content", "") or "").replace("\r\n", "\n").replace("\r", "\n").strip()
    if msg_type == "voice":
        body = voice_message_body(raw)
        if is_failed_voice_transcription_text(raw) or is_unrecognized_voice_placeholder(raw):
            return ""
        return body or UNRECOGNIZED_VOICE_TEXT
    if msg_type in {"", "text"}:
        return strip_leading_voice_label(raw)
    return format_message_semantic_text(message)


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


def message_content_fingerprint(chat_name, message):
    """Build a short-lived fingerprint for duplicate callbacks of the same message."""
    raw = "|".join([
        str(chat_name).strip(),
        str(getattr(message, "sender", "")).strip(),
        str(getattr(message, "type", "")).strip(),
        str(getattr(message, "attr", "")).strip(),
        str(getattr(message, "content", "")).strip(),
    ])
    return "content:" + hashlib.sha1(raw.encode("utf-8", errors="ignore")).hexdigest()


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
        semantic_text = format_message_semantic_text(msg)
        if msg_type == "voice":
            model_voice_text = format_model_message_text(msg)
            if not model_voice_text:
                continue
            contains_voice_message = True
            content = strip_voice_duration_metadata(content)
        if msg_type == "emotion":
            readable_emotion = readable_emotion_text(content)
            if readable_emotion:
                flush_emotions()
                text_parts.append(semantic_text)
            else:
                pending_emotion_count += 1
            continue
        if msg_type in {"link", "miniapp", "personal_card", "note", "video", "voice"}:
            flush_emotions()
            if semantic_text:
                text_parts.append(model_voice_text if msg_type == "voice" else semantic_text)
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
