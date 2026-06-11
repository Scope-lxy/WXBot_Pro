"""Pure rules for AI-driven material outreach."""

from datetime import datetime, timedelta
import json
import re

from core.config import coerce_int_range
from feature.task_workbench_runtime_summary import runtime_snapshot
from feature.material_outreach import (
    DEFAULT_AI_PREFACE_GOAL,
    FILTERABLE_MATERIAL_TYPES,
    normalize_material_types,
)


AI_AUTO_OUTREACH_TASK_ID = "ai_auto_outreach"
AI_AUTO_OUTREACH_TASK_NAME = "AI自动转发"
AI_OUTREACH_DECISION_FENCE_RE = re.compile(r"```(?:json)?\s*(.*?)```", re.DOTALL | re.IGNORECASE)
SENSITIVITY_LEVELS = {"conservative", "balanced", "aggressive"}
SENSITIVITY_LABELS = {
    "conservative": "严格：只选强相关素材",
    "balanced": "均衡：能自然接上就发",
    "aggressive": "宽松：可自然转场就发",
}
AI_OUTREACH_DELAY_MIN_SECONDS = 10
AI_OUTREACH_DELAY_MAX_SECONDS = 30
AI_DETECTION_INTERVAL_MINUTES = 30
AI_DETECTION_MESSAGE_THRESHOLD = 30
AI_AUTO_OUTREACH_RUNTIME_DEFAULTS = {
    "ai_material_outreach_sensitivity": "conservative",
    "ai_material_outreach_preface_enabled": True,
    "ai_material_outreach_preface_goal": DEFAULT_AI_PREFACE_GOAL,
    "ai_material_outreach_preface_intensity": "",
    "ai_material_outreach_allowed_sources": [],
    "ai_material_outreach_allowed_types": [],
}


class _AICandidateCardView(dict):
    __slots__ = ("_ai_hidden",)

    def __init__(self, public=None, hidden=None):
        super().__init__(public or {})
        self._ai_hidden = dict(hidden or {})

    def get(self, key, default=None):
        if key == "stable_signature" and "_stable_signature" in self._ai_hidden:
            return self._ai_hidden.get("_stable_signature", default)
        if key == "material_id" and "_material_id" in self._ai_hidden:
            return self._ai_hidden.get("_material_id", default)
        if key == "material_title":
            return self._ai_hidden.get("_material_title", super().get("content_preview", default))
        if key == "material_type" and "_material_type" in self._ai_hidden:
            return self._ai_hidden.get("_material_type", default)
        if key == "material_source" and "_material_source" in self._ai_hidden:
            return self._ai_hidden.get("_material_source", default)
        if key in self._ai_hidden:
            return self._ai_hidden.get(key, default)
        return super().get(key, default)


def _iso_now(now=None):
    now = now or datetime.now()
    return now.replace(microsecond=0).isoformat()


def describe_ai_outreach_sensitivity(value):
    level = str(value or "").strip().lower()
    if level not in SENSITIVITY_LEVELS:
        level = "conservative"
    return SENSITIVITY_LABELS[level]


def _parse_dt(value):
    try:
        return datetime.fromisoformat(str(value or ""))
    except ValueError:
        return None


def _minutes_since(value, now):
    past = _parse_dt(value)
    if past is None:
        return None
    return (now - past).total_seconds() / 60


def _normalize_string_list(raw):
    if isinstance(raw, str):
        raw = [raw]
    if not isinstance(raw, list):
        return []
    values = []
    for item in raw:
        text = str(item or "").strip()
        if text and text not in values:
            values.append(text)
    return values


def _normalize_allowed_material_types(raw):
    normalized = normalize_material_types(raw)
    if normalized == ["all"]:
        return []
    return [item for item in normalized if item in FILTERABLE_MATERIAL_TYPES]


def _safe_non_negative_int(raw, default=0):
    try:
        value = int(raw)
    except (TypeError, ValueError):
        value = int(default)
    return max(0, value)


