"""Contact identity calibration and storage reconciliation helpers."""

from __future__ import annotations

import copy
import hashlib
import json
import os
import shutil
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Any

from core.account_storage import account_area_dir
from core.memory import read_memory_original_name, resolve_memory_storage_name


SCHEMA_VERSION = 1
PENDING_STATUS_PENDING = "pending"
PENDING_STATUS_DISMISSED = "dismissed"


def _now_text(now: Any = None) -> str:
    if isinstance(now, datetime):
        return now.replace(microsecond=0).isoformat()
    if isinstance(now, str):
        return now
    return datetime.now().replace(microsecond=0).isoformat()


def _clean(value: Any) -> str:
    return str(value or "").strip()


def _raw_text(contact: dict[str, Any], key: str) -> str:
    raw = contact.get("raw_detail") if isinstance(contact.get("raw_detail"), dict) else {}
    return _clean(raw.get(key))


def _short_hash(value: str, length: int = 12) -> str:
    return hashlib.sha1(value.encode("utf-8", errors="ignore")).hexdigest()[:length]



def default_calibration_state(wx_id: str = "") -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "wx_id": _clean(wx_id),
        "updated_at": "",
        "identities": [],
        "pending": [],
        "dismissed_pairs": [],
    }


def normalize_snapshot(snapshot: dict[str, Any] | None) -> dict[str, str]:
    snapshot = snapshot if isinstance(snapshot, dict) else {}
    remark = _clean(snapshot.get("remark"))
    nickname = _clean(snapshot.get("nickname"))
    wechat_id = _clean(snapshot.get("wechat_id"))
    display_name = _clean(snapshot.get("display_name"))
    send_name = _clean(snapshot.get("send_name"))
    chat_name = _clean(snapshot.get("current_chat_name") or send_name or remark or nickname or display_name or wechat_id)
    return {
        "current_chat_name": chat_name,
        "storage_name": _clean(snapshot.get("storage_name")) or resolve_memory_storage_name(chat_name),
        "wechat_id": wechat_id,
        "remark": remark,
        "nickname": nickname,
        "display_name": display_name or chat_name,
        "send_name": send_name or chat_name,
        "region": _clean(snapshot.get("region")),
        "source": _clean(snapshot.get("source")),
        "added_at": _clean(snapshot.get("added_at")),
        "signature": _clean(snapshot.get("signature")),
        "last_seen_at": _clean(snapshot.get("last_seen_at")),
    }


def snapshot_from_contact(contact: dict[str, Any] | None) -> dict[str, str]:
    contact = contact if isinstance(contact, dict) else {}
    snapshot = {
        "wechat_id": contact.get("wechat_id"),
        "remark": contact.get("remark"),
        "nickname": contact.get("nickname"),
        "display_name": contact.get("display_name"),
        "send_name": contact.get("send_name"),
        "region": _raw_text(contact, "地区") or contact.get("region"),
        "source": _raw_text(contact, "来源") or contact.get("source"),
        "added_at": _raw_text(contact, "添加时间") or contact.get("added_at"),
        "signature": _raw_text(contact, "个性签名") or contact.get("signature"),
        "last_seen_at": contact.get("last_seen_at"),
    }
    return normalize_snapshot(snapshot)


def active_contact_snapshots(directory: dict[str, Any] | None) -> list[dict[str, str]]:
    directory = directory if isinstance(directory, dict) else {}
    snapshots = []
    for contact in directory.get("subjects") or []:
        if not isinstance(contact, dict):
            continue
        if contact.get("subject_type", "friend") != "friend":
            continue
        if contact.get("status", "active") != "active":
            continue
        snapshot = snapshot_from_contact(contact)
        if snapshot.get("current_chat_name"):
            snapshots.append(snapshot)
    return snapshots


def identity_id_for_snapshot(snapshot: dict[str, Any]) -> str:
    snapshot = normalize_snapshot(snapshot)
    source = snapshot.get("wechat_id") or "|".join([
        snapshot.get("remark", ""),
        snapshot.get("nickname", ""),
        snapshot.get("source", ""),
        snapshot.get("added_at", ""),
    ])
    return "person_" + _short_hash(source or snapshot.get("current_chat_name", "") or "unknown", 16)


def normalize_identity(identity: dict[str, Any] | None) -> dict[str, Any]:
    identity = identity if isinstance(identity, dict) else {}
    snapshot = normalize_snapshot(identity)
    identity_id = _clean(identity.get("identity_id")) or identity_id_for_snapshot(snapshot)
    return {
        "identity_id": identity_id,
        **snapshot,
        "created_at": _clean(identity.get("created_at")),
        "updated_at": _clean(identity.get("updated_at")),
    }


