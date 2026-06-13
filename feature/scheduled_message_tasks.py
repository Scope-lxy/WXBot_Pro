"""Scheduled-message task helpers for the taskized panel and runtime."""

from copy import deepcopy
from datetime import datetime, timedelta
import random

from core.scheduled_tasks import (
    advance_task_plan_after_success,
    compile_task_plan,
    normalize_fixed_task_schedule,
    normalize_random_task_schedule,
)

STATUS_PENDING_CONFIRM = "pending_confirm"
STATUS_PENDING = "pending"
STATUS_RUNNING = "running"
STATUS_EXECUTED = "executed"

SCHEDULED_MESSAGE_TASK_DEFINITION_FIELDS = (
    "id",
    "name",
    "enabled",
    "trigger_kind",
    "schedule_mode",
    "repeat_mode",
    "repeat_rule",
    "repeat_values",
    "time_value",
    "time_window_start",
    "time_window_end",
    "start_at",
    "execute_after",
    "targets_mode",
    "target_tags",
    "exclude_target_tags",
    "manual_target_names",
    "targets",
    "msgs",
)

SCHEDULED_MESSAGE_TASK_RUNTIME_FIELDS = (
    "status",
    "next_run_at",
    "current_run_id",
    "run_started_at",
    "pending_snapshot",
    "last_result",
    "return_reason",
    "stop_requested",
)

def _clean_str(value):
    return str(value or "").strip()


def _clean_list(values):
    cleaned = []
    for item in values or []:
        text = _clean_str(item)
        if text:
            cleaned.append(text)
    return cleaned


def _iso_datetime(value):
    if not isinstance(value, datetime):
        return ""
    return value.replace(microsecond=0).isoformat()


def _parse_datetime(value):
    try:
        return datetime.fromisoformat(str(value or ""))
    except ValueError:
        return None


def _parse_hhmm(value, default="00:00"):
    text = str(value or default).strip() or default
    try:
        hour_text, minute_text = text.split(":")
        hour = max(0, min(23, int(hour_text or 0)))
        minute = max(0, min(59, int(minute_text or 0)))
        return hour, minute
    except (TypeError, ValueError):
        return _parse_hhmm(default, "00:00") if default != "00:00" else (0, 0)


def _normalize_execution_snapshot(snapshot):
    if not isinstance(snapshot, dict):
        return {}
    return {
        "targets_summary": _clean_str(snapshot.get("targets_summary")),
        "content_summary": _clean_str(snapshot.get("content_summary")),
        "media_summary": _clean_str(snapshot.get("media_summary")),
        "material_summary": _clean_str(snapshot.get("material_summary")),
        "batch_summary": _clean_str(snapshot.get("batch_summary")),
        "result_summary": _clean_str(snapshot.get("result_summary")),
        "raw_targets": deepcopy(snapshot.get("raw_targets")) if isinstance(snapshot.get("raw_targets"), list) else [],
        "raw_messages": deepcopy(snapshot.get("raw_messages")) if isinstance(snapshot.get("raw_messages"), list) else [],
        "raw_media": deepcopy(snapshot.get("raw_media")) if isinstance(snapshot.get("raw_media"), list) else [],
        "raw_material": deepcopy(snapshot.get("raw_material")) if isinstance(snapshot.get("raw_material"), dict) else {},
        "batch_id": _clean_str(snapshot.get("batch_id")),
        "run_id": _clean_str(snapshot.get("run_id")),
    }


def _merge_execution_snapshot(record, snapshot, *, default_result_summary=""):
    merged = dict(record or {})
    normalized_snapshot = _normalize_execution_snapshot(snapshot)
    if normalized_snapshot:
        if default_result_summary and not normalized_snapshot.get("result_summary"):
            normalized_snapshot["result_summary"] = _clean_str(default_result_summary)
        merged.update(normalized_snapshot)
    return merged


