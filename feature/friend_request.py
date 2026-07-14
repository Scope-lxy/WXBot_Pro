"""Friend request outreach settings, candidates, and runtime orchestration."""

from __future__ import annotations

import copy
import json
import random
import uuid
from datetime import datetime, time as datetime_time, timedelta
from pathlib import Path
from typing import Any

from core.account_storage import account_area_file
from core.contact_profiles import (
    WARNING_DUPLICATE_SEND_NAME,
    WARNING_SEND_NAME_UNSEARCHABLE,
    contact_display_name,
    contact_identity_key,
    contact_send_name,
    directory_path,
    load_directory,
    mark_send_name_conflicts,
    normalize_tag_list,
)
from core.logger import log
from core import wechat_ui_actions
from feature.task_workbench_storage import file_lock_for_path


SCHEMA_VERSION = 1
AREA_NAME = "friend_request"
STATE_FILENAME = "state.json"

DEFAULT_SETTINGS = {
    "enabled": False,
    "add_object": "deleted_me",
    "include_tags": ["删除我的人"],
    "daily_limit": 20,
    "base_interval_minutes": 30,
    "allowed_time_ranges": ["09:00-22:00"],
    "permission": "不设置",
    "success_tags": [],
    "recent_duplicate_days": 7,
    "sender_kind": "conversation_verify",
}

ADD_OBJECT_DELETED_ME = "deleted_me"
ADD_OBJECT_GROUP_MEMBER = "group_member"
ADD_OBJECT_PHONE = "phone"
ADD_OBJECT_VALUES = {ADD_OBJECT_DELETED_ME, ADD_OBJECT_GROUP_MEMBER, ADD_OBJECT_PHONE}
ADD_OBJECT_OPTIONS = [
    {"value": ADD_OBJECT_DELETED_ME, "label": "删除我的人", "enabled": True},
    {"value": ADD_OBJECT_GROUP_MEMBER, "label": "群内成员", "enabled": False},
    {"value": ADD_OBJECT_PHONE, "label": "手机号", "enabled": False},
]


def _clean_text(value: Any) -> str:
    return str(value or "").strip()


def _iso_timestamp(now: Any = None) -> str:
    if isinstance(now, datetime):
        return now.replace(microsecond=0).isoformat()
    if isinstance(now, str):
        return now
    return datetime.now().replace(microsecond=0).isoformat()


def _parse_time(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return value
    text = _clean_text(value)
    if not text:
        return None
    for fmt in ("%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S.%f", "%Y-%m-%d %H:%M:%S.%f"):
        try:
            return datetime.strptime(text, fmt)
        except ValueError:
            continue
    return None


def _today_key(now: Any = None) -> str:
    current = now if isinstance(now, datetime) else _parse_time(now) or datetime.now()
    return current.strftime("%Y-%m-%d")


def _coerce_int(value: Any, default: int, minimum: int, maximum: int) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError):
        number = default
    return max(minimum, min(maximum, number))


def _clean_string_list(value: Any) -> list[str]:
    if isinstance(value, str):
        raw = [item.strip() for item in value.replace("，", ",").replace("；", ",").split(",")]
    elif isinstance(value, (list, tuple, set)):
        raw = list(value)
    else:
        raw = []
    result: list[str] = []
    seen: set[str] = set()
    for item in raw:
        text = _clean_text(item)
        if text and text not in seen:
            seen.add(text)
            result.append(text)
    return result


def normalize_time_ranges(value: Any) -> list[str]:
    ranges = _clean_string_list(value)
    result: list[str] = []
    for item in ranges:
        if "-" not in item:
            continue
        start, end = [part.strip() for part in item.split("-", 1)]
        if _parse_clock(start) and _parse_clock(end) and start < end:
            result.append(f"{start}-{end}")
    return result or list(DEFAULT_SETTINGS["allowed_time_ranges"])


def _parse_clock(value: Any) -> datetime_time | None:
    text = _clean_text(value)
    try:
        hour, minute = [int(part) for part in text.split(":", 1)]
        if 0 <= hour <= 23 and 0 <= minute <= 59:
            return datetime_time(hour, minute)
    except Exception:
        return None
    return None


