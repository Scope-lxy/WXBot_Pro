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
from core.memory import MemoryManager
from core.wechat_window import (
    is_wechat_client_binding_failure,
    rebind_wechat_client as core_rebind_wechat_client,
    run_with_wechat_rebind_retry,
)
from core.wechat_ui_runtime import OwnedChat
from core.wechat_observability import warn_slow_wechat_ui_action
from feature.material_outreach import iter_material_outreach_listen_sources
from feature.message_routing import prepare_message_media, record_runtime_inbound_event
from feature.new_friends import build_new_friend_remark, iter_new_friend_welcome_actions
from feature.voice_reply import load_voice_reply_state
from core.message_pipeline import message_unique_id, strip_voice_duration_metadata

LISTENER_RECOVERY_PROBE_INTERVAL_SECONDS = 5
LIGHTWEIGHT_DELAYED_LISTEN_ATTEMPT_DELAYS_SECONDS = (30, 60)
LIGHTWEIGHT_DELAYED_LISTEN_DELAY_SECONDS = LIGHTWEIGHT_DELAYED_LISTEN_ATTEMPT_DELAYS_SECONDS[0]
LIGHTWEIGHT_DELAYED_LISTEN_RETRY_SECONDS = 60
LIGHTWEIGHT_DELAYED_LISTEN_ESCALATION_SECONDS = 600
LIGHTWEIGHT_DELAYED_LISTEN_REBUILD_COOLDOWN_SECONDS = 600
LIGHTWEIGHT_DELAYED_LISTEN_VERIFY_INTERVAL_SECONDS = 0.3
LIGHTWEIGHT_DELAYED_LISTEN_MAX_MESSAGES_PER_CHAT = 20
_LIGHTWEIGHT_DELAYED_LISTEN_LOCK_BUSY = object()
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


def clear_listener_auto_recovery(bot) -> None:
    ensure_listener_recovery_state(bot)
    bot._listener_auto_recovery_active = False
    bot._listener_auto_recovery_probe_after = 0.0
    bot._listener_auto_recovery_last_error = ""
    bot._listener_auto_recovery_source = ""


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


def is_target_chat(chat, nickname):
    name = str(nickname or "").strip()
    return bool(
        name
        and chat
        and not isinstance(chat, dict)
        and subwindow_who(chat) == name
        and callable(getattr(chat, "SendMsg", None))
    )


def get_verified_subwindow(bot, nickname):
    try:
        chat = bot.wx.GetSubWindow(nickname=nickname)
    except Exception as exc:
        _bot_log(bot, level="WARNING", message=f"监听管理 {nickname}：获取监听子窗口失败，详情：{exc}")
        return None
    if chat and subwindow_who(chat) == str(nickname).strip():
        return chat
    return None


def get_verified_subwindow_with_retry(bot, nickname, retry_count=3, interval=LIGHTWEIGHT_DELAYED_LISTEN_VERIFY_INTERVAL_SECONDS):
    attempts = max(1, int(retry_count or 1))
    for attempt in range(1, attempts + 1):
        sub_chat = get_verified_subwindow(bot, nickname)
        if sub_chat:
            if attempt > 1:
                _bot_log(bot, level="DEBUG", message=f"监听管理 {nickname}：第 {attempt} 次验证获取到监听子窗口")
            return sub_chat
        if attempt < attempts:
            _bot_sleep(bot, interval)
    return None


def try_get_all_subwindow_names(bot):
    try:
        chats = bot.wx.GetAllSubWindow()
    except Exception as exc:
        _bot_log(bot, level="ERROR", message=f"获取全部监听子窗口失败: {exc}")
        return None
    return {who for who in (subwindow_who(chat) for chat in (chats or [])) if who}


def _dynamic_listener_entry_name(entry):
    if isinstance(entry, (list, tuple)) and entry:
        return str(entry[0] or "").strip()
    return str(entry or "").strip()


def has_dynamic_listener_entry(bot, nickname):
    nickname = str(nickname or "").strip()
    if not nickname:
        return False
    return any(
        _dynamic_listener_entry_name(item) == nickname
        for item in (getattr(bot, "all_Mode_listen_list", []) or [])
    )


def remove_dynamic_listener_entries(bot, nickname):
    nickname = str(nickname or "").strip()
    runtime_list = getattr(bot, "all_Mode_listen_list", None)
    if not nickname or not isinstance(runtime_list, list):
        return False
    kept = [item for item in runtime_list if _dynamic_listener_entry_name(item) != nickname]
    removed = len(kept) != len(runtime_list)
    if removed:
        runtime_list[:] = kept
    return removed


def touch_dynamic_listener_entry(bot, nickname, timestamp=None):
    nickname = str(nickname or "").strip()
    if not nickname:
        return False
    runtime_list = getattr(bot, "all_Mode_listen_list", None)
    if not isinstance(runtime_list, list):
        return False
    now_ts = time.time() if timestamp is None else float(timestamp)
    for item in runtime_list:
        if _dynamic_listener_entry_name(item) != nickname:
            continue
        if isinstance(item, list):
            if len(item) >= 2:
                item[1] = now_ts
            else:
                item.append(now_ts)
        elif isinstance(item, tuple):
            index = runtime_list.index(item)
            runtime_list[index] = [nickname, now_ts]
        else:
            index = runtime_list.index(item)
            runtime_list[index] = [nickname, now_ts]
        return True
    runtime_list.append([nickname, now_ts])
    return True


def _forget_runtime_listener_caches(bot, nickname):
    runtime_chat_state.remove_listen_chat(bot, nickname)
    material_chats = getattr(bot, "_material_source_chats", None)
    if isinstance(material_chats, dict):
        material_chats.pop(str(nickname or "").strip(), None)


def _call_remove_listen_chat(bot, nickname):
    remove_listen_chat = getattr(getattr(bot, "wx", None), "RemoveListenChat", None)
    if not callable(remove_listen_chat):
        raise RuntimeError("当前微信客户端不支持删除监听")
    try:
        return remove_listen_chat(nickname, close_window=True)
    except TypeError:
        return remove_listen_chat(nickname)


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
        if not has_dynamic_listener_entry(bot, nickname):
            continue
        remove_fn = getattr(bot, "_remove_listen_chat_verified", None)
        removed = remove_fn(nickname) if callable(remove_fn) else remove_listen_chat_verified(bot, nickname)
        if removed:
            remove_dynamic_listener_entries(bot, nickname)
            closed_names.append(nickname)
    return closed_names


