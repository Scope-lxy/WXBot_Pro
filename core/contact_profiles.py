"""Offline contact directory rules for material outreach.

This module is intentionally pure data logic. It must not import wxauto or touch
the WeChat UI; wxbot_core.py owns execution against the client.
"""

from __future__ import annotations

import copy
import hashlib
import json
import re
import unicodedata
from datetime import datetime
from pathlib import Path
from typing import Any

from core.account_storage import account_area_dir
from core.contact_identity import (
    default_calibration_state as default_contact_calibration_state,
    dismiss_pending as dismiss_contact_identity_pending,
    list_chat_memory_names,
    list_memory_chat_names,
    normalize_calibration_state as normalize_contact_calibration_state,
    reconcile_contact_storage as reconcile_contact_storage_names,
    update_calibration_from_directory as update_contact_identity_from_directory,
)


SCHEMA_VERSION = 1
DIRECTORY_FILENAME = "contacts.json"

WARNING_DUPLICATE_SEND_NAME = "duplicate_send_name"
WARNING_SEND_NAME_UNSEARCHABLE = "send_name_unsearchable"
WARNING_WXID_CONFLICT = "wxid_conflict"

_TAG_SPLIT_RE = re.compile(r"[,，、;；/|｜\n\r\t]+")
_SAFE_NAME_RE = re.compile(r"[^0-9A-Za-z_.-]+")


def _iso_timestamp(now: Any = None) -> str:
    if isinstance(now, str):
        return now
    if isinstance(now, datetime):
        return now.replace(microsecond=0).isoformat()
    return datetime.now().replace(microsecond=0).isoformat()


def _clean_text(value: Any) -> str:
    if value is None:
        return ""
    return str(value).strip()


def _first_text(mapping: dict[str, Any], keys: tuple[str, ...]) -> str:
    for key in keys:
        if key in mapping:
            value = _clean_text(mapping.get(key))
            if value:
                return value
    return ""


def _first_value(mapping: dict[str, Any], keys: tuple[str, ...]) -> Any:
    for key in keys:
        if key in mapping and mapping.get(key) is not None:
            return mapping.get(key)
    return None


def _short_hash(value: str, length: int = 12) -> str:
    return hashlib.sha1(value.encode("utf-8")).hexdigest()[:length]


def _without_warning(warnings: list[str], warning: str) -> list[str]:
    return [item for item in warnings if item != warning]


def _append_warning(warnings: list[str], warning: str) -> list[str]:
    if warning not in warnings:
        warnings.append(warning)
    return warnings


def _coerce_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, (list, tuple, set)):
        return [_clean_text(item) for item in value if _clean_text(item)]
    text = _clean_text(value)
    if not text:
        return []
    return [_clean_text(item) for item in _TAG_SPLIT_RE.split(text) if _clean_text(item)]


def normalize_tag_list(raw_tags: Any) -> list[str]:
    """Normalize wxautox4 tag output to a deduplicated list."""
    normalized: list[str] = []
    seen: set[str] = set()
    for tag in _coerce_list(raw_tags):
        if tag not in seen:
            normalized.append(tag)
            seen.add(tag)
    return normalized


def is_default_wechat_id(value: Any) -> bool:
    return _clean_text(value).lower().startswith("wxid_")


def _looks_like_history_target(value: Any) -> bool:
    text = _clean_text(value)
    return bool(
        text == "filehelper"
        or text.startswith("wxid_")
        or text.endswith("@chatroom")
    )


def is_searchable_send_name(value: Any) -> bool:
    """Return whether a name has at least one searchable letter or number."""
    text = _clean_text(value)
    if not text:
        return False
    for char in text:
        category = unicodedata.category(char)
        if category[0] in {"L", "N"}:
            return True
    return False


def _local_contact_key(kind: str, value: str) -> str:
    return f"{kind}:{_short_hash(value)}"


def _build_contact_key(wechat_id: str, remark: str, nickname: str, raw_detail: dict[str, Any]) -> tuple[str, str]:
    if wechat_id:
        confidence = "medium" if is_default_wechat_id(wechat_id) else "high"
        return f"wechat_id:{wechat_id}", confidence
    if remark:
        return _local_contact_key("remark", remark), "low"
    if nickname:
        return _local_contact_key("nickname", nickname), "low"
    raw_fingerprint = json.dumps(raw_detail, ensure_ascii=False, sort_keys=True)
    return _local_contact_key("unknown", raw_fingerprint), "low"


def _choose_send_name(wechat_id: str, remark: str, nickname: str) -> tuple[str, str]:
    if remark:
        return remark, "remark"
    if nickname:
        return nickname, "nickname"
    if wechat_id and not is_default_wechat_id(wechat_id):
        return wechat_id, "wechat_id"
    return "", "none"


