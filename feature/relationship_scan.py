"""Relationship scan rules and runtime helpers."""

from __future__ import annotations

import copy
import json
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from core.account_storage import account_area_file
from core.contact_profiles import (
    contact_identity_key,
    load_directory as load_contact_directory,
    normalize_tag_list,
    save_directory as save_contact_directory,
)
from core.logger import log
from core.local_wechat_reader import read_local_sessions_with_status
from core.wechat_observability import warn_slow_wechat_ui_action
from feature.contacts import modify_friend_tags_via_chat_profile


SCHEMA_VERSION = 1
AREA_NAME = "relationship_scan"
STATE_FILENAME = "relationships.json"

STATUS_BLOCKED = "blocked"
STATUS_DELETED = "deleted"
STATUS_NORMAL = "normal"

SYNC_PENDING = "pending"
SYNC_SYNCED = "synced"
SYNC_SKIPPED = "skipped"

TAG_BLOCKED = "拉黑我的人"
TAG_DELETED = "删除我的人"
RELATION_TAGS = (TAG_BLOCKED, TAG_DELETED)

FULL_SCAN_MAX_SCROLLS = 1000
CLI_SESSION_SCAN_LIMIT = 1000
CLI_FULL_SESSION_SCAN_LIMIT = 10000
CLI_AUTO_SCAN_INTERVAL_SECONDS = 6000
FULL_SCAN_LOCK_SLICE_SCROLLS = 200
FULL_SCAN_STALE_ROUNDS = 8
FULL_SCAN_SCROLL_SETTLE_SECONDS = 1.0
FULL_SCAN_LOCK_RELEASE_SETTLE_SECONDS = 0.2

EVENT_BLOCKED = "blocked"
EVENT_DELETED = "deleted"
EVENT_RECOVERED = "recovered"
EVENT_WECHAT_SYNCED = "wechat_synced"
EVENT_WECHAT_SYNC_FAILED = "wechat_sync_failed"

DEFAULT_SETTINGS = {
    "auto_scan_enabled": True,
    "auto_sync_wechat_tags": True,
    "sync_batch_size": 5,
    "sync_interval_minutes": 10,
    "scan_interval_seconds": 10,
}
_last_local_session_warning_at = 0.0


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


def coerce_sync_batch_size(value: Any) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError):
        number = DEFAULT_SETTINGS["sync_batch_size"]
    return max(1, min(10, number))


def coerce_scan_interval_seconds(value: Any) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError):
        number = DEFAULT_SETTINGS["scan_interval_seconds"]
    return max(5, min(20, number))


def coerce_sync_interval_minutes(value: Any) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError):
        number = DEFAULT_SETTINGS["sync_interval_minutes"]
    return max(1, min(1440, number))


def normalize_settings(settings: Any) -> dict[str, Any]:
    raw = dict(settings or {}) if isinstance(settings, dict) else {}
    return {
        "auto_scan_enabled": bool(raw.get("auto_scan_enabled", DEFAULT_SETTINGS["auto_scan_enabled"])),
        "auto_sync_wechat_tags": bool(raw.get("auto_sync_wechat_tags", DEFAULT_SETTINGS["auto_sync_wechat_tags"])),
        "sync_batch_size": coerce_sync_batch_size(raw.get("sync_batch_size", DEFAULT_SETTINGS["sync_batch_size"])),
        "sync_interval_minutes": coerce_sync_interval_minutes(raw.get("sync_interval_minutes", DEFAULT_SETTINGS["sync_interval_minutes"])),
        "scan_interval_seconds": coerce_scan_interval_seconds(raw.get("scan_interval_seconds", DEFAULT_SETTINGS["scan_interval_seconds"])),
    }


def state_path(base_dir: str | Path, wx_id: str) -> Path:
    return account_area_file(base_dir, wx_id, AREA_NAME, STATE_FILENAME, create_parent=True)


def default_state(wx_id: str) -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "wx_id": _clean_text(wx_id),
        "updated_at": "",
        "settings": dict(DEFAULT_SETTINGS),
        "runtime": {
            "last_auto_scan_at": "",
            "last_scan_at": "",
            "last_scan_mode": "",
            "last_scan_count": 0,
            "last_wechat_tag_sync_at": "",
            "full_scan_running": False,
            "stop_requested": False,
            "full_scan_progress": {},
        },
        "records": [],
        "events": [],
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
    runtime = state.get("runtime")
    if isinstance(runtime, dict):
        normalized["runtime"].update(copy.deepcopy(runtime))
    records = state.get("records")
    if isinstance(records, dict):
        records = list(records.values())
    normalized["records"] = [normalize_record(item) for item in (records or []) if isinstance(item, dict)]
    normalized["events"] = [dict(item) for item in (state.get("events") or []) if isinstance(item, dict)]
    return normalized


