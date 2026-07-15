"""Wechat listening and ingress routing helpers."""

from __future__ import annotations

import os
import sys
import time
import uuid

from core import runtime_chat_state, wechat_ui_actions
from core.account_storage import account_area_dir, migrate_default_account
from core.contact_profiles import directory_lock, merge_directory as merge_contact_directory
from core.logger import log
from core.listener_window_supervisor import ListenerWindowSupervisor
from core.wechat_window import (
    is_wechat_client_binding_failure,
    rebind_wechat_client as core_rebind_wechat_client,
    run_with_wechat_rebind_retry,
)
from core.wechat_ui_runtime import OwnedChat
from core.wechat_observability import warn_slow_wechat_ui_action
from feature.material_outreach import iter_material_outreach_listen_sources
from feature.message_routing import prepare_message_media
from feature.new_friends import iter_new_friend_welcome_actions
from feature.voice_reply import load_voice_reply_state
from core.message_pipeline import ConversationRef, MessageEnvelope

LISTENER_RECOVERY_PROBE_INTERVAL_SECONDS = 5
LISTENER_WINDOW_RECOVERY_ATTEMPT_DELAYS_SECONDS = (30, 60)
LISTENER_WINDOW_RECOVERY_FIRST_DELAY_SECONDS = LISTENER_WINDOW_RECOVERY_ATTEMPT_DELAYS_SECONDS[0]
LISTENER_WINDOW_RECOVERY_RETRY_SECONDS = 60
LISTENER_WINDOW_RECOVERY_DEGRADED_AFTER_SECONDS = 600
LISTENER_WINDOW_RECOVERY_VERIFY_INTERVAL_SECONDS = 0.3
LISTENER_RECOVERY_HRESULTS = {
    -2147220991,  # 事件无法调用任何订户
    -2147023174,  # RPC 服务器不可用
    -2146233088,
}
LISTENER_RECOVERY_ERROR_PATTERNS = (
    "事件无法调用任何订户",
    "RPC 服务器不可用",
    "远程过程调用失败",
    "元素不可用",
    "对象不再连接到服务器",
    "Find Control Timeout",
)


def _bot_log(bot, *args, **kwargs) -> None:
    module = sys.modules.get(getattr(bot.__class__, "__module__", ""))
    log_fn = getattr(module, "log", log) if module else log
    log_fn(*args, **kwargs)


def _bot_sleep(bot, seconds: float) -> None:
    module = sys.modules.get(getattr(bot.__class__, "__module__", ""))
    time_module = getattr(module, "time", time) if module else time
    time_module.sleep(seconds)


def process_listen_message(bot, chat, message):
    prepare_message_media(bot, message, chat)
    return bot.process_message(chat, message)


def ensure_listener_recovery_state(bot) -> None:
    if not hasattr(bot, "_listener_auto_recovery_active"):
        bot._listener_auto_recovery_active = False
    if not hasattr(bot, "_listener_auto_recovery_attempted"):
        bot._listener_auto_recovery_attempted = False
    if not hasattr(bot, "_listener_auto_recovery_probe_after"):
        bot._listener_auto_recovery_probe_after = 0.0
    if not hasattr(bot, "_listener_auto_recovery_last_error"):
        bot._listener_auto_recovery_last_error = ""
    if not hasattr(bot, "_listener_auto_recovery_source"):
        bot._listener_auto_recovery_source = ""


def is_listener_recovery_desktop_error(exc) -> bool:
    if exc is None:
        return False
    hresult = getattr(exc, "hresult", None)
    if isinstance(hresult, int) and hresult in LISTENER_RECOVERY_HRESULTS:
        return True
    args = getattr(exc, "args", ())
    if args and isinstance(args[0], int) and args[0] in LISTENER_RECOVERY_HRESULTS:
        return True
    text = str(exc or "")
    return any(pattern in text for pattern in LISTENER_RECOVERY_ERROR_PATTERNS)


def clear_listener_auto_recovery(bot, *, clear_error=False) -> None:
    ensure_listener_recovery_state(bot)
    bot._listener_auto_recovery_active = False
    bot._listener_auto_recovery_probe_after = 0.0
    bot._listener_auto_recovery_source = ""
    if clear_error:
        bot._listener_auto_recovery_last_error = ""


def arm_listener_auto_recovery(bot, exc, source="") -> bool:
    if not is_listener_recovery_desktop_error(exc):
        return False
    ensure_listener_recovery_state(bot)
    now_ts = time.time()
    already_active = bool(getattr(bot, "_listener_auto_recovery_active", False))
    bot._listener_auto_recovery_active = True
    bot._listener_auto_recovery_attempted = False
    bot._listener_auto_recovery_probe_after = now_ts + LISTENER_RECOVERY_PROBE_INTERVAL_SECONDS
    bot._listener_auto_recovery_last_error = str(exc or "")
    bot._listener_auto_recovery_source = str(source or "").strip()
    if hasattr(bot, "callback_is_die"):
        bot.callback_is_die = False
    if not already_active:
        source_text = f"{bot._listener_auto_recovery_source}触发" if bot._listener_auto_recovery_source else "运行时触发"
        _bot_log(
            bot,
            level="WARNING",
            message=f"检测到桌面环境暂时不可操作（{source_text}），进入隐藏等待恢复态",
        )
    return True


def listen_add_error(result):
    """Extract an error message from wxautox AddListenChat results."""
    if isinstance(result, dict):
        return str(result.get("message") or result)
    return str(result)


def listen_remove_succeeded(result):
    """Only discard listener state when removal is confirmed or already complete."""
    if result is None or result is True:
        return True
    if result is False:
        return False
    text = listen_add_error(result).strip()
    missing = any(
        marker in text
        for marker in ("未找到监听对象", "未找到监听", "监听对象不存在", "未监听")
    )
    if isinstance(result, dict):
        status = str(result.get("status") or "").strip().lower()
        if status in {"success", "ok", "true", "成功"}:
            return True
        if status in {"error", "fail", "failed", "false", "失败", "错误"}:
            return missing
        if result.get("success") is True or result.get("code") == 0:
            return True
        if result.get("success") is False:
            return missing
        return missing
    return text.lower() in {"ok", "success", "true"} or text in {"成功", "已成功"} or missing


def listen_add_action_label(label):
    label = str(label or "").strip()
    if not label:
        return "添加监听"
    if label.endswith("监听"):
        return f"添加{label}"
    return f"添加{label}监听"