def normalize_ai_material_outreach_config(raw):
    raw = raw if isinstance(raw, dict) else {}
    sensitivity = str(raw.get("ai_material_outreach_sensitivity", "conservative") or "").strip().lower()
    if sensitivity not in SENSITIVITY_LEVELS:
        sensitivity = "conservative"
    return {
        "ai_material_outreach_switch": bool(raw.get("ai_material_outreach_switch", False)),
        "ai_material_outreach_sensitivity": sensitivity,
        "ai_material_outreach_daily_limit_per_friend": coerce_int_range(
            raw.get("ai_material_outreach_daily_limit_per_friend", 3), 3, 1, 99
        ),
        "ai_material_outreach_delay_min_seconds": AI_OUTREACH_DELAY_MIN_SECONDS,
        "ai_material_outreach_delay_max_seconds": AI_OUTREACH_DELAY_MAX_SECONDS,
        "ai_material_outreach_preface_enabled": bool(raw.get("ai_material_outreach_preface_enabled", True)),
        "ai_material_outreach_preface_goal": str(raw.get("ai_material_outreach_preface_goal") or "").strip() or DEFAULT_AI_PREFACE_GOAL,
        "ai_material_outreach_preface_intensity": str(raw.get("ai_material_outreach_preface_intensity") or "").strip(),
        "ai_material_outreach_allowed_sources": _normalize_string_list(raw.get("ai_material_outreach_allowed_sources")),
        "ai_material_outreach_allowed_types": _normalize_allowed_material_types(raw.get("ai_material_outreach_allowed_types")),
        "ai_material_outreach_detection_interval_minutes": coerce_int_range(
            raw.get("ai_material_outreach_detection_interval_minutes", AI_DETECTION_INTERVAL_MINUTES),
            AI_DETECTION_INTERVAL_MINUTES,
            1,
            1440,
        ),
        "ai_material_outreach_detection_message_threshold": coerce_int_range(
            raw.get("ai_material_outreach_detection_message_threshold", AI_DETECTION_MESSAGE_THRESHOLD),
            AI_DETECTION_MESSAGE_THRESHOLD,
            1,
            999,
        ),
    }


def normalize_ai_auto_outreach_runtime_config(raw):
    defaults = normalize_ai_material_outreach_config(AI_AUTO_OUTREACH_RUNTIME_DEFAULTS)
    defaults.update(normalize_ai_material_outreach_config(raw))
    return defaults


def normalize_ai_detection_state(raw):
    raw = raw if isinstance(raw, dict) else {}
    normalized = {}
    for target, record in raw.items():
        target = str(target or "").strip()
        if not target:
            continue
        record = record if isinstance(record, dict) else {}
        normalized[target] = {
            "window_started_at": str(record.get("window_started_at") or "").strip(),
            "new_message_count": _safe_non_negative_int(record.get("new_message_count"), 0),
        }
    return normalized


def normalize_ai_detection_record(raw):
    if not isinstance(raw, dict):
        raw = {}
    return {
        "window_started_at": str(raw.get("window_started_at") or "").strip(),
        "new_message_count": _safe_non_negative_int(raw.get("new_message_count"), 0),
    }


def record_ai_detection_message(target, state, now=None):
    now = now or datetime.now()
    target = str(target or "").strip()
    normalized = normalize_ai_detection_state(state)
    if not target:
        return normalized
    record = dict(normalized.get(target) or {})
    if not record.get("window_started_at"):
        record["window_started_at"] = _iso_now(now)
        record["new_message_count"] = 1
    else:
        record["new_message_count"] = _safe_non_negative_int(
            record.get("new_message_count"), 0
        ) + 1
    normalized[target] = record
    return normalized


def clear_ai_detection_target(target, state):
    target = str(target or "").strip()
    normalized = normalize_ai_detection_state(state)
    if not target:
        return normalized
    normalized.pop(target, None)
    return normalized


def clear_ai_detection_target_if_matches(target, state, expected_record):
    target = str(target or "").strip()
    normalized = normalize_ai_detection_state(state)
    if not target:
        return normalized
    current_record = normalize_ai_detection_record(normalized.get(target) or {})
    expected_record = normalize_ai_detection_record(expected_record)
    if current_record != expected_record:
        return normalized
    normalized.pop(target, None)
    return normalized


def should_trigger_ai_detection(target, state, *, interval_minutes, message_threshold, now=None):
    now = now or datetime.now()
    target = str(target or "").strip()
    if not target:
        return False
    record = dict(normalize_ai_detection_state(state).get(target) or {})
    window_started_at = record.get("window_started_at")
    if not window_started_at:
        return False
    minutes = _minutes_since(window_started_at, now)
    if minutes is None:
        return False
    return minutes >= int(interval_minutes or 0) and _safe_non_negative_int(
        record.get("new_message_count"), 0
    ) >= int(message_threshold or 0)
