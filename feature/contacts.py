"""Contact directory rules and runtime helpers."""

from __future__ import annotations

import copy
import os
import sys
from datetime import datetime, timedelta
from typing import Any

from core.contact_profiles import (
    apply_repaired_remark,
    directory_path as contact_directory_path,
    load_directory as load_contact_directory,
    merge_directory as merge_contact_directory,
    repair_candidates as contact_repair_candidates,
    save_directory as save_contact_directory,
)
from core.wechat_window import (
    close_top_windows_by_title,
    rebind_wechat_client as core_rebind_wechat_client,
    run_with_wechat_rebind_retry,
)
from core.logger import log
from feature import takeover_runtime
from feature.material_outreach import append_bounded_record


MODE_TEST = "test"
MODE_STANDARD = "standard"
MODE_FORCE = "force"

_MODE_SETTINGS = {
    MODE_TEST: {"count": 5, "interval": 1.5, "label": "快速测试"},
    MODE_STANDARD: {"count": 10, "interval": 1.0, "label": "立即建档"},
    MODE_FORCE: {"count": 20, "interval": 0.5, "label": "暴力建档"},
}

AUTO_BATCH_SIZE_CHOICES = (10, 20, 50)
AUTO_BATCH_SIZE_DEFAULT = 10
AUTO_INTERVAL_DEFAULT_MINUTES = 10
AUTO_INTERVAL_MIN_MINUTES = 5
AUTO_INTERVAL_MAX_MINUTES = 1440
AUTO_FULL_SCAN_DEFAULT_DAYS = 7
AUTO_FULL_SCAN_MIN_DAYS = 1
AUTO_FULL_SCAN_MAX_DAYS = 30
AUTO_WINDOW_START_DEFAULT = "00:00"
AUTO_WINDOW_END_DEFAULT = "23:59"
STOP_MAINTENANCE_HINT = "停止后续建档；当前已开始的这一批会先完成。"


def _clean_text(value: Any) -> str:
    return str(value or "").strip()


def _bot_log(bot, *args, **kwargs) -> None:
    module = sys.modules.get(getattr(bot.__class__, "__module__", ""))
    log_fn = getattr(module, "log", log) if module else log
    log_fn(*args, **kwargs)


def close_contact_directory_management_windows(bot) -> int:
    closed = close_top_windows_by_title("通讯录管理")
    if closed:
        _bot_log(bot, message=f"[通讯录维护] 已关闭残留通讯录管理窗口 {closed} 个")
    return closed


def rebind_wechat_client(bot):
    return core_rebind_wechat_client(bot)


def normalize_refresh_mode(mode: Any) -> str:
    mode = _clean_text(mode).lower()
    if mode in _MODE_SETTINGS:
        return mode
    return MODE_STANDARD


def refresh_batch_settings(mode: Any, interval: Any = None) -> dict[str, Any]:
    normalized = normalize_refresh_mode(mode)
    settings = dict(_MODE_SETTINGS[normalized])
    if interval is not None:
        try:
            settings["interval"] = max(0.1, float(interval))
        except (TypeError, ValueError):
            pass
    settings["mode"] = normalized
    return settings


def normalize_auto_maintenance_batch_size(value: Any) -> int:
    try:
        value = int(value)
    except (TypeError, ValueError):
        return AUTO_BATCH_SIZE_DEFAULT
    if value in AUTO_BATCH_SIZE_CHOICES:
        return value
    return AUTO_BATCH_SIZE_DEFAULT


def coerce_auto_maintenance_interval_minutes(value: Any) -> int:
    try:
        value = int(value)
    except (TypeError, ValueError):
        value = AUTO_INTERVAL_DEFAULT_MINUTES
    return max(AUTO_INTERVAL_MIN_MINUTES, min(AUTO_INTERVAL_MAX_MINUTES, value))


def coerce_auto_maintenance_full_scan_interval_days(value: Any) -> int:
    try:
        value = int(value)
    except (TypeError, ValueError):
        value = AUTO_FULL_SCAN_DEFAULT_DAYS
    return max(AUTO_FULL_SCAN_MIN_DAYS, min(AUTO_FULL_SCAN_MAX_DAYS, value))


def coerce_auto_maintenance_window_time(value: Any, default_value: str) -> str:
    text = _clean_text(value)
    fallback = _clean_text(default_value) or AUTO_WINDOW_START_DEFAULT
    if not text:
        return fallback
    parts = text.split(":")
    if len(parts) != 2:
        return fallback
    try:
        hour = int(parts[0])
        minute = int(parts[1])
    except (TypeError, ValueError):
        return fallback
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        return fallback
    return f"{hour:02d}:{minute:02d}"


def _auto_maintenance_window_minutes(value: Any, default_value: str) -> int:
    text = coerce_auto_maintenance_window_time(value, default_value)
    hour_text, minute_text = text.split(":")
    return int(hour_text) * 60 + int(minute_text)


def auto_maintenance_time_window_allows(
    start_value: Any,
    end_value: Any,
    *,
    now: Any = None,
) -> bool:
    current = now if isinstance(now, datetime) else _parse_maintenance_time(now) or datetime.now()
    current_minutes = current.hour * 60 + current.minute
    start_minutes = _auto_maintenance_window_minutes(start_value, AUTO_WINDOW_START_DEFAULT)
    end_minutes = _auto_maintenance_window_minutes(end_value, AUTO_WINDOW_END_DEFAULT)
    if start_minutes == end_minutes:
        return True
    if start_minutes < end_minutes:
        return start_minutes <= current_minutes <= end_minutes
    return current_minutes >= start_minutes or current_minutes <= end_minutes


def stop_maintenance_hint() -> str:
    return STOP_MAINTENANCE_HINT


def coerce_detail_list(result: Any) -> list[Any]:
    if result is None:
        return []
    if isinstance(result, list):
        return result
    if isinstance(result, tuple):
        return list(result)
    return [result]


def contact_name_matches(name: Any, start_name: Any) -> bool:
    start_name = _clean_text(start_name)
    if not start_name:
        return True
    return _clean_text(name).startswith(start_name)


def _detail_name(raw_detail: Any) -> str:
    if not isinstance(raw_detail, dict):
        return ""
    for key in ("备注", "remark", "昵称", "nickname", "name", "微信号", "wechat_id", "wxid"):
        value = _clean_text(raw_detail.get(key))
        if value:
            return value
    return ""


def next_start_name_from_details(raw_details: list[Any] | tuple[Any, ...] | None) -> str:
    details = list(raw_details or [])
    for raw_detail in reversed(details):
        name = _detail_name(raw_detail)
        if name:
            return name
    return ""