def normalize_friend_detail(
    raw_detail: dict[str, Any],
    *,
    existing: dict[str, Any] | None = None,
    now: Any = None,
) -> dict[str, Any]:
    """Convert one wxautox4 friend detail dict to a local contact subject."""
    raw_detail = dict(raw_detail or {})
    existing = existing or {}

    nickname = _first_text(raw_detail, ("昵称", "nickname", "NickName", "name", "Name"))
    remark = _first_text(raw_detail, ("备注", "remark", "Remark", "alias", "Alias"))
    wechat_id = _first_text(raw_detail, ("微信号", "微信ID", "wechat_id", "wxid", "wx_id", "WeChatId"))
    wxid = _first_text(raw_detail, ("wxid", "wx_id", "username", "UserName"))
    if not wxid and is_default_wechat_id(wechat_id):
        wxid = wechat_id
    raw_tags = _first_value(raw_detail, ("标签", "tags", "Tags", "tag", "Tag"))
    tags = normalize_tag_list(raw_tags)
    send_name, send_name_source = _choose_send_name(wechat_id, remark, nickname)
    display_name = remark or nickname or send_name or wechat_id
    contact_key, confidence = _build_contact_key(wechat_id, remark, nickname, raw_detail)

    warnings = list(existing.get("warnings") or [])
    warnings = _without_warning(warnings, WARNING_SEND_NAME_UNSEARCHABLE)
    warnings = _without_warning(warnings, WARNING_DUPLICATE_SEND_NAME)
    if not is_searchable_send_name(send_name):
        _append_warning(warnings, WARNING_SEND_NAME_UNSEARCHABLE)

    contact = {
        "subject_type": "friend",
        "contact_key": contact_key,
        "identity_confidence": confidence,
        "wechat_id": wechat_id,
        "wxid": wxid,
        "nickname": nickname,
        "remark": remark,
        "display_name": display_name,
        "send_name": send_name,
        "send_name_source": send_name_source,
        "tags": tags,
        "raw_tags": raw_tags if raw_tags is not None else "",
        "status": "active",
        "warnings": warnings,
        "stable_suffix": existing.get("stable_suffix") or "",
        "last_seen_at": _iso_timestamp(now),
        "raw_detail": raw_detail,
    }
    contact["stable_suffix"] = stable_suffix_for_contact(contact)
    return contact


def contact_history_target(contact: dict[str, Any]) -> str:
    if not isinstance(contact, dict):
        return ""
    raw_detail = contact.get("raw_detail") if isinstance(contact.get("raw_detail"), dict) else {}
    for value in (
        contact.get("wxid"),
        raw_detail.get("wxid"),
        raw_detail.get("wx_id"),
        raw_detail.get("username"),
        contact.get("wechat_id"),
    ):
        text = _clean_text(value)
        if _looks_like_history_target(text):
            return text
    return ""


def _cli_contact_basics_values(raw_detail: dict[str, Any]) -> dict[str, str]:
    raw_detail = raw_detail or {}
    username = _first_text(raw_detail, ("wxid", "wx_id", "username", "UserName"))
    nickname = _first_text(raw_detail, ("昵称", "nickname", "nick_name", "NickName", "name", "Name"))
    remark = _first_text(raw_detail, ("备注", "remark", "Remark"))
    alias = _first_text(raw_detail, ("alias", "Alias"))
    wechat_id = _first_text(raw_detail, ("微信号", "微信ID", "wechat_id", "WeChatId"))
    if not wechat_id or is_default_wechat_id(wechat_id):
        wechat_id = alias or username or wechat_id
    return {
        "wxid": username,
        "wechat_id": wechat_id,
        "nickname": nickname,
        "remark": remark,
    }


def _merge_cli_raw_detail(existing_raw: dict[str, Any], raw_detail: dict[str, Any], values: dict[str, str]) -> dict[str, Any]:
    merged = copy.deepcopy(existing_raw) if isinstance(existing_raw, dict) else {}
    if values["wxid"]:
        merged["wxid"] = values["wxid"]
        merged["username"] = values["wxid"]
    if values["wechat_id"]:
        merged["微信号"] = values["wechat_id"]
        merged["wechat_id"] = values["wechat_id"]
    if values["nickname"]:
        merged["昵称"] = values["nickname"]
        merged["nickname"] = values["nickname"]
    if values["remark"]:
        merged["备注"] = values["remark"]
        merged["remark"] = values["remark"]
    for key in ("alias", "description", "avatar", "verify_flag", "local_type"):
        if key in raw_detail and raw_detail.get(key) not in (None, ""):
            merged[key] = raw_detail.get(key)
    return merged