def normalize_calibration_state(index: dict[str, Any] | None, wx_id: str = "") -> dict[str, Any]:
    index = index if isinstance(index, dict) else {}
    normalized = default_calibration_state(wx_id or index.get("wx_id", ""))
    normalized["schema_version"] = index.get("schema_version") or SCHEMA_VERSION
    normalized["updated_at"] = _clean(index.get("updated_at"))
    identities = []
    seen_ids = set()
    for item in index.get("identities") or []:
        if not isinstance(item, dict):
            continue
        identity = normalize_identity(item)
        identity_id = identity["identity_id"]
        if identity_id in seen_ids:
            suffix = _short_hash(json.dumps(identity, ensure_ascii=False, sort_keys=True), 6)
            identity["identity_id"] = f"{identity_id}_{suffix}"
        seen_ids.add(identity["identity_id"])
        identities.append(identity)
    normalized["identities"] = identities
    normalized["pending"] = [
        normalize_pending_item(item)
        for item in (index.get("pending") or [])
        if isinstance(item, dict)
    ]
    normalized["dismissed_pairs"] = sorted({
        _clean(item)
        for item in (index.get("dismissed_pairs") or [])
        if _clean(item)
    })
    return normalized



def _group_by(items: list[dict[str, Any]], key: str) -> dict[str, list[dict[str, Any]]]:
    grouped: dict[str, list[dict[str, Any]]] = {}
    for item in items:
        value = _clean(item.get(key))
        if value:
            grouped.setdefault(value, []).append(item)
    return grouped


def _no_remark_key(snapshot: dict[str, Any]) -> str:
    snapshot = normalize_snapshot(snapshot)
    if snapshot.get("remark"):
        return ""
    nickname = snapshot.get("nickname")
    source = snapshot.get("source")
    added_at = snapshot.get("added_at")
    if not (nickname and source and added_at):
        return ""
    return "|".join([nickname, source, added_at])


def _same_no_remark_snapshot(left: dict[str, Any], right: dict[str, Any]) -> bool:
    left = normalize_snapshot(left)
    right = normalize_snapshot(right)
    key = _no_remark_key(left)
    return bool(key and key == _no_remark_key(right))


def _nickname_source_added_key(snapshot: dict[str, Any]) -> str:
    snapshot = normalize_snapshot(snapshot)
    nickname = snapshot.get("nickname")
    source = snapshot.get("source")
    added_at = snapshot.get("added_at")
    if not (nickname and source and added_at):
        return ""
    return "|".join([nickname, source, added_at])


def _is_only_wechat_id_changed(left: dict[str, Any], right: dict[str, Any]) -> bool:
    left = normalize_snapshot(left)
    right = normalize_snapshot(right)
    fields = ("remark", "nickname", "source", "added_at")
    if not all(left.get(field) == right.get(field) for field in fields):
        return False
    left_id = left.get("wechat_id")
    right_id = right.get("wechat_id")
    return left_id != right_id and bool(left_id or right_id)


def _is_same_no_remark_snapshot(left: dict[str, Any], right: dict[str, Any]) -> bool:
    left = normalize_snapshot(left)
    right = normalize_snapshot(right)
    fields = ("remark", "nickname", "source", "added_at", "wechat_id")
    return _same_no_remark_snapshot(left, right) and all(left.get(field) == right.get(field) for field in fields)


def match_identity(
    snapshot: dict[str, Any],
    identities: list[dict[str, Any]],
    *,
    incoming_snapshots: list[dict[str, Any]] | None = None,
) -> tuple[dict[str, Any] | None, str]:
    snapshot = normalize_snapshot(snapshot)
    identities = [normalize_identity(item) for item in identities]
    incoming_snapshots = [normalize_snapshot(item) for item in (incoming_snapshots or [])]

    wechat_id = snapshot.get("wechat_id")
    if wechat_id:
        matches = [item for item in identities if item.get("wechat_id") == wechat_id]
        if len(matches) == 1:
            return matches[0], "wechat_id"
        if len(matches) > 1:
            return None, "conflict_wechat_id"

    remark = snapshot.get("remark")
    if remark:
        old_matches = [item for item in identities if item.get("remark") == remark]
        incoming_matches = [item for item in incoming_snapshots if item.get("remark") == remark]
        if len(old_matches) == 1 and len(incoming_matches) <= 1:
            return old_matches[0], "unique_remark"
        if len(old_matches) > 1 or len(incoming_matches) > 1:
            return None, "conflict_remark"

    no_remark_key = _no_remark_key(snapshot)
    if no_remark_key:
        old_matches = [
            item for item in identities
            if _no_remark_key(item) == no_remark_key
            and (_is_only_wechat_id_changed(item, snapshot) or _is_same_no_remark_snapshot(item, snapshot))
        ]
        incoming_matches = [item for item in incoming_snapshots if _no_remark_key(item) == no_remark_key]
        if len(old_matches) == 1 and len(incoming_matches) <= 1:
            return old_matches[0], "no_remark_snapshot"
        if len(old_matches) > 1 or len(incoming_matches) > 1:
            return None, "conflict_no_remark_snapshot"

    return None, "new_or_pending"


