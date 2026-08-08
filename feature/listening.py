"""Wechat listening and ingress routing helpers."""

from __future__ import annotations

import os
import sys
import threading
import time
import uuid

from core import runtime_chat_state, wechat_recovery, wechat_ui_actions
from core.account_storage import account_area_dir, migrate_default_account
from core.contact_profiles import directory_lock, merge_directory as merge_contact_directory
from core.logger import log
from core.listener_window_supervisor import ListenerWindowSupervisor
from core.wechat_window import (
    is_wechat_client_binding_failure,
    rebind_wechat_client as core_rebind_wechat_client,
    run_with_wechat_rebind_retry,
)
from core.wechat_ui_runtime import (
    ListenSubwindowNotReady,
    MoveWindowListenRecoveryExhausted,
    OwnedChat,
)
from feature.material_outreach import iter_material_outreach_listen_sources
from feature.message_routing import prepare_message_media
from feature.new_friends import iter_new_friend_welcome_actions
from feature.voice_reply import load_voice_reply_state
from core.message_pipeline import ConversationRef, MessageEnvelope

LISTENER_WINDOW_RECOVERY_ATTEMPT_DELAYS_SECONDS = (30, 60)
LISTENER_WINDOW_RECOVERY_FIRST_DELAY_SECONDS = LISTENER_WINDOW_RECOVERY_ATTEMPT_DELAYS_SECONDS[0]
LISTENER_WINDOW_RECOVERY_RETRY_SECONDS = 60
LISTENER_WINDOW_RECOVERY_DEGRADED_AFTER_SECONDS = 600
GLOBAL_SCAN_EMPTY_INTERVAL_SECONDS = 3.0
GLOBAL_SCAN_REPEAT_BACKOFF_MAX_SECONDS = 5.0
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


def _mark_listener_alive(bot) -> None:
    if hasattr(bot, "callback_is_die"):
        bot.callback_is_die = False


def listener_recovery_coordinator(bot):
    coordinator = getattr(bot, "_wechat_recovery", None)
    if coordinator is not None:
        return coordinator
    coordinator = wechat_recovery.WeChatRecoveryCoordinator(
        probe_client=lambda **kwargs: probe_listener_recovery_client(bot, **kwargs),
        rebuild_listener=lambda: rebuild_listener_runtime(
            bot,
            clear_runtime_cache=True,
            finish_message=None,
            track_global_scan_recovery_timing=True,
        ),
        set_client=lambda client: setattr(bot, "wx", client),
        is_client_binding_failure=is_wechat_client_binding_failure,
        log_event=lambda **kwargs: _bot_log(bot, **kwargs),
        mark_listener_alive=lambda: _mark_listener_alive(bot),
    )
    bot._wechat_recovery = coordinator
    return coordinator


def note_listener_subwindow_operation(bot, conversation, *, now=None) -> None:
    listener_recovery_coordinator(bot).note_listener_operation(conversation, now=now)


def _begin_listener_recovery_observation(bot, *, after_rebind, now=None) -> None:
    listener_recovery_coordinator(bot).begin_observation(after_rebind=after_rebind, now=now)


def record_move_window_local_recovery_failure(bot, conversation, *, now=None) -> str:
    return listener_recovery_coordinator(bot).record_local_recovery_failure(
        conversation,
        now=now,
    )


def arm_listener_auto_recovery(bot, exc, source="") -> bool:
    return listener_recovery_coordinator(bot).arm(exc, source=source)


def record_listener_recovery_exhausted(bot, conversation, *, now=None) -> str:
    return listener_recovery_coordinator(bot).record_listener_recovery_exhausted(
        conversation,
        now=now,
    )


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


def _material_source_listener_refs(bot):
    refs = getattr(bot, "_material_source_listener_refs", None)
    if not isinstance(refs, dict):
        refs = {}
        bot._material_source_listener_refs = refs
    return refs


def material_source_listener_names(bot):
    enabled = getattr(bot, "_material_source_runtime_enabled", None)
    if not callable(enabled) or not enabled():
        return []
    names = iter_material_outreach_listen_sources(
        getattr(bot.config, "material_source_list", []),
        listen_list=getattr(bot.config, "listen_list", []),
        groups=getattr(bot.config, "group", []),
        group_switch=getattr(bot.config, "group_switch", False),
    )
    return list(dict.fromkeys(str(name or "").strip() for name in names if str(name or "").strip()))