def _find_existing_contact_for_cli_basics(
    raw_detail: dict[str, Any],
    old_subjects: list[dict[str, Any]],
    incoming_values: list[dict[str, str]],
) -> dict[str, Any] | None:
    values = _cli_contact_basics_values(raw_detail)
    wxid = values["wxid"]
    if wxid:
        matches = [item for item in old_subjects if contact_history_target(item) == wxid]
        if len(matches) == 1:
            return matches[0]

    wechat_id = values["wechat_id"]
    if wechat_id:
        matches = [item for item in old_subjects if _clean_text(item.get("wechat_id")) == wechat_id]
        if len(matches) == 1:
            return matches[0]

    for field in ("remark", "nickname"):
        name = values[field]
        if not name:
            continue
        old_matches = [
            item for item in old_subjects
            if _clean_text(item.get(field)) == name
            or _clean_text(item.get("display_name")) == name
            or _clean_text(item.get("send_name")) == name
        ]
        incoming_matches = [item for item in incoming_values if item.get(field) == name]
        if len(old_matches) == 1 and len(incoming_matches) <= 1:
            return old_matches[0]
    return None


def _refresh_names_from_identity(contact: dict[str, Any]) -> None:
    wechat_id = _clean_text(contact.get("wechat_id"))
    remark = _clean_text(contact.get("remark"))
    nickname = _clean_text(contact.get("nickname"))
    send_name, send_name_source = _choose_send_name(wechat_id, remark, nickname)
    contact["display_name"] = remark or nickname or send_name or wechat_id or contact_history_target(contact)
    contact["send_name"] = send_name
    contact["send_name_source"] = send_name_source
    warnings = list(contact.get("warnings") or [])
    warnings = _without_warning(warnings, WARNING_SEND_NAME_UNSEARCHABLE)
    if not is_searchable_send_name(send_name):
        warnings = _append_warning(warnings, WARNING_SEND_NAME_UNSEARCHABLE)
    contact["warnings"] = warnings


def merge_cli_contact_basics(
    existing_directory: dict[str, Any] | None,
    raw_details: list[dict[str, Any]] | tuple[dict[str, Any], ...] | None,
    *,
    wx_id: str,
    now: Any = None,
) -> dict[str, Any]:
    """Merge CLI contact identities without replacing UI-only profile details."""
    timestamp = _iso_timestamp(now)
    existing = _normalize_directory_shape(existing_directory or {}, wx_id)
    old_subjects = [item for item in existing.get("subjects") or [] if isinstance(item, dict)]
    incoming_values = [_cli_contact_basics_values(item or {}) for item in (raw_details or []) if isinstance(item, dict)]

    updated_by_key: dict[str, dict[str, Any]] = {}
    new_key_order: list[str] = []
    for raw_detail in (raw_details or []):
        if not isinstance(raw_detail, dict):
            continue
        values = _cli_contact_basics_values(raw_detail)
        if not (values["wxid"] or values["wechat_id"] or values["remark"] or values["nickname"]):
            continue
        old_contact = _find_existing_contact_for_cli_basics(raw_detail, old_subjects, incoming_values)
        if old_contact:
            key = contact_identity_key(old_contact)
            contact = copy.deepcopy(old_contact)
            old_wxid = contact_history_target(contact)
            new_wxid = values["wxid"]
            if new_wxid:
                if old_wxid and old_wxid != new_wxid and contact.get("wxid_status") in {"verified", "strong_verified"}:
                    contact["wxid_conflict"] = {
                        "previous": old_wxid,
                        "candidate": new_wxid,
                        "detected_at": timestamp,
                    }
                    contact["warnings"] = _append_warning(list(contact.get("warnings") or []), WARNING_WXID_CONFLICT)
                else:
                    contact["wxid"] = new_wxid
                    if old_wxid != new_wxid:
                        contact.pop("wxid_conflict", None)
                    contact["warnings"] = _without_warning(list(contact.get("warnings") or []), WARNING_WXID_CONFLICT)
            for field in ("wechat_id", "nickname", "remark"):
                if values[field]:
                    contact[field] = values[field]
            contact["raw_detail"] = _merge_cli_raw_detail(contact.get("raw_detail") or {}, raw_detail, values)
        else:
            raw_for_new = _merge_cli_raw_detail({}, raw_detail, values)
            contact = normalize_friend_detail(raw_for_new, now=timestamp)
            if values["wxid"]:
                contact["wxid"] = values["wxid"]
            key = contact_identity_key(contact)

        contact["identity_source"] = "cli_basic"
        contact["last_cli_contact_basics_synced_at"] = timestamp
        contact["last_seen_at"] = timestamp
        if contact_history_target(contact) and not _clean_text(contact.get("wxid_status")):
            contact["wxid_status"] = "recent_cli"
        _refresh_names_from_identity(contact)
        updated_by_key[key] = contact
        if key not in new_key_order:
            new_key_order.append(key)

    merged_subjects: list[dict[str, Any]] = []
    emitted: set[str] = set()
    for old_contact in old_subjects:
        key = contact_identity_key(old_contact)
        if key in updated_by_key:
            merged_subjects.append(updated_by_key[key])
        else:
            merged_subjects.append(copy.deepcopy(old_contact))
        emitted.add(key)
    for key in new_key_order:
        if key not in emitted:
            merged_subjects.append(updated_by_key[key])

    merged = _normalize_directory_shape(existing, wx_id)
    merged["updated_at"] = timestamp
    merged["subjects"] = merged_subjects
    return mark_send_name_conflicts(merged)