def pending_candidates_for_snapshot(
    snapshot: dict[str, Any],
    identities: list[dict[str, Any]],
    *,
    reason: str = "",
) -> list[dict[str, Any]]:
    """Return meaningful manual-review candidates without weak name-only prompts."""
    snapshot = normalize_snapshot(snapshot)
    identities = [normalize_identity(item) for item in identities]
    candidates: list[dict[str, Any]] = []
    seen_ids: set[str] = set()

    def add_matches(matches: list[dict[str, Any]], candidate_reason: str) -> None:
        for item in matches:
            identity_id = _clean(item.get("identity_id"))
            if not identity_id or identity_id in seen_ids:
                continue
            seen_ids.add(identity_id)
            candidates.append({
                "identity": item,
                "reason": reason or candidate_reason,
            })

    wechat_id = snapshot.get("wechat_id")
    if wechat_id:
        add_matches([item for item in identities if item.get("wechat_id") == wechat_id], "same_wechat_id")

    remark = snapshot.get("remark")
    if remark:
        add_matches([item for item in identities if item.get("remark") == remark], "same_remark")

    no_remark_key = _no_remark_key(snapshot)
    if no_remark_key:
        add_matches([item for item in identities if _no_remark_key(item) == no_remark_key], "same_no_remark_snapshot")

    composite_key = _nickname_source_added_key(snapshot)
    if composite_key:
        add_matches(
            [item for item in identities if _nickname_source_added_key(item) == composite_key],
            "same_nickname_source_added",
        )

    return candidates


def pending_fingerprint(left: dict[str, Any], right: dict[str, Any]) -> str:
    left = normalize_snapshot(left)
    right = normalize_snapshot(right)
    payload = [
        left.get("current_chat_name", ""),
        left.get("wechat_id", ""),
        right.get("current_chat_name", ""),
        right.get("wechat_id", ""),
    ]
    ordered = sorted("|".join(payload[i:i + 2]) for i in (0, 2))
    return _short_hash("||".join(ordered), 16)


def normalize_pending_item(item: dict[str, Any] | None) -> dict[str, Any]:
    item = item if isinstance(item, dict) else {}
    left = normalize_snapshot(item.get("old_snapshot") if isinstance(item.get("old_snapshot"), dict) else {})
    right = normalize_snapshot(item.get("new_snapshot") if isinstance(item.get("new_snapshot"), dict) else {})
    fingerprint = _clean(item.get("fingerprint")) or pending_fingerprint(left, right)
    return {
        "fingerprint": fingerprint,
        "status": _clean(item.get("status")) or PENDING_STATUS_PENDING,
        "reason": _clean(item.get("reason")),
        "old_identity_id": _clean(item.get("old_identity_id")),
        "new_identity_id": _clean(item.get("new_identity_id")),
        "old_snapshot": left,
        "new_snapshot": right,
        "created_at": _clean(item.get("created_at")),
        "updated_at": _clean(item.get("updated_at")),
    }


def add_pending(
    index: dict[str, Any],
    old_identity: dict[str, Any],
    new_snapshot: dict[str, Any],
    *,
    reason: str,
    new_identity_id: str = "",
    now: Any = None,
) -> dict[str, Any]:
    index = normalize_calibration_state(index)
    old_identity = normalize_identity(old_identity)
    new_snapshot = normalize_snapshot(new_snapshot)
    fingerprint = pending_fingerprint(old_identity, new_snapshot)
    if fingerprint in set(index.get("dismissed_pairs") or []):
        return index
    existing = {
        _clean(item.get("fingerprint")): item
        for item in (index.get("pending") or [])
        if isinstance(item, dict)
    }
    stamp = _now_text(now)
    existing[fingerprint] = normalize_pending_item({
        "fingerprint": fingerprint,
        "status": PENDING_STATUS_PENDING,
        "reason": reason,
        "old_identity_id": old_identity.get("identity_id"),
        "new_identity_id": _clean(new_identity_id),
        "old_snapshot": old_identity,
        "new_snapshot": new_snapshot,
        "created_at": existing.get(fingerprint, {}).get("created_at") or stamp,
        "updated_at": stamp,
    })
    index["pending"] = list(existing.values())
    return index


def dismiss_pending(index: dict[str, Any], fingerprint: str) -> dict[str, Any]:
    index = normalize_calibration_state(index)
    fingerprint = _clean(fingerprint)
    if not fingerprint:
        return index
    dismissed = set(index.get("dismissed_pairs") or [])
    dismissed.add(fingerprint)
    index["dismissed_pairs"] = sorted(dismissed)
    pending = []
    for item in index.get("pending") or []:
        if _clean(item.get("fingerprint")) == fingerprint:
            item = dict(item)
            item["status"] = PENDING_STATUS_DISMISSED
            item["updated_at"] = _now_text()
        pending.append(item)
    index["pending"] = pending
    return index