def add_listen_chat_once(bot, nickname, label, *, allow_rebind=False):
    quiet_labels = {"动态监听", "轻量延后监听"}
    label_text = str(label or "").strip()
    log_level = "WARNING" if label_text in quiet_labels else "ERROR"

    def add_action():
        with wechat_ui_actions.hold(bot):
            with warn_slow_wechat_ui_action(f"AddListenChat({nickname})"):
                return bot.wx.AddListenChat(nickname=nickname, callback=bot.message_handle_callback)

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


def _set_last_dynamic_add_result(bot, chat_name, result=None, *, stale=False):
    bot._last_dynamic_add_result = {
        "chat": str(chat_name or "").strip(),
        "result": result,
        "stale": bool(stale),
        "error": listen_add_error(result),
        "at": time.time(),
    }


def _consume_last_dynamic_add_result(bot, chat_name):
    name = str(chat_name or "").strip()
    info = getattr(bot, "_last_dynamic_add_result", None)
    if not isinstance(info, dict) or str(info.get("chat") or "").strip() != name:
        return {}
    bot._last_dynamic_add_result = None
    return info


def ensure_lightweight_delayed_listen_state(bot):
    if not hasattr(bot, "_lightweight_delayed_listen_tasks") or bot._lightweight_delayed_listen_tasks is None:
        bot._lightweight_delayed_listen_tasks = {}
    if not hasattr(bot, "_lightweight_delayed_listen_last_rebuild_at") or bot._lightweight_delayed_listen_last_rebuild_at is None:
        bot._lightweight_delayed_listen_last_rebuild_at = {}
    if not hasattr(bot, "_lightweight_delayed_listen_flushing"):
        bot._lightweight_delayed_listen_flushing = False
    if not hasattr(bot, "_degraded_dynamic_listeners") or bot._degraded_dynamic_listeners is None:
        bot._degraded_dynamic_listeners = {}


def _has_due_lightweight_delayed_listen_task(bot, now_ts=None):
    tasks = getattr(bot, "_lightweight_delayed_listen_tasks", {}) or {}
    if not isinstance(tasks, dict):
        return False
    now_ts = time.time() if now_ts is None else float(now_ts)
    for task in tasks.values():
        if not isinstance(task, dict):
            continue
        try:
            due_at = float(task.get("due_at") or 0.0)
        except (TypeError, ValueError):
            due_at = 0.0
        if due_at <= now_ts:
            return True
    return False


def _queue_lightweight_delayed_listen(
    bot,
    chat_name,
    messages,
    *,
    reason="",
    allow_rebuild=False,
    recovered=False,
    now=None,
):
    name = str(chat_name or "").strip()
    msgs = list(messages or [])
    if not name or not msgs:
        return False
    ensure_lightweight_delayed_listen_state(bot)
    now_ts = time.time() if now is None else float(now)
    due_at = now_ts + LIGHTWEIGHT_DELAYED_LISTEN_DELAY_SECONDS
    tasks = bot._lightweight_delayed_listen_tasks
    task = tasks.get(name)
    if not isinstance(task, dict):
        task = {
            "chat": name,
            "messages": [],
            "message_keys": set(),
            "record_ids": {},
            "created_at": now_ts,
            "due_at": due_at,
            "reason": str(reason or "").strip(),
            "allow_rebuild": bool(allow_rebuild),
            "attempt_index": 0,
            "message_sequence": _get_bot_private_message_sequence(bot, name),
            "requires_snapshot_match": bool(recovered),
        }
        tasks[name] = task
    task["due_at"] = min(float(task.get("due_at") or due_at), due_at)
    task["reason"] = str(reason or task.get("reason") or "").strip()
    task["allow_rebuild"] = bool(task.get("allow_rebuild")) or bool(allow_rebuild)
    task["requires_snapshot_match"] = bool(task.get("requires_snapshot_match")) or bool(recovered)
    keys = task.get("message_keys")
    if not isinstance(keys, set):
        keys = set(keys or [])
        task["message_keys"] = keys
    queued = 0
    store = getattr(bot, "_unanswered_inbound_store", None)
    record_ids = task.setdefault("record_ids", {})
    for msg in msgs:
        key = message_unique_id(name, msg)
        if key in keys:
            continue
        if len(task["messages"]) >= LIGHTWEIGHT_DELAYED_LISTEN_MAX_MESSAGES_PER_CHAT:
            break
        keys.add(key)
        task["messages"].append(msg)
        record_id = str(getattr(msg, "_wxbot_awaiting_ui_record_id", "") or "")
        if not record_id and store is not None:
            record_id = store.begin(name, msg, chat_type="private", status="awaiting_ui")
            try:
                setattr(msg, "_wxbot_awaiting_ui_record_id", record_id)
            except Exception:
                pass
        if record_id:
            record_ids[key] = record_id
        queued += 1
    if queued <= 0:
        return False
    return True


def _set_delayed_record_status(bot, record_id, status):
    store = getattr(bot, "_unanswered_inbound_store", None)
    if not record_id or store is None:
        return
    if status == "resolved":
        store.resolve(record_id)
    else:
        store.set_status(record_id, status)


def _match_recovered_delayed_message(stored, snapshot):
    candidates = [
        item
        for item in snapshot or []
        if str(getattr(item, "sender", "") or "") == str(getattr(stored, "sender", "") or "")
        and str(getattr(item, "type", "") or "") == str(getattr(stored, "type", "") or "")
        and str(getattr(item, "attr", "") or "") == str(getattr(stored, "attr", "") or "")
    ]
    for field in ("id", "hash", "hash_text"):
        wanted = getattr(stored, field, None)
        if wanted in {None, ""}:
            continue
        exact = [item for item in candidates if getattr(item, field, None) == wanted]
        if len(exact) == 1:
            return exact[0]
        if len(exact) > 1:
            return None
    content = str(getattr(stored, "content", "") or "")
    candidates = [item for item in candidates if str(getattr(item, "content", "") or "") == content]
    wanted_time = str(getattr(stored, "time", "") or "")
    if wanted_time:
        same_time = [item for item in candidates if str(getattr(item, "time", "") or "") == wanted_time]
        if same_time:
            candidates = same_time
    return candidates[0] if len(candidates) == 1 else None


