"""Runtime executors for custom forward and takeover flows."""

import time

from core.logger import log
from core.reply_count_store import ReplyCountStore
from core.runtime_chat_state import pause_message_reply, send_text_to_target
from core.wechat_observability import warn_slow_wechat_ui_action
from feature.custom_forward import iter_custom_forward_actions, plan_custom_forward_takeover
from feature.material_outreach import is_forward_result_success


def send_custom_forward_action(bot, action, chat, message):
    target = action.get("target")
    if not target:
        return
    time.sleep(1)
    success = False
    error = ""
    if action.get("kind") == "forward":
        source_message = action.get("source_message")
        with bot._get_wechat_action_lock():
            with warn_slow_wechat_ui_action(f"message.forward({target})"):
                if source_message:
                    result = message.forward(target, message=source_message)
                else:
                    result = message.forward(target)
        remember_echo = getattr(bot, "_remember_private_outbound_echo", None)
        success, error = is_forward_result_success(result)
        if success and callable(remember_echo):
            if source_message:
                remember_echo(target, "text", source_message, source="custom_forward")
            remember_echo(target, getattr(message, "type", "unknown"), source="custom_forward")
    else:
        result = send_text_to_target(bot, target, action.get("content", ""))
        success = True if result is None else ReplyCountStore.was_send_success(result)
        if not success and isinstance(result, dict):
            error = str(result.get("message") or result.get("error") or "").strip()
    log(
        level="SUCCESS" if success else "WARNING",
        message=(
            f"[自定义转发] {chat.who} → {target}"
            f"（规则类型：{action.get('rule_type', 'all')}，附带来源：{bool(action.get('source_message'))}）"
            f"{('，错误：' + error) if error else ''}"
        )
    )


def handle_custom_forward_takeover(bot, chat, message):
    """
    预处理会暂停 AI 回复的关键词转发规则。
    命中后先执行所有匹配的转发规则，再暂停当前会话自动回复，调用方应跳过本次普通回复流程。
    """
    if not bot.config.custom_forward_switch:
        return False
    plan = plan_custom_forward_takeover(
        bot.config.custom_forward_list,
        chat.who,
        message,
        group_chats=getattr(bot.config, "group", []),
        chat_type=getattr(chat, "chat_type", ""),
        default_target=getattr(bot.config, "cmd", ""),
    )
    if not plan.get("should_takeover"):
        return False
    for action in plan.get("actions", []):
        send_custom_forward_action(bot, action, chat, message)
    if plan.get("should_pause") and pause_message_reply(
        bot,
        plan.get("pause_chat"),
        plan.get("pause_sender"),
        getattr(chat, "chat_type", ""),
    ):
        log(message=f"[自定义转发] {plan.get('pause_chat')} 已因关键词命中暂停 AI 自动回复")
    return True


def handle_custom_forward(bot, chat, message):
    """
    自定义规则转发执行器。
    遍历所有规则，找到 chat.who 匹配的来源，按规则类型判断是否转发，
    符合条件则逐目标转发（每次转发前延时 1 秒）。
    """
    if not bot.config.custom_forward_switch:
        return
    for action in iter_custom_forward_actions(
        bot.config.custom_forward_list,
        chat.who,
        message,
        group_chats=getattr(bot.config, "group", []),
        chat_type=getattr(chat, "chat_type", ""),
        default_target=getattr(bot.config, "cmd", ""),
    ):
        send_custom_forward_action(bot, action, chat, message)