def material_source_listener_conversations(bot):
    refs = _material_source_listener_refs(bot)
    return [
        conversation
        for name in material_source_listener_names(bot)
        if isinstance((conversation := refs.get(name)), ConversationRef)
    ]


def _discover_material_source_listener(bot, name, *, allow_rebind=False):
    name = str(name or "").strip()
    if not name:
        return None
    refs = _material_source_listener_refs(bot)
    known = refs.get(name)

    def add_action():
        if isinstance(known, ConversationRef):
            return bot.wx.AddListenChat(
                nickname=name,
                chat_type=known.chat_type,
            )
        return bot.wx.AddListenChat(nickname=name)

    try:
        result = (
            run_with_wechat_rebind_retry(bot, add_action, attempts=2)
            if allow_rebind
            else add_action()
        )
    except Exception as exc:
        if not isinstance(exc, ListenSubwindowNotReady) and is_wechat_client_binding_failure(exc):
            raise
        _bot_log(bot, level="WARNING", message=f"素材来源 {name}：自动识别会话类型失败，详情：{exc}")
        return None

    conversation = ConversationRef.from_wx_chat(result) if result and not isinstance(result, dict) else None
    if (
        not conversation
        or conversation.who != name
        or (isinstance(known, ConversationRef) and conversation != known)
        or not callable(getattr(result, "SendMsg", None))
    ):
        _bot_log(bot, level="WARNING", message=f"素材来源 {name}：微信未返回可用监听窗口")
        return None

    refs[name] = conversation
    runtime_chat_state.remember_listen_chat(bot, conversation, result)
    _bot_log(bot, level="DEBUG", message=f"素材来源 {name}：已自动识别为{'群聊' if conversation.chat_type == 'group' else '私聊'}并加入监听")
    return conversation


def _strict_registered_listener_chats(bot, conversations):
    unique = []
    expected = set()
    for conversation in conversations or ():
        conversation = _conversation_ref(conversation)
        key = (conversation.chat_type, conversation.who)
        if not conversation.who or key in expected:
            continue
        expected.add(key)
        unique.append(conversation)
    if not unique:
        return {}

    chats = bot.wx.GetRegisteredListenChats([
        {"name": conversation.who, "chat_type": conversation.chat_type}
        for conversation in unique
    ])
    registered = {}
    for chat in chats or ():
        conversation = ConversationRef.from_wx_chat(chat)
        key = (conversation.chat_type, conversation.who)
        if key not in expected or not is_target_chat(chat, conversation):
            continue
        registered[key] = chat
        runtime_chat_state.remember_listen_chat(bot, conversation, chat)
    return registered


def _discover_unknown_material_source_listeners(bot, *, allow_rebind=False):
    discovered = {}
    refs = _material_source_listener_refs(bot)
    for name in material_source_listener_names(bot):
        if isinstance(refs.get(name), ConversationRef):
            continue
        conversation = _discover_material_source_listener(
            bot,
            name,
            allow_rebind=allow_rebind,
        )
        if conversation is None:
            continue
        chat = runtime_chat_state.get_listen_chat(bot, conversation)
        if is_target_chat(chat, conversation):
            discovered[(conversation.chat_type, conversation.who)] = chat
    return discovered


def ensure_material_source_listeners(bot, *, allow_rebind=False):
    names = material_source_listener_names(bot)
    refs = _material_source_listener_refs(bot)
    known_before = [
        refs[name]
        for name in names
        if isinstance(refs.get(name), ConversationRef)
    ]
    ready = _discover_unknown_material_source_listeners(
        bot,
        allow_rebind=allow_rebind,
    )
    ready.update(_strict_registered_listener_chats(bot, known_before))

    for conversation in known_before:
        key = (conversation.chat_type, conversation.who)
        if key in ready:
            continue
        restored = _discover_material_source_listener(
            bot,
            conversation.who,
            allow_rebind=allow_rebind,
        )
        if restored is None:
            continue
        chat = runtime_chat_state.get_listen_chat(bot, restored)
        if is_target_chat(chat, restored):
            ready[(restored.chat_type, restored.who)] = chat

    return [
        conversation
        for name in names
        if isinstance((conversation := refs.get(name)), ConversationRef)
        and (conversation.chat_type, conversation.who) in ready
    ]


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