def _get_bot_private_message_sequence(bot, chat_name):
    getter = getattr(bot, "_get_private_message_sequence", None)
    if callable(getter):
        try:
            return int(getter(chat_name) or 0)
        except Exception:
            return 0
    return 0


def _is_bot_stop_requested(bot):
    stop_fn = getattr(bot, "is_stop_requested", None)
    if callable(stop_fn):
        try:
            return bool(stop_fn())
        except Exception:
            return False
    return False


def get_runtime_cached_subwindow(bot, nickname):
    name = str(nickname or "").strip()
    if not name:
        return None
    cached = runtime_chat_state.get_listen_chat(bot, name)
    if is_target_chat(cached, name):
        return cached
    if cached:
        runtime_chat_state.remove_listen_chat(bot, name)
    return None


def _rebuild_lightweight_delayed_listener(bot, chat_name):
    name = str(chat_name or "").strip()
    if not name:
        return None
    ensure_lightweight_delayed_listen_state(bot)
    now_ts = time.time()
    last_at = float(bot._lightweight_delayed_listen_last_rebuild_at.get(name, 0.0) or 0.0)
    if last_at and now_ts - last_at < LIGHTWEIGHT_DELAYED_LISTEN_REBUILD_COOLDOWN_SECONDS:
        _bot_log(
            bot,
            level="WARNING",
            message=f"全局监听 {name}：轻量延后监听命中 {LIGHTWEIGHT_DELAYED_LISTEN_REBUILD_COOLDOWN_SECONDS}s 冷却，本次不关闭重建",
        )
        return None
    existing = get_cached_or_verified_subwindow(bot, name)
    if existing:
        return existing
    _remove_listen_chat_verified_locked(bot, name, log_success=False)
    bot._lightweight_delayed_listen_last_rebuild_at[name] = time.time()
    try:
        with warn_slow_wechat_ui_action(f"AddListenChat({name})"):
            result = bot.wx.AddListenChat(nickname=name, callback=bot.message_handle_callback)
    except Exception as exc:
        _bot_log(bot, level="WARNING", message=f"全局监听 {name}：轻量延后监听重建异常，稍后继续等待，详情：{exc}")
        return None
    if is_target_chat(result, name):
        runtime_chat_state.remember_listen_chat(bot, name, result)
        touch_dynamic_listener_entry(bot, name)
        return result
    sub_chat = get_verified_subwindow_with_retry(bot, name, retry_count=3)
    if sub_chat:
        runtime_chat_state.remember_listen_chat(bot, name, sub_chat)
        touch_dynamic_listener_entry(bot, name)
        return sub_chat
    _bot_log(bot, level="WARNING", message=f"全局监听 {name}：轻量延后监听重建失败，稍后继续等待，详情：{listen_add_error(result)}")
    return None


def _reschedule_lightweight_delayed_listen(bot, name, task, now_ts):
    attempt_index = int(task.get("attempt_index") or 0)
    next_index = attempt_index + 1
    created_at = float(task.get("created_at") or now_ts)
    if now_ts - created_at >= LIGHTWEIGHT_DELAYED_LISTEN_ESCALATION_SECONDS:
        for record_id in (task.get("record_ids") or {}).values():
            _set_delayed_record_status(bot, record_id, "listen_degraded")
        ensure_lightweight_delayed_listen_state(bot)
        bot._degraded_dynamic_listeners[name] = {
            "since": now_ts,
            "reason": "pending_window_exhausted",
            "pending_messages": len(task.get("messages") or []),
        }
        _bot_log(
            bot,
            level="ERROR",
            message=(
                f"全局监听 {name}：临时接管在 {LIGHTWEIGHT_DELAYED_LISTEN_ESCALATION_SECONDS}s 内始终未恢复，"
                "已停止自动重试并标记为降级；不会重绑微信客户端"
            ),
        )
        return False
    if next_index < len(LIGHTWEIGHT_DELAYED_LISTEN_ATTEMPT_DELAYS_SECONDS):
        next_delay = LIGHTWEIGHT_DELAYED_LISTEN_ATTEMPT_DELAYS_SECONDS[next_index]
        next_due = created_at + next_delay
    else:
        next_delay = LIGHTWEIGHT_DELAYED_LISTEN_RETRY_SECONDS
        next_due = now_ts + next_delay
    task["attempt_index"] = next_index
    task["due_at"] = next_due
    bot._lightweight_delayed_listen_tasks[name] = task
    if next_index < len(LIGHTWEIGHT_DELAYED_LISTEN_ATTEMPT_DELAYS_SECONDS):
        message = (
            f"全局监听 {name}：轻量延后监听第 {attempt_index + 1} 次未恢复，"
            f"将在 {next_delay}s 后再试一次"
        )
    else:
        message = (
            f"全局监听 {name}：轻量延后监听第 {attempt_index + 1} 次未恢复，"
            f"将在 {next_delay}s 后继续等待窗口恢复"
        )
    _bot_log(
        bot,
        level="INFO",
        message=message,
    )
    return True


