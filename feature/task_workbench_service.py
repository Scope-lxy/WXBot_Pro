"""Unified service for task workbench API."""

from __future__ import annotations

from datetime import datetime
import ntpath

from feature.ai_material_outreach import cancel_ai_pending_record
from feature.material_outreach import (
    load_json_list,
    normalize_material_outreach_history_payload,
    normalize_material_outreach_runtime_payload,
)
from feature.material_outreach_preface import cancel_preface_pending_record
from feature.moments_tasks import (
    cancel_queued_moments_task,
    deserialize_moments_task_collection,
    moments_task_has_ai_candidates,
    moments_task_publish_text,
    normalize_moments_task,
    queue_moments_task,
    split_moments_task_storage,
    serialize_moments_task_collection,
)
from feature.scheduled_message_tasks import (
    STATUS_PENDING,
    STATUS_RUNNING,
    deserialize_scheduled_message_task_collection,
    ensure_scheduled_message_next_run,
    normalize_scheduled_message_task_payload,
    queue_scheduled_message_task,
    return_scheduled_message_task,
    split_scheduled_message_task_storage,
    serialize_scheduled_message_task_collection,
)
from feature.task_workbench_contract import (
    MODULES,
    build_execution_item,
    build_queue_item,
    build_runtime_hints,
    build_task_fields,
)
from feature.task_workbench_runtime_summary import runtime_snapshot
from feature.task_workbench_storage import TaskWorkbenchStorage
from feature.task_display_titles import (
    is_likely_local_file_path,
    material_outreach_record_title,
    material_outreach_task_title,
    moments_task_title,
    scheduled_message_task_title,
)


class TaskWorkbenchServiceError(Exception):
    def __init__(self, message, status_code=400):
        super().__init__(message)
        self.message = str(message or "")
        self.status_code = int(status_code or 400)


def build_workbench_payload(module, *, data_dir, wx_id, active_task_id=""):
    module = _normalize_module(module)
    raw_tasks = _load_tasks(module, data_dir=data_dir, wx_id=wx_id)
    runtime = _build_runtime(module, tasks=raw_tasks, data_dir=data_dir, wx_id=wx_id)
    task_views = _build_task_views(module, raw_tasks, runtime=runtime)
    return _build_payload(
        module,
        wx_id=wx_id,
        tasks=task_views,
        runtime=runtime,
        active_task_id=active_task_id,
    )


def build_runtime_payload(module, *, data_dir, wx_id):
    module = _normalize_module(module)
    tasks = _load_tasks(module, data_dir=data_dir, wx_id=wx_id)
    return _build_runtime(module, tasks=tasks, data_dir=data_dir, wx_id=wx_id)


def clear_executions(module, *, data_dir, wx_id, hooks=None):
    module = _normalize_module(module)
    hooks = hooks if isinstance(hooks, dict) else {}

    if module == "scheduled_message":
        _clear_scheduled_message_executions(data_dir=data_dir, wx_id=wx_id)
        message = "已清空定时消息执行记录"
    elif module == "moments":
        _clear_moments_executions(data_dir=data_dir, wx_id=wx_id)
        message = "已清空朋友圈执行记录"
    else:
        _clear_material_outreach_executions(data_dir=data_dir, wx_id=wx_id)
        message = "已清空素材转发执行记录"

    _call_reload(hooks)
    response = build_workbench_payload(module, data_dir=data_dir, wx_id=wx_id, active_task_id="")
    response["message"] = message
    return response


def queue_task(module, task_id, *, data_dir, wx_id, payload=None, hooks=None):
    module = _normalize_module(module)
    task_id = _clean(task_id)
    hooks = hooks if isinstance(hooks, dict) else {}
    payload = payload if isinstance(payload, dict) else {}
    if not task_id:
        raise TaskWorkbenchServiceError("缺少任务ID", 400)

    if module == "scheduled_message":
        raise TaskWorkbenchServiceError(
            "定时消息任务改为按卡片启停自动生成运行时实例，不支持手动加入队列",
            400,
        )
    elif module == "moments":
        updated_task = _queue_moments_task(
            task_id,
            data_dir=data_dir,
            wx_id=wx_id,
            payload=payload,
            hooks=hooks,
        )
        response = build_workbench_payload(module, data_dir=data_dir, wx_id=wx_id, active_task_id=task_id)
        response["message"] = "已确认这条发圈任务"
    else:
        raise TaskWorkbenchServiceError(
            "素材转发任务改为按卡片启停自动生成运行时实例，不支持手动加入队列",
            400,
        )

    queue_item = _find_queue_item(response.get("runtime", {}).get("queue"), f"manual:{task_id}")
    if queue_item is None:
        queue_item = build_queue_item(
            module=module,
            task_id=task_id,
            source="manual",
            title=moments_task_title(updated_task),
            detail=_clean(_moments_pending_snapshot(updated_task).get("media_summary")) or "无图片",
            scheduled_at=_clean(updated_task.get("execute_after")) or "等待调度",
            status=_normalize_queue_status(updated_task.get("status")),
        )
    response["task"] = _build_task_view(module, updated_task)
    response["queue_item"] = queue_item
    return response


def cancel_queue_item(module, queue_id, *, data_dir, wx_id, hooks=None):
    module = _normalize_module(module)
    queue_id = _clean(queue_id)
    hooks = hooks if isinstance(hooks, dict) else {}
    if not queue_id:
        raise TaskWorkbenchServiceError("缺少队列ID", 400)

    if module == "scheduled_message":
        raise TaskWorkbenchServiceError(
            "定时消息运行时实例由任务设置自动生成，不支持手动取消",
            400,
        )
    elif module == "moments":
        task_id = _require_manual_task_id(queue_id)
        updated_task, message = _cancel_moments_queue_item(
            task_id,
            data_dir=data_dir,
            wx_id=wx_id,
            hooks=hooks,
        )
    else:
        if queue_id.startswith("manual:"):
            raise TaskWorkbenchServiceError(
                "素材转发运行时实例由任务设置自动生成，不支持手动取消",
                400,
            )
        updated_task, message = _cancel_material_outreach_queue_item(
            queue_id,
            data_dir=data_dir,
            wx_id=wx_id,
            hooks=hooks,
        )

    response = build_workbench_payload(module, data_dir=data_dir, wx_id=wx_id, active_task_id="")
    response["message"] = message
    response["task"] = _build_task_view(module, updated_task)
    return response


def _normalize_module(module):
    module = _clean(module)
    if module not in MODULES:
        raise TaskWorkbenchServiceError("不支持的任务模块", 404)
    return module


def _storage(module, *, data_dir, wx_id):
    return TaskWorkbenchStorage(data_dir, wx_id, module)


def _clean(value):
    return str(value or "").strip()


def _normalize_queue_status(status):
    status = _clean(status)
    if status == "pending":
        return "pending"
    if status in {"pending_confirm"}:
        return "pending_confirm"
    if status == "running":
        return "running"
    if status in {"success", "executed", "sent"}:
        return "executed"
    if status in {"failed", "error"}:
        return "failed"
    return "pending"


