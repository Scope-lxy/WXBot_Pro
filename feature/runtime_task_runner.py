"""Unified runtime task scheduling helpers for WXBot."""

from __future__ import annotations

import uuid
from datetime import datetime

from core import wechat_ui_actions
from core.logger import log
from core.scheduled_tasks import (
    advance_task_plan_after_success,
    compile_task_plan,
    is_task_due,
    iter_enabled_tasks,
)
from feature.material_outreach import (
    execute_material_outreach_task,
    iter_enabled_material_outreach_tasks,
    material_random_time_window,
    plan_random_material_outreach_fire_time,
    prepare_random_material_outreach_day,
)
from feature.scheduled_message_tasks import (
    apply_scheduled_message_run_result,
    ensure_scheduled_message_next_run,
    is_scheduled_message_task_due,
    mark_scheduled_message_running,
    recover_interrupted_scheduled_message_task,
    split_scheduled_message_task_storage,
)
from feature.scheduled_messages import execute_scheduled_message_task
from feature.task_display_titles import is_likely_local_file_path
from feature.task_workbench_runtime_summary import runtime_snapshot


def _scheduled_message_batch_summary(targets, messages):
    target_count = len(targets or [])
    message_count = len(messages or [])
    return f"目标 {target_count} 人，消息 {message_count} 条"


def _scheduled_message_result_summary(result):
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


def _task_log_name(task, default="未命名任务"):
    task = task if isinstance(task, dict) else {}
    return str(
        task.get("display_title")
        or task.get("task_name")
        or task.get("name")
        or task.get("task_id")
        or task.get("id")
        or default
    ).strip() or default


def _log_scheduled_message_run_result(task, result):
    result = result if isinstance(result, dict) else {}
    success_count = int(result.get("success_count") or 0)
    failed_count = int(result.get("failed_count") or 0)
    skipped_count = int(result.get("skipped_count") or 0)
    queued_count = int(result.get("queued_count") or 0)
    result_type = str(result.get("result_type") or "").strip()
    if success_count > 0 and failed_count == 0 and skipped_count == 0 and queued_count == 0:
        level = "SUCCESS"
    elif failed_count == 0 and skipped_count == 0 and result_type == "queued" and queued_count > 0:
        level = "INFO"
    else:
        level = "WARNING"
    log(
        level=level,
        message=f"定时消息任务 {_task_log_name(task, '未命名定时消息')} 完成：{_scheduled_message_result_summary(result)}",
    )


def _log_material_outreach_run_result(task, success):
    log(
        level="SUCCESS" if success else "WARNING",
        message=f"素材转发任务 {_task_log_name(task, '未命名素材转发')} {'执行成功' if success else '执行失败'}",
    )


def _scheduled_message_runtime_parts(messages):
    raw_messages = []
    raw_media = []
    for item in messages or []:
        value = str(item or "").strip()
        if not value:
            continue
        if is_likely_local_file_path(value):
            raw_media.append({"type": "file", "path": value, "name": value.replace("\\", "/").rsplit("/", 1)[-1]})
        else:
            raw_messages.append({"type": "text", "text": value})
    return raw_messages, raw_media


def _guard_scheduled_message_send(send):
    try:
        return send()
    except wechat_ui_actions.UIOutboundNotStarted as exc:
        return {"status": "failed", "message": f"监听子窗口未准备好，消息未发送：{exc}"}
    except wechat_ui_actions.IntentCancelled as exc:
        return {"status": "cancelled", "message": str(exc)}


