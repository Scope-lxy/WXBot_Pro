"""Reusable scheduling rules for time-based features."""

import calendar
from datetime import datetime, timedelta
import random


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
    hour_text, minute_text = text.split(":")
    hour = max(0, min(23, int(hour_text or 0)))
    minute = max(0, min(59, int(minute_text or 0)))
    return hour, minute


def repeat_type_to_rule(repeat_type):
    repeat_type = str(repeat_type or "daily").strip() or "daily"
    mapping = {
        "daily": "daily",
        "weekly": "weekly",
        "monthly": "monthly",
        "custom": "custom_dates",
        "once": "custom_dates",
    }
    return mapping.get(repeat_type, "daily")


def repeat_rule_to_type(repeat_rule, *, repeat_mode="repeat"):
    repeat_rule = str(repeat_rule or "daily").strip() or "daily"
    repeat_mode = str(repeat_mode or "repeat").strip() or "repeat"
    if repeat_rule == "custom_dates":
        return "once" if repeat_mode == "once" else "custom"
    mapping = {
        "daily": "daily",
        "weekly": "weekly",
        "monthly": "monthly",
    }
    return mapping.get(repeat_rule, "daily")


def _clean_int_list(values, *, low=None, high=None):
    cleaned = []
    seen = set()
    for value in values or []:
        try:
            parsed = int(value)
        except (TypeError, ValueError):
            continue
        if low is not None and parsed < low:
            continue
        if high is not None and parsed > high:
            continue
        if parsed in seen:
            continue
        seen.add(parsed)
        cleaned.append(parsed)
    return cleaned


def _clean_date_list(values):
    cleaned = []
    seen = set()
    for value in values or []:
        text = str(value or "").strip()
        if not text or text in seen:
            continue
        try:
            datetime.fromisoformat(f"{text}T00:00:00")
        except ValueError:
            continue
        seen.add(text)
        cleaned.append(text)
    return cleaned


def _normalize_random_days_count(value, *, repeat_rule="daily"):
    repeat_rule = str(repeat_rule or "daily").strip() or "daily"
    limit = 31 if repeat_rule == "monthly" else 7 if repeat_rule == "weekly" else 1
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        parsed = 1
    return max(1, min(limit, parsed))


def _start_at_parts(task, *, start_at_key="start_at"):
    text = str((task or {}).get(start_at_key) or "").strip()
    if len(text) < 10:
        return "", ""
    date_part = text[:10]
    time_part = text[11:16] if len(text) >= 16 else ""
    return date_part, time_part


def normalize_fixed_task_schedule(task, *, default_time="08:00", start_at_key=None):
    task = dict(task or {})
    schedule_mode = str(task.get("schedule_mode") or "").strip()
    anchor_key = start_at_key or "start_at"
    if schedule_mode == "fixed_at":
        repeat_mode = str(task.get("repeat_mode") or "once").strip() or "once"
        repeat_rule = str(task.get("repeat_rule") or "daily").strip() or "daily"
        task["schedule_mode"] = "fixed_at"
        task["repeat_mode"] = repeat_mode
        task["repeat_rule"] = repeat_rule
        if repeat_rule == "weekly":
            task["repeat_values"] = _clean_int_list(task.get("repeat_values"), low=1, high=7)
        elif repeat_rule == "monthly":
            task["repeat_values"] = _clean_int_list(task.get("repeat_values"), low=1, high=31)
        elif repeat_rule == "custom_dates":
            task["repeat_values"] = _clean_date_list(task.get("repeat_values"))
        else:
            task["repeat_values"] = []
        task["time_value"] = str(task.get("time_value") or default_time).strip() or default_time
        fire_at = _parse_datetime(task.get("fire_at"))
        anchor_text = str(task.get(anchor_key) or "").strip()
        if fire_at is None and repeat_mode == "once" and repeat_rule == "custom_dates":
            start_date, _ = _start_at_parts(task, start_at_key=anchor_key)
            candidate_date = start_date or (task["repeat_values"][0] if task["repeat_values"] else "")
            if candidate_date:
                fire_at = _parse_datetime(f"{candidate_date}T{task['time_value']}:00")
        task["fire_at"] = _iso_datetime(fire_at) if fire_at else ""
        if task["fire_at"] and not anchor_text:
            anchor_text = task["fire_at"][:16]
        task[anchor_key] = anchor_text[:16] if anchor_text else ""
        return task

    task["schedule_mode"] = "fixed_at"
    task["repeat_mode"] = "once"
    task["repeat_rule"] = "custom_dates"
    task["repeat_values"] = []
    task["time_value"] = str(default_time or "08:00").strip() or "08:00"
    task["fire_at"] = ""
    task[anchor_key] = ""
    for key in ("time", "repeat_type", "weekdays", "dates"):
        task.pop(key, None)
    return task