def _queue_status_is_active(status):
    return _normalize_queue_status(status) in {"pending", "running"}


def _build_payload(module, *, wx_id, tasks, runtime, active_task_id):
    return {
        "status": "success",
        "module": module,
        "schema_version": 1,
        "tasks": tasks,
        "active_task_id": _clean(active_task_id),
        "draft_defaults": {},
        "runtime": runtime,
        "capabilities": {},
        "meta": {
            "loaded_at": datetime.now().replace(microsecond=0).isoformat(),
            "wx_id": _clean(wx_id),
        },
    }


def _clean_list(values):
    return [_clean(item) for item in (values or []) if _clean(item)]


def _scheduled_attachment_names(messages):
    names = []
    for item in messages or []:
        text = _clean(item).strip("\"'")
        if not text or not is_likely_local_file_path(text):
            continue
        file_name = ntpath.basename(text.replace("/", "\\"))
        if file_name:
            names.append(file_name)
    return names


def _scheduled_attachment_summary(messages):
    names = _scheduled_attachment_names(messages)
    if not names:
        return "无附件"
    if len(names) == 1:
        return names[0]
    return f"{names[0]}、{names[1]} 共 {len(names)} 个文件"


def _material_outreach_task_copy_summary(task):
    task = task if isinstance(task, dict) else {}
    preface_mode = _clean(task.get("preface_mode")) or "ai"
    preface_text = _clean(task.get("preface_text"))
    if preface_mode == "custom":
        return preface_text or "无文案"
    if preface_mode == "ai":
        parts = _clean_list([task.get("ai_preface_goal"), task.get("ai_preface_intensity")])
        return f"AI生成文案：{' · '.join(parts)}" if parts else "无文案"
    return "无文案"


def _material_outreach_content_is_placeholder(value):
    value = _clean(value)
    return value.startswith("AI 附加文案待") or value in {"待生成文案", "待发送文案"}


def _material_outreach_record_copy_summary(record):
    record = record if isinstance(record, dict) else {}
    preface = _clean(record.get("preface")) or _clean(record.get("preface_text"))
    if preface:
        return preface
    content_summary = _clean(record.get("content_summary"))
    if content_summary and not _material_outreach_content_is_placeholder(content_summary):
        return content_summary
    parts = _clean_list([record.get("ai_preface_goal"), record.get("ai_preface_intensity")])
    if parts:
        return f"AI生成文案：{' · '.join(parts)}"
    return "无文案"


def _material_outreach_group_copy_summary(records, snapshot):
    for record in records or []:
        copy_summary = _material_outreach_record_copy_summary(record)
        if copy_summary != "无文案":
            return copy_summary
    return _material_outreach_record_copy_summary(snapshot)


def _material_outreach_record_target_name(record):
    record = record if isinstance(record, dict) else {}
    direct_name = (
        _clean(record.get("display_name"))
        or _clean(record.get("send_name"))
        or _clean(record.get("target"))
        or _clean(record.get("chat_name"))
        or _clean(record.get("contact_key"))
    )
    if direct_name:
        return direct_name
    raw_targets = record.get("raw_targets") if isinstance(record.get("raw_targets"), list) else []
    for item in raw_targets:
        if not isinstance(item, dict):
            continue
        target_name = (
            _clean(item.get("display_name"))
            or _clean(item.get("send_name"))
            or _clean(item.get("target"))
            or _clean(item.get("chat_name"))
            or _clean(item.get("contact_key"))
        )
        if target_name:
            return target_name
    return ""


def _material_outreach_records_targets_summary(records):
    names = []
    seen = set()
    for record in records or []:
        name = _material_outreach_record_target_name(record)
        if not name or name in seen:
            continue
        seen.add(name)
        names.append(name)
    if not names:
        return ""
    if len(names) <= 2:
        return "、".join(names)
    return f"{names[0]}、{names[1]} 等 {len(names)} 人"


def _summarize_moments_tags(tags):
    tags = _clean_list(tags)
    if not tags:
        return ""
    if len(tags) == 1:
        return tags[0]
    if len(tags) == 2:
        return "、".join(tags)
    return f"{tags[0]}、{tags[1]} 等 {len(tags)} 个标签"


def _moments_visibility_summary(task):
    task = task if isinstance(task, dict) else {}
    visibility_type = _clean(task.get("visibility_type")) or "all"
    tags_summary = _summarize_moments_tags(task.get("tags"))
    if visibility_type == "include":
        return f"仅 {tags_summary} 可见" if tags_summary else "部分好友可见"
    if visibility_type == "exclude":
        return f"不给 {tags_summary} 看" if tags_summary else "排除部分好友"
    return "全部好友可见"


def _moments_pending_snapshot(task):
    task = task if isinstance(task, dict) else {}
    text = moments_task_publish_text(task)
    images = _clean_list(task.get("images"))
    return runtime_snapshot(
        raw_targets=[
            {
                "visibility_type": _clean(task.get("visibility_type")) or "all",
                "tags": _clean_list(task.get("tags")),
            }
        ],
        raw_messages=[{"type": "text", "text": text}] if text else [],
        raw_media=[{"type": "image", "path": image} for image in images],
        targets_summary=_moments_visibility_summary(task),
    )


def _snapshot_field_kwargs(snapshot, *, include_result_summary):
    snapshot = snapshot if isinstance(snapshot, dict) else {}
    fields = {
        "targets_summary": _clean(snapshot.get("targets_summary")),
        "content_summary": _clean(snapshot.get("content_summary")),
        "media_summary": _clean(snapshot.get("media_summary")),
        "material_summary": _clean(snapshot.get("material_summary")),
        "batch_summary": _clean(snapshot.get("batch_summary")),
        "raw_targets": snapshot.get("raw_targets"),
        "raw_messages": snapshot.get("raw_messages"),
        "raw_media": snapshot.get("raw_media"),
        "raw_material": snapshot.get("raw_material"),
        "batch_id": _clean(snapshot.get("batch_id")),
        "run_id": _clean(snapshot.get("run_id")),
    }
    if include_result_summary:
        fields["result_summary"] = _clean(snapshot.get("result_summary"))
    return fields


def _build_task_views(module, raw_tasks, *, runtime=None):
    return [
        _build_task_view(module, task, runtime=runtime)
        for task in (raw_tasks or [])
        if isinstance(task, dict)
    ]