def flush_lightweight_delayed_listen_tasks(bot, *, limit=1):
    ensure_lightweight_delayed_listen_state(bot)
    if getattr(bot, "_lightweight_delayed_listen_flushing", False):
        return False
    if _is_bot_stop_requested(bot):
        bot._lightweight_delayed_listen_tasks.clear()
        return False
    now_ts = time.time()
    due_items = [
        (name, task)
        for name, task in list(bot._lightweight_delayed_listen_tasks.items())
        if float((task or {}).get("due_at") or 0.0) <= now_ts
    ]
    if not due_items:
        return False
    bot._lightweight_delayed_listen_flushing = True
    handled = False
    try:
        for name, task in due_items[: max(1, int(limit or 1))]:
            sub_chat = get_runtime_cached_subwindow(bot, name)
            current = bot._lightweight_delayed_listen_tasks.pop(name, None)
            if not current:
                continue
            if _get_bot_private_message_sequence(bot, name) != int(current.get("message_sequence") or 0):
                _bot_log(bot, level="INFO", message=f"全局监听 {name}：轻量延后监听期间已有新消息处理，已放弃旧批次")
                for record_id in (current.get("record_ids") or {}).values():
                    _set_delayed_record_status(bot, record_id, "uncertain")
                handled = True
                continue
            if not sub_chat:
                release_wechat_lock = wechat_ui_actions.try_acquire(bot)
                if not release_wechat_lock:
                    bot._lightweight_delayed_listen_tasks[name] = current
                    continue
                try:
                    sub_chat = get_cached_or_verified_subwindow(bot, name)
                    if not sub_chat:
                        if bool(current.get("allow_rebuild")):
                            sub_chat = _rebuild_lightweight_delayed_listener(bot, name)
                        else:
                            sub_chat = add_chat_to_listen(bot, name)
                finally:
                    release_wechat_lock()
            if not sub_chat:
                _reschedule_lightweight_delayed_listen(bot, name, current, now_ts)
                handled = True
                continue
            messages = list(current.get("messages") or [])
            if current.get("requires_snapshot_match"):
                try:
                    snapshot = list(sub_chat.GetAllMessage() or [])
                except Exception as exc:
                    current["due_at"] = now_ts + LIGHTWEIGHT_DELAYED_LISTEN_RETRY_SECONDS
                    bot._lightweight_delayed_listen_tasks[name] = current
                    _bot_log(bot, level="WARNING", message=f"全局监听 {name}：重启恢复读取子窗口失败，稍后继续，详情：{exc}")
                    handled = True
                    continue
                matched = []
                unmatched = []
                original_record_ids = dict(current.get("record_ids") or {})
                for msg in messages:
                    fresh = _match_recovered_delayed_message(msg, snapshot)
                    if fresh is None:
                        unmatched.append(msg)
                    else:
                        matched.append((msg, fresh))
                failed = []
                recovered_count = 0
                for stored, fresh in matched:
                    try:
                        process_listen_message(bot, sub_chat, fresh)
                    except Exception as exc:
                        failed.append(stored)
                        _bot_log(bot, level="WARNING", message=f"全局监听 {name}：重启恢复消息处理失败，已保留待重试，详情：{exc}")
                        continue
                    key = message_unique_id(name, stored)
                    _set_delayed_record_status(bot, original_record_ids.get(key), "resolved")
                    recovered_count += 1
                pending = unmatched + failed
                if pending:
                    current["messages"] = pending
                    current["message_keys"] = {message_unique_id(name, msg) for msg in pending}
                    current["record_ids"] = {
                        key: record_id
                        for key, record_id in original_record_ids.items()
                        if key in current["message_keys"]
                    }
                    current["due_at"] = now_ts + LIGHTWEIGHT_DELAYED_LISTEN_RETRY_SECONDS
                    bot._lightweight_delayed_listen_tasks[name] = current
                _bot_log(
                    bot,
                    level="INFO" if recovered_count else "WARNING",
                    message=f"全局监听 {name}：重启恢复成功处理 {recovered_count} 条，仍待确认 {len(pending)} 条",
                )
                handled = True
                continue
            _bot_log(bot, level="INFO", message=f"全局监听 {name}：轻量延后监听恢复成功，开始处理 {len(messages)} 条暂存消息")
            for index, msg in enumerate(messages):
                try:
                    process_listen_message(bot, sub_chat, msg)
                except Exception as exc:
                    pending = messages[index:]
                    current["messages"] = pending
                    current["message_keys"] = {message_unique_id(name, item) for item in pending}
                    current["record_ids"] = {
                        key: record_id
                        for key, record_id in (current.get("record_ids") or {}).items()
                        if key in current["message_keys"]
                    }
                    current["due_at"] = now_ts + LIGHTWEIGHT_DELAYED_LISTEN_RETRY_SECONDS
                    bot._lightweight_delayed_listen_tasks[name] = current
                    _bot_log(bot, level="WARNING", message=f"全局监听 {name}：暂存消息处理失败，已保留待重试，详情：{exc}")
                    break
                key = message_unique_id(name, msg)
                _set_delayed_record_status(bot, (current.get("record_ids") or {}).get(key), "resolved")
            handled = True
        return handled
    finally:
        bot._lightweight_delayed_listen_flushing = False


def get_cached_or_verified_subwindow(bot, nickname):
    name = str(nickname or "").strip()
    if not name:
        return None
    cached = runtime_chat_state.get_listen_chat(bot, name)
    if cached and not isinstance(cached, dict) and subwindow_who(cached) == name:
        return cached
    if cached:
        runtime_chat_state.remove_listen_chat(bot, name)
    sub_chat = get_verified_subwindow(bot, name)
    if sub_chat:
        runtime_chat_state.remember_listen_chat(bot, name, sub_chat)
        return sub_chat
    return None


def add_and_verify_subwindow(bot, nickname, retry_count=3):
    name = str(nickname or "").strip()
    if not name:
        return None
    _set_last_dynamic_add_result(bot, name, None, stale=False)
    sub_chat = get_cached_or_verified_subwindow(bot, name)
    if sub_chat:
        return sub_chat
    result = add_listen_chat_once(bot, name, "动态监听")
    if is_target_chat(result, name):
        runtime_chat_state.remember_listen_chat(bot, name, result)
        _bot_log(bot, level="DEBUG", message=f"监听管理 {name}：AddListenChat 返回可用子窗口，已直接接管")
        return result
    sub_chat = get_verified_subwindow_with_retry(bot, name, retry_count=retry_count)
    if sub_chat:
        runtime_chat_state.remember_listen_chat(bot, name, sub_chat)
        return sub_chat
    if is_stale_listen_registration_error(result):
        _bot_log(bot, level="WARNING", message=f"{name} 已存在监听登记但未获取到可用子窗口，本次不删除重建")
        _set_last_dynamic_add_result(bot, name, result, stale=True)
    else:
        _set_last_dynamic_add_result(bot, name, result, stale=False)
    return None


def expected_listener_names(bot):
    expected = []
    if not getattr(bot.config, "AllListen_switch", False):
        expected.extend(str(item or "").strip() for item in (getattr(bot.config, "listen_list", []) or []))
    if getattr(bot.config, "group_switch", False):
        expected.extend(str(item or "").strip() for item in (getattr(bot.config, "group", []) or []))
    material_source_runtime_enabled = getattr(bot, "_material_source_runtime_enabled", None)
    if callable(material_source_runtime_enabled) and material_source_runtime_enabled():
        expected.extend(
            iter_material_outreach_listen_sources(
                getattr(bot.config, "material_source_list", []),
                listen_list=getattr(bot.config, "listen_list", []),
                groups=getattr(bot.config, "group", []),
                group_switch=getattr(bot.config, "group_switch", False),
            )
        )

    names = []
    seen = set()
    for item in expected:
        name = str(item or "").strip()
        if name and name not in seen:
            seen.add(name)
            names.append(name)
    return names