def _contact_lookup_names(contact: dict[str, Any]) -> set[str]:
    raw_detail = contact.get("raw_detail") if isinstance(contact.get("raw_detail"), dict) else {}
    values = {
        _clean_text(contact.get("remark")),
        _clean_text(contact.get("nickname")),
        _clean_text(contact.get("display_name")),
        _clean_text(contact.get("send_name")),
        _clean_text(contact.get("wechat_id")),
        _clean_text(contact.get("wxid")),
        _clean_text(raw_detail.get("remark")),
        _clean_text(raw_detail.get("nickname")),
        _clean_text(raw_detail.get("备注")),
        _clean_text(raw_detail.get("昵称")),
        _clean_text(raw_detail.get("微信号")),
        _clean_text(raw_detail.get("wxid")),
        _clean_text(raw_detail.get("username")),
    }
    return {value for value in values if value}


def resolve_directory_history_target(
    directory: dict[str, Any] | None,
    chat_name: Any,
    *,
    chat_type: str = "private",
) -> dict[str, Any]:
    if _clean_text(chat_type).lower() == "group":
        return {"ok": False, "target": "", "source": "", "reason": "group_uses_sessions"}
    name = _clean_text(chat_name)
    if not name:
        return {"ok": False, "target": "", "source": "", "reason": "empty_name"}
    directory = mark_send_name_conflicts(directory or {})
    matches = []
    for contact in directory.get("subjects") or []:
        if not isinstance(contact, dict) or not _is_active_friend(contact):
            continue
        if name in _contact_lookup_names(contact):
            target = contact_history_target(contact)
            if target:
                matches.append((target, contact))
    targets = {target for target, _contact in matches if target}
    if len(targets) == 1:
        target = next(iter(targets))
        return {
            "ok": True,
            "target": target,
            "source": "directory_wxid",
            "reason": "",
            "contact": matches[0][1],
        }
    if len(targets) > 1:
        return {"ok": False, "target": "", "source": "", "reason": "ambiguous_directory_wxid"}
    return {"ok": False, "target": "", "source": "", "reason": "missing_directory_wxid"}


def mark_history_target_status(
    directory: dict[str, Any] | None,
    chat_name: Any,
    history_target: Any,
    *,
    wx_id: str,
    status: str = "verified",
    source: str = "history_success",
    now: Any = None,
) -> dict[str, Any]:
    target = _clean_text(history_target)
    if not _looks_like_history_target(target):
        return _normalize_directory_shape(directory or {}, wx_id)
    timestamp = _iso_timestamp(now)
    updated = _normalize_directory_shape(directory or {}, wx_id)
    subjects = [item for item in updated.get("subjects") or [] if isinstance(item, dict)]
    match = None
    for contact in subjects:
        if contact_history_target(contact) == target:
            match = contact
            break
    if match is None:
        resolved = resolve_directory_history_target(updated, chat_name)
        if resolved.get("ok"):
            resolved_key = contact_identity_key(resolved.get("contact") or {})
            for contact in subjects:
                if resolved_key and contact_identity_key(contact) == resolved_key:
                    match = contact
                    break
    if match is None:
        raw_detail = {"wxid": target, "username": target, "昵称": _clean_text(chat_name)}
        match = normalize_friend_detail(raw_detail, now=timestamp)
        subjects.append(match)
    match["wxid"] = target
    match["wxid_status"] = _clean_text(status) or "verified"
    match["wxid_source"] = _clean_text(source) or "history_success"
    match["last_history_success_at"] = timestamp
    match["last_seen_at"] = timestamp
    raw_detail = dict(match.get("raw_detail") or {})
    raw_detail["wxid"] = target
    raw_detail["username"] = target
    if _clean_text(chat_name) and not (_clean_text(raw_detail.get("昵称")) or _clean_text(raw_detail.get("nickname"))):
        raw_detail["昵称"] = _clean_text(chat_name)
        raw_detail["nickname"] = _clean_text(chat_name)
    match["raw_detail"] = raw_detail
    match["warnings"] = _without_warning(list(match.get("warnings") or []), WARNING_WXID_CONFLICT)
    match.pop("wxid_conflict", None)
    _refresh_names_from_identity(match)
    updated["subjects"] = subjects
    updated["updated_at"] = timestamp
    return mark_send_name_conflicts(updated)