def subwindow_who(chat):
    try:
        return str(getattr(chat, "who", "") or "").strip()
    except Exception:
        return ""


def _conversation_ref(value, chat_type=None):
    if isinstance(value, ConversationRef):
        if chat_type is not None and ConversationRef(value.who, chat_type) != value:
            raise ValueError("conversation chat_type does not match")
        return value
    return ConversationRef(str(value or "").strip(), chat_type or "private")


def is_target_chat(chat, nickname, chat_type=None):
    expected = _conversation_ref(nickname, chat_type)
    actual = ConversationRef.from_wx_chat(chat) if chat and not isinstance(chat, dict) else None
    return bool(
        expected.who
        and chat
        and not isinstance(chat, dict)
        and actual == expected
        and callable(getattr(chat, "SendMsg", None))
    )


def get_verified_subwindow(bot, nickname, *, chat_type=None):
    conversation = _conversation_ref(nickname, chat_type)
    try:
        chat = bot.wx.GetSubWindow(
            nickname=conversation.who,
            chat_type=conversation.chat_type,
        )
    except Exception as exc:
        _bot_log(bot, level="WARNING", message=f"监听管理 {conversation.who}：获取监听子窗口失败，详情：{exc}")
        return None
    return chat if is_target_chat(chat, conversation) else None


def get_verified_subwindow_with_retry(
    bot,
    nickname,
    retry_count=3,
    interval=LISTENER_WINDOW_RECOVERY_VERIFY_INTERVAL_SECONDS,
    *,
    chat_type=None,
):
    conversation = _conversation_ref(nickname, chat_type)
    attempts = max(1, int(retry_count or 1))
    for attempt in range(1, attempts + 1):
        sub_chat = get_verified_subwindow(bot, conversation)
        if sub_chat:
            if attempt > 1:
                _bot_log(bot, level="DEBUG", message=f"监听管理 {conversation.who}：第 {attempt} 次验证获取到监听子窗口")
            return sub_chat
        if attempt < attempts:
            _bot_sleep(bot, interval)
    return None


def try_get_all_subwindow_refs(bot):
    try:
        chats = bot.wx.GetAllSubWindow()
    except Exception as exc:
        _bot_log(bot, level="ERROR", message=f"获取全部监听子窗口失败: {exc}")
        return None
    return {
        (conversation.chat_type, conversation.who)
        for conversation in (
            ConversationRef.from_wx_chat(chat)
            for chat in (chats or [])
        )
        if conversation.who
    }


def try_get_all_subwindow_names(bot):
    refs = try_get_all_subwindow_refs(bot)
    return None if refs is None else {name for _chat_type, name in refs}


def _dynamic_listener_entry_name(entry):
    if isinstance(entry, (list, tuple)) and entry:
        return str(entry[0] or "").strip()
    return str(entry or "").strip()


def _dynamic_listener_entry_ref(entry):
    name = _dynamic_listener_entry_name(entry)
    if not name:
        return None
    chat_type = entry[2] if isinstance(entry, (list, tuple)) and len(entry) >= 3 else "private"
    return ConversationRef(name, chat_type)


def has_dynamic_listener_entry(bot, nickname, *, chat_type=None):
    conversation = _conversation_ref(nickname, chat_type)
    if not conversation.who:
        return False
    return any(
        _dynamic_listener_entry_ref(item) == conversation
        for item in (getattr(bot, "all_Mode_listen_list", []) or [])
    )


def remove_dynamic_listener_entries(bot, nickname, *, chat_type=None):
    conversation = _conversation_ref(nickname, chat_type)
    runtime_list = getattr(bot, "all_Mode_listen_list", None)
    if not conversation.who or not isinstance(runtime_list, list):
        return False
    kept = [item for item in runtime_list if _dynamic_listener_entry_ref(item) != conversation]
    removed = len(kept) != len(runtime_list)
    if removed:
        runtime_list[:] = kept
    return removed


def touch_dynamic_listener_entry(bot, nickname, timestamp=None, *, chat_type=None):
    conversation = _conversation_ref(nickname, chat_type)
    if not conversation.who:
        return False
    runtime_list = getattr(bot, "all_Mode_listen_list", None)
    if not isinstance(runtime_list, list):
        return False
    now_ts = time.time() if timestamp is None else float(timestamp)
    for item in runtime_list:
        if _dynamic_listener_entry_ref(item) != conversation:
            continue
        if isinstance(item, list):
            if len(item) >= 2:
                item[1] = now_ts
            else:
                item.append(now_ts)
            if len(item) >= 3:
                item[2] = conversation.chat_type
            else:
                item.append(conversation.chat_type)
        elif isinstance(item, tuple):
            index = runtime_list.index(item)
            runtime_list[index] = [conversation.who, now_ts, conversation.chat_type]
        else:
            index = runtime_list.index(item)
            runtime_list[index] = [conversation.who, now_ts, conversation.chat_type]
        return True
    runtime_list.append([conversation.who, now_ts, conversation.chat_type])
    return True


def _forget_runtime_listener_caches(bot, nickname, *, chat_type=None):
    runtime_chat_state.remove_listen_chat(bot, nickname, chat_type=chat_type)


def _call_remove_listen_chat(bot, nickname, *, chat_type=None):
    conversation = _conversation_ref(nickname, chat_type)
    remove_listen_chat = getattr(getattr(bot, "wx", None), "RemoveListenChat", None)
    if not callable(remove_listen_chat):
        raise RuntimeError("当前微信客户端不支持删除监听")
    return remove_listen_chat(
        nickname=conversation.who,
        chat_type=conversation.chat_type,
    )


def close_dynamic_listener_subwindows(bot, nicknames):
    if isinstance(nicknames, str):
        raw_names = [nicknames]
    else:
        raw_names = list(nicknames or [])
    closed_names = []
    seen = set()
    for raw_name in raw_names:
        nickname = str(raw_name or "").strip()
        if not nickname or nickname in seen:
            continue
        seen.add(nickname)
        if not has_dynamic_listener_entry(bot, nickname, chat_type="private"):
            continue
        remove_fn = getattr(bot, "_remove_listen_chat_verified", None)
        removed = (
            remove_fn(nickname, chat_type="private")
            if callable(remove_fn)
            else remove_listen_chat_verified(bot, nickname, chat_type="private")
        )
        if removed:
            remove_dynamic_listener_entries(bot, nickname, chat_type="private")
            closed_names.append(nickname)
    return closed_names


