"""Admin workspace and takeover runtime helpers."""

import os
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace

from core import runtime_chat_state
from feature import admin_forward_flow, admin_moments_flow


IDLE_MODE = "idle"
TAKEOVER_MODE = "takeover"
MOMENTS_MODE = "moments"
FORWARD_MODE = "forward"


def _silent_reply_chat():
    return SimpleNamespace(SendMsg=lambda message: message)


def ensure_admin_workspace_state(bot):
    state = getattr(bot, "_admin_workspace_state", None)
    if not isinstance(state, dict):
        state = {}
    mode = str(state.get("mode") or IDLE_MODE).strip() or IDLE_MODE
    target = str(state.get("target") or "").strip()
    source = str(state.get("source") or "").strip()
    normalized = {
        "mode": mode,
        "target": target,
        "source": source,
    }
    bot._admin_workspace_state = normalized
    return normalized


def ensure_admin_workspace_echo_messages(bot):
    cache = getattr(bot, "_admin_workspace_echo_messages", None)
    if isinstance(cache, set):
        cache = {str(item): 1 for item in cache if str(item or "").strip()}
    elif not isinstance(cache, dict):
        cache = {}
    bot._admin_workspace_echo_messages = cache
    return cache


def ensure_pending_takeover_messages(bot):
    cache = getattr(bot, "_admin_workspace_pending_takeover", None)
    if not isinstance(cache, dict):
        cache = {}
        bot._admin_workspace_pending_takeover = cache
    return cache


def has_active_moments_draft(bot):
    loader = getattr(bot, "_load_admin_moments_draft", None)
    if not callable(loader):
        return False
    try:
        draft = loader()
    except Exception:
        return False
    return admin_moments_flow.is_active_draft(draft)


def has_active_forward_draft(bot):
    loader = getattr(bot, "_load_admin_forward_draft", None)
    if not callable(loader):
        return False
    try:
        draft = loader()
    except Exception:
        return False
    return admin_forward_flow.is_active_draft(draft)


def get_workspace_mode(bot):
    if has_active_moments_draft(bot):
        return MOMENTS_MODE, ""
    if has_active_forward_draft(bot):
        return FORWARD_MODE, ""
    state = ensure_admin_workspace_state(bot)
    mode = str(state.get("mode") or IDLE_MODE).strip() or IDLE_MODE
    if mode == TAKEOVER_MODE and str(state.get("target") or "").strip():
        return TAKEOVER_MODE, str(state.get("target") or "").strip()
    return IDLE_MODE, ""


def describe_workspace(bot):
    mode, target = get_workspace_mode(bot)
    if mode == TAKEOVER_MODE and target:
        return f"接管（{target}）"
    if mode == MOMENTS_MODE:
        return "发圈"
    if mode == FORWARD_MODE:
        return "转发"
    return "空闲"


def list_paused_friends(bot):
    paused = sorted(runtime_chat_state.ensure_pause_chat_reply_users(bot))
    return paused


def remember_admin_echo_message(bot, content):
    content = str(content or "").strip()
    if not content:
        return
    cache = ensure_admin_workspace_echo_messages(bot)
    cache[content] = int(cache.get(content, 0) or 0) + 1


def consume_admin_echo_message(bot, content):
    content = str(content or "").strip()
    if not content:
        return False
    cache = ensure_admin_workspace_echo_messages(bot)
    count = int(cache.get(content, 0) or 0)
    if count <= 0:
        return False
    if count == 1:
        cache.pop(content, None)
    else:
        cache[content] = count - 1
    return True


def _extract_send_message_text(args, kwargs):
    if "msg" in kwargs:
        payload = kwargs.get("msg")
    elif "message" in kwargs:
        payload = kwargs.get("message")
    elif args:
        payload = args[0]
    else:
        payload = ""
    return str(payload or "").strip()


@contextmanager
def capture_admin_chat_replies(bot, chat):
    admin_chat = str(getattr(getattr(bot, "config", None), "cmd", "") or "").strip()
    chat_name = str(getattr(chat, "who", "") or "").strip()
    send_msg = getattr(chat, "SendMsg", None)
    if not admin_chat or chat_name != admin_chat or not callable(send_msg):
        yield
        return

    def wrapped_send_msg(*args, **kwargs):
        result = send_msg(*args, **kwargs)
        payload = _extract_send_message_text(args, kwargs)
        if result is not False and payload:
            remember_admin_echo_message(bot, payload)
        return result

    chat.SendMsg = wrapped_send_msg
    try:
        yield
    finally:
        chat.SendMsg = send_msg


def _resolve_existing_image_path(bot, content):
    content = str(content or "").strip()
    if not content:
        return ""
    loader = getattr(bot, "_existing_local_image_path", None)
    if callable(loader):
        try:
            resolved = str(loader(content) or "").strip()
        except Exception:
            resolved = ""
        if resolved:
            return resolved
    suffix = Path(content).suffix.lower()
    if suffix in {".jpg", ".jpeg", ".png", ".gif", ".bmp", ".webp"}:
        return content
    return ""