def build_refresh_run_policy(run_kind: str, *, count: int) -> dict[str, Any]:
    run_kind = _clean_text(run_kind) or "manual_standard"
    is_auto = run_kind == "auto_maintenance"
    return {
        "run_kind": run_kind,
        "max_total": 50 if run_kind == "manual_standard" else None,
        "retry_limit": 3,
        "prepare_window_once_per_batch": True,
        "count": max(1, int(count or 1)),
        "single_batch_only": is_auto,
    }


def analyze_refresh_batch(
    *,
    raw_details: list[Any] | tuple[Any, ...] | None,
    requested_count: int,
    current_start_name: Any,
    previous_next_start_name: Any = "",
) -> dict[str, Any]:
    details = list(raw_details or [])
    names = []
    for item in details:
        name = _detail_name(item)
        if name:
            names.append(name)
    next_start_name = names[-1] if names else ""
    unique_names = list(dict.fromkeys(names))
    repeat_count = max(0, len(names) - len(unique_names))
    repeated_tail = len(names) > 1 and len(unique_names) == 1
    previous_next = _clean_text(previous_next_start_name)
    current_start = _clean_text(current_start_name)
    advanced = bool(next_start_name) and _clean_text(next_start_name) not in {previous_next, current_start}
    short_batch = len(details) < max(1, int(requested_count or 1))
    if not details:
        return {
            "outcome": "empty_batch",
            "completed": False,
            "advanced": False,
            "next_start_name": "",
            "repeat_count": 0,
        }
    if repeated_tail or (not advanced and repeat_count > 0):
        return {
            "outcome": "suspicious_repeat",
            "completed": False,
            "advanced": False,
            "next_start_name": next_start_name,
            "repeat_count": repeat_count,
        }
    if short_batch and advanced:
        return {
            "outcome": "tail_complete",
            "completed": True,
            "advanced": True,
            "next_start_name": next_start_name,
            "repeat_count": repeat_count,
        }
    if not advanced:
        return {
            "outcome": "not_advanced",
            "completed": False,
            "advanced": False,
            "next_start_name": next_start_name,
            "repeat_count": repeat_count,
        }
    return {
        "outcome": "advanced",
        "completed": False,
        "advanced": True,
        "next_start_name": next_start_name,
        "repeat_count": repeat_count,
    }