def normalize_random_task_schedule(task, *, default_start="09:00", default_end="21:00"):
    task = dict(task or {})
    repeat_rule = str(task.get("repeat_rule") or "").strip()
    if str(task.get("schedule_mode") or "").strip() == "random_in_date_window":
        repeat_rule = repeat_rule or "daily"
        task["schedule_mode"] = "random_in_date_window"
        task["repeat_mode"] = str(task.get("repeat_mode") or "repeat").strip() or "repeat"
        task["repeat_rule"] = repeat_rule
        task["repeat_values"] = _clean_date_list(task.get("repeat_values")) if repeat_rule == "custom_dates" else []
        task["time_window_start"] = str(task.get("time_window_start") or default_start).strip() or default_start
        task["time_window_end"] = str(task.get("time_window_end") or default_end).strip() or default_end
        task["random_days_count"] = _normalize_random_days_count(
            task.get("random_days_count", 1),
            repeat_rule=repeat_rule,
        )
        task["time_value"] = ""
        task["fire_at"] = ""
        task["start_at"] = ""
        return task

    task["schedule_mode"] = "random_in_date_window"
    task["repeat_mode"] = "repeat"
    task["repeat_rule"] = "daily"
    task["repeat_values"] = []
    task["time_window_start"] = str(default_start or "09:00").strip() or "09:00"
    task["time_window_end"] = str(default_end or "21:00").strip() or "21:00"
    task["random_days_count"] = _normalize_random_days_count(
        1,
        repeat_rule="daily",
    )
    task["time_value"] = ""
    task["fire_at"] = ""
    task["start_at"] = ""
    for key in ("time_start", "time_end", "repeat_type", "weekdays", "dates"):
        task.pop(key, None)
    return task


def _match_repeat_date(repeat_rule, repeat_values, day):
    repeat_rule = str(repeat_rule or "daily").strip() or "daily"
    repeat_values = list(repeat_values or [])
    if repeat_rule == "daily":
        return True
    if repeat_rule == "weekly":
        return day.isoweekday() in {int(value) for value in repeat_values}
    if repeat_rule == "monthly":
        return day.day in {int(value) for value in repeat_values}
    if repeat_rule == "custom_dates":
        return day.strftime("%Y-%m-%d") in {str(value).strip() for value in repeat_values}
    return True


def _next_matching_fixed_datetime(repeat_rule, repeat_values, time_value, now):
    hour, minute = _parse_hhmm(time_value, default="00:00")
    for offset in range(0, 400):
        current_date = now.date() + timedelta(days=offset)
        if not _match_repeat_date(repeat_rule, repeat_values, current_date):
            continue
        candidate = datetime(
            current_date.year,
            current_date.month,
            current_date.day,
            hour,
            minute,
            0,
        )
        if candidate > now:
            return candidate
    return None


def _eligible_custom_dates(repeat_values, now):
    eligible = []
    for value in repeat_values or []:
        parsed = _parse_datetime(f"{str(value).strip()}T00:00:00")
        if parsed is None:
            continue
        if parsed.date() >= now.date():
            eligible.append(parsed.date())
    return sorted(set(eligible))


def _random_datetime_in_window(day, start_value, end_value, *, randint=None):
    randint = randint or random.randint
    start_hour, start_minute = _parse_hhmm(start_value, default="00:00")
    end_hour, end_minute = _parse_hhmm(end_value, default="23:59")
    start_minutes = start_hour * 60 + start_minute
    end_minutes = end_hour * 60 + end_minute
    if end_minutes < start_minutes:
        end_minutes = start_minutes
    picked_minutes = randint(start_minutes, end_minutes)
    hour, minute = divmod(picked_minutes, 60)
    return datetime(day.year, day.month, day.day, hour, minute, randint(0, 59))