def listener_registration_changed(chat):
    return bool(getattr(chat, "_listener_registration_changed", True))


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
        if not isinstance(exc, ListenSubwindowNotReady) and is_wechat_client_binding_failure(exc):
            raise
        if label_text not in quiet_labels:
            _bot_log(
                bot,
                level=log_level,
                message=f"监听管理 {nickname}：{listen_add_action_label(label)}调用异常，详情：{exc}",
            )
        return exc if isinstance(exc, MoveWindowListenRecoveryExhausted) else None
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
    if not _remove_listen_chat_verified_locked(
        bot,
        name,
        chat_type=conversation.chat_type,
        log_success=False,
    ):
        return None
    sub_chat = add_and_verify_subwindow(bot, conversation)
    if sub_chat:
        touch_dynamic_listener_entry(bot, conversation)
        return sub_chat
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
            if listener_registration_changed(sub_chat):
                bot._mark_context_repair_needed_after_restore(name, chat_type=chat_type)
                _bot_log(bot, level="DEBUG", message=f"全局监听 {name}：监听窗口已恢复")
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
        if state["entered_degraded"]:
            _bot_log(
                bot,
                level="DEBUG",
                message=f"全局监听 {name}：监听窗口持续不可用，已标记降级，将在 {delay}s 后继续尝试；不会重绑微信客户端",
            )
        elif state["degraded"]:
            _bot_log(
                bot,
                level="DEBUG",
                message=f"全局监听 {name}：监听窗口仍处于降级，将在 {delay}s 后继续尝试",
            )
        else:
            _bot_log(
                bot,
                level="DEBUG",
                message=f"全局监听 {name}：监听窗口第 {state['attempts']} 次恢复失败，将在 {delay}s 后继续尝试",
            )
    return handled


def add_and_verify_subwindow(bot, nickname, *, chat_type=None):
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
    result = add_listen_chat_once(
        bot,
        name,
        "动态监听",
        chat_type=conversation.chat_type,
    )
    if is_target_chat(result, conversation):
        runtime_chat_state.remember_listen_chat(bot, conversation, result)
        if listener_registration_changed(result):
            _bot_log(bot, level="DEBUG", message=f"监听管理 {name}：AddListenChat 返回可用子窗口，已直接接管")
        return result
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
    refs = [
        conversation
        for label, conversation in listener_registration_specs(bot)
        if label != "动态监听"
    ]
    refs.extend(material_source_listener_conversations(bot))
    unique_refs = []
    seen = set()
    for conversation in refs:
        key = (conversation.chat_type, conversation.who)
        if key in seen:
            continue
        seen.add(key)
        unique_refs.append(conversation)
    return unique_refs


def listener_registration_specs(bot):
    specs = []
    private_enabled = bool(getattr(bot.config, "chat_switch", True))
    if private_enabled and not getattr(bot.config, "AllListen_switch", False):
        specs.extend(
            ("用户", ConversationRef(str(item or "").strip(), "private"))
            for item in (getattr(bot.config, "listen_list", []) or [])
        )
    if getattr(bot.config, "group_switch", False):
        specs.extend(
            ("群组", ConversationRef(str(item or "").strip(), "group"))
            for item in (getattr(bot.config, "group", []) or [])
        )
    for item in getattr(bot, "all_Mode_listen_list", []) or []:
        conversation = _dynamic_listener_entry_ref(item)
        if conversation is not None and (private_enabled or conversation.chat_type != "private"):
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
    return core_rebind_wechat_client(bot, listeners=()) if force_rebind else core_rebind_wechat_client(bot)


def probe_listener_recovery_client(bot, *, force_rebind=False):
    client = bind_wechat_client(bot, force_rebind=force_rebind)
    if hasattr(client, "IsOnline"):
        client.IsOnline()
    return client


