"""Wechat listening and ingress routing helpers."""

from __future__ import annotations

import os
import random
import sys
import time
from contextlib import nullcontext
from wxautox4 import WeChat

from core import runtime_chat_state
from core.account_storage import account_area_dir, migrate_default_account
from core.logger import log
from core.memory import MemoryManager
from core.wechat_window import (
    rebind_wechat_client as core_rebind_wechat_client,
    run_with_wechat_rebind_retry,
)
from core.wechat_observability import warn_slow_wechat_ui_action
from feature.custom_forward import iter_custom_forward_listen_sources
from feature.custom_forward_runtime import handle_custom_forward, handle_custom_forward_takeover
from feature.material_outreach import iter_material_outreach_listen_sources
from feature.new_friends import build_new_friend_remark, iter_new_friend_welcome_actions
from feature.voice_reply import load_voice_reply_state

LISTENER_RECOVERY_PROBE_INTERVAL_SECONDS = 5
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
)


def _bot_log(bot, *args, **kwargs) -> None:
    module = sys.modules.get(getattr(bot.__class__, "__module__", ""))
    log_fn = getattr(module, "log", log) if module else log
    log_fn(*args, **kwargs)


def _bot_sleep(bot, seconds: float) -> None:
    module = sys.modules.get(getattr(bot.__class__, "__module__", ""))
    time_module = getattr(module, "time", time) if module else time
    time_module.sleep(seconds)


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


def _wechat_action_lock_context(bot):
    lock_getter = getattr(bot, "_get_wechat_action_lock", None)
    if not callable(lock_getter):
        return nullcontext()
    return lock_getter()


def subwindow_who(chat):
    try:
        return str(getattr(chat, "who", "") or "").strip()
    except Exception:
        return ""


def get_verified_subwindow(bot, nickname):
    try:
        chat = bot.wx.GetSubWindow(nickname=nickname)
    except Exception as exc:
        _bot_log(bot, level="WARNING", message=f"监听管理 {nickname}：获取监听子窗口失败，详情：{exc}")
        return None
    if chat and subwindow_who(chat) == str(nickname).strip():
        return chat
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
    log_level = "WARNING" if str(label or "").strip() == "动态监听" else "ERROR"

    def add_action():
        with bot._get_wechat_action_lock():
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
        if str(label or "").strip() != "动态监听":
            _bot_log(
                bot,
                level=log_level,
                message=f"监听管理 {nickname}：{listen_add_action_label(label)}调用异常，详情：{exc}",
            )
        return None
    if result:
        if str(label or "").strip() != "动态监听":
            _bot_log(bot, message=f"监听管理 {nickname}：{listen_add_action_label(label)}调用成功")
    else:
        _bot_log(bot, level=log_level, message=f"监听管理 {nickname}：{listen_add_action_label(label)}失败，详情：{listen_add_error(result)}")
    return result


def is_stale_listen_registration_error(result):
    return not result and "已监听" in listen_add_error(result)


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
    sub_chat = get_cached_or_verified_subwindow(bot, name)
    if sub_chat:
        return sub_chat
    result = add_listen_chat_once(bot, name, "动态监听")
    sub_chat = get_verified_subwindow(bot, name)
    if sub_chat:
        runtime_chat_state.remember_listen_chat(bot, name, sub_chat)
        return sub_chat
    if is_stale_listen_registration_error(result):
        _bot_log(bot, level="WARNING", message=f"{name} 已存在监听登记但未获取到可用子窗口，本次不删除重建")
    return None