def listener_registration_specs(bot):
    specs = []
    if not getattr(bot.config, "AllListen_switch", False):
        specs.extend(
            ("用户", str(item or "").strip(), False)
            for item in (getattr(bot.config, "listen_list", []) or [])
        )
    if getattr(bot.config, "group_switch", False):
        specs.extend(
            ("群组", str(item or "").strip(), False)
            for item in (getattr(bot.config, "group", []) or [])
        )
    material_source_runtime_enabled = getattr(bot, "_material_source_runtime_enabled", None)
    if callable(material_source_runtime_enabled) and material_source_runtime_enabled():
        specs.extend(
            ("素材投喂监听源", str(source or "").strip(), True)
            for source in iter_material_outreach_listen_sources(
                getattr(bot.config, "material_source_list", []),
                listen_list=getattr(bot.config, "listen_list", []),
                groups=getattr(bot.config, "group", []),
                group_switch=getattr(bot.config, "group_switch", False),
            )
        )
    for item in getattr(bot, "all_Mode_listen_list", []) or []:
        name = ""
        if isinstance(item, (list, tuple)) and item:
            name = str(item[0] or "").strip()
        else:
            name = str(item or "").strip()
        specs.append(("动态监听", name, False))

    unique_specs = []
    seen = set()
    for label, name, cache_material in specs:
        if not name or name in seen:
            continue
        seen.add(name)
        unique_specs.append((label, name, cache_material))
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
        bot._material_source_chats = {}

    bot.wx.StopListening()
    _bot_sleep(bot, 1)
    bot.wx.StartListening()

    result = None
    expected_listeners = []
    for label, name, cache_material in listener_registration_specs(bot):
        _bot_sleep(bot, 0.5)
        result = add_listen_chat_once(bot, name, label, allow_rebind=True)
        expected_listeners.append(name)
        if is_target_chat(result, name):
            runtime_chat_state.remember_listen_chat(bot, name, result)
            if cache_material:
                bot._material_source_chats[name] = result

    verify_initial_listeners(bot, expected_listeners, retry_count=verify_retry_count)
    bot._listener_reconcile_last_at = time.time()
    _bot_log(bot, level="INFO", message=finish_message)
    return all(runtime_chat_state.get_listen_chat(bot, name) for name in expected_listeners)


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
        clear_listener_auto_recovery(bot)
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


def ensure_listener_subwindow(bot, nickname, retry_count=3):
    name = str(nickname or "").strip()
    if not name:
        return None
    sub_chat = get_verified_subwindow(bot, name)
    if sub_chat:
        runtime_chat_state.remember_listen_chat(bot, name, sub_chat)
        return sub_chat
    runtime_chat_state.remove_listen_chat(bot, name)
    sub_chat = add_and_verify_subwindow(bot, name, retry_count=retry_count)
    if sub_chat:
        _bot_log(bot, message=f"{name} 监听子窗口已自动恢复")
    return sub_chat


def reconcile_listener_subwindows(bot, retry_count=3):
    if not getattr(bot, "wx", None):
        return []

    expected = expected_listener_names(bot)
    if not expected:
        return []

    listened_names = try_get_all_subwindow_names(bot)
    reopened = []
    for name in expected:
        if listened_names is not None and name in listened_names:
            sub_chat = get_verified_subwindow(bot, name)
            if sub_chat:
                runtime_chat_state.remember_listen_chat(bot, name, sub_chat)
                continue
        sub_chat = ensure_listener_subwindow(bot, name, retry_count=retry_count)
        if sub_chat:
            reopened.append(name)
    return reopened


def maybe_reconcile_listener_subwindows(bot, force=False, retry_count=3):
    if not getattr(bot, "wx", None):
        return []

    if not force and getattr(getattr(bot, "config", None), "AllListen_switch", False):
        ensure_lightweight_delayed_listen_state(bot)
        if (
            getattr(bot, "_lightweight_delayed_listen_flushing", False)
            or _has_due_lightweight_delayed_listen_task(bot)
        ):
            return []

    now_ts = time.time()
    interval = max(1, int(getattr(bot, "_listener_reconcile_interval_seconds", 30) or 30))
    last_at = float(getattr(bot, "_listener_reconcile_last_at", 0.0) or 0.0)
    if not force and last_at and now_ts - last_at < interval:
        return []

    release_wechat_lock = wechat_ui_actions.try_acquire(bot)
    if not release_wechat_lock:
        return []
    try:
        reopened = reconcile_listener_subwindows(bot, retry_count=retry_count)
        bot._listener_reconcile_last_at = now_ts
        return reopened
    finally:
        release_wechat_lock()


def remove_listen_chat_verified(bot, nickname, *, log_success=True):
    with wechat_ui_actions.hold(bot):
        return _remove_listen_chat_verified_locked(bot, nickname, log_success=log_success)


def _remove_listen_chat_verified_locked(bot, nickname, *, log_success=True):
    name = str(nickname or "").strip()
    try:
        with warn_slow_wechat_ui_action(f"RemoveListenChat({name})"):
            remove_result = _call_remove_listen_chat(bot, name)
        remove_result_text = str(listen_add_error(remove_result)).strip()
        if log_success and not (remove_result_text.lower() in {"ok", "success", "true"} or remove_result_text in {"成功", "已成功"}):
            _bot_log(bot, message=f"监听管理 {name}：删除监听结果：{remove_result_text}")
    except Exception as exc:
        _bot_log(bot, level="WARNING", message=f"监听管理 {name}：删除监听调用异常，已清理运行缓存，详情：{exc}")

    _forget_runtime_listener_caches(bot, name)
    remove_dynamic_listener_entries(bot, name)
    if log_success:
        _bot_log(bot, message=f"监听管理 {name}：删除监听完成，已清理运行缓存")
    return True