def rebuild_listener_runtime(
    bot,
    *,
    clear_runtime_cache=True,
    finish_message="监听器初始化完成",
    track_global_scan_recovery_timing=False,
):
    if not getattr(bot, "wx", None):
        raise RuntimeError("当前未绑定微信客户端，无法重建监听器")
    if track_global_scan_recovery_timing:
        bot._global_scan_recovery_started_at = 0.0

    preserved_material_sources = material_source_listener_conversations(bot)
    _bot_log(bot, level="DEBUG", message="启动wxautox监听器...")
    if clear_runtime_cache:
        bot._listen_chats = {}
        bot._material_source_listener_refs = {}

    specs = listener_registration_specs(bot)
    rebuild = getattr(bot.wx, "RebuildListeners", None)
    if not callable(rebuild):
        raise RuntimeError("当前微信客户端不支持 owner 监听器原子重建")
    if (
        getattr(bot.config, "chat_switch", True)
        and bot.config.AllListen_switch
    ):
        rebuild(())
        scan = global_scan_snapshot(bot)
        if scan.get("fail_stopped"):
            raise RuntimeError(scan.get("last_error") or "全局扫描已停止")
        refs = [conversation for _label, conversation in specs]
        if track_global_scan_recovery_timing:
            bot._global_scan_recovery_started_at = time.monotonic()
        thread = bot._global_scan_thread
        if thread is None or not thread.is_alive():
            start_global_scan_pump(bot, refs)
        else:
            bot._global_scan_deferred_listener_refs = refs
            _update_global_scan_state(
                bot,
                initial_drain_complete=False,
                last_scan_empty=False,
            )
        bot._listener_reconcile_last_at = time.time()
        if finish_message:
            _bot_log(bot, level="INFO", message=finish_message)
        return True

    expected_listeners = [conversation for _label, conversation in specs]
    expected_listeners.extend(preserved_material_sources)
    expected_listeners = list(dict.fromkeys(expected_listeners))
    restored = rebuild([
        {"name": conversation.who, "chat_type": conversation.chat_type}
        for conversation in expected_listeners
    ])
    for chat in restored:
        conversation = ConversationRef.from_wx_chat(chat)
        runtime_chat_state.remember_listen_chat(bot, conversation, chat)
    for conversation in expected_listeners:
        if runtime_chat_state.get_listen_chat(bot, conversation):
            bot._mark_context_repair_needed_after_restore(
                conversation.who,
                chat_type=conversation.chat_type,
            )
    bot._listener_reconcile_last_at = time.time()
    recovered = all(
        runtime_chat_state.get_listen_chat(bot, conversation)
        for conversation in expected_listeners
    )
    if recovered and finish_message:
        _bot_log(bot, level="INFO", message=finish_message)
    return recovered


def process_listener_auto_recovery(bot):
    return listener_recovery_coordinator(bot).process()


def listener_recovery_snapshot(bot) -> dict[str, str | bool]:
    return listener_recovery_coordinator(bot).status_snapshot()


def ensure_listener_subwindow(bot, nickname, *, chat_type=None):
    conversation = _conversation_ref(nickname, chat_type)
    name = conversation.who
    if not name:
        return None
    runtime_chat_state.remove_listen_chat(bot, conversation)
    sub_chat = add_and_verify_subwindow(
        bot,
        conversation,
    )
    if sub_chat and listener_registration_changed(sub_chat):
        _bot_log(bot, message=f"{name} 监听子窗口已自动恢复")
    return sub_chat


def reconcile_listener_subwindows(bot):
    if not getattr(bot, "wx", None):
        return []

    discovered_material_chats = _discover_unknown_material_source_listeners(bot)
    expected = expected_listener_refs(bot)
    if not expected:
        return []
    registered = _strict_registered_listener_chats(bot, expected)

    reopened = []
    for conversation in expected:
        key = (conversation.chat_type, conversation.who)
        sub_chat = registered.get(key)
        if sub_chat is not None and key in discovered_material_chats:
            sub_chat = discovered_material_chats[key]
        if sub_chat is None:
            sub_chat = ensure_listener_subwindow(bot, conversation)
        if sub_chat and listener_registration_changed(sub_chat):
            reopened.append(conversation.who)
            bot._mark_context_repair_needed_after_restore(
                conversation.who,
                chat_type=conversation.chat_type,
            )
    return reopened


