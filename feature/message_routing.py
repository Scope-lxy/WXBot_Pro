"""Message filtering and routing helpers."""

from __future__ import annotations

import sys
import time
from contextlib import nullcontext
from datetime import datetime

from core import runtime_chat_state
from core.logger import log
from feature import takeover_runtime
from feature.custom_forward_runtime import handle_custom_forward, handle_custom_forward_takeover
from feature.keyword_reply import normalize_keyword_reply_actions, plan_group_keyword_reply


def _bot_log(bot, *args, **kwargs) -> None:
    module = sys.modules.get(getattr(bot.__class__, "__module__", ""))
    log_fn = getattr(module, "log", log) if module else log
    log_fn(*args, **kwargs)


def _bot_time_module(bot):
    module = sys.modules.get(getattr(bot.__class__, "__module__", ""))
    return getattr(module, "time", time) if module else time


def _recognition_switches_for_chat(bot, chat):
    is_group = chat.who in getattr(bot.config, "group", [])
    if is_group:
        return (
            bool(getattr(bot.config, "group_image_recognition_switch", False)),
            bool(getattr(bot.config, "group_voice_recognition_switch", False)),
        )
    if (
        not getattr(bot.config, "AllListen_switch", False)
        and chat.who in getattr(bot.config, "listen_list", [])
    ):
        return (
            bool(getattr(bot.config, "chat_image_recognition_switch", False)),
            bool(getattr(bot.config, "chat_voice_recognition_switch", False)),
        )
    if (
        getattr(bot.config, "AllListen_switch", False)
        and chat.who not in getattr(bot.config, "global_blacklist", [])
        and getattr(chat, "chat_type", "") != "group"
    ):
        return (
            bool(getattr(bot.config, "chat_image_recognition_switch", False)),
            bool(getattr(bot.config, "chat_voice_recognition_switch", False)),
        )
    return False, False


def _update_alllisten_timestamp(bot, chat_name: str) -> None:
    if not getattr(bot.config, "AllListen_switch", False):
        return
    now_ts = _bot_time_module(bot).time()
    for listen_chat in getattr(bot, "all_Mode_listen_list", []):
        if listen_chat[0] == chat_name:
            listen_chat[1] = now_ts
            break


def _prepare_friend_message_media(bot, msg, chat) -> None:
    image_enabled, voice_enabled = _recognition_switches_for_chat(bot, chat)
    try:
        if image_enabled:
            if msg.type == "image":
                down_path = msg.download()
                if down_path:
                    msg.content = str(down_path)
                else:
                    _bot_log(bot, "ERROR", "消息处理：图片下载失败，详情：未返回文件路径")
            elif msg.type == "quote":
                down_path = msg.download_quote_image()
                if down_path:
                    msg.content = str(msg.content) + "+引用的图片:" + str(down_path)
                else:
                    _bot_log(bot, "INFO", "引用内容不是图片或视频")
    except Exception as exc:
        _bot_log(bot, level="ERROR", message=f"消息处理：图片下载失败，请尝试将 Windows 屏幕缩放设置为 100%，详情：{exc}")

    if msg.type != "voice":
        return
    if not voice_enabled:
        msg._skip_ai_reply = True
        return
    try:
        voice_content = msg.to_text()
        if voice_content:
            msg.content = str(voice_content)
            return
    except Exception:
        pass
    if getattr(chat, "chat_type", "") == "group":
        msg._skip_ai_reply = True
    else:
        msg._voice_transcription_failed = True
    _bot_log(bot, "WARNING", "消息自动语音转文字失败")