def contact_identity_key(contact: dict[str, Any]) -> str:
    contact_key = _clean_text(contact.get("contact_key"))
    if contact_key:
        return contact_key
    wechat_id = _clean_text(contact.get("wechat_id"))
    if wechat_id:
        return f"wechat_id:{wechat_id}"
    remark = _clean_text(contact.get("remark"))
    if remark:
        return _local_contact_key("remark", remark)
    nickname = _clean_text(contact.get("nickname"))
    if nickname:
        return _local_contact_key("nickname", nickname)
    raw_detail = contact.get("raw_detail") if isinstance(contact.get("raw_detail"), dict) else {}
    raw_fingerprint = json.dumps(raw_detail, ensure_ascii=False, sort_keys=True)
    return _local_contact_key("unknown", raw_fingerprint)


def _contact_raw_text(contact: dict[str, Any], keys: tuple[str, ...]) -> str:
    raw_detail = contact.get("raw_detail") if isinstance(contact.get("raw_detail"), dict) else {}
    return _first_text(raw_detail, keys)


def _contact_source(contact: dict[str, Any]) -> str:
    return _contact_raw_text(contact, ("来源", "source", "Source"))


def _contact_added_at(contact: dict[str, Any]) -> str:
    return _contact_raw_text(contact, ("添加时间", "added_at", "AddedAt", "added_time"))


def _no_remark_directory_key(contact: dict[str, Any]) -> str:
    if _clean_text(contact.get("remark")):
        return ""
    nickname = _clean_text(contact.get("nickname"))
    source = _contact_source(contact)
    added_at = _contact_added_at(contact)
    if not (nickname and source and added_at):
        return ""
    return "|".join([nickname, source, added_at])


def _only_wechat_id_changed_for_directory(old_contact: dict[str, Any], new_contact: dict[str, Any]) -> bool:
    return (
        _clean_text(old_contact.get("remark")) == _clean_text(new_contact.get("remark"))
        and _clean_text(old_contact.get("nickname")) == _clean_text(new_contact.get("nickname"))
        and _contact_source(old_contact) == _contact_source(new_contact)
        and _contact_added_at(old_contact) == _contact_added_at(new_contact)
        and _clean_text(old_contact.get("wechat_id")) != _clean_text(new_contact.get("wechat_id"))
    )


def _find_existing_contact_for_probe(
    probe: dict[str, Any],
    old_subjects: list[dict[str, Any]],
    incoming_probes: list[dict[str, Any]],
) -> dict[str, Any] | None:
    direct_key = contact_identity_key(probe)
    old_by_key = {contact_identity_key(item): item for item in old_subjects if isinstance(item, dict)}
    if direct_key in old_by_key:
        return old_by_key[direct_key]

    wechat_id = _clean_text(probe.get("wechat_id"))
    if wechat_id:
        matches = [item for item in old_subjects if _clean_text(item.get("wechat_id")) == wechat_id]
        if len(matches) == 1:
            return matches[0]

    remark = _clean_text(probe.get("remark"))
    if remark:
        old_matches = [item for item in old_subjects if _clean_text(item.get("remark")) == remark]
        incoming_matches = [item for item in incoming_probes if _clean_text(item.get("remark")) == remark]
        if len(old_matches) == 1 and len(incoming_matches) <= 1:
            return old_matches[0]
        return None

    no_remark_key = _no_remark_directory_key(probe)
    if no_remark_key:
        old_matches = [
            item for item in old_subjects
            if _no_remark_directory_key(item) == no_remark_key
            and _only_wechat_id_changed_for_directory(item, probe)
        ]
        incoming_matches = [item for item in incoming_probes if _no_remark_directory_key(item) == no_remark_key]
        if len(old_matches) == 1 and len(incoming_matches) <= 1:
            return old_matches[0]

    return None


def stable_suffix_for_contact(contact: dict[str, Any]) -> str:
    existing = _clean_text(contact.get("stable_suffix"))
    if existing:
        return existing
    source = contact_identity_key(contact) or _clean_text(contact.get("send_name")) or "contact"
    number = int(hashlib.sha1(source.encode("utf-8")).hexdigest()[:8], 16) % 10000
    return f"{number:04d}"


def default_directory(wx_id: str) -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "wx_id": _clean_text(wx_id),
        "updated_at": "",
        "maintenance": {
            "mode": "",
            "status": "idle",
            "round_id": "",
            "collected_count": 0,
            "last_batch_finished_at": "",
            "last_error": "",
            "paused": False,
        },
        "identity_calibration": {
            "identities": [],
            "pending": [],
            "dismissed_pairs": [],
        },
        "subjects": [],
    }


