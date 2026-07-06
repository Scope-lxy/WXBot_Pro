"""Material outreach rules and persistence helpers."""

from datetime import datetime, timedelta
import json
from pathlib import Path
import random
import re

from core.media import image_content_hash, is_image_path
from feature.material_outreach_preface import normalize_preface_pending_queue
from feature.task_workbench_runtime_summary import runtime_snapshot
from core.scheduled_tasks import (
    iter_enabled_tasks,
    normalize_fixed_task_schedule,
    normalize_random_task_schedule,
    plan_random_fire_time,
    prepare_random_task_day,
    repeat_rule_to_type,
)


FORWARDABLE_MATERIAL_TYPES = {
    "image",
    "video",
    "file",
    "location",
    "link",
    "emotion",
    "merge",
    "personal_card",
    "note",
    "miniapp",
}

FILTERABLE_MATERIAL_TYPES = {
    "image",
    "video",
    "file",
    "note",
    "location",
    "link",
    "miniapp",
    "personal_card",
    "merge",
}
PRIMARY_MATERIAL_BUCKETS = set(FILTERABLE_MATERIAL_TYPES)
DEFAULT_AI_PREFACE_GOAL = "日常问候"
MATERIAL_TYPE_LABELS = {
    "link": "链接",
    "miniapp": "小程序",
    "image": "图片",
    "video": "视频",
    "file": "文件",
    "note": "笔记",
    "location": "位置",
    "personal_card": "名片",
    "merge": "聊天记录",
}
POSITIVE_WECHAT_EMOJI_CODES = (
    "[微笑]",
    "[太阳]",
    "[礼物]",
    "[爱心]",
    "[庆祝]",
    "[强]",
    "[玫瑰]",
    "[愉快]",
    "[咖啡]",
    "[蛋糕]",
)
NON_MATERIAL_TYPES = {"text", "voice", "quote"}
DEFAULT_MATERIAL_POOL_LIMIT = 10
MAX_MATERIAL_POOL_LIMIT = 50
MAX_FORWARD_BATCH_SIZE = 9
DEFAULT_MATERIAL_OWNERSHIP = "我的作品"
MATERIAL_OWNERSHIP_VALUES = {"我的作品", "第三方作品"}
MATERIAL_STATUS_VALUES = {"active", "disabled"}
FORWARD_TEST_STATUS_VALUES = {"unknown", "success", "failed"}
MATERIAL_REPLACEMENT_METADATA_FIELDS = (
    "ownership",
    "copy_note",
    "status",
    "forward_test_status",
    "last_error",
)

PROGRESS_STATUS_LABELS = {
    "pending": "待转发",
    "success": "已转发",
    "failed": "转发失败",
    "skipped": "已跳过",
    "warning": "已提示",
}

PROGRESS_REASON_LABELS = {
    ("skipped", "cooldown"): "已跳过：冷却中",
    ("skipped", "exclude_tags"): "已跳过：命中排除标签",
    ("skipped", "missing_contact"): "已跳过：未找到通讯录联系人",
    ("skipped", "no_material"): "已跳过：没有可用素材",
    ("skipped", "fixed_material_missing"): "已跳过：没有可用素材",
    ("skipped", "fixed_material_unavailable"): "已跳过：没有可用素材",
    ("skipped", "limit"): "已跳过：本轮转发上限已达到",
    ("warning", "duplicate_send_name"): "已提示：发送名重复，已按排序发送第一个",
    ("warning", "send_name_unsearchable"): "已提示：昵称可能不可搜索，建议维护备注",
}

_RUN_ID_SAFE_RE = re.compile(r"[^0-9A-Za-z_.-]+")


class _AICandidateCard(dict):
    __slots__ = ("_ai_hidden",)

    def __init__(self, public=None, hidden=None):
        super().__init__(public or {})
        self._ai_hidden = dict(hidden or {})

    def get(self, key, default=None):
        if key == "stable_signature" and "_stable_signature" in self._ai_hidden:
            return self._ai_hidden.get("_stable_signature", default)
        if key == "material_id" and "_material_id" in self._ai_hidden:
            return self._ai_hidden.get("_material_id", default)
        if key == "material_title":
            return self._ai_hidden.get("_material_title", super().get("content_preview", default))
        if key == "material_type" and "_material_type" in self._ai_hidden:
            return self._ai_hidden.get("_material_type", default)
        if key == "material_source" and "_material_source" in self._ai_hidden:
            return self._ai_hidden.get("_material_source", default)
        if key in self._ai_hidden:
            return self._ai_hidden.get(key, default)
        return super().get(key, default)

    def __getitem__(self, key):
        if key == "stable_signature" and "_stable_signature" in self._ai_hidden:
            return self._ai_hidden["_stable_signature"]
        if key == "material_id" and "_material_id" in self._ai_hidden:
            return self._ai_hidden["_material_id"]
        if key == "material_title":
            return self._ai_hidden.get("_material_title", super().get("content_preview"))
        if key == "material_type" and "_material_type" in self._ai_hidden:
            return self._ai_hidden["_material_type"]
        if key == "material_source" and "_material_source" in self._ai_hidden:
            return self._ai_hidden["_material_source"]
        if key in self._ai_hidden:
            return self._ai_hidden[key]
        return super().__getitem__(key)


def coerce_material_pool_limit(value, default=DEFAULT_MATERIAL_POOL_LIMIT):
    try:
        value = int(value)
    except (TypeError, ValueError):
        value = default
    return max(1, min(MAX_MATERIAL_POOL_LIMIT, value))


def coerce_forward_batch_size(value, default=MAX_FORWARD_BATCH_SIZE):
    try:
        value = int(value)
    except (TypeError, ValueError):
        value = default
    return max(1, min(MAX_FORWARD_BATCH_SIZE, value))


def normalize_material_source_pool_limit_map(raw_map):
    if not isinstance(raw_map, dict):
        return {}
    cleaned = {}
    for source, value in raw_map.items():
        source = str(source or "").strip()
        if not source:
            continue
        cleaned[source] = coerce_material_pool_limit(value)
    return cleaned


def normalize_material_ownership(value):
    ownership = str(value or "").strip()
    if ownership in MATERIAL_OWNERSHIP_VALUES:
        return ownership
    return DEFAULT_MATERIAL_OWNERSHIP


def _normalize_material_copy_note(value):
    return str(value or "").strip()


def _normalize_material_status(value):
    status = str(value or "").strip().lower()
    if status in MATERIAL_STATUS_VALUES:
        return status
    return "active"


def _normalize_forward_test_status(value):
    status = str(value or "").strip().lower()
    if status in FORWARD_TEST_STATUS_VALUES:
        return status
    return "unknown"


def normalize_material_record(material):
    item = dict(material) if isinstance(material, dict) else {}
    if not item:
        return {}
    material_type = str(item.get("type") or "").strip()
    return {
        "id": str(item.get("id") or "").strip(),
        "source": str(item.get("source") or "").strip(),
        "type": material_type,
        "type_bucket": str(item.get("type_bucket") or material_type_bucket(material_type)).strip(),
        "content_preview": str(item.get("content_preview") or "").strip(),
        "stable_signature": str(item.get("stable_signature") or "").strip(),
        "created_at": str(item.get("created_at") or "").strip(),
        "status": _normalize_material_status(item.get("status")),
        "ownership": normalize_material_ownership(item.get("ownership")),
        "copy_note": _normalize_material_copy_note(item.get("copy_note")),
        "forward_test_status": _normalize_forward_test_status(item.get("forward_test_status")),
        "last_error": str(item.get("last_error") or "").strip(),
    }


