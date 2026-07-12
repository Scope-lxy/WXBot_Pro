"""Runtime executors for custom forward and takeover flows."""

import time

from core.logger import log
from core.reply_count_store import ReplyCountStore
from core.runtime_chat_state import pause_message_reply, send_text_to_target
from core.wechat_observability import warn_slow_wechat_ui_action
from core import wechat_ui_actions
from feature.custom_forward import iter_custom_forward_actions, plan_custom_forward_takeover
from feature.material_outreach import is_forward_result_success


def send_custom_forward_action(bot, action, chat, message):
    target = action.get("target")
    if not target:
        return
    time.sleep(1)
    guard = getattr(bot, "_config_ui_task_guard", None)
    task_key, task_version = guard("custom_forward") if callable(guard) else ("", 0)
    stable_delivery = getattr(bot, "_stable_inbound_delivery_id", None)
    delivery_id = (
        stable_delivery(f"custom-forward:{target}:{action.get('kind')}", chat, message)
        if callable(stable_delivery)
        else ""
    )
    success = False
    error = ""
    if action.get("kind") == "forward":
        source_message = action.get("source_message")
        remember_group = getattr(bot, "_remember_material_outbound_echoes", None)
        discard_group = getattr(bot, "_discard_private_outbound_echo_group", None)
        mark_reported_failed = getattr(bot, "_mark_private_outbound_echo_group_reported_failed", None)
        schedule_fallback = getattr(bot, "_schedule_private_outbound_echo_fallback", None)
        echo_group_id = ""
        if callable(remember_group):
            echo_group_id = remember_group(
                [target],
                getattr(message, "type", "unknown"),
                preface=source_message,
                material_title=str(
                    getattr(message, "original_content", "") or getattr(message, "content", "") or ""
                ),
                source="custom_forward",
                schedule_fallback=False,
            )
        try:
            if getattr(bot, "_ui_owner", None) is None and callable(getattr(message, "forward", None)):
                with wechat_ui_actions.hold(bot):
                    with warn_slow_wechat_ui_action(f"message.forward({target})"):
                        if source_message:
                            result = message.forward(target, message=source_message)
                        else:
                            result = message.forward(target)
            else:
                result = bot._ui_forward_message(
                    chat,
                    message,
                    target,
                    preface=source_message,
                    task_key=task_key,
                    task_version=task_version,
                    delivery_id=delivery_id,
                )
        except wechat_ui_actions.IntentCancelled:
            if callable(discard_group):
                discard_group(echo_group_id)
            log(message=f"[自定义转发] 规则已更新或关闭，已取消 {chat.who} → {target} 的旧转发")
            return
        except Exception:
            if callable(schedule_fallback):
                schedule_fallback("")
            raise
        success, error = is_forward_result_success(result)
        if success:
            if callable(schedule_fallback):
                schedule_fallback("")
        elif callable(mark_reported_failed):
            mark_reported_failed(echo_group_id)
    else:
        send_actions = getattr(bot, "_send_actions_to_target_without_child", None)
        if getattr(bot, "_ui_owner", None) is not None and callable(send_actions):
            try:
                result = send_actions(
                    target,
                    [{"type": "text", "text": str(action.get("content") or "")}],
                    task_key=task_key,
                    task_version=task_version,
                    delivery_id=delivery_id,
                )
            except wechat_ui_actions.IntentCancelled:
                log(message=f"[自定义转发] 规则已更新或关闭，已取消 {chat.who} → {target} 的旧转发")
                return
        else:
            result = send_text_to_target(bot, target, action.get("content", ""))
        success = True if result is None else ReplyCountStore.was_send_success(result)
        if not success and isinstance(result, dict):
            error = str(result.get("message") or result.get("error") or "").strip()
    log(
        level="INFO" if success else "WARNING",
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