def run_due_scheduled_message_tasks(bot, now=None):
    now = now or datetime.now()
    tasks = getattr(bot.config, "scheduled_message_task_list", [])
    if not isinstance(tasks, list):
        tasks = []
    changed = False
    definitions_dirty = False

    for index, raw_task in enumerate(list(tasks)):
        if not isinstance(raw_task, dict):
            continue
        if str(raw_task.get("status") or "").strip() == "running":
            recovered = recover_interrupted_scheduled_message_task(raw_task)
            raw_task.clear()
            raw_task.update(recovered)
            changed = True
            bot._save_scheduled_message_runtime_history_records(raw_task)
            if index < len(tasks):
                tasks[index] = raw_task
            continue
        task = ensure_scheduled_message_next_run(raw_task, now=now)
        if task != raw_task:
            raw_task.clear()
            raw_task.update(task)
            changed = True
            bot._save_scheduled_message_runtime_record(raw_task)
        if not is_scheduled_message_task_due(raw_task, now=now):
            continue

        run_id = str(uuid.uuid4())
        running = mark_scheduled_message_running(
            raw_task,
            run_id=run_id,
            started_at=now.replace(microsecond=0).isoformat(),
        )
        resolve_target_records = getattr(bot, "_resolve_scheduled_message_task_target_records", None)
        targets = (
            resolve_target_records(raw_task)
            if callable(resolve_target_records)
            else bot._resolve_scheduled_message_task_targets(raw_task)
        )
        guard_task = getattr(bot, "_scheduled_message_ui_guard", None)
        task_key, task_version = guard_task(raw_task) if callable(guard_task) else ("", 0)

        def task_is_stale():
            return bool(task_version and bot._current_ui_task_version(task_key) != task_version)

        messages = [
            str(message or "").strip()
            for message in (raw_task.get("msgs") or [])
            if str(message or "").strip()
        ]
        raw_messages, raw_media = _scheduled_message_runtime_parts(messages)
        execution_snapshot = runtime_snapshot(
            raw_targets=targets,
            raw_messages=raw_messages,
            raw_media=raw_media,
            batch_summary=_scheduled_message_batch_summary(targets, messages),
            batch_id=run_id,
            run_id=run_id,
        )
        execution_snapshot["delivery_records"] = [
            {
                "key": f"{target_index}:{message_index}",
                "target": target,
                "message_index": message_index,
                "status": "pending",
                "error": "",
            }
            for target_index, target in enumerate(targets)
            for message_index, _message in enumerate(messages)
        ]
        running["pending_snapshot"] = execution_snapshot
        raw_task.clear()
        raw_task.update(running)
        changed = True
        bot._save_scheduled_message_runtime_record(raw_task)

        def persist_delivery_state(_record):
            raw_task["pending_snapshot"] = execution_snapshot
            bot._save_scheduled_message_runtime_record(raw_task)

        result = execute_scheduled_message_task(
            task={**raw_task, "targets": targets},
            send_text=lambda target, msg: _guard_scheduled_message_send(lambda: bot._send_outbound_to_target(
                    str((target or {}).get("send_name") or "") if isinstance(target, dict) else target,
                    {"type": "text", "text": msg},
                    contact_key=str((target or {}).get("contact_key") or "") if isinstance(target, dict) else "",
                    task_key=task_key,
                    task_version=task_version,
                    require_contact_key=bool((target or {}).get("require_contact_key")) if isinstance(target, dict) else False,
                )),
            send_file=lambda target, path: _guard_scheduled_message_send(lambda: bot._send_outbound_to_target(
                    str((target or {}).get("send_name") or "") if isinstance(target, dict) else target,
                    {"type": "file", "path": path},
                    contact_key=str((target or {}).get("contact_key") or "") if isinstance(target, dict) else "",
                    task_key=task_key,
                    task_version=task_version,
                    require_contact_key=bool((target or {}).get("require_contact_key")) if isinstance(target, dict) else False,
                )),
            is_image_path=bot.is_image_path,
            human_delay=bot._inter_message_delay_or_stop,
            should_stop=lambda: bool(bot.is_stop_requested() or task_is_stale()),
            notify_error=bot.is_err,
            nickname=bot.wx.nickname,
            scheduled_tasks=[],
            config_data={},
            save_config=None,
            log_info=lambda message: log(message=message),
            log_error=lambda message: log(level="WARNING", message=message),
            delivery_records=execution_snapshot["delivery_records"],
            on_delivery_state=persist_delivery_state,
        )
        _log_scheduled_message_run_result(raw_task, result)
        record_scheduled_sends = getattr(bot, "_record_scheduled_message_send_successes", None)
        if callable(record_scheduled_sends):
            record_scheduled_sends(
                (result or {}).get("success_count", 0),
                trigger_kind=raw_task.get("trigger_kind") or raw_task.get("schedule_mode"),
            )
        before_definition, _runtime, _history = split_scheduled_message_task_storage(raw_task)
        finished = apply_scheduled_message_run_result(
            raw_task,
            result,
            now=now,
            execution_snapshot={
                **execution_snapshot,
                "result_summary": _scheduled_message_result_summary(result),
            },
        )
        raw_task.clear()
        raw_task.update(finished)
        after_definition, _runtime, _history = split_scheduled_message_task_storage(raw_task)
        if after_definition != before_definition:
            definitions_dirty = True
        bot._save_scheduled_message_runtime_history_records(raw_task)
        if index < len(tasks):
            tasks[index] = raw_task

    if changed:
        bot._set_runtime_task_list("scheduled_message_task_list", tasks)
        if definitions_dirty:
            bot._save_scheduled_message_task_definitions_only(tasks)