def material_pool_limit_for_source(limit_map, source, *, default=DEFAULT_MATERIAL_POOL_LIMIT):
    limit_map = normalize_material_source_pool_limit_map(limit_map)
    source = str(source or "").strip()
    if source and source in limit_map:
        return limit_map[source]
    return coerce_material_pool_limit(default)


def message_type(message):
    return str(getattr(message, "type", "") or "").strip()


def is_forwardable_material_message(message):
    msg_type = message_type(message)
    return bool(msg_type and msg_type not in NON_MATERIAL_TYPES and msg_type in FORWARDABLE_MATERIAL_TYPES)


def material_type_bucket(msg_type):
    msg_type = str(msg_type or "").strip()
    return msg_type if msg_type in PRIMARY_MATERIAL_BUCKETS else "other"


def material_type_label(msg_type):
    msg_type = str(msg_type or "").strip()
    return MATERIAL_TYPE_LABELS.get(msg_type, msg_type)


def material_display_label(msg_type, title):
    msg_type = str(msg_type or "").strip()
    title = str(title or "").strip()
    if not msg_type and not title:
        return "无素材"
    type_label = material_type_label(msg_type)
    if msg_type == "link":
        return type_label or title or "无素材"
    if not title:
        return f"[{type_label}]" if type_label else "无素材"
    if type_label and (title.startswith(type_label) or title.startswith(f"[{type_label}]")):
        return title
    return f"[{type_label}] {title}" if type_label else title


def normalize_material_types(raw_types):
    if isinstance(raw_types, str):
        raw_types = [raw_types]
    if not isinstance(raw_types, list):
        raw_types = ["all"]
    clean_types = []
    for item in raw_types:
        item = str(item or "").strip()
        if not item:
            continue
        if item == "all":
            return ["all"]
        if item in FILTERABLE_MATERIAL_TYPES and item not in clean_types:
            clean_types.append(item)
    return clean_types or ["all"]


def _normalize_preface_text_lines(value):
    lines = []
    for raw_line in str(value or "").splitlines():
        line = raw_line.strip()
        if line:
            lines.append(line)
    return lines


def _coerce_preface_bool(value):
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "on", "yes"}
    return bool(value)


def _normalize_task_runtime_status(value):
    status = str(value or "").strip().lower()
    if status in {"pending", "running"}:
        return status
    return ""


def normalize_material_outreach_preface_config(raw_task):
    task = raw_task if isinstance(raw_task, dict) else {}
    raw_mode_text = str(task.get("preface_mode") or "").strip().lower()
    has_preface_mode = bool(raw_mode_text)
    raw_mode = raw_mode_text if raw_mode_text in {"none", "custom", "ai"} else ""
    if has_preface_mode and not raw_mode:
        mode = "none"
    else:
        mode = raw_mode or "ai"

    preface_text_lines = _normalize_preface_text_lines(task.get("preface_text", ""))
    preface_text = "\n".join(preface_text_lines)
    preface_random_emojis = _coerce_preface_bool(task.get("preface_random_emojis"))
    if mode == "custom" and not preface_text:
        mode = "none"
    if mode != "custom":
        preface_random_emojis = False

    failure_mode = str(task.get("ai_preface_failure_mode") or "").strip().lower()
    if failure_mode not in {"send_without_preface", "skip_target"}:
        failure_mode = "send_without_preface"

    return {
        "preface_mode": mode,
        "preface_text": preface_text,
        "preface_random_emojis": preface_random_emojis,
        "ai_preface_goal": str(task.get("ai_preface_goal") or "").strip() or DEFAULT_AI_PREFACE_GOAL,
        "ai_preface_intensity": str(task.get("ai_preface_intensity") or "").strip().lower(),
        "ai_preface_extra_instruction": str(task.get("ai_preface_extra_instruction") or "").strip(),
        "ai_preface_failure_mode": failure_mode,
    }


def build_custom_material_preface(text, *, random_emojis=False, sample=None):
    lines = _normalize_preface_text_lines(text)
    if not lines:
        return ""
    sample = sample or random.sample
    selected = sample(lines, 1)[0]
    if not random_emojis:
        return selected
    emojis = sample(list(POSITIVE_WECHAT_EMOJI_CODES), 2)
    return f"{selected}{''.join(emojis)}"


def _hhmm_to_minutes(value):
    try:
        hour, minute = map(int, str(value or "").split(":"))
    except ValueError:
        return None
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        return None
    return hour * 60 + minute


def material_random_time_window(start_stop_enabled, start_time, stop_time, *, now=None):
    """Return the daily random outreach window derived from bot running settings."""
    if not start_stop_enabled:
        return "00:00", "23:59"
    now = now or datetime.now()
    start_mins = _hhmm_to_minutes(start_time)
    stop_mins = _hhmm_to_minutes(stop_time)
    if start_mins is None or stop_mins is None or start_mins == stop_mins:
        return "00:00", "23:59"
    start_time = str(start_time)
    stop_time = str(stop_time)
    now_mins = now.hour * 60 + now.minute
    if start_mins < stop_mins:
        if now_mins > stop_mins:
            return None, None
        return start_time, stop_time

    if now_mins <= stop_mins:
        return "00:00", stop_time
    if now_mins < start_mins:
        return None, None
    return start_time, "23:59"


def _preview(value, max_chars=80):
    text = str(value or "").strip().replace("\r", " ").replace("\n", " ")
    return text[:max_chars]


def build_stable_material_signature(message, *, msg_type=""):
    msg_type = str(msg_type or message_type(message) or "").strip()
    content = str(getattr(message, "content", "") or "").strip()
    if msg_type == "image" and is_image_path(content):
        digest = image_content_hash(content)
        if digest:
            return f"{msg_type}|{digest}"
    preview = _preview(content, max_chars=120) or msg_type or "material"
    return f"{msg_type}|{preview}"


def build_material_entry(material_id, source, message, *, now=None):
    now = now or datetime.now()
    msg_type = message_type(message)
    return {
        "id": str(material_id),
        "source": str(source or ""),
        "type": msg_type,
        "type_bucket": material_type_bucket(msg_type),
        "content_preview": _preview(getattr(message, "content", "")),
        "stable_signature": build_stable_material_signature(message, msg_type=msg_type),
        "created_at": now.replace(microsecond=0).isoformat(),
        "status": "active",
        "ownership": DEFAULT_MATERIAL_OWNERSHIP,
        "copy_note": "",
        "forward_test_status": "unknown",
        "last_error": "",
    }


def _material_signature(material):
    return str((material or {}).get("stable_signature") or "").strip()


def _material_source(material):
    return str((material or {}).get("source") or "").strip()


