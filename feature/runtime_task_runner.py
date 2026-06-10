"""Unified runtime task scheduling helpers for WXBot."""

from __future__ import annotations

import uuid
from datetime import datetime

from core import runtime_chat_state
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
from feature.moments_like import execute_moments_like_task
from feature.moments_tasks import (
    STATUS_EXECUTED,
    STATUS_PENDING,
    STATUS_PENDING_CONFIRM,
    STATUS_RUNNING,
    moments_task_has_ai_candidates,
    mark_moments_task_running,
    moments_task_publish_text,
    split_moments_task_storage,
)
from feature.scheduled_message_tasks import (
    apply_scheduled_message_run_result,
    ensure_scheduled_message_next_run,
    is_scheduled_message_task_due,
    mark_scheduled_message_running,
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


def _moments_tag_summary(tags):
    tags = [str(tag or "").strip() for tag in (tags or []) if str(tag or "").strip()]
    if not tags:
        return ""
    if len(tags) == 1:
        return tags[0]
    if len(tags) == 2:
        return "、".join(tags)
    return f"{tags[0]}、{tags[1]} 等 {len(tags)} 个标签"


def _moments_visibility_summary(task):
    task = task if isinstance(task, dict) else {}
    visibility_type = str(task.get("visibility_type") or "all").strip()
    tags_summary = _moments_tag_summary(task.get("tags"))
    if visibility_type == "include":
        return f"仅 {tags_summary} 可见" if tags_summary else "部分好友可见"
    if visibility_type == "exclude":
        return f"不给 {tags_summary} 看" if tags_summary else "排除部分好友"
    return "全部好友可见"


def _moments_batch_summary(text, images):
    has_text = bool(str(text or "").strip())
    image_count = len(images or [])
    if has_text and image_count:
        return f"文案 1 条，图片 {image_count} 张"
    if has_text:
        return "纯文字朋友圈"
    if image_count:
        return f"图片 {image_count} 张"
    return "空内容"


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
        targets = bot._resolve_scheduled_message_task_targets(raw_task)
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
        running["pending_snapshot"] = execution_snapshot
        raw_task.clear()
        raw_task.update(running)
        changed = True
        bot._save_scheduled_message_runtime_record(raw_task)
        result = execute_scheduled_message_task(
            task={**raw_task, "targets": targets},
            send_text=lambda target, msg: runtime_chat_state.send_text_to_target(bot, target, msg),
            send_file=lambda target, path: runtime_chat_state.send_file_to_target(bot, target, path),
            is_image_path=bot.is_image_path,
            human_delay=bot.config.human_delay,
            notify_error=bot.is_err,
            nickname=bot.wx.nickname,
            scheduled_tasks=[],
            config_data={},
            save_config=None,
            log_info=lambda message: log(message=message),
            log_error=lambda message: log(level="ERROR", message=message),
        )
        record_scheduled_sends = getattr(bot, "_record_scheduled_message_send_successes", None)
        if callable(record_scheduled_sends):
            record_scheduled_sends((result or {}).get("success_count", 0))
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
            log_error=lambda message: log(level="ERROR", message=message),
        )
        if bot._material_outreach_result_failed(executed):
            if bot._resolve_material_outreach_direct_failure(task.get("task_id"), executed, now=now):
                changed = True
            continue
        if bot._material_outreach_preface_is_queued(executed):
            continue
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
            log_error=lambda message: log(level="ERROR", message=message),
        )
        if bot._material_outreach_result_failed(executed):
            if bot._resolve_material_outreach_direct_failure(task.get("task_id"), executed, now=now):
                changed = True
            continue
        if bot._material_outreach_preface_is_queued(executed):
            continue
        state["last_fire_date"] = now.date()
        state["next_fire"] = None
        if bot._sync_random_runtime_state(raw_task, state):
            changed = True
    if changed:
        next_tasks = [task for task in getattr(bot.config, "material_outreach_list", []) or [] if isinstance(task, dict)]
        bot._set_runtime_task_list("material_outreach_list", next_tasks)
        bot._save_material_outreach_task_definitions_only(next_tasks)


