"""Shared task workbench contract helpers."""

from copy import deepcopy

MODULES = {
    "scheduled_message",
    "material_outreach",
}

QUEUE_SOURCES = {
    "manual",
    "ai",
    "preface",
    "system",
}

_FORBIDDEN_RUNTIME_HINT_KEYS = {
    "content",
    "targets",
    "tags",
    "schedule",
    "notes",
    "queue",
    "executions",
    "history",
    "runtime",
}

_STATUS_LABELS = {
    "pending_confirm": "待确认",
    "pending": "待执行",
    "running": "执行中",
    "executed": "已执行",
    "enabled": "已启用",
    "disabled": "已停用",
}

_RESULT_LABELS = {
    "success": "成功",
    "partial_success": "部分成功",
    "failed": "失败",
    "skipped": "已跳过",
}


def build_task_fields(
    *,
    content=None,
    targets=None,
    tags=None,
    schedule=None,
    notes="",
    module_fields=None,
):
    fields = {
        "content": deepcopy(content) if content is not None else {},
        "targets": deepcopy(targets) if targets is not None else {},
        "tags": deepcopy(list(tags)) if tags is not None else [],
        "schedule": deepcopy(schedule) if schedule is not None else {},
        "notes": notes,
    }
    for key, value in (module_fields or {}).items():
        if key in fields:
            raise ValueError("module_fields key conflict with shared fields: %s" % key)
        fields[key] = deepcopy(value)
    return fields


def build_runtime_hints(runtime_hints=None):
    hints = deepcopy(dict(runtime_hints or {}))
    forbidden_keys = sorted(_FORBIDDEN_RUNTIME_HINT_KEYS.intersection(hints))
    if forbidden_keys:
        raise ValueError(
            "runtime_hints contains forbidden truth/storage keys: %s"
            % ", ".join(forbidden_keys)
        )
    return hints


def build_queue_item(
    *,
    module,
    task_id="",
    queue_id=None,
    source,
    title,
    detail,
    scheduled_at,
    status,
    targets_summary="",
    content_summary="",
    media_summary="",
    material_summary="",
    batch_summary="",
    raw_targets=None,
    raw_messages=None,
    raw_media=None,
    raw_material=None,
    batch_id="",
    run_id="",
):
    _validate_module(module)
    queue_source = _validate_queue_source(source)
    action_type, action_label = _queue_action_for_source(queue_source)
    task_id = str(task_id or "").strip()
    queue_suffix = str(queue_id or "").strip() or task_id
    resolved_queue_id = f"{queue_source}:{queue_suffix}"
    action = None
    if action_type and action_label and str(status or "").strip() != "running":
        action = {
            "type": action_type,
            "label": action_label,
            "danger": True,
        }
    return {
        "module": module,
        "queue_id": resolved_queue_id,
        "task_id": task_id,
        "source": queue_source,
        "title": title,
        "detail": detail,
        "scheduled_at": scheduled_at,
        "status": status,
        "status_label": status_label(status),
        "targets_summary": _clean_summary_value(targets_summary),
        "content_summary": _clean_summary_value(content_summary),
        "media_summary": _clean_summary_value(media_summary),
        "material_summary": _clean_summary_value(material_summary),
        "batch_summary": _clean_summary_value(batch_summary),
        "raw_targets": _deepcopy_runtime_value(raw_targets, []),
        "raw_messages": _deepcopy_runtime_value(raw_messages, []),
        "raw_media": _deepcopy_runtime_value(raw_media, []),
        "raw_material": _deepcopy_runtime_value(raw_material, {}),
        "batch_id": _clean_summary_value(batch_id),
        "run_id": _clean_summary_value(run_id),
        "action": action,
    }


def build_execution_item(
    *,
    module,
    execution_id,
    task_id,
    title,
    detail,
    executed_at,
    status,
    result="",
    result_message,
    result_label="",
    targets_summary="",
    content_summary="",
    media_summary="",
    material_summary="",
    batch_summary="",
    result_summary="",
    raw_targets=None,
    raw_messages=None,
    raw_media=None,
    raw_material=None,
    batch_id="",
    run_id="",
):
    _validate_module(module)
    return {
        "module": module,
        "execution_id": execution_id,
        "task_id": task_id,
        "title": title,
        "detail": detail,
        "executed_at": executed_at,
        "status": status,
        "status_label": status_label(status),
        "result": _clean_summary_value(result),
        "result_label": result_label_for(result_label or result),
        "result_message": result_message,
        "targets_summary": _clean_summary_value(targets_summary),
        "content_summary": _clean_summary_value(content_summary),
        "media_summary": _clean_summary_value(media_summary),
        "material_summary": _clean_summary_value(material_summary),
        "batch_summary": _clean_summary_value(batch_summary),
        "result_summary": _clean_summary_value(result_summary),
        "raw_targets": _deepcopy_runtime_value(raw_targets, []),
        "raw_messages": _deepcopy_runtime_value(raw_messages, []),
        "raw_media": _deepcopy_runtime_value(raw_media, []),
        "raw_material": _deepcopy_runtime_value(raw_material, {}),
        "batch_id": _clean_summary_value(batch_id),
        "run_id": _clean_summary_value(run_id),
        "action_slot": None,
    }


def status_label(status_value):
    if not status_value:
        return "待执行"
    return _STATUS_LABELS.get(status_value, status_value)


def result_label_for(result_value):
    value = str(result_value or "").strip()
    if not value:
        return ""
    return _RESULT_LABELS.get(value, value)


def _queue_action_for_source(source):
    if source == "system":
        return None, None
    return "cancel", "取消"


def _validate_module(module):
    if module not in MODULES:
        raise ValueError("invalid module: %r" % module)
    return module


def _validate_queue_source(source):
    if source not in QUEUE_SOURCES:
        raise ValueError("invalid queue source: %r" % source)
    return source


def _clean_summary_value(value):
    return str(value or "").strip()


def _deepcopy_runtime_value(value, default):
    if value is None:
        return deepcopy(default)
    return deepcopy(value)