def add_listen_chat_once(bot, nickname, label, *, chat_type=None, allow_rebind=False):
    conversation = _conversation_ref(nickname, chat_type)
    nickname = conversation.who
    quiet_labels = {"动态监听", "监听窗口恢复"}
    label_text = str(label or "").strip()
    log_level = "WARNING" if label_text in quiet_labels else "ERROR"

    def add_action():
        with warn_slow_wechat_ui_action(f"AddListenChat({nickname})"):
            return bot.wx.AddListenChat(
                nickname=nickname,
                chat_type=conversation.chat_type,
            )

    try:
        if allow_rebind:
            result = run_with_wechat_rebind_retry(
                bot,
                add_action,
                attempts=2,
                on_retry=lambda exc, _attempt: _bot_log(
                    bot,
                    level="WARNING",
                    message=f"监听管理 {nickname}：{listen_add_action_label(label)}异常，重新初始化微信客户端后重试，详情：{exc}",
                ),
            )
        else:
            result = add_action()
    except Exception as exc:
        if label_text not in quiet_labels:
            _bot_log(
                bot,
                level=log_level,
                message=f"监听管理 {nickname}：{listen_add_action_label(label)}调用异常，详情：{exc}",
            )
        return None
    if result:
        if label_text not in quiet_labels:
            _bot_log(bot, level="DEBUG", message=f"监听管理 {nickname}：{listen_add_action_label(label)}调用成功")
    else:
        _bot_log(bot, level=log_level, message=f"监听管理 {nickname}：{listen_add_action_label(label)}失败，详情：{listen_add_error(result)}")
    return result


def is_stale_listen_registration_error(result):
    return "已监听" in listen_add_error(result)


def _set_last_dynamic_add_result(
    bot,
    chat_name,
    result=None,
    *,
    chat_type=None,
    stale=False,
):
    conversation = _conversation_ref(chat_name, chat_type)
    bot._last_dynamic_add_result = {
        "chat": conversation.who,
        "chat_type": conversation.chat_type,
        "result": result,
        "stale": bool(stale),
        "error": listen_add_error(result),
        "at": time.time(),
    }


def _consume_last_dynamic_add_result(bot, chat_name, *, chat_type=None):
    conversation = _conversation_ref(chat_name, chat_type)
    info = getattr(bot, "_last_dynamic_add_result", None)
    if not isinstance(info, dict) or (
        str(info.get("chat") or "").strip(),
        str(info.get("chat_type") or "private").strip().lower(),
    ) != (conversation.who, conversation.chat_type):
        return {}
    bot._last_dynamic_add_result = None
    return info


def ensure_listener_window_recovery_state(bot):
    supervisor = getattr(bot, "_listener_window_supervisor", None)
    if supervisor is None:
        supervisor = ListenerWindowSupervisor(
            retry_delays=LISTENER_WINDOW_RECOVERY_ATTEMPT_DELAYS_SECONDS,
            retry_interval=LISTENER_WINDOW_RECOVERY_RETRY_SECONDS,
            degraded_after=LISTENER_WINDOW_RECOVERY_DEGRADED_AFTER_SECONDS,
            degraded_interval=300,
        )
        bot._listener_window_supervisor = supervisor
    return supervisor


def _has_due_listener_window_recovery_task(bot, now_ts=None):
    supervisor = ensure_listener_window_recovery_state(bot)
    now_ts = time.time() if now_ts is None else float(now_ts)
    return any(
        not item["inflight"] and float(item["next_retry_at"]) <= now_ts
        for item in supervisor.snapshot()
    )


def _queue_listener_window_recovery(
    bot,
    chat_name,
    *,
    chat_type=None,
    reason="",
    allow_rebuild=False,
    now=None,
):
    """Schedule window repair without retaining message data."""
    conversation = _conversation_ref(chat_name, chat_type)
    name = conversation.who
    if not name:
        return False
    supervisor = ensure_listener_window_recovery_state(bot)
    now_ts = time.time() if now is None else float(now)
    if supervisor.contains(name, chat_type=conversation.chat_type):
        supervisor.request(
            name,
            chat_type=conversation.chat_type,
            error=str(reason or "").strip(),
            allow_rebuild=allow_rebuild,
            now=now_ts,
        )
    else:
        supervisor.failed(
            name,
            str(reason or "").strip(),
            chat_type=conversation.chat_type,
            allow_rebuild=allow_rebuild,
            now=now_ts,
        )
    return True


def _is_bot_stop_requested(bot):
    stop_fn = getattr(bot, "is_stop_requested", None)
    if callable(stop_fn):
        try:
            return bool(stop_fn())
        except Exception:
            return False
    return False


def get_runtime_cached_subwindow(bot, nickname, *, chat_type=None):
    conversation = _conversation_ref(nickname, chat_type)
    name = conversation.who
    if not name:
        return None
    cached = runtime_chat_state.get_listen_chat(
        bot,
        name,
        chat_type=conversation.chat_type,
    )
    if is_target_chat(cached, conversation):
        return cached
    if cached:
        runtime_chat_state.remove_listen_chat(
            bot,
            name,
            chat_type=conversation.chat_type,
        )
    return None


def _rebuild_listener_window(bot, chat_name, *, chat_type=None):
    conversation = _conversation_ref(chat_name, chat_type)
    name = conversation.who
    if not name:
        return None
    existing = get_cached_or_verified_subwindow(
        bot,
        name,
        chat_type=conversation.chat_type,
    )
    if existing:
        return existing
    if not _remove_listen_chat_verified_locked(
        bot,
        name,
        chat_type=conversation.chat_type,
        log_success=False,
    ):
        return None
    try:
        with warn_slow_wechat_ui_action(f"AddListenChat({name})"):
            result = bot.wx.AddListenChat(
                nickname=name,
                chat_type=conversation.chat_type,
            )
    except Exception as exc:
        _bot_log(bot, level="WARNING", message=f"全局监听 {name}：监听窗口重建异常，稍后继续等待，详情：{exc}")
        return None
    if is_target_chat(result, conversation):
        runtime_chat_state.remember_listen_chat(bot, conversation, result)
        touch_dynamic_listener_entry(bot, conversation)
        return result
    sub_chat = get_verified_subwindow_with_retry(
        bot,
        conversation,
        retry_count=3,
    )
    if sub_chat:
        runtime_chat_state.remember_listen_chat(bot, conversation, sub_chat)
        touch_dynamic_listener_entry(bot, conversation)
        return sub_chat
    _bot_log(bot, level="WARNING", message=f"全局监听 {name}：监听窗口重建失败，稍后继续等待，详情：{listen_add_error(result)}")
    return None