def run_due_fixed_material_outreach(bot, now=None):
    now = now or datetime.now()
    changed = False
    for raw_task in iter_enabled_tasks(getattr(bot.config, "material_outreach_list", [])):
        task = next(iter_enabled_material_outreach_tasks([raw_task]), None)
        if not task or task.get("mode") != "fixed":
            continue
        plan = bot._compile_fixed_runtime_plan(raw_task, now=now)
        if bot._sync_runtime_plan_fields(raw_task, plan):
            changed = True
        task = dict(task)
        guard_task = getattr(bot, "_material_outreach_ui_guard", None)
        task["_ui_task_key"], task["_ui_task_version"] = guard_task(raw_task) if callable(guard_task) else ("", 0)
        scheduled_at = str(plan.get("next_fire_at") or "").strip()
        if bot._material_outreach_queue_time_due(task, scheduled_at, now=now):
            cycle_records = bot._material_outreach_preface_cycle_records(task.get("task_id"), scheduled_at=scheduled_at)
            if cycle_records:
                if bot._resolve_material_outreach_preface_cycle(
                    task.get("task_id"),
                    scheduled_at=scheduled_at,
                    now=now,
                ):
                    changed = True
                continue
            task["_preface_scheduled_at"] = scheduled_at
        elif not is_task_due(plan, now=now):
            continue
        executed = execute_material_outreach_task(
            task=task,
            send_material_outreach=bot.send_material_outreach,
            log_info=lambda message: log(message=message),
            log_error=lambda message: log(level="WARNING", message=message),
        )
        if bot._material_outreach_result_failed(executed):
            _log_material_outreach_run_result(task, False)
            if bot._resolve_material_outreach_direct_failure(task.get("task_id"), executed, now=now):
                changed = True
            continue
        if bot._material_outreach_is_deferred(executed):
            continue
        if bot._material_outreach_preface_is_queued(executed):
            continue
        _log_material_outreach_run_result(task, True)
        if plan.get("repeat_mode") == "once":
            bot._disable_once_material_outreach_task(task.get("task_id"))
        plan = advance_task_plan_after_success(plan, now=now)
        if bot._sync_runtime_plan_fields(raw_task, plan):
            changed = True
    if changed:
        next_tasks = [task for task in getattr(bot.config, "material_outreach_list", []) or [] if isinstance(task, dict)]
        bot._set_runtime_task_list("material_outreach_list", next_tasks)
        bot._save_material_outreach_task_definitions_only(next_tasks)