def _build_task_view(module, task, *, runtime=None):
    task = task if isinstance(task, dict) else {}
    task_id = _clean(task.get("id") or task.get("task_id"))
    instance_stats = _task_instance_stats(module, task_id, runtime)
    status = _task_status(module, task)
    title = _task_title(module, task)
    summary = _task_summary(module, task)
    fields = _build_task_fields_for_module(module, task)
    runtime_hints = _build_runtime_hints_for_module(module, task, instance_stats=instance_stats)
    timestamps = {
        "created_at": _clean(task.get("created_at")),
        "updated_at": _clean(task.get("updated_at")),
        "queued_at": _clean(task.get("queued_at")),
        "execute_after": _clean(task.get("execute_after")),
        "next_run_at": _clean(task.get("next_run_at") or task.get("next_fire_at")),
        "executed_at": _clean(task.get("executed_at")),
    }
    return {
        "id": task_id,
        "title": title,
        "summary": summary,
        "status": status,
        "fields": fields,
        "editable": _task_editable(module, task),
        "timestamps": timestamps,
        "runtime_hints": runtime_hints,
    }


def _task_title(module, task):
    if module == "scheduled_message":
        return scheduled_message_task_title(task)
    if module == "moments":
        return moments_task_title(task)
    return material_outreach_task_title(task)


def _task_summary(module, task):
    if module == "scheduled_message":
        return _scheduled_attachment_summary(task.get("msgs"))
    if module == "moments":
        snapshot = _moments_pending_snapshot(task)
        return _clean(snapshot.get("media_summary")) or "无图片"
    return _material_outreach_task_copy_summary(task)


def _task_status(module, task):
    if module in {"scheduled_message", "material_outreach"}:
        return "enabled" if bool(task.get("enabled", True)) else "disabled"
    return _clean(task.get("status"))


def _task_instance_stats(module, task_id, runtime):
    stats = {
        "instance_total": 0,
        "instance_pending": 0,
        "instance_running": 0,
    }
    if module == "moments" or not task_id:
        return stats
    queue = list((runtime or {}).get("queue") or [])
    for item in queue:
        if not isinstance(item, dict) or _clean(item.get("task_id")) != task_id:
            continue
        stats["instance_total"] += 1
        status = _clean(item.get("status"))
        if status == "running":
            stats["instance_running"] += 1
        else:
            stats["instance_pending"] += 1
    return stats


def _task_editable(module, task):
    status = _clean(task.get("status"))
    if module == "scheduled_message":
        return status != "running"
    if module == "moments":
        return status != "pending"
    return status != "running"


def _build_task_fields_for_module(module, task):
    if module == "scheduled_message":
        messages = _clean_list(task.get("msgs"))
        return build_task_fields(
            content={
                "text": "\n".join(messages),
                "messages": messages,
            },
            targets={
                "mode": _clean(task.get("targets_mode")) or "all",
                "contact_ids": _clean_list(task.get("targets")),
                "tag_ids": _clean_list(task.get("target_tags")),
                "custom_names": _clean_list(task.get("manual_target_names")),
                "visibility": "all",
            },
            tags=_clean_list(task.get("target_tags")),
            schedule={
                "mode": _clean(task.get("schedule_mode")) or _clean(task.get("trigger_kind")) or "fixed",
                "value": _clean(task.get("start_at")) or _clean(task.get("time_value")),
                "window_start": _clean(task.get("time_window_start")),
                "window_end": _clean(task.get("time_window_end")),
                "repeat_rule": _clean(task.get("repeat_rule")),
                "repeat_values": list(task.get("repeat_values") or []),
            },
            notes=_clean(task.get("return_reason")),
            module_fields={
                "scheduled_message": {
                    "enabled": bool(task.get("enabled", True)),
                }
            },
        )

    if module == "moments":
        return build_task_fields(
            content={
                "text": _clean(task.get("raw_text")),
                "selected": _clean(task.get("selected_caption")),
                "candidates": _clean_list(task.get("candidates")),
                "images": _clean_list(task.get("images")),
            },
            targets={
                "mode": _clean(task.get("visibility_type")) or "all",
                "contact_ids": [],
                "tag_ids": _clean_list(task.get("tags")),
                "custom_names": [],
                "visibility": _clean(task.get("visibility_type")) or "all",
            },
            tags=_clean_list(task.get("tags")),
            schedule={
                "mode": _clean(task.get("publish_rule")) or "random",
                "value": _clean(task.get("publish_time")),
                "window": _clean(task.get("publish_window")),
            },
            notes=_clean(task.get("execution_message")),
            module_fields={
                "moments": {
                    "copy_mode": _clean(task.get("copy_mode")) or "ai",
                    "enabled": bool(task.get("enabled", True)),
                }
            },
        )

    selector = task.get("target_selector") if isinstance(task.get("target_selector"), dict) else {}
    include_tags = _clean_list(selector.get("include_tags"))
    exclude_tags = _clean_list(selector.get("exclude_tags"))
    return build_task_fields(
        content={
            "text": _clean(task.get("material_title")) or _clean(task.get("name")),
            "material_types": _clean_list(task.get("material_types")),
        },
        targets={
            "mode": _clean(selector.get("base")) or "all_friends",
            "contact_ids": _clean_list(selector.get("include_contact_keys")),
            "tag_ids": include_tags,
            "custom_names": _clean_list(task.get("manual_target_names")),
            "visibility": "all",
        },
        tags=include_tags + [f"!{value}" for value in exclude_tags],
        schedule={
            "mode": _clean(task.get("trigger_strategy")) or "fixed",
            "value": _clean(task.get("start_at")) or _clean(task.get("time")) or _clean(task.get("next_fire_at")),
            "repeat_type": _clean(task.get("repeat_type")),
            "weekdays": list(task.get("weekdays") or []),
            "dates": list(task.get("dates") or []),
        },
        notes=_clean(task.get("last_error")),
        module_fields={
            "material": {
                "enabled": bool(task.get("enabled", True)),
                "fixed_material_id": _clean(task.get("fixed_material_id")),
            }
        },
    )


def _build_runtime_hints_for_module(module, task, *, instance_stats=None):
    instance_stats = instance_stats if isinstance(instance_stats, dict) else {}
    if module == "scheduled_message":
        hints = {
            "enabled": bool(task.get("enabled", True)),
            "next_run_at": _clean(task.get("next_run_at")),
            "current_run_id": _clean(task.get("current_run_id")),
            "run_started_at": _clean(task.get("run_started_at")),
            "instance_total": int(instance_stats.get("instance_total") or 0),
            "instance_pending": int(instance_stats.get("instance_pending") or 0),
            "instance_running": int(instance_stats.get("instance_running") or 0),
        }
    elif module == "moments":
        hints = {
            "enabled": bool(task.get("enabled", True)),
            "execute_after": _clean(task.get("execute_after")),
            "queued_mode": _clean(task.get("queued_mode")),
            "ai_generation_status": _clean(task.get("ai_generation_status")),
        }
    else:
        hints = {
            "enabled": bool(task.get("enabled", True)),
            "next_fire_at": _clean(task.get("next_fire_at")),
            "execute_after": _clean(task.get("execute_after")),
            "fixed_material_id": _clean(task.get("fixed_material_id")),
            "last_error": _clean(task.get("last_error")),
            "instance_total": int(instance_stats.get("instance_total") or 0),
            "instance_pending": int(instance_stats.get("instance_pending") or 0),
            "instance_running": int(instance_stats.get("instance_running") or 0),
        }
    return build_runtime_hints(hints)