def _parse_maintenance_time(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return value
    text = _clean_text(value)
    if not text:
        return None
    for fmt in ("%Y-%m-%d %H:%M:%S.%f", "%Y-%m-%dT%H:%M:%S.%f", "%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S"):
        try:
            return datetime.strptime(text, fmt)
        except ValueError:
            continue
    return None


def auto_maintenance_is_due(
    directory: dict[str, Any] | None,
    *,
    interval_minutes: Any,
    now: Any = None,
    not_before: Any = None,
) -> bool:
    maintenance = (directory or {}).get("maintenance") if isinstance(directory, dict) else {}
    if not isinstance(maintenance, dict):
        maintenance = {}
    if maintenance.get("status") == "running":
        return False
    if bool(maintenance.get("paused", False)):
        return False
    interval = coerce_auto_maintenance_interval_minutes(interval_minutes)
    current = now if isinstance(now, datetime) else _parse_maintenance_time(now) or datetime.now()
    last_attempt = _parse_maintenance_time(
        maintenance.get("last_attempted_at") or maintenance.get("last_batch_finished_at")
    )
    start_gate = _parse_maintenance_time(not_before)
    if start_gate is not None and current < start_gate + timedelta(minutes=interval):
        if last_attempt is None or last_attempt < start_gate:
            return False
    if last_attempt is None:
        return True
    return current >= last_attempt + timedelta(minutes=interval)


def auto_maintenance_full_scan_is_due(
    directory: dict[str, Any] | None,
    *,
    interval_days: Any,
    now: Any = None,
) -> bool:
    maintenance = (directory or {}).get("maintenance") if isinstance(directory, dict) else {}
    if not isinstance(maintenance, dict):
        maintenance = {}
    if _clean_text(maintenance.get("auto_cycle_status")) in {"running", "stalled"}:
        return True
    interval = coerce_auto_maintenance_full_scan_interval_days(interval_days)
    current = now if isinstance(now, datetime) else _parse_maintenance_time(now) or datetime.now()
    last_full_scan = _parse_maintenance_time(maintenance.get("last_full_scan_completed_at"))
    if last_full_scan is None:
        return True
    return current >= last_full_scan + timedelta(days=interval)


def effective_start_name(
    directory: dict[str, Any] | None,
    start_name: Any = "",
    *,
    use_saved_position: bool = True,
) -> str:
    explicit = _clean_text(start_name)
    if explicit:
        return explicit
    if not use_saved_position:
        return ""
    maintenance = (directory or {}).get("maintenance") if isinstance(directory, dict) else {}
    if isinstance(maintenance, dict):
        for key in ("next_start_name", "matched_name", "last_start_name"):
            value = _clean_text(maintenance.get(key))
            if value:
                return value
    return ""


def maintenance_snapshot(
    directory: dict[str, Any] | None,
    *,
    mode: Any,
    status: str,
    paused: bool | None = None,
    start_name: Any = "",
    count_returned: int | None = None,
    matched_name: Any = "",
    next_start_name: Any = "",
    last_error: Any = "",
    callback_names: list[str] | None = None,
    now: Any = None,
) -> dict[str, Any]:
    updated = copy.deepcopy(directory or {})
    updated.setdefault("maintenance", {})
    maintenance = updated["maintenance"]
    maintenance["mode"] = normalize_refresh_mode(mode)
    maintenance["status"] = _clean_text(status) or "idle"
    if paused is not None:
        maintenance["paused"] = bool(paused)
    maintenance["last_start_name"] = _clean_text(start_name)
    maintenance["matched_name"] = _clean_text(matched_name)
    maintenance["next_start_name"] = _clean_text(next_start_name)
    maintenance["last_error"] = _clean_text(last_error)
    if count_returned is not None:
        maintenance["collected_count"] = max(0, int(count_returned))
    if callback_names is not None:
        maintenance["last_callback_name"] = _clean_text(callback_names[-1]) if callback_names else ""
    stamp = now if isinstance(now, str) else (now or datetime.now()).strftime("%Y-%m-%d %H:%M:%S")
    if maintenance["status"] == "running":
        maintenance["last_attempted_at"] = stamp
    else:
        maintenance["last_batch_finished_at"] = stamp
    return updated


def prepare_contact_directory_window(bot) -> None:
    if not getattr(bot, "wx", None):
        raise RuntimeError("微信客户端未初始化，请先启动机器人并保持微信主窗口可用。")

    def switch_to_contact():
        switch = getattr(bot.wx, "SwitchToContact", None)
        if not callable(switch):
            raise RuntimeError("当前微信客户端不支持切换通讯录。")
        switch()
        close_contact_directory_management_windows(bot)
        _bot_log(bot, message="[通讯录维护] 已自动切换微信到前台通讯录")

    return run_with_wechat_rebind_retry(
        bot,
        switch_to_contact,
        cleanup=lambda: close_contact_directory_management_windows(bot),
        attempts=2,
        on_retry=lambda exc, _attempt: _bot_log(
            bot,
            level="WARNING",
            message=f"[通讯录维护] 切换通讯录失败，准备重新初始化微信客户端后重试：{exc}",
        ),
    )


def refresh_run_kind(mode: str, *, automatic: bool = False) -> str:
    if automatic:
        return "auto_maintenance"
    return "manual_force" if str(mode or "").strip().lower() == "force" else "manual_standard"


def contact_directory_run_label(mode: str, *, run_kind: str = "") -> str:
    run_kind = str(run_kind or "").strip().lower()
    if run_kind == "auto_maintenance":
        return "自动维护"
    normalized_mode = refresh_batch_settings(mode).get("mode", "standard")
    return {
        "test": "快速测试",
        "standard": "立即建档",
        "force": "暴力建档",
    }.get(normalized_mode, "通讯录维护")


def summarize_directory_growth(before_directory, after_directory) -> dict[str, int]:
    before_subjects = (before_directory or {}).get("subjects") or []
    after_subjects = (after_directory or {}).get("subjects") or []
    before_keys = {
        str(item.get("contact_key") or "").strip()
        for item in before_subjects
        if isinstance(item, dict) and str(item.get("contact_key") or "").strip()
    }
    after_keys = {
        str(item.get("contact_key") or "").strip()
        for item in after_subjects
        if isinstance(item, dict) and str(item.get("contact_key") or "").strip()
    }
    return {
        "new_unique_count": len(after_keys - before_keys),
        "directory_total_unique_count": len(after_keys),
    }


def contact_profiles_directory_file(bot):
    wx_id = str(getattr(bot, "wx_id", "") or "").strip()
    base_dir = bot.config.DATA_DIR
    return contact_directory_path(base_dir, wx_id), wx_id


def load_contact_profiles_directory(bot):
    directory_file, wx_id = contact_profiles_directory_file(bot)
    return load_contact_directory(directory_file, wx_id=wx_id), directory_file, wx_id


def contact_profiles_remark_repair_records_file(bot):
    directory_file, _wx_id = contact_profiles_directory_file(bot)
    return os.path.join(os.path.dirname(directory_file), "remark_repair_records.json")


def contact_directory_auto_maintenance_enabled(bot):
    if hasattr(bot, "contact_directory_auto_maintenance_switch"):
        return bool(getattr(bot, "contact_directory_auto_maintenance_switch"))
    return bool(getattr(bot.config, "contact_directory_auto_maintenance_switch", False))


def contact_directory_auto_maintenance_batch_size_value(bot):
    if hasattr(bot, "contact_directory_auto_maintenance_batch_size"):
        value = getattr(bot, "contact_directory_auto_maintenance_batch_size")
    else:
        value = getattr(bot.config, "contact_directory_auto_maintenance_batch_size", 10)
    return normalize_auto_maintenance_batch_size(value)


def contact_directory_auto_maintenance_interval_minutes_value(bot):
    if hasattr(bot, "contact_directory_auto_maintenance_interval_minutes"):
        value = getattr(bot, "contact_directory_auto_maintenance_interval_minutes")
    else:
        value = getattr(bot.config, "contact_directory_auto_maintenance_interval_minutes", 10)
    return coerce_auto_maintenance_interval_minutes(value)


def contact_directory_auto_maintenance_full_scan_interval_days_value(bot):
    if hasattr(bot, "contact_directory_auto_maintenance_full_scan_interval_days"):
        value = getattr(bot, "contact_directory_auto_maintenance_full_scan_interval_days")
    else:
        value = getattr(bot.config, "contact_directory_auto_maintenance_full_scan_interval_days", 7)
    return coerce_auto_maintenance_full_scan_interval_days(value)


def contact_directory_auto_maintenance_window_start_value(bot):
    if hasattr(bot, "contact_directory_auto_maintenance_window_start"):
        value = getattr(bot, "contact_directory_auto_maintenance_window_start")
    else:
        value = getattr(bot.config, "contact_directory_auto_maintenance_window_start", "00:00")
    return coerce_auto_maintenance_window_time(value, "00:00")


def contact_directory_auto_maintenance_window_end_value(bot):
    if hasattr(bot, "contact_directory_auto_maintenance_window_end"):
        value = getattr(bot, "contact_directory_auto_maintenance_window_end")
    else:
        value = getattr(bot.config, "contact_directory_auto_maintenance_window_end", "23:59")
    return coerce_auto_maintenance_window_time(value, "23:59")


def maintenance_now(now=None):
    if isinstance(now, datetime):
        return now
    if isinstance(now, str):
        for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S"):
            try:
                return datetime.strptime(now, fmt)
            except ValueError:
                continue
    return datetime.now()


def contact_directory_auto_maintenance_time_window_allows(bot, now=None):
    return auto_maintenance_time_window_allows(
        contact_directory_auto_maintenance_window_start_value(bot),
        contact_directory_auto_maintenance_window_end_value(bot),
        now=maintenance_now(now),
    )


def has_recent_chat_activity_for_contact_maintenance(bot, now=None):
    last_msg_time = getattr(bot, "last_msg_time", None)
    if not last_msg_time:
        return False
    last_dt = None
    if isinstance(last_msg_time, datetime):
        last_dt = last_msg_time
    elif isinstance(last_msg_time, str):
        for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S"):
            try:
                last_dt = datetime.strptime(last_msg_time, fmt)
                break
            except ValueError:
                continue
    if last_dt is None:
        return False
    current_dt = maintenance_now(now)
    return current_dt - last_dt < timedelta(minutes=1)


def is_contact_directory_auto_maintenance_idle(bot):
    mode, _target = takeover_runtime.get_workspace_mode(bot)
    return mode == takeover_runtime.IDLE_MODE


def contact_directory_auto_cycle_state(directory):
    maintenance = ((directory or {}).get("maintenance") or {}) if isinstance(directory, dict) else {}
    if not isinstance(maintenance, dict):
        maintenance = {}
    status = str(maintenance.get("auto_cycle_status") or "").strip().lower()
    if status not in {"idle", "running", "stalled", "completed", "reset_required"}:
        status = "idle"
    try:
        batches = max(0, int(maintenance.get("auto_cycle_batches_completed", 0) or 0))
    except (TypeError, ValueError):
        batches = 0
    try:
        retries = max(0, int(maintenance.get("auto_cycle_retry_count", 0) or 0))
    except (TypeError, ValueError):
        retries = 0
    return {
        "status": status,
        "started_at": str(maintenance.get("auto_cycle_started_at") or "").strip(),
        "next_start_name": str(maintenance.get("auto_cycle_next_start_name") or "").strip(),
        "last_progress_at": str(maintenance.get("auto_cycle_last_progress_at") or "").strip(),
        "last_outcome": str(maintenance.get("auto_cycle_last_outcome") or "").strip(),
        "last_restart_at": str(maintenance.get("auto_cycle_last_restart_at") or "").strip(),
        "last_full_scan_completed_at": str(maintenance.get("last_full_scan_completed_at") or "").strip(),
        "batches_completed": batches,
        "retry_count": retries,
    }


def write_contact_directory_auto_cycle_state(directory, *, now=None, **updates):
    directory = directory if isinstance(directory, dict) else {}
    maintenance = directory.setdefault("maintenance", {})
    stamp = maintenance_now(now).strftime("%Y-%m-%d %H:%M:%S")
    for key, value in updates.items():
        if key in {"auto_cycle_batches_completed", "auto_cycle_retry_count"}:
            try:
                maintenance[key] = max(0, int(value or 0))
            except (TypeError, ValueError):
                maintenance[key] = 0
        elif value is None:
            maintenance[key] = ""
        else:
            maintenance[key] = str(value).strip() if isinstance(value, str) else value
    maintenance.setdefault("auto_cycle_status", "idle")
    maintenance.setdefault("auto_cycle_started_at", "")
    maintenance.setdefault("auto_cycle_next_start_name", "")
    maintenance.setdefault("auto_cycle_last_progress_at", "")
    maintenance.setdefault("auto_cycle_last_outcome", "")
    maintenance.setdefault("auto_cycle_last_restart_at", "")
    maintenance.setdefault("auto_cycle_batches_completed", 0)
    maintenance.setdefault("auto_cycle_retry_count", 0)
    maintenance.setdefault("last_full_scan_completed_at", "")
    maintenance["auto_cycle_updated_at"] = stamp
    return directory


def save_contact_profiles_directory(bot, directory):
    directory_file, wx_id = contact_profiles_directory_file(bot)
    if wx_id and isinstance(directory, dict):
        directory["wx_id"] = wx_id
    save_contact_directory(directory_file, directory)
    return directory


def refresh_contact_profiles_single_batch(
    bot,
    mode="standard",
    start_name="",
    interval=None,
    *,
    use_saved_position=False,
    count_override=None,
    log_start_finish=True,
    previous_next_start_name="",
    run_kind="manual_standard",
    preserve_current_position=False,
    logical_start_name=None,
):
    if not getattr(bot, "wx", None):
        raise RuntimeError("微信客户端未初始化，请先启动机器人并保持微信主窗口可用。")

    load_directory_fn = getattr(bot, "_load_contact_profiles_directory", None)
    if callable(load_directory_fn):
        directory, directory_file, wx_id = load_directory_fn()
    else:
        directory, directory_file, wx_id = load_contact_profiles_directory(bot)
    if not wx_id:
        raise RuntimeError("当前微信号未初始化，请先启动机器人后再试。")

    settings = refresh_batch_settings(mode, interval)
    label_fn = getattr(bot, "_contact_directory_run_label", None)
    if callable(label_fn):
        mode_label = label_fn(settings["mode"], run_kind=run_kind)
    else:
        mode_label = contact_directory_run_label(settings["mode"], run_kind=run_kind)
    if count_override is not None:
        try:
            settings["count"] = max(1, int(count_override))
        except (TypeError, ValueError):
            pass
    callback_start_name = effective_start_name(
        directory,
        start_name,
        use_saved_position=bool(use_saved_position),
    )
    used_start_name = str(logical_start_name or callback_start_name or "").strip()
    if preserve_current_position:
        callback_start_name = ""
    if log_start_finish:
        _bot_log(bot, message=f"[通讯录维护] 开始{mode_label}，起点：{used_start_name or '通讯录头部'}")
    running_directory = maintenance_snapshot(
        directory,
        mode=settings["mode"],
        status="running",
        paused=False,
        start_name=used_start_name,
    )
    save_contact_directory(directory_file, running_directory)

    callback_names = []
    matched_name = ""

    def callback(name):
        nonlocal matched_name
        name_text = str(name or "")
        callback_names.append(name_text)
        matched = contact_name_matches(name_text, callback_start_name)
        if matched and not matched_name:
            matched_name = name_text
        return matched

    try:
        with bot._get_wechat_action_lock():
            def read_friend_details():
                prepare_window_fn = getattr(bot, "_prepare_contact_directory_window", None)
                if callable(prepare_window_fn):
                    prepare_window_fn()
                else:
                    prepare_contact_directory_window(bot)
                try:
                    return bot.wx.GetFriendDetails(
                        n=settings["count"],
                        callback=callback,
                        interval=settings["interval"],
                    )
                finally:
                    switch_to_chat = getattr(bot.wx, "SwitchToChat", None)
                    if callable(switch_to_chat):
                        try:
                            switch_to_chat()
                        except Exception as exc:
                            _bot_log(bot, level="WARNING", message=f"[通讯录维护] 切回聊天页失败：{exc}")
                    close_contact_directory_management_windows(bot)
            result = run_with_wechat_rebind_retry(
                bot,
                read_friend_details,
                cleanup=lambda: close_contact_directory_management_windows(bot),
                attempts=2,
                on_retry=lambda exc, _attempt: _bot_log(
                    bot,
                    level="WARNING",
                    message=f"[通讯录维护] 读取好友资料失败，重新初始化微信客户端后重试：{exc}",
                ),
            )
        raw_details = coerce_detail_list(result)
        total_details = len(raw_details)
        for index, detail in enumerate(raw_details, start=1):
            label = str(detail.get("备注") or detail.get("昵称") or detail.get("微信号") or f"联系人{index}")
            _bot_log(bot, message=f"[通讯录维护] 识别联系人 {index}/{total_details}：{label}")
        merged = merge_contact_directory(
            running_directory,
            raw_details,
            wx_id=wx_id,
            now=datetime.now(),
            mark_missing=False,
        )
        analysis = analyze_refresh_batch(
            raw_details=raw_details,
            requested_count=settings["count"],
            current_start_name=used_start_name,
            previous_next_start_name=previous_next_start_name,
        )
        next_start_name = str(analysis.get("next_start_name") or "") or next_start_name_from_details(raw_details)
        latest_directory = load_contact_directory(directory_file, wx_id=wx_id)
        externally_paused = bool(((latest_directory or {}).get("maintenance") or {}).get("paused", False))
        finished = maintenance_snapshot(
            merged,
            mode=settings["mode"],
            status="paused" if externally_paused else "idle",
            paused=externally_paused,
            start_name=used_start_name,
            count_returned=len(raw_details),
            matched_name=matched_name,
            next_start_name=next_start_name,
            callback_names=callback_names,
        )
        summarize_growth_fn = getattr(bot, "_summarize_directory_growth", None)
        if callable(summarize_growth_fn):
            growth = summarize_growth_fn(directory, finished)
        else:
            growth = summarize_directory_growth(directory, finished)
        finished_maintenance = finished.setdefault("maintenance", {})
        finished_maintenance["last_batch_unique_count"] = growth["directory_total_unique_count"]
        finished_maintenance["last_batch_new_unique_count"] = growth["new_unique_count"]
        finished_maintenance["last_batch_repeat_count"] = int(analysis.get("repeat_count", 0) or 0)
        finished_maintenance["last_batch_outcome"] = str(analysis.get("outcome") or "")
        finished_maintenance["retry_count"] = 0
        save_contact_directory(directory_file, finished)
        if log_start_finish:
            _bot_log(bot, message=f"[通讯录维护] {mode_label}完成，本次读取 {len(raw_details)} 个好友")
        return {
            "mode": settings["mode"],
            "wx_id": wx_id,
            "requested_start_name": str(start_name or "").strip(),
            "used_start_name": used_start_name,
            "matched_name": matched_name,
            "next_start_name": next_start_name,
            "count_requested": settings["count"],
            "count_returned": len(raw_details),
            "interval": settings["interval"],
            "callback_names": callback_names,
            "directory": finished,
            "stopped_early": externally_paused,
            "analysis": analysis,
            "completed": bool(analysis.get("completed")),
            "retry_count": 0,
            "stopped_reason": "",
            "run_kind": run_kind,
            "read_item_count": len(raw_details),
            "new_unique_count": growth["new_unique_count"],
            "directory_total_unique_count": growth["directory_total_unique_count"],
        }
    except Exception as exc:
        latest_directory = load_contact_directory(directory_file, wx_id=wx_id)
        externally_paused = bool(((latest_directory or {}).get("maintenance") or {}).get("paused", False))
        failed = maintenance_snapshot(
            running_directory,
            mode=settings["mode"],
            status="paused" if externally_paused else "error",
            paused=externally_paused,
            start_name=used_start_name,
            last_error=str(exc),
            callback_names=callback_names,
        )
        save_contact_directory(directory_file, failed)
        _bot_log(bot, level="ERROR", message=f"[通讯录维护] {mode_label}失败：{exc}")
        raise


def refresh_contact_profiles_batch(
    bot,
    mode="standard",
    start_name="",
    interval=None,
    *,
    use_saved_position=False,
    count_override=None,
    run_to_completion=False,
    automatic=False,
    preserve_current_position=False,
):
    settings = refresh_batch_settings(mode, interval)
    run_kind_fn = getattr(bot, "_refresh_run_kind", None)
    if callable(run_kind_fn):
        run_kind = run_kind_fn(mode, automatic=automatic)
    else:
        run_kind = refresh_run_kind(mode, automatic=automatic)
    if settings["mode"] == "test" or not run_to_completion:
        single_batch_fn = getattr(bot, "_refresh_contact_profiles_single_batch", None)
        if callable(single_batch_fn):
            result = single_batch_fn(
                mode=mode,
                start_name=start_name,
                interval=interval,
                use_saved_position=use_saved_position,
                count_override=count_override,
                run_kind=run_kind,
                preserve_current_position=preserve_current_position,
            )
        else:
            result = refresh_contact_profiles_single_batch(
                bot,
                mode=mode,
                start_name=start_name,
                interval=interval,
                use_saved_position=use_saved_position,
                count_override=count_override,
                run_kind=run_kind,
                preserve_current_position=preserve_current_position,
            )
        result["run_kind"] = run_kind
        return result

    initial_start_name = str(start_name or "").strip()
    current_start_name = initial_start_name
    total_count = 0
    batches_completed = 0
    stopped_early = False
    last_result = None
    seen_starts: set[str] = set()
    retry_count = 0
    stopped_reason = ""
    load_directory_fn = getattr(bot, "_load_contact_profiles_directory", None)
    if callable(load_directory_fn):
        initial_directory, _initial_directory_file, _initial_wx_id = load_directory_fn()
    else:
        initial_directory, _initial_directory_file, _initial_wx_id = load_contact_profiles_directory(bot)
    policy_count = settings["count"]
    if count_override is not None:
        try:
            policy_count = max(1, int(count_override))
        except (TypeError, ValueError):
            pass
    policy = build_refresh_run_policy(run_kind, count=policy_count)
    label_fn = getattr(bot, "_contact_directory_run_label", None)
    if callable(label_fn):
        mode_label = label_fn(settings["mode"], run_kind=run_kind)
    else:
        mode_label = contact_directory_run_label(settings["mode"], run_kind=run_kind)
    _bot_log(bot, message=f"[通讯录维护] 开始{mode_label}，起点：{initial_start_name or '通讯录头部'}")

    while True:
        preserve_current_position = batches_completed > 0 and retry_count == 0
        single_batch_fn = getattr(bot, "_refresh_contact_profiles_single_batch", None)
        if callable(single_batch_fn):
            result = single_batch_fn(
                mode=mode,
                start_name="" if preserve_current_position else current_start_name,
                interval=interval,
                use_saved_position=False,
                count_override=count_override,
                log_start_finish=False,
                previous_next_start_name=current_start_name,
                run_kind=run_kind,
                preserve_current_position=preserve_current_position,
                logical_start_name=current_start_name,
            )
        else:
            result = refresh_contact_profiles_single_batch(
                bot,
                mode=mode,
                start_name="" if preserve_current_position else current_start_name,
                interval=interval,
                use_saved_position=False,
                count_override=count_override,
                log_start_finish=False,
                previous_next_start_name=current_start_name,
                run_kind=run_kind,
                preserve_current_position=preserve_current_position,
                logical_start_name=current_start_name,
            )
        last_result = result
        total_count += int(result.get("count_returned", 0) or 0)
        batches_completed += 1

        directory = result.get("directory") or {}
        maintenance = directory.get("maintenance") or {}
        if bool(maintenance.get("paused")) or maintenance.get("status") == "paused":
            stopped_early = True
            stopped_reason = "paused"
            break

        analysis = result.get("analysis") or {}
        outcome = str(analysis.get("outcome") or "").strip()
        if outcome in {"suspicious_repeat", "not_advanced", "empty_batch"}:
            if retry_count >= int(policy.get("retry_limit", 0) or 0):
                stopped_reason = "stalled"
                result["completed"] = False
                result["retry_count"] = retry_count
                break
            retry_count += 1
            continue

        next_start_name = str(result.get("next_start_name", "") or "").strip()

        retry_count = 0
        if policy.get("max_total") is not None and total_count >= int(policy["max_total"]):
            stopped_reason = "manual_cap_reached"
            result["completed"] = False
            break

        if bool(analysis.get("completed")):
            stopped_reason = "directory_complete"
            result["completed"] = True
            break

        if not next_start_name or next_start_name == current_start_name or next_start_name in seen_starts:
            stopped_reason = "stalled"
            result["completed"] = False
            break

        seen_starts.add(next_start_name)
        current_start_name = next_start_name

    if last_result is None:
        raise RuntimeError("建档未启动，请重试")

    final_directory = last_result.get("directory") or {}
    summarize_growth_fn = getattr(bot, "_summarize_directory_growth", None)
    if callable(summarize_growth_fn):
        growth = summarize_growth_fn(initial_directory, final_directory)
    else:
        growth = summarize_directory_growth(initial_directory, final_directory)
    if stopped_early:
        _bot_log(bot, message=f"[通讯录维护] {mode_label}已停止，本次共读取 {total_count} 个好友")
    elif stopped_reason == "manual_cap_reached":
        _bot_log(bot, message=f"[通讯录维护] {mode_label}达到 50 人上限，本次共读取 {total_count} 个好友")
    elif stopped_reason == "stalled":
        _bot_log(bot, level="WARNING", message=f"[通讯录维护] {mode_label}疑似卡住，停止重试，本次累计读取 {total_count} 个条目")
    else:
        _bot_log(bot, message=f"[通讯录维护] {mode_label}完成，本次共读取 {total_count} 个好友")

    last_result["count_returned"] = total_count
    last_result["read_item_count"] = total_count
    last_result["batches_completed"] = batches_completed
    last_result["stopped_early"] = stopped_early
    last_result["requested_start_name"] = initial_start_name
    last_result["run_kind"] = run_kind
    last_result["retry_count"] = retry_count
    last_result["stopped_reason"] = stopped_reason
    last_result["completed"] = stopped_reason == "directory_complete"
    last_result["new_unique_count"] = growth["new_unique_count"]
    last_result["directory_total_unique_count"] = growth["directory_total_unique_count"]
    if stopped_reason == "directory_complete":
        stamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        cycle_state_fn = getattr(bot, "_contact_directory_auto_cycle_state", None)
        if callable(cycle_state_fn):
            cycle = cycle_state_fn(final_directory)
        else:
            cycle = contact_directory_auto_cycle_state(final_directory)
        write_cycle_fn = getattr(bot, "_write_contact_directory_auto_cycle_state", None)
        if callable(write_cycle_fn):
            final_directory = write_cycle_fn(
                final_directory,
                auto_cycle_status="completed" if automatic else "idle",
                auto_cycle_started_at="" if not automatic else (cycle["started_at"] or stamp),
                auto_cycle_next_start_name="",
                auto_cycle_last_progress_at=stamp,
                auto_cycle_last_outcome="completed",
                auto_cycle_last_restart_at="" if not automatic else cycle["last_restart_at"],
                auto_cycle_batches_completed=0 if not automatic else cycle["batches_completed"],
                auto_cycle_retry_count=0,
                last_full_scan_completed_at=stamp,
            )
        else:
            final_directory = write_contact_directory_auto_cycle_state(
                final_directory,
                auto_cycle_status="completed" if automatic else "idle",
                auto_cycle_started_at="" if not automatic else (cycle["started_at"] or stamp),
                auto_cycle_next_start_name="",
                auto_cycle_last_progress_at=stamp,
                auto_cycle_last_outcome="completed",
                auto_cycle_last_restart_at="" if not automatic else cycle["last_restart_at"],
                auto_cycle_batches_completed=0 if not automatic else cycle["batches_completed"],
                auto_cycle_retry_count=0,
                last_full_scan_completed_at=stamp,
            )
        save_directory_fn = getattr(bot, "_save_contact_profiles_directory", None)
        if callable(save_directory_fn):
            save_directory_fn(final_directory)
        else:
            save_contact_profiles_directory(bot, final_directory)
        last_result["directory"] = final_directory
    return last_result


def check_contact_directory_auto_maintenance(bot, now=None):
    if not getattr(bot, "wx", None):
        return False
    enabled_fn = getattr(bot, "_contact_directory_auto_maintenance_enabled", None)
    if callable(enabled_fn):
        enabled = enabled_fn()
    else:
        enabled = contact_directory_auto_maintenance_enabled(bot)
    if not enabled:
        return False

    load_directory_fn = getattr(bot, "_load_contact_profiles_directory", None)
    if callable(load_directory_fn):
        directory, _, wx_id = load_directory_fn()
    else:
        directory, _, wx_id = load_contact_profiles_directory(bot)
    if not wx_id:
        return False
    now_fn = getattr(bot, "_maintenance_now", None)
    if callable(now_fn):
        now_dt = now_fn(now)
    else:
        now_dt = maintenance_now(now)
    interval_minutes_fn = getattr(bot, "_contact_directory_auto_maintenance_interval_minutes_value", None)
    if callable(interval_minutes_fn):
        interval_minutes = interval_minutes_fn()
    else:
        interval_minutes = contact_directory_auto_maintenance_interval_minutes_value(bot)
    if not auto_maintenance_is_due(
        directory,
        interval_minutes=interval_minutes,
        now=now_dt,
        not_before=getattr(bot, "start_time", None),
    ):
        return False
    time_window_fn = getattr(bot, "_contact_directory_auto_maintenance_time_window_allows", None)
    if callable(time_window_fn):
        time_window_allows = time_window_fn(now=now_dt)
    else:
        time_window_allows = contact_directory_auto_maintenance_time_window_allows(bot, now=now_dt)
    if not time_window_allows:
        return False
    recent_chat_fn = getattr(bot, "_has_recent_chat_activity_for_contact_maintenance", None)
    if callable(recent_chat_fn):
        has_recent_chat = recent_chat_fn(now=now_dt)
    else:
        has_recent_chat = has_recent_chat_activity_for_contact_maintenance(bot, now=now_dt)
    if has_recent_chat:
        return False
    idle_fn = getattr(bot, "_is_contact_directory_auto_maintenance_idle", None)
    if callable(idle_fn):
        is_idle = idle_fn()
    else:
        is_idle = is_contact_directory_auto_maintenance_idle(bot)
    if not is_idle:
        return False
    cycle_state_fn = getattr(bot, "_contact_directory_auto_cycle_state", None)
    if callable(cycle_state_fn):
        cycle = cycle_state_fn(directory)
    else:
        cycle = contact_directory_auto_cycle_state(directory)
    active_cycle = cycle["status"] in {"running", "stalled"}
    full_scan_days_fn = getattr(bot, "_contact_directory_auto_maintenance_full_scan_interval_days_value", None)
    if callable(full_scan_days_fn):
        full_scan_days = full_scan_days_fn()
    else:
        full_scan_days = contact_directory_auto_maintenance_full_scan_interval_days_value(bot)
    if not active_cycle and not auto_maintenance_full_scan_is_due(
        directory,
        interval_days=full_scan_days,
        now=now_dt,
    ):
        return False

    cycle_start_name = cycle["next_start_name"]
    if cycle["status"] == "reset_required":
        cycle_start_name = ""
        active_cycle = False
    preserve_current_position = (
        active_cycle
        and cycle["batches_completed"] > 0
        and cycle["retry_count"] == 0
        and cycle["last_outcome"] == "advanced"
    )

    lock = bot._get_wechat_action_lock()
    if not lock.acquire(blocking=False):
        return False
    lock.release()
    write_cycle_fn = getattr(bot, "_write_contact_directory_auto_cycle_state", None)
    save_directory_fn = getattr(bot, "_save_contact_profiles_directory", None)
    try:
        if not active_cycle:
            if callable(write_cycle_fn):
                directory = write_cycle_fn(
                    directory,
                    now=now_dt,
                    auto_cycle_status="running",
                    auto_cycle_started_at=now_dt.strftime("%Y-%m-%d %H:%M:%S"),
                    auto_cycle_next_start_name="",
                    auto_cycle_last_progress_at="",
                    auto_cycle_last_outcome="",
                    auto_cycle_last_restart_at=now_dt.strftime("%Y-%m-%d %H:%M:%S"),
                    auto_cycle_batches_completed=0,
                    auto_cycle_retry_count=0,
                )
            else:
                directory = write_contact_directory_auto_cycle_state(
                    directory,
                    now=now_dt,
                    auto_cycle_status="running",
                    auto_cycle_started_at=now_dt.strftime("%Y-%m-%d %H:%M:%S"),
                    auto_cycle_next_start_name="",
                    auto_cycle_last_progress_at="",
                    auto_cycle_last_outcome="",
                    auto_cycle_last_restart_at=now_dt.strftime("%Y-%m-%d %H:%M:%S"),
                    auto_cycle_batches_completed=0,
                    auto_cycle_retry_count=0,
                )
            if callable(save_directory_fn):
                save_directory_fn(directory)
            else:
                save_contact_profiles_directory(bot, directory)

        batch_size_fn = getattr(bot, "_contact_directory_auto_maintenance_batch_size_value", None)
        if callable(batch_size_fn):
            batch_size = batch_size_fn()
        else:
            batch_size = contact_directory_auto_maintenance_batch_size_value(bot)
        refresh_batch_fn = getattr(bot, "refresh_contact_profiles_batch", None)
        if callable(refresh_batch_fn):
            result = refresh_batch_fn(
                mode="standard",
                start_name=cycle_start_name,
                use_saved_position=bool(cycle_start_name),
                count_override=batch_size,
                run_to_completion=False,
                automatic=True,
                preserve_current_position=preserve_current_position,
            )
        else:
            result = refresh_contact_profiles_batch(
                bot,
                mode="standard",
                start_name=cycle_start_name,
                use_saved_position=bool(cycle_start_name),
                count_override=batch_size,
                run_to_completion=False,
                automatic=True,
                preserve_current_position=preserve_current_position,
            )
        refreshed_directory = result.get("directory") or {}
        if callable(cycle_state_fn):
            refreshed_cycle = cycle_state_fn(refreshed_directory)
        else:
            refreshed_cycle = contact_directory_auto_cycle_state(refreshed_directory)
        analysis = result.get("analysis") or {}
        outcome = str(analysis.get("outcome") or "").strip()
        next_start_name = str(result.get("next_start_name") or "").strip()
        stamp = now_dt.strftime("%Y-%m-%d %H:%M:%S")

        if bool(result.get("completed")):
            if callable(write_cycle_fn):
                refreshed_directory = write_cycle_fn(
                    refreshed_directory,
                    now=now_dt,
                    auto_cycle_status="completed",
                    auto_cycle_next_start_name="",
                    auto_cycle_last_progress_at=stamp,
                    auto_cycle_last_outcome="completed",
                    auto_cycle_retry_count=0,
                    auto_cycle_batches_completed=refreshed_cycle["batches_completed"] + 1,
                    last_full_scan_completed_at=stamp,
                )
            else:
                refreshed_directory = write_contact_directory_auto_cycle_state(
                    refreshed_directory,
                    now=now_dt,
                    auto_cycle_status="completed",
                    auto_cycle_next_start_name="",
                    auto_cycle_last_progress_at=stamp,
                    auto_cycle_last_outcome="completed",
                    auto_cycle_retry_count=0,
                    auto_cycle_batches_completed=refreshed_cycle["batches_completed"] + 1,
                    last_full_scan_completed_at=stamp,
                )
        elif outcome == "advanced":
            if callable(write_cycle_fn):
                refreshed_directory = write_cycle_fn(
                    refreshed_directory,
                    now=now_dt,
                    auto_cycle_status="running",
                    auto_cycle_next_start_name=next_start_name,
                    auto_cycle_last_progress_at=stamp,
                    auto_cycle_last_outcome=outcome,
                    auto_cycle_retry_count=0,
                    auto_cycle_batches_completed=refreshed_cycle["batches_completed"] + 1,
                )
            else:
                refreshed_directory = write_contact_directory_auto_cycle_state(
                    refreshed_directory,
                    now=now_dt,
                    auto_cycle_status="running",
                    auto_cycle_next_start_name=next_start_name,
                    auto_cycle_last_progress_at=stamp,
                    auto_cycle_last_outcome=outcome,
                    auto_cycle_retry_count=0,
                    auto_cycle_batches_completed=refreshed_cycle["batches_completed"] + 1,
                )
        else:
            retry_count = refreshed_cycle["retry_count"] + 1
            if callable(write_cycle_fn):
                refreshed_directory = write_cycle_fn(
                    refreshed_directory,
                    now=now_dt,
                    auto_cycle_status="reset_required" if retry_count >= 3 else "stalled",
                    auto_cycle_next_start_name=cycle_start_name or next_start_name,
                    auto_cycle_last_outcome=outcome or "stalled",
                    auto_cycle_retry_count=retry_count,
                )
            else:
                refreshed_directory = write_contact_directory_auto_cycle_state(
                    refreshed_directory,
                    now=now_dt,
                    auto_cycle_status="reset_required" if retry_count >= 3 else "stalled",
                    auto_cycle_next_start_name=cycle_start_name or next_start_name,
                    auto_cycle_last_outcome=outcome or "stalled",
                    auto_cycle_retry_count=retry_count,
                )
        if callable(save_directory_fn):
            save_directory_fn(refreshed_directory)
        else:
            save_contact_profiles_directory(bot, refreshed_directory)
        return True
    finally:
        flush_lightweight = getattr(bot, "_flush_lightweight_send_queue", None)
        if callable(flush_lightweight):
            flush_lightweight()


def set_contact_profiles_paused(bot, paused=True):
    load_directory_fn = getattr(bot, "_load_contact_profiles_directory", None)
    if callable(load_directory_fn):
        directory, directory_file, _wx_id = load_directory_fn()
    else:
        directory, directory_file, _wx_id = load_contact_profiles_directory(bot)
    status = "paused" if paused else "idle"
    updated = maintenance_snapshot(
        directory,
        mode=(directory.get("maintenance") or {}).get("mode", "standard"),
        status=status,
        paused=bool(paused),
        start_name=(directory.get("maintenance") or {}).get("last_start_name", ""),
        count_returned=(directory.get("maintenance") or {}).get("collected_count", 0),
        matched_name=(directory.get("maintenance") or {}).get("matched_name", ""),
        next_start_name=(directory.get("maintenance") or {}).get("next_start_name", ""),
        last_error="" if paused else (directory.get("maintenance") or {}).get("last_error", ""),
        callback_names=[],
    )
    save_contact_directory(directory_file, updated)
    return updated


def contact_repair_before_display(contact):
    contact = contact or {}
    nickname = str(contact.get("nickname") or contact.get("display_name") or "").strip()
    wechat_id = str(contact.get("wechat_id") or "").strip()
    parts = [part for part in (nickname, wechat_id) if part]
    return " | ".join(parts) or "未命名联系人"


def repair_contact_profile_remarks(bot, contact_keys=None):
    if not getattr(bot, "wx", None):
        raise RuntimeError("微信客户端未初始化，请先启动机器人并保持微信主窗口可用。")

    load_directory_fn = getattr(bot, "_load_contact_profiles_directory", None)
    if callable(load_directory_fn):
        directory, directory_file, wx_id = load_directory_fn()
    else:
        directory, directory_file, wx_id = load_contact_profiles_directory(bot)
    if not wx_id:
        raise RuntimeError("当前微信号未初始化，请先启动机器人后再试。")

    candidates = contact_repair_candidates(directory)
    if contact_keys:
        wanted = {str(item or "").strip() for item in contact_keys if str(item or "").strip()}
        if wanted:
            candidates = [item for item in candidates if str(item.get("contact_key") or "") in wanted]

    result = {
        "wx_id": wx_id,
        "candidate_count": len(candidates),
        "success_count": 0,
        "failed_count": 0,
        "skipped_count": 0,
        "records": [],
    }
    if not candidates:
        return result

    current_directory = directory
    records_file_fn = getattr(bot, "_contact_profiles_remark_repair_records_file", None)
    if callable(records_file_fn):
        records_file = records_file_fn()
    else:
        records_file = contact_profiles_remark_repair_records_file(bot)
    _bot_log(bot, message=f"[通讯录维护] 开始备注修复，共 {len(candidates)} 个联系人")
    with bot._get_wechat_action_lock():
        for index, candidate in enumerate(candidates, start=1):
            contact_key = str(candidate.get("contact_key") or "")
            suggested_remark = str(candidate.get("suggested_remark") or "").strip()
            contact = next(
                (
                    item for item in (current_directory.get("subjects") or [])
                    if isinstance(item, dict) and str(item.get("contact_key") or "") == contact_key
                ),
                None,
            )
            if not contact or not suggested_remark:
                result["skipped_count"] += 1
                continue
            repair_display_fn = getattr(bot, "_contact_repair_before_display", None)
            if callable(repair_display_fn):
                target_name = repair_display_fn(contact)
            else:
                target_name = contact_repair_before_display(contact)
            error = ""
            success = False
            try:
                if target_name and hasattr(bot.wx, "ChatWith"):
                    bot.wx.ChatWith(target_name, exact=True)
                bot.wx.EditFriendInfo(remark=suggested_remark)
                success = True
            except Exception as exc:
                error = str(exc)

            record = {
                "at": datetime.now().replace(microsecond=0).isoformat(),
                "wx_id": wx_id,
                "contact_key": contact_key,
                "target_name": target_name,
                "old_remark": str(contact.get("remark") or ""),
                "new_remark": suggested_remark,
                "success": success,
                "error": error,
            }
            append_bounded_record(records_file, record, limit=1000)
            result["records"].append(record)
            if success:
                _bot_log(bot, message=f"[通讯录维护] 备注修复 {index}/{len(candidates)}：{target_name} -> {suggested_remark}")
            else:
                _bot_log(bot, message=f"[通讯录维护] 备注修复失败 {index}/{len(candidates)}：{target_name} -> {suggested_remark}，错误：{error}")

            if success:
                current_directory = apply_repaired_remark(
                    current_directory,
                    contact_key,
                    suggested_remark,
                    now=datetime.now(),
                )
                save_contact_directory(directory_file, current_directory)
                result["success_count"] += 1
            else:
                result["failed_count"] += 1
    result["directory"] = current_directory
    _bot_log(
        bot,
        message=(
            f"[通讯录维护] 备注修复完成：成功 {result['success_count']}，"
            f"失败 {result['failed_count']}，跳过 {result['skipped_count']}"
        )
    )
    return result


def preview_contact_profile_remark_repairs(bot, contact_keys=None):
    load_directory_fn = getattr(bot, "_load_contact_profiles_directory", None)
    if callable(load_directory_fn):
        directory, _directory_file, wx_id = load_directory_fn()
    else:
        directory, _directory_file, wx_id = load_contact_profiles_directory(bot)
    if not wx_id:
        raise RuntimeError("当前微信号未初始化，请先启动机器人后再试。")

    candidates = contact_repair_candidates(directory)
    if contact_keys:
        wanted = {str(item or "").strip() for item in contact_keys if str(item or "").strip()}
        if wanted:
            candidates = [item for item in candidates if str(item.get("contact_key") or "") in wanted]

    subjects = {
        str(item.get("contact_key") or ""): item
        for item in (directory.get("subjects") or [])
        if isinstance(item, dict)
    }
    preview_items = []
    for candidate in candidates:
        contact_key = str(candidate.get("contact_key") or "").strip()
        contact = subjects.get(contact_key, {})
        repair_display_fn = getattr(bot, "_contact_repair_before_display", None)
        if callable(repair_display_fn):
            before_display = repair_display_fn(contact)
        else:
            before_display = contact_repair_before_display(contact)
        preview_items.append({
            "contact_key": contact_key,
            "before_display": before_display,
            "after_display": str(candidate.get("suggested_remark") or "").strip(),
            "reasons": list(candidate.get("reasons") or []),
        })

    return {
        "wx_id": wx_id,
        "candidate_count": len(preview_items),
        "candidates": preview_items,
    }