def _copy_replaced_material_metadata(entry, matched):
    matched = matched if isinstance(matched, dict) else {}
    if not matched:
        return entry
    matched_id = str(matched.get("id") or "").strip()
    if matched_id:
        entry["id"] = matched_id
    for field in MATERIAL_REPLACEMENT_METADATA_FIELDS:
        if field not in matched:
            continue
        if field == "ownership":
            entry[field] = normalize_material_ownership(matched.get(field))
        elif field == "status":
            entry[field] = _normalize_material_status(matched.get(field))
        elif field == "forward_test_status":
            entry[field] = _normalize_forward_test_status(matched.get(field))
        elif field == "copy_note":
            entry[field] = str(matched.get(field) or "").strip()
        else:
            entry[field] = str(matched.get(field) or "").strip()
    return entry


def trim_material_pool_by_source(materials, *, limit_per_source=DEFAULT_MATERIAL_POOL_LIMIT, limit_map=None):
    default_limit = coerce_material_pool_limit(limit_per_source)
    source_limits = normalize_material_source_pool_limit_map(limit_map)
    kept_reversed = []
    counts = {}
    for item in reversed(list(materials or [])):
        source = str((item or {}).get("source") or "")
        limit = source_limits.get(source, default_limit)
        count = counts.get(source, 0)
        if count >= limit:
            continue
        kept_reversed.append(item)
        counts[source] = count + 1
    return list(reversed(kept_reversed))


def append_material_to_pool(materials, source, message, *, material_id, limit_map=None, now=None):
    entry = build_material_entry(material_id, source, message, now=now)
    source = str(source or "").strip()
    signature = _material_signature(entry)
    materials = list(materials or [])
    retained = []
    matched = None
    for item in materials:
        if (
            signature
            and _material_source(item) == source
            and _material_signature(item) == signature
        ):
            matched = item
            continue
        retained.append(item)
    if matched:
        entry = _copy_replaced_material_metadata(entry, matched)
    materials = retained
    materials.append(entry)
    return trim_material_pool_by_source(materials, limit_map=limit_map), entry


def collect_material_source_message(materials, source, message, *, material_id_factory, limit_map=None, now=None):
    if not is_forwardable_material_message(message):
        return list(materials or []), None, ""
    material_id = material_id_factory()
    pool, entry = append_material_to_pool(
        materials,
        source,
        message,
        material_id=material_id,
        limit_map=limit_map,
        now=now,
    )
    return pool, entry, str((entry or {}).get("id") or material_id)


def rebuild_material_pool_for_source(
    materials,
    source,
    messages,
    *,
    limit,
    limit_map=None,
    material_id_factory,
    now=None,
):
    source = str(source or "").strip()
    limit = coerce_material_pool_limit(limit)
    all_existing = list(materials or [])
    existing = [item for item in all_existing if item.get("source") != source]
    source_existing_by_signature = {}
    for item in all_existing:
        if item.get("source") != source:
            continue
        signature = str(item.get("stable_signature") or "").strip()
        if signature:
            source_existing_by_signature[signature] = item
    rebuilt_by_signature = {}
    runtime_messages = {}
    for message in messages or []:
        if not is_forwardable_material_message(message):
            continue
        material_id = material_id_factory()
        entry = build_material_entry(material_id, source, message, now=now)
        matched = source_existing_by_signature.get(entry.get("stable_signature"))
        if matched:
            entry = _copy_replaced_material_metadata(entry, matched)
            material_id = str(entry.get("id") or material_id).strip()
        signature = _material_signature(entry) or str(material_id)
        if signature in rebuilt_by_signature:
            rebuilt_by_signature.pop(signature, None)
        rebuilt_by_signature[signature] = (entry, message)
    rebuilt_pairs = list(rebuilt_by_signature.values())[-limit:]
    rebuilt = []
    for entry, message in rebuilt_pairs:
        rebuilt.append(entry)
        runtime_messages[str(entry.get("id") or "")] = message
    pool = trim_material_pool_by_source(existing + rebuilt, limit_map=limit_map)
    return pool, runtime_messages, rebuilt


def material_title(material, max_chars=120):
    return _preview((material or {}).get("content_preview", ""), max_chars=max_chars)


def build_ai_candidate_material_cards(materials):
    cards = []
    index = 0
    for material in materials or []:
        material = normalize_material_record(material)
        if not material:
            continue
        if str(material.get("status", "active") or "active").strip() != "active":
            continue
        index += 1
        cards.append(
            _AICandidateCard(
                {
                    "index": index,
                    "type": str(material.get("type_bucket") or material.get("type") or ""),
                    "content_preview": material_title(material),
                    "ownership": normalize_material_ownership(material.get("ownership")),
                    "copy_note": _normalize_material_copy_note(material.get("copy_note")),
                },
                {
                    "_stable_signature": str(material.get("stable_signature") or ""),
                    "_material_id": str(material.get("id") or ""),
                    "_material_title": material_title(material),
                    "_material_source": str(material.get("source") or ""),
                    "_material_type": str(material.get("type_bucket") or material.get("type") or ""),
                },
            )
        )
    return cards


def _parse_dt(value):
    try:
        return datetime.fromisoformat(str(value or ""))
    except ValueError:
        return None


def is_target_in_cooldown(send_records, target, cooldown_hours, *, now=None):
    if not cooldown_hours:
        return False
    now = now or datetime.now()
    cutoff = now - timedelta(hours=max(0, int(cooldown_hours)))
    for record in send_records or []:
        if record.get("target") != target or not record.get("success"):
            continue
        sent_at = _parse_dt(record.get("sent_at"))
        if sent_at and sent_at > cutoff:
            return True
    return False


def _safe_run_id_component(value):
    text = str(value or "").strip() or "task"
    text = _RUN_ID_SAFE_RE.sub("_", text).strip("._")
    return text or "task"


def build_outreach_run_id(task_id, now=None):
    now = now or datetime.now()
    return f"outreach_{_safe_run_id_component(task_id)}_{now.strftime('%Y%m%d_%H%M%S')}"


def _progress_status_label(status, reason):
    status = str(status or "").strip()
    reason = str(reason or "").strip()
    return PROGRESS_REASON_LABELS.get((status, reason)) or PROGRESS_STATUS_LABELS.get(status, status or "待转发")


def _snapshot_contact(contact):
    contact = contact or {}
    return {
        "contact_key": str(contact.get("contact_key") or ""),
        "send_name": str(contact.get("send_name") or ""),
        "display_name": str(contact.get("display_name") or contact.get("send_name") or ""),
        "tags": list(contact.get("tags") or []),
        "warnings": list(contact.get("warnings") or []),
    }


def _record_snapshot_fields(
    *,
    raw_targets=None,
    raw_messages=None,
    raw_media=None,
    raw_material=None,
    targets_summary="",
    content_summary="",
    media_summary="",
    material_summary="",
    batch_id="",
    run_id="",
):
    return runtime_snapshot(
        raw_targets=raw_targets,
        raw_messages=raw_messages,
        raw_media=raw_media,
        raw_material=raw_material,
        targets_summary=targets_summary,
        content_summary=content_summary,
        media_summary=media_summary,
        material_summary=material_summary,
        batch_id=batch_id,
        run_id=run_id,
    )


