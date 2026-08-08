"""Message filtering and routing helpers."""

from __future__ import annotations

import sys
import time
import re
from datetime import datetime

from core import wechat_ui_actions
from core.logger import log
from core.message_pipeline import (
    contains_group_mention,
    is_failed_voice_transcription_text,
    is_unrecognized_voice_placeholder,
    voice_message_body,
)
from core.wechat_ui_runtime import MessageLocateError, MoveWindowListenRecoveryExhausted
from feature.keyword_reply import normalize_keyword_reply_actions, plan_group_keyword_reply


def _chat_identity(chat):
    chat_name = str(getattr(chat, "who", "") or "").strip()
    chat_type = str(getattr(chat, "chat_type", "") or "").strip().lower()
    if not chat_name:
        raise ValueError("chat.who must not be empty")
    if chat_type not in {"private", "group"}:
        raise ValueError("chat.chat_type must be private or group")
    return chat_name, chat_type


def _bot_log(bot, *args, **kwargs) -> None:
    module = sys.modules.get(getattr(bot.__class__, "__module__", ""))
    log_fn = getattr(module, "log", log) if module else log
    log_fn(*args, **kwargs)


def _bot_time_module(bot):
    module = sys.modules.get(getattr(bot.__class__, "__module__", ""))
    return getattr(module, "time", time) if module else time


def record_runtime_inbound_event(bot, message, chat_type="") -> None:
    if getattr(message, "attr", "") in {"self", "system"}:
        return
    if bool(getattr(message, "_wxbot_runtime_inbound_logged", False)):
        return
    runtime_id = str(getattr(bot, "_runtime_instance_id", "") or "").strip().lower()
    if len(runtime_id) != 32 or any(char not in "0123456789abcdef" for char in runtime_id):
        return
    scope = "group" if str(chat_type or "").strip().lower() == "group" else "private"
    message_type = str(getattr(message, "type", "") or "unknown").strip().lower() or "unknown"
    if message_type not in {"text", "image", "voice", "video", "file", "quote", "link", "emotion"}:
        message_type = "unknown"
    try:
        _bot_log(
            bot,
            level="DEBUG",
            message=f"运行事件：入站消息 scope={scope} type={message_type} runtime_id={runtime_id}",
        )
    except Exception:
        return
    try:
        setattr(message, "_wxbot_runtime_inbound_logged", True)
    except Exception:
        pass


def _recognition_switches_for_chat(bot, chat):
    config = getattr(bot, "config", None)
    if config is None:
        return False, False
    chat_name, chat_type = _chat_identity(chat)
    if chat_type == "group":
        if not (
            getattr(config, "group_switch", False)
            and chat_name in getattr(config, "group", [])
        ):
            return False, False
        return (
            bool(getattr(config, "group_image_recognition_switch", False)),
            bool(getattr(config, "group_voice_recognition_switch", False)),
        )
    if (
        not getattr(config, "AllListen_switch", False)
        and chat_name in getattr(config, "listen_list", [])
    ):
        return (
            bool(getattr(config, "chat_image_recognition_switch", False)),
            bool(getattr(config, "chat_voice_recognition_switch", False)),
        )
    if (
        getattr(config, "AllListen_switch", False)
        and chat_name not in getattr(config, "global_blacklist", [])
    ):
        return (
            bool(getattr(config, "chat_image_recognition_switch", False)),
            bool(getattr(config, "chat_voice_recognition_switch", False)),
        )
    return False, False


def voice_content_state(content):
    text = str(content or "").strip()
    if not text:
        return "pending"
    if is_failed_voice_transcription_text(text):
        return "failed"
    if is_unrecognized_voice_placeholder(text):
        return "pending"
    if not voice_message_body(text):
        return "pending"
    return "valid"


VOICE_DURATION_RE = re.compile(r"语音\s*(\d+)\s*[\"”]?\s*秒", re.IGNORECASE)


def voice_duration_seconds(content):
    match = VOICE_DURATION_RE.search(str(content or ""))
    return int(match.group(1)) if match else None