def normalize_settings(settings: Any) -> dict[str, Any]:
    raw = dict(settings or {}) if isinstance(settings, dict) else {}
    add_object = _clean_text(raw.get("add_object")) or DEFAULT_SETTINGS["add_object"]
    if add_object not in ADD_OBJECT_VALUES:
        add_object = DEFAULT_SETTINGS["add_object"]
    permission = _clean_text(raw.get("permission")) or DEFAULT_SETTINGS["permission"]
    if permission not in {"不设置", "仅聊天"}:
        permission = DEFAULT_SETTINGS["permission"]
    sender_kind = _clean_text(raw.get("sender_kind")) or "conversation_verify"
    if sender_kind not in {"conversation_verify"}:
        sender_kind = "conversation_verify"
    return {
        "enabled": bool(raw.get("enabled", DEFAULT_SETTINGS["enabled"])),
        "add_object": add_object,
        "include_tags": _clean_string_list(raw.get("include_tags")) or list(DEFAULT_SETTINGS["include_tags"]),
        "daily_limit": _coerce_int(raw.get("daily_limit"), DEFAULT_SETTINGS["daily_limit"], 1, 200),
        "base_interval_minutes": _coerce_int(raw.get("base_interval_minutes"), DEFAULT_SETTINGS["base_interval_minutes"], 5, 1440),
        "allowed_time_ranges": normalize_time_ranges(raw.get("allowed_time_ranges", DEFAULT_SETTINGS["allowed_time_ranges"])),
        "permission": permission,
        "success_tags": _clean_string_list(raw.get("success_tags")),
        "recent_duplicate_days": _coerce_int(raw.get("recent_duplicate_days"), DEFAULT_SETTINGS["recent_duplicate_days"], 0, 365),
        "sender_kind": sender_kind,
    }


def normalize_message_rule(rule: Any) -> dict[str, Any] | None:
    if not isinstance(rule, dict):
        return None
    object_kind = _clean_text(rule.get("object_kind"))
    if object_kind not in ADD_OBJECT_VALUES:
        return None
    messages = _clean_string_list(rule.get("messages"))
    if not messages:
        return None
    return {
        "rule_id": _clean_text(rule.get("rule_id")) or f"rule_{abs(hash(tuple([object_kind] + messages))) % 100000}",
        "enabled": bool(rule.get("enabled", True)),
        "object_kind": object_kind,
        "messages": messages,
    }


def normalize_state(state: Any, wx_id: str = "") -> dict[str, Any]:
    fallback = default_state(wx_id)
    if not isinstance(state, dict):
        return fallback
    normalized = copy.deepcopy(fallback)
    normalized["schema_version"] = state.get("schema_version") or SCHEMA_VERSION
    normalized["wx_id"] = _clean_text(state.get("wx_id")) or _clean_text(wx_id)
    normalized["updated_at"] = _clean_text(state.get("updated_at"))
    normalized["settings"] = normalize_settings(state.get("settings"))
    rules = [normalize_message_rule(item) for item in (state.get("message_rules") or [])]
    normalized["message_rules"] = [item for item in rules if item]
    runtime = state.get("runtime")
    if isinstance(runtime, dict):
        normalized["runtime"].update(copy.deepcopy(runtime))
    normalized["candidates"] = [normalize_candidate(item) for item in (state.get("candidates") or []) if isinstance(item, dict)]
    normalized["executions"] = [dict(item) for item in (state.get("executions") or []) if isinstance(item, dict)]
    _reset_daily_runtime_if_needed(normalized)
    return normalized


def default_state(wx_id: str) -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "wx_id": _clean_text(wx_id),
        "updated_at": "",
        "settings": dict(DEFAULT_SETTINGS),
        "message_rules": [],
        "runtime": {
            "today": _today_key(),
            "today_sent": 0,
            "today_success": 0,
            "today_failed": 0,
            "last_sent_at": "",
            "next_run_at": "",
            "last_result": "",
        },
        "candidates": [],
        "executions": [],
    }


def state_path(base_dir: str | Path, wx_id: str) -> Path:
    return account_area_file(base_dir, wx_id, AREA_NAME, STATE_FILENAME, create_parent=True)