def verify_initial_listeners(bot, expected_chats, retry_count=3):
    expected = []
    seen = set()
    for item in expected_chats or []:
        name = str(item or "").strip()
        if name and name not in seen:
            seen.add(name)
            expected.append(name)
    if not expected:
        return

    listened_names = try_get_all_subwindow_names(bot)
    missing = list(expected) if listened_names is None else [name for name in expected if name not in listened_names]
    for name in expected:
        if name not in missing:
            sub_chat = get_verified_subwindow(bot, name)
            if sub_chat:
                runtime_chat_state.remember_listen_chat(bot, name, sub_chat)
    if not missing:
        _bot_log(bot, level="DEBUG", message="监听管理：初始化监听子窗口校验通过")
        return

    for name in missing:
        sub_chat = add_and_verify_subwindow(bot, name, retry_count=retry_count)
        if not sub_chat:
            _bot_log(bot, level="ERROR", message=f"{name} 初始化监听子窗口重试失败，已跳过运行缓存")


def init_wx_listeners(bot):
    """Initialize WeChat client and listener registrations."""
    if getattr(bot, "_use_ui_owner", False):
        specs = listener_registration_specs(bot)
        identity = bot._bootstrap_ui_owner([name for _label, name, _cache_material in specs])
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
        _base = os.path.dirname(sys.executable) if hasattr(sys, "_MEIPASS") else os.path.abspath(".")
        bot.memory_manager = MemoryManager(wx_id, os.path.join(_base, "data"))
        bot._init_prompt_system(str(account_area_dir(os.path.join(_base, "data"), wx_id, "chat_memory", create=True)))
        bot._listen_chats = {}
        for _label, name, cache_material in specs:
            chat = OwnedChat(bot._ui_owner, name)
            runtime_chat_state.remember_listen_chat(bot, name, chat)
            if cache_material:
                bot._material_source_chats[name] = chat
        drain_recovery = getattr(bot, "_drain_unanswered_inbound_recovery", None)
        if callable(drain_recovery):
            drain_recovery()
        bot._register_runtime_task_schedules()
        _bot_log(bot, level="DEBUG", message="监听器初始化完成")
        return True

    if not getattr(bot, "wx", None):
        _bot_log(bot, message="本次未获取客户端，正在初始化微信客户端...")
    bind_wechat_client(bot, force_rebind=not getattr(bot, "wx", None))

    bot.config.AtMe = "@" + bot.wx.nickname
    _bot_log(bot, level="DEBUG", message="绑定微信：" + bot.config.AtMe)

    try:
        my_info = bot.wx.GetMyInfo()
        wx_id = my_info.get("id", f"{bot.wx.nickname}")
    except Exception:
        wx_id = f"{bot.wx.nickname}"
    bot.wx_id = wx_id
    try:
        migrated_default = migrate_default_account(bot.config.DATA_DIR, wx_id)
        if migrated_default:
            _bot_log(bot, message=f"已将 default 账号目录迁移到 {wx_id}")
    except Exception as exc:
        _bot_log(bot, level="WARNING", message=f"default 账号目录迁移失败：{exc}")
    try:
        last_wx_id_file = os.path.join(bot.config.DATA_DIR, "config", "last_wx_id.txt")
        os.makedirs(os.path.dirname(last_wx_id_file), exist_ok=True)
        with open(last_wx_id_file, "w", encoding="utf-8") as f:
            f.write(str(wx_id or "").strip())
    except Exception:
        pass
    if hasattr(bot.config, "bind_account_wx_id"):
        bot.config.bind_account_wx_id(wx_id)
    bot._voice_reply_state = load_voice_reply_state(bot._voice_reply_state_path())
    bot._set_material_outreach_namespace(wx_id)
    _base = os.path.dirname(sys.executable) if hasattr(sys, "_MEIPASS") else os.path.abspath(".")
    memory_base = os.path.join(_base, "data")
    bot.memory_manager = MemoryManager(
        wx_id,
        memory_base,
    )
    bot._init_prompt_system(str(account_area_dir(os.path.join(_base, "data"), wx_id, "chat_memory", create=True)))
    _bot_log(bot, message=f"记忆管理器已初始化，微信号: {wx_id}")
    enqueue_memory_checks = getattr(bot, "_enqueue_existing_chat_memory_checks", None)
    if callable(enqueue_memory_checks):
        enqueue_memory_checks()
    rebuild_listener_runtime(bot, verify_retry_count=3, clear_runtime_cache=True, finish_message="监听器初始化完成")
    bot._register_runtime_task_schedules()
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

    def send_welcome(new_friend):
        _bot_sleep(bot, 5)
        if isinstance(chat, OwnedChat):
            guard = getattr(bot, "_config_ui_task_guard", None)
            task_key, task_version = guard("group_welcome") if callable(guard) else ("", 0)
            seed = "|".join([
                str(chat.who or ""),
                str(getattr(message, "id", "") or ""),
                str(getattr(message, "hash", "") or ""),
                str(getattr(message, "time", "") or ""),
                str(getattr(message, "content", "") or ""),
                str(new_friend or ""),
            ])
            try:
                return chat.SendActions(
                    [{"type": "text", "text": str(bot.config.group_welcome_msg or ""), "at": new_friend}],
                    task_key=task_key,
                    task_version=task_version,
                    delivery_id=f"group-welcome:{uuid.uuid5(uuid.NAMESPACE_URL, seed)}",
                )
            except wechat_ui_actions.IntentCancelled:
                _bot_log(bot, message=f"群欢迎设置已更新或关闭，已取消向 {new_friend} 发送旧欢迎语")
                return True
        with wechat_ui_actions.hold(bot):
            with bot._get_chat_send_lock(chat.who):
                return chat.SendMsg(msg=bot.config.group_welcome_msg, at=new_friend)

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
    owner = getattr(bot, "_ui_owner", None)
    if owner is not None:
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
        guard = getattr(bot, "_config_ui_task_guard", None)
        task_key, task_version = guard("new_friend") if callable(guard) else ("", 0)
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
            record_metric = getattr(bot, "_metric_increment", None)
            if callable(record_metric):
                record_metric("new_friend_accepted_count")
            _bot_log(bot, level="INFO", message="已通过" + str(item.get("send_name") or item.get("name") or "") + "的好友请求")
            try:
                archive_accepted_friend(bot, item)
            except Exception as exc:
                _bot_log(bot, level="WARNING", message=f"新好友已通过，但即时写入通讯录档案失败：{exc}")
            if welcome_actions:
                _bot_sleep(bot, 5)
                send_name = str(item.get("send_name") or item.get("name") or "")
                for index, action in enumerate(welcome_actions):
                    if str(action.get("type") or "") == "file":
                        prepared_action = {"type": "file", "path": str(action.get("path") or "")}
                    else:
                        prepared_action = {"type": "text", "text": str(action.get("content") or "")}
                    try:
                        owner.call(wechat_ui_actions.UIIntent(
                            wechat_ui_actions.UIIntentKind.SEND_ACTIONS,
                            {
                                "conversation": send_name,
                                "task_key": task_key,
                                "delivery_id": f"new-friend-welcome:{uuid.uuid4()}:{index}",
                                "actions": [prepared_action],
                            },
                            task_version=task_version,
                        ), wechat_ui_actions.UI_CALL_WAIT_TIMEOUT)
                    except wechat_ui_actions.IntentCancelled:
                        _bot_log(bot, message=f"新好友欢迎规则已更新或关闭，已停止向 {send_name} 发送旧欢迎内容")
                        break
                    delay = getattr(bot, "_inter_message_delay_or_stop", None)
                    if index < len(welcome_actions) - 1 and callable(delay):
                        delay()
        return True

    release_wechat_lock = wechat_ui_actions.try_acquire(bot)
    if not release_wechat_lock:
        _bot_log(bot, message="新好友检测跳过：微信操作锁占用中")
        return False
    try:
        with warn_slow_wechat_ui_action("GetNewFriends()"):
            new_friends = bot.wx.GetNewFriends(acceptable=True)
        _bot_sleep(bot, 1)
        if len(new_friends) != 0:
            _bot_log(bot, message="以下是新朋友：\n" + str(new_friends))
            for new in new_friends:
                accept_kwargs = {}
                send_name = new.name
                if bool(getattr(bot.config, "new_friend_archive_switch", True)):
                    send_name = build_new_friend_remark(
                        new.name,
                        prefix=bot.config.new_friend_remark_prefix,
                        suffix=bot.config.new_friend_remark_suffix,
                        prefix_timestamp=bot.config.new_friend_remark_prefix_timestamp,
                        suffix_timestamp=bot.config.new_friend_remark_suffix_timestamp,
                    )
                    accept_kwargs["remark"] = send_name
                    if bot.config.new_friend_tags:
                        accept_kwargs["tags"] = bot.config.new_friend_tags
                new.accept(**accept_kwargs)
                record_metric = getattr(bot, "_metric_increment", None)
                if callable(record_metric):
                    record_metric("new_friend_accepted_count")
                _bot_log(bot, level="INFO", message="已通过" + send_name + "的好友请求")
                bot.wx.SwitchToChat()
                _bot_sleep(bot, 5)
                if bool(getattr(bot.config, "new_friend_reply_switch", False)):
                    fallback_actions = list(iter_new_friend_welcome_actions(getattr(bot.config, "new_friend_msg", {})))
                    for index, action in enumerate(fallback_actions):
                        if action["type"] == "file":
                            bot.wx.SendFiles(who=send_name, filepath=action["path"])
                        else:
                            bot.wx.SendMsg(who=send_name, msg=action["content"])
                        delay = getattr(bot, "_inter_message_delay_or_stop", None)
                        if index < len(fallback_actions) - 1 and callable(delay):
                            delay()
                bot.wx.ChatWith(who="文件传输助手")
                _bot_sleep(bot, 1)
                bot.wx.SwitchToContact()
            _bot_sleep(bot, 1)
        bot.wx.SwitchToChat()
        _bot_sleep(bot, 1)
        return True
    finally:
        release_wechat_lock()