def _date_matches_rule(task, day):
    repeat_rule = _clean_str(task.get("repeat_rule")) or "daily"
    repeat_values = list(task.get("repeat_values") or [])
    if repeat_rule == "daily":
        return True
    if repeat_rule == "weekly":
        return day.isoweekday() in {int(value) for value in repeat_values}
    if repeat_rule == "monthly":
        return day.day in {int(value) for value in repeat_values}
    if repeat_rule == "custom_dates":
        return day.strftime("%Y-%m-%d") in {str(value).strip() for value in repeat_values}
    return True


def _random_datetime_for_task(task, now, *, choice=None, randint=None):
    choice = choice or random.choice
    randint = randint or random.randint
    candidates = []
    for offset in range(0, 400):
        day = now.date() + timedelta(days=offset)
        if not _date_matches_rule(task, day):
            continue
        start_hour, start_minute = _parse_hhmm(task.get("time_window_start"), "09:00")
        end_hour, end_minute = _parse_hhmm(task.get("time_window_end"), "21:00")
        start_minutes = start_hour * 60 + start_minute
        end_minutes = end_hour * 60 + end_minute
        if end_minutes < start_minutes:
            end_minutes = start_minutes
        if day == now.date():
            current_minutes = now.hour * 60 + now.minute
            if current_minutes > end_minutes:
                continue
            start_minutes = max(start_minutes, current_minutes)
        candidates.append((day, start_minutes, end_minutes))
        if len(candidates) >= 35:
            break
    if not candidates:
        return None
    day, start_minutes, end_minutes = choice(candidates)
    picked_minutes = randint(start_minutes, end_minutes)
    hour, minute = divmod(picked_minutes, 60)
    second = randint(0, 59)
    return datetime(day.year, day.month, day.day, hour, minute, second)


def _normalize_last_result(last_result):
    if not isinstance(last_result, dict):
        return {}
    return _merge_execution_snapshot(
        {
        "result_type": _clean_str(last_result.get("result_type")),
        "success_count": int(last_result.get("success_count") or 0),
        "failed_count": int(last_result.get("failed_count") or 0),
        "skipped_count": int(last_result.get("skipped_count") or 0),
        "queued_count": int(last_result.get("queued_count") or 0),
        "finished_at": _clean_str(last_result.get("finished_at")),
        "summary": _clean_str(last_result.get("summary")),
        },
        last_result,
    )


def _normalize_history(history):
    normalized = []
    for item in history or []:
        if not isinstance(item, dict):
            continue
        normalized.append(
            _merge_execution_snapshot(
                {
                "run_id": _clean_str(item.get("run_id")),
                "result_type": _clean_str(item.get("result_type")),
                "success_count": int(item.get("success_count") or 0),
                "failed_count": int(item.get("failed_count") or 0),
                "skipped_count": int(item.get("skipped_count") or 0),
                "queued_count": int(item.get("queued_count") or 0),
                "started_at": _clean_str(item.get("started_at")),
                "finished_at": _clean_str(item.get("finished_at")),
                "summary": _clean_str(item.get("summary")),
                },
                item,
            )
        )
    return normalized