def load_state(base_dir: str | Path, wx_id: str) -> dict[str, Any]:
    path = state_path(base_dir, wx_id)
    if not path.exists():
        return default_state(wx_id)
    try:
        return normalize_state(json.loads(path.read_text(encoding="utf-8")), wx_id=wx_id)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return default_state(wx_id)


def save_state(base_dir: str | Path, state: dict[str, Any]) -> dict[str, Any]:
    state = normalize_state(state, wx_id=_clean_text((state or {}).get("wx_id")))
    state["updated_at"] = _iso_timestamp()
    path = state_path(base_dir, state["wx_id"])
    with file_lock_for_path(path):
        temp_path = path.with_name(path.name + ".tmp")
        temp_path.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
        temp_path.replace(path)
    return state


def normalize_candidate(candidate: dict[str, Any]) -> dict[str, Any]:
    tags = normalize_tag_list(candidate.get("tags"))
    name = (
        _clean_text(candidate.get("name"))
        or _clean_text(candidate.get("display_name"))
        or _clean_text(candidate.get("send_target"))
        or _clean_text(candidate.get("send_name"))
    )
    send_target = _clean_text(candidate.get("send_target")) or _clean_text(candidate.get("send_name")) or name
    candidate_id = _clean_text(candidate.get("candidate_id")) or _clean_text(candidate.get("contact_key")) or send_target
    status = _clean_text(candidate.get("status")) or "pending"
    if status not in {"pending", "sent", "skipped", "failed", "uncertain", "accepted", "archived"}:
        status = "pending"
    return {
        "candidate_id": candidate_id,
        "contact_key": _clean_text(candidate.get("contact_key")) or candidate_id,
        "name": name,
        "send_target": send_target,
        "remark": _clean_text(candidate.get("remark")),
        "nickname": _clean_text(candidate.get("nickname")),
        "wechat_id": _clean_text(candidate.get("wechat_id")),
        "tags": tags,
        "sender_kind": _clean_text(candidate.get("sender_kind")) or "conversation_verify",
        "add_object": _clean_text(candidate.get("add_object")) or ADD_OBJECT_DELETED_ME,
        "conversation_keyword": _clean_text(candidate.get("conversation_keyword")) or send_target or name,
        "status": status,
        "last_result": _clean_text(candidate.get("last_result")),
        "last_attempt_at": _clean_text(candidate.get("last_attempt_at")),
        "next_retry_at": _clean_text(candidate.get("next_retry_at")),
        "sent_at": _clean_text(candidate.get("sent_at")),
        "claim_token": _clean_text(candidate.get("claim_token")),
    }


def _reset_daily_runtime_if_needed(state: dict[str, Any], *, now: Any = None) -> None:
    runtime = state.setdefault("runtime", {})
    today = _today_key(now)
    if runtime.get("today") != today:
        runtime["today"] = today
        runtime["today_sent"] = 0
        runtime["today_success"] = 0
        runtime["today_failed"] = 0


def build_candidates_from_directory(directory: dict[str, Any], settings: dict[str, Any]) -> list[dict[str, Any]]:
    if settings.get("add_object") != ADD_OBJECT_DELETED_ME:
        return []
    directory = mark_send_name_conflicts(directory)
    include_tags = set(settings["include_tags"])
    unsafe_warnings = {WARNING_DUPLICATE_SEND_NAME, WARNING_SEND_NAME_UNSEARCHABLE}
    candidates: list[dict[str, Any]] = []
    seen: set[str] = set()
    for subject in directory.get("subjects") or []:
        if not isinstance(subject, dict):
            continue
        if subject.get("status", "active") != "active":
            continue
        if unsafe_warnings.intersection(subject.get("warnings") or []):
            continue
        tags = normalize_tag_list(subject.get("tags"))
        tag_set = set(tags)
        if include_tags and not (tag_set & include_tags):
            continue
        key = contact_identity_key(subject)
        if key in seen:
            continue
        seen.add(key)
        send_name = contact_send_name(subject)
        name = contact_display_name(subject)
        candidates.append(normalize_candidate({
            "candidate_id": key,
            "contact_key": key,
            "name": name,
            "send_target": send_name,
            "remark": _clean_text(subject.get("remark")),
            "nickname": _clean_text(subject.get("nickname")),
            "wechat_id": _clean_text(subject.get("wechat_id")),
            "tags": tags,
            "sender_kind": "conversation_verify",
            "add_object": ADD_OBJECT_DELETED_ME,
            "conversation_keyword": send_name or name,
            "status": "pending",
        }))
    return candidates