def maybe_reconcile_listener_subwindows(bot, force=False):
    if not getattr(bot, "wx", None):
        return []

    owner = getattr(bot, "_ui_owner", None)
    is_idle = getattr(owner, "is_idle", None)
    if callable(is_idle) and not is_idle():
        return []
    recovery = getattr(bot, "_wechat_recovery", None)
    state_snapshot = getattr(recovery, "state_snapshot", None)
    if callable(state_snapshot) and state_snapshot().active:
        return []

    config = getattr(bot, "config", None)
    all_listen = bool(
        getattr(config, "chat_switch", True)
        and getattr(config, "AllListen_switch", False)
    )
    if all_listen:
        scan_state = global_scan_snapshot(bot)
        if (
            scan_state.get("fail_stopped")
            or not scan_state.get("initial_drain_complete")
            or not scan_state.get("last_scan_empty")
        ):
            return []

    if not force and all_listen:
        if _has_due_listener_window_recovery_task(bot):
            return []

    now_ts = time.time()
    interval = max(1, int(getattr(bot, "_listener_reconcile_interval_seconds", 60) or 60))
    last_at = float(getattr(bot, "_listener_reconcile_last_at", 0.0) or 0.0)
    if not force and last_at and now_ts - last_at < interval:
        return []

    reopened = reconcile_listener_subwindows(bot)
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
        remove_result = _call_remove_listen_chat(bot, conversation)
        remove_result_text = str(listen_add_error(remove_result)).strip()
    except Exception as exc:
        if is_wechat_client_binding_failure(exc):
            raise
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


def verify_initial_listeners(bot, expected_chats):
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

    missing = []
    for conversation in expected:
        sub_chat = add_and_verify_subwindow(
            bot,
            conversation,
        )
        if not sub_chat:
            missing.append(conversation)
            _bot_log(bot, level="ERROR", message=f"{conversation.who} 初始化监听子窗口重试失败，已跳过运行缓存")
    if not missing:
        _bot_log(bot, level="DEBUG", message="监听管理：初始化监听子窗口校验通过")


def _update_global_scan_state(bot, **updates):
    with bot._global_scan_state_lock:
        bot._global_scan_state.update(updates)
        return dict(bot._global_scan_state)


def global_scan_snapshot(bot):
    with bot._global_scan_state_lock:
        state = dict(bot._global_scan_state)
        degraded = dict(state.get("degraded_conversations") or {})
    if state.get("fail_stopped"):
        status = "failed"
    elif degraded:
        status = "degraded"
    elif state.get("initial_drain_complete"):
        status = "complete"
    elif state.get("running"):
        status = "scanning"
    else:
        status = "idle"
    message = str(state.get("last_error") or "").strip()
    if not message and degraded:
        latest = next(reversed(degraded.values()))
        message = (
            f"{latest['conversation']} 的未读深度覆盖不完整："
            f"扫描前 {latest['expected_count']} 条，实际取得 {latest['actual_count']} 条"
        )
    return {
        **state,
        "degraded_conversations": degraded,
        "scan_coverage_status": status,
        "scan_coverage_message": message,
    }


def _expected_unread_count(batch, conversation):
    candidates = [
        item
        for item in (batch.get("unread_before") or [])
        if str(item.get("name") or "").strip() == conversation.who
    ]
    exact = [
        item
        for item in candidates
        if str(item.get("chat_type") or "").strip() == conversation.chat_type
    ]
    if len(exact) == 1:
        candidates = exact
    elif len(candidates) != 1:
        return 0
    try:
        return max(0, int(candidates[0].get("new_count", 0) or 0))
    except (TypeError, ValueError):
        return 0


def _mark_global_scan_degraded(bot, conversation, details):
    snapshot = global_scan_snapshot(bot)
    degraded = dict(snapshot.get("degraded_conversations") or {})
    degraded[f"{conversation.chat_type}:{conversation.who}"] = dict(details)
    _update_global_scan_state(bot, degraded_conversations=degraded)