def _build_runtime(module, *, tasks, data_dir, wx_id):
    if module == "scheduled_message":
        queue = _sort_runtime_items(_build_scheduled_message_manual_queue(tasks), time_field="scheduled_at")
        executions = _sort_runtime_items(_build_scheduled_message_executions(tasks), time_field="executed_at")
    elif module == "moments":
        queue = _sort_runtime_items(_build_moments_manual_queue(tasks), time_field="scheduled_at")
        executions = _sort_runtime_items(_build_moments_executions(tasks), time_field="executed_at")
    else:
        queue = _sort_runtime_items(_build_material_outreach_queue(tasks, data_dir=data_dir, wx_id=wx_id), time_field="scheduled_at")
        executions = _sort_runtime_items(_build_material_outreach_executions(data_dir=data_dir, wx_id=wx_id), time_field="executed_at")
    return {
        "queue": queue,
        "executions": executions,
        "stats": _build_runtime_stats(queue, executions),
    }


def _sort_runtime_items(items, *, time_field):
    items = [dict(item) for item in (items or []) if isinstance(item, dict)]
    return sorted(
        items,
        key=lambda item: (
            _clean(item.get(time_field)),
            _clean(item.get("execution_id") or item.get("queue_id") or item.get("task_id") or item.get("title")),
        ),
        reverse=True,
    )


def _build_runtime_stats(queue, executions):
    queue = [item for item in (queue or []) if isinstance(item, dict)]
    executions = [item for item in (executions or []) if isinstance(item, dict)]
    return {
        "queue_total": len(queue),
        "queue_pending": sum(1 for item in queue if item.get("status") == "pending"),
        "queue_running": sum(1 for item in queue if item.get("status") == "running"),
        "execution_total": len(executions),
        "execution_success": sum(1 for item in executions if _clean(item.get("result")) == "success"),
        "execution_failed": sum(1 for item in executions if _clean(item.get("result")) == "failed"),
        "execution_skipped": sum(1 for item in executions if _clean(item.get("result")) == "skipped"),
    }


def _load_tasks(module, *, data_dir, wx_id):
    if module == "scheduled_message":
        return _load_scheduled_message_tasks(data_dir=data_dir, wx_id=wx_id)
    if module == "moments":
        return _load_moments_tasks(data_dir=data_dir, wx_id=wx_id)
    return _load_material_outreach_tasks(data_dir=data_dir, wx_id=wx_id)


def _load_scheduled_message_tasks(*, data_dir, wx_id):
    storage = _storage("scheduled_message", data_dir=data_dir, wx_id=wx_id)
    definitions = storage.load_tasks()
    runtime_map = storage.load_runtime()
    history_map = storage.load_history()
    return deserialize_scheduled_message_task_collection(definitions, runtime_map, history_map)


def _save_scheduled_message_tasks(tasks, *, data_dir, wx_id):
    storage = _storage("scheduled_message", data_dir=data_dir, wx_id=wx_id)
    normalized = [
        normalize_scheduled_message_task_payload(task)
        for task in (tasks or [])
        if isinstance(task, dict)
    ]
    definitions, runtime_map, history_map = serialize_scheduled_message_task_collection(normalized)
    storage.save_tasks(definitions)
    storage.save_runtime(runtime_map)
    storage.save_history(history_map)
    return normalized


def _save_scheduled_message_definitions_only(tasks, *, data_dir, wx_id):
    storage = _storage("scheduled_message", data_dir=data_dir, wx_id=wx_id)
    normalized = [
        normalize_scheduled_message_task_payload(task)
        for task in (tasks or [])
        if isinstance(task, dict)
    ]
    definitions, _runtime_map, _history_map = serialize_scheduled_message_task_collection(normalized)
    storage.save_tasks(definitions)
    return normalized


def _save_scheduled_message_runtime_record(task, *, data_dir, wx_id):
    storage = _storage("scheduled_message", data_dir=data_dir, wx_id=wx_id)
    definition, runtime_record, _history = split_scheduled_message_task_storage(task)
    task_id = _clean(definition.get("id"))
    if not task_id:
        raise TaskWorkbenchServiceError("定时消息任务ID无效", 400)
    storage.mutate_runtime(
        lambda runtime_map: {
            **(runtime_map if isinstance(runtime_map, dict) else {}),
            task_id: runtime_record,
        }
    )
    return runtime_record


def _save_scheduled_message_runtime_and_history_record(task, *, data_dir, wx_id):
    storage = _storage("scheduled_message", data_dir=data_dir, wx_id=wx_id)
    definition, runtime_record, history_record = split_scheduled_message_task_storage(task)
    task_id = _clean(definition.get("id"))
    if not task_id:
        raise TaskWorkbenchServiceError("定时消息任务ID无效", 400)
    storage.mutate_runtime(
        lambda runtime_map: {
            **(runtime_map if isinstance(runtime_map, dict) else {}),
            task_id: runtime_record,
        }
    )
    storage.mutate_history(
        lambda history_map: {
            **(history_map if isinstance(history_map, dict) else {}),
            task_id: history_record,
        }
    )
    return runtime_record, history_record


def _load_moments_tasks(*, data_dir, wx_id):
    storage = _storage("moments", data_dir=data_dir, wx_id=wx_id)
    definitions = storage.load_tasks()
    runtime_map = storage.load_runtime()
    history_map = storage.load_history()
    return deserialize_moments_task_collection(definitions, runtime_map, history_map)


def _save_moments_tasks(tasks, *, data_dir, wx_id):
    storage = _storage("moments", data_dir=data_dir, wx_id=wx_id)
    normalized = [
        normalize_moments_task(task)
        for task in (tasks or [])
        if isinstance(task, dict)
    ]
    definitions, runtime_map, history_map = serialize_moments_task_collection(normalized)
    storage.save_tasks(definitions)
    storage.save_runtime(runtime_map)
    storage.save_history(history_map)
    return normalized


def _save_moments_definitions_only(tasks, *, data_dir, wx_id):
    storage = _storage("moments", data_dir=data_dir, wx_id=wx_id)
    normalized = [
        normalize_moments_task(task)
        for task in (tasks or [])
        if isinstance(task, dict)
    ]
    definitions, _runtime_map, _history_map = serialize_moments_task_collection(normalized)
    storage.save_tasks(definitions)
    return normalized


def _save_moments_runtime_record(task, *, data_dir, wx_id):
    storage = _storage("moments", data_dir=data_dir, wx_id=wx_id)
    definition, runtime_record, _history = split_moments_task_storage(task)
    task_id = _clean(definition.get("id"))
    if not task_id:
        raise TaskWorkbenchServiceError("朋友圈任务ID无效", 400)
    storage.mutate_runtime(
        lambda runtime_map: {
            **(runtime_map if isinstance(runtime_map, dict) else {}),
            task_id: runtime_record,
        }
    )
    return runtime_record