def flush_listener_window_recovery_tasks(bot, *, limit=1):
    """Repair due listener windows without replaying messages."""
    supervisor = ensure_listener_window_recovery_state(bot)
    if _is_bot_stop_requested(bot):
        supervisor.clear()
        return False
    now_ts = time.time()
    due_items = supervisor.claim_due(limit=limit, now=now_ts)
    if not due_items:
        return False
    handled = False
    for item in due_items:
        name = item["conversation"]
        chat_type = item["chat_type"]
        sub_chat = get_runtime_cached_subwindow(bot, name, chat_type=chat_type)
        if not sub_chat:
            sub_chat = get_cached_or_verified_subwindow(bot, name, chat_type=chat_type)
            if not sub_chat:
                if item["allow_rebuild"] and supervisor.consume_rebuild(
                    name,
                    chat_type=chat_type,
                ):
                    sub_chat = _rebuild_listener_window(bot, name, chat_type=chat_type)
                else:
                    sub_chat = add_chat_to_listen(bot, name, chat_type=chat_type)
        handled = True
        if sub_chat:
            supervisor.succeeded(name, chat_type=chat_type)
            touch_dynamic_listener_entry(bot, name, chat_type=chat_type)
            mark_context_repair = getattr(bot, "_mark_context_repair_needed_after_restore", None)
            if callable(mark_context_repair):
                mark_context_repair(name, chat_type=chat_type)
            _bot_log(bot, level="INFO", message=f"全局监听 {name}：监听窗口已恢复")
            continue

        add_result = _consume_last_dynamic_add_result(bot, name, chat_type=chat_type)
        state = supervisor.failed(
            name,
            add_result.get("error", ""),
            chat_type=chat_type,
            allow_rebuild=bool(add_result.get("stale")),
            now=now_ts,
        )
        delay = max(0, int(float(state["next_retry_at"]) - now_ts))
        if state["degraded"]:
            _bot_log(
                bot,
                level="ERROR",
                message=f"全局监听 {name}：监听窗口持续不可用，已标记降级，将在 {delay}s 后继续尝试；不会重绑微信客户端",
            )
        else:
            _bot_log(
                bot,
                level="INFO",
                message=f"全局监听 {name}：监听窗口第 {state['attempts']} 次恢复失败，将在 {delay}s 后继续尝试",
            )
    return handled


def get_cached_or_verified_subwindow(bot, nickname, *, chat_type=None):
    conversation = _conversation_ref(nickname, chat_type)
    name = conversation.who
    if not name:
        return None
    cached = runtime_chat_state.get_listen_chat(
        bot,
        name,
        chat_type=conversation.chat_type,
    )
    if cached and is_target_chat(cached, conversation):
        return cached
    if cached:
        runtime_chat_state.remove_listen_chat(
            bot,
            name,
            chat_type=conversation.chat_type,
        )
    sub_chat = get_verified_subwindow(bot, conversation)
    if sub_chat:
        runtime_chat_state.remember_listen_chat(bot, conversation, sub_chat)
        return sub_chat
    return None


def add_and_verify_subwindow(bot, nickname, retry_count=3, *, chat_type=None):
    conversation = _conversation_ref(nickname, chat_type)
    name = conversation.who
    if not name:
        return None
    _set_last_dynamic_add_result(
        bot,
        name,
        None,
        chat_type=conversation.chat_type,
        stale=False,
    )
    sub_chat = get_cached_or_verified_subwindow(bot, conversation)
    if sub_chat:
        return sub_chat
    result = add_listen_chat_once(
        bot,
        name,
        "动态监听",
        chat_type=conversation.chat_type,
    )
    if is_target_chat(result, conversation):
        runtime_chat_state.remember_listen_chat(bot, conversation, result)
        _bot_log(bot, level="DEBUG", message=f"监听管理 {name}：AddListenChat 返回可用子窗口，已直接接管")
        return result
    sub_chat = get_verified_subwindow_with_retry(
        bot,
        conversation,
        retry_count=retry_count,
    )
    if sub_chat:
        runtime_chat_state.remember_listen_chat(bot, conversation, sub_chat)
        return sub_chat
    if is_stale_listen_registration_error(result):
        _bot_log(bot, level="WARNING", message=f"{name} 已存在监听登记但未获取到可用子窗口，本次不删除重建")
        _set_last_dynamic_add_result(
            bot,
            name,
            result,
            chat_type=conversation.chat_type,
            stale=True,
        )
    else:
        _set_last_dynamic_add_result(
            bot,
            name,
            result,
            chat_type=conversation.chat_type,
            stale=False,
        )
    return None


def expected_listener_names(bot):
    return list(dict.fromkeys(ref.who for ref in expected_listener_refs(bot)))


def expected_listener_refs(bot):
    return [
        conversation
        for label, conversation in listener_registration_specs(bot)
        if label != "动态监听"
    ]


def listener_registration_specs(bot):
    specs = []
    if not getattr(bot.config, "AllListen_switch", False):
        specs.extend(
            ("用户", ConversationRef(str(item or "").strip(), "private"))
            for item in (getattr(bot.config, "listen_list", []) or [])
        )
    if getattr(bot.config, "group_switch", False):
        specs.extend(
            ("群组", ConversationRef(str(item or "").strip(), "group"))
            for item in (getattr(bot.config, "group", []) or [])
        )
    material_source_runtime_enabled = getattr(bot, "_material_source_runtime_enabled", None)
    if callable(material_source_runtime_enabled) and material_source_runtime_enabled():
        specs.extend(
            (
                "素材投喂监听源",
                ConversationRef(str(source or "").strip(), "private"),
            )
            for source in iter_material_outreach_listen_sources(
                getattr(bot.config, "material_source_list", []),
                listen_list=getattr(bot.config, "listen_list", []),
                groups=getattr(bot.config, "group", []),
                group_switch=getattr(bot.config, "group_switch", False),
            )
        )
    for item in getattr(bot, "all_Mode_listen_list", []) or []:
        conversation = _dynamic_listener_entry_ref(item)
        if conversation is not None:
            specs.append(("动态监听", conversation))

    unique_specs = []
    seen = set()
    for label, conversation in specs:
        key = (conversation.chat_type, conversation.who)
        if not conversation.who or key in seen:
            continue
        seen.add(key)
        unique_specs.append((label, conversation))
    return unique_specs