def match_pending_voice_snapshot(items, messages):
    """Match one fresh visible-window snapshot to queued voice placeholders."""
    candidates = [message for message in messages or [] if str(getattr(message, "type", "")).lower() == "voice"]
    used = set()
    matched = {}
    for item in items or []:
        signature = item.get("signature") or {}
        wanted_attr = str(signature.get("attr") or "")
        wanted_sender = str(signature.get("sender") or "")
        wanted_duration = signature.get("duration")
        wanted_hash = signature.get("hash")
        options = []
        for index, candidate in enumerate(candidates):
            if index in used:
                continue
            if wanted_attr and str(getattr(candidate, "attr", "") or "") != wanted_attr:
                continue
            if wanted_sender and str(getattr(candidate, "sender", "") or "") != wanted_sender:
                continue
            duration = voice_duration_seconds(getattr(candidate, "content", ""))
            if wanted_duration is not None and duration is not None and duration != wanted_duration:
                continue
            options.append((index, candidate))
        if not options:
            continue
        if wanted_hash not in {None, ""}:
            hash_options = [
                option for option in options
                if getattr(option[1], "hash", None) == wanted_hash
            ]
            if hash_options:
                options = hash_options
        if len(options) != 1:
            continue
        index, candidate = options[0]
        used.add(index)
        matched[item.get("key")] = candidate
    return matched


def mark_failed_voice_silent_ignore(bot, msg) -> None:
    msg._skip_ai_reply = True
    msg._voice_transcription_failed = True
    _bot_log(bot, "INFO", "语音识别失败，未得到有效文字，已静默忽略")


def _update_alllisten_timestamp(bot, chat_name: str, chat_type: str) -> None:
    if not (
        getattr(bot.config, "chat_switch", True)
        and getattr(bot.config, "AllListen_switch", False)
    ):
        return
    now_ts = _bot_time_module(bot).time()
    for listen_chat in getattr(bot, "all_Mode_listen_list", []):
        entry_type = (
            str(listen_chat[2] or "private").strip().lower()
            if len(listen_chat) >= 3
            else "private"
        )
        if listen_chat[0] == chat_name and entry_type == chat_type:
            listen_chat[1] = now_ts
            break


def prepare_message_media(bot, msg, chat) -> None:
    if getattr(msg, "_wxbot_media_prepared", False):
        return
    try:
        setattr(msg, "_wxbot_media_prepared", True)
    except Exception:
        pass
    image_enabled, voice_enabled = _recognition_switches_for_chat(bot, chat)
    try:
        if image_enabled:
            if msg.type == "image":
                down_path = bot._ui_download_message(chat, msg)
                if down_path:
                    msg.content = str(down_path)
                else:
                    _bot_log(bot, "WARNING", "消息处理：图片下载失败，详情：未返回文件路径")
                    msg._skip_ai_reply = True
            elif msg.type == "quote":
                down_path = bot._ui_download_message(chat, msg, quote_image=True)
                if down_path:
                    msg.content = str(msg.content) + "+引用的图片:" + str(down_path)
                else:
                    _bot_log(bot, "INFO", "引用内容不是图片或视频")
    except Exception as exc:
        if isinstance(exc, MessageLocateError):
            reason = "未能安全定位原消息，本次未下载"
        elif isinstance(exc, MoveWindowListenRecoveryExhausted):
            reason = "监听窗口暂不可用，已触发自动恢复，本次未下载"
        else:
            reason = "本次未下载"
        _bot_log(bot, level="WARNING", message=f"消息处理：图片下载失败，{reason}，详情：{exc}")
        if msg.type == "image":
            msg._skip_ai_reply = True

    if msg.type != "voice":
        return
    if not voice_enabled:
        msg._skip_ai_reply = True
        return
    state = voice_content_state(getattr(msg, "content", ""))
    if state == "valid":
        return
    if state == "failed":
        mark_failed_voice_silent_ignore(bot, msg)
        return
    queue_pending = getattr(bot, "_queue_pending_private_voice_transcription", None)
    if callable(queue_pending):
        queue_pending(chat, msg)
    msg._skip_ai_reply = True