def normalize_record(record: dict[str, Any]) -> dict[str, Any]:
    status = _clean_text(record.get("status")) or STATUS_NORMAL
    if status not in {STATUS_BLOCKED, STATUS_DELETED, STATUS_NORMAL}:
        status = STATUS_NORMAL
    sync_status = _clean_text(record.get("wechat_sync_status")) or SYNC_SKIPPED
    if sync_status not in {SYNC_PENDING, SYNC_SYNCED, SYNC_SKIPPED}:
        sync_status = SYNC_PENDING
    try:
        sync_retry_count = int(record.get("wechat_sync_retry_count", 0) or 0)
    except (TypeError, ValueError):
        sync_retry_count = 0
    return {
        "name": _clean_text(record.get("name")),
        "status": status,
        "previous_status": _clean_text(record.get("previous_status")),
        "evidence": _clean_text(record.get("evidence")),
        "source": _clean_text(record.get("source")) or "session_preview",
        "first_seen_at": _clean_text(record.get("first_seen_at")),
        "last_seen_at": _clean_text(record.get("last_seen_at")),
        "changed_at": _clean_text(record.get("changed_at")),
        "contact_key": _clean_text(record.get("contact_key")),
        "contact_matched": bool(record.get("contact_matched", False)),
        "wechat_sync_status": sync_status,
        "wechat_sync_error": _clean_text(record.get("wechat_sync_error")),
        "wechat_synced_at": _clean_text(record.get("wechat_synced_at")),
        "wechat_sync_attempted_at": _clean_text(record.get("wechat_sync_attempted_at")),
        "wechat_sync_next_retry_at": _clean_text(record.get("wechat_sync_next_retry_at")),
        "wechat_sync_retry_count": max(0, sync_retry_count),
    }


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
    path.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    return state


def relationship_status_from_preview(content: Any) -> str:
    text = _clean_text(content)
    if "消息已发出，但被对方拒收了" in text:
        return STATUS_BLOCKED
    if "开启了朋友验证" in text and ("你还不是他" in text or "你还不是她" in text):
        return STATUS_DELETED
    return STATUS_NORMAL


def normalize_session_item(item: Any) -> dict[str, str]:
    if isinstance(item, dict):
        name = item.get("name") or item.get("nickname") or item.get("chat_name") or item.get("sender") or ""
        content = item.get("content") or item.get("last_msg") or item.get("message") or item.get("text") or ""
        time_text = item.get("time") or item.get("timestamp") or ""
        info = item.get("info") or ""
    else:
        name = getattr(item, "name", "") or getattr(item, "nickname", "") or getattr(item, "chat_name", "")
        content = getattr(item, "content", "") or getattr(item, "last_msg", "") or getattr(item, "message", "")
        time_text = getattr(item, "time", "") or getattr(item, "timestamp", "")
        info = getattr(item, "info", "")
    return {
        "name": _clean_text(name),
        "content": _clean_text(content),
        "time": _clean_text(time_text),
        "info": _clean_text(info),
    }


def normalize_session_items(items: Any) -> list[dict[str, str]]:
    if items is None:
        return []
    if not isinstance(items, (list, tuple)):
        items = [items]
    sessions = []
    seen = set()
    for item in items:
        session = normalize_session_item(item)
        name = session["name"]
        if not name or name in seen:
            continue
        seen.add(name)
        sessions.append(session)
    return sessions


def _record_map(state: dict[str, Any]) -> dict[str, dict[str, Any]]:
    records = {}
    for record in state.get("records") or []:
        name = _clean_text((record or {}).get("name"))
        if name:
            records[name] = normalize_record(record)
    return records


def _append_event(state: dict[str, Any], event_type: str, name: str, *, status: str = "", evidence: str = "", now: Any = None, error: str = "") -> None:
    events = state.setdefault("events", [])
    events.append({
        "at": _iso_timestamp(now),
        "type": _clean_text(event_type),
        "name": _clean_text(name),
        "status": _clean_text(status),
        "evidence": _clean_text(evidence),
        "error": _clean_text(error),
    })
    del events[:-1000]


def _relationship_event_type(status: str) -> str:
    if status == STATUS_BLOCKED:
        return EVENT_BLOCKED
    if status == STATUS_DELETED:
        return EVENT_DELETED
    return EVENT_RECOVERED


def update_state_from_sessions(
    state: dict[str, Any],
    sessions: list[dict[str, str]],
    *,
    now: Any = None,
    source: str = "session_preview",
) -> dict[str, Any]:
    state = normalize_state(state, wx_id=_clean_text((state or {}).get("wx_id")))
    timestamp = _iso_timestamp(now)
    records = _record_map(state)
    changed = False
    for session in sessions or []:
        name = _clean_text((session or {}).get("name"))
        if not name:
            continue
        evidence = _clean_text((session or {}).get("content"))
        status = relationship_status_from_preview(evidence)
        existing = records.get(name)
        if not existing and status == STATUS_NORMAL:
            continue
        previous_status = _clean_text((existing or {}).get("status")) or STATUS_NORMAL
        is_status_change = status != previous_status
        if not existing:
            existing = normalize_record({
                "name": name,
                "first_seen_at": timestamp,
                "status": status,
            })
        existing["previous_status"] = previous_status if is_status_change else _clean_text(existing.get("previous_status"))
        existing["status"] = status
        existing["evidence"] = evidence
        existing["source"] = _clean_text(source) or "session_preview"
        existing["first_seen_at"] = existing.get("first_seen_at") or timestamp
        existing["last_seen_at"] = timestamp
        if is_status_change or not existing.get("changed_at"):
            existing["changed_at"] = timestamp
        if is_status_change or existing.get("wechat_sync_status") not in {SYNC_PENDING, SYNC_SYNCED}:
            existing["wechat_sync_status"] = SYNC_PENDING
            existing["wechat_sync_error"] = ""
            existing["wechat_synced_at"] = ""
            existing["wechat_sync_attempted_at"] = ""
            existing["wechat_sync_next_retry_at"] = ""
            existing["wechat_sync_retry_count"] = 0
        if is_status_change:
            _append_event(state, _relationship_event_type(status), name, status=status, evidence=evidence, now=timestamp)
        records[name] = existing
        changed = True
    if changed:
        state["records"] = sorted(records.values(), key=lambda item: _clean_text(item.get("changed_at")), reverse=True)
        state["updated_at"] = timestamp
    return state