def _normalize_directory_shape(directory: Any, wx_id: str = "") -> dict[str, Any]:
    fallback = default_directory(wx_id)
    if not isinstance(directory, dict):
        return fallback

    normalized = copy.deepcopy(fallback)
    normalized["schema_version"] = directory.get("schema_version") or SCHEMA_VERSION
    normalized["wx_id"] = _clean_text(directory.get("wx_id")) or _clean_text(wx_id)
    normalized["updated_at"] = _clean_text(directory.get("updated_at"))

    maintenance = directory.get("maintenance")
    if isinstance(maintenance, dict):
        normalized["maintenance"].update(copy.deepcopy(maintenance))

    identity_calibration = directory.get("identity_calibration")
    if isinstance(identity_calibration, dict):
        normalized["identity_calibration"] = normalize_contact_calibration_state(identity_calibration, wx_id=normalized["wx_id"])

    subjects = directory.get("subjects")
    normalized["subjects"] = copy.deepcopy(subjects) if isinstance(subjects, list) else []
    return normalized


def merge_directory(
    existing_directory: dict[str, Any] | None,
    raw_details: list[dict[str, Any]] | tuple[dict[str, Any], ...] | None,
    *,
    wx_id: str,
    now: Any = None,
    mark_missing: bool = True,
) -> dict[str, Any]:
    """Merge one fetched batch into a directory without deleting old contacts."""
    timestamp = _iso_timestamp(now)
    existing = _normalize_directory_shape(existing_directory or {}, wx_id)
    old_subjects = list(existing.get("subjects") or [])

    updated_by_key: dict[str, dict[str, Any]] = {}
    new_key_order: list[str] = []
    incoming_items = [
        (raw_detail, normalize_friend_detail(raw_detail, now=timestamp))
        for raw_detail in (raw_details or [])
    ]
    incoming_probes = [probe for _raw_detail, probe in incoming_items]
    for raw_detail, probe in incoming_items:
        old_contact = _find_existing_contact_for_probe(probe, old_subjects, incoming_probes)
        key = contact_identity_key(old_contact) if old_contact else contact_identity_key(probe)
        normalized = normalize_friend_detail(raw_detail, existing=old_contact, now=timestamp)
        updated_by_key[key] = normalized
        if key not in new_key_order:
            new_key_order.append(key)

    merged_subjects: list[dict[str, Any]] = []
    emitted: set[str] = set()
    for old_contact in old_subjects:
        if not isinstance(old_contact, dict):
            continue
        key = contact_identity_key(old_contact)
        if key in updated_by_key:
            merged_subjects.append(updated_by_key[key])
        else:
            untouched = copy.deepcopy(old_contact)
            if mark_missing:
                untouched["status"] = "missing"
            merged_subjects.append(untouched)
        emitted.add(key)

    for key in new_key_order:
        if key not in emitted:
            merged_subjects.append(updated_by_key[key])

    merged = _normalize_directory_shape(existing, wx_id)
    merged["wx_id"] = _clean_text(wx_id) or merged.get("wx_id", "")
    merged["updated_at"] = timestamp
    merged["subjects"] = merged_subjects
    return mark_send_name_conflicts(merged)


def safe_name(value: Any) -> str:
    text = _clean_text(value)
    text = text.replace("\\", "_").replace("/", "_")
    text = _SAFE_NAME_RE.sub("_", text).strip(" ._")
    return text or "_"


def directory_path(base_dir: str | Path, wx_id: str) -> Path:
    return account_area_dir(base_dir, safe_name(wx_id), "contact_profiles") / DIRECTORY_FILENAME


def load_directory(path: str | Path, wx_id: str = "") -> dict[str, Any]:
    path = Path(path)
    if not path.exists():
        return default_directory(wx_id)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, UnicodeDecodeError):
        return default_directory(wx_id)
    return _normalize_directory_shape(data, wx_id)


def save_directory(path: str | Path, directory: dict[str, Any]) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(directory, ensure_ascii=False, indent=2), encoding="utf-8")