def build_scheduled_message_task(raw):
    raw = raw if isinstance(raw, dict) else {}
    schedule_mode = _clean_str(raw.get("schedule_mode")) or "fixed_at"
    trigger_kind = "random" if schedule_mode == "random_in_date_window" else "fixed"
    targets_mode = _clean_str(raw.get("targets_mode")) or "all"
    repeat_rule = _clean_str(raw.get("repeat_rule")) or "custom_dates"
    repeat_mode = _clean_str(raw.get("repeat_mode"))
    if not repeat_mode:
        repeat_mode = "repeat" if repeat_rule in {"daily", "weekly", "monthly"} else "once"

    task = {
        "id": _clean_str(raw.get("id")),
        "name": _clean_str(raw.get("name")) or "未命名任务",
        "enabled": bool(raw.get("enabled", True)),
        "status": _clean_str(raw.get("status")) or STATUS_PENDING_CONFIRM,
        "trigger_kind": trigger_kind,
        "schedule_mode": schedule_mode,
        "repeat_mode": repeat_mode,
        "repeat_rule": repeat_rule,
        "repeat_values": [value for value in (raw.get("repeat_values") or [])],
        "time_value": _clean_str(raw.get("time_value")),
        "time_window_start": _clean_str(raw.get("time_window_start")),
        "time_window_end": _clean_str(raw.get("time_window_end")),
        "start_at": _clean_str(raw.get("start_at")),
        "next_run_at": _clean_str(raw.get("next_run_at") or raw.get("next_fire_at")),
        "execute_after": _clean_str(raw.get("execute_after")),
        "targets_mode": targets_mode,
        "target_tags": _clean_list(raw.get("target_tags")),
        "exclude_target_tags": _clean_list(raw.get("exclude_target_tags")),
        "manual_target_names": _clean_list(raw.get("manual_target_names")),
        "targets": _clean_list(raw.get("targets")),
        "msgs": _clean_list(raw.get("msgs")),
        "current_run_id": _clean_str(raw.get("current_run_id")),
        "run_started_at": _clean_str(raw.get("run_started_at")),
        "pending_snapshot": _normalize_execution_snapshot(raw.get("pending_snapshot")),
        "last_result": _normalize_last_result(raw.get("last_result")),
        "return_reason": _clean_str(raw.get("return_reason")),
        "run_history": _normalize_history(raw.get("run_history")),
        "stop_requested": bool(raw.get("stop_requested", False)),
    }
    return task


def split_scheduled_message_task_storage(task):
    normalized = build_scheduled_message_task(normalize_scheduled_message_task_payload(task))
    definition = {field: normalized.get(field) for field in SCHEDULED_MESSAGE_TASK_DEFINITION_FIELDS}
    runtime = {}
    for field in SCHEDULED_MESSAGE_TASK_RUNTIME_FIELDS:
        value = normalized.get(field)
        if field in {"pending_snapshot", "last_result"}:
            if field == "pending_snapshot":
                runtime[field] = _normalize_execution_snapshot(value)
                continue
            runtime[field] = _normalize_last_result(value)
            continue
        runtime[field] = value
    history = _normalize_history(normalized.get("run_history"))
    return definition, runtime, history


def merge_scheduled_message_task_storage(definition, runtime=None, history=None):
    payload = {}
    if isinstance(definition, dict):
        payload.update(definition)
    if isinstance(runtime, dict):
        payload.update(runtime)
    payload["run_history"] = _normalize_history(history)
    return build_scheduled_message_task(normalize_scheduled_message_task_payload(payload))


def serialize_scheduled_message_task_collection(tasks):
    definitions = []
    runtime_map = {}
    history_map = {}
    for task in tasks or []:
        if not isinstance(task, dict):
            continue
        definition, runtime, history = split_scheduled_message_task_storage(task)
        task_id = _clean_str(definition.get("id"))
        if not task_id:
            continue
        definitions.append(definition)
        runtime_map[task_id] = runtime
        history_map[task_id] = history
    return definitions, runtime_map, history_map


def deserialize_scheduled_message_task_collection(definitions, runtime_map=None, history_map=None):
    runtime_map = runtime_map if isinstance(runtime_map, dict) else {}
    history_map = history_map if isinstance(history_map, dict) else {}
    tasks = []
    for definition in definitions or []:
        if not isinstance(definition, dict):
            continue
        task_id = _clean_str(definition.get("id"))
        tasks.append(
            merge_scheduled_message_task_storage(
                definition,
                runtime_map.get(task_id, {}),
                history_map.get(task_id, []),
            )
        )
    return tasks


def queue_scheduled_message_task(task, *, next_run_at):
    queued = build_scheduled_message_task(task)
    queued["status"] = STATUS_PENDING
    queued["next_run_at"] = _clean_str(next_run_at)
    queued["current_run_id"] = ""
    queued["run_started_at"] = ""
    queued["pending_snapshot"] = {}
    queued["return_reason"] = ""
    queued["stop_requested"] = False
    return queued