def _record_contact_fields(target, raw_targets):
    target_text = str(target or "").strip()
    matched = {}
    for item in raw_targets or []:
        if not isinstance(item, dict):
            continue
        item_target = str(item.get("target") or "").strip()
        display_name = str(item.get("display_name") or "").strip()
        send_name = str(item.get("send_name") or "").strip()
        if target_text and target_text not in {item_target, display_name, send_name}:
            continue
        matched = item
        break
    return {
        "contact_key": str(matched.get("contact_key") or ""),
        "send_name": str(matched.get("send_name") or matched.get("display_name") or matched.get("target") or target_text),
        "display_name": str(matched.get("display_name") or matched.get("send_name") or matched.get("target") or target_text),
    }


def build_progress_record(
    run_id,
    task_id,
    contact,
    status,
    *,
    reason="",
    detail="",
    now=None,
    targets_summary="",
    content_summary="",
    media_summary="",
    material_summary="",
    raw_targets=None,
    raw_messages=None,
    raw_media=None,
    raw_material=None,
    batch_id="",
):
    now = now or datetime.now()
    contact = _snapshot_contact(contact)
    status = str(status or "pending").strip() or "pending"
    reason = str(reason or "").strip()
    snapshot = _record_snapshot_fields(
        raw_targets=raw_targets if raw_targets is not None else [contact],
        raw_messages=raw_messages,
        raw_media=raw_media,
        raw_material=raw_material,
        targets_summary=targets_summary,
        content_summary=content_summary,
        media_summary=media_summary,
        material_summary=material_summary,
        batch_id=batch_id or run_id or task_id,
        run_id=run_id,
    )
    return {
        "run_id": str(run_id or ""),
        "task_id": str(task_id or ""),
        "contact_key": contact["contact_key"],
        "send_name": contact["send_name"],
        "display_name": contact["display_name"],
        "status": status,
        "status_label": _progress_status_label(status, reason),
        "reason": reason,
        "detail": str(detail or ""),
        "warnings": contact["warnings"],
        "created_at": now.replace(microsecond=0).isoformat(),
        **snapshot,
    }


def build_target_snapshot(task, resolved_targets, *, now=None):
    now = now or datetime.now()
    task = task or {}
    resolved_targets = resolved_targets or {}
    task_id = str(task.get("task_id") or task.get("id") or "")
    run_id = build_outreach_run_id(task_id, now=now)
    send_records = task.get("send_records") or []
    cooldown_hours = task.get("cooldown_hours", 0)

    targets = []
    pending_targets = []
    progress_records = []
    for contact in resolved_targets.get("selected") or []:
        snapshot_contact = _snapshot_contact(contact)
        targets.append(snapshot_contact)

        if is_target_in_cooldown(send_records, snapshot_contact["send_name"], cooldown_hours, now=now):
            progress_records.append(
                build_progress_record(
                    run_id,
                    task_id,
                    snapshot_contact,
                    "skipped",
                    reason="cooldown",
                    detail="同好友冷却未结束",
                    now=now,
                )
            )
            continue

        for warning in snapshot_contact["warnings"]:
            if warning in {"duplicate_send_name", "send_name_unsearchable"}:
                progress_records.append(
                    build_progress_record(run_id, task_id, snapshot_contact, "warning", reason=warning, now=now)
                )
        progress_records.append(build_progress_record(run_id, task_id, snapshot_contact, "pending", now=now))
        pending_targets.append(snapshot_contact)

    for excluded in resolved_targets.get("excluded") or []:
        contact = excluded.get("contact") if isinstance(excluded, dict) else None
        if not contact:
            continue
        reason = str(excluded.get("reason") or "missing_contact")
        progress_records.append(
            build_progress_record(run_id, task_id, contact, "skipped", reason=reason, now=now)
        )

    snapshot = runtime_snapshot(
        raw_targets=targets,
        batch_id=run_id or task_id,
        run_id=run_id,
    )
    return {
        "run_id": run_id,
        "task_id": task_id,
        "created_at": now.replace(microsecond=0).isoformat(),
        "targets": targets,
        "pending_targets": pending_targets,
        "progress_records": progress_records,
        **snapshot,
        "stats": {
            "targets": len(targets),
            "pending": len(pending_targets),
            "progress_records": len(progress_records),
        },
    }


def send_names_from_target_snapshot(snapshot):
    """Return stable, de-duplicated send names for the current outreach run."""
    send_names = []
    seen = set()
    for contact in (snapshot or {}).get("pending_targets") or []:
        send_name = str(contact.get("send_name") or "").strip()
        if not send_name or send_name in seen:
            continue
        seen.add(send_name)
        send_names.append(send_name)
    return send_names


def retryable_progress_targets(progress_records):
    latest_by_key = {}
    for record in progress_records or []:
        key = str(record.get("contact_key") or record.get("send_name") or "")
        if key:
            latest_by_key[key] = record
    retryable = []
    for record in progress_records or []:
        key = str(record.get("contact_key") or record.get("send_name") or "")
        if not key or latest_by_key.get(key) is not record:
            continue
        status = str(record.get("status") or "")
        reason = str(record.get("reason") or "")
        if status in {"pending", "failed"} or (status == "skipped" and reason in {"unknown", "missing_contact"}):
            retryable.append(record)
    return retryable


def _material_sent_to_target(send_records, material_id, target):
    return any(
        record.get("material_id") == material_id and record.get("target") == target and record.get("success")
        for record in send_records or []
    )


def select_material_for_target(
    materials,
    *,
    runtime_material_ids,
    send_records,
    target,
    allowed_buckets=None,
    choice=None,
):
    choice = choice or random.choice
    allowed = set(normalize_material_types(allowed_buckets))
    runtime_ids = set(runtime_material_ids or [])
    candidates = []
    for material in materials or []:
        material_id = material.get("id")
        if material.get("status", "active") != "active":
            continue
        if material_id not in runtime_ids:
            continue
        bucket = material.get("type_bucket") or material_type_bucket(material.get("type"))
        if "all" not in allowed and bucket not in allowed:
            continue
        candidates.append(material)
    if not candidates:
        return None
    unsent = [item for item in candidates if not _material_sent_to_target(send_records, item.get("id"), target)]
    return choice(unsent or candidates)


def chunk_targets_for_forward(targets, *, min_size=MAX_FORWARD_BATCH_SIZE, max_size=MAX_FORWARD_BATCH_SIZE, randint=None):
    randint = randint or random.randint
    min_size = coerce_forward_batch_size(min_size)
    max_size = coerce_forward_batch_size(max_size)
    if min_size > max_size:
        min_size, max_size = max_size, min_size
    chunks = []
    index = 0
    targets = list(targets or [])
    while index < len(targets):
        size = randint(min_size, max_size)
        size = coerce_forward_batch_size(size)
        chunk = targets[index:index + size]
        if not chunk:
            break
        chunks.append(chunk)
        index += size
    return chunks