def sync_identity_calibration_from_directory(
    directory: dict[str, Any],
    *,
    wx_id: str = "",
    now: Any = None,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Refresh embedded identity calibration state from the contact directory."""
    updated = _normalize_directory_shape(directory or {}, wx_id)
    index = normalize_contact_calibration_state(
        updated.get("identity_calibration") or default_contact_calibration_state(updated.get("wx_id", "")),
        wx_id=updated.get("wx_id", ""),
    )
    next_index, actions = update_contact_identity_from_directory(
        index,
        updated,
        wx_id=updated.get("wx_id", ""),
        now=now,
    )
    updated["identity_calibration"] = next_index
    updated["updated_at"] = _iso_timestamp(now)
    return updated, actions


def dismiss_identity_calibration_pending(directory: dict[str, Any], fingerprint: Any) -> dict[str, Any]:
    updated = _normalize_directory_shape(directory or {}, _clean_text((directory or {}).get("wx_id")) if isinstance(directory, dict) else "")
    index = dismiss_contact_identity_pending(updated.get("identity_calibration") or {}, _clean_text(fingerprint))
    updated["identity_calibration"] = normalize_contact_calibration_state(index, wx_id=updated.get("wx_id", ""))
    updated["updated_at"] = _iso_timestamp()
    return updated


def _is_active_friend(contact: dict[str, Any]) -> bool:
    return contact.get("subject_type") == "friend" and contact.get("status", "active") == "active"


def mark_send_name_conflicts(directory: dict[str, Any]) -> dict[str, Any]:
    """Mark active contacts sharing the same send_name with duplicate warning."""
    marked = _normalize_directory_shape(directory, _clean_text(directory.get("wx_id")) if isinstance(directory, dict) else "")
    name_counts: dict[str, int] = {}

    for contact in marked["subjects"]:
        if not isinstance(contact, dict):
            continue
        warnings = list(contact.get("warnings") or [])
        contact["warnings"] = _without_warning(warnings, WARNING_DUPLICATE_SEND_NAME)
        if _is_active_friend(contact):
            send_name = _clean_text(contact.get("send_name"))
            if send_name:
                name_counts[send_name] = name_counts.get(send_name, 0) + 1

    for contact in marked["subjects"]:
        if not isinstance(contact, dict) or not _is_active_friend(contact):
            continue
        send_name = _clean_text(contact.get("send_name"))
        if send_name and name_counts.get(send_name, 0) > 1:
            contact["warnings"] = _append_warning(list(contact.get("warnings") or []), WARNING_DUPLICATE_SEND_NAME)

    return marked


def remark_repair_text(contact: dict[str, Any]) -> str:
    suffix = stable_suffix_for_contact(contact)
    current_remark = _clean_text(contact.get("remark"))
    if current_remark:
        base = current_remark
    else:
        nickname = _clean_text(contact.get("nickname"))
        send_name = _clean_text(contact.get("send_name"))
        if is_searchable_send_name(nickname):
            base = nickname
        elif is_searchable_send_name(send_name):
            base = send_name
        else:
            base = "联系人"

    expected_suffix = f"_{suffix}"
    if base.endswith(expected_suffix):
        return base
    return f"{base}{expected_suffix}"


def repair_candidates(directory: dict[str, Any]) -> list[dict[str, Any]]:
    directory = mark_send_name_conflicts(directory)
    candidates: list[dict[str, Any]] = []
    for contact in directory.get("subjects") or []:
        if not isinstance(contact, dict) or not _is_active_friend(contact):
            continue
        if _clean_text(contact.get("remark")):
            continue
        warnings = list(contact.get("warnings") or [])
        reasons = [
            warning
            for warning in (WARNING_DUPLICATE_SEND_NAME, WARNING_SEND_NAME_UNSEARCHABLE)
            if warning in warnings
        ]
        if not reasons:
            continue
        candidates.append(
            {
                "contact_key": contact.get("contact_key", ""),
                "display_name": contact.get("display_name", ""),
                "current_remark": contact.get("remark", ""),
                "suggested_remark": remark_repair_text(contact),
                "reasons": reasons,
                "warnings": warnings,
            }
        )
    return candidates


def apply_repaired_remark(
    directory: dict[str, Any],
    contact_key: Any,
    new_remark: Any,
    *,
    now: Any = None,
) -> dict[str, Any]:
    updated = _normalize_directory_shape(directory, _clean_text((directory or {}).get("wx_id")) if isinstance(directory, dict) else "")
    target_key = _clean_text(contact_key)
    remark = _clean_text(new_remark)
    if not target_key or not remark:
        return updated

    for contact in updated.get("subjects") or []:
        if not isinstance(contact, dict):
            continue
        if contact_identity_key(contact) != target_key:
            continue
        contact["remark"] = remark
        contact["display_name"] = remark
        contact["send_name"] = remark
        contact["send_name_source"] = "remark"
        contact["last_seen_at"] = _iso_timestamp(now)
        warnings = list(contact.get("warnings") or [])
        warnings = _without_warning(warnings, WARNING_SEND_NAME_UNSEARCHABLE)
        warnings = _without_warning(warnings, WARNING_DUPLICATE_SEND_NAME)
        contact["warnings"] = warnings
        raw_detail = dict(contact.get("raw_detail") or {})
        raw_detail["备注"] = remark
        raw_detail["remark"] = remark
        contact["raw_detail"] = raw_detail
        break

    updated["updated_at"] = _iso_timestamp(now)
    return mark_send_name_conflicts(updated)


def _key_set(value: Any) -> set[str]:
    return {_clean_text(item) for item in _coerce_list(value) if _clean_text(item)}


def _tag_set(value: Any) -> set[str]:
    return set(normalize_tag_list(value))


def _manual_match_values(contact: dict[str, Any]) -> set[str]:
    values = {
        _clean_text(contact.get("remark")),
        _clean_text(contact.get("nickname")),
        _clean_text(contact.get("display_name")),
        _clean_text(contact.get("send_name")),
    }
    return {value for value in values if value}


def resolve_manual_target_names(directory: dict[str, Any], names: Any) -> dict[str, Any]:
    """Resolve user-entered nicknames/remarks to active contacts in the local directory."""
    directory = mark_send_name_conflicts(directory)
    active_contacts = [
        contact
        for contact in directory.get("subjects") or []
        if isinstance(contact, dict) and _is_active_friend(contact)
    ]

    selected: list[dict[str, Any]] = []
    missing: list[str] = []
    warnings: list[dict[str, Any]] = []
    emitted: set[str] = set()

    for raw_name in _coerce_list(names):
        name = _clean_text(raw_name)
        if not name:
            continue
        match = None
        for contact in active_contacts:
            if name in _manual_match_values(contact):
                match = contact
                break
        if not match:
            missing.append(name)
            continue

        key = contact_identity_key(match)
        if key not in emitted:
            selected.append(match)
            emitted.add(key)
        for warning in match.get("warnings") or []:
            warnings.append(
                {
                    "contact_key": match.get("contact_key", ""),
                    "send_name": match.get("send_name", ""),
                    "warning": warning,
                    "input": name,
                }
            )

    return {"selected": selected, "missing": missing, "warnings": warnings}


def resolve_target_selector(directory: dict[str, Any], selector: dict[str, Any] | None) -> dict[str, Any]:
    selector = selector or {}
    directory = mark_send_name_conflicts(directory)
    active_contacts = [
        contact
        for contact in directory.get("subjects") or []
        if isinstance(contact, dict) and _is_active_friend(contact)
    ]

    include_tags = _tag_set(selector.get("include_tags"))
    exclude_tags = _tag_set(selector.get("exclude_tags"))
    include_keys = _key_set(selector.get("include_contact_keys"))
    exclude_keys = _key_set(selector.get("exclude_contact_keys"))
    active_keys = {contact_identity_key(contact) for contact in active_contacts}

    base = selector.get("base") or "all_friends"
    desired_keys: set[str] = set()
    if base == "all_friends":
        for contact in active_contacts:
            contact_tags = set(contact.get("tags") or [])
            if include_tags and not (contact_tags & include_tags):
                continue
            desired_keys.add(contact_identity_key(contact))

    for contact in active_contacts:
        key = contact_identity_key(contact)
        if key in include_keys:
            desired_keys.add(key)

    excluded: list[dict[str, Any]] = []
    for key in sorted(include_keys - active_keys):
        excluded.append(
            {
                "contact": {
                    "subject_type": "friend",
                    "contact_key": key,
                    "send_name": "",
                    "display_name": key,
                    "tags": [],
                    "warnings": [],
                },
                "reason": "missing_contact",
            }
        )

    excluded_keys: set[str] = set()
    for contact in active_contacts:
        key = contact_identity_key(contact)
        contact_tags = set(contact.get("tags") or [])
        reason = ""
        if exclude_tags and contact_tags & exclude_tags:
            reason = "exclude_tags"
        elif key in exclude_keys:
            reason = "exclude_contact_keys"
        if reason:
            excluded.append({"contact": contact, "reason": reason})
            excluded_keys.add(key)

    selected: list[dict[str, Any]] = []
    for contact in active_contacts:
        key = contact_identity_key(contact)
        if key in desired_keys and key not in excluded_keys:
            selected.append(contact)

    max_targets = selector.get("max_targets_per_run")
    try:
        max_targets_int = int(max_targets)
    except (TypeError, ValueError):
        max_targets_int = 0
    if max_targets_int > 0 and len(selected) > max_targets_int:
        overflow = selected[max_targets_int:]
        selected = selected[:max_targets_int]
        excluded.extend({"contact": contact, "reason": "max_targets_per_run"} for contact in overflow)

    warnings: list[dict[str, Any]] = []
    for contact in selected:
        for warning in contact.get("warnings") or []:
            warnings.append(
                {
                    "contact_key": contact.get("contact_key", ""),
                    "send_name": contact.get("send_name", ""),
                    "warning": warning,
                }
            )

    return {
        "selected": selected,
        "excluded": excluded,
        "warnings": warnings,
        "stats": {
            "selected": len(selected),
            "excluded": len(excluded),
            "warnings": len(warnings),
        },
    }