def refresh_candidates(base_dir: str | Path, wx_id: str, *, contact_base_dir: str | Path | None = None) -> dict[str, Any]:
    state = load_state(base_dir, wx_id)
    settings = normalize_settings(state.get("settings"))
    directory_file = directory_path(contact_base_dir or base_dir, wx_id)
    directory = load_directory(directory_file, wx_id=wx_id)
    previous = {item["candidate_id"]: item for item in state.get("candidates") or []}
    candidates = []
    for candidate in build_candidates_from_directory(directory, settings):
        old = previous.get(candidate["candidate_id"])
        if old:
            candidate["status"] = old.get("status", "pending")
            candidate["last_result"] = old.get("last_result", "")
            candidate["last_attempt_at"] = old.get("last_attempt_at", "")
            candidate["next_retry_at"] = old.get("next_retry_at", "")
            candidate["sent_at"] = old.get("sent_at", "")
            candidate["claim_token"] = old.get("claim_token", "")
        candidates.append(candidate)
    state["candidates"] = candidates
    return save_state(base_dir, state)


def _in_allowed_time(settings: dict[str, Any], *, now: Any = None) -> bool:
    current = now if isinstance(now, datetime) else _parse_time(now) or datetime.now()
    clock = current.time()
    for item in settings["allowed_time_ranges"]:
        start_text, end_text = item.split("-", 1)
        start = _parse_clock(start_text)
        end = _parse_clock(end_text)
        if start and end and start <= clock <= end:
            return True
    return False


def _next_interval_minutes(settings: dict[str, Any]) -> int:
    base = int(settings.get("base_interval_minutes", DEFAULT_SETTINGS["base_interval_minutes"]) or DEFAULT_SETTINGS["base_interval_minutes"])
    jitter = max(1, round(base * 0.1))
    return max(1, base + random.randint(-jitter, jitter))


def _interval_due(state: dict[str, Any], settings: dict[str, Any], *, now: Any = None) -> bool:
    current = now if isinstance(now, datetime) else _parse_time(now) or datetime.now()
    next_run = _parse_time((state.get("runtime") or {}).get("next_run_at"))
    if next_run:
        return current >= next_run
    last_sent = _parse_time((state.get("runtime") or {}).get("last_sent_at"))
    if not last_sent:
        return True
    return current - last_sent >= timedelta(minutes=settings["base_interval_minutes"])


def _recent_duplicate(candidate: dict[str, Any], state: dict[str, Any], settings: dict[str, Any], *, now: Any = None) -> bool:
    days = int(settings.get("recent_duplicate_days", 0) or 0)
    if days <= 0:
        return False
    current = now if isinstance(now, datetime) else _parse_time(now) or datetime.now()
    candidate_id = candidate.get("candidate_id")
    for item in state.get("executions") or []:
        if item.get("candidate_id") != candidate_id:
            continue
        if item.get("status") != "sent":
            continue
        at = _parse_time(item.get("at"))
        if at and current - at <= timedelta(days=days):
            return True
    return False


def candidate_matches_settings(candidate: dict[str, Any], settings: dict[str, Any]) -> bool:
    if (_clean_text(candidate.get("add_object")) or ADD_OBJECT_DELETED_ME) != settings["add_object"]:
        return False
    include_tags = set(settings.get("include_tags") or [])
    if include_tags and not (set(normalize_tag_list(candidate.get("tags"))) & include_tags):
        return False
    return True


def _retry_due(candidate: dict[str, Any], *, now: Any = None) -> bool:
    retry_at = _parse_time(candidate.get("next_retry_at"))
    if not retry_at:
        return True
    current = now if isinstance(now, datetime) else _parse_time(now) or datetime.now()
    return current >= retry_at