def _save_moments_runtime_and_history_record(task, *, data_dir, wx_id):
    storage = _storage("moments", data_dir=data_dir, wx_id=wx_id)
    definition, runtime_record, history_record = split_moments_task_storage(task)
    task_id = _clean(definition.get("id"))
    if not task_id:
        raise TaskWorkbenchServiceError("朋友圈任务ID无效", 400)
    storage.mutate_runtime(
        lambda runtime_map: {
            **(runtime_map if isinstance(runtime_map, dict) else {}),
            task_id: runtime_record,
        }
    )
    storage.mutate_history(
        lambda history_map: {
            **(history_map if isinstance(history_map, dict) else {}),
            task_id: history_record,
        }
    )
    return runtime_record, history_record


def _load_material_outreach_tasks(*, data_dir, wx_id):
    storage = _storage("material_outreach", data_dir=data_dir, wx_id=wx_id)
    tasks = storage.load_tasks()
    materials_path = storage.module_file("materials.json", create_parent=False)
    materials = load_json_list(materials_path)
    materials_by_id = {
        _clean(item.get("id")): item
        for item in materials
        if isinstance(item, dict) and _clean(item.get("id"))
    }
    loaded_tasks = []
    for task in tasks or []:
        if not isinstance(task, dict):
            continue
        task_copy = dict(task)
        fixed_material_id = _clean(task_copy.get("fixed_material_id") or task_copy.get("material_id"))
        if fixed_material_id and fixed_material_id in materials_by_id:
            material = materials_by_id[fixed_material_id]
            task_copy["material_title"] = _clean(material.get("content_preview")) or _clean(material.get("title"))
        loaded_tasks.append(task_copy)
    return loaded_tasks


def _save_material_outreach_tasks(tasks, *, data_dir, wx_id):
    storage = _storage("material_outreach", data_dir=data_dir, wx_id=wx_id)
    return storage.save_tasks([task for task in (tasks or []) if isinstance(task, dict)])


def _load_material_outreach_runtime(*, data_dir, wx_id):
    storage = _storage("material_outreach", data_dir=data_dir, wx_id=wx_id)
    return normalize_material_outreach_runtime_payload(storage.load_runtime())


def _save_material_outreach_runtime(runtime, *, data_dir, wx_id):
    storage = _storage("material_outreach", data_dir=data_dir, wx_id=wx_id)
    normalized = normalize_material_outreach_runtime_payload(runtime)
    storage.save_runtime(normalized)
    return normalized


def _load_material_outreach_runtime_raw(*, data_dir, wx_id):
    storage = _storage("material_outreach", data_dir=data_dir, wx_id=wx_id)
    runtime = storage.load_runtime()
    return dict(runtime) if isinstance(runtime, dict) else {}


def _save_material_outreach_runtime_raw(runtime, *, data_dir, wx_id):
    storage = _storage("material_outreach", data_dir=data_dir, wx_id=wx_id)
    return storage.save_runtime(runtime if isinstance(runtime, dict) else {})


def _load_material_outreach_history(*, data_dir, wx_id):
    storage = _storage("material_outreach", data_dir=data_dir, wx_id=wx_id)
    return normalize_material_outreach_history_payload(storage.load_history())


def _load_material_outreach_materials(*, data_dir, wx_id):
    storage = _storage("material_outreach", data_dir=data_dir, wx_id=wx_id)
    return load_json_list(storage.module_file("materials.json", create_parent=False))


def _build_scheduled_message_manual_queue(tasks):
    items = []
    for task in tasks or []:
        if not isinstance(task, dict):
            continue
        task_id = _clean(task.get("id"))
        status = _clean(task.get("status"))
        if task_id and status in {STATUS_PENDING, STATUS_RUNNING}:
            snapshot = task.get("pending_snapshot") if isinstance(task.get("pending_snapshot"), dict) else {}
            scheduled_at = (
                _clean(task.get("next_run_at"))
                or _clean(task.get("execute_after"))
                or _clean(task.get("start_at"))
                or _clean(task.get("time_value"))
                or "等待调度"
            )
            items.append(
                build_queue_item(
                    module="scheduled_message",
                    task_id=task_id,
                    source="system",
                    title=scheduled_message_task_title(task),
                    detail=_clean(snapshot.get("media_summary")) or _scheduled_attachment_summary(task.get("msgs")),
                    scheduled_at=scheduled_at,
                    status=_normalize_queue_status(status),
                    **_snapshot_field_kwargs(snapshot, include_result_summary=False),
                )
            )
    return items


def _build_scheduled_message_executions(tasks):
    items = []
    for task in tasks or []:
        if not isinstance(task, dict):
            continue
        task_id = _clean(task.get("id"))
        title = scheduled_message_task_title(task)
        for index, record in enumerate(task.get("run_history") or [], start=1):
            if not isinstance(record, dict):
                continue
            snapshot = record
            execution_id = _clean(record.get("run_id")) or f"{task_id}:history:{index}"
            failed_count = int(record.get("failed_count") or 0)
            result_type = _clean(record.get("result_type"))
            if failed_count > 0 and int(record.get("success_count") or 0) <= 0:
                result = "failed"
            elif failed_count > 0:
                result = "partial_success"
            else:
                result = "success"
            result_summary = f"失败 {failed_count} 次" if failed_count > 0 else ""
            items.append(
                build_execution_item(
                    module="scheduled_message",
                    execution_id=execution_id,
                    task_id=task_id,
                    title=title,
                    detail=_clean(snapshot.get("media_summary")) or _scheduled_attachment_summary(task.get("msgs")),
                    executed_at=_clean(record.get("finished_at")) or _clean(record.get("started_at")),
                    status="executed",
                    result=result,
                    result_message=_clean(record.get("summary")) or result_type or "执行完成",
                    result_summary=result_summary,
                    **_snapshot_field_kwargs(snapshot, include_result_summary=False),
                )
            )
    items.sort(key=lambda item: _clean(item.get("executed_at")), reverse=True)
    return items[:50]


def _clear_scheduled_message_executions(*, data_dir, wx_id):
    storage = _storage("scheduled_message", data_dir=data_dir, wx_id=wx_id)
    history_map = storage.load_history()
    history_map = dict(history_map) if isinstance(history_map, dict) else {}
    for task_id, records in list(history_map.items()):
        if isinstance(records, list):
            history_map[task_id] = []
    storage.save_history(history_map)