def run_due_moments_task_list(bot, now=None):
    now = now or datetime.now()
    tasks = getattr(bot.config, "moments_task_list", []) or []
    changed = False
    definitions_dirty = False
    for task in tasks:
        if not isinstance(task, dict):
            continue
        if not task.get("enabled", True) or task.get("status") != STATUS_PENDING:
            continue
        execute_after = str(task.get("execute_after") or "").strip()
        if execute_after:
            try:
                if now < datetime.fromisoformat(execute_after):
                    continue
            except ValueError:
                pass
        text = moments_task_publish_text(task)
        resolved_images = bot._resolve_panel_moments_images(task.get("images", []))
        run_id = str(uuid.uuid4())
        base_snapshot = runtime_snapshot(
            raw_targets=[
                {
                    "visibility_type": str(task.get("visibility_type") or "all").strip(),
                    "tags": list(task.get("tags") or []),
                }
            ],
            raw_messages=[text] if text else [],
            raw_media=[{"path": image, "type": "image", "name": image.replace("\\", "/").rsplit("/", 1)[-1]} for image in resolved_images],
            targets_summary=_moments_visibility_summary(task),
            batch_summary=_moments_batch_summary(text, resolved_images),
            batch_id=run_id,
            run_id=run_id,
        )
        if not moments_task_has_ai_candidates(task):
            before_definition, _runtime, _history = split_moments_task_storage(task)
            task["status"] = STATUS_PENDING_CONFIRM
            task["enabled"] = True
            task["execute_after"] = ""
            task["queued_at"] = ""
            task["queued_mode"] = ""
            task["executed_at"] = now.replace(microsecond=0).isoformat()
            task["execution_result"] = "failed"
            task["execution_message"] = "AI文案模式缺少候选，任务已回到待确认"
            task["execution_snapshot"] = {
                **base_snapshot,
                "result_summary": task["execution_message"],
            }
            after_definition, _runtime, _history = split_moments_task_storage(task)
            if after_definition != before_definition:
                definitions_dirty = True
            bot._save_moments_runtime_history_records(task)
            changed = True
            continue
        before_definition, _runtime, _history = split_moments_task_storage(task)
        running_task = mark_moments_task_running(task, now=now)
        task.clear()
        task.update(running_task)
        after_definition, _runtime, _history = split_moments_task_storage(task)
        if after_definition != before_definition:
            definitions_dirty = True
        bot._save_moments_runtime_record(task)
        changed = True
        published = bot._execute_moments_publish_task(
            {
                "id": str(task.get("id") or "").strip(),
                "text": text,
                "images": resolved_images,
                "privacy": bot._panel_moments_privacy(task.get("visibility_type")),
                "tags": list(task.get("tags") or []),
            }
        )
        if published:
            record_moments_published = getattr(bot, "_record_moments_published", None)
            if callable(record_moments_published):
                record_moments_published()
        before_definition, _runtime, _history = split_moments_task_storage(task)
        task["status"] = STATUS_EXECUTED if published else STATUS_PENDING_CONFIRM
        task["enabled"] = False if published else True
        task["execute_after"] = ""
        task["queued_at"] = ""
        task["queued_mode"] = ""
        task["executed_at"] = now.replace(microsecond=0).isoformat()
        task["execution_result"] = "success" if published else "failed"
        task["execution_message"] = "朋友圈已执行" if published else "朋友圈发布失败，任务已回到待确认"
        task["execution_snapshot"] = {
            **base_snapshot,
            "result_summary": task["execution_message"],
        }
        after_definition, _runtime, _history = split_moments_task_storage(task)
        if after_definition != before_definition:
            definitions_dirty = True
        bot._save_moments_runtime_history_records(task)
        changed = True
    if changed:
        bot._set_runtime_task_list("moments_task_list", tasks)
        if definitions_dirty:
            bot._save_moments_task_definitions_only(tasks)


def run_due_moments_like_task(bot, now=None):
    now = now or datetime.now()
    if not getattr(bot.config, "moments_like_switch", False):
        bot._moments_like_next_time = None
        bot._moments_like_runtime_task = {}
        return
    bot._moments_like_runtime_task = compile_task_plan(
        {
            "id": "moments-like",
            "schedule_mode": "interval_next",
            "repeat_mode": "repeat",
            "interval_min": int(getattr(bot.config, "moments_like_min", 1) or 1),
            "interval_max": int(getattr(bot.config, "moments_like_max", 1) or 1),
            "next_fire_at": str(bot._moments_like_runtime_task.get("next_fire_at") or "").strip(),
            "status": str(bot._moments_like_runtime_task.get("status") or "pending").strip() or "pending",
            "last_run_at": str(bot._moments_like_runtime_task.get("last_run_at") or "").strip(),
            "last_error": str(bot._moments_like_runtime_task.get("last_error") or "").strip(),
        },
        now=now,
    )
    next_fire_at = str(bot._moments_like_runtime_task.get("next_fire_at") or "").strip()
    if next_fire_at and next_fire_at != str(bot._moments_like_next_time or ""):
        bot._moments_like_next_time = next_fire_at
        try:
            readable = datetime.fromisoformat(next_fire_at).strftime("%H:%M:%S")
            log(message=f"随机朋友圈点赞：下次触发 {readable}")
        except ValueError:
            pass
    if not is_task_due(bot._moments_like_runtime_task, now=now):
        return
    execute_moments_like_task(
        task=bot._moments_like_runtime_task,
        perform_like=bot._do_moments_like,
        log_info=lambda message: log(message=message),
        log_error=lambda message: log(level="ERROR", message=message),
    )
    bot._moments_like_runtime_task = advance_task_plan_after_success(bot._moments_like_runtime_task, now=now)
    bot._moments_like_next_time = str(bot._moments_like_runtime_task.get("next_fire_at") or "").strip() or None


def process_unified_runtime_tasks(bot, now=None):
    now = now or datetime.now()
    bot._run_due_scheduled_message_tasks(now=now)
    bot._run_due_fixed_material_outreach(now=now)
    bot._run_due_moments_task_list(now=now)
    bot._run_due_random_material_outreach(now=now)
    bot._run_due_moments_like_task(now=now)


def process_pending_runtime_task_reload(bot):
    if not bot._consume_runtime_task_reload_request():
        return {"reloaded": False, "success": True}
    try:
        log(message="检测到运行中任务配置更新，开始同步任务调度")
        bot.config.refresh_config()
        bot._reset_runtime_task_states()
        bot._register_runtime_task_schedules()
        log(message="运行中任务配置同步完成，后续任务将按新配置执行")
        return {"reloaded": True, "success": True}
    except Exception as e:
        log(level="ERROR", message=f"运行中任务配置同步失败：{e}")
        return {"reloaded": True, "success": False, "message": str(e)}