def _select_material_for_targets(
    materials,
    *,
    runtime_material_ids,
    send_records,
    targets,
    allowed_buckets=None,
    choice=None,
    preferred_material_id="",
    source_filter="",
    require_preferred=False,
):
    choice = choice or random.choice
    allowed = set(normalize_material_types(allowed_buckets))
    runtime_ids = set(runtime_material_ids or [])
    source_filter = str(source_filter or "").strip()
    candidates = []
    for material in materials or []:
        material_id = material.get("id")
        if material.get("status", "active") != "active":
            continue
        if material_id not in runtime_ids:
            continue
        if source_filter and str(material.get("source") or "").strip() != source_filter:
            continue
        bucket = material.get("type_bucket") or material_type_bucket(material.get("type"))
        if "all" not in allowed and bucket not in allowed:
            continue
        candidates.append(material)
    if not candidates:
        return None
    if preferred_material_id:
        for material in candidates:
            if material.get("id") == preferred_material_id:
                return material
        if require_preferred:
            return None
    unsent = [
        item for item in candidates
        if any(not _material_sent_to_target(send_records, item.get("id"), target) for target in targets or [])
    ]
    return choice(unsent or candidates)


def plan_material_outreach_batches(
    task,
    materials,
    send_records,
    runtime_material_ids,
    *,
    now=None,
    choice=None,
    randint=None,
    sample=None,
):
    now = now or datetime.now()
    send_actions = []
    skip_records = []
    preface_config = normalize_material_outreach_preface_config(task)
    preface_mode = preface_config.get("preface_mode", "none")
    eligible_targets = []
    for target in task.get("targets", []) or []:
        if is_target_in_cooldown(send_records, target, task.get("cooldown_hours", 0), now=now):
            skip_records.append(build_skip_record(task.get("task_id"), target, "cooldown", "同好友冷却未结束", now=now))
            continue
        eligible_targets.append(target)

    strategy = normalize_batch_material_strategy(task.get("batch_material_strategy") or "per_batch")
    fixed_material_id = str(task.get("fixed_material_id") or task.get("material_id") or "")
    run_material = None
    if strategy == "fixed" and not fixed_material_id:
        for target in eligible_targets:
            skip_records.append(
                build_skip_record(task.get("task_id"), target, "fixed_material_missing", "固定素材模式未指定素材", now=now)
            )
        return {"send": [], "skip": skip_records}
    if strategy in {"per_task", "fixed"}:
        run_material = _select_material_for_targets(
            materials,
            runtime_material_ids=runtime_material_ids,
            send_records=send_records,
            targets=eligible_targets,
            allowed_buckets=task.get("material_types", ["all"]),
            choice=choice,
            preferred_material_id=fixed_material_id,
            source_filter=task.get("material_source_filter", ""),
            require_preferred=(strategy == "fixed"),
        )
        if not run_material and eligible_targets:
            detail = "指定素材当前不可用" if strategy == "fixed" and fixed_material_id else "没有可用素材"
            reason = "fixed_material_unavailable" if strategy == "fixed" and fixed_material_id else "no_material"
            for target in eligible_targets:
                skip_records.append(build_skip_record(task.get("task_id"), target, reason, detail, now=now))
            return {"send": [], "skip": skip_records}

    if preface_mode == "ai":
        # AI 模式下每个单目标 action 都视为一个独立 batch。
        # 因此 per_batch 在这里自然退化为 per-target 选材。
        for target in eligible_targets:
            material = run_material
            if material is None:
                material = _select_material_for_targets(
                    materials,
                    runtime_material_ids=runtime_material_ids,
                    send_records=send_records,
                    targets=[target],
                    allowed_buckets=task.get("material_types", ["all"]),
                    choice=choice,
                    preferred_material_id=fixed_material_id,
                    source_filter=task.get("material_source_filter", ""),
                    require_preferred=(strategy == "fixed"),
                )
            if not material:
                reason = "fixed_material_unavailable" if strategy == "fixed" and fixed_material_id else "no_material"
                detail = "指定素材当前不可用" if reason == "fixed_material_unavailable" else "没有可用素材"
                skip_records.append(build_skip_record(task.get("task_id"), target, reason, detail, now=now))
                continue
            send_actions.append({"mode": "ai_preface", "target": target, "material": material})
        return {"send": send_actions, "skip": skip_records}

    batch_size = coerce_forward_batch_size(task.get("batch_size_fixed", MAX_FORWARD_BATCH_SIZE))
    chunks = chunk_targets_for_forward(
        eligible_targets,
        min_size=batch_size,
        max_size=batch_size,
        randint=randint,
    )
    for targets in chunks:
        material = run_material
        if material is None:
            material = _select_material_for_targets(
                materials,
                runtime_material_ids=runtime_material_ids,
                send_records=send_records,
                targets=targets,
                allowed_buckets=task.get("material_types", ["all"]),
                choice=choice,
                preferred_material_id=fixed_material_id,
                source_filter=task.get("material_source_filter", ""),
                require_preferred=(strategy == "fixed"),
            )
        if not material:
            for target in targets:
                skip_records.append(build_skip_record(task.get("task_id"), target, "no_material", "没有可用素材", now=now))
            continue
        preface = ""
        if preface_mode == "custom":
            preface = build_custom_material_preface(
                preface_config.get("preface_text", ""),
                random_emojis=preface_config.get("preface_random_emojis", False),
                sample=sample,
            )
        send_actions.append({"targets": targets, "material": material, "preface": preface})
    return {"send": send_actions, "skip": skip_records}


def build_send_record(
    task_id,
    material_id,
    material_type,
    target,
    success,
    *,
    now=None,
    error="",
    preface="",
    material_title="",
    material_source="",
    batch_id="",
    stable_signature="",
    task_name="",
    run_id="",
    targets_summary="",
    content_summary="",
    media_summary="",
    material_summary="",
    raw_targets=None,
    raw_messages=None,
    raw_media=None,
    raw_material=None,
):
    now = now or datetime.now()
    contact_fields = _record_contact_fields(target, raw_targets)
    snapshot = _record_snapshot_fields(
        raw_targets=raw_targets,
        raw_messages=raw_messages,
        raw_media=raw_media,
        raw_material=raw_material,
        targets_summary=targets_summary,
        content_summary=content_summary,
        media_summary=media_summary,
        material_summary=material_summary,
        batch_id=batch_id or run_id or task_id,
        run_id=run_id,
    )
    return {
        "task_id": str(task_id or ""),
        "task_name": str(task_name or ""),
        "material_id": str(material_id or ""),
        "material_type": str(material_type or ""),
        "material_title": str(material_title or ""),
        "material_source": str(material_source or ""),
        "stable_signature": str(stable_signature or ""),
        "target": str(target or ""),
        "contact_key": contact_fields["contact_key"],
        "send_name": contact_fields["send_name"],
        "display_name": contact_fields["display_name"],
        "sent_at": now.replace(microsecond=0).isoformat(),
        "success": bool(success),
        "error": str(error or ""),
        "preface": str(preface or ""),
        "batch_id": str(batch_id or ""),
        "run_id": str(run_id or ""),
        **snapshot,
    }