def compile_task_plan(task, *, now=None, choice=None, randint=None):
    task = task if isinstance(task, dict) else {}
    now = now or datetime.now()
    choice = choice or random.choice
    randint = randint or random.randint
    existing_next_fire = _parse_datetime(task.get("next_fire_at"))
    if existing_next_fire is not None:
        task["next_fire_at"] = _iso_datetime(existing_next_fire)
        task["status"] = str(task.get("status") or "pending").strip() or "pending"
        return task

    schedule_mode = str(task.get("schedule_mode") or "fixed_at").strip() or "fixed_at"
    repeat_mode = str(task.get("repeat_mode") or "once").strip() or "once"
    repeat_rule = str(task.get("repeat_rule") or "daily").strip() or "daily"
    repeat_values = list(task.get("repeat_values") or [])

    fire_at = None
    if schedule_mode == "fixed_at":
        explicit_fire = _parse_datetime(task.get("fire_at"))
        if explicit_fire is not None:
            fire_at = explicit_fire
        else:
            fire_at = _next_matching_fixed_datetime(
                repeat_rule,
                repeat_values,
                task.get("time_value", "00:00"),
                now - timedelta(seconds=1),
            )
    elif schedule_mode == "random_in_date_window":
        candidates = _eligible_custom_dates(repeat_values, now)
        if candidates:
            chosen_date = choice(candidates)
            fire_at = _random_datetime_in_window(
                chosen_date,
                task.get("time_window_start", "00:00"),
                task.get("time_window_end", "23:59"),
                randint=randint,
            )
    elif schedule_mode == "interval_next":
        low = max(1, int(task.get("interval_min", 1) or 1))
        high = max(low, int(task.get("interval_max", low) or low))
        fire_at = now + timedelta(minutes=randint(low, high))

    if fire_at is not None:
        task["next_fire_at"] = _iso_datetime(fire_at)
        task["status"] = "pending"
    return task


def is_task_due(task, *, now=None):
    task = task if isinstance(task, dict) else {}
    now = now or datetime.now()
    if str(task.get("status") or "pending").strip() not in {"pending", "running"}:
        return False
    fire_at = _parse_datetime(task.get("next_fire_at"))
    return bool(fire_at and fire_at <= now)


def advance_task_plan_after_success(task, *, now=None, choice=None, randint=None):
    task = task if isinstance(task, dict) else {}
    now = now or datetime.now()
    choice = choice or random.choice
    randint = randint or random.randint
    task["last_run_at"] = _iso_datetime(now)
    task["last_error"] = ""
    repeat_mode = str(task.get("repeat_mode") or "once").strip() or "once"
    if repeat_mode != "repeat":
        task["status"] = "succeeded"
        return task

    schedule_mode = str(task.get("schedule_mode") or "fixed_at").strip() or "fixed_at"
    if schedule_mode == "fixed_at":
        next_fire = _next_matching_fixed_datetime(
            str(task.get("repeat_rule") or "daily").strip() or "daily",
            list(task.get("repeat_values") or []),
            task.get("time_value", "00:00"),
            now,
        )
    elif schedule_mode == "random_in_date_window":
        next_fire = _next_random_repeat_datetime(task, now, choice=choice, randint=randint)
    elif schedule_mode == "interval_next":
        low = max(1, int(task.get("interval_min", 1) or 1))
        high = max(low, int(task.get("interval_max", low) or low))
        next_fire = now + timedelta(minutes=randint(low, high))
    else:
        next_fire = None

    task["next_fire_at"] = _iso_datetime(next_fire) if next_fire else ""
    task["status"] = "pending" if next_fire else "succeeded"
    return task


def _next_random_repeat_datetime(task, now, *, choice=None, randint=None):
    choice = choice or random.choice
    randint = randint or random.randint
    repeat_rule = str(task.get("repeat_rule") or "daily").strip() or "daily"
    repeat_values = list(task.get("repeat_values") or [])
    if repeat_rule == "custom_dates":
        candidates = [day for day in _eligible_custom_dates(repeat_values, now + timedelta(days=1)) if day > now.date()]
    else:
        candidates = []
        for offset in range(0, 35):
            day = now.date() + timedelta(days=offset)
            if _match_repeat_date(repeat_rule, repeat_values, day):
                candidates.append(day)
        if candidates and candidates[0] == now.date():
            candidates = candidates[1:] or candidates
    if not candidates:
        return None
    chosen_date = choice(candidates)
    return _random_datetime_in_window(
        chosen_date,
        task.get("time_window_start", "00:00"),
        task.get("time_window_end", "23:59"),
        randint=randint,
    )


def mark_task_failed(task, error, *, now=None):
    task = task if isinstance(task, dict) else {}
    now = now or datetime.now()
    task["status"] = "failed"
    task["last_error"] = str(error or "")
    task["last_run_at"] = _iso_datetime(now)
    return task


def cancel_task_plan(task, *, now=None):
    task = task if isinstance(task, dict) else {}
    now = now or datetime.now()
    task["status"] = "cancelled"
    task["last_run_at"] = _iso_datetime(now)
    return task


