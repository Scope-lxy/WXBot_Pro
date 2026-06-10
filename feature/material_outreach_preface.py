"""Queue helpers for material outreach AI preface prefetch/send."""

from datetime import datetime, timedelta
import uuid

from feature.task_workbench_runtime_summary import runtime_snapshot

PREFETCH_LEAD_SECONDS = 30
_ACTIVE_STATUSES = {"pending"}
_FINAL_STATUSES = {"sent", "failed"}
_DEFAULT_MATERIAL_OWNERSHIP = "我的作品"
_MATERIAL_OWNERSHIP_VALUES = {"我的作品", "第三方作品"}


def _normalize_material_ownership(value):
    ownership = str(value or "").strip()
    if ownership in _MATERIAL_OWNERSHIP_VALUES:
        return ownership
    return _DEFAULT_MATERIAL_OWNERSHIP


def _parse_dt(value):
    try:
        return datetime.fromisoformat(str(value or ""))
    except ValueError:
        return None


def _iso(value):
    dt = value if isinstance(value, datetime) else _parse_dt(value)
    if dt is None:
        return ""
    return dt.replace(microsecond=0).isoformat()


def _snapshot_contact(task, target):
    snapshot = (task or {}).get("_outreach_target_snapshot") if isinstance(task, dict) else None
    targets = (snapshot or {}).get("targets") or []
    target = str(target or "").strip()
    for item in targets:
        if str((item or {}).get("send_name") or "").strip() == target:
            return item
    return {
        "contact_key": "",
        "send_name": target,
        "display_name": target,
    }


def build_preface_queue_record(task, action, *, scheduled_at, now=None, queue_id_factory=None):
    now = now or datetime.now()
    queue_id_factory = queue_id_factory or (lambda: f"preface_{uuid.uuid4().hex[:8]}")
    task = task if isinstance(task, dict) else {}
    action = action if isinstance(action, dict) else {}
    material = action.get("material") if isinstance(action.get("material"), dict) else {}
    target = str(action.get("target") or "").strip()
    contact = _snapshot_contact(task, target)
    scheduled_dt = _parse_dt(scheduled_at) or now
    prefetch_dt = scheduled_dt - timedelta(seconds=PREFETCH_LEAD_SECONDS)
    failure_mode = str(task.get("ai_preface_failure_mode") or "").strip().lower()
    if failure_mode not in {"send_without_preface", "skip_target"}:
        failure_mode = "send_without_preface"
    queue_id = str(queue_id_factory() or "").strip()
    task_id = str(task.get("task_id") or task.get("id") or "").strip()
    run_id = str(((task.get("_outreach_target_snapshot") or {}) if isinstance(task, dict) else {}).get("run_id") or "").strip()
    batch_id = run_id or queue_id or task_id
    snapshot = runtime_snapshot(
        raw_targets=[contact],
        raw_messages=[{"type": "text", "text": "AI 附加文案待生成后发送"}],
        raw_media=[],
        raw_material={
            "material_id": str(material.get("id") or "").strip(),
            "title": str(material.get("content_preview") or "").strip(),
            "type": str(material.get("type_bucket") or material.get("type") or "").strip(),
            "source": str(material.get("source") or "").strip(),
            "ownership": _normalize_material_ownership(material.get("ownership") or _DEFAULT_MATERIAL_OWNERSHIP),
            "copy_note": str(material.get("copy_note") or "").strip(),
        },
        targets_summary=str(contact.get("display_name") or target or "").strip(),
        content_summary="AI 附加文案待生成后发送",
        material_summary="{}：{}".format(
            str(material.get("type_bucket") or material.get("type") or "").strip(),
            str(material.get("content_preview") or "").strip(),
        ).strip("："),
        batch_id=batch_id,
        run_id=run_id,
    )
    return {
        "queue_id": queue_id,
        "task_id": task_id,
        "task_name": str(task.get("task_name") or task.get("name") or "").strip(),
        "run_id": run_id,
        "contact_key": str(contact.get("contact_key") or "").strip(),
        "display_name": str(contact.get("display_name") or target or "").strip(),
        "target": target,
        "material_id": str(material.get("id") or "").strip(),
        "stable_signature": str(material.get("stable_signature") or "").strip(),
        "material_title": str(material.get("content_preview") or "").strip(),
        "material_type": str(material.get("type_bucket") or material.get("type") or "").strip(),
        "material_source": str(material.get("source") or "").strip(),
        "material_ownership": _normalize_material_ownership(material.get("ownership") or _DEFAULT_MATERIAL_OWNERSHIP),
        "material_copy_note": str(material.get("copy_note") or "").strip(),
        "failure_mode": failure_mode,
        "ai_preface_goal": str(task.get("ai_preface_goal") or "").strip(),
        "ai_preface_intensity": str(task.get("ai_preface_intensity") or "").strip(),
        "ai_preface_extra_instruction": str(task.get("ai_preface_extra_instruction") or "").strip(),
        "status": "pending",
        "preface_status": "pending",
        "preface": "",
        "preface_error": "",
        "created_at": _iso(now),
        "prefetch_at": _iso(prefetch_dt),
        "scheduled_at": _iso(scheduled_dt),
        "prefetched_at": "",
        "finished_at": "",
        "error": "",
        **snapshot,
    }