def mark_scheduled_message_running(task, *, run_id, started_at):
    running = build_scheduled_message_task(task)
    running["status"] = STATUS_RUNNING
    running["current_run_id"] = _clean_str(run_id)
    running["run_started_at"] = _clean_str(started_at)
    return running


def _append_run_history(task, run_record):
    history = list(task.get("run_history") or [])
    history.insert(0, run_record)
    task["run_history"] = history[:20]


def finish_scheduled_message_run(
    task,
    *,
    result_type,
    success_count,
    failed_count,
    skipped_count,
    finished_at,
    recurring,
    next_run_at,
    target_count=None,
    message_count=None,
    attempted_count=None,
    execution_snapshot=None,
    queued_count=0,
):
    finished = build_scheduled_message_task(task)
    success_count = int(success_count or 0)
    failed_count = int(failed_count or 0)
    skipped_count = int(skipped_count or 0)
    queued_count = int(queued_count or 0)
    total = success_count + failed_count + skipped_count + queued_count
    try:
        target_count = int(target_count)
    except (TypeError, ValueError):
        target_count = None
    try:
        attempted_count = int(attempted_count)
    except (TypeError, ValueError):
        attempted_count = total
    if target_count is None:
        summary = f"发送 {attempted_count} 条，成功 {success_count}，失败 {failed_count}，延后 {queued_count}"
    else:
        summary = f"目标 {target_count} 人，发送 {attempted_count} 条，成功 {success_count}，失败 {failed_count}，延后 {queued_count}"
    snapshot = _normalize_execution_snapshot(execution_snapshot or finished.get("pending_snapshot"))
    if finished.get("current_run_id") and not snapshot.get("run_id"):
        snapshot["run_id"] = _clean_str(finished.get("current_run_id"))
    last_result = _merge_execution_snapshot(
        {
        "result_type": _clean_str(result_type),
        "success_count": success_count,
        "failed_count": failed_count,
        "skipped_count": skipped_count,
        "queued_count": queued_count,
        "finished_at": _clean_str(finished_at),
        "summary": summary,
        },
        snapshot,
        default_result_summary=summary,
    )
    finished["last_result"] = last_result
    _append_run_history(
        finished,
        _merge_execution_snapshot(
            {
            "run_id": finished.get("current_run_id", ""),
            "result_type": last_result["result_type"],
            "success_count": success_count,
            "failed_count": failed_count,
            "skipped_count": skipped_count,
            "queued_count": queued_count,
            "started_at": finished.get("run_started_at", ""),
            "finished_at": last_result["finished_at"],
            "summary": summary,
            },
            snapshot,
            default_result_summary=summary,
        ),
    )
    finished["current_run_id"] = ""
    finished["run_started_at"] = ""
    finished["pending_snapshot"] = {}
    finished["return_reason"] = ""
    finished["stop_requested"] = False
    if recurring:
        finished["status"] = STATUS_PENDING
        finished["next_run_at"] = _clean_str(next_run_at)
    else:
        finished["status"] = STATUS_EXECUTED
        finished["enabled"] = False
        finished["next_run_at"] = ""
    return finished


def return_scheduled_message_task(task, *, reason, summary):
    returned = build_scheduled_message_task(task)
    snapshot = _normalize_execution_snapshot(returned.get("pending_snapshot"))
    returned["status"] = STATUS_PENDING_CONFIRM
    returned["return_reason"] = _clean_str(reason)
    returned["last_result"] = _merge_execution_snapshot(
        {
            "result_type": _clean_str(reason),
            "success_count": 0,
            "failed_count": 0,
            "skipped_count": 0,
            "finished_at": "",
            "summary": _clean_str(summary),
        },
        snapshot,
        default_result_summary=summary,
    )
    _append_run_history(
        returned,
        _merge_execution_snapshot(
            {
                "run_id": returned.get("current_run_id", ""),
                "result_type": _clean_str(reason),
                "success_count": 0,
                "failed_count": 0,
                "skipped_count": 0,
                "started_at": returned.get("run_started_at", ""),
                "finished_at": "",
                "summary": _clean_str(summary),
            },
            snapshot,
            default_result_summary=summary,
        ),
    )
    returned["current_run_id"] = ""
    returned["run_started_at"] = ""
    returned["next_run_at"] = ""
    returned["pending_snapshot"] = {}
    returned["stop_requested"] = False
    return returned