def build_ai_outreach_candidates_for_target(candidate_cards, send_records, target):
    target = str(target or "").strip()
    sent_signatures = {
        str(record.get("stable_signature") or "").strip()
        for record in (send_records or [])
        if str(record.get("target") or "").strip() == target and record.get("success")
    }
    candidates = []
    for item in candidate_cards or []:
        card = item if isinstance(item, dict) else {}
        signature = str(card.get("_stable_signature") or card.get("stable_signature") or "").strip()
        if not signature:
            material_type = str(card.get("_material_type") or card.get("material_type") or card.get("type") or "").strip()
            content_preview = str(
                card.get("content_preview")
                or card.get("material_title")
                or card.get("summary")
                or ""
            ).strip()
            if material_type and content_preview:
                signature = f"{material_type}|{content_preview}"
        if not signature or signature in sent_signatures:
            continue
        public = {
            "index": card.get("index", 0),
            "type": card.get("type") or card.get("material_type") or card.get("_material_type") or "",
            "content_preview": card.get("content_preview") or card.get("material_title") or card.get("summary") or "",
            "ownership": card.get("ownership") or "",
            "copy_note": card.get("copy_note") or "",
        }
        hidden = {
            "_stable_signature": signature,
        }
        for key in ("_material_id", "_material_source", "_material_type"):
            value = card.get(key)
            if str(value or "").strip():
                hidden[key] = str(value)
        material_title = card.get("_material_title") or card.get("material_title") or card.get("content_preview") or card.get("summary") or ""
        if str(material_title).strip():
            hidden["_material_title"] = str(material_title)
        candidates.append(_AICandidateCardView(public, hidden))
    return candidates


def filter_ai_outreach_candidate_pool(candidate_cards, *, allowed_sources=None, allowed_types=None):
    allowed_sources = _normalize_string_list(allowed_sources)
    allowed_types = _normalize_allowed_material_types(allowed_types)
    filtered = []
    for item in candidate_cards or []:
        card = item if isinstance(item, dict) else {}
        source = str(
            card.get("_material_source")
            or card.get("material_source")
            or card.get("source")
            or ""
        ).strip()
        material_type = str(
            card.get("_material_type")
            or card.get("material_type")
            or card.get("type")
            or ""
        ).strip()
        if allowed_sources and source not in allowed_sources:
            continue
        if allowed_types and material_type not in allowed_types:
            continue
        filtered.append(card)
    return filtered


def _count_daily_success(send_records, target, *, now=None):
    now = now or datetime.now()
    today = now.date()
    count = 0
    for record in send_records or []:
        if record.get("task_id") != AI_AUTO_OUTREACH_TASK_ID or record.get("target") != target or not record.get("success"):
            continue
        sent_at = _parse_dt(record.get("sent_at"))
        if sent_at and sent_at.date() == today:
            count += 1
    return count


def _has_pending_queue_for_target(queue_records, target):
    return any(
        str(record.get("status") or "").strip() == "pending"
        and str(record.get("target") or "").strip() == str(target or "").strip()
        for record in (queue_records or [])
    )


def evaluate_ai_outreach_gate(
    config,
    *,
    is_private_ai_reply,
    target,
    candidate_cards,
    send_records,
    queue_records,
    now=None,
):
    config = normalize_ai_material_outreach_config(config)
    target = str(target or "").strip()
    now = now or datetime.now()
    if not config["ai_material_outreach_switch"]:
        return {"allowed": False, "reason": "switch_off"}
    if not is_private_ai_reply:
        return {"allowed": False, "reason": "not_private_ai_reply"}
    if not target:
        return {"allowed": False, "reason": "missing_target"}
    if not candidate_cards:
        return {"allowed": False, "reason": "no_candidate"}
    if _has_pending_queue_for_target(queue_records, target):
        return {"allowed": False, "reason": "pending_exists"}
    if _count_daily_success(send_records, target, now=now) >= config["ai_material_outreach_daily_limit_per_friend"]:
        return {"allowed": False, "reason": "daily_limit_reached"}
    return {"allowed": True, "reason": ""}