def normalize_preface_queue_record(record):
    record = dict(record) if isinstance(record, dict) else {}
    status = str(record.get("status") or "").strip().lower() or "pending"
    preface_status = str(record.get("preface_status") or "").strip().lower()
    if status not in _ACTIVE_STATUSES | _FINAL_STATUSES:
        status = "pending"
    if preface_status not in {"pending", "success", "failed"}:
        if status == "sent":
            preface_status = "success" if str(record.get("preface") or "").strip() else "pending"
        elif status == "failed":
            preface_status = "failed"
        else:
            preface_status = "pending"
    failure_mode = str(record.get("failure_mode") or "").strip().lower()
    if failure_mode not in {"send_without_preface", "skip_target"}:
        failure_mode = "send_without_preface"
    return {
        "queue_id": str(record.get("queue_id") or "").strip(),
        "task_id": str(record.get("task_id") or "").strip(),
        "task_name": str(record.get("task_name") or "").strip(),
        "run_id": str(record.get("run_id") or "").strip(),
        "contact_key": str(record.get("contact_key") or "").strip(),
        "display_name": str(record.get("display_name") or record.get("target") or "").strip(),
        "target": str(record.get("target") or "").strip(),
        "material_id": str(record.get("material_id") or "").strip(),
        "stable_signature": str(record.get("stable_signature") or "").strip(),
        "material_title": str(record.get("material_title") or "").strip(),
        "material_type": str(record.get("material_type") or "").strip(),
        "material_source": str(record.get("material_source") or "").strip(),
        "material_ownership": _normalize_material_ownership(record.get("material_ownership") or _DEFAULT_MATERIAL_OWNERSHIP),
        "material_copy_note": str(record.get("material_copy_note") or "").strip(),
        "failure_mode": failure_mode,
        "ai_preface_goal": str(record.get("ai_preface_goal") or "").strip(),
        "ai_preface_intensity": str(record.get("ai_preface_intensity") or "").strip(),
        "ai_preface_extra_instruction": str(record.get("ai_preface_extra_instruction") or "").strip(),
        "status": status,
        "preface_status": preface_status,
        "preface": str(record.get("preface") or "").strip(),
        "preface_error": str(record.get("preface_error") or "").strip(),
        "created_at": _iso(record.get("created_at")),
        "prefetch_at": _iso(record.get("prefetch_at")),
        "scheduled_at": _iso(record.get("scheduled_at")),
        "prefetched_at": _iso(record.get("prefetched_at")),
        "finished_at": _iso(record.get("finished_at")),
        "error": str(record.get("error") or "").strip(),
        "targets_summary": str(record.get("targets_summary") or "").strip(),
        "content_summary": str(record.get("content_summary") or "").strip(),
        "media_summary": str(record.get("media_summary") or "").strip(),
        "material_summary": str(record.get("material_summary") or "").strip(),
        "raw_targets": list(record.get("raw_targets") or []),
        "raw_messages": list(record.get("raw_messages") or []),
        "raw_media": list(record.get("raw_media") or []),
        "raw_material": dict(record.get("raw_material") or {}),
        "batch_id": str(record.get("batch_id") or "").strip(),
        "run_id": str(record.get("run_id") or "").strip(),
    }


def normalize_preface_pending_queue(records):
    return [
        normalize_preface_queue_record(record)
        for record in (records or [])
        if isinstance(record, dict)
    ]


def due_prefetch_records(records, *, now=None):
    now = now or datetime.now()
    due = []
    for record in records or []:
        normalized = normalize_preface_queue_record(record)
        if normalized["status"] != "pending" or normalized["preface_status"] != "pending":
            continue
        prefetch_at = _parse_dt(normalized.get("prefetch_at"))
        scheduled_at = _parse_dt(normalized.get("scheduled_at"))
        if prefetch_at is None or scheduled_at is None:
            continue
        if prefetch_at <= now < scheduled_at:
            due.append(record)
    return due


def due_send_records(records, *, now=None):
    now = now or datetime.now()
    due = []
    for record in records or []:
        normalized = normalize_preface_queue_record(record)
        if normalized["status"] != "pending":
            continue
        scheduled_at = _parse_dt(normalized.get("scheduled_at"))
        if scheduled_at and scheduled_at <= now:
            due.append(record)
    return due


def mark_preface_generated(record, preface, *, now=None):
    now = now or datetime.now()
    record["preface"] = str(preface or "").strip()
    record["preface_status"] = "success"
    record["preface_error"] = ""
    record["prefetched_at"] = _iso(now)
    return record


def mark_preface_failed(record, error, *, now=None):
    now = now or datetime.now()
    record["preface"] = ""
    record["preface_status"] = "failed"
    record["preface_error"] = str(error or "").strip()
    record["prefetched_at"] = _iso(now)
    return record


def cancel_preface_pending_record(records, queue_id):
    queue_id = str(queue_id or "").strip()
    for index, record in enumerate(list(records or [])):
        if str(record.get("queue_id") or "").strip() != queue_id:
            continue
        if str(record.get("status") or "").strip() != "pending":
            return False
        del records[index]
        return True
    return False
