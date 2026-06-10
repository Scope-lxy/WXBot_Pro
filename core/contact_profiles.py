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


SCHEMA_VERSION = 1
DIRECTORY_FILENAME = "contacts.json"

WARNING_DUPLICATE_SEND_NAME = "duplicate_send_name"
WARNING_SEND_NAME_UNSEARCHABLE = "send_name_unsearchable"

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
    old_by_key = {contact_identity_key(item): item for item in old_subjects if isinstance(item, dict)}

    updated_by_key: dict[str, dict[str, Any]] = {}
    new_key_order: list[str] = []
    for raw_detail in raw_details or []:
        probe = normalize_friend_detail(raw_detail, now=timestamp)
        key = contact_identity_key(probe)
        old_contact = old_by_key.get(key)
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