def bind_wechat_client(bot, force_rebind=False):
    if not force_rebind and getattr(bot, "wx", None):
        return bot.wx
    return core_rebind_wechat_client(bot)


def probe_listener_recovery_client(bot, *, force_rebind=False):
    client = bind_wechat_client(bot, force_rebind=force_rebind)
    if hasattr(client, "IsOnline"):
        client.IsOnline()
    return client


def rebuild_listener_runtime(
    bot,
    *,
    verify_retry_count=3,
    clear_runtime_cache=True,
    finish_message="监听器初始化完成",
):
    if not getattr(bot, "wx", None):
        raise RuntimeError("当前未绑定微信客户端，无法重建监听器")

    _bot_log(bot, level="DEBUG", message="启动wxautox监听器...")
    if clear_runtime_cache:
        bot._listen_chats = {}

    bot.wx.StopListening()
    _bot_sleep(bot, 1)
    bot.wx.StartListening()

    result = None
    expected_listeners = []
    for label, conversation in listener_registration_specs(bot):
        _bot_sleep(bot, 0.5)
        result = add_listen_chat_once(
            bot,
            conversation,
            label,
            allow_rebind=True,
        )
        expected_listeners.append(conversation)
        if is_target_chat(result, conversation):
            runtime_chat_state.remember_listen_chat(bot, conversation, result)

    verify_initial_listeners(bot, expected_listeners, retry_count=verify_retry_count)
    bot._listener_reconcile_last_at = time.time()
    _bot_log(bot, level="INFO", message=finish_message)
    return all(
        runtime_chat_state.get_listen_chat(bot, conversation)
        for conversation in expected_listeners
    )


def process_listener_auto_recovery(bot):
    ensure_listener_recovery_state(bot)
    if not getattr(bot, "_listener_auto_recovery_active", False):
        return "idle"

    now_ts = time.time()
    probe_after = float(getattr(bot, "_listener_auto_recovery_probe_after", 0.0) or 0.0)
    if probe_after and now_ts < probe_after:
        return "waiting"

    try:
        client = probe_listener_recovery_client(bot)
    except Exception as initial_exc:
        recovery_exc = initial_exc
        if is_wechat_client_binding_failure(initial_exc):
            try:
                client = probe_listener_recovery_client(bot, force_rebind=True)
            except Exception as rebind_exc:
                recovery_exc = rebind_exc
            else:
                recovery_exc = None
        if recovery_exc is None:
            pass
        elif is_listener_recovery_desktop_error(recovery_exc):
            bot._listener_auto_recovery_probe_after = now_ts + LISTENER_RECOVERY_PROBE_INTERVAL_SECONDS
            if hasattr(bot, "callback_is_die"):
                bot.callback_is_die = False
            return "waiting"
        else:
            bot._listener_auto_recovery_attempted = True
            bot._listener_auto_recovery_last_error = str(recovery_exc or "")
            clear_listener_auto_recovery(bot)
            _bot_log(bot, level="ERROR", message=f"监听器自动恢复前探活失败：{recovery_exc}")
            return "failed"

    bot.wx = client
    bot._listener_auto_recovery_attempted = True
    bot._listener_auto_recovery_probe_after = 0.0
    try:
        recovered = rebuild_listener_runtime(
            bot,
            verify_retry_count=1,
            clear_runtime_cache=True,
            finish_message="监听器自动恢复完成",
        )
    except Exception as exc:
        bot._listener_auto_recovery_last_error = str(exc or "")
        if is_listener_recovery_desktop_error(exc):
            bot._listener_auto_recovery_active = True
            bot._listener_auto_recovery_probe_after = time.time() + LISTENER_RECOVERY_PROBE_INTERVAL_SECONDS
            _bot_log(bot, level="WARNING", message=f"监听器自动恢复遇到临时桌面异常，稍后继续：{exc}")
            return "waiting"
        clear_listener_auto_recovery(bot)
        _bot_log(bot, level="ERROR", message=f"监听器自动恢复失败：{exc}")
        return "failed"

    if recovered:
        clear_listener_auto_recovery(bot, clear_error=True)
        if hasattr(bot, "callback_is_die"):
            bot.callback_is_die = False
        _bot_log(bot, level="SUCCESS", message="监听器已自动恢复")
        return "recovered"

    bot._listener_auto_recovery_last_error = "监听器自动恢复后固定监听窗口仍不可用"
    clear_listener_auto_recovery(bot)
    _bot_log(bot, level="ERROR", message="监听器自动恢复失败，固定监听未恢复")
    return "failed"


def listener_recovery_snapshot(bot) -> dict[str, str | bool]:
    ensure_listener_recovery_state(bot)
    active = bool(getattr(bot, "_listener_auto_recovery_active", False))
    attempted = bool(getattr(bot, "_listener_auto_recovery_attempted", False))
    source = str(getattr(bot, "_listener_auto_recovery_source", "") or "").strip()
    last_error = str(getattr(bot, "_listener_auto_recovery_last_error", "") or "").strip()

    if active:
        status = "waiting"
        message = "桌面暂时不可操作，正在等待恢复后自动重建监听"
    elif attempted and last_error:
        status = "failed"
        message = "桌面恢复后自动重建监听失败"
    else:
        status = "idle"
        message = ""

    return {
        "listener_recovery_active": active,
        "listener_recovery_status": status,
        "listener_recovery_message": message,
        "listener_recovery_source": source,
        "listener_recovery_error": last_error,
    }


def ensure_listener_subwindow(bot, nickname, retry_count=3, *, chat_type=None):
    conversation = _conversation_ref(nickname, chat_type)
    name = conversation.who
    if not name:
        return None
    sub_chat = get_verified_subwindow(bot, conversation)
    if sub_chat:
        runtime_chat_state.remember_listen_chat(bot, conversation, sub_chat)
        return sub_chat
    runtime_chat_state.remove_listen_chat(bot, conversation)
    sub_chat = add_and_verify_subwindow(
        bot,
        conversation,
        retry_count=retry_count,
    )
    if sub_chat:
        _bot_log(bot, message=f"{name} 监听子窗口已自动恢复")
    return sub_chat


