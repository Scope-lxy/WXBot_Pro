"""Scheduled message business rules."""

import os

from core.scheduled_tasks import should_run_repeating_task

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


def should_send_scheduled_message(repeat_type, weekdays, dates, now=None):
    """Return whether a scheduled message task should fire on the given date."""
    return should_run_repeating_task(repeat_type, weekdays, dates, now)


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
