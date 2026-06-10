"""Prompt text builders."""

PRIVATE_IMAGE_MESSAGE = "[这是单独发送的一条图片消息，请根据上下文语境分析这张图片和发送者发送的意图进行回复]"
GROUP_IMAGE_MESSAGE_TEMPLATE = "{sender}: [这是 {sender} 单独发送的一条图片消息，请根据上下文语境分析这张图片和发送者发送的意图进行回复]"

IMAGE_DESCRIPTION_SYSTEM_PROMPT = (
    "你是辅助视觉分析器。"
    "只输出结构化视觉笔记，不要输出额外解释。"
    "固定输出四项，每项一行："
    "图片概览：... "
    "可见文字：... "
    "关键细节：... "
    "不确定项：... "
    "看不清、无法确认或只能保守判断的内容，统一写进“不确定项”。"
    "若图片或文字包含色情暴力等敏感内容，只做保守概括，不逐字复述，不展开细节，不要直接输出安全拒绝。"
)


def build_image_recognition_message(chat_type="private", sender=""):
    if chat_type == "group":
        sender = str(sender or "").strip()
        return GROUP_IMAGE_MESSAGE_TEMPLATE.format(sender=sender)
    return PRIVATE_IMAGE_MESSAGE


def build_image_user_message(chat_type="private", sender="", attached_text="", image_count=1):
    lines = []
    sender = str(sender or "").strip()
    try:
        image_count = max(1, int(image_count or 1))
    except Exception:
        image_count = 1
    image_label = "一张图片" if image_count == 1 else f"{image_count}张图片"
    if chat_type == "group" and sender:
        lines.append(f"{sender}发来{image_label}。")
    else:
        lines.append("本轮消息包含图片。" if image_count == 1 else f"本轮消息包含{image_count}张图片。")
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
        "不要代入人设，不要安慰、调侃或直接回复对方。",
        "只输出下面四项，每项一行：",
        "图片概览：",
        "可见文字：",
        "关键细节：",
        "不确定项：",
    ]
    sender = str(sender or "").strip()
    attached_text = str(attached_text or "").strip()
    if chat_type == "group" and sender:
        lines.append(f"发送者：{sender}")
    if attached_text:
        label = "发送者" if chat_type == "group" else "对方"
        lines.append(f"{label}附带文字：{attached_text}")
    return "\n".join(lines)