def build_ai_pending_record(
    target,
    selected_material,
    decision,
    config,
    *,
    chat_name="",
    now=None,
    random_delay_seconds=None,
    queue_id_factory=None,
    generated_preface="",
):
    config = normalize_ai_material_outreach_config(config)
    now = now or datetime.now()
    random_delay_seconds = random_delay_seconds or (lambda low, high: low)
    queue_id_factory = queue_id_factory or (lambda: f"aiq_{now.strftime('%Y%m%d%H%M%S')}")
    queue_id = str(queue_id_factory() or "").strip()
    delay_seconds = max(
        config["ai_material_outreach_delay_min_seconds"],
        min(
            config["ai_material_outreach_delay_max_seconds"],
            int(random_delay_seconds(
                config["ai_material_outreach_delay_min_seconds"],
                config["ai_material_outreach_delay_max_seconds"],
            )),
        ),
    )
    scheduled_at = now + timedelta(seconds=delay_seconds)
    expires_at = now + timedelta(minutes=30)
    preface_enabled = bool(config["ai_material_outreach_preface_enabled"])
    material_id = str(selected_material.get("material_id") or selected_material.get("_material_id") or "").strip()
    material_title = str(
        selected_material.get("material_title")
        or selected_material.get("_material_title")
        or selected_material.get("content_preview")
        or ""
    ).strip()
    material_type = str(selected_material.get("material_type") or selected_material.get("_material_type") or "").strip()
    material_source = str(selected_material.get("material_source") or selected_material.get("_material_source") or "").strip()
    ownership = str(selected_material.get("ownership") or "").strip()
    copy_note = str(selected_material.get("copy_note") or "").strip()
    snapshot = runtime_snapshot(
        raw_targets=[{"target": str(target or "").strip(), "display_name": str(chat_name or target or "").strip()}],
        raw_messages=(
            [{"type": "text", "text": str(generated_preface or "").strip()}]
            if preface_enabled and str(generated_preface or "").strip()
            else []
        ),
        raw_media=[],
        raw_material={
            "material_id": material_id,
            "title": material_title,
            "type": material_type,
            "source": material_source,
            "ownership": ownership,
            "copy_note": copy_note,
        },
        targets_summary=str(chat_name or target or "").strip(),
        content_summary="AI 附加文案待发送" if preface_enabled else "AI 素材待发送",
        material_summary="{}：{}".format(material_type, material_title).strip("："),
        batch_id=queue_id or AI_AUTO_OUTREACH_TASK_ID,
        run_id="",
    )
    return {
        "queue_id": queue_id,
        "task_id": AI_AUTO_OUTREACH_TASK_ID,
        "task_name": AI_AUTO_OUTREACH_TASK_NAME,
        "status": "pending",
        "chat_name": str(chat_name or target or ""),
        "target": str(target or ""),
        "material_id": material_id,
        "stable_signature": str(selected_material.get("stable_signature") or ""),
        "material_title": material_title,
        "material_type": material_type,
        "material_source": material_source,
        "decision_reason": "",
        "send_strategy": str(decision.get("send_strategy") or "").strip(),
        "scheduled_at": scheduled_at.replace(microsecond=0).isoformat(),
        "created_at": _iso_now(now),
        "expires_at": expires_at.replace(microsecond=0).isoformat(),
        "preface_enabled": preface_enabled,
        "preface": str(generated_preface or "").strip() if preface_enabled else "",
        "error": "",
        **snapshot,
    }


def expire_ai_pending_records(queue_records, *, now=None):
    now = now or datetime.now()
    changed = 0
    for record in queue_records or []:
        if str(record.get("status") or "").strip() != "pending":
            continue
        expires_at = _parse_dt(record.get("expires_at"))
        if expires_at and expires_at <= now:
            record["status"] = "expired"
            changed += 1
    return changed


def due_ai_pending_records(queue_records, *, now=None):
    now = now or datetime.now()
    due = []
    for record in queue_records or []:
        if str(record.get("status") or "").strip() != "pending":
            continue
        scheduled_at = _parse_dt(record.get("scheduled_at"))
        expires_at = _parse_dt(record.get("expires_at"))
        if expires_at and expires_at <= now:
            continue
        if scheduled_at and scheduled_at <= now:
            due.append(record)
    return due


def cancel_ai_pending_record(queue_records, queue_id):
    queue_id = str(queue_id or "").strip()
    for index, record in enumerate(list(queue_records or [])):
        if str(record.get("queue_id") or "").strip() != queue_id:
            continue
        if str(record.get("status") or "").strip() != "pending":
            return False
        del queue_records[index]
        return True
    return False


def cancel_ai_pending_records(queue_records):
    records = list(queue_records or [])
    kept_records = [record for record in records if str((record or {}).get("status") or "").strip() != "pending"]
    changed = len(records) - len(kept_records)
    if changed > 0:
        queue_records[:] = kept_records
    return changed


def parse_ai_outreach_decision(text):
    raw = str(text or "").strip()
    if not raw:
        raise ValueError("empty decision")
    match = AI_OUTREACH_DECISION_FENCE_RE.search(raw)
    if match:
        raw = match.group(1).strip()
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError("invalid json") from exc
    if not isinstance(data, dict):
        raise ValueError("invalid payload")
    if "stable_signature" in data:
        raise ValueError("legacy stable_signature payload")
    required = {"should_send", "selected_index", "send_strategy"}
    if not required.issubset(data):
        raise ValueError("missing required fields")
    unexpected = {"reason", "preface"} & set(data)
    if unexpected:
        raise ValueError("unexpected legacy fields")
    should_send = data.get("should_send")
    if not isinstance(should_send, bool):
        raise ValueError("invalid should_send")
    selected_index = data.get("selected_index")
    if isinstance(selected_index, bool) or not isinstance(selected_index, int):
        raise ValueError("invalid selected_index")
    if should_send and selected_index <= 0:
        raise ValueError("selected_index must be > 0 when should_send is true")
    if not should_send and selected_index != 0:
        raise ValueError("selected_index must be 0 when should_send is false")
    return {
        "should_send": should_send,
        "selected_index": selected_index,
        "send_strategy": str(data.get("send_strategy") or "").strip(),
    }
