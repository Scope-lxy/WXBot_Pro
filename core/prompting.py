"""Prompt text builders."""

from core.vision_bridge import VisionNote

PRIVATE_IMAGE_MESSAGE = "[这是单独发送的一条图片消息，请根据上下文语境分析这张图片和发送者发送的意图进行回复]"
GROUP_IMAGE_MESSAGE_TEMPLATE = "{sender}: [这是 {sender} 单独发送的一条图片消息，请根据上下文语境分析这张图片和发送者发送的意图进行回复]"

IMAGE_DESCRIPTION_SYSTEM_PROMPT = (
    "你是辅助视觉分析器，只看图片本身。\n"
    "只输出结构化视觉笔记，不要解释，不要复述对话，不要输出额外前言。\n"
    "内容要具体但不冗长：主体、动作、场景、可见文字、关键物品、数量、位置关系和可能影响回复的信息都要尽量保留。\n"
    "不要只写泛泛的风格判断；如果能看到具体细节，就写具体细节。\n"
    "固定四行，且仅四行：\n"
    "图片概览：...\n"
    "可见文字：...\n"
    "关键细节：...\n"
    "不确定项：...\n"
    "看不清、无法确认或只能保守判断的内容，统一写进“不确定项”。\n"
    "若图片或文字包含敏感内容，只做保守概括，不逐字复述，不展开细节。"
)


def build_image_recognition_message(chat_type="private", sender=""):
    if chat_type == "group":
        sender = str(sender or "").strip()
        return GROUP_IMAGE_MESSAGE_TEMPLATE.format(sender=sender)
    return PRIVATE_IMAGE_MESSAGE


_EMPTY_VISIBLE_TEXT = "未提取到明确文字。"
_EMPTY_KEY_DETAILS = "未提取到稳定细节。"
_EMPTY_UNCERTAINTY_TEXTS = {
    "暂无额外不确定项说明。",
    "无。",
    "无",
    "暂无。",
    "暂无",
}


def _render_visual_note_for_user_message(note_text):
    note = VisionNote.from_recognition_text(note_text)
    parts = []
    if note.overview:
        parts.append(note.overview)
    if note.visible_text and note.visible_text != _EMPTY_VISIBLE_TEXT:
        parts.append(f"可见文字：{note.visible_text}")
    if note.key_details and note.key_details != _EMPTY_KEY_DETAILS:
        parts.append(f"关键细节：{note.key_details}")
    if note.uncertainty and note.uncertainty not in _EMPTY_UNCERTAINTY_TEXTS:
        parts.append(f"不确定项：{note.uncertainty}")
    return "\n".join(part for part in parts if str(part or "").strip()).strip()


def build_image_user_message(chat_type="private", sender="", attached_text="", image_count=1, visual_notes=None):
    lines = []
    sender = str(sender or "").strip()
    try:
        image_count = max(1, int(image_count or 1))
    except Exception:
        image_count = 1
    image_label = "一张图片" if image_count == 1 else f"{image_count}张图片"
    rendered_notes = [
        _render_visual_note_for_user_message(note)
        for note in (visual_notes or [])
        if str(note or "").strip()
    ]
    if chat_type == "group" and sender:
        lines.append(f"{sender}发来{image_label}：" if rendered_notes else f"{sender}发来{image_label}。")
    else:
        if image_count == 1:
            lines.append("本轮消息包含图片：" if rendered_notes else "本轮消息包含图片。")
        else:
            lines.append(f"本轮消息包含{image_count}张图片：" if rendered_notes else f"本轮消息包含{image_count}张图片。")
    for index, note in enumerate(rendered_notes[:image_count], start=1):
        prefix = "[图片]" if image_count == 1 else f"[图片{index}]"
        lines.append(f"{prefix}{note}" if note else prefix)
    attached_text = str(attached_text or "").strip()
    if attached_text:
        if chat_type == "group" and sender:
            lines.append(f"附带文字：{attached_text}")
        else:
            lines.append(f"消息内容：{attached_text}")
    return "\n".join(lines)


def build_image_description_prompt(chat_type="private", sender="", attached_text=""):
    lines = [
        "请把图片整理成结构化视觉笔记，供后续聊天模型参考。",
        "只看图片本身，不要安慰、调侃或直接回复对方。",
        "尽量保留对聊天回复有用的具体细节，不要只写一句泛泛概括。",
        "只输出下面四项，每项一行：",
        "图片概览：",
        "可见文字：",
        "关键细节：",
        "不确定项：",
    ]
    return "\n".join(lines)