def update_calibration_from_directory(
    index: dict[str, Any],
    directory: dict[str, Any],
    *,
    wx_id: str = "",
    now: Any = None,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    index = normalize_calibration_state(index, wx_id=wx_id)
    snapshots = active_contact_snapshots(directory)
    identities = [normalize_identity(item) for item in index.get("identities") or []]
    by_id = {item["identity_id"]: item for item in identities}
    actions: list[dict[str, Any]] = []
    stamp = _now_text(now)

    for snapshot in snapshots:
        match, reason = match_identity(snapshot, list(by_id.values()), incoming_snapshots=snapshots)
        if match:
            identity_id = match["identity_id"]
            old_chat = match.get("current_chat_name", "")
            updated = normalize_identity({
                **match,
                **snapshot,
                "identity_id": identity_id,
                "created_at": match.get("created_at") or stamp,
                "updated_at": stamp,
            })
            by_id[identity_id] = updated
            if old_chat and updated.get("current_chat_name") and old_chat != updated.get("current_chat_name"):
                actions.append({
                    "type": "rename",
                    "reason": reason,
                    "identity_id": identity_id,
                    "old_chat_name": old_chat,
                    "new_chat_name": updated.get("current_chat_name", ""),
                })
            continue

        candidates = pending_candidates_for_snapshot(snapshot, list(by_id.values()), reason=reason)

        identity = normalize_identity({
            **snapshot,
            "identity_id": identity_id_for_snapshot(snapshot),
            "created_at": stamp,
            "updated_at": stamp,
        })
        while identity["identity_id"] in by_id:
            identity["identity_id"] = "person_" + _short_hash(
                json.dumps({**snapshot, "salt": len(by_id)}, ensure_ascii=False, sort_keys=True),
                16,
            )
        by_id[identity["identity_id"]] = identity
        for candidate in candidates:
            index = add_pending(
                index,
                candidate.get("identity") or {},
                snapshot,
                reason=candidate.get("reason") or "needs_confirmation",
                new_identity_id=identity["identity_id"],
                now=stamp,
            )

    index["identities"] = sorted(by_id.values(), key=lambda item: item.get("current_chat_name", ""))
    index["updated_at"] = stamp
    return normalize_calibration_state(index, wx_id=wx_id), actions



def _safe_copytree(src: Path, dst: Path) -> None:
    if not src.exists():
        return
    if src.is_dir():
        shutil.copytree(src, dst)
    else:
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dst)


def backup_paths(paths: dict[str, Path], backup_root: Path) -> dict[str, str]:
    backup_root.mkdir(parents=True, exist_ok=True)
    copied = {}
    for label, path in paths.items():
        path = Path(path)
        if not path.exists():
            continue
        target = backup_root / label
        _safe_copytree(path, target)
        copied[label] = str(target)
    return copied


def memory_dir(base_dir: str | Path, wx_id: str, chat_name: str) -> Path:
    return account_area_dir(base_dir, wx_id, "memory") / resolve_memory_storage_name(chat_name)


def memory_file_in_dir(chat_dir: Path) -> Path | None:
    if not chat_dir.exists() or not chat_dir.is_dir():
        return None
    preferred = chat_dir / f"{chat_dir.name}_memory.json"
    if preferred.exists():
        return preferred
    for path in chat_dir.iterdir():
        if path.is_file() and path.name.endswith("_memory.json"):
            return path
    return None


def _canonical_memory_file(chat_dir: Path, chat_name: str) -> Path:
    return chat_dir / f"{resolve_memory_storage_name(chat_name)}_memory.json"


def _write_memory_dir_payload(chat_dir: Path, chat_name: str, messages: list[Any]) -> Path:
    chat_dir.mkdir(parents=True, exist_ok=True)
    target_file = _canonical_memory_file(chat_dir, chat_name)
    existing_files = [
        path for path in chat_dir.iterdir()
        if path.is_file() and path.name.endswith("_memory.json")
    ]
    target_file.write_text(json.dumps(messages, ensure_ascii=False, indent=2), encoding="utf-8")
    for path in existing_files:
        if path != target_file:
            try:
                path.unlink()
            except OSError:
                pass
    (chat_dir / "name.json").write_text(json.dumps({"name": _clean(chat_name)}, ensure_ascii=False, indent=2), encoding="utf-8")
    return target_file


def chat_memory_file(base_dir: str | Path, wx_id: str, chat_name: str) -> Path:
    return account_area_dir(base_dir, wx_id, "chat_memory") / f"{resolve_memory_storage_name(chat_name)}.json"


def _read_json_list(path: Path) -> list[Any]:
    if not path or not path.exists():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, list) else []
    except Exception:
        return []


