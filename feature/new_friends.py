"""New-friend business rules kept separate from wxautox execution calls."""

from datetime import datetime


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


def build_new_friend_remark(
    nickname,
    *,
    prefix="",
    suffix="",
    use_nickname=True,
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
    name = str(nickname or "") if use_nickname else ""
    suffix = str(suffix or "")
    trailing = timestamp if suffix_timestamp else ""

    fixed_left = leading + prefix
    fixed_right = suffix + trailing
    fixed_units = remark_unit_len(fixed_left) + remark_unit_len(fixed_right)
    if fixed_units <= max_units:
        name_units = max_units - fixed_units
        remark = fixed_left + truncate_remark_units(name, name_units) + fixed_right
    else:
        trailing_units = remark_unit_len(trailing)
        main_units = max(0, max_units - trailing_units)
        main = truncate_remark_units(fixed_left + suffix, main_units)
        remark = main + trailing

    if remark:
        return remark
    fallback = str(nickname or "新好友") if use_nickname else "新好友"
    return truncate_remark_units(fallback, max_units)


def build_new_friend_status_lines(
    *,
    accept_enabled,
    reply_enabled,
    messages,
    use_nickname,
    prefix,
    suffix,
    prefix_timestamp,
    suffix_timestamp,
):
    """Render the admin command status text for new-friend settings."""
    accept = "开启" if accept_enabled else "关闭"
    reply = "开启" if reply_enabled else "关闭"
    use_name = "是" if use_nickname else "否"
    prefix_time = "是" if prefix_timestamp else "否"
    suffix_time = "是" if suffix_timestamp else "否"
    configured_messages = messages if messages else ["（无）"]
    return [
        "--- 新好友状态 ---",
        f"自动通过好友申请：{accept}",
        f"自动回复新好友：{reply}",
        f"备注采用昵称：{use_name}",
        f"备注前缀：{prefix or '（空）'}  前缀加时间戳：{prefix_time}",
        f"备注后缀：{suffix or '（空）'}  后缀加时间戳：{suffix_time}",
        "自动回复消息：",
        *[f"  · {message}" for message in configured_messages],
    ]