def reconcile_listener_subwindows(bot, retry_count=3):
    if not getattr(bot, "wx", None):
        return []

    expected = expected_listener_refs(bot)
    if not expected:
        return []

    listened_refs = try_get_all_subwindow_refs(bot)
    reopened = []
    for conversation in expected:
        key = (conversation.chat_type, conversation.who)
        if listened_refs is not None and key in listened_refs:
            sub_chat = get_verified_subwindow(bot, conversation)
            if sub_chat:
                runtime_chat_state.remember_listen_chat(bot, conversation, sub_chat)
                continue
        sub_chat = ensure_listener_subwindow(
            bot,
            conversation,
            retry_count=retry_count,
        )
        if sub_chat:
            reopened.append(conversation.who)
    return reopened


def maybe_reconcile_listener_subwindows(bot, force=False, retry_count=3):
    if not getattr(bot, "wx", None):
        return []

    if not force and getattr(getattr(bot, "config", None), "AllListen_switch", False):
        if _has_due_listener_window_recovery_task(bot):
            return []

    now_ts = time.time()
    interval = max(1, int(getattr(bot, "_listener_reconcile_interval_seconds", 30) or 30))
    last_at = float(getattr(bot, "_listener_reconcile_last_at", 0.0) or 0.0)
    if not force and last_at and now_ts - last_at < interval:
        return []

    reopened = reconcile_listener_subwindows(bot, retry_count=retry_count)
    bot._listener_reconcile_last_at = now_ts
    return reopened


def remove_listen_chat_verified(bot, nickname, *, chat_type=None, log_success=True):
    return _remove_listen_chat_verified_locked(
        bot,
        nickname,
        chat_type=chat_type,
        log_success=log_success,
    )


def _remove_listen_chat_verified_locked(
    bot,
    nickname,
    *,
    chat_type=None,
    log_success=True,
):
    conversation = _conversation_ref(nickname, chat_type)
    name = conversation.who
    try:
        with warn_slow_wechat_ui_action(f"RemoveListenChat({name})"):
            remove_result = _call_remove_listen_chat(bot, conversation)
        remove_result_text = str(listen_add_error(remove_result)).strip()
    except Exception as exc:
        _bot_log(bot, level="WARNING", message=f"监听管理 {name}：删除监听调用异常，已保留运行状态等待重试，详情：{exc}")
        return False

    if not listen_remove_succeeded(remove_result):
        _bot_log(
            bot,
            level="WARNING",
            message=f"监听管理 {name}：删除监听失败，已保留运行状态等待重试，详情：{remove_result_text}",
        )
        return False
    if log_success and not (
        remove_result_text.lower() in {"ok", "success", "true"}
        or remove_result_text in {"成功", "已成功"}
    ):
        _bot_log(bot, message=f"监听管理 {name}：删除监听结果：{remove_result_text}")

    _forget_runtime_listener_caches(
        bot,
        name,
        chat_type=conversation.chat_type,
    )
    remove_dynamic_listener_entries(
        bot,
        name,
        chat_type=conversation.chat_type,
    )
    if log_success:
        _bot_log(bot, message=f"监听管理 {name}：删除监听完成，已清理运行缓存")
    return True


def verify_initial_listeners(bot, expected_chats, retry_count=3):
    expected = []
    seen = set()
    for item in expected_chats or []:
        conversation = _conversation_ref(item)
        key = (conversation.chat_type, conversation.who)
        if conversation.who and key not in seen:
            seen.add(key)
            expected.append(conversation)
    if not expected:
        return

    listened_refs = try_get_all_subwindow_refs(bot)
    missing = (
        list(expected)
        if listened_refs is None
        else [
            conversation
            for conversation in expected
            if (conversation.chat_type, conversation.who) not in listened_refs
        ]
    )
    for conversation in expected:
        if conversation not in missing:
            sub_chat = get_verified_subwindow(bot, conversation)
            if sub_chat:
                runtime_chat_state.remember_listen_chat(bot, conversation, sub_chat)
    if not missing:
        _bot_log(bot, level="DEBUG", message="监听管理：初始化监听子窗口校验通过")
        return

    for conversation in missing:
        sub_chat = add_and_verify_subwindow(
            bot,
            conversation,
            retry_count=retry_count,
        )
        if not sub_chat:
            _bot_log(bot, level="ERROR", message=f"{conversation.who} 初始化监听子窗口重试失败，已跳过运行缓存")


def init_wx_listeners(bot):
    """Initialize WeChat client and listener registrations."""
    specs = listener_registration_specs(bot)
    listener_refs = [conversation for _label, conversation in specs]
    identity = bot._bootstrap_ui_owner([])
    bot.config.AtMe = "@" + str(identity.get("nickname") or "")
    _bot_log(bot, level="DEBUG", message="绑定微信：" + bot.config.AtMe)
    wx_id = str(identity.get("wx_id") or identity.get("nickname") or "")
    bot.wx_id = wx_id
    try:
        migrated_default = migrate_default_account(bot.config.DATA_DIR, wx_id)
        if migrated_default:
            _bot_log(bot, message=f"已将 default 账号目录迁移到 {wx_id}")
    except Exception as exc:
        _bot_log(bot, level="WARNING", message=f"default 账号目录迁移失败：{exc}")
    if hasattr(bot.config, "bind_account_wx_id"):
        bot.config.bind_account_wx_id(wx_id)
    bot._voice_reply_state = load_voice_reply_state(bot._voice_reply_state_path())
    bot._set_material_outreach_namespace(wx_id)
    bot._initialize_message_runtime(wx_id)
    bot._init_prompt_system(str(account_area_dir(bot.config.DATA_DIR, wx_id, "chat_memory", create=True)))
    bot._drain_message_recovery()
    bot._register_ui_listener_names(listener_refs)
    bot._listen_chats = {}
    for _label, conversation in specs:
        chat = OwnedChat(
            bot._ui_owner,
            conversation.who,
            conversation.chat_type,
        )
        runtime_chat_state.remember_listen_chat(bot, conversation, chat)
    bot._ui_ingress_ready.set()
    bot._register_runtime_task_schedules()
    _bot_log(bot, level="DEBUG", message="监听器初始化完成")
    return True


def find_new_group_friend(msg, flag):
    text = msg
    try:
        first_quote_content = text.split('"')[flag]
    except Exception:
        first_quote_content = text.split('"')[1]
    return first_quote_content