def _queue_scheduled_message_task(task_id, *, data_dir, wx_id, hooks):
    tasks = _load_scheduled_message_tasks(data_dir=data_dir, wx_id=wx_id)
    task = _find_task(tasks, task_id)
    if task is None:
        raise TaskWorkbenchServiceError("定时消息任务不存在", 404)
    if not task.get("enabled", True):
        raise TaskWorkbenchServiceError("请先启用这条定时消息任务", 400)
    if _clean(task.get("status") or "pending_confirm") != "pending_confirm":
        raise TaskWorkbenchServiceError("只有待确认的定时消息任务可以加入队列", 400)
    if not task.get("msgs"):
        raise TaskWorkbenchServiceError("请先添加消息内容", 400)
    targets_mode = _clean(task.get("targets_mode") or "all") or "all"
    selected_tags = list(task.get("target_tags") or []) + list(task.get("exclude_target_tags") or [])
    manual_names = list(task.get("manual_target_names") or [])
    if targets_mode != "all" and not selected_tags and not manual_names:
        raise TaskWorkbenchServiceError("请先选择标签或填写昵称/备注", 400)

    initial_next_run_at = (
        _clean(task.get("next_run_at"))
        or _clean(task.get("fire_at"))
        or _clean(task.get("start_at"))
    )
    updated_task = queue_scheduled_message_task(task, next_run_at=initial_next_run_at)
    updated_task = ensure_scheduled_message_next_run(updated_task, now=datetime.now())
    if not updated_task.get("next_run_at"):
        raise TaskWorkbenchServiceError("无法计算这条定时消息的下次执行时间，请检查时间规则", 400)
    _save_scheduled_message_runtime_record(updated_task, data_dir=data_dir, wx_id=wx_id)
    _call_reload(hooks)
    return updated_task


def _cancel_scheduled_message_queue_item(task_id, *, data_dir, wx_id, hooks):
    tasks = _load_scheduled_message_tasks(data_dir=data_dir, wx_id=wx_id)
    task = _find_task(tasks, task_id)
    if task is None:
        raise TaskWorkbenchServiceError("定时消息任务不存在", 404)
    status = _clean(task.get("status") or "pending_confirm") or "pending_confirm"
    if status == STATUS_RUNNING:
        updated_task = return_scheduled_message_task(task, reason="manual_stop", summary="已中止")
        message = "已中止这条定时消息任务"
        _save_scheduled_message_runtime_and_history_record(updated_task, data_dir=data_dir, wx_id=wx_id)
    elif status == STATUS_PENDING:
        updated_task = normalize_scheduled_message_task_payload(
            {
                **task,
                "status": "pending_confirm",
                "next_run_at": "",
                "current_run_id": "",
                "run_started_at": "",
                "pending_snapshot": {},
                "last_result": {},
                "return_reason": "",
                "stop_requested": False,
            }
        )
        message = "已取消这条定时消息待执行实例"
        _save_scheduled_message_runtime_record(updated_task, data_dir=data_dir, wx_id=wx_id)
    elif status == "pending_confirm":
        updated_task = normalize_scheduled_message_task_payload(
            {
                **task,
                "status": "pending_confirm",
                "next_run_at": "",
                "current_run_id": "",
                "run_started_at": "",
                "pending_snapshot": {},
                "last_result": {},
                "return_reason": "",
                "stop_requested": False,
            }
        )
        message = "已取消这条定时消息待执行实例"
        _save_scheduled_message_runtime_record(updated_task, data_dir=data_dir, wx_id=wx_id)
    else:
        raise TaskWorkbenchServiceError("这条定时消息任务不在待执行队列中", 400)
    _call_reload(hooks)
    return updated_task, message


def _build_moments_manual_queue(tasks):
    items = []
    for task in tasks or []:
        if not isinstance(task, dict):
            continue
        task_id = _clean(task.get("id"))
        normalized_status = _normalize_queue_status(task.get("status"))
        if not task_id or normalized_status not in {"pending", "running"}:
            continue
        snapshot = _moments_pending_snapshot(task)
        items.append(
            build_queue_item(
                module="moments",
                task_id=task_id,
                source="manual",
                title=moments_task_title(task),
                detail=_clean(snapshot.get("media_summary")) or "无图片",
                scheduled_at=_clean(task.get("execute_after")) or "等待调度",
                status=normalized_status,
                **_snapshot_field_kwargs(snapshot, include_result_summary=False),
            )
        )
    return items


def _build_moments_executions(tasks):
    items = []
    for task in tasks or []:
        if not isinstance(task, dict):
            continue
        task_id = _clean(task.get("id"))
        executed_at = _clean(task.get("executed_at")) or _clean(task.get("updated_at"))
        execution_result = _clean(task.get("execution_result"))
        snapshot = task.get("execution_snapshot") if isinstance(task.get("execution_snapshot"), dict) else {}
        if not (_clean(task.get("executed_at")) or execution_result):
            continue
        elif execution_result in {"failed", "error"}:
            item_result = "failed"
        else:
            item_result = "success"
        result_summary = (
            _clean(task.get("execution_message")) or execution_result
            if item_result == "failed"
            else ""
        )
        items.append(
            build_execution_item(
                module="moments",
                execution_id=f"{task_id}:{_clean(task.get('executed_at')) or 'execution'}",
                task_id=task_id,
                title=_clean(snapshot.get("content_summary")) or moments_task_title(task),
                detail=_clean(snapshot.get("media_summary")) or "无图片",
                executed_at=executed_at,
                status="executed",
                result=item_result,
                result_message=_clean(task.get("execution_message")) or execution_result or "已执行",
                result_summary=result_summary,
                **_snapshot_field_kwargs(snapshot, include_result_summary=False),
            )
        )
    items.sort(key=lambda item: _clean(item.get("executed_at")), reverse=True)
    return items[:50]


def _clear_moments_executions(*, data_dir, wx_id):
    storage = _storage("moments", data_dir=data_dir, wx_id=wx_id)
    history_map = storage.load_history()
    history_map = dict(history_map) if isinstance(history_map, dict) else {}
    for task_id, record in list(history_map.items()):
        if isinstance(record, dict):
            history_map[task_id] = {
                **record,
                "executed_at": "",
                "execution_result": "",
                "execution_message": "",
                "execution_snapshot": {},
            }
    storage.save_history(history_map)


def _queue_moments_task(task_id, *, data_dir, wx_id, payload, hooks):
    mode = _clean(payload.get("mode") or "queue")
    if mode == "immediate":
        raise TaskWorkbenchServiceError("朋友圈任务不支持立即执行，请先确认生成待发布实例", 400)
    if mode != "queue":
        mode = "queue"

    tasks = _load_moments_tasks(data_dir=data_dir, wx_id=wx_id)
    task = _find_task(tasks, task_id)
    if task is None:
        raise TaskWorkbenchServiceError("朋友圈任务不存在", 404)
    if _clean(task.get("status")) != "pending_confirm":
        raise TaskWorkbenchServiceError("只有待确认的朋友圈任务可以加入队列", 400)
    if not moments_task_has_ai_candidates(task):
        raise TaskWorkbenchServiceError("AI文案模式需要先生成 AI候选，或切换为原始文案后再发布", 400)
    if not task.get("selected_caption") and not task.get("raw_text") and not task.get("images"):
        raise TaskWorkbenchServiceError("这条朋友圈任务没有可发布内容", 400)

    updated_task = queue_moments_task(task, mode=mode)
    _save_moments_runtime_and_history_record(updated_task, data_dir=data_dir, wx_id=wx_id)
    _call_reload(hooks)
    return updated_task