def run_due_random_material_outreach(bot, now=None):
    now = now or datetime.now()
    changed = False
    for raw_task in iter_enabled_tasks(getattr(bot.config, "material_outreach_list", [])):
        task = next(iter_enabled_material_outreach_tasks([raw_task]), None)
        if not task or task.get("mode") != "random":
            continue
        task_id = str(task.get("task_id") or "").strip()
        if not task_id:
            continue
        state = bot._load_random_runtime_state(raw_task)
        if not prepare_random_material_outreach_day(
            task_id,
            task,
            state,
            now.date(),
            log_info=lambda message: log(message=message),
        ):
            if bot._sync_random_runtime_state(raw_task, state):
                changed = True
            continue
        task = dict(task)
        guard_task = getattr(bot, "_material_outreach_ui_guard", None)
        task["_ui_task_key"], task["_ui_task_version"] = guard_task(raw_task) if callable(guard_task) else ("", 0)
        task["time_start"], task["time_end"] = material_random_time_window(
            bot.config.config.get("everyday_start_stop_bot_switch", False),
            bot.config.config.get("everyday_start_bot_time", "08:00"),
            bot.config.config.get("everyday_stop_bot_time", "23:00"),
            now=now,
        )
        if not task["time_start"] or not task["time_end"]:
            state["next_fire"] = None
            if bot._sync_random_runtime_state(raw_task, state):
                changed = True
            continue
        if state.get("next_fire") is None:
            plan_random_material_outreach_fire_time(
                task_id,
                task,
                state,
                now,
                log_info=lambda message: log(message=message),
            )
        if bot._sync_random_runtime_state(raw_task, state):
            changed = True
        scheduled_at = str(raw_task.get("next_fire_at") or "").strip()
        if bot._material_outreach_queue_time_due(task, scheduled_at, now=now):
            cycle_records = bot._material_outreach_preface_cycle_records(task.get("task_id"), scheduled_at=scheduled_at)
            if cycle_records:
                if bot._resolve_material_outreach_preface_cycle(
                    task.get("task_id"),
                    scheduled_at=scheduled_at,
                    now=now,
                ):
                    changed = True
                continue
            task["_preface_scheduled_at"] = scheduled_at
        elif not is_task_due(raw_task, now=now):
            continue
        executed = execute_material_outreach_task(
            task=task,
            send_material_outreach=bot.send_material_outreach,
            log_info=lambda message: log(message=message),
            log_error=lambda message: log(level="WARNING", message=message),
        )
        if bot._material_outreach_result_failed(executed):
            _log_material_outreach_run_result(task, False)
            if bot._resolve_material_outreach_direct_failure(task.get("task_id"), executed, now=now):
                changed = True
            continue
        if bot._material_outreach_is_deferred(executed):
            continue
        if bot._material_outreach_preface_is_queued(executed):
            continue
        _log_material_outreach_run_result(task, True)
        state["last_fire_date"] = now.date()
        state["next_fire"] = None
        if bot._sync_random_runtime_state(raw_task, state):
            changed = True
    if changed:
        next_tasks = [task for task in getattr(bot.config, "material_outreach_list", []) or [] if isinstance(task, dict)]
        bot._set_runtime_task_list("material_outreach_list", next_tasks)
        bot._save_material_outreach_task_definitions_only(next_tasks)


def process_unified_runtime_tasks(bot, now=None):
    now = now or datetime.now()
    bot._run_due_scheduled_message_tasks(now=now)
    bot._run_due_fixed_material_outreach(now=now)
    bot._run_due_random_material_outreach(now=now)


def process_pending_runtime_task_reload(bot):
    if not bot._consume_runtime_task_reload_request():
        return {"reloaded": False, "success": True}
    try:
        log(message="检测到运行中任务配置更新，开始同步任务调度")
        bot.config.refresh_config()
        bot._reset_runtime_task_states()
        bot._register_runtime_task_schedules()
        log(level="INFO", message="运行中任务配置同步完成，后续任务将按新配置执行")
        return {"reloaded": True, "success": True}
    except Exception as e:
        log(level="ERROR", message=f"运行中任务配置同步失败：{e}")
        return {"reloaded": True, "success": False, "message": str(e)}