def _message_key(message: Any) -> str:
    if not isinstance(message, dict):
        return hashlib.sha1(str(message).encode("utf-8", errors="ignore")).hexdigest()
    digest = hashlib.sha1(str(message.get("content", "") or "").encode("utf-8", errors="ignore")).hexdigest()[:12]
    return "|".join([
        _clean(message.get("time")),
        _clean(message.get("sender")),
        _clean(message.get("type")),
        _clean(message.get("attr")),
        digest,
    ])


def merge_memory_dirs(base_dir: str | Path, wx_id: str, old_chat_name: str, new_chat_name: str) -> dict[str, Any]:
    old_dir = memory_dir(base_dir, wx_id, old_chat_name)
    new_dir = memory_dir(base_dir, wx_id, new_chat_name)
    result = {
        "old_chat_name": old_chat_name,
        "new_chat_name": new_chat_name,
        "old_dir": str(old_dir),
        "new_dir": str(new_dir),
        "old_count": 0,
        "new_count": 0,
        "merged_count": 0,
        "deduped_count": 0,
        "changed": False,
    }
    if not old_dir.exists():
        return result
    if not new_dir.exists():
        new_dir.parent.mkdir(parents=True, exist_ok=True)
        os.replace(str(old_dir), str(new_dir))
        memory_file = memory_file_in_dir(new_dir)
        messages = _read_json_list(memory_file) if memory_file else []
        result["old_count"] = len(messages)
        result["merged_count"] = len(messages)
        _write_memory_dir_payload(new_dir, new_chat_name, messages)
        result["changed"] = True
        return result

    old_file = memory_file_in_dir(old_dir)
    new_file = memory_file_in_dir(new_dir)
    old_messages = _read_json_list(old_file) if old_file else []
    new_messages = _read_json_list(new_file) if new_file else []
    result["old_count"] = len(old_messages)
    result["new_count"] = len(new_messages)
    merged = []
    seen = set()
    for message in [*new_messages, *old_messages]:
        key = _message_key(message)
        if key in seen:
            continue
        seen.add(key)
        merged.append(message)
    merged.sort(key=lambda item: _clean(item.get("time")) if isinstance(item, dict) else "")
    _write_memory_dir_payload(new_dir, new_chat_name, merged)
    shutil.rmtree(old_dir)
    result["merged_count"] = len(merged)
    result["deduped_count"] = len(old_messages) + len(new_messages) - len(merged)
    result["changed"] = True
    return result