def _pending_takeover_summary(bot):
    pending = ensure_pending_takeover_messages(bot)
    summary = []
    for target in sorted(pending):
        items = pending.get(target) or []
        if items:
            summary.append(f"{target}（{len(items)}）")
    return "、".join(summary)


def queue_pending_takeover_message(bot, chat, message):
    target = str(getattr(chat, "who", "") or "").strip()
    if not target:
        return
    pending = ensure_pending_takeover_messages(bot)
    pending.setdefault(target, []).append({
        "type": str(getattr(message, "type", "") or "").strip(),
        "content": str(getattr(message, "content", "") or "").strip(),
        "sender": str(getattr(message, "sender", "") or target).strip() or target,
    })


def clear_pending_takeover_messages(bot, target):
    target = str(target or "").strip()
    if not target:
        return
    ensure_pending_takeover_messages(bot).pop(target, None)


def enter_takeover(bot, target, *, source="manual"):
    target = str(target or "").strip()
    if not target:
        return False
    state = ensure_admin_workspace_state(bot)
    state["mode"] = TAKEOVER_MODE
    state["target"] = target
    state["source"] = str(source or "").strip()
    return True


def clear_takeover(bot, target=None):
    state = ensure_admin_workspace_state(bot)
    target = str(target or "").strip()
    if target and str(state.get("target") or "").strip() != target:
        return False
    state["mode"] = IDLE_MODE
    state["target"] = ""
    state["source"] = ""
    return True


def switch_takeover(bot, target, *, source="manual"):
    target = str(target or "").strip()
    if not target:
        return False
    if not runtime_chat_state.is_single_chat_reply_paused(bot, target):
        return False
    return enter_takeover(bot, target, source=source)


def build_current_session_message(bot):
    mode, target = get_workspace_mode(bot)
    paused = list_paused_friends(bot)
    pending_line = _pending_takeover_summary(bot)
    paused_line = "无"
    if paused:
        paused_line = f"{len(paused)} 个（{'、'.join(paused)}）"

    if mode == TAKEOVER_MODE and target:
        lines = [
            "当前会话",
            "当前工作模式：接管",
            f"当前接管好友：{target}",
            f"已接管好友：{paused_line}",
            "发送普通消息将自动转发给当前接管好友",
        ]
        if pending_line:
            lines.append(f"待处理接管消息：{pending_line}")
        return "\n".join(lines)

    if mode == MOMENTS_MODE:
        lines = [
            "当前会话",
            "当前工作模式：发圈",
            "普通文字会继续追加到发圈文案",
        ]
        if pending_line:
            lines.append(f"待处理接管消息：{pending_line}")
        lines.append("可用指令：/重新生成 /取消发圈")
        return "\n".join(lines)

    if mode == FORWARD_MODE:
        lines = [
            "当前会话",
            "当前工作模式：转发",
            "请继续按提示完成素材转发任务",
        ]
        if pending_line:
            lines.append(f"待处理接管消息：{pending_line}")
        return "\n".join(lines)

    lines = [
        "当前会话",
        "当前工作模式：空闲",
        f"已接管好友：{paused_line}",
    ]
    if paused:
        lines.append(f"可用 /切到 {paused[0]} 恢复接管")
    else:
        lines.append("发圈请先 /发圈")
        lines.append("转发请先 /转发")
    if pending_line:
        lines.append(f"待处理接管消息：{pending_line}")
    return "\n".join(lines)


def end_current_session(bot):
    mode, target = get_workspace_mode(bot)
    if mode == TAKEOVER_MODE and target:
        clear_takeover(bot, target)
        return "\n".join([
            f"已暂停当前接管会话：{target}",
            f"{target} 仍处于接管中，可用 /切到 {target} 恢复接管，或 /恢复 {target} 恢复自动回复",
        ])
    if mode == MOMENTS_MODE:
        if hasattr(bot, "cancel_admin_moments_draft"):
            return bot.cancel_admin_moments_draft(_silent_reply_chat())
        return "这次发圈任务已取消"
    if mode == FORWARD_MODE:
        if hasattr(bot, "cancel_admin_forward_draft"):
            try:
                bot.cancel_admin_forward_draft(_silent_reply_chat(), message="这次转发任务已取消")
            except TypeError:
                bot.cancel_admin_forward_draft(_silent_reply_chat())
        return "这次转发任务已取消"
    return "当前没有可暂停的前台会话"


def takeover_enter_reply():
    return "\n".join([
        "已接管该好友，你接下来的消息将被转发给对方。",
        "发 /恢复 好友名 可彻底退出接管",
        "发 /切到 好友名 可切换其他接管",
        "发 /暂停 可临时退出接管",
    ])