def _cancel_moments_queue_item(task_id, *, data_dir, wx_id, hooks):
    tasks = _load_moments_tasks(data_dir=data_dir, wx_id=wx_id)
    task = _find_task(tasks, task_id)
    if task is None:
        raise TaskWorkbenchServiceError("朋友圈任务不存在", 404)
    if _clean(task.get("status")) == "pending":
        updated_task = cancel_queued_moments_task(task)
    elif _clean(task.get("status")) == "pending_confirm":
        updated_task = normalize_moments_task(task)
    else:
        raise TaskWorkbenchServiceError("这条朋友圈任务不在待执行队列中", 400)
    _save_moments_runtime_and_history_record(updated_task, data_dir=data_dir, wx_id=wx_id)
    _call_reload(hooks)
    return updated_task, "已取消这条朋友圈待执行实例"


def _material_outreach_record_status(source_kind, record):
    record = record if isinstance(record, dict) else {}
    raw_status = _clean(record.get("status"))
    success_flag = record.get("success")
    if source_kind == "skip" and not raw_status:
        return "skipped"
    if raw_status in {"success", "sent"} or success_flag is True:
        return "success"
    if raw_status in {"failed", "error"} or success_flag is False:
        return "failed"
    if raw_status == "skipped":
        return "skipped"
    if raw_status == "running":
        return "running"
    if raw_status in {"pending"}:
        return "pending"
    if source_kind == "skip":
        return "skipped"
    if source_kind == "send":
        return "failed"
    return "pending"


def _material_outreach_record_time(record):
    record = record if isinstance(record, dict) else {}
    return (
        _clean(record.get("time"))
        or _clean(record.get("created_at"))
        or _clean(record.get("sent_at"))
        or _clean(record.get("executed_at"))
        or _clean(record.get("finished_at"))
        or _clean(record.get("scheduled_at"))
    )


def _material_outreach_group_key(record, index):
    record = record if isinstance(record, dict) else {}
    batch_id = _clean(record.get("batch_id"))
    if batch_id:
        return f"batch:{batch_id}"
    run_id = _clean(record.get("run_id"))
    if run_id:
        return f"run:{run_id}"
    task_id = _clean(record.get("task_id"))
    target = _clean(record.get("contact_key")) or _clean(record.get("send_name")) or _clean(record.get("target"))
    return f"single:{task_id}:{target or index}:{index}"


def _pick_material_outreach_snapshot(records):
    snapshot = {
        "targets_summary": "",
        "content_summary": "",
        "media_summary": "",
        "material_summary": "",
        "batch_summary": "",
        "raw_targets": [],
        "raw_messages": [],
        "raw_media": [],
        "raw_material": {},
        "batch_id": "",
        "run_id": "",
    }
    for record in records:
        for key in ("targets_summary", "content_summary", "media_summary", "material_summary", "batch_summary", "batch_id", "run_id"):
            if not snapshot[key]:
                snapshot[key] = _clean(record.get(key))
        if not snapshot["raw_targets"] and isinstance(record.get("raw_targets"), list):
            snapshot["raw_targets"] = list(record.get("raw_targets") or [])
        if not snapshot["raw_messages"] and isinstance(record.get("raw_messages"), list):
            snapshot["raw_messages"] = list(record.get("raw_messages") or [])
        if not snapshot["raw_media"] and isinstance(record.get("raw_media"), list):
            snapshot["raw_media"] = list(record.get("raw_media") or [])
        if not snapshot["raw_material"] and isinstance(record.get("raw_material"), dict):
            snapshot["raw_material"] = dict(record.get("raw_material") or {})
    return snapshot


def _material_outreach_failure_reason(records):
    for record in records or []:
        status = _material_outreach_record_status("", record)
        if status != "failed":
            continue
        reason = (
            _clean(record.get("error"))
            or _clean(record.get("reason"))
            or _clean(record.get("detail"))
            or _clean(record.get("status_label"))
        )
        if reason:
            return reason
    return ""


def _material_outreach_result_summary(counts, failure_reason=""):
    parts = []
    if counts["failed"]:
        failed_summary = f"失败 {counts['failed']} 人"
        if failure_reason:
            failed_summary = f"{failed_summary}：{failure_reason}"
        parts.append(failed_summary)
    if counts["skipped"]:
        parts.append(f"跳过 {counts['skipped']} 人")
    return "，".join(parts)


def _material_outreach_execution_result(counts):
    if counts["failed"] > 0:
        return "failed" if counts["success"] <= 0 else "partial_success"
    if counts["success"] > 0:
        return "success"
    if counts["skipped"] > 0:
        return "skipped"
    if counts["running"] > 0:
        return "running"
    return "pending"


def _build_material_outreach_queue(tasks, *, data_dir, wx_id):
    queue = []
    runtime = _load_material_outreach_runtime(data_dir=data_dir, wx_id=wx_id)
    for record in runtime.get("preface_pending_queue", []):
        if not isinstance(record, dict):
            continue
        if not _queue_status_is_active(record.get("status") or "pending"):
            continue
        queue_record_id = _clean(record.get("queue_id"))
        if not queue_record_id:
            continue
        detail = _material_outreach_record_copy_summary(record)
        fields = _snapshot_field_kwargs(record, include_result_summary=False)
        fields["content_summary"] = detail
        queue.append(
            build_queue_item(
                module="material_outreach",
                task_id=_clean(record.get("task_id")),
                queue_id=queue_record_id,
                source="preface",
                title=material_outreach_record_title(record),
                detail=detail,
                scheduled_at=_clean(record.get("scheduled_at")) or "等待调度",
                status=_normalize_queue_status(record.get("status") or "pending"),
                **fields,
            )
        )
    for record in runtime.get("ai_pending_queue", []):
        if not isinstance(record, dict):
            continue
        if not _queue_status_is_active(record.get("status") or "pending"):
            continue
        queue_record_id = _clean(record.get("queue_id"))
        if not queue_record_id:
            continue
        real_task_id = _clean(record.get("task_id"))
        detail = _material_outreach_record_copy_summary(record)
        fields = _snapshot_field_kwargs(record, include_result_summary=False)
        fields["content_summary"] = detail
        queue.append(
            build_queue_item(
                module="material_outreach",
                task_id=real_task_id,
                queue_id=queue_record_id,
                source="ai",
                title=material_outreach_record_title(record),
                detail=detail,
                scheduled_at=_clean(record.get("scheduled_at")) or "等待调度",
                status=_normalize_queue_status(record.get("status") or "pending"),
                **fields,
            )
        )
    return queue