def _handle_global_scan_batch(bot, batch):
    if not isinstance(batch, dict):
        raise TypeError("全局扫描必须返回字典批次")
    ignored_chat_type = str(batch.get("ignored_unsupported_chat_type") or "").strip()
    if ignored_chat_type:
        bot._release_message_recovery_from_global_scan(batch.get("unread_before") or ())
        chat_name = str(batch.get("chat_name") or "").strip() or "未知会话"
        _bot_log(
            bot,
            level="INFO",
            message=f"全局扫描 {chat_name}：已跳过不支持的会话类型 {ignored_chat_type}",
        )
        return {
            "raw_count": max(1, int(batch.get("raw_message_count") or 0)),
            "new_fact_count": 0,
        }
    messages = list(batch.get("msg") or [])
    if not messages:
        bot._release_message_recovery_from_global_scan(batch.get("unread_before") or ())
        return {"raw_count": 0, "new_fact_count": 0}
    if any(not isinstance(message, MessageEnvelope) for message in messages):
        raise TypeError("全局扫描边界只能返回 MessageEnvelope")
    conversation = ConversationRef(
        str(batch.get("chat_name") or "").strip(),
        str(batch.get("chat_type") or "private").strip() or "private",
    )
    if any(
        message._wxbot_ingress_source != "global"
        or not str(getattr(message, "_wxbot_source_batch", "") or "").strip()
        for message in messages
    ):
        raise ValueError("全局扫描批次缺少稳定来源身份")

    accepted_items = bot._persist_ui_message_batch(conversation, messages)
    bot._release_message_recovery_from_global_scan(
        batch.get("unread_before") or (), conversation
    )
    for message in messages:
        voice_enabled = bool(
            bot.config.group_voice_recognition_switch
            if conversation.chat_type == "group"
            else bot.config.chat_voice_recognition_switch
        )
        if message.type == "voice" and not voice_enabled:
            message._skip_ai_reply = True
        bot._dispatch_persisted_ui_message(conversation, message)

    expected_count = _expected_unread_count(batch, conversation)
    actual_count = sum(1 for item in accepted_items if item.direction == "friend")
    new_fact_count = sum(1 for item in accepted_items if item.is_new)
    elapsed = float(batch["elapsed_seconds"])
    max_quantity = int(batch["max_quantity"])
    max_runtime = float(batch["max_runtime_seconds"])
    reasons = []
    if expected_count and actual_count < expected_count:
        reasons.append("returned_less_than_unread")
    if elapsed >= max_runtime:
        reasons.append("runtime_limit")
    if len(messages) >= max_quantity:
        reasons.append("quantity_limit")
    if reasons:
        details = {
            "conversation": conversation.who,
            "chat_type": conversation.chat_type,
            "expected_count": expected_count,
            "actual_count": actual_count,
            "raw_count": len(messages),
            "elapsed_seconds": elapsed,
            "reasons": reasons,
        }
        _mark_global_scan_degraded(bot, conversation, details)
        _bot_log(
            bot,
            level="DEBUG",
            message=(
                f"全局扫描 {conversation.who}：未读深度覆盖不完整，"
                f"扫描前 {expected_count} 条，实际取得 {actual_count} 条，"
                f"耗时 {elapsed:.2f}s，原因 {','.join(reasons)}；已取得消息仍正常处理"
            ),
        )

    if conversation.who in bot.config.global_blacklist:
        _bot_log(bot, message=f"{conversation.who} 为黑名单用户，已保存消息并跳过回复")
    elif global_scan_snapshot(bot).get("initial_drain_complete"):
        cached = get_runtime_cached_subwindow(
            bot,
            conversation.who,
            chat_type=conversation.chat_type,
        )
        if cached:
            touch_dynamic_listener_entry(bot, conversation)
        else:
            ensure_listener_window_recovery_state(bot).request(
                conversation.who,
                chat_type=conversation.chat_type,
                now=time.time(),
            )
    return {
        "raw_count": len(messages),
        "new_fact_count": new_fact_count,
        "conversation": conversation,
    }


def _activate_deferred_listener_windows(bot):
    refs = list(bot._global_scan_deferred_listener_refs)
    bot._global_scan_deferred_listener_refs = []
    recovery_started_at = float(
        getattr(bot, "_global_scan_recovery_started_at", 0.0) or 0.0
    )
    bot._global_scan_recovery_started_at = 0.0
    scan_elapsed_seconds = (
        max(0.0, time.monotonic() - recovery_started_at)
        if recovery_started_at
        else 0.0
    )
    ensure_material_source_listeners(bot)
    supervisor = ensure_listener_window_recovery_state(bot)
    now_ts = time.time()
    for conversation in refs:
        supervisor.request(
            conversation.who,
            chat_type=conversation.chat_type,
            now=now_ts,
        )
    _update_global_scan_state(bot, initial_drain_complete=True, last_empty_at=now_ts)
    if recovery_started_at:
        _bot_log(
            bot,
            level="INFO",
            message=(
                "自恢复【全局扫描】阶段耗时："
                f"首次扫空 {scan_elapsed_seconds:.1f}s，"
                f"已提交 {len(refs)} 个按需补窗任务"
            ),
        )