def handle_friend_message_callback(bot, msg, chat, *, text: str):
    _prepare_friend_message_media(bot, msg, chat)
    record_received = getattr(bot, "_record_received_message", None)
    if callable(record_received):
        record_received()
    else:
        bot.msg_received_count += 1
    bot.last_msg_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    bot.last_msg_sender = msg.sender
    _update_alllisten_timestamp(bot, chat.who)

    admin_reply_context = (
        takeover_runtime.capture_admin_chat_replies(bot, chat)
        if chat.who == bot.config.cmd
        else nullcontext()
    )
    with admin_reply_context:
        if chat.who == bot.config.cmd and bot._handle_admin_forward_input(chat, msg):
            bot._mark_message_skip_memory(msg)
            return True
        if chat.who == bot.config.cmd and bot._handle_admin_moments_input(chat, msg):
            bot._mark_message_skip_memory(msg)
            return True
        if bot._handle_material_source_message(chat, msg):
            if chat.who == getattr(bot.config, "cmd", ""):
                bot._mark_message_skip_memory(msg)
            return True

        takeover_handled = False
        if getattr(bot.config, "custom_forward_switch", False):
            try:
                takeover_handled = handle_custom_forward_takeover(bot, chat, msg)
            except Exception as exc:
                _bot_log(bot, level="ERROR", message=f"自定义转发人工接管处理出错: {exc}")

        if takeover_handled:
            result = True
        else:
            result = bot.process_message(chat, msg)
            if getattr(bot.config, "custom_forward_switch", False):
                try:
                    handle_custom_forward(bot, chat, msg)
                except Exception as exc:
                    _bot_log(bot, level="ERROR", message=f"自定义转发处理出错: {exc}")

        if not result:
            bot.is_err(
                bot.wx.nickname + " wxbot处理监听新消息失败！",
                text + "\n" + bot._result_error_text(result),
            )
    return None


def _is_monitored_chat(bot, chat) -> bool:
    return (
        (
            getattr(bot.config, "AllListen_switch", False)
            and chat.who not in getattr(bot.config, "global_blacklist", [])
        )
        or (
            not getattr(bot.config, "AllListen_switch", False)
            and chat.who in getattr(bot.config, "listen_list", [])
        )
        or (
            chat.who in getattr(bot.config, "group", [])
            and getattr(bot.config, "group_switch", False)
        )
        or chat.who == getattr(bot.config, "cmd", "")
    )


def _route_group_message(bot, chat, message):
    if chat.who in getattr(bot.config, "group", []) and not getattr(bot.config, "group_switch", False):
        return {"action": "skip"}
    if runtime_chat_state.is_message_reply_paused(
        bot,
        chat.who,
        getattr(message, "sender", ""),
        chat_type="group",
    ):
        return {"action": "skip"}
    if getattr(message, "_skip_ai_reply", False):
        _bot_log(bot, message=f"群组 {chat.who} 消息已标记跳过 AI 回复：" + str(getattr(message, "content", "")))
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
        (at_marker and at_marker in getattr(message, "content", "") and getattr(bot.config, "group_reply_at", False))
        or not getattr(bot.config, "group_reply_at", False)
    ):
        return {"action": "skip"}
    if getattr(bot, "_pause_group_reply", False) or getattr(bot.config, "group_listen_only", False):
        if getattr(bot.config, "group_listen_only", False):
            _bot_log(bot, message=f"群组 {chat.who} 已启用只监听不AI回复，跳过 AI 调用")
        return {"action": "skip"}
    return {"action": "group_ai"}


def route_process_message(bot, chat, message):
    if not _is_monitored_chat(bot, chat):
        return {"action": "skip"}

    if (
        chat.who != getattr(bot.config, "cmd", "")
        and getattr(chat, "chat_type", "") != "group"
        and runtime_chat_state.is_single_chat_reply_paused(bot, chat.who)
    ):
        return {"action": "takeover_mirror"}

    if chat.who in getattr(bot.config, "group", []):
        return _route_group_message(bot, chat, message)

    if chat.who == getattr(bot.config, "cmd", ""):
        return {"action": "admin_command"}

    if (
        getattr(bot.config, "AllListen_switch", False)
        and (
            chat.who in getattr(bot.config, "global_blacklist", [])
            or getattr(chat, "chat_type", "") == "group"
        )
    ):
        return {"action": "skip"}

    return {"action": "private_ai"}