def send_group_welcome_msg(bot, chat, message):
    result = True
    _bot_log(bot, message=f"{chat.who} 系统消息:" + message.content)
    welcome_text = str(getattr(bot.config, "group_welcome_msg", "") or "").strip()
    if not welcome_text:
        return True

    def send_welcome(new_friend):
        _bot_sleep(bot, 5)
        task_key, task_version = bot._config_ui_task_guard("group_welcome")
        seed = "|".join([
            str(chat.who or ""),
            str(getattr(message, "id", "") or ""),
            str(getattr(message, "hash", "") or ""),
            str(getattr(message, "time", "") or ""),
            str(getattr(message, "content", "") or ""),
            str(new_friend or ""),
        ])
        delivery_id = f"group-welcome:{uuid.uuid5(uuid.NAMESPACE_URL, seed)}"
        action = {"type": "text", "text": welcome_text, "at": new_friend}
        try:
            return bot._send_tracked_outbound(
                chat.who,
                action,
                lambda echo_id: chat.SendActions(
                    [action],
                    task_key=task_key,
                    task_version=task_version,
                    delivery_id=delivery_id,
                    echo_delivery_ids=(echo_id,),
                ),
                source="group_welcome",
                chat_type="group",
                delivery_id=delivery_id,
                at=new_friend,
            )
        except wechat_ui_actions.IntentCancelled:
            _bot_log(bot, message=f"群欢迎设置已更新或关闭，已取消向 {new_friend} 发送旧欢迎语")
            return True

    if "加入群聊" in message.content:
        new_friend = find_new_group_friend(message.content, 1)
        _bot_log(bot, message=f"{chat.who} 新群友:" + new_friend)
        result = send_welcome(new_friend)
    elif "加入了群聊" in message.content:
        new_friend = find_new_group_friend(message.content, 3)
        _bot_log(bot, message=f"{chat.who} 新群友:" + new_friend)
        result = send_welcome(new_friend)

    return result


def archive_accepted_friend(bot, accepted):
    load_directory = getattr(bot, "_load_contact_profiles_directory", None)
    save_directory = getattr(bot, "_save_contact_profiles_directory", None)
    if not callable(load_directory) or not callable(save_directory):
        return False
    name = str((accepted or {}).get("name") or "").strip()
    send_name = str((accepted or {}).get("send_name") or name).strip()
    remark = str((accepted or {}).get("remark") or "").strip()
    if not name:
        return False
    raw_detail = {
        "昵称": name,
        "备注": remark if remark and remark != name else "",
        "标签": list((accepted or {}).get("tags") or []),
        "来源": "新好友自动通过",
    }
    directory, directory_file, wx_id = load_directory()
    with directory_lock(directory_file):
        directory, _directory_file, wx_id = load_directory()
        updated = merge_contact_directory(
            directory,
            [raw_detail],
            wx_id=wx_id,
            mark_missing=False,
        )
        save_directory(updated)
    _bot_log(bot, level="INFO", message=f"新好友 {send_name} 已加入通讯录档案")
    return True


def pass_new_friends(bot):
    owner = bot._ui_owner
    tags = list(getattr(bot.config, "new_friend_tags", []) or [])
    archive_enabled = bool(getattr(bot.config, "new_friend_archive_switch", True))
    remark_rules = {
        "enabled": archive_enabled,
        "prefix": str(getattr(bot.config, "new_friend_remark_prefix", "") or ""),
        "suffix": str(getattr(bot.config, "new_friend_remark_suffix", "") or ""),
        "prefix_timestamp": bool(getattr(bot.config, "new_friend_remark_prefix_timestamp", False)),
        "suffix_timestamp": bool(getattr(bot.config, "new_friend_remark_suffix_timestamp", False)),
    }
    welcome_actions = (
        list(iter_new_friend_welcome_actions(getattr(bot.config, "new_friend_msg", {})))
        if bool(getattr(bot.config, "new_friend_reply_switch", False))
        else []
    )
    task_key, task_version = bot._config_ui_task_guard("new_friend")
    try:
        result = owner.call(
            wechat_ui_actions.UIIntent(
                wechat_ui_actions.UIIntentKind.NEW_FRIEND,
                {
                    "remark_rules": remark_rules,
                    "tags": tags if archive_enabled else [],
                    "task_key": task_key,
                },
                task_version=task_version,
            ),
            wechat_ui_actions.UI_CALL_WAIT_TIMEOUT,
        )
    except wechat_ui_actions.IntentCancelled:
        _bot_log(bot, message="新好友自动通过规则已更新或关闭，本轮已取消")
        return True
    for item in result or []:
        bot._metric_increment("new_friend_accepted_count")
        _bot_log(bot, level="INFO", message="已通过" + str(item.get("send_name") or item.get("name") or "") + "的好友请求")
        try:
            archive_accepted_friend(bot, item)
        except Exception as exc:
            _bot_log(bot, level="WARNING", message=f"新好友已通过，但即时写入通讯录档案失败：{exc}")
        if not welcome_actions:
            continue
        _bot_sleep(bot, 5)
        send_name = str(item.get("send_name") or item.get("name") or "")
        for index, action in enumerate(welcome_actions):
            prepared_action = (
                {"type": "file", "path": str(action.get("path") or "")}
                if str(action.get("type") or "") == "file"
                else {"type": "text", "text": str(action.get("content") or "")}
            )
            try:
                owner.call(wechat_ui_actions.UIIntent(
                    wechat_ui_actions.UIIntentKind.SEND_ACTIONS,
                    {
                        "conversation": send_name,
                        "chat_type": "private",
                        "task_key": task_key,
                        "delivery_id": f"new-friend-welcome:{uuid.uuid4()}:{index}",
                        "actions": [prepared_action],
                    },
                    task_version=task_version,
                ), wechat_ui_actions.UI_CALL_WAIT_TIMEOUT)
            except wechat_ui_actions.IntentCancelled:
                _bot_log(bot, message=f"新好友欢迎规则已更新或关闭，已停止向 {send_name} 发送旧欢迎内容")
                break
            if index < len(welcome_actions) - 1:
                bot._inter_message_delay_or_stop()
    return True


def listen_mode(bot):
    bot.wx.poll_listen_messages()


