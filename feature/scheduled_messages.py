"""Scheduled message business rules."""

import os

from core.wechat_ui_actions import ActionBatchInterrupted
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


def _normalize_target(value):
    if isinstance(value, dict):
        send_name = str(value.get("send_name") or value.get("target") or "").strip()
        if not send_name:
            return None
        return {
            "contact_key": str(value.get("contact_key") or "").strip(),
            "send_name": send_name,
            "display_name": str(value.get("display_name") or send_name).strip() or send_name,
            "require_contact_key": bool(value.get("require_contact_key")),
        }
    send_name = str(value or "").strip()
    return send_name or None


def _target_label(value):
    if isinstance(value, dict):
        return str(value.get("display_name") or value.get("send_name") or "").strip()
    return str(value or "").strip()


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
        if status in {"cancelled", "canceled", "已取消"}:
            return "cancelled"
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
    delivery_records=None,
    on_delivery_state=None,
    send_actions=None,
):
    """Execute one concrete scheduled-message task that is already due."""
    log_info = log_info or (lambda message: None)
    log_error = log_error or (lambda message: None)
    should_stop = should_stop if callable(should_stop) else (lambda: False)
    task = task if isinstance(task, dict) else {}
    targets = [_normalize_target(target) for target in (task.get("targets") or [])]
    targets = [target for target in targets if target]
    messages = [str(message or "").strip() for message in (task.get("msgs") or [])]
    messages = [message for message in messages if message]
    success_count = 0
    failed_count = 0
    skipped_count = 0
    queued_count = 0
    uncertain_count = 0
    delivery_records = delivery_records if isinstance(delivery_records, list) else []
    delivery_by_key = {
        str(item.get("key") or ""): item
        for item in delivery_records
        if isinstance(item, dict) and str(item.get("key") or "")
    }
    on_delivery_state = on_delivery_state if callable(on_delivery_state) else (lambda _record: None)
    send_actions = send_actions if callable(send_actions) else None

    def record_delivery_result(delivery, result, user_label):
        nonlocal success_count, failed_count, queued_count
        result_status = _send_result_status(result)
        if result_status == "queued":
            queued_count += 1
            delivery["status"] = "queued"
        elif result_status != "success":
            failed_count += 1
            message = _send_error_message(result)
            delivery["status"] = "cancelled" if result_status == "cancelled" else "failed"
            delivery["error"] = message
            if result_status != "cancelled":
                log_error(f"定时消息发送失败：{message}")
                notify_error(
                    f"{nickname} wxbot定时消息发送失败！",
                    f"{user_label} 定时消息发送失败：{message}",
                )
        else:
            success_count += 1
            delivery["status"] = "done"

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
        for user_offset, user in enumerate(batch):
            target_index = start + user_offset
            if send_actions is not None:
                pending = []
                actions = []
                user_label = _target_label(user)
                for message_index, msg in enumerate(messages):
                    key = f"{target_index}:{message_index}"
                    delivery = delivery_by_key.get(key)
                    if delivery is not None and delivery.get("status") in {"done", "failed", "queued", "uncertain", "cancelled"}:
                        continue
                    if delivery is None:
                        delivery = {
                            "key": key,
                            "target": user,
                            "message_index": message_index,
                            "status": "pending",
                            "error": "",
                        }
                        delivery_records.append(delivery)
                        delivery_by_key[key] = delivery
                    pending.append(delivery)
                    actions.append({
                        "type": "file" if is_image_path(msg) or _looks_like_local_file_path(msg) else "text",
                        "path" if is_image_path(msg) or _looks_like_local_file_path(msg) else "text": msg,
                    })
                if not actions:
                    continue
                for delivery in pending:
                    delivery["status"] = "inflight"
                    delivery["error"] = ""
                    on_delivery_state(delivery)
                try:
                    action_results = send_actions(user, actions)
                    action_results = list(action_results or []) if isinstance(action_results, (list, tuple)) else [action_results]
                    while len(action_results) < len(pending):
                        action_results.append(False)
                    for delivery, action_result in zip(pending, action_results):
                        record_delivery_result(delivery, action_result, user_label)
                except ActionBatchInterrupted as exc:
                    for delivery, action_result in zip(pending, exc.completed_results):
                        record_delivery_result(delivery, action_result, user_label)
                    failed_index = min(max(0, exc.failed_index), len(pending) - 1)
                    failed_delivery = pending[failed_index]
                    failed_delivery["status"] = "uncertain"
                    failed_delivery["error"] = str(exc)
                    uncertain_count += 1
                    for delivery in pending[failed_index + 1:]:
                        delivery["status"] = "pending"
                        delivery["error"] = ""
                    log_error(f"定时消息第 {failed_index + 1} 项发送结果不确定：{exc.cause}")
                    notify_error(
                        f"{nickname} wxbot定时消息发送结果待确认！",
                        f"{user_label} 第 {failed_index + 1} 项可能已发出，后续未开始项将保留：{exc.cause}",
                    )
                except Exception as exc:
                    uncertain_count += len(pending)
                    for delivery in pending:
                        delivery["status"] = "uncertain"
                        delivery["error"] = str(exc)
                    log_error(f"定时消息发送结果不确定：{exc}")
                    notify_error(
                        f"{nickname} wxbot定时消息发送结果待确认！",
                        f"{user_label} 定时消息可能已发出，请勿盲目重发：{exc}",
                    )
                finally:
                    for delivery in pending:
                        on_delivery_state(delivery)
                for _action in actions:
                    human_delay()
                continue
            for message_index, msg in enumerate(messages):
                if should_stop():
                    log_info("定时消息检测到机器人停止请求，已停止后续发送")
                    break
                key = f"{target_index}:{message_index}"
                user_label = _target_label(user)
                delivery = delivery_by_key.get(key)
                if delivery is not None and delivery.get("status") in {"done", "failed", "queued", "uncertain", "cancelled"}:
                    continue
                if delivery is None:
                    delivery = {
                        "key": key,
                        "target": user,
                        "message_index": message_index,
                        "status": "pending",
                        "error": "",
                    }
                    delivery_records.append(delivery)
                    delivery_by_key[key] = delivery
                delivery["status"] = "inflight"
                delivery["error"] = ""
                on_delivery_state(delivery)
                try:
                    if is_image_path(msg) or _looks_like_local_file_path(msg):
                        result = send_file(user, msg)
                    else:
                        result = send_text(user, msg)
                    human_delay()
                    record_delivery_result(delivery, result, user_label)
                except Exception as exc:
                    uncertain_count += 1
                    delivery["status"] = "uncertain"
                    delivery["error"] = str(exc)
                    log_error(f"定时消息发送结果不确定：{exc}")
                    notify_error(
                        f"{nickname} wxbot定时消息发送结果待确认！",
                        f"{user_label} 定时消息可能已发出，请勿盲目重发：{exc}",
                    )
                finally:
                    on_delivery_state(delivery)
            if should_stop():
                break

    stopped = bool(should_stop())
    if stopped:
        result_type = "manual_stop"
    elif uncertain_count:
        result_type = "uncertain"
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
        "uncertain_count": uncertain_count,
        "delivery_records": delivery_records,
        "target_count": len(targets),
        "message_count": len(messages),
        "attempted_count": len(targets) * len(messages),
    }


def _send_error_message(result):
    if isinstance(result, dict):
        return result.get("message", "")
    return ""