def handle_friend_message_callback(bot, msg, chat, *, text: str):
    prepare_message_media(bot, msg, chat)
    record_received = getattr(bot, "_record_received_message", None)
    if callable(record_received):
        record_received()
    else:
        bot.msg_received_count += 1
    bot.last_msg_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    bot.last_msg_sender = msg.sender
    _chat_name, chat_type = _chat_identity(chat)
    _update_alllisten_timestamp(bot, chat.who, chat_type)

    if bot._handle_material_source_message(chat, msg):
        bot._mark_inbound_no_reply(msg)
        return True

    result = bot.process_message(chat, msg)
    if not result:
        bot.is_err(
            bot.wx.nickname + " wxbot处理监听新消息失败！",
            text + "\n" + bot._result_error_text(result),
        )
    return None


def _is_monitored_chat(bot, chat) -> bool:
    chat_name, chat_type = _chat_identity(chat)
    if chat_type == "group":
        return bool(
            getattr(bot.config, "group_switch", False)
            and chat_name in getattr(bot.config, "group", [])
        )
    if not getattr(bot.config, "chat_switch", True):
        return False
    if getattr(bot.config, "AllListen_switch", False):
        return chat_name not in getattr(bot.config, "global_blacklist", [])
    return chat_name in getattr(bot.config, "listen_list", [])


def _route_group_message(bot, chat, message):
    chat_name, chat_type = _chat_identity(chat)
    if chat_type != "group":
        raise ValueError("group routing requires chat_type=group")
    if not (
        getattr(bot.config, "group_switch", False)
        and chat_name in getattr(bot.config, "group", [])
    ):
        return {"action": "skip"}
    if getattr(message, "_skip_ai_reply", False):
        _bot_log(bot, message=f"群组 {chat.who} 消息已标记跳过 AI 回复：" + str(getattr(message, "content", "")))
        return {"action": "skip"}
    if (
        str(getattr(message, "type", "") or "").strip().lower() == "voice"
        and not getattr(bot.config, "group_voice_recognition_switch", False)
    ):
        _bot_log(bot, message=f"群组 {chat.who} 语音识别已关闭，跳过语音消息")
        return {"action": "skip"}
    if (
        not getattr(bot.config, "group_image_recognition_switch", False)
        and (
            getattr(message, "type", "") == "image"
            or "+引用的图片:" in str(getattr(message, "content", "") or "")
        )
    ):
        _bot_log(bot, message=f"群组 {chat.who} 图片识别未开启，跳过图片消息")
        return {"action": "skip"}

    if (
        getattr(bot.config, "group_image_recognition_switch", False)
        and getattr(message, "type", "") == "image"
    ):
        set_pending_visual_context = getattr(bot, "_set_pending_visual_context", None)
        if callable(set_pending_visual_context):
            set_pending_visual_context(
                chat.who,
                [getattr(message, "content", "")],
                chat_type="group",
                senders=[getattr(message, "sender", "")],
                append=True,
            )
        return {"action": "skip"}

    keyword_plan = plan_group_keyword_reply(
        bool(getattr(bot.config, "group_keyword_switch", False)),
        getattr(bot.config, "keyword_dict", {}),
        getattr(message, "content", ""),
        at_only=getattr(bot.config, "group_keyword_at_only", False),
        at_marker=getattr(bot.config, "AtMe", ""),
    )
    if keyword_plan:
        return {
            "action": "group_keyword_reply",
            "reply_actions": normalize_keyword_reply_actions(keyword_plan["reply"]),
        }

    at_marker = getattr(bot.config, "AtMe", "")
    if not (
        (
            contains_group_mention(getattr(message, "content", ""), at_marker)
            and getattr(bot.config, "group_reply_at", False)
        )
        or not getattr(bot.config, "group_reply_at", False)
    ):
        return {"action": "skip"}
    if getattr(bot, "_pause_group_reply", False) or getattr(bot.config, "group_listen_only", False):
        if getattr(bot.config, "group_listen_only", False):
            _bot_log(bot, message=f"群组 {chat.who} 已启用只监听不AI回复，跳过 AI 调用")
        return {"action": "skip"}
    return {"action": "group_ai"}


def route_process_message(bot, chat, message):
    _chat_name, chat_type = _chat_identity(chat)
    if not _is_monitored_chat(bot, chat):
        return {"action": "skip"}

    if chat_type == "group":
        return _route_group_message(bot, chat, message)

    return {"action": "private_ai"}