def admin_idle_prompt(bot):
    paused = list_paused_friends(bot)
    lines = [
        "当前无活动会话",
        "接管请输入 /接管 会话名称",
        "发圈请输入 /发圈",
        "转发请输入 /转发",
    ]
    if paused:
        lines.append(f"已接管好友：{'、'.join(paused)}")
        lines.append(f"切回接管请用 /切到 {paused[0]}")
    pending_line = _pending_takeover_summary(bot)
    if pending_line:
        lines.append(f"待处理接管消息：{pending_line}")
    return "\n".join(lines)


def _build_takeover_mirror_lines(chat, message, current_target):
    sender = str(getattr(message, "sender", "") or chat.who).strip() or chat.who
    content = str(getattr(message, "content", "") or "").strip()
    msg_type = str(getattr(message, "type", "") or "").strip()
    lines = ["[接管消息]", f"当前接管好友：{current_target or chat.who}"]
    if msg_type == "image" and content:
        lines.append(f"{sender} 发来图片：{content}")
    elif content:
        lines.append(f"{sender}：{content}")
    else:
        lines.append(f"{sender} 发来一条新消息")
    return lines


def _build_pending_takeover_mirror_lines(target, item):
    target = str(target or "").strip()
    sender = str((item or {}).get("sender") or target).strip() or target
    content = str((item or {}).get("content") or "").strip()
    msg_type = str((item or {}).get("type") or "").strip()
    lines = ["[接管消息]", f"当前接管好友：{target}"]
    if msg_type == "image" and content:
        lines.append(f"{sender} 发来图片：{content}")
    elif content:
        lines.append(f"{sender}：{content}")
    else:
        lines.append(f"{sender} 发来一条新消息")
    return lines


def replay_pending_takeover_messages_to_admin(bot, target):
    target = str(target or "").strip()
    if not target:
        return False
    admin_chat = str(getattr(getattr(bot, "config", None), "cmd", "") or "").strip()
    if not admin_chat:
        return False
    pending = ensure_pending_takeover_messages(bot)
    items = list(pending.get(target) or [])
    if not items:
        return False
    for item in items:
        text_payload = "\n".join(_build_pending_takeover_mirror_lines(target, item))
        remember_admin_echo_message(bot, text_payload)
        result = runtime_chat_state.send_text_to_target(bot, admin_chat, text_payload)
        if result is False:
            return False
        image_path = ""
        if str((item or {}).get("type") or "").strip() == "image":
            image_path = _resolve_existing_image_path(bot, (item or {}).get("content", ""))
        if image_path:
            remember_admin_echo_message(bot, image_path)
            file_result = runtime_chat_state.send_file_to_target(bot, admin_chat, image_path)
            if file_result is False:
                return False
    clear_pending_takeover_messages(bot, target)
    return True


def mirror_takeover_message_to_admin(bot, chat, message):
    admin_chat = str(getattr(getattr(bot, "config", None), "cmd", "") or "").strip()
    if not admin_chat:
        return True
    mode, current_target = get_workspace_mode(bot)
    chat_target = str(getattr(chat, "who", "") or "").strip()
    if mode == MOMENTS_MODE:
        queue_pending_takeover_message(bot, chat, message)
        return True
    if mode == TAKEOVER_MODE and current_target and current_target != chat_target:
        queue_pending_takeover_message(bot, chat, message)
        return True
    text_payload = "\n".join(_build_takeover_mirror_lines(chat, message, current_target))
    remember_admin_echo_message(bot, text_payload)
    runtime_chat_state.send_text_to_target(bot, admin_chat, text_payload)
    message_type = str(getattr(message, "type", "") or "").strip()
    image_path = _resolve_existing_image_path(bot, getattr(message, "content", ""))
    if message_type == "image" and image_path:
        remember_admin_echo_message(bot, image_path)
        runtime_chat_state.send_file_to_target(bot, admin_chat, image_path)
    return True


def route_admin_plain_message(bot, chat, message):
    content = str(getattr(message, "content", "") or "").strip()
    if not content or content.startswith("/"):
        return None
    mode, target = get_workspace_mode(bot)
    if mode == TAKEOVER_MODE and target:
        image_path = _resolve_existing_image_path(bot, content)
        if image_path:
            runtime_chat_state.send_file_to_target(bot, target, image_path)
        else:
            runtime_chat_state.send_text_to_target(bot, target, content)
        return True
    if mode == MOMENTS_MODE:
        return chat.SendMsg("当前正在发圈，请继续发送文案/图片，或使用 /重新生成、/取消发圈")
    if mode == FORWARD_MODE:
        return chat.SendMsg("当前正在创建转发任务，请继续按提示操作")
    return chat.SendMsg(admin_idle_prompt(bot))
