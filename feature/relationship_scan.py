"""Relationship scan rules and runtime helpers."""

from __future__ import annotations

import copy
import json
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

EVENT_BLOCKED = "blocked"
EVENT_DELETED = "deleted"
EVENT_RECOVERED = "recovered"
EVENT_WECHAT_SYNCED = "wechat_synced"
EVENT_WECHAT_SYNC_FAILED = "wechat_sync_failed"

DEFAULT_SETTINGS = {
    "auto_scan_enabled": True,
    "auto_write_contact_directory": True,
    "auto_sync_wechat_tags": True,
    "sync_batch_size": 5,
    "scan_interval_seconds": 10,
}


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


def normalize_settings(settings: Any) -> dict[str, Any]:
    raw = dict(settings or {}) if isinstance(settings, dict) else {}
    return {
        "auto_scan_enabled": bool(raw.get("auto_scan_enabled", DEFAULT_SETTINGS["auto_scan_enabled"])),
        "auto_write_contact_directory": bool(raw.get("auto_write_contact_directory", DEFAULT_SETTINGS["auto_write_contact_directory"])),
        "auto_sync_wechat_tags": bool(raw.get("auto_sync_wechat_tags", DEFAULT_SETTINGS["auto_sync_wechat_tags"])),
        "sync_batch_size": coerce_sync_batch_size(raw.get("sync_batch_size", DEFAULT_SETTINGS["sync_batch_size"])),
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
            "full_scan_running": False,
            "stop_requested": False,
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
        if is_status_change or existing.get("wechat_sync_status") != SYNC_SYNCED:
            existing["wechat_sync_status"] = SYNC_PENDING
            existing["wechat_sync_error"] = ""
            existing["wechat_synced_at"] = ""
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
    for contact in updated.get("subjects") or []:
        if not isinstance(contact, dict):
            continue
        values = _contact_match_values(contact)
        if not values:
            continue
        matched_record = next((record for record in records if record["name"] in values), None)
        if not matched_record:
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
    return {
        **counts,
        "auto_scan_enabled": bool((state.get("settings") or {}).get("auto_scan_enabled", True)),
        "last_scan_at": _clean_text(runtime.get("last_scan_at")),
        "last_scan_mode": _clean_text(runtime.get("last_scan_mode")),
        "last_scan_count": int(runtime.get("last_scan_count", 0) or 0),
        "full_scan_running": bool(runtime.get("full_scan_running", False)),
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
    settings = normalize_settings(state.get("settings"))
    if not settings["auto_write_contact_directory"]:
        return state
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


def _read_sessions(bot) -> list[dict[str, str]]:
    get_session = getattr(getattr(bot, "wx", None), "GetSession", None)
    if not callable(get_session):
        return []
    with warn_slow_wechat_ui_action("GetSession()"):
        return normalize_session_items(get_session())


def scan_current_sessions(bot, *, mode: str = "manual", acquire_lock: bool = True) -> dict[str, Any]:
    if not getattr(bot, "wx", None):
        raise RuntimeError("微信客户端未初始化")
    lock = bot._get_wechat_action_lock()
    if acquire_lock:
        with lock:
            sessions = _read_sessions(bot)
    else:
        sessions = _read_sessions(bot)
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


def scan_full_sessions(bot, *, max_scrolls: int = 80) -> dict[str, Any]:
    if not getattr(bot, "wx", None):
        raise RuntimeError("微信客户端未初始化")
    state = _load_bot_state(bot)
    runtime = state.setdefault("runtime", {})
    runtime["full_scan_running"] = True
    runtime["stop_requested"] = False
    _save_bot_state(bot, state)

    sessions_by_name: dict[str, dict[str, str]] = {}
    stale_rounds = 0
    lock = bot._get_wechat_action_lock()
    try:
        with lock:
            session_box = getattr(bot.wx, "SessionBox", None)
            go_top = getattr(session_box, "go_top", None)
            roll_down = getattr(session_box, "roll_down", None)
            if callable(go_top):
                go_top()
            for _index in range(max(1, int(max_scrolls or 1))):
                if stop_requested(bot):
                    break
                batch = _read_sessions(bot)
                before_count = len(sessions_by_name)
                for session in batch:
                    name = session["name"]
                    if name and name not in sessions_by_name:
                        sessions_by_name[name] = session
                if len(sessions_by_name) == before_count:
                    stale_rounds += 1
                else:
                    stale_rounds = 0
                if stale_rounds >= 3:
                    break
                if not callable(roll_down):
                    break
                roll_down()
    finally:
        state = _load_bot_state(bot)
        runtime = state.setdefault("runtime", {})
        runtime["full_scan_running"] = False
        runtime["stop_requested"] = False
        _save_bot_state(bot, state)

    sessions = list(sessions_by_name.values())
    state = _load_bot_state(bot)
    state = update_state_from_sessions(state, sessions, source="session_preview_full")
    state = apply_state_to_local_contacts(bot, state)
    runtime = state.setdefault("runtime", {})
    runtime["last_scan_at"] = _iso_timestamp()
    runtime["last_scan_mode"] = "full"
    runtime["last_scan_count"] = len(sessions)
    state = _save_bot_state(bot, state)
    return {"sessions": sessions, "state": state, "payload": relationship_scan_payload(state)}


def due_for_auto_scan(state: dict[str, Any], *, now: Any = None) -> bool:
    settings = normalize_settings((state or {}).get("settings"))
    if not settings["auto_scan_enabled"]:
        return False
    runtime = (state or {}).get("runtime") or {}
    last_scan = _parse_time(runtime.get("last_auto_scan_at"))
    if not last_scan:
        return True
    current = now if isinstance(now, datetime) else _parse_time(now) or datetime.now()
    return current - last_scan >= timedelta(seconds=settings["scan_interval_seconds"])


def check_auto_scan(bot, *, now: Any = None) -> bool:
    if not getattr(bot, "wx", None):
        return False
    state = _load_bot_state(bot)
    if not due_for_auto_scan(state, now=now):
        process_pending_wechat_tag_sync(bot)
        return False
    lock = bot._get_wechat_action_lock()
    if not lock.acquire(blocking=False):
        return False
    try:
        sessions = _read_sessions(bot)
    finally:
        lock.release()
    state = update_state_from_sessions(state, sessions, source="session_preview")
    state = apply_state_to_local_contacts(bot, state)
    runtime = state.setdefault("runtime", {})
    runtime["last_auto_scan_at"] = _iso_timestamp(now)
    runtime["last_scan_at"] = runtime["last_auto_scan_at"]
    runtime["last_scan_mode"] = "auto"
    runtime["last_scan_count"] = len(sessions)
    _save_bot_state(bot, state)
    process_pending_wechat_tag_sync(bot)
    return True


def pending_sync_records(state: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        normalize_record(item)
        for item in (state or {}).get("records") or []
        if _clean_text((item or {}).get("wechat_sync_status")) == SYNC_PENDING
    ]


def desired_wechat_tag_update(status: str) -> tuple[list[str], list[str]]:
    return _relation_tags_for_status(status)


def process_pending_wechat_tag_sync(bot, *, limit: int | None = None) -> dict[str, Any]:
    if not getattr(bot, "wx", None):
        return {"processed": 0, "success": 0, "failed": 0}
    state = _load_bot_state(bot)
    settings = normalize_settings(state.get("settings"))
    if not settings["auto_sync_wechat_tags"]:
        return {"processed": 0, "success": 0, "failed": 0}
    batch_limit = coerce_sync_batch_size(limit if limit is not None else settings["sync_batch_size"])
    records = pending_sync_records(state)[:batch_limit]
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
            add_tags, remove_tags = desired_wechat_tag_update(record["status"])
            try:
                result = modify_friend_tags_via_chat_profile(
                    bot,
                    [{"name": name}],
                    add_tags=add_tags,
                    remove_tags=remove_tags,
                    log_prefix="[关系扫描]",
                )
                processed += 1
                current = state_records.get(name)
                if result.get("status") == "success" and current:
                    current["wechat_sync_status"] = SYNC_SYNCED
                    current["wechat_sync_error"] = ""
                    current["wechat_synced_at"] = _iso_timestamp()
                    success += 1
                    _append_event(state, EVENT_WECHAT_SYNCED, name, status=current.get("status", ""), now=current["wechat_synced_at"])
                elif current:
                    current["wechat_sync_status"] = SYNC_PENDING
                    current["wechat_sync_error"] = result.get("message") or "微信标签同步失败"
                    failed += 1
                    _append_event(state, EVENT_WECHAT_SYNC_FAILED, name, status=current.get("status", ""), now=_iso_timestamp(), error=current["wechat_sync_error"])
            except Exception as exc:
                processed += 1
                failed += 1
                current = state_records.get(name)
                if current:
                    current["wechat_sync_status"] = SYNC_PENDING
                    current["wechat_sync_error"] = str(exc)
                _append_event(state, EVENT_WECHAT_SYNC_FAILED, name, status=record.get("status", ""), now=_iso_timestamp(), error=str(exc))
        state["records"] = sorted(state_records.values(), key=lambda item: _clean_text(item.get("changed_at")), reverse=True)
        _save_bot_state(bot, state)
        if processed:
            log(message=f"[关系扫描] 微信标签同步完成：成功 {success}，失败 {failed}")
        return {"processed": processed, "success": success, "failed": failed}
    finally:
        lock.release()