def candidate_can_run(candidate: dict[str, Any], state: dict[str, Any], settings: dict[str, Any], *, now: Any = None) -> bool:
    status = candidate.get("status")
    if status not in {"pending", "failed", "sent"}:
        return False
    if not candidate_matches_settings(candidate, settings):
        return False
    if status == "failed" and not _retry_due(candidate, now=now):
        return False
    if _recent_duplicate(candidate, state, settings, now=now):
        return False
    return True


def select_message_for_candidate(state: dict[str, Any], candidate: dict[str, Any]) -> str:
    object_kind = _clean_text(candidate.get("add_object")) or ADD_OBJECT_DELETED_ME
    messages: list[str] = []
    for rule in state.get("message_rules") or []:
        if not rule.get("enabled", True):
            continue
        if _clean_text(rule.get("object_kind")) != object_kind:
            continue
        messages.extend(rule.get("messages") or [])
    return random.choice(messages) if messages else ""


def next_pending_candidate(state: dict[str, Any], *, now: Any = None, ignore_schedule: bool = False) -> tuple[dict[str, Any] | None, str]:
    settings = normalize_settings(state.get("settings"))
    _reset_daily_runtime_if_needed(state, now=now)
    runtime = state.setdefault("runtime", {})
    if int(runtime.get("today_sent", 0) or 0) >= settings["daily_limit"]:
        return None, "今日申请数已达到上限"
    if not ignore_schedule:
        if not _in_allowed_time(settings, now=now):
            return None, "当前不在可发送时间段"
        if not _interval_due(state, settings, now=now):
            return None, "申请间隔未到"
    for candidate in state.get("candidates") or []:
        if not candidate_can_run(candidate, state, settings, now=now):
            continue
        if not candidate.get("conversation_keyword"):
            candidate["status"] = "skipped"
            candidate["last_result"] = "缺少可打开会话名"
            continue
        return candidate, ""
    return None, "没有待申请候选人"


def record_execution(state: dict[str, Any], candidate: dict[str, Any], result: dict[str, Any], *, addmsg: str, now: Any = None) -> dict[str, Any]:
    timestamp = _iso_timestamp(now)
    _reset_daily_runtime_if_needed(state, now=timestamp)
    runtime = state.setdefault("runtime", {})
    status = _clean_text(result.get("status")) or "failed"
    message = _clean_text(result.get("message"))
    if status == "sent":
        candidate["status"] = "sent"
        candidate["sent_at"] = timestamp
        candidate["next_retry_at"] = ""
        runtime["today_sent"] = int(runtime.get("today_sent", 0) or 0) + 1
        runtime["today_success"] = int(runtime.get("today_success", 0) or 0) + 1
        runtime["last_sent_at"] = timestamp
        current = _parse_time(timestamp) or datetime.now()
        runtime["next_run_at"] = (current + timedelta(minutes=_next_interval_minutes(normalize_settings(state.get("settings"))))).replace(microsecond=0).isoformat()
    elif status == "skipped":
        candidate["status"] = "skipped"
    elif status == "uncertain":
        candidate["status"] = "uncertain"
        candidate["next_retry_at"] = ""
        runtime["today_uncertain"] = int(runtime.get("today_uncertain", 0) or 0) + 1
    else:
        candidate["status"] = "failed"
        current = _parse_time(timestamp) or datetime.now()
        candidate["next_retry_at"] = (current + timedelta(minutes=_next_interval_minutes(normalize_settings(state.get("settings"))))).replace(microsecond=0).isoformat()
        runtime["today_failed"] = int(runtime.get("today_failed", 0) or 0) + 1
    candidate["last_attempt_at"] = timestamp
    candidate["last_result"] = message
    candidate["claim_token"] = ""
    runtime["last_result"] = f"{candidate.get('name') or candidate.get('send_target')}: {message}"
    execution = {
        "at": timestamp,
        "candidate_id": candidate.get("candidate_id"),
        "target": candidate.get("conversation_keyword"),
        "name": candidate.get("name"),
        "status": status,
        "message": message,
        "addmsg": addmsg,
        "data": result.get("data") if isinstance(result.get("data"), dict) else {},
    }
    state.setdefault("executions", []).append(execution)
    state["executions"] = state["executions"][-300:]
    return state


