"""Scheduled message business rules."""

import os

from core.scheduled_tasks import (
    iter_enabled_tasks,
    normalize_fixed_task_schedule,
    plan_random_fire_time,
    prepare_random_task_day,
    repeat_rule_to_type,
    should_run_repeating_task,
)

SCHEDULED_MESSAGE_TARGET_BATCH_SIZE = 9


def _looks_like_local_file_path(value):
    text = str(value or "").strip().strip('"').strip("'")
    if not text or text.startswith(("http://", "https://")):
        return False
    if os.path.isabs(text):
        return True
    return len(text) >= 3 and text[1:3] in (":\\", ":/")


def _send_result_status(result):
    if result is True:
        return "success"
    if result is False or result is None:
        return "failed"
    if isinstance(result, dict):
        status = str(result.get("status") or "").strip().lower()
        if status in {"success", "ok", "true", "成功"}:
            return "success"
        if status in {"queued", "pending", "deferred", "延后", "待发送"}:
            return "queued"
        if status in {"error", "fail", "failed", "false", "失败", "错误"}:
            return "failed"
        if result.get("code") == 0 or result.get("success") is True:
            return "success"
        if result.get("success") is False:
            return "failed"
    return "success" if result else "failed"


def iter_enabled_scheduled_message_tasks(tasks):
    """Yield normalized scheduled message tasks ready for schedule registration."""
    for task in iter_enabled_tasks(tasks):
        schedule = normalize_fixed_task_schedule(task, default_time="08:00")
        repeat_type = repeat_rule_to_type(
            schedule.get("repeat_rule"),
            repeat_mode=schedule.get("repeat_mode"),
        )
        repeat_values = list(schedule.get("repeat_values") or [])
        weekdays = repeat_values if repeat_type == "weekly" else []
        dates = repeat_values if repeat_type in {"monthly", "custom", "once"} else []
        yield {
            "time": schedule.get("time_value", "08:00"),
            "msgs": task.get("msgs", []),
            "targets": task.get("targets", []),
            "repeat_type": repeat_type,
            "weekdays": weekdays,
            "dates": dates,
            "task_id": task.get("id", ""),
        }


def should_send_scheduled_message(repeat_type, weekdays, dates, now=None):
    """Return whether a scheduled message task should fire on the given date."""
    return should_run_repeating_task(repeat_type, weekdays, dates, now)


def prepare_random_scheduled_message_day(
    task_id,
    task,
    state,
    today,
    *,
    sample_days=None,
    log_info=None,
):
    """
    Update random-message day caches and return whether later scheduling should continue.

    This only decides day eligibility. It deliberately leaves next_fire time planning,
    actual sending, and state ownership to the runtime layer.
    """
    return prepare_random_task_day(
        task_id,
        task,
        state,
        today,
        log_prefix="随机定时消息",
        log_action="发送",
        sample_days=sample_days,
        log_info=log_info,
    )


def plan_random_scheduled_message_fire_time(
    task_id,
    task,
    state,
    now,
    *,
    randint=None,
    log_info=None,
):
    """Plan today's random fire time if one is not already present."""
    return plan_random_fire_time(
        task_id,
        task,
        state,
        now,
        log_prefix="随机定时消息",
        fire_word="发送",
        randint=randint,
        log_info=log_info,
    )


def trigger_random_scheduled_message_if_due(
    task_id,
    task,
    state,
    now,
    *,
    send_scheduled_msg,
    log_info=None,
    log_error=None,
):
    """Trigger a random scheduled message when its planned fire time has arrived."""
    next_fire = state.get("next_fire")
    if next_fire is None or now < next_fire:
        return False

    log_info = log_info or (lambda message: None)
    log_error = log_error or (lambda message: None)
    log_info(f"随机定时消息 {task_id}：触发发送...")
    try:
        send_scheduled_msg(
            targets=task.get("targets", []),
            msgs=task.get("msgs", []),
            repeat_type="daily",
            weekdays=[],
            dates=[],
            task_id="",
        )
        state["last_fire_date"] = now.date()
    except Exception as exc:
        log_error(f"随机定时消息 {task_id} 发送失败：{exc}")
    finally:
        state["next_fire"] = None
    return True