def normalize_scheduled_message_task_payload(raw):
    task = build_scheduled_message_task(raw)
    if _clean_str(raw.get("schedule_mode")) == "":
        trigger_kind = _clean_str(raw.get("trigger_kind")) or task.get("trigger_kind", "fixed")
        task["schedule_mode"] = "random_in_date_window" if trigger_kind == "random" else "fixed_at"
        task["trigger_kind"] = trigger_kind
    if task["trigger_kind"] == "random":
        normalized = normalize_random_task_schedule(task, default_start="09:00", default_end="21:00")
    else:
        normalized = normalize_fixed_task_schedule(task, default_time="08:00")
    normalized_task = build_scheduled_message_task({**task, **normalized})
    normalized_task["schedule_mode"] = normalized["schedule_mode"]
    normalized_task["repeat_mode"] = normalized["repeat_mode"]
    normalized_task["repeat_rule"] = normalized["repeat_rule"]
    normalized_task["repeat_values"] = list(normalized.get("repeat_values") or [])
    normalized_task["time_value"] = normalized.get("time_value", "")
    normalized_task["time_window_start"] = normalized.get("time_window_start", "")
    normalized_task["time_window_end"] = normalized.get("time_window_end", "")
    normalized_task["fire_at"] = normalized.get("fire_at", "")
    normalized_task["start_at"] = normalized.get("start_at", "")
    normalized_task["trigger_kind"] = "random" if normalized["schedule_mode"] == "random_in_date_window" else "fixed"
    return normalized_task


def ensure_scheduled_message_next_run(task, *, now=None, choice=None, randint=None):
    normalized = normalize_scheduled_message_task_payload(task)
    if normalized.get("next_run_at"):
        parsed = _parse_datetime(normalized.get("next_run_at"))
        normalized["next_run_at"] = _iso_datetime(parsed) if parsed else ""
        return normalized
    if not normalized.get("enabled", True):
        return normalized
    if normalized.get("status") not in {STATUS_PENDING, STATUS_RUNNING}:
        return normalized

    now = now or datetime.now()
    if normalized.get("schedule_mode") == "random_in_date_window":
        next_run = _random_datetime_for_task(normalized, now, choice=choice, randint=randint)
        normalized["next_run_at"] = _iso_datetime(next_run) if next_run else ""
        return normalized

    plan = compile_task_plan(
        {
            **normalized,
            "status": "pending",
            "next_fire_at": normalized.get("next_run_at", ""),
        },
        now=now,
        choice=choice,
        randint=randint,
    )
    normalized["next_run_at"] = _clean_str(plan.get("next_fire_at"))
    return normalized


def is_scheduled_message_task_due(task, *, now=None):
    task = task if isinstance(task, dict) else {}
    if not task.get("enabled", True):
        return False
    if _clean_str(task.get("status")) not in {STATUS_PENDING, STATUS_RUNNING}:
        return False
    fire_at = _parse_datetime(task.get("next_run_at"))
    return bool(fire_at and fire_at <= (now or datetime.now()))


def _message_result_summary(result):
    result = result if isinstance(result, dict) else {}
    success_count = int(result.get("success_count") or 0)
    failed_count = int(result.get("failed_count") or 0)
    skipped_count = int(result.get("skipped_count") or 0)
    queued_count = int(result.get("queued_count") or 0)
    total = success_count + failed_count + skipped_count + queued_count
    try:
        target_count = int(result.get("target_count"))
    except (TypeError, ValueError):
        target_count = None
    try:
        attempted_count = int(result.get("attempted_count"))
    except (TypeError, ValueError):
        attempted_count = total
    if target_count is None:
        return f"发送 {attempted_count} 条，成功 {success_count}，失败 {failed_count}，延后 {queued_count}"
    return f"目标 {target_count} 人，发送 {attempted_count} 条，成功 {success_count}，失败 {failed_count}，延后 {queued_count}"


