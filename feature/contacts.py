"""Contact directory rules and runtime helpers."""

from __future__ import annotations

import copy
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
from datetime import datetime, timedelta
from typing import Any

from core.contact_profiles import (
    apply_repaired_remark,
    contact_display_name,
    directory_path as contact_directory_path,
    contact_send_name,
    directory_lock as contact_directory_lock,
    load_directory as load_contact_directory,
    merge_directory as merge_contact_directory,
    normalize_tag_list,
    repair_candidates as contact_repair_candidates,
    save_directory as save_contact_directory,
)
from core.wechat_window import run_with_wechat_rebind_retry
from core.wechat_observability import warn_slow_wechat_ui_action
from core import wechat_ui_actions
from core.logger import log
from feature import listening
from feature import takeover_runtime
from feature.material_outreach import append_bounded_record


MODE_TEST = "test"
MODE_STANDARD = "standard"

_MODE_SETTINGS = {
    MODE_TEST: {"count": 5, "interval": 1.5, "label": "快速测试"},
    MODE_STANDARD: {"count": 50, "interval": 0.5, "label": "立即建档"},
}

AUTO_BATCH_SIZE_DEFAULT = 50
AUTO_INTERVAL_DEFAULT_MINUTES = 20
AUTO_INTERVAL_MIN_MINUTES = 5
AUTO_INTERVAL_MAX_MINUTES = 1440
AUTO_FULL_SCAN_DEFAULT_DAYS = 7
AUTO_WINDOW_START_DEFAULT = "00:00"
AUTO_WINDOW_END_DEFAULT = "23:59"
AUTO_TAIL_PROBE_COUNT = 2
STOP_MAINTENANCE_HINT = "已请求停止建档，当前批次会继续跑完，并在本批返回后停止。"
CONTACT_PROFILES_READING_ATTR = "_contact_profiles_reading_active"
CONTACT_CURSOR_MATCH_SETTLE_SECONDS = 1.0
CONTACT_READ_PROGRESS_LOG_INTERVAL = 20
AUTO_MAINTENANCE_READ_TIMEOUT_SECONDS = 600
AUTO_MAINTENANCE_COLLECT_HARD_TIMEOUT_SECONDS = 300
AUTO_MAINTENANCE_ACTIVITY_GRACE_SECONDS = 10


def _clean_text(value: Any) -> str:
    return str(value or "").strip()


def _bot_log(bot, *args, **kwargs) -> None:
    module = sys.modules.get(getattr(bot.__class__, "__module__", ""))
    log_fn = getattr(module, "log", log) if module else log
    log_fn(*args, **kwargs)


def _should_log_contact_read_progress(count: int) -> bool:
    return count > 0 and count % CONTACT_READ_PROGRESS_LOG_INTERVAL == 0


def _runtime_base_dir() -> str:
    return os.path.dirname(sys.executable) if hasattr(sys, "_MEIPASS") else os.path.abspath(".")


def _contact_auto_collector_script_path() -> str:
    base = _runtime_base_dir()
    candidates = [
        os.path.join(base, "feature", "contact_auto_collector_worker.py"),
        os.path.join(os.path.abspath("."), "feature", "contact_auto_collector_worker.py"),
    ]
    for path in candidates:
        if os.path.exists(path):
            return path
    return candidates[0]


def _contact_auto_collector_python_executable() -> str:
    if not hasattr(sys, "_MEIPASS"):
        return sys.executable
    base = _runtime_base_dir()
    candidates = [
        os.path.join(base, "runtime", "python", "python.exe"),
        os.path.join(base, "venv", "Scripts", "python.exe"),
        sys.executable,
    ]
    for path in candidates:
        if path and os.path.exists(path):
            return path
    return sys.executable


def contact_auto_maintenance_collect_hard_timeout_seconds(count: Any) -> int:
    return AUTO_MAINTENANCE_COLLECT_HARD_TIMEOUT_SECONDS


def _contact_auto_collector_lock_path() -> str:
    runtime_dir = os.path.join(_runtime_base_dir(), "runtime")
    os.makedirs(runtime_dir, exist_ok=True)
    return os.path.join(runtime_dir, "contact_auto_collector.lock")


def _contact_auto_collector_registry_path() -> str:
    runtime_dir = os.path.join(_runtime_base_dir(), "runtime")
    os.makedirs(runtime_dir, exist_ok=True)
    return os.path.join(runtime_dir, "contact_auto_collector.json")


def _write_contact_auto_collector_registry(*, pid: int, script_path: str, request_path: str) -> str:
    path = _contact_auto_collector_registry_path()
    temp_path = path + ".tmp"
    payload = {
        "pid": int(pid or 0),
        "script_path": os.path.abspath(script_path),
        "request_path": os.path.abspath(request_path),
        "created_at": time.time(),
    }
    with open(temp_path, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False)
    os.replace(temp_path, path)
    return path


def _clear_contact_auto_collector_registry(path: str, *, pid: int) -> None:
    try:
        with open(path, "r", encoding="utf-8") as handle:
            payload = json.load(handle)
        if int(payload.get("pid") or 0) != int(pid or 0):
            return
    except FileNotFoundError:
        return
    except Exception:
        pass
    try:
        os.remove(path)
    except FileNotFoundError:
        pass