def _read_json_object(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _importance_rank(value: str) -> int:
    return {"高": 0, "中": 1, "低": 2}.get(_clean(value), 1)


def _normalize_chat_memory_payload(payload: dict[str, Any], chat_name: str, wx_id: str) -> dict[str, Any]:
    from core.prompt_system import ChatMemoryStore

    store = ChatMemoryStore(os.path.join(tempfile.gettempdir(), "wxbot_identity_memory_normalize"))
    normalized = store.normalize_state(
        payload if isinstance(payload, dict) else {},
        chat_name=chat_name,
        wx_id=wx_id,
        keep_updated_at=True,
    )
    return store._state_to_json_payload(normalized)


def merge_chat_memory_files(base_dir: str | Path, wx_id: str, old_chat_name: str, new_chat_name: str) -> dict[str, Any]:
    old_file = chat_memory_file(base_dir, wx_id, old_chat_name)
    new_file = chat_memory_file(base_dir, wx_id, new_chat_name)
    result = {
        "old_chat_name": old_chat_name,
        "new_chat_name": new_chat_name,
        "old_file": str(old_file),
        "new_file": str(new_file),
        "old_count": 0,
        "new_count": 0,
        "merged_count": 0,
        "deduped_count": 0,
        "changed": False,
    }
    if not old_file.exists():
        return result
    if not new_file.exists():
        new_file.parent.mkdir(parents=True, exist_ok=True)
        payload = _normalize_chat_memory_payload(_read_json_object(old_file), new_chat_name, wx_id)
        new_file.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        old_file.unlink()
        result["old_count"] = len(payload.get("memories") or [])
        result["merged_count"] = result["old_count"]
        result["changed"] = True
        return result

    old_payload = _normalize_chat_memory_payload(_read_json_object(old_file), old_chat_name, wx_id)
    new_payload = _normalize_chat_memory_payload(_read_json_object(new_file), new_chat_name, wx_id)
    old_items = old_payload.get("memories") if isinstance(old_payload.get("memories"), list) else []
    new_items = new_payload.get("memories") if isinstance(new_payload.get("memories"), list) else []
    result["old_count"] = len(old_items)
    result["new_count"] = len(new_items)
    by_content: dict[str, dict[str, Any]] = {}
    for item in [*new_items, *old_items]:
        if not isinstance(item, dict):
            continue
        key = "|".join([_clean(item.get("type") or item.get("category")), _clean(item.get("content"))])
        if not key.strip("|"):
            continue
        current = by_content.get(key)
        if current is None:
            by_content[key] = copy.deepcopy(item)
            continue
        incoming_rank = _importance_rank(item.get("importance", "中"))
        current_rank = _importance_rank(current.get("importance", "中"))
        if incoming_rank < current_rank or _clean(item.get("updated_at")) > _clean(current.get("updated_at")):
            by_content[key] = copy.deepcopy(item)
    merged_items = list(by_content.values())
    merged_items.sort(key=lambda item: (_importance_rank(item.get("importance", "中")), _clean(item.get("updated_at"))), reverse=False)
    normalized_items = []
    for index, item in enumerate(merged_items[:50], start=1):
        normalized = copy.deepcopy(item)
        normalized["id"] = f"M{index:02d}"
        normalized.setdefault("importance", "中")
        normalized_items.append(normalized)
    merged_payload = copy.deepcopy(new_payload or old_payload)
    merged_payload.update({
        "schema_version": 2,
        "wx_id": _clean(wx_id),
        "chat_name": _clean(new_chat_name),
        "updated_at": _now_text(),
        "memories": normalized_items,
    })
    new_file.write_text(json.dumps(merged_payload, ensure_ascii=False, indent=2), encoding="utf-8")
    old_file.unlink()
    result["merged_count"] = len(normalized_items)
    result["deduped_count"] = len(old_items) + len(new_items) - len(by_content)
    result["changed"] = True
    return result


CONFIG_LIST_FIELDS = (
    "listen_list",
    "global_blacklist",
    "chat_memory_exclude_list",
    "material_source_list",
    "ai_material_outreach_allowed_sources",
)
CONFIG_MAP_FIELDS = (
    "chat_prompt_map",
    "chat_api_map",
    "chat_tts_map",
    "material_source_pool_limit_map",
)
TASK_NAME_FILES = (
    ("tasks", "scheduled_message", "tasks.json"),
    ("tasks", "scheduled_message", "runtime.json"),
    ("tasks", "scheduled_message", "history.json"),
    ("tasks", "material_outreach", "tasks.json"),
    ("tasks", "material_outreach", "runtime.json"),
    ("tasks", "material_outreach", "history.json"),
    ("tasks", "moments", "tasks.json"),
    ("tasks", "moments", "runtime.json"),
    ("tasks", "moments", "history.json"),
    ("tasks", "custom_forward", "rules.json"),
)
RELATIONSHIP_SCAN_AREA = "relationship_scan"
RELATIONSHIP_SCAN_FILENAME = "relationships.json"


def _json_file(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


def _write_json_file(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = str(path) + ".tmp"
    with open(tmp_path, "w", encoding="utf-8") as file:
        json.dump(payload if isinstance(payload, dict) else {}, file, ensure_ascii=False, indent=4)
    os.replace(tmp_path, path)


def _merge_config_list(value: Any, old_chat_name: str, new_chat_name: str) -> tuple[list[Any], bool]:
    if not isinstance(value, list):
        return value, False
    changed = False
    result = []
    has_new = any(_clean(item) == new_chat_name for item in value)
    for item in value:
        text = _clean(item)
        if text == old_chat_name:
            changed = True
            if not has_new:
                result.append(new_chat_name)
                has_new = True
            continue
        result.append(item)
    return result, changed


def _merge_config_map(value: Any, old_chat_name: str, new_chat_name: str) -> tuple[dict[str, Any], bool]:
    if not isinstance(value, dict):
        return value, False
    if old_chat_name not in value:
        return value, False
    updated = dict(value)
    old_value = updated.pop(old_chat_name)
    if new_chat_name not in updated:
        updated[new_chat_name] = old_value
    return updated, True


def _replace_exact_string_values(value: Any, old_text: str, new_text: str) -> tuple[Any, int]:
    if isinstance(value, str):
        return (new_text, 1) if value == old_text else (value, 0)
    if isinstance(value, list):
        changed = 0
        updated = []
        for item in value:
            next_item, count = _replace_exact_string_values(item, old_text, new_text)
            changed += count
            updated.append(next_item)
        return updated, changed
    if isinstance(value, dict):
        changed = 0
        updated = {}
        for key, item in value.items():
            next_key = new_text if isinstance(key, str) and key == old_text else key
            if next_key != key:
                changed += 1
            next_item, count = _replace_exact_string_values(item, old_text, new_text)
            changed += count
            if next_key in updated and next_key != key:
                continue
            updated[next_key] = next_item
        return updated, changed
    return value, 0


def sync_contact_task_names(base_dir: str | Path, wx_id: str, old_chat_name: str, new_chat_name: str) -> dict[str, Any]:
    old_chat_name = _clean(old_chat_name)
    new_chat_name = _clean(new_chat_name)
    result = {
        "old_chat_name": old_chat_name,
        "new_chat_name": new_chat_name,
        "changed": False,
        "files": [],
    }
    if not old_chat_name or not new_chat_name or old_chat_name == new_chat_name:
        return result
    account_root = account_area_dir(base_dir, wx_id, "")
    for parts in TASK_NAME_FILES:
        path = account_root.joinpath(*parts)
        if not path.exists() or not path.is_file():
            continue
        try:
            raw = json.loads(path.read_text(encoding="utf-8-sig"))
        except Exception:
            continue
        updated, count = _replace_exact_string_values(raw, old_chat_name, new_chat_name)
        if count <= 0:
            continue
        if isinstance(updated, dict):
            _write_json_file(path, updated)
        else:
            tmp_path = str(path) + ".tmp"
            with open(tmp_path, "w", encoding="utf-8") as file:
                json.dump(updated, file, ensure_ascii=False, indent=4)
            os.replace(tmp_path, path)
        result["files"].append({"path": str(path), "replace_count": count})
        result["changed"] = True
    return result


def _merge_reply_count_user(old_data: dict[str, Any], new_data: dict[str, Any]) -> dict[str, Any]:
    old_data = old_data if isinstance(old_data, dict) else {}
    new_data = new_data if isinstance(new_data, dict) else {}
    merged = dict(new_data)
    try:
        merged["count"] = max(0, int(old_data.get("count", 0) or 0)) + max(0, int(new_data.get("count", 0) or 0))
    except Exception:
        counts = []
        for item in (old_data, new_data):
            value = item.get("count", 0)
            counts.append(int(value) if str(value).isdigit() else 0)
        merged["count"] = sum(counts)
    for field in (
        "api_err_notified",
        "limit_notified",
        "meta_reply_blocked_notified",
    ):
        merged[field] = bool(old_data.get(field)) or bool(new_data.get(field))
    for field in ("window_started_at",):
        old_value = _clean(old_data.get(field))
        new_value = _clean(new_data.get(field))
        merged[field] = min(value for value in (old_value, new_value) if value) if (old_value or new_value) else ""
    return merged


def sync_contact_config_names(base_dir: str | Path, old_chat_name: str, new_chat_name: str) -> dict[str, Any]:
    old_chat_name = _clean(old_chat_name)
    new_chat_name = _clean(new_chat_name)
    result = {
        "old_chat_name": old_chat_name,
        "new_chat_name": new_chat_name,
        "config_changed": False,
        "reply_count_changed": False,
        "list_fields": [],
        "map_fields": [],
    }
    if not old_chat_name or not new_chat_name or old_chat_name == new_chat_name:
        return result

    config_path = Path(base_dir) / "config" / "config.json"
    config = _json_file(config_path)
    if config:
        for field in CONFIG_LIST_FIELDS:
            updated, changed = _merge_config_list(config.get(field), old_chat_name, new_chat_name)
            if changed:
                config[field] = updated
                result["list_fields"].append(field)
        for field in CONFIG_MAP_FIELDS:
            updated, changed = _merge_config_map(config.get(field), old_chat_name, new_chat_name)
            if changed:
                config[field] = updated
                result["map_fields"].append(field)
        if result["list_fields"] or result["map_fields"]:
            _write_json_file(config_path, config)
            result["config_changed"] = True

    reply_count_path = Path(base_dir) / "config" / "reply_count.json"
    reply_count = _json_file(reply_count_path)
    users = reply_count.get("users") if isinstance(reply_count.get("users"), dict) else {}
    if old_chat_name in users:
        old_data = users.pop(old_chat_name)
        if new_chat_name in users:
            users[new_chat_name] = _merge_reply_count_user(old_data, users.get(new_chat_name))
        else:
            users[new_chat_name] = old_data
        reply_count["users"] = users
        _write_json_file(reply_count_path, reply_count)
        result["reply_count_changed"] = True

    return result


def _latest_relationship_record(left: dict[str, Any], right: dict[str, Any]) -> dict[str, Any]:
    left_time = max(_clean(left.get("changed_at")), _clean(left.get("last_seen_at")))
    right_time = max(_clean(right.get("changed_at")), _clean(right.get("last_seen_at")))
    return left if left_time > right_time else right


def sync_relationship_scan_names(base_dir: str | Path, wx_id: str, old_chat_name: str, new_chat_name: str) -> dict[str, Any]:
    old_chat_name = _clean(old_chat_name)
    new_chat_name = _clean(new_chat_name)
    result = {
        "old_chat_name": old_chat_name,
        "new_chat_name": new_chat_name,
        "changed": False,
        "records_changed": 0,
        "events_changed": 0,
    }
    if not old_chat_name or not new_chat_name or old_chat_name == new_chat_name:
        return result
    path = account_area_dir(base_dir, wx_id, RELATIONSHIP_SCAN_AREA) / RELATIONSHIP_SCAN_FILENAME
    state = _json_file(path)
    if not state:
        return result

    records = state.get("records") if isinstance(state.get("records"), list) else []
    by_name: dict[str, dict[str, Any]] = {}
    for item in records:
        if not isinstance(item, dict):
            continue
        name = _clean(item.get("name"))
        if not name:
            continue
        by_name[name] = dict(item)
    if old_chat_name in by_name:
        old_record = by_name.pop(old_chat_name)
        if new_chat_name in by_name:
            target = by_name[new_chat_name]
            latest = _latest_relationship_record(old_record, target)
            for field in ("first_seen_at",):
                values = [value for value in (_clean(old_record.get(field)), _clean(target.get(field))) if value]
                if values:
                    target[field] = min(values)
            for field in ("last_seen_at", "changed_at"):
                values = [value for value in (_clean(old_record.get(field)), _clean(target.get(field))) if value]
                if values:
                    target[field] = max(values)
            for field in ("status", "previous_status", "evidence", "source", "wechat_sync_status", "wechat_sync_error", "wechat_synced_at", "wechat_sync_attempted_at", "wechat_sync_next_retry_at", "wechat_sync_retry_count"):
                if field in latest:
                    target[field] = latest.get(field)
            for field in ("contact_key",):
                if not _clean(target.get(field)) and _clean(old_record.get(field)):
                    target[field] = old_record.get(field)
        else:
            old_record["name"] = new_chat_name
            by_name[new_chat_name] = old_record
        state["records"] = list(by_name.values())
        result["records_changed"] = 1

    events = state.get("events") if isinstance(state.get("events"), list) else []
    for event in events:
        if isinstance(event, dict) and _clean(event.get("name")) == old_chat_name:
            event["name"] = new_chat_name
            result["events_changed"] += 1

    if result["records_changed"] or result["events_changed"]:
        state["updated_at"] = _now_text()
        _write_json_file(path, state)
        result["changed"] = True
    return result


def reconcile_contact_storage(
    base_dir: str | Path,
    wx_id: str,
    old_chat_name: str,
    new_chat_name: str,
    *,
    reason: str = "",
    backup_base: str | Path | None = None,
) -> dict[str, Any]:
    old_chat_name = _clean(old_chat_name)
    new_chat_name = _clean(new_chat_name)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    backup_root = Path(backup_base) if backup_base else account_area_dir(base_dir, wx_id, "contact_merge_backups", create=True)
    backup_root = backup_root / f"merged_{stamp}_{_short_hash(old_chat_name + '->' + new_chat_name, 6)}"
    paths = {
        "old_memory": memory_dir(base_dir, wx_id, old_chat_name),
        "new_memory": memory_dir(base_dir, wx_id, new_chat_name),
        "old_chat_memory": chat_memory_file(base_dir, wx_id, old_chat_name),
        "new_chat_memory": chat_memory_file(base_dir, wx_id, new_chat_name),
    }
    copied = backup_paths(paths, backup_root)
    memory_result = merge_memory_dirs(base_dir, wx_id, old_chat_name, new_chat_name)
    chat_memory_result = merge_chat_memory_files(base_dir, wx_id, old_chat_name, new_chat_name)
    config_result = sync_contact_config_names(base_dir, old_chat_name, new_chat_name)
    task_result = sync_contact_task_names(base_dir, wx_id, old_chat_name, new_chat_name)
    relationship_result = sync_relationship_scan_names(base_dir, wx_id, old_chat_name, new_chat_name)
    manifest = {
        "at": _now_text(),
        "wx_id": _clean(wx_id),
        "reason": _clean(reason),
        "old_chat_name": old_chat_name,
        "new_chat_name": new_chat_name,
        "backup_paths": copied,
        "memory": memory_result,
        "chat_memory": chat_memory_result,
        "config": config_result,
        "tasks": task_result,
        "relationship_scan": relationship_result,
    }
    backup_root.mkdir(parents=True, exist_ok=True)
    (backup_root / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    return manifest


def list_memory_chat_names(base_dir: str | Path, wx_id: str) -> list[str]:
    base = account_area_dir(base_dir, wx_id, "memory")
    if not base.exists():
        return []
    names = []
    for child in base.iterdir():
        if child.is_dir():
            names.append(read_memory_original_name(child, child.name))
    return sorted(set(name for name in names if _clean(name)))


def list_chat_memory_names(base_dir: str | Path, wx_id: str) -> list[str]:
    base = account_area_dir(base_dir, wx_id, "chat_memory")
    if not base.exists():
        return []
    names = []
    for child in base.iterdir():
        if child.is_file() and child.suffix == ".json":
            payload = _read_json_object(child)
            names.append(_clean(payload.get("chat_name")) or child.stem)
    return sorted(set(name for name in names if _clean(name)))