def new_msg_get_plus(chat_records):
    filtered = [msg for msg in chat_records if msg[0] not in ("SYS", "Recall")]

    if any(msg[0] == "Self" for msg in filtered):
        latest_self_index = None
        for idx, msg in enumerate(filtered):
            if msg[0] == "Self":
                latest_self_index = idx
        post_self = filtered[latest_self_index + 1:]

        latest_time_index = None
        for idx, msg in enumerate(post_self):
            if msg[0] == "Time":
                latest_time_index = idx

        if latest_time_index is not None:
            post_time = post_self[latest_time_index + 1:]
            return [msg for msg in post_time if msg[0] not in ("Self", "Time")]
        return post_self

    latest_time_index = None
    for idx, msg in enumerate(filtered):
        if msg[0] == "Time":
            latest_time_index = idx

    if latest_time_index is not None:
        post_time = filtered[latest_time_index + 1:]
        return [msg for msg in post_time if msg[0] not in ("Self", "Time")]
    return filtered


def add_chat_to_listen(bot, chat, *, chat_type=None):
    conversation = _conversation_ref(chat, chat_type)
    add_verify_fn = getattr(bot, "_add_and_verify_subwindow", None)
    if callable(add_verify_fn):
        sub_chat = add_verify_fn(
            conversation.who,
            chat_type=conversation.chat_type,
        )
    else:
        sub_chat = add_and_verify_subwindow(bot, conversation)
    if not sub_chat:
        return None
    is_listened_fn = getattr(bot, "is_chat_listened", None)
    if callable(is_listened_fn) and is_listened_fn(
        conversation.who,
        chat_type=conversation.chat_type,
    ):
        touch_dynamic_listener_entry(bot, conversation)
        return sub_chat
    touch_dynamic_listener_entry(bot, conversation)
    _bot_log(bot, message=f"全局监听 {conversation.who}：已加入监听")
    return sub_chat


def is_chat_listened(bot, chat, *, chat_type=None):
    return has_dynamic_listener_entry(bot, chat, chat_type=chat_type)


def alllisten_mode(bot, last_time, timeout=10):
    flush_listener_window_recovery_tasks(bot)

    def remove_timeout_listen(chat_time_out=600):
        protected_listeners = {
            (conversation.chat_type, conversation.who)
            for conversation in expected_listener_refs(bot)
        }
        for listen_chat in bot.all_Mode_listen_list[:]:
            if time.time() - listen_chat[1] >= chat_time_out:
                conversation = _dynamic_listener_entry_ref(listen_chat)
                if conversation is None:
                    continue
                listen_name = conversation.who
                if (conversation.chat_type, listen_name) in protected_listeners:
                    continue
                remove_fn = getattr(bot, "_remove_listen_chat_verified", None)
                if callable(remove_fn):
                    removed = remove_fn(
                        listen_name,
                        chat_type=conversation.chat_type,
                        log_success=False,
                    )
                else:
                    removed = remove_listen_chat_verified(
                        bot,
                        listen_name,
                        chat_type=conversation.chat_type,
                        log_success=False,
                    )
                if removed:
                    remove_dynamic_listener_entries(
                        bot,
                        listen_name,
                        chat_type=conversation.chat_type,
                    )
                    _bot_log(bot, message=f"全局监听 {listen_name}：对话超时，已停止监听")

    def get_next_new_message():
        messages_new = bot.wx.GetNextNewMessage(
            filter_mute=bot.config.AllListen_filter_mute,
            download_media=bool(
                getattr(bot.config, "chat_image_recognition_switch", False)
                or getattr(bot.config, "group_image_recognition_switch", False)
            ),
        )
        chat = messages_new.get("chat_name")
        chat_type = messages_new.get("chat_type")
        msgs = messages_new.get("msg")

        if not msgs:
            return

        conversation = ConversationRef(str(chat or "").strip(), str(chat_type or "private").strip() or "private")
        envelopes = []
        for index, raw_message in enumerate(msgs):
            message = raw_message
            if not isinstance(message, MessageEnvelope):
                message = MessageEnvelope.from_wx_message(
                    raw_message,
                    ingress_source="global",
                    received_at=time.time(),
                    window_order=index,
                )
            else:
                message._wxbot_ingress_source = "global"
            voice_recognition_enabled = bool(
                getattr(
                    bot.config,
                    "group_voice_recognition_switch"
                    if conversation.chat_type == "group"
                    else "chat_voice_recognition_switch",
                    False,
                )
            )
            if message.type == "voice" and not voice_recognition_enabled:
                message._skip_ai_reply = True
            bot._enqueue_ui_message(conversation, message)
            envelopes.append(message)

        if chat in bot.config.global_blacklist:
            _bot_log(bot, message=f"{chat} 为黑名单用户，已保存消息并跳过回复")
            return

        supervisor = ensure_listener_window_recovery_state(bot)
        if supervisor.contains(
            conversation.who,
            chat_type=conversation.chat_type,
        ):
            return

        is_listened_fn = getattr(bot, "is_chat_listened", None)
        sub_chat = None
        if callable(is_listened_fn) and is_listened_fn(
            conversation.who,
            chat_type=conversation.chat_type,
        ):
            sub_chat = get_cached_or_verified_subwindow(bot, conversation)
            if sub_chat:
                touch_dynamic_listener_entry(bot, conversation)
        if not sub_chat:
            add_chat_fn = getattr(bot, "add_chat_to_listen", None)
            sub_chat = (
                add_chat_fn(
                    conversation.who,
                    chat_type=conversation.chat_type,
                )
                if callable(add_chat_fn)
                else add_chat_to_listen(bot, conversation)
            )
        if sub_chat:
            supervisor.succeeded(
                conversation.who,
                chat_type=conversation.chat_type,
            )
            return

        _forget_runtime_listener_caches(
            bot,
            conversation.who,
            chat_type=conversation.chat_type,
        )
        remove_dynamic_listener_entries(bot, conversation)
        add_result = _consume_last_dynamic_add_result(bot, conversation)
        _queue_listener_window_recovery(
            bot,
            conversation,
            reason=add_result.get("error", ""),
            allow_rebuild=bool(add_result.get("stale")),
        )
        _bot_log(
            bot,
            level="INFO",
            message=(
                f"全局监听 {chat}：消息已进入处理队列；监听窗口不可用，"
                f"将在 {LISTENER_WINDOW_RECOVERY_FIRST_DELAY_SECONDS}s 后重试"
            ),
        )

    get_next_new_message()

    if time.time() - last_time >= timeout:
        remove_timeout_listen()
        return time.time()
    return last_time