def return_scheduled_message_run(task, result, *, finished_at):
    result = result if isinstance(result, dict) else {}
    returned = build_scheduled_message_task(task)
    success_count = int(result.get("success_count") or 0)
    failed_count = int(result.get("failed_count") or 0)
    skipped_count = int(result.get("skipped_count") or 0)
    queued_count = int(result.get("queued_count") or 0)
    result_type = _clean_str(result.get("result_type")) or "all_failed"
    summary = _message_result_summary(result)
    snapshot = _normalize_execution_snapshot(result.get("execution_snapshot") or returned.get("pending_snapshot"))
    if returned.get("current_run_id") and not snapshot.get("run_id"):
        snapshot["run_id"] = _clean_str(returned.get("current_run_id"))
    last_result = _merge_execution_snapshot(
        {
            "result_type": result_type,
            "success_count": success_count,
            "failed_count": failed_count,
            "skipped_count": skipped_count,
            "queued_count": queued_count,
            "finished_at": _clean_str(finished_at),
            "summary": summary,
        },
        snapshot,
        default_result_summary=summary,
    )
    returned["status"] = STATUS_PENDING_CONFIRM
    returned["enabled"] = True
    returned["return_reason"] = result_type
    returned["last_result"] = last_result
    _append_run_history(
        returned,
        _merge_execution_snapshot(
            {
                "run_id": returned.get("current_run_id", ""),
                "result_type": result_type,
                "success_count": success_count,
                "failed_count": failed_count,
                "skipped_count": skipped_count,
                "queued_count": queued_count,
                "started_at": returned.get("run_started_at", ""),
                "finished_at": last_result["finished_at"],
                "summary": summary,
            },
            snapshot,
            default_result_summary=summary,
        ),
    )
    returned["current_run_id"] = ""
    returned["run_started_at"] = ""
    returned["next_run_at"] = ""
    returned["pending_snapshot"] = {}
    returned["stop_requested"] = False
    return returned


def apply_scheduled_message_run_result(task, result, *, now=None, choice=None, randint=None, execution_snapshot=None):
    now = now or datetime.now()
    result = result if isinstance(result, dict) else {"result_type": "all_failed"}
    if execution_snapshot is not None:
        result = {**result, "execution_snapshot": execution_snapshot}
    result_type = _clean_str(result.get("result_type")) or "all_failed"
    finished_at = _iso_datetime(now)
    if result_type in {"all_failed", "manual_stop"}:
        return return_scheduled_message_run(task, result, finished_at=finished_at)

    task = build_scheduled_message_task(task)
    recurring = task.get("repeat_mode") == "repeat"
    next_run_at = ""
    if recurring:
        plan = advance_task_plan_after_success(
            {
                **task,
                "status": "pending",
                "next_fire_at": task.get("next_run_at", ""),
            },
            now=now,
            choice=choice,
            randint=randint,
        )
        next_run_at = _clean_str(plan.get("next_fire_at"))
        recurring = bool(next_run_at)
    return finish_scheduled_message_run(
        task,
        result_type=result_type,
        success_count=int(result.get("success_count") or 0),
        failed_count=int(result.get("failed_count") or 0),
        skipped_count=int(result.get("skipped_count") or 0),
        queued_count=int(result.get("queued_count") or 0),
        finished_at=finished_at,
        recurring=recurring,
        next_run_at=next_run_at,
        target_count=result.get("target_count"),
        message_count=result.get("message_count"),
        attempted_count=result.get("attempted_count"),
        execution_snapshot=result.get("execution_snapshot"),
    )


def build_scheduled_message_task_view(task):
    task = normalize_scheduled_message_task_payload(task)
    return build_scheduled_message_task(task)