def listen_mode(bot):
    messages_dict = bot.wx.GetListenMessage()
    for chat in messages_dict:
        for message in messages_dict.get(chat, []):
            process_listen_message(bot, chat, message)


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


def add_chat_to_listen(bot, chat):
    add_verify_fn = getattr(bot, "_add_and_verify_subwindow", None)
    if callable(add_verify_fn):
        sub_chat = add_verify_fn(chat)
    else:
        sub_chat = add_and_verify_subwindow(bot, chat)
    if not sub_chat:
        return None
    is_listened_fn = getattr(bot, "is_chat_listened", None)
    if callable(is_listened_fn) and is_listened_fn(chat):
        touch_dynamic_listener_entry(bot, chat)
        return sub_chat
    touch_dynamic_listener_entry(bot, chat)
    _bot_log(bot, message=f"全局监听 {chat}：已加入监听")
    return sub_chat


def is_chat_listened(bot, chat):
    chat = str(chat or "").strip()
    return any(_dynamic_listener_entry_name(listen_chat) == chat for listen_chat in bot.all_Mode_listen_list)


def alllisten_mode(bot, last_time, timeout=10):
    flush_lightweight_delayed_listen_tasks(bot)

    def remove_timeout_listen(chat_time_out=600):
        protected_listeners = set(expected_listener_names(bot))
        for listen_chat in bot.all_Mode_listen_list[:]:
            if time.time() - listen_chat[1] >= chat_time_out:
                listen_name = listen_chat[0]
                if listen_name in protected_listeners:
                    continue
                remove_fn = getattr(bot, "_remove_listen_chat_verified", None)
                if callable(remove_fn):
                    removed = remove_fn(listen_name, log_success=False)
                else:
                    removed = remove_listen_chat_verified(bot, listen_name, log_success=False)
                if removed:
                    remove_dynamic_listener_entries(bot, listen_name)
                    _bot_log(bot, message=f"全局监听 {listen_name}：对话超时，已停止监听")

    def get_next_new_message():
        messages_new = bot.wx.GetNextNewMessage(
            filter_mute=bot.config.AllListen_filter_mute,
            callback=None,
            download_media=bool(getattr(bot.config, "chat_image_recognition_switch", False)),
        )
        chat = messages_new.get("chat_name")
        chat_type = messages_new.get("chat_type")
        msgs = messages_new.get("msg")

        if chat in bot.config.global_blacklist:
            _bot_log(bot, message=f"{chat} 为黑名单用户，跳过处理")
            return

        if msgs:
            is_listened_fn = getattr(bot, "is_chat_listened", None)

            processed_msgs = []
            import types as _types

            for msg in msgs:
                setattr(msg, "_wxbot_ingress_source", "global")
                record_runtime_inbound_event(bot, msg, chat_type)
                if msg.type == "voice":
                    if not bot.config.chat_voice_recognition_switch:
                        msg._skip_ai_reply = True

                if msg.attr == "self" and chat_type != "group":
                    memory_chat = _types.SimpleNamespace(who=chat, chat_type="private")
                    try:
                        should_skip_memory = False
                        should_skip = getattr(bot, "_should_skip_message_memory", None)
                        if callable(should_skip):
                            should_skip_memory = bool(should_skip(memory_chat, msg))
                        if bot.config.memory_switch and bot.memory_manager and not should_skip_memory:
                            saved_by_image_path = False
                            save_image_memory = getattr(bot, "_save_incoming_image_memory_message", None)
                            if msg.type == "image" and callable(save_image_memory):
                                saved_by_image_path = bool(save_image_memory(memory_chat, msg))
                            if not saved_by_image_path:
                                save_kwargs = {
                                    "chat_name": chat,
                                    "sender": msg.sender,
                                    "content": strip_voice_duration_metadata(msg.content) if msg.type == "voice" else msg.content,
                                    "msg_type": msg.type,
                                    "msg_attr": msg.attr,
                                    "max_count": bot.config.memory_max_count,
                                    "message_time": getattr(msg, "time", None) or getattr(msg, "_wxbot_received_at", None),
                                }
                                if msg.type == "image" and str(getattr(msg, "content", "") or "").strip():
                                    save_kwargs["image_paths"] = [str(msg.content).strip()]
                                bot.memory_manager.save_message(**save_kwargs)
                                mark_memory_dirty = getattr(bot, "_mark_chat_memory_dirty", None)
                                if callable(mark_memory_dirty):
                                    mark_memory_dirty(memory_chat, msg)
                        if not bool(getattr(msg, "_wxbot_private_reply_persisted_echo", False)):
                            consume_runtime_echo = getattr(bot, "_consume_private_reply_runtime_echo", None)
                            runtime_echo = (
                                bool(consume_runtime_echo(chat, getattr(msg, "content", "")))
                                if callable(consume_runtime_echo)
                                else False
                            )
                            if not runtime_echo:
                                handle_self_boundary = getattr(bot, "_handle_private_self_message_boundary", None)
                                if callable(handle_self_boundary):
                                    handle_self_boundary(memory_chat, msg)
                    except Exception as exc:
                        _bot_log(bot, level="WARNING", message=f"处理 self 消息失败: {exc}")
                    continue

                if msg.attr == "friend" and chat_type != "group":
                    should_save_memory = msg.type in {"image", "quote"}
                    if should_save_memory and bot.config.memory_switch and bot.memory_manager:
                        try:
                            memory_chat = _types.SimpleNamespace(who=chat, chat_type="private")
                            save_image_memory = getattr(bot, "_save_incoming_image_memory_message", None)
                            saved_by_image_path = False
                            if msg.type == "image" and callable(save_image_memory):
                                saved_by_image_path = bool(save_image_memory(memory_chat, msg))
                            if not saved_by_image_path:
                                save_kwargs = {
                                    "chat_name": chat,
                                    "sender": msg.sender,
                                    "content": strip_voice_duration_metadata(msg.content) if msg.type == "voice" else msg.content,
                                    "msg_type": msg.type,
                                    "msg_attr": msg.attr,
                                    "max_count": bot.config.memory_max_count,
                                    "message_time": getattr(msg, "time", None) or getattr(msg, "_wxbot_received_at", None),
                                }
                                if msg.type == "image" and str(getattr(msg, "content", "") or "").strip():
                                    save_kwargs["image_paths"] = [str(msg.content).strip()]
                                bot.memory_manager.save_message(**save_kwargs)
                                mark_memory_dirty = getattr(bot, "_mark_chat_memory_dirty", None)
                                if callable(mark_memory_dirty):
                                    mark_memory_dirty(memory_chat, msg)
                        except Exception as exc:
                            _bot_log(bot, level="WARNING", message=f"写入记忆失败: {exc}")

                    if bot._handle_material_source_message(_types.SimpleNamespace(who=chat), msg):
                        continue

                    processed_msgs.append(msg)
            if processed_msgs:
                ensure_lightweight_delayed_listen_state(bot)
                if chat in getattr(bot, "_lightweight_delayed_listen_tasks", {}):
                    task = bot._lightweight_delayed_listen_tasks.get(chat)
                    before_count = len((task or {}).get("messages") or []) if isinstance(task, dict) else 0
                    delayed_queued = _queue_lightweight_delayed_listen(bot, chat, processed_msgs)
                    if delayed_queued:
                        task = bot._lightweight_delayed_listen_tasks.get(chat)
                        after_count = len((task or {}).get("messages") or []) if isinstance(task, dict) else before_count
                        merged_count = max(0, after_count - before_count)
                        _bot_log(
                            bot,
                            level="INFO",
                            message=f"全局监听 {chat}：已有延后接管任务，已合并 {merged_count} 条新消息",
                        )
                    return
                add_chat_fn = getattr(bot, "add_chat_to_listen", None)
                sub_chat = None
                if chat_type != "group" and callable(is_listened_fn) and is_listened_fn(chat):
                    sub_chat = get_cached_or_verified_subwindow(bot, chat)
                    if sub_chat:
                        touch_dynamic_listener_entry(bot, chat)
                if not sub_chat:
                    sub_chat = add_chat_fn(chat) if callable(add_chat_fn) else add_chat_to_listen(bot, chat)
                if not sub_chat:
                    _forget_runtime_listener_caches(bot, chat)
                    remove_dynamic_listener_entries(bot, chat)
                    add_result = _consume_last_dynamic_add_result(bot, chat)
                    delayed_queued = _queue_lightweight_delayed_listen(
                        bot,
                        chat,
                        processed_msgs,
                        reason=add_result.get("error", ""),
                        allow_rebuild=bool(add_result.get("stale")),
                    )
                    if delayed_queued:
                        _bot_log(
                            bot,
                            level="INFO",
                            message=(
                                f"全局监听 {chat}：临时接管窗口不可用，"
                                f"已暂存 {len(processed_msgs)} 条并延后 {LIGHTWEIGHT_DELAYED_LISTEN_DELAY_SECONDS}s 重试"
                            ),
                        )
                    else:
                        _bot_log(bot, level="WARNING", message=f"全局监听 {chat}：临时接管窗口不可用，已清理运行状态并等待后续重试")
                    return
                for msg in processed_msgs:
                    process_listen_message(bot, sub_chat, msg)

    get_next_new_message()

    if time.time() - last_time >= timeout:
        remove_timeout_listen()
        return time.time()
    return last_time