def _windows_process_command_line(pid: int) -> str | None:
    if os.name != "nt" or not pid:
        return None
    command = (
        "$p=Get-CimInstance Win32_Process -Filter \"ProcessId = " + str(int(pid)) + "\";"
        "if($null -eq $p){exit 3};"
        "[Console]::OutputEncoding=[Text.UTF8Encoding]::new();"
        "$p.CommandLine"
    )
    completed = subprocess.run(
        ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", command],
        timeout=10,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    if completed.returncode == 3:
        return ""
    if completed.returncode != 0:
        return None
    return str(completed.stdout or "").strip()


def cleanup_orphaned_contact_auto_collector() -> dict[str, Any]:
    path = _contact_auto_collector_registry_path()
    try:
        with open(path, "r", encoding="utf-8") as handle:
            payload = json.load(handle)
    except FileNotFoundError:
        return {"found": False, "terminated": False}
    except Exception as exc:
        return {"found": True, "terminated": False, "error": str(exc)}

    pid = int(payload.get("pid") or 0)
    command_line = _windows_process_command_line(pid)
    script_path = os.path.abspath(str(payload.get("script_path") or ""))
    request_path = os.path.abspath(str(payload.get("request_path") or ""))
    verified = bool(
        command_line
        and script_path.lower() in command_line.lower()
        and request_path.lower() in command_line.lower()
    )
    terminated = False
    if verified:
        completed = subprocess.run(
            ["taskkill", "/PID", str(pid), "/T", "/F"],
            timeout=10,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        terminated = completed.returncode == 0
    if command_line is not None:
        _clear_contact_auto_collector_registry(path, pid=pid)
    return {"found": True, "pid": pid, "verified": verified, "terminated": terminated}


def _acquire_contact_auto_collector_process_lock():
    path = _contact_auto_collector_lock_path()
    handle = open(path, "a+b")
    try:
        handle.seek(0)
        if not handle.read(1):
            handle.write(b"0")
            handle.flush()
        handle.seek(0)
        if os.name == "nt":
            import msvcrt

            try:
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            except OSError:
                handle.close()
                return None

            def release():
                try:
                    handle.seek(0)
                    msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
                finally:
                    handle.close()

            return release

        import fcntl

        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            handle.close()
            return None

        def release():
            try:
                fcntl.flock(handle, fcntl.LOCK_UN)
            finally:
                handle.close()

        return release
    except Exception:
        try:
            handle.close()
        except Exception:
            pass
        raise


def _terminate_contact_auto_collector_process(proc: Any) -> dict[str, Any]:
    pid = getattr(proc, "pid", 0)
    cleanup: dict[str, Any] = {
        "pid": pid,
        "kill_sent": False,
        "taskkill_attempted": False,
        "taskkill_returncode": None,
        "taskkill_output": "",
        "still_running": False,
    }

    try:
        proc.kill()
        cleanup["kill_sent"] = True
    except Exception as exc:
        cleanup["kill_error"] = str(exc)

    try:
        proc.communicate(timeout=5)
    except subprocess.TimeoutExpired:
        pass
    except Exception as exc:
        cleanup["wait_error"] = str(exc)

    try:
        still_running = proc.poll() is None
    except Exception as exc:
        cleanup["poll_error"] = str(exc)
        still_running = True

    if still_running and os.name == "nt" and pid:
        cleanup["taskkill_attempted"] = True
        taskkill_kwargs = {}
        if hasattr(subprocess, "CREATE_NO_WINDOW"):
            taskkill_kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW
        try:
            completed = subprocess.run(
                ["taskkill", "/PID", str(pid), "/T", "/F"],
                timeout=10,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                **taskkill_kwargs,
            )
            cleanup["taskkill_returncode"] = completed.returncode
            cleanup["taskkill_output"] = _clean_text(completed.stdout) or _clean_text(completed.stderr)
        except Exception as exc:
            cleanup["taskkill_error"] = str(exc)

        try:
            proc.communicate(timeout=5)
        except subprocess.TimeoutExpired:
            pass
        except Exception as exc:
            cleanup["post_taskkill_wait_error"] = str(exc)

    try:
        cleanup["still_running"] = proc.poll() is None
    except Exception as exc:
        cleanup["poll_error"] = str(exc)
        cleanup["still_running"] = True
    return cleanup


def _contact_auto_collector_timeout_message(timeout_seconds: int, cleanup: dict[str, Any]) -> str:
    status = "已发起强制终止但进程仍未退出" if cleanup.get("still_running") else "已终止本批次"
    details = []
    if cleanup.get("pid"):
        details.append(f"PID {cleanup['pid']}")
    if cleanup.get("kill_error"):
        details.append(f"kill 失败：{cleanup['kill_error']}")
    elif cleanup.get("kill_sent"):
        details.append("kill 已发送")
    if cleanup.get("taskkill_attempted"):
        if cleanup.get("taskkill_returncode") == 0:
            details.append("taskkill 已执行")
        elif cleanup.get("taskkill_error"):
            details.append(f"taskkill 异常：{cleanup['taskkill_error']}")
        else:
            details.append(f"taskkill 返回 {cleanup.get('taskkill_returncode')}")
    if cleanup.get("still_running"):
        details.append("进程仍未退出")
    detail_text = f"（{'; '.join(details)}）" if details else ""
    return f"通讯录采集超过 {timeout_seconds}s，{status}{detail_text}"


def friend_info_edit_noop(response: Any) -> bool:
    if not isinstance(response, dict):
        return False
    return "未进行任何修改" in _clean_text(response.get("message"))


def _chat_info_tags(raw_info: Any) -> list[str] | None:
    if not isinstance(raw_info, dict):
        return None
    for key in ("标签", "tags", "Tags", "tag", "Tag", "raw_tags"):
        if key in raw_info:
            return normalize_tag_list(raw_info.get(key))
    return None


def _tags_update_is_noop(current_tags: list[str] | None, *, add_tags: list[str] | None, remove_tags: list[str] | None) -> bool:
    if current_tags is None:
        return False
    current = set(normalize_tag_list(current_tags))
    add_set = {tag for tag in normalize_tag_list(add_tags or []) if tag}
    remove_set = {tag for tag in normalize_tag_list(remove_tags or []) if tag}
    return add_set.issubset(current) and not (remove_set & current)


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


def contact_read_timeout_seconds(count: Any) -> int:
    if count is None:
        return 0xFFFFF
    try:
        count = max(1, int(count))
    except (TypeError, ValueError):
        count = 50
    return max(120, count * 3)


def contact_auto_maintenance_read_timeout_seconds(count: Any) -> int:
    return AUTO_MAINTENANCE_READ_TIMEOUT_SECONDS


def normalize_auto_maintenance_batch_size(value: Any) -> int:
    return AUTO_BATCH_SIZE_DEFAULT


def coerce_auto_maintenance_interval_minutes(value: Any) -> int:
    try:
        value = int(value)
    except (TypeError, ValueError):
        value = AUTO_INTERVAL_DEFAULT_MINUTES
    return max(AUTO_INTERVAL_MIN_MINUTES, min(AUTO_INTERVAL_MAX_MINUTES, value))


def coerce_auto_maintenance_full_scan_interval_days(value: Any) -> int:
    return AUTO_FULL_SCAN_DEFAULT_DAYS


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


def contact_edit_target_name(contact: dict[str, Any]) -> str:
    contact = contact or {}
    return contact_send_name(contact) or contact_display_name(contact)


def contact_expected_chat_names(contact: dict[str, Any], target_name: str = "") -> set[str]:
    contact = contact or {}
    names = {_clean_text(target_name)}
    names.add(contact_display_name(contact))
    names.add(contact_send_name(contact))
    for key in ("remark", "nickname", "wechat_id", "wxid"):
        names.add(_clean_text(contact.get(key)))
    return {name for name in names if name}


def friend_info_edit_success(response: Any) -> bool:
    return isinstance(response, dict) and response.get("status") == "成功"


def _friend_edit_cleanup_names(target_name: str, expected_names: set[str] | None, remark: str | None) -> list[str]:
    names = [target_name]
    names.extend(expected_names or [])
    if remark:
        names.append(remark)
    cleaned = []
    seen = set()
    for name in names:
        name = _clean_text(name)
        if not name or name in seen:
            continue
        seen.add(name)
        cleaned.append(name)
    return cleaned


def close_dynamic_listener_after_friend_edit(
    bot,
    target_name: str,
    *,
    expected_names: set[str] | None = None,
    remark: str | None = None,
    log_prefix: str = "[通讯录维护]",
) -> list[str]:
    close_fn = getattr(bot, "_close_dynamic_listener_subwindows", None)
    names = _friend_edit_cleanup_names(target_name, expected_names, remark)
    closed_names = close_fn(names) if callable(close_fn) else listening.close_dynamic_listener_subwindows(bot, names)
    return closed_names


def edit_friend_info_via_chat_profile(
    bot,
    target_name: str,
    *,
    expected_names: set[str] | None = None,
    contact_key: str = "",
    remark: str | None = None,
    add_tags: list[str] | None = None,
    remove_tags: list[str] | None = None,
    close_dynamic_listener: bool = True,
    log_prefix: str = "[通讯录维护]",
) -> Any:
    target_name = _clean_text(target_name)
    if not target_name:
        raise RuntimeError("缺少可搜索的好友名称")
    if not any([remark is not None, add_tags, remove_tags]):
        raise RuntimeError("缺少要修改的好友信息")
    if not getattr(bot, "wx", None):
        raise RuntimeError("微信客户端未初始化，请先启动机器人并保持微信主窗口可用。")

    owner = getattr(bot, "_ui_owner", None)
    if owner is not None:
        response = owner.call(
            wechat_ui_actions.UIIntent(
                wechat_ui_actions.UIIntentKind.CONTACT_EDIT,
                {
                    "target": target_name,
                    "contact_key": _clean_text(contact_key),
                    "expected_names": sorted(expected_names or set()),
                    "remark": remark,
                    "add_tags": list(add_tags or []),
                    "remove_tags": list(remove_tags or []),
                },
            ),
            wechat_ui_actions.UI_CALL_WAIT_TIMEOUT,
        )
        if close_dynamic_listener:
            close_dynamic_listener_after_friend_edit(
                bot,
                target_name,
                expected_names=expected_names,
                remark=remark,
                log_prefix=log_prefix,
            )
        return response

    raise RuntimeError("好友资料 UI 只能由微信 UI owner 执行")


def modify_friend_tags_via_chat_profile(
    bot,
    targets: list[dict[str, str]],
    *,
    add_tags: list[str] | None = None,
    remove_tags: list[str] | None = None,
    log_prefix: str = "[通讯录维护]",
    rebind_attempts: int = 2,
) -> dict[str, Any]:
    add_tags = [str(item or "").strip() for item in (add_tags or []) if str(item or "").strip()]
    remove_tags = [str(item or "").strip() for item in (remove_tags or []) if str(item or "").strip()]
    records = []
    result = {
        "add_tags": add_tags,
        "remove_tags": remove_tags,
        "target_count": len(targets or []),
        "success_count": 0,
        "failed_count": 0,
        "records": records,
        "status": "skipped",
        "message": "",
    }
    if not targets:
        return result
    if not add_tags and not remove_tags:
        result["failed_count"] = len(targets or [])
        result["status"] = "failed"
        result["message"] = "缺少要修改的标签"
        return result

    tag_label = "、".join(add_tags or remove_tags)
    action_label = "添加" if add_tags else "移除"
    for target in targets:
        target_name = _clean_text((target or {}).get("name"))
        contact_key = _clean_text((target or {}).get("contact_key"))
        record = {
            "name": target_name,
            "add_tags": add_tags,
            "remove_tags": remove_tags,
            "success": False,
            "error": "",
        }
        if not target_name:
            record["error"] = "缺少可搜索的好友名称"
            result["failed_count"] += 1
            records.append(record)
            continue

        def apply_single_tag_update():
            return edit_friend_info_via_chat_profile(
                bot,
                target_name,
                expected_names={target_name},
                contact_key=contact_key,
                add_tags=add_tags,
                remove_tags=remove_tags,
                log_prefix=log_prefix,
            )

        try:
            response = run_with_wechat_rebind_retry(
                bot,
                apply_single_tag_update,
                attempts=max(1, int(rebind_attempts or 1)),
                on_retry=lambda exc, _attempt: _bot_log(
                    bot,
                    level="WARNING",
                    message=f"{log_prefix} 给 {target_name} {action_label}标签【{tag_label}】失败，重新初始化微信客户端后重试：{exc}",
                ),
            )
            record["success"] = True
            record["response"] = response
            result["success_count"] += 1
        except Exception as exc:
            record["error"] = str(exc)
            result["failed_count"] += 1
        records.append(record)

    if result["failed_count"] and result["success_count"]:
        result["status"] = "partial"
        result["message"] = "部分好友标签修改成功"
    elif result["failed_count"]:
        result["status"] = "failed"
        first_error = next((_clean_text(item.get("error")) for item in records if item.get("error")), "")
        result["message"] = f"好友标签修改失败：{first_error}" if first_error else "好友标签修改失败"
    else:
        result["status"] = "success"
        result["message"] = "好友标签修改成功"
    return result


def contact_name_matches(name: Any, start_name: Any) -> bool:
    start_name = _clean_text(start_name)
    if not start_name:
        return True
    return _clean_text(name) == start_name


def _detail_name(raw_detail: Any) -> str:
    if not isinstance(raw_detail, dict):
        return ""
    for key in ("备注", "remark", "昵称", "nickname", "name", "微信号", "wechat_id", "wxid"):
        value = _clean_text(raw_detail.get(key))
        if value:
            return value
    return ""


def start_names_from_details(raw_details: list[Any] | tuple[Any, ...] | None, *, limit: int = 2) -> list[str]:
    names: list[str] = []
    seen: set[str] = set()
    for raw_detail in reversed(list(raw_details or [])):
        name = _clean_text(_detail_name(raw_detail))
        if not name or name in seen:
            continue
        names.append(name)
        seen.add(name)
        if len(names) >= max(1, int(limit or 1)):
            break
    return names


def start_cursors_from_details(
    raw_details: list[Any] | tuple[Any, ...] | None,
    result_identities: list[str] | tuple[str, ...] | None = None,
    *,
    limit: int = 2,
) -> list[tuple[str, str]]:
    details = list(raw_details or [])
    identities = list(result_identities or [])
    cursors: list[tuple[str, str]] = []
    for index in range(len(details) - 1, -1, -1):
        name = _clean_text(_detail_name(details[index]))
        if not name:
            continue
        identity = _clean_text(identities[index]) if index < len(identities) else ""
        cursor = (name, identity)
        if cursor in cursors:
            continue
        cursors.append(cursor)
        if len(cursors) >= max(1, int(limit or 1)):
            break
    return cursors


def _detail_identity(raw_detail: Any, *, name: str) -> str:
    if isinstance(raw_detail, dict):
        for key in ("微信号", "wechat_id", "wxid"):
            value = _clean_text(raw_detail.get(key))
            if value:
                return f"id:{value}"
    return f"name:{name}"


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
    current_start_identity: Any = "",
    previous_next_start_name: Any = "",
    previous_next_start_identity: Any = "",
    result_identities: list[str] | tuple[str, ...] | None = None,
    allow_short_batch_complete: bool = True,
) -> dict[str, Any]:
    details = list(raw_details or [])
    names = []
    result_identity_values = list(result_identities or [])
    identities = []
    for index, item in enumerate(details):
        name = _detail_name(item)
        if name:
            names.append(name)
            identity = _clean_text(result_identity_values[index]) if index < len(result_identity_values) else ""
            identities.append(identity or _detail_identity(item, name=name))
    next_start_name = names[-1] if names else ""
    next_start_identity = identities[-1] if identities else ""
    repeat_count = max(0, len(identities) - len(set(identities)))
    previous_next = _clean_text(previous_next_start_name)
    current_start = _clean_text(current_start_name)
    previous_cursor = (previous_next, _clean_text(previous_next_start_identity))
    current_cursor = (current_start, _clean_text(current_start_identity))
    next_cursor = (_clean_text(next_start_name), _clean_text(next_start_identity))
    advanced = bool(next_start_name) and next_cursor not in {previous_cursor, current_cursor}
    short_batch = len(details) < max(1, int(requested_count or 1))
    if not details:
        return {
            "outcome": "empty_batch",
            "completed": False,
            "advanced": False,
            "next_start_name": "",
            "next_start_identity": "",
            "repeat_count": 0,
        }
    if repeat_count > 0:
        return {
            "outcome": "suspicious_repeat",
            "completed": False,
            "advanced": False,
            "next_start_name": next_start_name,
            "next_start_identity": next_start_identity,
            "repeat_count": repeat_count,
        }
    if short_batch and advanced:
        if not allow_short_batch_complete:
            return {
                "outcome": "short_advanced",
                "completed": False,
                "advanced": True,
                "next_start_name": next_start_name,
                "next_start_identity": next_start_identity,
                "repeat_count": repeat_count,
            }
        return {
            "outcome": "tail_complete",
            "completed": True,
            "advanced": True,
            "next_start_name": next_start_name,
            "next_start_identity": next_start_identity,
            "repeat_count": repeat_count,
        }
    if not advanced:
        return {
            "outcome": "not_advanced",
            "completed": False,
            "advanced": False,
            "next_start_name": next_start_name,
            "next_start_identity": next_start_identity,
            "repeat_count": repeat_count,
        }
    return {
        "outcome": "advanced",
        "completed": False,
        "advanced": True,
        "next_start_name": next_start_name,
        "next_start_identity": next_start_identity,
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
    if bool(maintenance.get("paused", False)):
        return False
    interval = coerce_auto_maintenance_interval_minutes(interval_minutes)
    current = now if isinstance(now, datetime) else _parse_maintenance_time(now) or datetime.now()
    last_attempt = _parse_maintenance_time(
        maintenance.get("last_batch_finished_at") or maintenance.get("last_attempted_at")
    )
    if maintenance.get("status") == "running":
        last_activity = last_attempt or _parse_maintenance_time(
            maintenance.get("updated_at") or maintenance.get("started_at")
        )
        if last_activity is not None and current < last_activity + timedelta(minutes=max(interval, 10)):
            return False
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
    start_identity: Any = "",
    count_returned: int | None = None,
    matched_name: Any = "",
    next_start_name: Any = "",
    next_start_identity: Any = "",
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
    maintenance["last_start_identity"] = _clean_text(start_identity)
    maintenance["matched_name"] = _clean_text(matched_name)
    maintenance["next_start_name"] = _clean_text(next_start_name)
    maintenance["next_start_identity"] = _clean_text(next_start_identity)
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

    return run_with_wechat_rebind_retry(
        bot,
        switch_to_contact,
        attempts=2,
        on_retry=lambda exc, _attempt: _bot_log(
            bot,
            level="WARNING",
            message=f"[通讯录维护] 切换通讯录失败，准备重新初始化微信客户端后重试：{exc}",
        ),
    )


def switch_contact_directory_back_to_chat(bot, *, use_lock: bool = False) -> bool:
    if not getattr(bot, "wx", None):
        return False
    switch_to_chat = getattr(bot.wx, "SwitchToChat", None)
    if not callable(switch_to_chat):
        return False

    def do_switch():
        try:
            switch_to_chat()
            return True
        except Exception as exc:
            _bot_log(bot, level="WARNING", message=f"[通讯录维护] 切回聊天页失败：{exc}")
            return False

    if use_lock:
        lock_fn = getattr(bot, "_get_wechat_action_lock", None)
        if callable(lock_fn):
            with lock_fn():
                return do_switch()
            return False
    return do_switch()


def restore_contact_directory_back_to_chat(bot) -> bool:
    if switch_contact_directory_back_to_chat(bot):
        return True

    def strict_switch():
        if not getattr(bot, "wx", None):
            raise RuntimeError("微信客户端不可用，无法切回聊天页")
        switch_to_chat = getattr(bot.wx, "SwitchToChat", None)
        if not callable(switch_to_chat):
            raise RuntimeError("当前微信客户端不支持切回聊天页")
        switch_to_chat()
        return True

    try:
        return bool(run_with_wechat_rebind_retry(
            bot,
            strict_switch,
            attempts=2,
            on_retry=lambda exc, _attempt: _bot_log(
                bot,
                level="WARNING",
                message=f"[通讯录维护] 切回聊天页失败，准备重新初始化微信客户端后重试：{exc}",
            ),
        ))
    except Exception as exc:
        _bot_log(bot, level="ERROR", message=f"[通讯录维护] 切回聊天页最终失败：{exc}")
        return False


def run_contact_auto_maintenance_collector(
    *,
    start_name: str,
    start_identity: str = "",
    count: int,
    timeout_seconds: int | None = None,
) -> dict[str, Any]:
    handle = start_contact_auto_maintenance_collector(
        start_name=start_name,
        start_identity=start_identity,
        count=count,
        timeout_seconds=timeout_seconds,
    )
    while True:
        done, result = handle.poll()
        if done:
            if isinstance(result, BaseException):
                raise result
            return result
        time.sleep(0.1)


class ContactAutoCollectorProcess:
    def __init__(self, proc, cmd, output_path, temp_dir, release_lock, timeout_seconds, registry_path):
        self.proc = proc
        self.cmd = cmd
        self.output_path = output_path
        self.temp_dir = temp_dir
        self.release_lock = release_lock
        self.deadline = time.monotonic() + timeout_seconds
        self.timeout_seconds = timeout_seconds
        self.registry_path = registry_path
        self.closed = False

    def _close(self):
        if self.closed:
            return
        self.closed = True
        try:
            self.release_lock()
        finally:
            try:
                _clear_contact_auto_collector_registry(self.registry_path, pid=self.proc.pid)
            finally:
                self.temp_dir.cleanup()

    def terminate(self):
        if self.closed:
            return
        _terminate_contact_auto_collector_process(self.proc)
        self._close()

    def poll(self):
        if self.closed:
            return True, RuntimeError("通讯录采集器已结束")
        if self.proc.poll() is None:
            if time.monotonic() < self.deadline:
                return False, None
            cleanup = _terminate_contact_auto_collector_process(self.proc)
            error = RuntimeError(_contact_auto_collector_timeout_message(self.timeout_seconds, cleanup))
            self._close()
            return True, error
        try:
            stdout, stderr = self.proc.communicate()
            payload = {}
            if os.path.exists(self.output_path):
                with open(self.output_path, "r", encoding="utf-8") as f:
                    payload = json.load(f)
            if self.proc.returncode != 0 or not payload.get("ok"):
                detail = _clean_text(payload.get("error")) or _clean_text(stderr) or _clean_text(stdout)
                raise RuntimeError(f"通讯录采集失败：{detail or '子进程未返回有效结果'}")
            return True, payload
        except BaseException as exc:
            return True, exc
        finally:
            self._close()


def start_contact_auto_maintenance_collector(
    *,
    start_name: str,
    start_identity: str = "",
    count: int,
    timeout_seconds: int | None = None,
):
    release_process_lock = _acquire_contact_auto_collector_process_lock()
    if not release_process_lock:
        raise RuntimeError("通讯录采集器正在运行，已跳过本次自动维护")
    timeout_seconds = max(1, int(timeout_seconds or AUTO_MAINTENANCE_COLLECT_HARD_TIMEOUT_SECONDS))
    temp_dir = tempfile.TemporaryDirectory(prefix="wxbot_contact_collect_")
    proc = None
    try:
            temp_dir_path = temp_dir.name
            request_path = os.path.join(temp_dir_path, "request.json")
            output_path = os.path.join(temp_dir_path, "output.json")
            request_payload = {
                "start_name": _clean_text(start_name),
                "start_identity": _clean_text(start_identity),
                "count": max(1, int(count or 1)),
            }
            with open(request_path, "w", encoding="utf-8") as f:
                json.dump(request_payload, f, ensure_ascii=False)

            cmd = [
                _contact_auto_collector_python_executable(),
                "-X",
                "utf8",
                _contact_auto_collector_script_path(),
                "--request",
                request_path,
                "--output",
                output_path,
            ]
            env = dict(os.environ)
            env["PYTHONUTF8"] = "1"
            run_kwargs = {}
            if os.name == "nt" and hasattr(subprocess, "CREATE_NO_WINDOW"):
                run_kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW
            proc = subprocess.Popen(
                cmd,
                cwd=_runtime_base_dir(),
                env=env,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                errors="replace",
                **run_kwargs,
            )
            registry_path = _write_contact_auto_collector_registry(
                pid=proc.pid,
                script_path=cmd[3],
                request_path=request_path,
            )
            return ContactAutoCollectorProcess(
                proc,
                cmd,
                output_path,
                temp_dir,
                release_process_lock,
                timeout_seconds,
                registry_path,
            )
    except Exception:
        if proc is not None:
            _terminate_contact_auto_collector_process(proc)
        temp_dir.cleanup()
        release_process_lock()
        raise


def refresh_run_kind(mode: str, *, automatic: bool = False) -> str:
    if automatic:
        return "auto_maintenance"
    return "manual_standard"


def contact_directory_run_label(mode: str, *, run_kind: str = "") -> str:
    run_kind = str(run_kind or "").strip().lower()
    if run_kind == "auto_maintenance":
        return "自动维护"
    normalized_mode = refresh_batch_settings(mode).get("mode", "standard")
    return {
        "test": "快速测试",
        "standard": "立即建档",
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
    return AUTO_BATCH_SIZE_DEFAULT


def contact_directory_auto_maintenance_interval_minutes_value(bot):
    if hasattr(bot, "contact_directory_auto_maintenance_interval_minutes"):
        value = getattr(bot, "contact_directory_auto_maintenance_interval_minutes")
    else:
        value = getattr(bot.config, "contact_directory_auto_maintenance_interval_minutes", AUTO_INTERVAL_DEFAULT_MINUTES)
    return coerce_auto_maintenance_interval_minutes(value)


def contact_directory_auto_maintenance_full_scan_interval_days_value(bot):
    return AUTO_FULL_SCAN_DEFAULT_DAYS


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


def has_pending_lightweight_send_queue(bot) -> bool:
    ensure_queue = getattr(bot, "_ensure_lightweight_send_queue_state", None)
    if callable(ensure_queue):
        ensure_queue()
    queue = getattr(bot, "_lightweight_send_queue", None)
    queue_lock = getattr(bot, "_lightweight_send_queue_lock", None)
    if queue_lock is None:
        return bool(queue)
    try:
        with queue_lock:
            return bool(getattr(bot, "_lightweight_send_queue", None))
    except Exception:
        return bool(queue)


def has_pending_private_outbound_echoes(bot) -> bool:
    pending_echoes = getattr(bot, "_has_pending_private_outbound_echoes", None)
    if not callable(pending_echoes):
        return False
    try:
        return bool(pending_echoes())
    except Exception:
        return False


def is_contact_directory_auto_maintenance_idle(bot):
    mode, _target = takeover_runtime.get_workspace_mode(bot)
    return mode == takeover_runtime.IDLE_MODE


def has_active_contact_maintenance_conflict(bot, *, now_ts: float | None = None) -> bool:
    now_ts = time.time() if now_ts is None else float(now_ts or 0)
    try:
        last_incoming_at = float(getattr(bot, "_last_incoming_message_at", 0.0) or 0.0)
    except (TypeError, ValueError):
        last_incoming_at = 0.0
    if last_incoming_at > 0 and now_ts - last_incoming_at < AUTO_MAINTENANCE_ACTIVITY_GRACE_SECONDS:
        return True

    pipelines = getattr(bot, "_private_message_pipelines", {}) or {}
    if isinstance(pipelines, dict):
        for pipeline in pipelines.values():
            if not isinstance(pipeline, dict):
                continue
            if (
                pipeline.get("open_messages")
                or pipeline.get("queued_batches")
                or bool(pipeline.get("worker_running"))
            ):
                return True

    pending_voice = getattr(bot, "_pending_private_voice_transcription", {}) or {}
    if isinstance(pending_voice, dict) and pending_voice:
        return True

    delayed_listen = getattr(bot, "_lightweight_delayed_listen_tasks", {}) or {}
    return isinstance(delayed_listen, dict) and bool(delayed_listen)


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
        "next_start_identity": str(maintenance.get("auto_cycle_next_start_identity") or "").strip(),
        "backup_start_name": str(maintenance.get("auto_cycle_backup_start_name") or "").strip(),
        "backup_start_identity": str(maintenance.get("auto_cycle_backup_start_identity") or "").strip(),
        "last_progress_at": str(maintenance.get("auto_cycle_last_progress_at") or "").strip(),
        "last_outcome": str(maintenance.get("auto_cycle_last_outcome") or "").strip(),
        "last_restart_at": str(maintenance.get("auto_cycle_last_restart_at") or "").strip(),
        "last_full_scan_completed_at": str(maintenance.get("last_full_scan_completed_at") or "").strip(),
        "batches_completed": batches,
        "retry_count": retries,
    }


def reset_legacy_tail_complete_auto_cycle(directory, *, now=None) -> tuple[dict[str, Any], bool]:
    maintenance = ((directory or {}).get("maintenance") or {}) if isinstance(directory, dict) else {}
    if not isinstance(maintenance, dict):
        return directory if isinstance(directory, dict) else {}, False
    if (
        str(maintenance.get("auto_cycle_status") or "").strip().lower() != "completed"
        or str(maintenance.get("last_batch_outcome") or "").strip() != "tail_complete"
    ):
        return directory, False
    updated = write_contact_directory_auto_cycle_state(
        directory,
        now=now,
        auto_cycle_status="reset_required",
        auto_cycle_next_start_name="",
        auto_cycle_next_start_identity="",
        auto_cycle_backup_start_name="",
        auto_cycle_backup_start_identity="",
        auto_cycle_last_outcome="legacy_tail_complete_reset",
        auto_cycle_retry_count=0,
        last_full_scan_completed_at="",
    )
    updated.setdefault("maintenance", {})["last_error"] = "旧版短批次完成判定已重置，等待重新维护"
    return updated, True


def write_contact_directory_auto_cycle_state(directory, *, now=None, **updates):
    directory = directory if isinstance(directory, dict) else {}
    maintenance = directory.setdefault("maintenance", {})
    stamp = maintenance_now(now).strftime("%Y-%m-%d %H:%M:%S")
    for key, value in updates.items():
        if key in {
            "auto_cycle_batches_completed",
            "auto_cycle_retry_count",
        }:
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
    maintenance.setdefault("auto_cycle_next_start_identity", "")
    maintenance.setdefault("auto_cycle_backup_start_name", "")
    maintenance.setdefault("auto_cycle_backup_start_identity", "")
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


def save_contact_maintenance_state(bot, directory, *, directory_file=None, wx_id=""):
    """Persist only maintenance fields without overwriting newer contacts."""
    load_directory_fn = getattr(bot, "_load_contact_profiles_directory", None)
    save_directory_fn = getattr(bot, "_save_contact_profiles_directory", None)
    if directory_file is None and callable(load_directory_fn):
        _initial, directory_file, loaded_wx_id = load_directory_fn()
        wx_id = str(loaded_wx_id or wx_id or "").strip()
    elif directory_file is None:
        directory_file, wx_id = contact_profiles_directory_file(bot)
    with contact_directory_lock(directory_file):
        if callable(load_directory_fn):
            latest, _loaded_file, loaded_wx_id = load_directory_fn()
            wx_id = str(loaded_wx_id or wx_id or "").strip()
        else:
            latest = load_contact_directory(directory_file, wx_id=wx_id)
        latest = latest if isinstance(latest, dict) else {}
        latest["maintenance"] = copy.deepcopy((directory or {}).get("maintenance") or {})
        if wx_id:
            latest["wx_id"] = wx_id
        if callable(save_directory_fn):
            save_directory_fn(latest)
        else:
            save_contact_directory(directory_file, latest)
        return latest


def mark_contact_directory_full_scan_completed(bot, directory, *, automatic: bool = False, now=None):
    stamp = (now or datetime.now()).strftime("%Y-%m-%d %H:%M:%S")
    cycle_state_fn = getattr(bot, "_contact_directory_auto_cycle_state", None)
    if callable(cycle_state_fn):
        cycle = cycle_state_fn(directory)
    else:
        cycle = contact_directory_auto_cycle_state(directory)
    write_cycle_fn = getattr(bot, "_write_contact_directory_auto_cycle_state", None)
    updates = {
        "auto_cycle_status": "completed" if automatic else "idle",
        "auto_cycle_started_at": "" if not automatic else (cycle["started_at"] or stamp),
        "auto_cycle_next_start_name": "",
        "auto_cycle_backup_start_name": "",
        "auto_cycle_last_progress_at": stamp,
        "auto_cycle_last_outcome": "completed",
        "auto_cycle_last_restart_at": "" if not automatic else cycle["last_restart_at"],
        "auto_cycle_batches_completed": 0 if not automatic else cycle["batches_completed"],
        "auto_cycle_retry_count": 0,
        "last_full_scan_completed_at": stamp,
    }
    if callable(write_cycle_fn):
        updated = write_cycle_fn(directory, **updates)
    else:
        updated = write_contact_directory_auto_cycle_state(directory, **updates)
    return save_contact_maintenance_state(bot, updated)


def refresh_contact_profiles_single_batch(
    bot,
    mode="standard",
    start_name="",
    start_identity="",
    interval=None,
    *,
    use_saved_position=False,
    count_override=None,
    log_start_finish=True,
    previous_next_start_name="",
    previous_next_start_identity="",
    run_kind="manual_standard",
    logical_start_name=None,
    logical_start_identity=None,
    switch_back_to_chat=True,
    block_on_wechat_lock=True,
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
    if getattr(bot, "_ui_owner", None) is not None:
        settings["count"] = 50
    read_interval = 0 if run_kind == "auto_maintenance" else settings["interval"]
    callback_start_name = effective_start_name(
        directory,
        start_name,
        use_saved_position=bool(use_saved_position),
    )
    used_start_name = str(logical_start_name or callback_start_name or "").strip()
    callback_start_identity = _clean_text(start_identity)
    used_start_identity = _clean_text(logical_start_identity or callback_start_identity)
    if log_start_finish:
        _bot_log(bot, message=f"[通讯录维护] 开始{mode_label}，起点：{used_start_name or '通讯录头部'}")
    running_directory = maintenance_snapshot(
        directory,
        mode=settings["mode"],
        status="running",
        paused=False,
        start_name=used_start_name,
        start_identity=used_start_identity,
    )
    running_directory = save_contact_maintenance_state(
        bot,
        running_directory,
        directory_file=directory_file,
        wx_id=wx_id,
    )

    callback_names = []
    matched_name = ""
    raw_result_count = 0
    raw_result_identities: list[str] = []
    result_identities: list[str] = []
    cursor_candidates: list[dict[str, str]] = []
    callback_seen_names = set()

    def callback_detail_name(detail):
        if isinstance(detail, dict):
            return _detail_name(detail)
        return _clean_text(detail)

    def pause_requested():
        try:
            latest = load_contact_directory(directory_file, wx_id=wx_id)
            return bool(((latest or {}).get("maintenance") or {}).get("paused", False))
        except Exception:
            return False

    def callback(detail):
        nonlocal matched_name
        name_text = callback_detail_name(detail)
        matched = contact_name_matches(name_text, callback_start_name)
        if matched and not matched_name:
            matched_name = name_text
        if matched:
            callback_names.append(name_text)
            display_name = _clean_text(name_text)
            if display_name and display_name not in callback_seen_names:
                callback_seen_names.add(display_name)
                read_count = len(callback_seen_names)
                if _should_log_contact_read_progress(read_count):
                    _bot_log(bot, message=f"[通讯录维护] 已读取联系人 {read_count} 人，当前：{display_name}")
            if callback_start_name:
                time.sleep(CONTACT_CURSOR_MATCH_SETTLE_SECONDS)
        return matched

    local_contact_source = False
    result = []
    if True:
        if getattr(bot, "_ui_owner", None) is not None:
            release_wechat_lock = lambda: None
        else:
            release_wechat_lock = wechat_ui_actions.acquire(bot, blocking=bool(block_on_wechat_lock))
        if not release_wechat_lock:
            raise RuntimeError("微信操作繁忙，已跳过本次通讯录维护")
        try:
            def read_friend_details():
                nonlocal matched_name, raw_result_count, raw_result_identities, result_identities, cursor_candidates
                if pause_requested():
                    _bot_log(bot, message="[通讯录维护] 检测到停止请求，跳过本次读取")
                    return []
                if run_kind != "auto_maintenance" and getattr(bot, "_ui_owner", None) is None:
                    prepare_window_fn = getattr(bot, "_prepare_contact_directory_window", None)
                    if callable(prepare_window_fn):
                        prepare_window_fn()
                    else:
                        prepare_contact_directory_window(bot)
                read_success = False
                try:
                    with warn_slow_wechat_ui_action(f"GetFriendDetails(n={settings['count']})"):
                        setattr(bot, CONTACT_PROFILES_READING_ATTR, True)
                        if run_kind == "auto_maintenance" or getattr(bot, "_ui_owner", None) is not None:
                            timeout_seconds = contact_auto_maintenance_collect_hard_timeout_seconds(settings["count"])
                            collector_fn = getattr(bot, "_run_contact_auto_maintenance_collector", None)
                            if callable(collector_fn):
                                payload = collector_fn(
                                    start_name=callback_start_name,
                                    start_identity=callback_start_identity,
                                    count=settings["count"],
                                    timeout_seconds=timeout_seconds,
                                    run_kind=run_kind,
                                )
                            else:
                                payload = run_contact_auto_maintenance_collector(
                                    start_name=callback_start_name,
                                    start_identity=callback_start_identity,
                                    count=settings["count"],
                                    timeout_seconds=timeout_seconds,
                                )
                            child_callback_names = [
                                _clean_text(name)
                                for name in (payload.get("callback_names") or [])
                                if _clean_text(name)
                            ]
                            if child_callback_names:
                                callback_names.extend(child_callback_names)
                                if not matched_name:
                                    matched_name = _clean_text(payload.get("matched_name")) or child_callback_names[0]
                                for name_text in child_callback_names:
                                    if name_text in callback_seen_names:
                                        continue
                                    callback_seen_names.add(name_text)
                                    read_count = len(callback_seen_names)
                                    if _should_log_contact_read_progress(read_count):
                                        _bot_log(bot, message=f"[通讯录维护] 已读取联系人 {read_count} 人，当前：{name_text}")
                            result_identities = [
                                _clean_text(value)
                                for value in (payload.get("result_identities") or [])
                            ]
                            raw_result_identities = [
                                _clean_text(value)
                                for value in (payload.get("raw_result_identities") or [])
                                if _clean_text(value)
                            ]
                            raw_result_count = int(payload.get("raw_result_count") or len(raw_result_identities))
                            cursor_candidates = [
                                {
                                    "name": _clean_text(item.get("name")),
                                    "identity": _clean_text(item.get("identity")),
                                }
                                for item in (payload.get("cursor_candidates") or [])
                                if isinstance(item, dict)
                                and _clean_text(item.get("name"))
                                and _clean_text(item.get("identity"))
                            ]
                            result = payload.get("result") or []
                        else:
                            kwargs = {
                                "n": settings["count"],
                                "timeout": contact_read_timeout_seconds(settings["count"]),
                                "interval": read_interval,
                                "save_head_image": False,
                                "callback": callback,
                            }
                            result = bot.wx.GetFriendDetails(**kwargs)
                        read_success = True
                        return result
                except Exception:
                    if pause_requested():
                        _bot_log(bot, message="[通讯录维护] 读取已被停止请求中断")
                        read_success = True
                        return []
                    raise
                finally:
                    try:
                        setattr(bot, CONTACT_PROFILES_READING_ATTR, False)
                    except Exception:
                        pass
                    if switch_back_to_chat or not read_success:
                        restore_contact_directory_back_to_chat(bot)
            result = run_with_wechat_rebind_retry(
                bot,
                read_friend_details,
                attempts=1 if run_kind == "auto_maintenance" or getattr(bot, "_ui_owner", None) is not None else 2,
                on_retry=lambda exc, _attempt: _bot_log(
                    bot,
                    level="WARNING",
                    message=f"[通讯录维护] 读取好友资料失败，重新初始化微信客户端后重试：{exc}",
                ),
            )
        finally:
            release_wechat_lock()
    try:
        raw_details = coerce_detail_list(result)
        if not raw_result_count:
            raw_result_count = len(raw_details)
        if not raw_result_identities:
            raw_result_identities = [
                _detail_identity(detail, name=_detail_name(detail))
                for detail in raw_details
            ]
        if not callback_seen_names:
            total_details = len(raw_details)
            for index, detail in enumerate(raw_details, start=1):
                label = str(detail.get("备注") or detail.get("昵称") or detail.get("微信号") or f"联系人{index}")
                if _should_log_contact_read_progress(index):
                    _bot_log(bot, message=f"[通讯录维护] 已读取联系人 {index}/{total_details} 人，当前：{label}")
        with contact_directory_lock(directory_file):
            latest_before_merge = load_contact_directory(directory_file, wx_id=wx_id)
            merged = merge_contact_directory(
                latest_before_merge,
                raw_details,
                wx_id=wx_id,
                now=datetime.now(),
                mark_missing=False,
            )
        cursor_points = [
            (item["name"], item["identity"])
            for item in cursor_candidates[:2]
        ] or start_cursors_from_details(
            raw_details,
            result_identities,
            limit=2,
        )
        next_start_name, next_start_identity = cursor_points[0] if cursor_points else ("", "")
        backup_start_name, backup_start_identity = (
            cursor_points[1] if len(cursor_points) > 1 else ("", "")
        )
        analysis = analyze_refresh_batch(
            raw_details=raw_details,
            requested_count=settings["count"],
            current_start_name=used_start_name,
            current_start_identity=used_start_identity,
            previous_next_start_name=previous_next_start_name,
            previous_next_start_identity=previous_next_start_identity,
            result_identities=result_identities,
            allow_short_batch_complete=run_kind != "auto_maintenance",
        )
        next_start_name = str(analysis.get("next_start_name") or next_start_name)
        next_start_identity = _clean_text(analysis.get("next_start_identity") or next_start_identity)
        latest_directory = load_contact_directory(directory_file, wx_id=wx_id)
        externally_paused = bool(((latest_directory or {}).get("maintenance") or {}).get("paused", False))
        finished = maintenance_snapshot(
            merged,
            mode=settings["mode"],
            status="paused" if externally_paused else "idle",
            paused=externally_paused,
            start_name=used_start_name,
            start_identity=used_start_identity,
            count_returned=len(raw_details),
            matched_name=matched_name,
            next_start_name=next_start_name,
            next_start_identity=next_start_identity,
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
        with contact_directory_lock(directory_file):
            latest_before_save = load_contact_directory(directory_file, wx_id=wx_id)
            persisted = merge_contact_directory(
                latest_before_save,
                raw_details,
                wx_id=wx_id,
                now=datetime.now(),
                mark_missing=False,
            )
            persisted["maintenance"] = copy.deepcopy(finished.get("maintenance") or {})
            save_contact_directory(directory_file, persisted)
            finished = persisted
        sync_identity_fn = getattr(bot, "_sync_contact_identity_from_contact_directory", None)
        if callable(sync_identity_fn):
            synced_directory = sync_identity_fn(finished)
            if isinstance(synced_directory, dict):
                finished = synced_directory
        sync_relationship_fn = getattr(bot, "_sync_relationship_state_from_contact_directory", None)
        if callable(sync_relationship_fn):
            synced_directory = sync_relationship_fn(finished)
            if isinstance(synced_directory, dict):
                finished = synced_directory
        if log_start_finish:
            if externally_paused:
                _bot_log(bot, level="WARNING", message=f"[通讯录维护] {mode_label}已停止，本次读取 {len(raw_details)} 个好友")
            else:
                if run_kind == "auto_maintenance":
                    _bot_log(bot, level="SUCCESS", message=f"[通讯录维护] 自动维护本批完成，本次读取 {len(raw_details)} 个好友")
                    runtime_id = str(getattr(bot, "_runtime_instance_id", "") or "").strip().lower()
                    if len(runtime_id) == 32 and all(char in "0123456789abcdef" for char in runtime_id):
                        try:
                            _bot_log(bot, message=f"运行事件：通讯录批次完成 runtime_id={runtime_id}")
                        except Exception:
                            pass
                else:
                    _bot_log(bot, level="SUCCESS", message=f"[通讯录维护] {mode_label}完成，本次读取 {len(raw_details)} 个好友")
        return {
            "mode": settings["mode"],
            "wx_id": wx_id,
            "requested_start_name": str(start_name or "").strip(),
            "used_start_name": used_start_name,
            "matched_name": matched_name,
            "next_start_name": next_start_name,
            "next_start_identity": next_start_identity,
            "backup_start_name": backup_start_name,
            "backup_start_identity": backup_start_identity,
            "count_requested": settings["count"],
            "count_returned": len(raw_details),
            "interval": read_interval,
            "callback_names": callback_names,
            "result_identities": result_identities,
            "raw_result_count": raw_result_count,
            "raw_result_identities": raw_result_identities,
            "directory": finished,
            "stopped_early": externally_paused,
            "analysis": analysis,
            "completed": False if externally_paused else bool(analysis.get("completed")),
            "retry_count": 0,
            "stopped_reason": "paused" if externally_paused else "",
            "run_kind": run_kind,
            "local_contact_source": local_contact_source,
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
            start_identity=used_start_identity,
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
    start_identity="",
    interval=None,
    *,
    use_saved_position=False,
    count_override=None,
    run_to_completion=False,
    automatic=False,
    block_on_wechat_lock=True,
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
                start_identity=start_identity,
                interval=interval,
                use_saved_position=use_saved_position,
                count_override=count_override,
                run_kind=run_kind,
                block_on_wechat_lock=block_on_wechat_lock,
            )
        else:
            result = refresh_contact_profiles_single_batch(
                bot,
                mode=mode,
                start_name=start_name,
                start_identity=start_identity,
                interval=interval,
                use_saved_position=use_saved_position,
                count_override=count_override,
                run_kind=run_kind,
                block_on_wechat_lock=block_on_wechat_lock,
            )
        result["run_kind"] = run_kind
        return result

    initial_start_name = str(start_name or "").strip()
    current_start_name = initial_start_name
    current_start_identity = _clean_text(start_identity)
    total_count = 0
    batches_completed = 0
    stopped_early = False
    last_result = None
    seen_starts: set[tuple[str, str]] = set()
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
        single_batch_fn = getattr(bot, "_refresh_contact_profiles_single_batch", None)
        if callable(single_batch_fn):
            result = single_batch_fn(
                mode=mode,
                start_name=current_start_name,
                start_identity=current_start_identity,
                interval=interval,
                use_saved_position=False,
                count_override=count_override,
                log_start_finish=False,
                previous_next_start_name=current_start_name,
                previous_next_start_identity=current_start_identity,
                run_kind=run_kind,
                logical_start_name=current_start_name,
                logical_start_identity=current_start_identity,
                switch_back_to_chat=False,
                block_on_wechat_lock=block_on_wechat_lock,
            )
        else:
            result = refresh_contact_profiles_single_batch(
                bot,
                mode=mode,
                start_name=current_start_name,
                start_identity=current_start_identity,
                interval=interval,
                use_saved_position=False,
                count_override=count_override,
                log_start_finish=False,
                previous_next_start_name=current_start_name,
                previous_next_start_identity=current_start_identity,
                run_kind=run_kind,
                logical_start_name=current_start_name,
                logical_start_identity=current_start_identity,
                switch_back_to_chat=False,
                block_on_wechat_lock=block_on_wechat_lock,
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
        next_start_identity = _clean_text(result.get("next_start_identity"))
        next_cursor = (next_start_name, next_start_identity)
        current_cursor = (current_start_name, current_start_identity)

        retry_count = 0
        if policy.get("max_total") is not None and total_count >= int(policy["max_total"]):
            stopped_reason = "manual_cap_reached"
            result["completed"] = False
            break

        if bool(analysis.get("completed")):
            stopped_reason = "directory_complete"
            result["completed"] = True
            break

        if not next_start_name or next_cursor == current_cursor or next_cursor in seen_starts:
            stopped_reason = "stalled"
            result["completed"] = False
            break

        seen_starts.add(next_cursor)
        current_start_name = next_start_name
        current_start_identity = next_start_identity

    if last_result is None:
        raise RuntimeError("建档未启动，请重试")
    switch_contact_directory_back_to_chat(bot, use_lock=True)

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
        _bot_log(bot, level="SUCCESS", message=f"[通讯录维护] {mode_label}完成，本次共读取 {total_count} 个好友")

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
    if stopped_reason == "directory_complete" and not bool(last_result.get("local_contact_source")):
        final_directory = mark_contact_directory_full_scan_completed(bot, final_directory, automatic=automatic)
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
        directory, directory_file, wx_id = load_directory_fn()
    else:
        directory, directory_file, wx_id = load_contact_profiles_directory(bot)
    if not wx_id:
        return False
    now_fn = getattr(bot, "_maintenance_now", None)
    if callable(now_fn):
        now_dt = now_fn(now)
    else:
        now_dt = maintenance_now(now)
    reset_fn = getattr(bot, "_reset_legacy_tail_complete_auto_cycle", None)
    if callable(reset_fn):
        directory, legacy_reset = reset_fn(directory, now=now_dt)
    else:
        directory, legacy_reset = reset_legacy_tail_complete_auto_cycle(directory, now=now_dt)
    if legacy_reset:
        save_directory_fn = getattr(bot, "_save_contact_profiles_directory", None)
        if callable(save_directory_fn):
            save_directory_fn(directory)
        else:
            save_contact_profiles_directory(bot, directory)
        _bot_log(bot, level="WARNING", message="[通讯录维护] 检测到旧版短批次误完成状态，已重置并等待重新维护")

    full_scan_days_fn = getattr(bot, "_contact_directory_auto_maintenance_full_scan_interval_days_value", None)
    if callable(full_scan_days_fn):
        full_scan_days = full_scan_days_fn()
    else:
        full_scan_days = contact_directory_auto_maintenance_full_scan_interval_days_value(bot)

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
    idle_fn = getattr(bot, "_is_contact_directory_auto_maintenance_idle", None)
    if callable(idle_fn):
        is_idle = idle_fn()
    else:
        is_idle = is_contact_directory_auto_maintenance_idle(bot)
    if not is_idle:
        return False
    if has_active_contact_maintenance_conflict(bot):
        return False
    flush_lightweight = getattr(bot, "_flush_lightweight_send_queue", None)
    if callable(flush_lightweight):
        flush_lightweight()
    pending_queue_fn = getattr(bot, "_has_pending_lightweight_send_queue", None)
    if callable(pending_queue_fn):
        has_pending_queue = pending_queue_fn()
    else:
        has_pending_queue = has_pending_lightweight_send_queue(bot)
    if has_pending_queue:
        return False
    if has_pending_private_outbound_echoes(bot):
        return False

    cycle_state_fn = getattr(bot, "_contact_directory_auto_cycle_state", None)
    if callable(cycle_state_fn):
        cycle = cycle_state_fn(directory)
    else:
        cycle = contact_directory_auto_cycle_state(directory)
    active_cycle = cycle["status"] in {"running", "stalled"}
    if not active_cycle and not auto_maintenance_full_scan_is_due(
        directory,
        interval_days=full_scan_days,
        now=now_dt,
    ):
        return False

    cycle_start_name = cycle["next_start_name"]
    cycle_start_identity = cycle["next_start_identity"]
    if cycle["status"] == "reset_required":
        cycle_start_name = ""
        cycle_start_identity = ""
        active_cycle = False
    elif cycle_start_name and not cycle_start_identity:
        _bot_log(bot, level="WARNING", message="[通讯录维护] 当前游标缺少身份锚点，本轮从通讯录头部重开")
        cycle_start_name = ""
        active_cycle = False
    tail_probe_active = bool(
        active_cycle
        and cycle_start_name
        and cycle["last_outcome"] in {"short_advanced", "tail_confirm_pending"}
    )
    if cycle_start_name:
        backup_label = f"，备用游标：{cycle['backup_start_name']}" if cycle["backup_start_name"] else ""
        _bot_log(bot, message=f"[通讯录维护] 自动维护使用游标：{cycle_start_name}{backup_label}")
    else:
        _bot_log(bot, message="[通讯录维护] 自动维护从通讯录头部开始")

    release_preflight_lock = wechat_ui_actions.try_acquire(bot)
    if not release_preflight_lock:
        return False
    release_preflight_lock()

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
                    auto_cycle_next_start_identity="",
                    auto_cycle_backup_start_name="",
                    auto_cycle_backup_start_identity="",
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
                    auto_cycle_next_start_identity="",
                    auto_cycle_backup_start_name="",
                    auto_cycle_backup_start_identity="",
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
        request_count = AUTO_TAIL_PROBE_COUNT if tail_probe_active else batch_size
        try:
            refresh_batch_fn = getattr(bot, "refresh_contact_profiles_batch", None)
            if callable(refresh_batch_fn):
                result = refresh_batch_fn(
                mode="standard",
                start_name=cycle_start_name,
                start_identity=cycle_start_identity,
                    use_saved_position=bool(cycle_start_name),
                    count_override=request_count,
                    run_to_completion=False,
                    automatic=True,
                    block_on_wechat_lock=False,
                )
            else:
                result = refresh_contact_profiles_batch(
                    bot,
                mode="standard",
                start_name=cycle_start_name,
                start_identity=cycle_start_identity,
                    use_saved_position=bool(cycle_start_name),
                    count_override=request_count,
                    run_to_completion=False,
                    automatic=True,
                    block_on_wechat_lock=False,
                )
        except Exception as exc:
            retry_count = cycle["retry_count"] + 1
            fallback_start_name = cycle_start_name
            fallback_start_identity = cycle_start_identity
            fallback_backup_name = cycle["backup_start_name"]
            fallback_backup_identity = cycle["backup_start_identity"]
            fallback_status = "stalled"
            fallback_outcome = "batch_failed"
            if tail_probe_active:
                fallback_status = "running"
                fallback_outcome = "tail_confirm_pending"
            elif cycle_start_name and fallback_backup_name and cycle_start_name != fallback_backup_name:
                fallback_start_name = fallback_backup_name
                fallback_start_identity = fallback_backup_identity
                fallback_backup_name = ""
                fallback_backup_identity = ""
                fallback_outcome = "primary_cursor_failed"
            elif cycle_start_name:
                fallback_status = "reset_required"
                fallback_start_name = ""
                fallback_start_identity = ""
                fallback_backup_name = ""
                fallback_backup_identity = ""
                fallback_outcome = "cursor_batch_failed"
            failure_updates = {
                "auto_cycle_status": fallback_status,
                "auto_cycle_next_start_name": fallback_start_name,
                "auto_cycle_next_start_identity": fallback_start_identity,
                "auto_cycle_backup_start_name": fallback_backup_name,
                "auto_cycle_backup_start_identity": fallback_backup_identity,
                "auto_cycle_last_outcome": fallback_outcome,
                "auto_cycle_retry_count": retry_count,
            }
            if callable(write_cycle_fn):
                failed_directory = write_cycle_fn(directory, now=now_dt, **failure_updates)
            else:
                failed_directory = write_contact_directory_auto_cycle_state(directory, now=now_dt, **failure_updates)
            failed_directory.setdefault("maintenance", {})["last_error"] = str(exc)
            save_contact_maintenance_state(
                bot,
                failed_directory,
                directory_file=directory_file,
                wx_id=wx_id,
            )
            if tail_probe_active:
                next_step = f"保留尾部游标：{fallback_start_name}，稍后重试"
            elif fallback_start_name:
                next_step = f"改用游标：{fallback_start_name}"
            else:
                next_step = "从通讯录头部重开"
            _bot_log(
                bot,
                level="WARNING",
                message=f"[通讯录维护] 本批读取失败：{exc}；下一轮{next_step}",
            )
            raise
        refreshed_directory = result.get("directory") or {}
        if callable(cycle_state_fn):
            refreshed_cycle = cycle_state_fn(refreshed_directory)
        else:
            refreshed_cycle = contact_directory_auto_cycle_state(refreshed_directory)
        analysis = result.get("analysis") or {}
        outcome = str(analysis.get("outcome") or "").strip()
        next_start_name = str(result.get("next_start_name") or "").strip()
        next_start_identity = _clean_text(result.get("next_start_identity"))
        backup_start_name = str(result.get("backup_start_name") or "").strip()
        backup_start_identity = _clean_text(result.get("backup_start_identity"))
        finished_dt = maintenance_now(None)
        stamp = finished_dt.strftime("%Y-%m-%d %H:%M:%S")

        raw_result_identities = [
            _clean_text(value)
            for value in (result.get("raw_result_identities") or [])
            if _clean_text(value)
        ]
        raw_result_count = int(result.get("raw_result_count") or len(raw_result_identities))
        tail_probe_no_new_identity = bool(
            tail_probe_active
            and outcome in {"not_advanced", "suspicious_repeat"}
            and raw_result_count >= AUTO_TAIL_PROBE_COUNT
            and len(raw_result_identities) == raw_result_count
            and all(identity == cycle_start_identity for identity in raw_result_identities)
        )
        tail_confirm_retry_count = refreshed_cycle["retry_count"] + 1 if tail_probe_active else 0
        confirmed_tail_complete = tail_probe_no_new_identity
        ordinary_batch_complete = bool(result.get("completed")) and not tail_probe_active
        if ordinary_batch_complete or confirmed_tail_complete:
            if confirmed_tail_complete:
                _bot_log(
                    bot,
                    message=f"[通讯录维护] 自动维护短批次后游标连续未推进，确认本轮完成：{cycle_start_name}",
                )
            _bot_log(bot, level="SUCCESS", message="[通讯录维护] 本轮自动维护完成，已扫到通讯录尾部")
            if callable(write_cycle_fn):
                refreshed_directory = write_cycle_fn(
                    refreshed_directory,
                    now=finished_dt,
                    auto_cycle_status="completed",
                    auto_cycle_next_start_name="",
                    auto_cycle_next_start_identity="",
                    auto_cycle_backup_start_name="",
                    auto_cycle_backup_start_identity="",
                    auto_cycle_last_progress_at=stamp,
                    auto_cycle_last_outcome="completed",
                    auto_cycle_retry_count=0,
                    auto_cycle_batches_completed=refreshed_cycle["batches_completed"] + 1,
                    last_full_scan_completed_at=stamp,
                )
            else:
                refreshed_directory = write_contact_directory_auto_cycle_state(
                    refreshed_directory,
                    now=finished_dt,
                    auto_cycle_status="completed",
                    auto_cycle_next_start_name="",
                    auto_cycle_next_start_identity="",
                    auto_cycle_backup_start_name="",
                    auto_cycle_backup_start_identity="",
                    auto_cycle_last_progress_at=stamp,
                    auto_cycle_last_outcome="completed",
                    auto_cycle_retry_count=0,
                    auto_cycle_batches_completed=refreshed_cycle["batches_completed"] + 1,
                    last_full_scan_completed_at=stamp,
                )
        elif tail_probe_active and outcome in {"not_advanced", "suspicious_repeat"}:
            _bot_log(
                bot,
                level="WARNING",
                message=(
                    f"[通讯录维护] 自动维护短批次后游标暂未推进：{cycle_start_name}，"
                    "本轮未取得足够的重复身份凭据，稍后继续确认尾部"
                ),
            )
            if callable(write_cycle_fn):
                refreshed_directory = write_cycle_fn(
                    refreshed_directory,
                    now=finished_dt,
                    auto_cycle_status="running",
                    auto_cycle_next_start_name=cycle_start_name,
                    auto_cycle_next_start_identity=cycle_start_identity,
                    auto_cycle_backup_start_name=cycle["backup_start_name"],
                    auto_cycle_backup_start_identity=cycle["backup_start_identity"],
                    auto_cycle_last_outcome="tail_confirm_pending",
                    auto_cycle_retry_count=tail_confirm_retry_count,
                    auto_cycle_batches_completed=refreshed_cycle["batches_completed"] + 1,
                )
            else:
                refreshed_directory = write_contact_directory_auto_cycle_state(
                    refreshed_directory,
                    now=finished_dt,
                    auto_cycle_status="running",
                    auto_cycle_next_start_name=cycle_start_name,
                    auto_cycle_next_start_identity=cycle_start_identity,
                    auto_cycle_backup_start_name=cycle["backup_start_name"],
                    auto_cycle_backup_start_identity=cycle["backup_start_identity"],
                    auto_cycle_last_outcome="tail_confirm_pending",
                    auto_cycle_retry_count=tail_confirm_retry_count,
                    auto_cycle_batches_completed=refreshed_cycle["batches_completed"] + 1,
                )
        elif outcome in {"advanced", "short_advanced"}:
            _bot_log(
                bot,
                message=(
                    f"[通讯录维护] 自动维护游标推进：{cycle_start_name or '通讯录头部'} -> "
                    f"{next_start_name or '无'}"
                    f"{('，备用游标：' + backup_start_name) if backup_start_name else ''}"
                ),
            )
            if callable(write_cycle_fn):
                refreshed_directory = write_cycle_fn(
                    refreshed_directory,
                    now=finished_dt,
                    auto_cycle_status="running",
                    auto_cycle_next_start_name=next_start_name,
                    auto_cycle_next_start_identity=next_start_identity,
                    auto_cycle_backup_start_name=backup_start_name,
                    auto_cycle_backup_start_identity=backup_start_identity,
                    auto_cycle_last_progress_at=stamp,
                    auto_cycle_last_outcome=outcome,
                    auto_cycle_retry_count=0,
                    auto_cycle_batches_completed=refreshed_cycle["batches_completed"] + 1,
                )
            else:
                refreshed_directory = write_contact_directory_auto_cycle_state(
                    refreshed_directory,
                    now=finished_dt,
                    auto_cycle_status="running",
                    auto_cycle_next_start_name=next_start_name,
                    auto_cycle_next_start_identity=next_start_identity,
                    auto_cycle_backup_start_name=backup_start_name,
                    auto_cycle_backup_start_identity=backup_start_identity,
                    auto_cycle_last_progress_at=stamp,
                    auto_cycle_last_outcome=outcome,
                    auto_cycle_retry_count=0,
                    auto_cycle_batches_completed=refreshed_cycle["batches_completed"] + 1,
                )
        else:
            retry_count = refreshed_cycle["retry_count"] + 1
            fallback_status = "stalled"
            fallback_start_name = cycle_start_name or next_start_name
            fallback_start_identity = cycle_start_identity if cycle_start_name else next_start_identity
            fallback_backup_name = cycle["backup_start_name"] if cycle_start_name != cycle["backup_start_name"] else ""
            fallback_backup_identity = cycle["backup_start_identity"]
            fallback_outcome = outcome or "stalled"
            if cycle_start_name and retry_count == 1 and cycle["backup_start_name"] and cycle_start_name != cycle["backup_start_name"]:
                fallback_start_name = cycle["backup_start_name"]
                fallback_start_identity = cycle["backup_start_identity"]
                fallback_backup_name = ""
                fallback_backup_identity = ""
                fallback_outcome = outcome or "primary_cursor_failed"
            elif cycle_start_name or retry_count >= 3:
                fallback_status = "reset_required"
                fallback_start_name = ""
                fallback_start_identity = ""
                fallback_backup_name = ""
                fallback_backup_identity = ""
            _bot_log(
                bot,
                level="WARNING",
                message=(
                    f"[通讯录维护] 自动维护游标未推进：当前游标 {cycle_start_name or '通讯录头部'}，"
                    f"结果 {outcome or 'stalled'}，下一轮"
                    f"{'改用游标：' + fallback_start_name if fallback_start_name else '从通讯录头部重开'}"
                ),
            )
            if callable(write_cycle_fn):
                refreshed_directory = write_cycle_fn(
                    refreshed_directory,
                    now=finished_dt,
                    auto_cycle_status=fallback_status,
                    auto_cycle_next_start_name=fallback_start_name,
                    auto_cycle_next_start_identity=fallback_start_identity,
                    auto_cycle_backup_start_name=fallback_backup_name,
                    auto_cycle_backup_start_identity=fallback_backup_identity,
                    auto_cycle_last_outcome=fallback_outcome,
                    auto_cycle_retry_count=retry_count,
                )
            else:
                refreshed_directory = write_contact_directory_auto_cycle_state(
                    refreshed_directory,
                    now=finished_dt,
                    auto_cycle_status=fallback_status,
                    auto_cycle_next_start_name=fallback_start_name,
                    auto_cycle_next_start_identity=fallback_start_identity,
                    auto_cycle_backup_start_name=fallback_backup_name,
                    auto_cycle_backup_start_identity=fallback_backup_identity,
                    auto_cycle_last_outcome=fallback_outcome,
                    auto_cycle_retry_count=retry_count,
                )
        refreshed_directory = save_contact_maintenance_state(
            bot,
            refreshed_directory,
            directory_file=directory_file,
            wx_id=wx_id,
        )
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
    return save_contact_maintenance_state(
        bot,
        updated,
        directory_file=directory_file,
        wx_id=_wx_id,
    )


def contact_repair_before_display(contact):
    contact = contact or {}
    nickname = contact_display_name(contact)
    wechat_id = str(contact.get("wechat_id") or "").strip()
    parts = [part for part in (nickname, wechat_id) if part]
    return " | ".join(parts) or "未命名联系人"


def repair_contact_profile_remarks(bot, contact_keys=None):
    if not getattr(bot, "wx", None):
        raise RuntimeError("微信客户端未初始化，请先启动机器人并保持微信主窗口可用。")

    load_directory_fn = getattr(bot, "_load_contact_profiles_directory", None)
    save_directory_fn = getattr(bot, "_save_contact_profiles_directory", None)
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
    with wechat_ui_actions.hold(bot):
        for index, candidate in enumerate(candidates, start=1):
            contact_key = str(candidate.get("contact_key") or "")
            suggested_remark = str(candidate.get("suggested_remark") or "").strip()
            if callable(load_directory_fn):
                current_directory, _loaded_file, _loaded_wx_id = load_directory_fn()
            else:
                current_directory = load_contact_directory(directory_file, wx_id=wx_id)
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
                target_display = repair_display_fn(contact)
            else:
                target_display = contact_repair_before_display(contact)
            target_name = contact_edit_target_name(contact)
            error = ""
            success = False
            def apply_single_remark():
                return edit_friend_info_via_chat_profile(
                    bot,
                    target_name,
                    expected_names=contact_expected_chat_names(contact, target_name),
                    contact_key=contact_key,
                    remark=suggested_remark,
                    log_prefix="[通讯录维护]",
                )

            try:
                run_with_wechat_rebind_retry(
                    bot,
                    apply_single_remark,
                    attempts=2,
                    on_retry=lambda exc, _attempt: _bot_log(
                        bot,
                        level="WARNING",
                        message=f"[通讯录维护] 备注修复 {index}/{len(candidates)} 失败，重新初始化微信客户端后重试：{exc}",
                    ),
                )
                success = True
            except Exception as exc:
                error = str(exc)

            record = {
                "at": datetime.now().replace(microsecond=0).isoformat(),
                "wx_id": wx_id,
                "contact_key": contact_key,
                "target_name": target_name,
                "target_display": target_display,
                "old_remark": str(contact.get("remark") or ""),
                "new_remark": suggested_remark,
                "success": success,
                "error": error,
            }
            append_bounded_record(records_file, record, limit=1000)
            result["records"].append(record)
            if success:
                _bot_log(bot, level="INFO", message=f"[通讯录维护] 备注修复 {index}/{len(candidates)}：{target_display} -> {suggested_remark}")
            else:
                _bot_log(bot, level="WARNING", message=f"[通讯录维护] 备注修复失败 {index}/{len(candidates)}：{target_display} -> {suggested_remark}，错误：{error}")

            if success:
                with contact_directory_lock(directory_file):
                    if callable(load_directory_fn):
                        current_directory, _loaded_file, _loaded_wx_id = load_directory_fn()
                    else:
                        current_directory = load_contact_directory(directory_file, wx_id=wx_id)
                    current_directory = apply_repaired_remark(
                        current_directory,
                        contact_key,
                        suggested_remark,
                        now=datetime.now(),
                    )
                    if callable(save_directory_fn):
                        save_directory_fn(current_directory)
                    else:
                        save_contact_directory(directory_file, current_directory)
                sync_identity_fn = getattr(bot, "_sync_contact_identity_from_contact_directory", None)
                if callable(sync_identity_fn):
                    sync_identity_fn(current_directory)
                result["success_count"] += 1
            else:
                result["failed_count"] += 1
    result["directory"] = current_directory
    _bot_log(
        bot,
        level="SUCCESS" if result["failed_count"] == 0 else "WARNING",
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