def save_execution_state(base_dir: str | Path, state: dict[str, Any]) -> dict[str, Any]:
    wx_id = _clean_text(state.get("wx_id")) or "default"
    path = state_path(base_dir, wx_id)
    with file_lock_for_path(path):
        latest = load_state(base_dir, wx_id)
        latest_candidates = {item.get("candidate_id"): item for item in latest.get("candidates") or []}
        for candidate in state.get("candidates") or []:
            candidate_id = candidate.get("candidate_id")
            if candidate_id in latest_candidates:
                latest_candidates[candidate_id].update(copy.deepcopy(candidate))
        latest["runtime"] = copy.deepcopy(state.get("runtime") or {})
        latest["executions"] = copy.deepcopy(state.get("executions") or [])
        return save_state(base_dir, latest)


def ui_guard(state: dict[str, Any], candidate: dict[str, Any]) -> tuple[str, int]:
    candidate_id = _clean_text(candidate.get("candidate_id"))
    if not candidate_id:
        return "", 0
    definition = {
        "settings": normalize_settings(state.get("settings")),
        "message_rules": state.get("message_rules") or [],
        "candidate": dict(candidate),
    }
    return f"friend_request:{candidate_id}", wechat_ui_actions.task_definition_version(definition)


def run_once(bot, *, force: bool = False, now: Any = None) -> dict[str, Any]:
    wx_id = _clean_text(getattr(bot, "wx_id", "")) or "default"
    path = state_path(bot.config.DATA_DIR, wx_id)
    with file_lock_for_path(path):
        state = load_state(bot.config.DATA_DIR, wx_id)
        if not state.get("candidates"):
            state = refresh_candidates(bot.config.DATA_DIR, wx_id)
        settings = normalize_settings(state.get("settings"))
        candidate, reason = next_pending_candidate(state, now=now, ignore_schedule=bool(force))
        if not candidate:
            state.setdefault("runtime", {})["last_result"] = reason
            state = save_state(bot.config.DATA_DIR, state)
            return {"status": "skipped", "message": reason, "payload": friend_request_payload(state)}
        addmsg = select_message_for_candidate(state, candidate)
        claim_token = uuid.uuid4().hex
        candidate["status"] = "uncertain"
        candidate["claim_token"] = claim_token
        candidate["next_retry_at"] = ""
        candidate["last_attempt_at"] = _iso_timestamp(now)
        candidate["last_result"] = "好友申请正在提交；若进程中断需人工核实"
        state = save_state(bot.config.DATA_DIR, state)
        candidate = next(
            item for item in state["candidates"]
            if _clean_text(item.get("candidate_id")) == _clean_text(candidate.get("candidate_id"))
        )

    task_key, task_version = ui_guard(state, candidate)
    try:
        owner = getattr(bot, "_ui_owner", None)
        if owner is None:
            raise RuntimeError("微信 UI owner 未运行")
        result = owner.call(
            wechat_ui_actions.UIIntent(
                wechat_ui_actions.UIIntentKind.FRIEND_REQUEST,
                {
                    "target": _clean_text(candidate.get("conversation_keyword")),
                    "contact_key": _clean_text(candidate.get("contact_key")),
                    "task_key": task_key,
                    "addmsg": addmsg,
                    "remark": _clean_text(candidate.get("remark") or candidate.get("name") or candidate.get("conversation_keyword")),
                    "tags": list(settings.get("success_tags") or []),
                    "permission": _clean_text(settings.get("permission")) or "不设置",
                    "max_attempts": 2,
                },
                task_version=task_version,
            ),
            wechat_ui_actions.UI_CALL_WAIT_TIMEOUT,
        )
    except wechat_ui_actions.IntentCancelled as exc:
        result = {"status": "cancelled", "message": _clean_text(exc) or "好友申请已取消"}
    except Exception as exc:
        result = {"status": "uncertain", "message": _clean_text(exc) or "好友申请提交结果未知"}
    if _clean_text(result.get("status")) == "cancelled":
        path = state_path(bot.config.DATA_DIR, wx_id)
        with file_lock_for_path(path):
            latest = load_state(bot.config.DATA_DIR, wx_id)
            latest_candidate = next((
                item for item in (latest.get("candidates") or [])
                if _clean_text(item.get("candidate_id")) == _clean_text(candidate.get("candidate_id"))
            ), None)
            if (
                latest_candidate is not None
                and _clean_text(latest_candidate.get("claim_token")) == claim_token
                and _clean_text(latest_candidate.get("status")) == "uncertain"
            ):
                latest_candidate["status"] = "pending"
                latest_candidate["claim_token"] = ""
                latest_candidate["last_attempt_at"] = ""
                latest_candidate["next_retry_at"] = ""
                latest_candidate["last_result"] = "规则已更新，本次尚未提交微信"
                latest = save_state(bot.config.DATA_DIR, latest)
        return {
            "status": "skipped",
            "message": result.get("message", ""),
            "payload": friend_request_payload(latest),
            "result": result,
        }
    state = record_execution(state, candidate, result, addmsg=addmsg, now=now)
    state = save_execution_state(bot.config.DATA_DIR, state)
    if _clean_text(result.get("status")) == "sent":
        bot._metric_increment("friend_request_sent_count")
    status = _clean_text(result.get("status"))
    target_label = (
        _clean_text(candidate.get("name"))
        or _clean_text(candidate.get("send_target"))
        or _clean_text(candidate.get("conversation_keyword"))
        or "未知好友"
    )
    if status == "sent":
        log(level="INFO", message=f"[好友申请] 已发送好友申请：{target_label}")
    elif status == "failed":
        log(level="WARNING", message=f"[好友申请] 发送失败：{target_label}，{_clean_text(result.get('message')) or '未知原因'}")
    elif status == "uncertain":
        log(level="WARNING", message=f"[好友申请] 提交结果待核实：{target_label}，不会自动重发")
    return {"status": result.get("status", "failed"), "message": result.get("message", ""), "payload": friend_request_payload(state), "result": result}