def _run_global_scan_pump(bot):
    repeated_batches = 0
    stop_event = bot._global_scan_stop
    while not stop_event.is_set() and not _is_bot_stop_requested(bot):
        try:
            batch = bot.wx.GetNextNewMessage(
                filter_mute=bot.config.AllListen_filter_mute,
            )
            result = _handle_global_scan_batch(bot, batch)
        except Exception as exc:
            if stop_event.is_set() or _is_bot_stop_requested(bot):
                break
            message = f"全局扫描已停止，避免继续清除未保存的微信未读：{exc}"
            _update_global_scan_state(
                bot,
                running=False,
                fail_stopped=True,
                last_error=message,
            )
            _bot_log(bot, level="ERROR", message=message)
            return

        _update_global_scan_state(
            bot,
            last_success_at=time.time(),
            last_scan_empty=result["raw_count"] == 0,
        )
        if result["raw_count"] == 0:
            repeated_batches = 0
            bot._release_message_recovery_from_global_scan(
                batch.get("unread_before") or (), final=True
            )
            if not global_scan_snapshot(bot).get("initial_drain_complete"):
                _activate_deferred_listener_windows(bot)
            if stop_event.wait(GLOBAL_SCAN_EMPTY_INTERVAL_SECONDS):
                break
            continue
        if result["new_fact_count"]:
            repeated_batches = 0
            continue
        repeated_batches += 1
        delay = min(
            GLOBAL_SCAN_REPEAT_BACKOFF_MAX_SECONDS,
            0.25 * (2 ** min(repeated_batches - 1, 5)),
        )
        if stop_event.wait(delay):
            break
    _update_global_scan_state(bot, running=False)


def start_global_scan_pump(bot, deferred_listener_refs):
    thread = bot._global_scan_thread
    if thread is not None and thread.is_alive():
        return thread
    bot._global_scan_deferred_listener_refs = list(deferred_listener_refs or [])
    bot._global_scan_stop.clear()
    _update_global_scan_state(
        bot,
        running=True,
        fail_stopped=False,
        initial_drain_complete=False,
        degraded_conversations={},
        last_error="",
        last_success_at=0.0,
        last_empty_at=0.0,
        last_scan_empty=False,
    )
    thread = threading.Thread(
        target=_run_global_scan_pump,
        args=(bot,),
        name="wechat-global-scan",
        daemon=True,
    )
    bot._global_scan_thread = thread
    thread.start()
    return thread


def stop_global_scan_pump(bot):
    bot._global_scan_stop.set()
    return True


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
    bot._listen_chats = {}
    bot._material_source_listener_refs = {}
    if (
        getattr(bot.config, "chat_switch", True)
        and getattr(bot.config, "AllListen_switch", False)
    ):
        bot._drain_message_recovery(defer_for_global_scan=True)
        bot._ui_ingress_ready.set()
        start_global_scan_pump(bot, listener_refs)
    else:
        bot._drain_message_recovery()
        registered = list(bot._register_ui_listener_names(listener_refs) or listener_refs)
        registered.extend(ensure_material_source_listeners(bot, allow_rebind=True))
        for conversation in registered:
            chat = OwnedChat(
                bot._ui_owner,
                conversation.who,
                conversation.chat_type,
            )
            runtime_chat_state.remember_listen_chat(bot, conversation, chat)
            bot._mark_context_repair_needed_after_restore(
                conversation.who,
                chat_type=conversation.chat_type,
            )
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
        except wechat_ui_actions.UIOutboundNotStarted as exc:
            _bot_log(
                bot,
                level="WARNING",
                message=f"群欢迎语未发送：{new_friend}，监听子窗口未准备好，详情：{exc}",
            )
            return False
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
            except wechat_ui_actions.UIOutboundNotStarted as exc:
                _bot_log(
                    bot,
                    level="WARNING",
                    message=f"新好友 {send_name} 的欢迎消息未发送：监听子窗口未准备好，详情：{exc}",
                )
                break
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
    if not getattr(bot.config, "chat_switch", True):
        return last_time
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
                pipelines = (
                    getattr(bot, "_group_message_pipelines", {})
                    if conversation.chat_type == "group"
                    else getattr(bot, "_private_message_pipelines", {})
                ) or {}
                pipeline = pipelines.get(listen_name) if isinstance(pipelines, dict) else None
                if isinstance(pipeline, dict) and (
                    pipeline.get("open_messages")
                    or pipeline.get("messages")
                    or pipeline.get("queued_batches")
                    or pipeline.get("worker_running")
                ):
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

    if time.time() - last_time >= timeout:
        remove_timeout_listen()
        return time.time()
    return last_time