def send_scheduled_messages(
    *,
    targets,
    msgs,
    repeat_type,
    weekdays,
    dates,
    task_id,
    send_text,
    send_file,
    is_image_path,
    human_delay,
    notify_error,
    nickname,
    scheduled_tasks,
    config_data,
    save_config,
    now=None,
    cancel_job=None,
    log_info=None,
    log_error=None,
):
    """
    Execute a scheduled message task using injected WeChat send operations.

    The feature owns date/repeat rules and one-time task disabling. Concrete
    WeChat operations stay injected by the runtime layer.
    """
    log_info = log_info or (lambda message: None)
    log_error = log_error or (lambda message: None)

    if not should_send_scheduled_message(repeat_type, weekdays, dates, now):
        return cancel_job if repeat_type == "once" else None

    task = {
        "id": task_id,
        "targets": list(targets or []),
        "msgs": list(msgs or []),
        "repeat_mode": "once" if repeat_type == "once" else "repeat",
    }
    execute_scheduled_message_task(
        task=task,
        scheduled_tasks=scheduled_tasks,
        config_data=config_data,
        save_config=save_config,
        send_text=send_text,
        send_file=send_file,
        is_image_path=is_image_path,
        human_delay=human_delay,
        notify_error=notify_error,
        nickname=nickname,
        log_info=log_info,
        log_error=log_error,
    )

    if repeat_type == "once":
        return cancel_job
    return None


def execute_scheduled_message_task(
    *,
    task,
    send_text,
    send_file,
    is_image_path,
    human_delay,
    should_stop=None,
    notify_error,
    nickname,
    scheduled_tasks,
    config_data,
    save_config,
    log_info=None,
    log_error=None,
):
    """Execute one concrete scheduled-message task that is already due."""
    log_info = log_info or (lambda message: None)
    log_error = log_error or (lambda message: None)
    should_stop = should_stop if callable(should_stop) else (lambda: False)
    task = task if isinstance(task, dict) else {}
    targets = [str(target or "").strip() for target in (task.get("targets") or [])]
    targets = [target for target in targets if target]
    messages = [str(message or "").strip() for message in (task.get("msgs") or [])]
    messages = [message for message in messages if message]
    success_count = 0
    failed_count = 0
    skipped_count = 0
    queued_count = 0

    log_info(f"定时消息任务开始：目标 {len(targets)} 个，内容 {len(messages)} 条")
    if not targets:
        skipped_count = 1
        log_error("定时消息没有可发送目标，本次任务已跳过")
        return {
            "result_type": "all_failed",
            "success_count": 0,
            "failed_count": 0,
            "skipped_count": skipped_count,
            "queued_count": 0,
            "target_count": 0,
            "message_count": len(messages),
            "attempted_count": 0,
        }
    if not messages:
        skipped_count = len(targets)
        log_error("定时消息没有可发送内容，本次任务已跳过")
        return {
            "result_type": "all_failed",
            "success_count": 0,
            "failed_count": 0,
            "skipped_count": skipped_count,
            "queued_count": 0,
            "target_count": len(targets),
            "message_count": 0,
            "attempted_count": 0,
        }

    for batch_index, start in enumerate(range(0, len(targets), SCHEDULED_MESSAGE_TARGET_BATCH_SIZE), start=1):
        if should_stop():
            log_info("定时消息检测到机器人停止请求，已停止后续发送")
            break
        batch = targets[start : start + SCHEDULED_MESSAGE_TARGET_BATCH_SIZE]
        for user in batch:
            for msg in messages:
                if should_stop():
                    log_info("定时消息检测到机器人停止请求，已停止后续发送")
                    break
                try:
                    if is_image_path(msg) or _looks_like_local_file_path(msg):
                        result = send_file(user, msg)
                    else:
                        result = send_text(user, msg)
                    human_delay()
                    result_status = _send_result_status(result)
                    if result_status == "queued":
                        queued_count += 1
                    elif result_status != "success":
                        failed_count += 1
                        message = _send_error_message(result)
                        log_error(f"定时消息发送失败：{message}")
                        notify_error(
                            f"{nickname} wxbot定时消息发送失败！",
                            f"{user} 定时消息发送失败：{message}",
                        )
                    else:
                        success_count += 1
                except Exception as exc:
                    failed_count += 1
                    log_error(f"定时消息发送失败：{exc}")
                    notify_error(
                        f"{nickname} wxbot定时消息发送失败！",
                        f"{user} 定时消息发送失败：{exc}",
                    )
            if should_stop():
                break

    stopped = bool(should_stop())
    if stopped:
        result_type = "manual_stop"
    elif success_count and (failed_count or queued_count):
        result_type = "partial_success"
    elif queued_count and not failed_count:
        result_type = "queued"
    elif failed_count or skipped_count:
        result_type = "all_failed"
    else:
        result_type = "success"
    return {
        "result_type": result_type,
        "success_count": success_count,
        "failed_count": failed_count,
        "skipped_count": skipped_count,
        "queued_count": queued_count,
        "target_count": len(targets),
        "message_count": len(messages),
        "attempted_count": len(targets) * len(messages),
    }


def _send_error_message(result):
    if isinstance(result, dict):
        return result.get("message", "")
    return ""