def due_for_auto_run(state: dict[str, Any], *, now: Any = None) -> bool:
    settings = normalize_settings(state.get("settings"))
    if not settings["enabled"]:
        return False
    candidate, _reason = next_pending_candidate(state, now=now)
    return bool(candidate)


def check_auto_run(bot, *, now: Any = None) -> bool:
    wx_id = _clean_text(getattr(bot, "wx_id", "")) or "default"
    state = load_state(bot.config.DATA_DIR, wx_id)
    if not due_for_auto_run(state, now=now):
        return False
    run_once(bot, now=now)
    return True


def friend_request_summary(state: dict[str, Any]) -> dict[str, Any]:
    _reset_daily_runtime_if_needed(state)
    settings = normalize_settings(state.get("settings"))
    candidates = state.get("candidates") or []
    runtime = state.get("runtime") or {}
    return {
        "enabled": bool((state.get("settings") or {}).get("enabled", False)),
        "candidate_count": len(candidates),
        "pending_count": sum(1 for item in candidates if item.get("conversation_keyword") and candidate_can_run(item, state, settings)),
        "sent_count": sum(1 for item in candidates if item.get("status") == "sent"),
        "today_sent": int(runtime.get("today_sent", 0) or 0),
        "today_success": int(runtime.get("today_success", 0) or 0),
        "today_failed": int(runtime.get("today_failed", 0) or 0),
        "last_result": runtime.get("last_result", ""),
        "updated_at": state.get("updated_at", ""),
    }


def friend_request_payload(state: dict[str, Any]) -> dict[str, Any]:
    state = normalize_state(state, wx_id=_clean_text((state or {}).get("wx_id")))
    settings = normalize_settings(state.get("settings"))
    candidates = []
    for candidate in state.get("candidates", [])[:100]:
        item = dict(candidate)
        item["can_run"] = candidate_can_run(candidate, state, settings)
        candidates.append(item)
    return {
        "settings": state.get("settings", {}),
        "add_object_options": [dict(item) for item in ADD_OBJECT_OPTIONS if item.get("enabled")],
        "message_rules": state.get("message_rules", []),
        "runtime": state.get("runtime", {}),
        "summary": friend_request_summary(state),
        "candidates": candidates,
        "executions": list(reversed(state.get("executions", [])[-30:])),
    }