def _build_material_outreach_executions(*, data_dir, wx_id):
    history = _load_material_outreach_history(data_dir=data_dir, wx_id=wx_id)
    items = []
    grouped = {}
    records = []
    records.extend([("send", item) for item in history.get("send_records", []) if isinstance(item, dict)])
    records.extend([("skip", item) for item in history.get("skip_records", []) if isinstance(item, dict)])
    records.extend([("progress", item) for item in history.get("progress_records", []) if isinstance(item, dict)])
    for index, (source_kind, record) in enumerate(records, start=1):
        record = record if isinstance(record, dict) else {}
        status = _material_outreach_record_status(source_kind, record)
        if source_kind == "progress" and status not in {"success", "failed", "skipped"}:
            continue
        key = _material_outreach_group_key(record, index)
        grouped.setdefault(key, []).append((source_kind, record, status, index))

    for group_key, group_records in grouped.items():
        plain_records = [record for _, record, _, _ in group_records]
        snapshot = _pick_material_outreach_snapshot(plain_records)
        counts = {
            "success": 0,
            "failed": 0,
            "skipped": 0,
            "running": 0,
            "pending": 0,
        }
        latest_by_target = {}
        for source_kind, record, status, index in group_records:
            target_key = _material_outreach_record_target_key(record, index=index)
            timestamp = _material_outreach_record_time(record) or str(index)
            previous = latest_by_target.get(target_key)
            if previous is None or timestamp >= previous[0]:
                latest_by_target[target_key] = (timestamp, status)
        for _, status in latest_by_target.values():
            if status in counts:
                counts[status] += 1
        task_id = _clean(plain_records[0].get("task_id")) if plain_records else ""
        executed_at = max((_material_outreach_record_time(record) or "" for record in plain_records), default="")
        title = material_outreach_record_title(plain_records[0] if plain_records else {})
        detail = _material_outreach_group_copy_summary(plain_records, snapshot)
        snapshot["content_summary"] = detail
        if not snapshot["targets_summary"]:
            snapshot["targets_summary"] = _material_outreach_records_targets_summary(plain_records)
        failure_reason = _material_outreach_failure_reason(plain_records)
        result_summary = _material_outreach_result_summary(counts, failure_reason=failure_reason)
        latest_detail = ""
        for record in reversed(plain_records):
            latest_detail = _clean(record.get("detail")) or _clean(record.get("status_label")) or _clean(record.get("status"))
            if latest_detail:
                break
        result = _material_outreach_execution_result(counts)
        items.append(
            build_execution_item(
                module="material_outreach",
                execution_id=snapshot["batch_id"] or snapshot["run_id"] or group_key,
                task_id=task_id,
                title=title,
                detail=detail,
                executed_at=executed_at or group_key,
                status="executed",
                result=result,
                result_message=latest_detail or result_summary,
                result_summary=result_summary,
                **_snapshot_field_kwargs(snapshot, include_result_summary=False),
            )
        )
    items.sort(key=lambda item: _clean(item.get("executed_at")), reverse=True)
    return items[:50]


def _material_outreach_record_target_key(record, *, index):
    record = record if isinstance(record, dict) else {}
    direct_key = (
        _clean(record.get("send_name"))
        or _clean(record.get("display_name"))
        or _clean(record.get("target"))
        or _clean(record.get("contact_key"))
    )
    if direct_key:
        return direct_key
    raw_targets = record.get("raw_targets") if isinstance(record.get("raw_targets"), list) else []
    for item in raw_targets:
        if not isinstance(item, dict):
            continue
        matched_key = (
            _clean(item.get("send_name"))
            or _clean(item.get("display_name"))
            or _clean(item.get("target"))
            or _clean(item.get("contact_key"))
        )
        if matched_key:
            return matched_key
    return f"__group__{index}"


def _clear_material_outreach_executions(*, data_dir, wx_id):
    storage = _storage("material_outreach", data_dir=data_dir, wx_id=wx_id)
    history = storage.load_history()
    history = dict(history) if isinstance(history, dict) else {}
    for key in ("send_records", "skip_records", "progress_records"):
        if isinstance(history.get(key), list):
            history[key] = []
    storage.save_history(history)


def _cancel_material_outreach_queue_item(queue_id, *, data_dir, wx_id, hooks):
    if queue_id.startswith("ai:"):
        ai_queue_id = _clean(queue_id.split(":", 1)[1])
        storage = _storage("material_outreach", data_dir=data_dir, wx_id=wx_id)
        changed = {"ok": False}

        def _cancel_ai(runtime):
            queue_records = runtime.get("ai_pending_queue")
            queue_records = list(queue_records) if isinstance(queue_records, list) else []
            if cancel_ai_pending_record(queue_records, ai_queue_id):
                runtime["ai_pending_queue"] = queue_records
                changed["ok"] = True
            return runtime

        storage.mutate_runtime(_cancel_ai)
        if not changed["ok"]:
            raise TaskWorkbenchServiceError("未找到可取消的待发送记录", 404)
        return {"queue_id": ai_queue_id, "removed": True}, "已取消这条 AI 聊天智能转发待发送记录"

    if queue_id.startswith("preface:"):
        preface_queue_id = _clean(queue_id.split(":", 1)[1])
        storage = _storage("material_outreach", data_dir=data_dir, wx_id=wx_id)
        changed = {"ok": False}

        def _cancel_preface(runtime):
            queue_records = runtime.get("preface_pending_queue")
            queue_records = list(queue_records) if isinstance(queue_records, list) else []
            if cancel_preface_pending_record(queue_records, preface_queue_id):
                runtime["preface_pending_queue"] = queue_records
                changed["ok"] = True
            return runtime

        storage.mutate_runtime(_cancel_preface)
        if not changed["ok"]:
            raise TaskWorkbenchServiceError("未找到可取消的 AI 预生成文案记录", 404)
        return {"queue_id": preface_queue_id, "removed": True}, "已取消这条素材转发 AI 预生成文案记录"

    raise TaskWorkbenchServiceError("不支持的队列ID", 400)


def _find_task(tasks, task_id):
    for task in tasks or []:
        if not isinstance(task, dict):
            continue
        if _clean(task.get("id") or task.get("task_id")) == task_id:
            return task
    return None


def _find_queue_item(queue, queue_id):
    for item in queue or []:
        if not isinstance(item, dict):
            continue
        if _clean(item.get("queue_id")) == queue_id:
            return item
    return None


def _require_manual_task_id(queue_id):
    if not queue_id.startswith("manual:"):
        raise TaskWorkbenchServiceError("仅支持取消手动待执行实例", 400)
    task_id = _clean(queue_id.split(":", 1)[1])
    if not task_id:
        raise TaskWorkbenchServiceError("无效的队列ID", 400)
    return task_id


def _call_reload(hooks):
    callback = hooks.get("reload_runtime")
    if callback is None:
        return
    callback()