def build_skip_record(
    task_id,
    target,
    reason,
    detail,
    *,
    now=None,
    material_title="",
    material_type="",
    material_id="",
    batch_id="",
    run_id="",
    targets_summary="",
    content_summary="",
    media_summary="",
    material_summary="",
    raw_targets=None,
    raw_messages=None,
    raw_media=None,
    raw_material=None,
):
    now = now or datetime.now()
    contact_fields = _record_contact_fields(target, raw_targets)
    snapshot = _record_snapshot_fields(
        raw_targets=raw_targets,
        raw_messages=raw_messages,
        raw_media=raw_media,
        raw_material=raw_material,
        targets_summary=targets_summary,
        content_summary=content_summary,
        media_summary=media_summary,
        material_summary=material_summary,
        batch_id=batch_id or run_id or task_id,
        run_id=run_id,
    )
    return {
        "task_id": str(task_id or ""),
        "material_id": str(material_id or ""),
        "material_type": str(material_type or ""),
        "material_title": str(material_title or ""),
        "target": str(target or ""),
        "contact_key": contact_fields["contact_key"],
        "send_name": contact_fields["send_name"],
        "display_name": contact_fields["display_name"],
        "reason": str(reason or ""),
        "created_at": now.replace(microsecond=0).isoformat(),
        "detail": str(detail or ""),
        "batch_id": str(batch_id or ""),
        "run_id": str(run_id or ""),
        **snapshot,
    }


def is_forward_result_success(result):
    if result is None:
        return True, ""
    if result is False:
        return False, ""
    if isinstance(result, dict):
        status = str(result.get("status", "") or "").strip().lower()
        message = str(result.get("message", "") or "")
        if status in {"失败", "错误", "failed", "failure", "error"}:
            return False, message
        if status in {"成功", "success", "ok"}:
            return True, message
    return bool(result), ""


def material_outreach_timeline(send_records, skip_records, *, progress_records=None, limit=20):
    if progress_records:
        records = []
        for record in progress_records or []:
            records.append({
                "time": record.get("created_at") or "",
                "task_id": record.get("task_id") or "",
                "target": record.get("display_name") or record.get("send_name") or "",
                "material_type": record.get("material_type") or "",
                "material_title": record.get("material_title") or "",
                "material_source": record.get("material_source") or "",
                "status": record.get("status") or "",
                "status_label": record.get("status_label") or _progress_status_label(record.get("status"), record.get("reason")),
                "detail": record.get("detail") or record.get("reason") or "",
            })
        records.sort(key=lambda item: item.get("time") or "", reverse=True)
        return records[:limit]

    records = []
    for record in send_records or []:
        success = bool(record.get("success"))
        records.append({
            "time": record.get("sent_at") or "",
            "task_id": record.get("task_id") or "",
            "target": record.get("target") or "",
            "material_type": record.get("material_type") or "",
            "material_title": record.get("material_title") or "",
            "material_source": record.get("material_source") or "",
            "status": "success" if success else "failed",
            "status_label": "成功" if success else "失败",
            "detail": record.get("error") or "",
        })
    for record in skip_records or []:
        records.append({
            "time": record.get("created_at") or "",
            "task_id": record.get("task_id") or "",
            "target": record.get("target") or "",
            "material_type": record.get("material_type") or "",
            "material_title": record.get("material_title") or "",
            "status": "skipped",
            "status_label": "跳过",
            "detail": record.get("detail") or record.get("reason") or "",
        })
    records.sort(key=lambda item: item.get("time") or "", reverse=True)
    return records[:limit]


def load_json_list(path):
    path = Path(path)
    if not path.exists():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return []
    return data if isinstance(data, list) else []


def save_json_list(path, items):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(list(items or []), ensure_ascii=False, indent=2), encoding="utf-8")


def load_json_object(path):
    path = Path(path)
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


def save_json_object(path, payload):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload if isinstance(payload, dict) else {}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def normalize_material_outreach_runtime_payload(raw):
    from feature.ai_material_outreach import (
        normalize_ai_detection_state,
    )

    raw = raw if isinstance(raw, dict) else {}
    pending_queue = [item for item in (raw.get("ai_pending_queue") or []) if isinstance(item, dict)]
    preface_pending_queue = normalize_preface_pending_queue(raw.get("preface_pending_queue") or [])
    return {
        "ai_pending_queue": pending_queue,
        "preface_pending_queue": preface_pending_queue,
        "ai_detection_state": normalize_ai_detection_state(raw.get("ai_detection_state")),
    }


def normalize_material_outreach_history_payload(raw):
    raw = raw if isinstance(raw, dict) else {}
    return {
        "send_records": [item for item in (raw.get("send_records") or []) if isinstance(item, dict)],
        "skip_records": [item for item in (raw.get("skip_records") or []) if isinstance(item, dict)],
        "progress_records": [item for item in (raw.get("progress_records") or []) if isinstance(item, dict)],
    }


def append_bounded_record(path, record, *, limit=1000):
    items = load_json_list(path)
    items.append(record)
    if limit and len(items) > limit:
        items = items[-int(limit):]
    save_json_list(path, items)
    return items


def append_progress_records(path, records, *, limit=1000):
    items = load_json_list(path)
    for record in records or []:
        if isinstance(record, dict):
            items.append(record)
    if limit and len(items) > limit:
        items = items[-int(limit):]
    save_json_list(path, items)
    return items


def _snapshot_contact_by_send_name(snapshot):
    by_name = {}
    for contact in (snapshot or {}).get("targets") or []:
        send_name = str(contact.get("send_name") or "").strip()
        if send_name and send_name not in by_name:
            by_name[send_name] = contact
    return by_name


def update_progress_records_for_send(path, snapshot, targets, *, success, error="", now=None, limit=1000):
    by_name = _snapshot_contact_by_send_name(snapshot)
    run_id = (snapshot or {}).get("run_id") or ""
    task_id = (snapshot or {}).get("task_id") or ""
    status = "success" if success else "failed"
    progress = []
    for target in targets or []:
        send_name = str(target or "").strip()
        if not send_name:
            continue
        contact = by_name.get(send_name) or {
            "contact_key": "",
            "send_name": send_name,
            "display_name": send_name,
            "warnings": [],
        }
        progress.append(build_progress_record(run_id, task_id, contact, status, detail=error, now=now))
        progress[-1].update(
            _record_snapshot_fields(
                raw_targets=(snapshot or {}).get("raw_targets"),
                raw_messages=(snapshot or {}).get("raw_messages"),
                raw_media=(snapshot or {}).get("raw_media"),
                raw_material=(snapshot or {}).get("raw_material"),
                targets_summary=(snapshot or {}).get("targets_summary", ""),
                content_summary=(snapshot or {}).get("content_summary", ""),
                media_summary=(snapshot or {}).get("media_summary", ""),
                material_summary=(snapshot or {}).get("material_summary", ""),
                batch_id=(snapshot or {}).get("batch_id") or run_id or task_id,
                run_id=run_id,
            )
        )
    return append_progress_records(path, progress, limit=limit)


def _within_days(value, now, days):
    dt = _parse_dt(value)
    return bool(dt and now - timedelta(days=days) <= dt <= now)