def iter_enabled_tasks(tasks):
    """Yield enabled task dicts and skip malformed entries."""
    for task in tasks or []:
        if not isinstance(task, dict):
            continue
        if not task.get("enabled", True):
            continue
        yield task


def should_run_repeating_task(repeat_type, weekdays, dates, now=None):
    """Return whether a repeating task should run on the given date."""
    now = now or datetime.now()
    repeat_type = repeat_type or "daily"

    if repeat_type == "daily":
        return True
    if repeat_type == "weekly":
        return now.isoweekday() in (weekdays or [])
    if repeat_type == "monthly":
        return now.day in (dates or [])
    if repeat_type in ("custom", "once"):
        return now.strftime("%Y-%m-%d") in (dates or [])
    return True


def prepare_random_task_day(
    task_id,
    task,
    state,
    today,
    *,
    log_prefix,
    log_action,
    sample_days=None,
    log_info=None,
):
    """Update random day caches and return whether later scheduling should continue."""
    sample_days = sample_days or random.sample
    log_info = log_info or (lambda message: None)
    repeat_type = task.get("repeat_type", "daily")
    random_days_count = max(1, int(task.get("random_days_count", 1)))

    if repeat_type == "daily":
        is_eligible = True
    elif repeat_type == "weekly":
        is_eligible = _prepare_weekly_random_task_day(
            task_id,
            state,
            today,
            random_days_count,
            sample_days,
            log_info,
            log_prefix,
            log_action,
        )
    elif repeat_type == "monthly":
        is_eligible = _prepare_monthly_random_task_day(
            task_id,
            state,
            today,
            random_days_count,
            sample_days,
            log_info,
            log_prefix,
            log_action,
        )
    else:
        is_eligible = False

    if not is_eligible:
        next_fire = state.get("next_fire")
        if next_fire is not None and next_fire.date() == today:
            state["next_fire"] = None
        return False
    return state.get("last_fire_date") != today


def _prepare_weekly_random_task_day(
    task_id,
    state,
    today,
    random_days_count,
    sample_days,
    log_info,
    log_prefix,
    log_action,
):
    iso = today.isocalendar()
    week_key = (iso[0], iso[1])
    week_cache = state.get("week_cache")
    if week_cache is None or week_cache.get("key") != week_key:
        count = min(random_days_count, 7)
        selected = sorted(sample_days(range(1, 8), count))
        state["week_cache"] = {"key": week_key, "days": selected}
        log_info(f"{log_prefix} {task_id}：本周 {week_key} 随机{log_action}日 {selected}")
    return today.isoweekday() in state["week_cache"]["days"]


def _prepare_monthly_random_task_day(
    task_id,
    state,
    today,
    random_days_count,
    sample_days,
    log_info,
    log_prefix,
    log_action,
):
    month_key = (today.year, today.month)
    month_cache = state.get("month_cache")
    if month_cache is None or month_cache.get("key") != month_key:
        days_in_month = calendar.monthrange(today.year, today.month)[1]
        count = min(random_days_count, days_in_month)
        selected = sorted(sample_days(range(1, days_in_month + 1), count))
        state["month_cache"] = {"key": month_key, "days": selected}
        log_info(f"{log_prefix} {task_id}：本月 {month_key} 随机{log_action}日 {selected}")
    return today.day in state["month_cache"]["days"]


def plan_random_fire_time(
    task_id,
    task,
    state,
    now,
    *,
    log_prefix,
    fire_word,
    randint=None,
    log_info=None,
):
    """Plan today's random fire time if one is not already present."""
    if state.get("next_fire") is not None:
        return

    randint = randint or random.randint
    log_info = log_info or (lambda message: None)
    start_hour, start_minute = map(int, task.get("time_start", "00:00").split(":"))
    end_hour, end_minute = map(int, task.get("time_end", "23:59").split(":"))
    start_mins = start_hour * 60 + start_minute
    end_mins = end_hour * 60 + end_minute
    if start_mins >= end_mins:
        end_mins = start_mins + 1

    fire_mins = randint(start_mins, end_mins)
    fire_hour, fire_minute = divmod(fire_mins, 60)
    fire_dt = now.replace(
        hour=fire_hour,
        minute=fire_minute,
        second=randint(0, 59),
        microsecond=0,
    )
    if fire_dt <= now:
        fire_dt = now + timedelta(seconds=10)
    state["next_fire"] = fire_dt
    log_info(f"{log_prefix} {task_id}：今天计划于 {fire_dt.strftime('%H:%M:%S')} {fire_word}")
