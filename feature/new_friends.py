"""New-friend business rules kept separate from wxautox execution calls."""

from datetime import datetime
import unicodedata

MAX_NEW_FRIEND_MESSAGE_FILES = 9


def normalize_new_friend_welcome_messages(value):
    """Return one clean welcome message block with text and files."""
    if not isinstance(value, dict):
        return {"text": "", "files": []}
    text = str(value.get("text") or "").strip()
    files = []
    raw_files = value.get("files")
    if isinstance(raw_files, list):
        for path in raw_files:
            normalized_path = str(path or "").strip()
            if not normalized_path:
                continue
            files.append(normalized_path)
            if len(files) >= MAX_NEW_FRIEND_MESSAGE_FILES:
                break
    return {"text": text, "files": files}


def new_friend_welcome_message_has_content(value):
    message = normalize_new_friend_welcome_messages(value)
    return bool(message.get("text") or message.get("files"))


def iter_new_friend_welcome_actions(messages):
    """Yield ordered send actions for the welcome message block."""
    message = normalize_new_friend_welcome_messages(messages)
    text = message.get("text") or ""
    if text:
        yield {"type": "text", "content": text}
    for path in message.get("files") or []:
        if path:
            yield {"type": "file", "path": path}


def new_friend_welcome_message_summary(message):
    message = normalize_new_friend_welcome_messages(message)
    text = str(message.get("text") or "").strip()
    files = [str(path or "").strip() for path in (message.get("files") or []) if str(path or "").strip()]
    parts = []
    if text:
        parts.append(text)
    if files:
        parts.append(f"文件 {len(files)} 个")
    return " + ".join(parts) or "（空）"


def remark_unit_len(text):
    """Approximate WeChat remark length: ASCII as 1 unit, CJK/other as 2."""
    total = 0
    for char in str(text or ""):
        try:
            total += len(char.encode("gbk"))
        except UnicodeEncodeError:
            total += 2
    return total


def truncate_remark_units(text, max_units):
    """Truncate text by WeChat remark units without splitting characters."""
    if max_units <= 0:
        return ""
    result = []
    used = 0
    for char in str(text or ""):
        try:
            unit = len(char.encode("gbk"))
        except UnicodeEncodeError:
            unit = 2
        if used + unit > max_units:
            break
        result.append(char)
        used += unit
    return "".join(result)


def normalize_new_friend_nickname(value):
    """Keep legitimate Unicode nicknames while removing clearly unusable text."""
    cleaned = []
    for char in str(value or ""):
        if char == "\ufffd":
            continue
        category = unicodedata.category(char)
        if category in {"Cc", "Cf", "Cs"}:
            if char in "\r\n\t":
                cleaned.append(" ")
            continue
        cleaned.append(char)
    nickname = " ".join("".join(cleaned).split())
    visible = nickname.replace(" ", "")
    if not visible or all(char in "?？" for char in visible):
        return ""
    return nickname


def build_new_friend_remark(
    nickname,
    *,
    prefix="",
    suffix="",
    prefix_timestamp=False,
    suffix_timestamp=False,
    now=None,
    max_units=32,
):
    """Build a WeChat remark for an accepted friend request."""
    current_time = now or datetime.now()
    timestamp = current_time.strftime("%Y%m%d%H%M%S")
    leading = timestamp if prefix_timestamp else ""
    prefix = str(prefix or "")
    name = normalize_new_friend_nickname(nickname) or f"新好友_{timestamp}"
    suffix = str(suffix or "")
    trailing = timestamp if suffix_timestamp else ""

    fixed_left = leading + prefix
    fixed_right = suffix + trailing
    name_part = truncate_remark_units(name, max_units)
    remaining = max(0, max_units - remark_unit_len(name_part))
    left_part = truncate_remark_units(fixed_left, remaining)
    remaining -= remark_unit_len(left_part)
    right_part = truncate_remark_units(fixed_right, remaining)
    return left_part + name_part + right_part


def build_new_friend_status_lines(
    *,
    accept_enabled,
    reply_enabled,
    messages,
    prefix,
    suffix,
    prefix_timestamp,
    suffix_timestamp,
):
    """Render the admin command status text for new-friend settings."""
    accept = "开启" if accept_enabled else "关闭"
    reply = "开启" if reply_enabled else "关闭"
    prefix_time = "是" if prefix_timestamp else "否"
    suffix_time = "是" if suffix_timestamp else "否"
    configured_messages = [new_friend_welcome_message_summary(messages)] if new_friend_welcome_message_has_content(messages) else ["（无）"]
    return [
        "--- 新好友状态 ---",
        f"自动通过好友申请：{accept}",
        f"自动回复新好友：{reply}",
        f"备注前缀：{prefix or '（空）'}  前缀加时间戳：{prefix_time}",
        f"备注后缀：{suffix or '（空）'}  后缀加时间戳：{suffix_time}",
        "自动回复消息：",
        *[f"  · {message}" for message in configured_messages],
    ]