def _contact_match_values(contact: dict[str, Any]) -> set[str]:
    values = {
        _clean_text(contact.get("remark")),
        _clean_text(contact.get("nickname")),
        _clean_text(contact.get("display_name")),
        _clean_text(contact.get("send_name")),
        _clean_text(contact.get("wechat_id")),
    }
    return {value for value in values if value}


def _relationship_contact_match_map(contacts: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    by_key: dict[str, dict[str, Any]] = {}
    value_counts: dict[str, int] = {}
    value_contact: dict[str, dict[str, Any]] = {}
    for contact in contacts:
        if not isinstance(contact, dict):
            continue
        key = contact_identity_key(contact)
        if key and key not in by_key:
            by_key[key] = contact
        for value in _contact_match_values(contact):
            value_counts[value] = value_counts.get(value, 0) + 1
            value_contact[value] = contact
    unique_values = {
        value: contact
        for value, contact in value_contact.items()
        if value_counts.get(value, 0) == 1
    }
    return {"by_key": by_key, "unique_values": unique_values}


def _matched_contact_for_record(record: dict[str, Any], match_map: dict[str, dict[str, Any]]) -> dict[str, Any] | None:
    contact_key = _clean_text(record.get("contact_key"))
    if contact_key:
        contact = match_map["by_key"].get(contact_key)
        if contact is not None:
            return contact
        return None
    name = _clean_text(record.get("name"))
    if not name:
        return None
    return match_map["unique_values"].get(name)


def _relation_tags_for_status(status: str) -> tuple[list[str], list[str]]:
    if status == STATUS_BLOCKED:
        return [TAG_BLOCKED], [TAG_DELETED]
    if status == STATUS_DELETED:
        return [TAG_DELETED], [TAG_BLOCKED]
    return [], [TAG_BLOCKED, TAG_DELETED]


def _apply_tags(tags: Any, *, add_tags: list[str], remove_tags: list[str]) -> list[str]:
    values = [tag for tag in normalize_tag_list(tags) if tag not in set(remove_tags)]
    for tag in add_tags:
        if tag and tag not in values:
            values.append(tag)
    return values


def merge_state_into_contact_directory(directory: dict[str, Any], state: dict[str, Any], *, now: Any = None) -> tuple[dict[str, Any], dict[str, str]]:
    updated = copy.deepcopy(directory or {})
    records = [normalize_record(item) for item in (state or {}).get("records") or []]
    timestamp = _iso_timestamp(now)
    matched_names: dict[str, str] = {}
    changed = False
    contacts = [contact for contact in (updated.get("subjects") or []) if isinstance(contact, dict)]
    match_map = _relationship_contact_match_map(contacts)
    for matched_record in records:
        contact = _matched_contact_for_record(matched_record, match_map)
        if contact is None:
            continue
        status = matched_record["status"]
        add_tags, remove_tags = _relation_tags_for_status(status)
        next_tags = _apply_tags(contact.get("tags"), add_tags=add_tags, remove_tags=remove_tags)
        if next_tags != list(contact.get("tags") or []):
            contact["tags"] = next_tags
            contact["raw_tags"] = "，".join(next_tags)
            changed = True
        contact["relationship_status"] = status
        contact["relationship_evidence"] = matched_record["evidence"]
        contact["relationship_updated_at"] = timestamp
        key = contact_identity_key(contact)
        matched_names[matched_record["name"]] = key
    if changed or matched_names:
        updated["updated_at"] = timestamp
    return updated, matched_names


def attach_contact_matches(state: dict[str, Any], matched_names: dict[str, str]) -> dict[str, Any]:
    if not matched_names:
        return state
    for record in state.get("records") or []:
        name = _clean_text((record or {}).get("name"))
        if name in matched_names:
            record["contact_key"] = matched_names[name]
            record["contact_matched"] = True
    return state


def relationship_scan_summary(state: dict[str, Any], *, now: Any = None) -> dict[str, Any]:
    state = normalize_state(state, wx_id=_clean_text((state or {}).get("wx_id")))
    current = now if isinstance(now, datetime) else _parse_time(now) or datetime.now()
    today = current.date()
    counts = {
        "today_blocked": 0,
        "today_deleted": 0,
        "today_recovered": 0,
        "wechat_synced_today": 0,
        "wechat_pending": 0,
    }
    for event in state.get("events") or []:
        at = _parse_time((event or {}).get("at"))
        if not at or at.date() != today:
            continue
        event_type = _clean_text((event or {}).get("type"))
        if event_type == EVENT_BLOCKED:
            counts["today_blocked"] += 1
        elif event_type == EVENT_DELETED:
            counts["today_deleted"] += 1
        elif event_type == EVENT_RECOVERED:
            counts["today_recovered"] += 1
        elif event_type == EVENT_WECHAT_SYNCED:
            counts["wechat_synced_today"] += 1
    counts["wechat_pending"] = sum(
        1
        for record in state.get("records") or []
        if _clean_text((record or {}).get("wechat_sync_status")) == SYNC_PENDING
    )
    runtime = state.get("runtime") or {}
    full_scan_progress = runtime.get("full_scan_progress") if isinstance(runtime.get("full_scan_progress"), dict) else {}
    return {
        **counts,
        "auto_scan_enabled": bool((state.get("settings") or {}).get("auto_scan_enabled", True)),
        "last_scan_at": _clean_text(runtime.get("last_scan_at")),
        "last_scan_mode": _clean_text(runtime.get("last_scan_mode")),
        "last_scan_count": int(runtime.get("last_scan_count", 0) or 0),
        "full_scan_running": bool(runtime.get("full_scan_running", False)),
        "full_scan_progress": dict(full_scan_progress),
    }


def relationship_scan_payload(state: dict[str, Any]) -> dict[str, Any]:
    state = normalize_state(state, wx_id=_clean_text((state or {}).get("wx_id")))
    records = sorted(
        [normalize_record(item) for item in state.get("records") or []],
        key=lambda item: _clean_text(item.get("changed_at")) or _clean_text(item.get("last_seen_at")),
        reverse=True,
    )
    return {
        "wx_id": state.get("wx_id", ""),
        "settings": normalize_settings(state.get("settings")),
        "summary": relationship_scan_summary(state),
        "records": records,
    }


def clear_state(state: dict[str, Any]) -> dict[str, Any]:
    state = normalize_state(state, wx_id=_clean_text((state or {}).get("wx_id")))
    cleared = default_state(state.get("wx_id", ""))
    cleared["settings"] = normalize_settings(state.get("settings"))
    cleared["settings"]["auto_scan_enabled"] = False
    cleared["settings"]["auto_sync_wechat_tags"] = False
    return cleared


def _data_dir(bot) -> str:
    return str(getattr(getattr(bot, "config", None), "DATA_DIR", "") or getattr(bot, "DATA_DIR", "") or "data")


def _wx_id(bot) -> str:
    return _clean_text(getattr(bot, "wx_id", "") or getattr(bot, "current_account_wx_id", ""))


def _contact_directory_file(bot, wx_id: str):
    file_fn = getattr(bot, "_contact_profiles_directory_file", None)
    if callable(file_fn):
        directory_file, resolved_wx_id = file_fn()
        return directory_file, _clean_text(resolved_wx_id) or wx_id
    from core.contact_profiles import directory_path as contact_directory_path

    return contact_directory_path(_data_dir(bot), wx_id), wx_id


def _load_bot_state(bot) -> dict[str, Any]:
    wx_id = _wx_id(bot)
    if not wx_id:
        return default_state("")
    return load_state(_data_dir(bot), wx_id)


def _save_bot_state(bot, state: dict[str, Any]) -> dict[str, Any]:
    return save_state(_data_dir(bot), state)


def apply_state_to_local_contacts(bot, state: dict[str, Any]) -> dict[str, Any]:
    wx_id = _clean_text(state.get("wx_id")) or _wx_id(bot)
    if not wx_id:
        return state
    directory_file, wx_id = _contact_directory_file(bot, wx_id)
    directory = load_contact_directory(directory_file, wx_id=wx_id)
    updated, matched_names = merge_state_into_contact_directory(directory, state)
    if matched_names:
        save_contact_directory(directory_file, updated)
        state = attach_contact_matches(state, matched_names)
    return state


def _expected_wx_id(bot) -> str:
    return _clean_text(getattr(bot, "wx_id", "") or getattr(getattr(bot, "config", None), "current_account_wx_id", ""))


def _read_local_sessions(bot, *, limit: int = CLI_SESSION_SCAN_LIMIT) -> list[dict[str, str]] | None:
    local_result = read_local_sessions_with_status(
        limit=limit,
        expected_wx_id=_expected_wx_id(bot),
    )
    if local_result.ok:
        log(message=f"[关系扫描] 已从本地微信数据库读取会话 {len(local_result.items)} 个")
        return normalize_session_items(local_result.items)
    global _last_local_session_warning_at
    now = time.time()
    if now - _last_local_session_warning_at >= 300:
        _last_local_session_warning_at = now
        log(level="WARNING", message=f"[关系扫描] 本地微信数据库读取会话失败：{local_result.error}")
    return None


def _read_sessions(bot, *, prefer_local: bool = True) -> list[dict[str, str]]:
    if prefer_local:
        local_sessions = _read_local_sessions(bot)
        if local_sessions is not None:
            return local_sessions
    get_session = getattr(getattr(bot, "wx", None), "GetSession", None)
    if not callable(get_session):
        return []
    with warn_slow_wechat_ui_action("GetSession()"):
        return normalize_session_items(get_session())


def scan_current_sessions(bot, *, mode: str = "manual", acquire_lock: bool = True) -> dict[str, Any]:
    if not getattr(bot, "wx", None):
        raise RuntimeError("微信客户端未初始化")
    if not acquire_lock:
        sessions = _read_sessions(bot)
    else:
        sessions = _read_local_sessions(bot)
        if sessions is None:
            log(level="WARNING", message="[关系扫描] 手动扫描回退微信界面读取会话")
            lock = bot._get_wechat_action_lock()
            with lock:
                sessions = _read_sessions(bot, prefer_local=False)
    state = _load_bot_state(bot)
    state = update_state_from_sessions(state, sessions, source="session_preview")
    state = apply_state_to_local_contacts(bot, state)
    runtime = state.setdefault("runtime", {})
    runtime["last_scan_at"] = _iso_timestamp()
    runtime["last_scan_mode"] = _clean_text(mode) or "manual"
    runtime["last_scan_count"] = len(sessions)
    if mode == "auto":
        runtime["last_auto_scan_at"] = runtime["last_scan_at"]
    state = _save_bot_state(bot, state)
    return {"sessions": sessions, "state": state, "payload": relationship_scan_payload(state)}


def request_stop_full_scan(bot) -> dict[str, Any]:
    state = _load_bot_state(bot)
    state.setdefault("runtime", {})["stop_requested"] = True
    state = _save_bot_state(bot, state)
    return relationship_scan_payload(state)


def stop_requested(bot) -> bool:
    return bool((_load_bot_state(bot).get("runtime") or {}).get("stop_requested", False))


def _update_full_scan_progress(bot, **updates) -> dict[str, Any]:
    state = _load_bot_state(bot)
    runtime = state.setdefault("runtime", {})
    progress = runtime.get("full_scan_progress")
    if not isinstance(progress, dict):
        progress = {}
    progress.update({key: value for key, value in updates.items() if value is not None})
    runtime["full_scan_progress"] = progress
    _save_bot_state(bot, state)
    return progress


def _safe_session_box_go_top(bot) -> bool:
    session_box = getattr(getattr(bot, "wx", None), "SessionBox", None)
    go_top = getattr(session_box, "go_top", None)
    if not callable(go_top):
        return False
    try:
        go_top()
        time.sleep(FULL_SCAN_SCROLL_SETTLE_SECONDS)
        return True
    except Exception as exc:
        log(level="WARNING", message=f"[关系扫描] 消息列表回到顶部失败：{exc}")
        return False


def _flush_after_full_scan_lock_release(bot) -> None:
    flush_queue = getattr(bot, "_flush_lightweight_send_queue", None)
    if not callable(flush_queue):
        return
    try:
        flush_queue(limit=20)
    except Exception as exc:
        log(level="WARNING", message=f"[关系扫描] 分片释放锁后处理轻量发送队列失败：{exc}")


def scan_full_sessions(bot, *, max_scrolls: int = FULL_SCAN_MAX_SCROLLS, allow_running: bool = False) -> dict[str, Any]:
    if not getattr(bot, "wx", None):
        raise RuntimeError("微信客户端未初始化")
    state = _load_bot_state(bot)
    runtime = state.setdefault("runtime", {})
    if runtime.get("full_scan_running") and not allow_running:
        return {"sessions": [], "state": state, "payload": relationship_scan_payload(state), "already_running": True}
    runtime["full_scan_running"] = True
    runtime["stop_requested"] = False
    runtime["full_scan_progress"] = {
        "status": "running",
        "started_at": _iso_timestamp(),
        "updated_at": _iso_timestamp(),
        "scrolled_rounds": 0,
        "max_scrolls": max(1, int(max_scrolls or 1)),
        "unique_count": 0,
        "last_name": "",
        "message": "全量扫描已开始",
    }
    _save_bot_state(bot, state)

    local_sessions = _read_local_sessions(bot, limit=CLI_FULL_SESSION_SCAN_LIMIT)
    if local_sessions is not None:
        state = _load_bot_state(bot)
        state = update_state_from_sessions(state, local_sessions, source="session_preview_full")
        state = apply_state_to_local_contacts(bot, state)
        runtime = state.setdefault("runtime", {})
        runtime["full_scan_running"] = False
        runtime["stop_requested"] = False
        runtime["last_scan_at"] = _iso_timestamp()
        runtime["last_scan_mode"] = "full"
        runtime["last_scan_count"] = len(local_sessions)
        runtime["full_scan_progress"] = {
            "status": "completed",
            "started_at": _clean_text(((state.get("runtime") or {}).get("full_scan_progress") or {}).get("started_at")),
            "updated_at": _iso_timestamp(),
            "scrolled_rounds": 0,
            "max_scrolls": max(1, int(max_scrolls or 1)),
            "unique_count": len(local_sessions),
            "last_name": _clean_text((local_sessions[-1] or {}).get("name")) if local_sessions else "",
            "message": f"全量扫描完成，读取 {len(local_sessions)} 个会话",
        }
        state = _save_bot_state(bot, state)
        return {"sessions": local_sessions, "state": state, "payload": relationship_scan_payload(state)}

    sessions_by_name: dict[str, dict[str, str]] = {}
    stale_rounds = 0
    scrolled_rounds = 0
    started = False
    finished = False
    lock = bot._get_wechat_action_lock()
    try:
        log(level="WARNING", message="[关系扫描] 全量扫描回退微信界面滚动读取会话")
        max_rounds = max(1, int(max_scrolls or 1))
        slice_rounds = max(1, min(FULL_SCAN_LOCK_SLICE_SCROLLS, max_rounds))
        while not finished and scrolled_rounds < max_rounds:
            with lock:
                session_box = getattr(bot.wx, "SessionBox", None)
                roll_down = getattr(session_box, "roll_down", None)
                if not started:
                    _safe_session_box_go_top(bot)
                    started = True
                for _index in range(min(slice_rounds, max_rounds - scrolled_rounds)):
                    if stop_requested(bot):
                        finished = True
                        break
                    batch = _read_sessions(bot, prefer_local=False)
                    before_count = len(sessions_by_name)
                    for session in batch:
                        name = session["name"]
                        if name and name not in sessions_by_name:
                            sessions_by_name[name] = session
                    last_name = _clean_text((batch[-1] or {}).get("name")) if batch else ""
                    _update_full_scan_progress(
                        bot,
                        status="running",
                        updated_at=_iso_timestamp(),
                        scrolled_rounds=scrolled_rounds,
                        max_scrolls=max_rounds,
                        unique_count=len(sessions_by_name),
                        last_name=last_name,
                        message=f"已读取 {len(sessions_by_name)} 个会话",
                    )
                    if len(sessions_by_name) == before_count:
                        stale_rounds += 1
                    else:
                        stale_rounds = 0
                    if stale_rounds >= FULL_SCAN_STALE_ROUNDS:
                        finished = True
                        break
                    if not callable(roll_down):
                        finished = True
                        break
                    roll_down()
                    scrolled_rounds += 1
                    time.sleep(FULL_SCAN_SCROLL_SETTLE_SECONDS)
                if scrolled_rounds >= max_rounds:
                    finished = True
            if not finished:
                time.sleep(FULL_SCAN_LOCK_RELEASE_SETTLE_SECONDS)
                _flush_after_full_scan_lock_release(bot)
        with lock:
            _safe_session_box_go_top(bot)
    finally:
        state = _load_bot_state(bot)
        runtime = state.setdefault("runtime", {})
        runtime["full_scan_running"] = False
        runtime["stop_requested"] = False
        progress = runtime.get("full_scan_progress")
        if not isinstance(progress, dict):
            progress = {}
        progress.update({
            "status": "saving",
            "updated_at": _iso_timestamp(),
            "scrolled_rounds": scrolled_rounds,
            "max_scrolls": max(1, int(max_scrolls or 1)),
            "unique_count": len(sessions_by_name),
            "message": "全量扫描结果保存中",
        })
        runtime["full_scan_progress"] = progress
        _save_bot_state(bot, state)

    sessions = list(sessions_by_name.values())
    state = _load_bot_state(bot)
    state = update_state_from_sessions(state, sessions, source="session_preview_full")
    state = apply_state_to_local_contacts(bot, state)
    runtime = state.setdefault("runtime", {})
    runtime["last_scan_at"] = _iso_timestamp()
    runtime["last_scan_mode"] = "full"
    runtime["last_scan_count"] = len(sessions)
    runtime["full_scan_progress"] = {
        "status": "completed",
        "started_at": _clean_text(((state.get("runtime") or {}).get("full_scan_progress") or {}).get("started_at")),
        "updated_at": _iso_timestamp(),
        "scrolled_rounds": scrolled_rounds,
        "max_scrolls": max(1, int(max_scrolls or 1)),
        "unique_count": len(sessions),
        "last_name": _clean_text((sessions[-1] or {}).get("name")) if sessions else "",
        "message": f"全量扫描完成，读取 {len(sessions)} 个会话",
    }
    state = _save_bot_state(bot, state)
    return {"sessions": sessions, "state": state, "payload": relationship_scan_payload(state)}


def due_for_auto_scan(state: dict[str, Any], *, now: Any = None) -> bool:
    settings = normalize_settings((state or {}).get("settings"))
    if not settings["auto_scan_enabled"]:
        return False
    return _due_for_auto_scan_interval(
        state,
        settings["scan_interval_seconds"],
        now=now,
    )


def _due_for_auto_scan_interval(state: dict[str, Any], interval_seconds: int, *, now: Any = None, timestamp_field: str = "last_auto_scan_at") -> bool:
    runtime = (state or {}).get("runtime") or {}
    last_scan = _parse_time(runtime.get(timestamp_field))
    if not last_scan:
        return True
    current = now if isinstance(now, datetime) else _parse_time(now) or datetime.now()
    return current - last_scan >= timedelta(seconds=max(1, int(interval_seconds or 1)))


def check_auto_scan(bot, *, now: Any = None) -> bool:
    if not getattr(bot, "wx", None):
        return False
    state = _load_bot_state(bot)
    settings = normalize_settings(state.get("settings"))
    if not settings["auto_scan_enabled"]:
        process_pending_wechat_tag_sync(bot, now=now)
        return False

    cli_due = _due_for_auto_scan_interval(
        state,
        CLI_AUTO_SCAN_INTERVAL_SECONDS,
        now=now,
        timestamp_field="last_cli_auto_scan_at",
    )
    if not cli_due:
        process_pending_wechat_tag_sync(bot, now=now)
        return False

    sessions = _read_local_sessions(bot, limit=CLI_SESSION_SCAN_LIMIT)
    if sessions is None:
        log(level="WARNING", message="[关系扫描] 自动扫描未使用微信界面回退，本轮跳过")
        process_pending_wechat_tag_sync(bot, now=now)
        return False
    state = update_state_from_sessions(state, sessions, source="session_preview")
    state = apply_state_to_local_contacts(bot, state)
    runtime = state.setdefault("runtime", {})
    runtime["last_auto_scan_at"] = _iso_timestamp(now)
    runtime["last_auto_scan_source"] = "wechat_cli"
    runtime["last_cli_auto_scan_at"] = runtime["last_auto_scan_at"]
    runtime["last_scan_at"] = runtime["last_auto_scan_at"]
    runtime["last_scan_mode"] = "auto"
    runtime["last_scan_count"] = len(sessions)
    _save_bot_state(bot, state)
    process_pending_wechat_tag_sync(bot, now=now)
    return True


def pending_sync_records(state: dict[str, Any], *, now: Any = None) -> list[dict[str, Any]]:
    current = now if isinstance(now, datetime) else _parse_time(now) or datetime.now()
    pending: list[tuple[int, dict[str, Any]]] = []
    for index, item in enumerate((state or {}).get("records") or []):
        record = normalize_record(item)
        if _clean_text(record.get("wechat_sync_status")) != SYNC_PENDING:
            continue
        next_retry = _parse_time(record.get("wechat_sync_next_retry_at"))
        if next_retry and next_retry > current:
            continue
        pending.append((index, record))
    return [
        record
        for _index, record in sorted(
            pending,
            key=lambda pair: (
                1 if _clean_text(pair[1].get("wechat_sync_attempted_at")) else 0,
                _clean_text(pair[1].get("wechat_sync_attempted_at")),
                pair[0],
            ),
        )
    ]


def due_for_wechat_tag_sync(state: dict[str, Any], *, now: Any = None) -> bool:
    settings = normalize_settings((state or {}).get("settings"))
    if not settings["auto_sync_wechat_tags"]:
        return False
    runtime = (state or {}).get("runtime") or {}
    last_sync = _parse_time(runtime.get("last_wechat_tag_sync_at"))
    if not last_sync:
        return True
    current = now if isinstance(now, datetime) else _parse_time(now) or datetime.now()
    return current - last_sync >= timedelta(minutes=settings["sync_interval_minutes"])


def desired_wechat_tag_update(status: str) -> tuple[list[str], list[str]]:
    return _relation_tags_for_status(status)


def _sync_record_still_current(state: dict[str, Any], name: str, status: str) -> bool:
    if not normalize_settings((state or {}).get("settings"))["auto_sync_wechat_tags"]:
        return False
    record = _record_map(state).get(_clean_text(name))
    return bool(
        record
        and _clean_text(record.get("wechat_sync_status")) == SYNC_PENDING
        and _clean_text(record.get("status")) == _clean_text(status)
    )


def _merge_new_events(target_state: dict[str, Any], source_state: dict[str, Any]) -> None:
    target_events = target_state.setdefault("events", [])
    existing = {
        (
            _clean_text((event or {}).get("at")),
            _clean_text((event or {}).get("type")),
            _clean_text((event or {}).get("name")),
            _clean_text((event or {}).get("status")),
            _clean_text((event or {}).get("evidence")),
            _clean_text((event or {}).get("error")),
        )
        for event in target_events
        if isinstance(event, dict)
    }
    for event in (source_state or {}).get("events") or []:
        if not isinstance(event, dict):
            continue
        key = (
            _clean_text(event.get("at")),
            _clean_text(event.get("type")),
            _clean_text(event.get("name")),
            _clean_text(event.get("status")),
            _clean_text(event.get("evidence")),
            _clean_text(event.get("error")),
        )
        if key in existing:
            continue
        target_events.append(dict(event))
        existing.add(key)
    del target_events[:-1000]


def _save_tag_sync_record_if_current(
    bot,
    source_state: dict[str, Any],
    *,
    name: str,
    expected_status: str,
    now: Any = None,
) -> tuple[bool, dict[str, Any], dict[str, dict[str, Any]]]:
    latest_state = _load_bot_state(bot)
    if not _sync_record_still_current(latest_state, name, expected_status):
        return False, latest_state, _record_map(latest_state)
    source_records = _record_map(source_state)
    updated_record = source_records.get(_clean_text(name))
    if not updated_record:
        return False, latest_state, _record_map(latest_state)
    latest_records = _record_map(latest_state)
    latest_records[_clean_text(name)] = updated_record
    latest_state["records"] = sorted(latest_records.values(), key=lambda item: _clean_text(item.get("changed_at")), reverse=True)
    latest_state.setdefault("runtime", {})["last_wechat_tag_sync_at"] = _iso_timestamp(now)
    _merge_new_events(latest_state, source_state)
    saved_state = _save_bot_state(bot, latest_state)
    return True, saved_state, _record_map(saved_state)


def process_pending_wechat_tag_sync(bot, *, limit: int | None = None, now: Any = None, force: bool = False) -> dict[str, Any]:
    if not getattr(bot, "wx", None):
        return {"processed": 0, "success": 0, "failed": 0}
    state = _load_bot_state(bot)
    settings = normalize_settings(state.get("settings"))
    if not settings["auto_sync_wechat_tags"]:
        return {"processed": 0, "success": 0, "failed": 0}
    current_time = now if isinstance(now, datetime) else _parse_time(now) or datetime.now()
    if not force and not due_for_wechat_tag_sync(state, now=current_time):
        return {"processed": 0, "success": 0, "failed": 0}
    batch_limit = coerce_sync_batch_size(limit if limit is not None else settings["sync_batch_size"])
    records = pending_sync_records(state, now=current_time)[:batch_limit]
    if not records:
        return {"processed": 0, "success": 0, "failed": 0}
    lock = bot._get_wechat_action_lock()
    if not lock.acquire(blocking=False):
        return {"processed": 0, "success": 0, "failed": 0}
    processed = success = failed = 0
    try:
        state_records = _record_map(state)
        for record in records:
            name = record["name"]
            latest_state = _load_bot_state(bot)
            latest_settings = normalize_settings(latest_state.get("settings"))
            if not latest_settings["auto_sync_wechat_tags"]:
                state = latest_state
                state_records = _record_map(state)
                log(message="[关系扫描] 微信标签同步已关闭，停止处理待同步队列")
                break
            latest_records = _record_map(latest_state)
            latest_record = latest_records.get(name)
            if (
                not latest_record
                or _clean_text(latest_record.get("wechat_sync_status")) != SYNC_PENDING
                or _clean_text(latest_record.get("status")) != _clean_text(record.get("status"))
            ):
                continue
            state = latest_state
            settings = latest_settings
            state_records = latest_records
            record = latest_record
            add_tags, remove_tags = desired_wechat_tag_update(record["status"])
            attempt_at = _iso_timestamp(current_time)
            try:
                result = modify_friend_tags_via_chat_profile(
                    bot,
                    [{"name": name}],
                    add_tags=add_tags,
                    remove_tags=remove_tags,
                    log_prefix="[关系扫描]",
                )
                post_state = _load_bot_state(bot)
                post_settings = normalize_settings(post_state.get("settings"))
                post_records = _record_map(post_state)
                post_record = post_records.get(name)
                if not post_settings["auto_sync_wechat_tags"]:
                    state = post_state
                    state_records = post_records
                    log(message="[关系扫描] 微信标签同步已关闭，放弃写回本轮剩余同步结果")
                    break
                if (
                    not post_record
                    or _clean_text(post_record.get("wechat_sync_status")) != SYNC_PENDING
                    or _clean_text(post_record.get("status")) != _clean_text(record.get("status"))
                ):
                    state = post_state
                    state_records = post_records
                    continue
                state = post_state
                settings = post_settings
                state_records = post_records
                current_record = post_record
                processed += 1
                current_record["wechat_sync_attempted_at"] = attempt_at
                current_record["wechat_sync_retry_count"] = int(current_record.get("wechat_sync_retry_count", 0) or 0) + 1
                if result.get("status") == "success":
                    current_record["wechat_sync_status"] = SYNC_SYNCED
                    current_record["wechat_sync_error"] = ""
                    current_record["wechat_sync_next_retry_at"] = ""
                    current_record["wechat_synced_at"] = _iso_timestamp()
                    success += 1
                    _append_event(state, EVENT_WECHAT_SYNCED, name, status=current_record.get("status", ""), now=current_record["wechat_synced_at"])
                else:
                    current_record["wechat_sync_status"] = SYNC_PENDING
                    current_record["wechat_sync_error"] = result.get("message") or "微信标签同步失败"
                    current_record["wechat_sync_next_retry_at"] = (current_time + timedelta(minutes=settings["sync_interval_minutes"])).replace(microsecond=0).isoformat()
                    failed += 1
                    _append_event(state, EVENT_WECHAT_SYNC_FAILED, name, status=current_record.get("status", ""), now=_iso_timestamp(), error=current_record["wechat_sync_error"])
                saved, state, state_records = _save_tag_sync_record_if_current(
                    bot,
                    state,
                    name=name,
                    expected_status=record.get("status", ""),
                    now=current_time,
                )
                if not saved:
                    log(message="[关系扫描] 微信标签同步状态已变化，放弃写回本轮结果")
                    break
            except Exception as exc:
                post_state = _load_bot_state(bot)
                post_settings = normalize_settings(post_state.get("settings"))
                post_records = _record_map(post_state)
                post_record = post_records.get(name)
                if not post_settings["auto_sync_wechat_tags"]:
                    state = post_state
                    state_records = post_records
                    log(message="[关系扫描] 微信标签同步已关闭，放弃写回本轮失败结果")
                    break
                if (
                    not post_record
                    or _clean_text(post_record.get("wechat_sync_status")) != SYNC_PENDING
                    or _clean_text(post_record.get("status")) != _clean_text(record.get("status"))
                ):
                    state = post_state
                    state_records = post_records
                    continue
                state = post_state
                settings = post_settings
                state_records = post_records
                processed += 1
                failed += 1
                failed_record = post_record
                failed_record["wechat_sync_status"] = SYNC_PENDING
                failed_record["wechat_sync_error"] = str(exc)
                failed_record["wechat_sync_attempted_at"] = attempt_at
                failed_record["wechat_sync_retry_count"] = int(failed_record.get("wechat_sync_retry_count", 0) or 0) + 1
                failed_record["wechat_sync_next_retry_at"] = (current_time + timedelta(minutes=settings["sync_interval_minutes"])).replace(microsecond=0).isoformat()
                _append_event(state, EVENT_WECHAT_SYNC_FAILED, name, status=record.get("status", ""), now=_iso_timestamp(), error=str(exc))
                saved, state, state_records = _save_tag_sync_record_if_current(
                    bot,
                    state,
                    name=name,
                    expected_status=record.get("status", ""),
                    now=current_time,
                )
                if not saved:
                    log(message="[关系扫描] 微信标签同步状态已变化，放弃写回本轮失败结果")
                    break
        if processed:
            log(message=f"[关系扫描] 微信标签同步完成：成功 {success}，失败 {failed}")
        return {"processed": processed, "success": success, "failed": failed}
    finally:
        lock.release()