def material_outreach_stats(materials, send_records, skip_records, *, runtime_material_ids=None, now=None):
    now = now or datetime.now()
    runtime_ids = set(runtime_material_ids or [])
    today = now.date()
    stats = {
        "materials_total": len(materials or []),
        "available_materials": sum(
            1 for item in materials or [] if item.get("status", "active") == "active"
        ),
        "available_runtime_materials": sum(
            1 for item in materials or [] if item.get("id") in runtime_ids and item.get("status", "active") == "active"
        ),
        "today": {
            "attempts": 0,
            "success": 0,
            "failed": 0,
            "cooldown_skips": 0,
            "no_material_skips": 0,
        },
        "last_7_days": {
            "attempts": 0,
            "success": 0,
            "failed": 0,
            "cooldown_skips": 0,
            "no_material_skips": 0,
        },
        "by_type": {"link": 0, "miniapp": 0, "image": 0, "other": 0},
    }
    for item in materials or []:
        bucket = item.get("type_bucket") or material_type_bucket(item.get("type"))
        stats["by_type"][bucket if bucket in stats["by_type"] else "other"] += 1
    for record in send_records or []:
        sent_at = _parse_dt(record.get("sent_at"))
        if not sent_at:
            continue
        for key, include in (
            ("today", sent_at.date() == today),
            ("last_7_days", _within_days(record.get("sent_at"), now, 7)),
        ):
            if include:
                stats[key]["attempts"] += 1
                if record.get("success"):
                    stats[key]["success"] += 1
                else:
                    stats[key]["failed"] += 1
    for record in skip_records or []:
        created_at = _parse_dt(record.get("created_at"))
        if not created_at:
            continue
        reason_key = "cooldown_skips" if record.get("reason") == "cooldown" else "no_material_skips"
        if created_at.date() == today:
            stats["today"][reason_key] += 1
        if _within_days(record.get("created_at"), now, 7):
            stats["last_7_days"][reason_key] += 1
    return stats


def _clean_unique_strings(value):
    if isinstance(value, (list, tuple, set)):
        raw_values = value
    elif value is None:
        raw_values = []
    else:
        raw_values = [value]
    result = []
    seen = set()
    for item in raw_values:
        text = str(item or "").strip()
        if not text or text in seen:
            continue
        seen.add(text)
        result.append(text)
    return result


def normalize_target_selector(selector):
    selector = selector if isinstance(selector, dict) else {}
    include_tags = _clean_unique_strings(selector.get("include_tags"))
    exclude_tags = _clean_unique_strings(selector.get("exclude_tags"))
    mode = str(selector.get("mode") or "").strip()
    if mode not in {"all", "include", "exclude"}:
        if include_tags or str(selector.get("base") or "").strip() == "manual":
            mode = "include"
        elif exclude_tags:
            mode = "exclude"
        else:
            mode = "all"
    return {
        "mode": mode,
        "base": "manual" if mode == "include" and not include_tags else "all_friends",
        "include_tags": include_tags if mode == "include" else [],
        "exclude_tags": exclude_tags if mode == "exclude" else [],
        "include_contact_keys": [],
        "exclude_contact_keys": [],
    }


def normalize_manual_target_names(value):
    return _clean_unique_strings(value)


def normalize_trigger_strategy(value):
    strategy = str(value or "fixed").strip()
    return strategy if strategy in {"fixed", "random"} else "fixed"


def normalize_batch_material_strategy(value):
    strategy = str(value or "per_batch").strip()
    mapping = {
        "per_run": "per_task",
        "per_task": "per_task",
        "per_batch": "per_batch",
        "fixed": "fixed",
    }
    return mapping.get(strategy, "per_batch")


def _schedule_display_fields(schedule, *, trigger_strategy):
    repeat_type = repeat_rule_to_type(
        schedule.get("repeat_rule"),
        repeat_mode=schedule.get("repeat_mode"),
    )
    repeat_values = list(schedule.get("repeat_values") or [])
    fields = {
        "time": str(schedule.get("time_value") or "08:00").strip() or "08:00",
        "time_start": str(schedule.get("time_window_start") or "00:00").strip() or "00:00",
        "time_end": str(schedule.get("time_window_end") or "23:59").strip() or "23:59",
        "repeat_type": repeat_type,
        "weekdays": repeat_values if repeat_type == "weekly" else [],
        "dates": repeat_values if repeat_type in {"monthly", "custom", "once"} else [],
    }
    if trigger_strategy == "fixed" and repeat_type == "once":
        fire_at = str(schedule.get("fire_at") or "").strip()
        if fire_at:
            fields["time"] = fire_at[11:16] or fields["time"]
            fields["dates"] = [fire_at[:10]]
    return fields


def normalize_material_outreach_task(task):
    task = task if isinstance(task, dict) else {}
    preface_config = normalize_material_outreach_preface_config(task)
    trigger_strategy = normalize_trigger_strategy(task.get("trigger_strategy") or task.get("mode") or "fixed")
    if trigger_strategy == "random":
        schedule = normalize_random_task_schedule(
            task,
            default_start="00:00",
            default_end="23:59",
        )
    else:
        schedule = normalize_fixed_task_schedule(
            task,
            default_time="08:00",
            start_at_key="start_at",
        )
    schedule_display = _schedule_display_fields(schedule, trigger_strategy=trigger_strategy)
    batch_size_fixed = coerce_forward_batch_size(task.get("batch_size_fixed", MAX_FORWARD_BATCH_SIZE))
    return {
        "id": str(task.get("id") or ""),
        "name": str(task.get("name") or "").strip(),
        "enabled": _coerce_preface_bool(task.get("enabled", True)),
        "status": _normalize_task_runtime_status(task.get("status")),
        "targets": _clean_unique_strings(task.get("targets")),
        "manual_target_names": normalize_manual_target_names(task.get("manual_target_names")),
        "material_types": normalize_material_types(task.get("material_types", ["all"])),
        "trigger_strategy": trigger_strategy,
        "mode": trigger_strategy,
        "time": schedule_display["time"],
        "time_start": schedule_display["time_start"],
        "time_end": schedule_display["time_end"],
        "schedule_mode": str(schedule.get("schedule_mode") or ""),
        "repeat_mode": str(schedule.get("repeat_mode") or ""),
        "repeat_rule": str(schedule.get("repeat_rule") or ""),
        "repeat_values": list(schedule.get("repeat_values") or []),
        "repeat_type": schedule_display["repeat_type"],
        "weekdays": list(schedule_display["weekdays"] or []),
        "dates": list(schedule_display["dates"] or []),
        "time_value": str(schedule.get("time_value") or ""),
        "next_fire_at": str(task.get("next_fire_at") or "").strip(),
        "execute_after": str(task.get("execute_after") or "").strip(),
        "fire_at": str(schedule.get("fire_at") or ""),
        "start_at": str(schedule.get("start_at") or "").strip(),
        "time_window_start": str(schedule.get("time_window_start") or "").strip(),
        "time_window_end": str(schedule.get("time_window_end") or "").strip(),
        "random_days_count": max(1, int(schedule.get("random_days_count", 1) or 1)),
        "material_source_filter": str(task.get("material_source_filter") or "").strip(),
        "preface_mode": preface_config["preface_mode"],
        "preface_text": preface_config["preface_text"],
        "preface_random_emojis": preface_config["preface_random_emojis"],
        "ai_preface_goal": preface_config["ai_preface_goal"],
        "ai_preface_intensity": preface_config["ai_preface_intensity"],
        "ai_preface_extra_instruction": preface_config["ai_preface_extra_instruction"],
        "ai_preface_failure_mode": preface_config["ai_preface_failure_mode"],
        "cooldown_hours": 0,
        "batch_size_mode": "fixed",
        "batch_size_fixed": batch_size_fixed,
        "batch_size_min": batch_size_fixed,
        "batch_size_max": batch_size_fixed,
        "batch_material_strategy": normalize_batch_material_strategy(task.get("batch_material_strategy")),
        "fixed_material_id": str(task.get("fixed_material_id") or "").strip(),
        "last_error": str(task.get("last_error") or "").strip(),
        "target_selector": normalize_target_selector(task.get("target_selector")),
    }