def expected_listener_names(bot):
    expected = [str(getattr(bot.config, "cmd", "") or "").strip()]
    if not getattr(bot.config, "AllListen_switch", False):
        expected.extend(str(item or "").strip() for item in (getattr(bot.config, "listen_list", []) or []))
    if getattr(bot.config, "group_switch", False):
        expected.extend(str(item or "").strip() for item in (getattr(bot.config, "group", []) or []))
    if getattr(bot.config, "custom_forward_switch", False):
        expected.extend(
            iter_custom_forward_listen_sources(
                getattr(bot.config, "custom_forward_list", []),
                listen_list=getattr(bot.config, "listen_list", []),
                groups=getattr(bot.config, "group", []),
                group_switch=getattr(bot.config, "group_switch", False),
                command_chat=getattr(bot.config, "cmd", ""),
            )
        )
    material_source_runtime_enabled = getattr(bot, "_material_source_runtime_enabled", None)
    if callable(material_source_runtime_enabled) and material_source_runtime_enabled():
        expected.extend(
            iter_material_outreach_listen_sources(
                getattr(bot.config, "material_source_list", []),
                listen_list=getattr(bot.config, "listen_list", []),
                groups=getattr(bot.config, "group", []),
                group_switch=getattr(bot.config, "group_switch", False),
                command_chat=getattr(bot.config, "cmd", ""),
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
    specs = [("管理员", str(getattr(bot.config, "cmd", "") or "").strip(), False)]
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
    if getattr(bot.config, "custom_forward_switch", False):
        specs.extend(
            ("自定义转发监听源", str(source or "").strip(), False)
            for source in iter_custom_forward_listen_sources(
                getattr(bot.config, "custom_forward_list", []),
                listen_list=getattr(bot.config, "listen_list", []),
                groups=getattr(bot.config, "group", []),
                group_switch=getattr(bot.config, "group_switch", False),
                command_chat=getattr(bot.config, "cmd", ""),
            )
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
                command_chat=getattr(bot.config, "cmd", ""),
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


def probe_listener_recovery_client(bot):
    client = bind_wechat_client(bot, force_rebind=True)
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

    _bot_log(bot, message="启动wxautox监听器...")
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
        if result:
            runtime_chat_state.remember_listen_chat(bot, name, result)
            if cache_material:
                bot._material_source_chats[name] = result

    verify_initial_listeners(bot, expected_listeners, retry_count=verify_retry_count)
    bot._listener_reconcile_last_at = time.time()
    _bot_log(bot, level="SUCCESS", message=finish_message)
    admin_name = str(getattr(bot.config, "cmd", "") or "").strip()
    return bool(admin_name and runtime_chat_state.get_listen_chat(bot, admin_name))


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
    except Exception as exc:
        if is_listener_recovery_desktop_error(exc):
            bot._listener_auto_recovery_probe_after = now_ts + LISTENER_RECOVERY_PROBE_INTERVAL_SECONDS
            if hasattr(bot, "callback_is_die"):
                bot.callback_is_die = False
            return "waiting"
        bot._listener_auto_recovery_attempted = True
        bot._listener_auto_recovery_last_error = str(exc or "")
        clear_listener_auto_recovery(bot)
        _bot_log(bot, level="ERROR", message=f"监听器自动恢复前探活失败：{exc}")
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
        clear_listener_auto_recovery(bot)
        _bot_log(bot, level="ERROR", message=f"监听器自动恢复失败：{exc}")
        return "failed"

    if recovered:
        clear_listener_auto_recovery(bot)
        if hasattr(bot, "callback_is_die"):
            bot.callback_is_die = False
        _bot_log(bot, level="SUCCESS", message="监听器已自动恢复")
        return "recovered"

    bot._listener_auto_recovery_last_error = "监听器自动恢复后管理员窗口仍不可用"
    clear_listener_auto_recovery(bot)
    _bot_log(bot, level="ERROR", message="监听器自动恢复失败，管理员监听未恢复")
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

    now_ts = time.time()
    interval = max(1, int(getattr(bot, "_listener_reconcile_interval_seconds", 30) or 30))
    last_at = float(getattr(bot, "_listener_reconcile_last_at", 0.0) or 0.0)
    if not force and last_at and now_ts - last_at < interval:
        return []

    lock = bot._get_wechat_action_lock()
    if not lock.acquire(blocking=False):
        return []
    try:
        reopened = reconcile_listener_subwindows(bot, retry_count=retry_count)
        bot._listener_reconcile_last_at = now_ts
        return reopened
    finally:
        lock.release()


def remove_listen_chat_verified(bot, nickname, *, log_success=True):
    with _wechat_action_lock_context(bot):
        return _remove_listen_chat_verified_locked(bot, nickname, log_success=log_success)


def _remove_listen_chat_verified_locked(bot, nickname, *, log_success=True):
    name = str(nickname or "").strip()
    try:
        with warn_slow_wechat_ui_action(f"RemoveListenChat({name})"):
            remove_result = _call_remove_listen_chat(bot, name)
        remove_result_text = str(listen_add_error(remove_result)).strip()
        if not (remove_result_text.lower() in {"ok", "success", "true"} or remove_result_text in {"成功", "已成功"}):
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
        _bot_log(bot, message="监听管理：初始化监听子窗口校验通过")
        return

    for name in missing:
        sub_chat = add_and_verify_subwindow(bot, name, retry_count=retry_count)
        if not sub_chat:
            _bot_log(bot, level="ERROR", message=f"{name} 初始化监听子窗口重试失败，已跳过运行缓存")


def init_wx_listeners(bot):
    """Initialize WeChat client and listener registrations."""
    if not getattr(bot, "wx", None):
        _bot_log(bot, message="本次未获取客户端，正在初始化微信客户端...")
    bind_wechat_client(bot, force_rebind=not getattr(bot, "wx", None))

    bot.config.AtMe = "@" + bot.wx.nickname
    _bot_log(bot, message="绑定@：" + bot.config.AtMe)

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
    bot._set_admin_moments_draft_namespace(wx_id)
    bot._set_admin_forward_draft_namespace(wx_id)
    _base = os.path.dirname(sys.executable) if hasattr(sys, "_MEIPASS") else os.path.abspath(".")
    memory_base = os.path.join(_base, "data")
    bot.memory_manager = MemoryManager(wx_id, memory_base)
    bot._init_prompt_system(str(account_area_dir(os.path.join(_base, "data"), wx_id, "conversation_memory", create=True)))
    _bot_log(bot, message=f"记忆管理器已初始化，微信号: {wx_id}")
    enqueue_memory_checks = getattr(bot, "_enqueue_existing_conversation_memory_checks", None)
    if callable(enqueue_memory_checks):
        enqueue_memory_checks()
    rebuild_listener_runtime(bot, verify_retry_count=3, clear_runtime_cache=True, finish_message="监听器初始化完成")
    bot._register_runtime_task_schedules()
    return runtime_chat_state.get_listen_chat(bot, bot.config.cmd)


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

    if "加入群聊" in message.content and random.random() < bot.config.group_welcome_random:
        new_friend = find_new_group_friend(message.content, 1)
        _bot_log(bot, message=f"{chat.who} 新群友:" + new_friend)
        _bot_sleep(bot, 5)
        result = chat.SendMsg(msg=bot.config.group_welcome_msg, at=new_friend)
    elif "加入了群聊" in message.content and random.random() < bot.config.group_welcome_random:
        new_friend = find_new_group_friend(message.content, 3)
        _bot_log(bot, message=f"{chat.who} 新群友:" + new_friend)
        _bot_sleep(bot, 5)
        result = chat.SendMsg(msg=bot.config.group_welcome_msg, at=new_friend)

    return result


def pass_new_friends(bot):
    lock = bot._get_wechat_action_lock()
    if not lock.acquire(blocking=False):
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
                    remark_configured = any([
                        bool(getattr(bot.config, "new_friend_remark_use_nickname", True)),
                        bool(str(getattr(bot.config, "new_friend_remark_prefix", "") or "").strip()),
                        bool(str(getattr(bot.config, "new_friend_remark_suffix", "") or "").strip()),
                        bool(getattr(bot.config, "new_friend_remark_prefix_timestamp", False)),
                        bool(getattr(bot.config, "new_friend_remark_suffix_timestamp", False)),
                    ])
                    if remark_configured:
                        send_name = build_new_friend_remark(
                            new.name,
                            prefix=bot.config.new_friend_remark_prefix,
                            suffix=bot.config.new_friend_remark_suffix,
                            use_nickname=bot.config.new_friend_remark_use_nickname,
                            prefix_timestamp=bot.config.new_friend_remark_prefix_timestamp,
                            suffix_timestamp=bot.config.new_friend_remark_suffix_timestamp,
                        )
                        accept_kwargs["remark"] = send_name
                    if bot.config.new_friend_tags:
                        accept_kwargs["tags"] = bot.config.new_friend_tags
                new.accept(**accept_kwargs)
                _bot_log(bot, message="已通过" + send_name + "的好友请求")
                bot.wx.SwitchToChat()
                _bot_sleep(bot, 5)
                if bool(getattr(bot.config, "new_friend_reply_switch", False)):
                    for action in iter_new_friend_welcome_actions(getattr(bot.config, "new_friend_msg", {})):
                        if action["type"] == "file":
                            bot.wx.SendFiles(who=send_name, filepath=action["path"])
                        else:
                            bot.wx.SendMsg(who=send_name, msg=action["content"])
                        bot.config.human_delay()
                bot.wx.ChatWith(who="文件传输助手")
                _bot_sleep(bot, 1)
                bot.wx.SwitchToContact()
            _bot_sleep(bot, 1)
        bot.wx.SwitchToChat()
        _bot_sleep(bot, 1)
        return True
    finally:
        lock.release()


def listen_mode(bot):
    messages_dict = bot.wx.GetListenMessage()
    for chat in messages_dict:
        for message in messages_dict.get(chat, []):
            bot.process_message(chat, message)


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


def next_message_handle(bot):
    all_message = bot.wx.GetAllMessage()
    return new_msg_get_plus(all_message)


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
    def remove_timeout_listen(chat_time_out=600):
        for listen_chat in bot.all_Mode_listen_list[:]:
            if time.time() - listen_chat[1] >= chat_time_out:
                listen_name = listen_chat[0]
                remove_fn = getattr(bot, "_remove_listen_chat_verified", None)
                if callable(remove_fn):
                    removed = remove_fn(listen_name, log_success=False)
                else:
                    removed = remove_listen_chat_verified(bot, listen_name, log_success=False)
                if removed:
                    remove_dynamic_listener_entries(bot, listen_name)
                    _bot_log(bot, message=f"全局监听 {listen_name}：对话超时，已停止监听")

    def get_next_new_message():
        next_callback_down_map = {}

        def next_callback(msg):
            nonlocal next_callback_down_map
            if bot.wx.chat_type != "group":
                _bot_log(bot, message=f"全局监听 {msg.sender}：收到私聊消息，内容：{msg.content}")
                any_img_enabled = bot.config.chat_image_recognition_switch
                any_voice_enabled = bot.config.chat_voice_recognition_switch
                try:
                    if any_voice_enabled and msg.type == "voice":
                        try:
                            voice_content = msg.to_text()
                            if voice_content:
                                next_callback_down_map[msg.id] = voice_content
                            else:
                                next_callback_down_map[msg.id] = {"voice_transcription_failed": True}
                                _bot_log(bot, "WARNING", "消息自动语音转文字失败")
                        except Exception:
                            next_callback_down_map[msg.id] = {"voice_transcription_failed": True}
                            _bot_log(bot, "WARNING", "消息自动语音转文字失败")
                    if any_img_enabled:
                        if msg.type == "image":
                            path = msg.download()
                            if path:
                                next_callback_down_map[msg.id] = path
                            else:
                                _bot_log(bot, "ERROR", "全局监听：图片下载失败，请尝试将 Windows 屏幕缩放设置为 100%")
                        elif msg.type == "quote":
                            path = msg.download_quote_image()
                            if path:
                                next_callback_down_map[msg.id] = path
                            else:
                                _bot_log(bot, "INFO", "引用内容不是图片或视频")
                except Exception as exc:
                    _bot_log(bot, level="ERROR", message=f"全局监听：图片下载失败，请尝试将 Windows 屏幕缩放设置为 100%，详情：{exc}")
            else:
                _bot_log(bot, "INFO", "私聊全局监听收到群聊消息，跳过")

        messages_new = bot.wx.GetNextNewMessage(filter_mute=bot.config.AllListen_filter_mute, callback=next_callback)
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
                if msg.type == "image":
                    if msg.id in next_callback_down_map:
                        msg.content = str(next_callback_down_map[msg.id])
                elif msg.type == "quote":
                    if msg.id in next_callback_down_map:
                        msg.content = msg.content + "+引用的图片:" + str(next_callback_down_map[msg.id])
                elif msg.type == "voice":
                    if msg.id in next_callback_down_map:
                        cached_voice = next_callback_down_map[msg.id]
                        if isinstance(cached_voice, dict) and cached_voice.get("voice_transcription_failed"):
                            msg._voice_transcription_failed = True
                        elif cached_voice is None:
                            msg._skip_ai_reply = True
                        else:
                            msg.content = str(cached_voice)
                    else:
                        msg._skip_ai_reply = True

                if msg.attr == "friend" and chat_type != "group":
                    if bot.config.memory_switch and bot.memory_manager:
                        try:
                            bot.memory_manager.save_message(
                                chat_name=chat,
                                sender=msg.sender,
                                content=msg.content,
                                msg_type=msg.type,
                                msg_attr=msg.attr,
                                max_count=bot.config.memory_max_count,
                            )
                            mark_memory_dirty = getattr(bot, "_mark_conversation_memory_dirty", None)
                            if callable(mark_memory_dirty):
                                mark_memory_dirty(_types.SimpleNamespace(who=chat, chat_type="private"), msg)
                        except Exception as exc:
                            _bot_log(bot, level="WARNING", message=f"写入记忆失败: {exc}")

                    if bot._handle_material_source_message(_types.SimpleNamespace(who=chat), msg):
                        continue

                    takeover_handled = False
                    if bot.config.custom_forward_switch:
                        try:
                            chat_ref = _types.SimpleNamespace(who=chat)
                            takeover_handled = handle_custom_forward_takeover(bot, chat_ref, msg)
                            if not takeover_handled:
                                handle_custom_forward(bot, chat_ref, msg)
                        except Exception as exc:
                            _bot_log(bot, level="ERROR", message=f"自定义转发处理出错: {exc}")

                    if not takeover_handled:
                        processed_msgs.append(msg)
            if processed_msgs:
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
                    _bot_log(bot, level="WARNING", message=f"全局监听 {chat}：临时接管窗口不可用，已清理运行状态并等待后续重试")
                    return
                for msg in processed_msgs:
                    bot.process_message(sub_chat, msg)

    get_next_new_message()

    if time.time() - last_time >= timeout:
        remove_timeout_listen()
        return time.time()
    return last_time