def iter_enabled_material_outreach_tasks(tasks):
    for task in iter_enabled_tasks(tasks):
        normalized = normalize_material_outreach_task(task)
        trigger_strategy = normalized["trigger_strategy"]
        yield {
            "task_id": normalized.get("id", ""),
            "mode": trigger_strategy,
            "time": normalized.get("time", "08:00"),
            "time_start": normalized.get("time_start", "00:00"),
            "time_end": normalized.get("time_end", "23:59"),
            "targets": normalized.get("targets", []),
            "manual_target_names": normalized.get("manual_target_names", []),
            "material_types": normalized.get("material_types", ["all"]),
            "trigger_strategy": trigger_strategy,
            "start_at": normalized.get("start_at", ""),
            "material_source_filter": normalized.get("material_source_filter", ""),
            "preface_mode": normalized.get("preface_mode", "none"),
            "preface_text": normalized.get("preface_text", ""),
            "preface_random_emojis": normalized.get("preface_random_emojis", False),
            "ai_preface_goal": normalized.get("ai_preface_goal", ""),
            "ai_preface_intensity": normalized.get("ai_preface_intensity", ""),
            "ai_preface_extra_instruction": normalized.get("ai_preface_extra_instruction", ""),
            "ai_preface_failure_mode": normalized.get("ai_preface_failure_mode", "send_without_preface"),
            "cooldown_hours": normalized.get("cooldown_hours", 0),
            "batch_size_mode": normalized.get("batch_size_mode", "fixed"),
            "batch_size_fixed": normalized.get("batch_size_fixed", MAX_FORWARD_BATCH_SIZE),
            "batch_size_min": normalized.get("batch_size_min", MAX_FORWARD_BATCH_SIZE),
            "batch_size_max": normalized.get("batch_size_max", MAX_FORWARD_BATCH_SIZE),
            "batch_material_strategy": normalized.get("batch_material_strategy", "per_batch"),
            "fixed_material_id": normalized.get("fixed_material_id", ""),
            "repeat_type": normalized.get("repeat_type", "daily"),
            "weekdays": normalized.get("weekdays", []),
            "dates": normalized.get("dates", []),
            "random_days_count": max(1, int(normalized.get("random_days_count", 1) or 1)),
            "target_selector": normalized.get("target_selector", normalize_target_selector({})),
        }


def prepare_random_material_outreach_day(task_id, task, state, today, *, sample_days=None, log_info=None):
    return prepare_random_task_day(
        task_id,
        task,
        state,
        today,
        log_prefix="素材转发",
        log_action="发送",
        sample_days=sample_days,
        log_info=log_info,
    )


def plan_random_material_outreach_fire_time(task_id, task, state, now, *, randint=None, log_info=None):
    return plan_random_fire_time(
        task_id,
        task,
        state,
        now,
        log_prefix="素材转发",
        fire_word="发送",
        randint=randint,
        log_info=log_info,
    )


def trigger_random_material_outreach_if_due(
    task_id,
    task,
    state,
    now,
    *,
    send_material_outreach,
    log_info=None,
    log_error=None,
):
    next_fire = state.get("next_fire")
    if next_fire is None or now < next_fire:
        return False
    log_info = log_info or (lambda message: None)
    log_error = log_error or (lambda message: None)
    should_clear_next_fire = True
    try:
        result = send_material_outreach(task)
        status = str(result.get("status") or "").strip().lower() if isinstance(result, dict) else ""
        if status in {"deferred", "deferred_lock_busy"}:
            log_info(f"素材转发 {task_id} 微信 UI 正忙，稍后重试")
            should_clear_next_fire = False
            return False
        state["last_fire_date"] = now.date()
    except Exception as exc:
        log_error(f"素材转发 {task_id} 发送失败：{exc}")
    finally:
        if should_clear_next_fire:
            state["next_fire"] = None
    return True


def execute_material_outreach_task(
    *,
    task,
    send_material_outreach,
    log_info=None,
    log_error=None,
):
    """Execute one concrete material-outreach task that is already due."""
    log_info = log_info or (lambda message: None)
    log_error = log_error or (lambda message: None)
    task = task if isinstance(task, dict) else {}
    task_id = str(task.get("task_id") or task.get("id") or "").strip()

    log_info(f"素材转发 {task_id or '未命名任务'}：开始执行")
    try:
        result = send_material_outreach(task)
        if isinstance(result, dict):
            return result
        return bool(result)
    except Exception as exc:
        log_error(f"素材转发 {task_id or '未命名任务'} 执行失败：{exc}")
        return False


def material_sources_for_task(task, configured_sources):
    task = task or {}
    seen = set()
    sources = []
    for source in configured_sources or []:
        source = str(source or "").strip()
        if not source or source in seen:
            continue
        seen.add(source)
        sources.append(source)

    source_filter = str(task.get("material_source_filter") or "").strip()
    if source_filter:
        return [source_filter] if source_filter in seen else []
    return sources


def is_material_source(sources, chat_who):
    return bool(chat_who and chat_who in set(sources or []))


def iter_material_outreach_listen_sources(sources, *, listen_list, groups, group_switch, command_chat):
    listened_groups = set(groups or []) if group_switch else set()
    already_listened = set(listen_list or []) | listened_groups | {command_chat}
    seen = set()
    for source in sources or []:
        if not source or source in seen or source in already_listened:
            continue
        seen.add(source)
        yield source


def plan_material_outreach_run(task, materials, send_records, runtime_material_ids, *, now=None, choice=None):
    now = now or datetime.now()
    send_actions = []
    skip_records = []
    for target in task.get("targets", []) or []:
        if is_target_in_cooldown(send_records, target, task.get("cooldown_hours", 0), now=now):
            skip_records.append(build_skip_record(task.get("task_id"), target, "cooldown", "同好友冷却未结束", now=now))
            continue
        material = select_material_for_target(
            materials,
            runtime_material_ids=runtime_material_ids,
            send_records=send_records,
            target=target,
            allowed_buckets=task.get("material_types", ["all"]),
            choice=choice,
        )
        if not material:
            skip_records.append(build_skip_record(task.get("task_id"), target, "no_material", "没有可用素材", now=now))
            continue
        send_actions.append({"target": target, "material": material})
    return {"send": send_actions, "skip": skip_records}
