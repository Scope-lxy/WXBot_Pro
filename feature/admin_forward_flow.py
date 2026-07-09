"""Thin helpers for the admin forward workflow."""

from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timedelta
import re


FORWARD_ACTIVE_STATUSES = {
    "waiting_material",
    "waiting_target_scope",
    "waiting_target_exclude",
    "waiting_delay",
    "confirming",
}
FORWARD_AUTO_CANCEL_SECONDS = 20
PRESET_DELAY_MINUTES = {"1": 10, "2": 30, "3": 60}


def start_prompt():
    return "请现在转发一条素材给我，20秒内未收到会自动取消"


def is_active_draft(draft):
    return bool(isinstance(draft, dict) and str(draft.get("status") or "").strip() in FORWARD_ACTIVE_STATUSES)


def arm_auto_cancel(draft, *, now=None):
    draft = draft if isinstance(draft, dict) else {}
    now = now or datetime.now()
    draft["auto_cancel_deadline"] = (
        now + timedelta(seconds=FORWARD_AUTO_CANCEL_SECONDS)
    ).replace(microsecond=0).isoformat()
    return draft


def top_contact_tags(directory, *, limit=9):
    counts = defaultdict(int)
    order = {}
    for subject in (directory or {}).get("subjects") or []:
        if not isinstance(subject, dict):
            continue
        if subject.get("subject_type", "friend") != "friend" or subject.get("status", "active") != "active":
            continue
        for tag in subject.get("tags") or []:
            text = str(tag or "").strip()
            if text:
                if text not in order:
                    order[text] = len(order)
                counts[text] += 1
    ordered = sorted(counts.items(), key=lambda item: (-item[1], order[item[0]], item[0]))
    return [tag for tag, _count in ordered[: max(0, int(limit or 9))]]


def render_target_prompt(tags):
    lines = ["想发给谁：", "1. 全部"]
    for index, tag in enumerate(tags[:9], start=2):
        lines.append(f"{index}. {tag}")
    lines.append("直接回复编号即可")
    return "\n".join(lines)


def render_exclude_prompt(tags):
    lines = ["不发给谁：", "0. 不排除"]
    for index, tag in enumerate(tags[:9], start=1):
        lines.append(f"{index}. {tag}")
    lines.append("直接回复编号即可")
    return "\n".join(lines)


def render_delay_prompt():
    return "\n".join(
        [
            "你想什么时候开始发送？",
            "1. 10分钟后",
            "2. 30分钟后",
            "3. 60分钟后",
            "4. 直接回 X 分钟后",
            "例如直接回复 123",
        ]
    )


def parse_compact_choices(raw):
    text = str(raw or "").strip()
    if not text or not re.fullmatch(r"[0-9,\s]+", text):
        return []
    digits = [char for char in text if char.isdigit()]
    choices = []
    seen = set()
    for char in digits:
        value = int(char)
        if value in seen:
            continue
        seen.add(value)
        choices.append(value)
    return choices


def parse_target_scope(raw, tags):
    choices = parse_compact_choices(raw)
    if choices == [1]:
        return {"mode": "all", "include_tags": [], "exclude_tags": []}
    max_index = len(tags) + 1
    if not choices or 1 in choices or any(choice < 2 or choice > max_index for choice in choices):
        return None
    return {
        "mode": "include",
        "include_tags": [tags[choice - 2] for choice in choices],
        "exclude_tags": [],
    }


def parse_exclude_scope(raw, tags):
    choices = parse_compact_choices(raw)
    if choices == [0]:
        return []
    max_index = len(tags)
    if not choices or 0 in choices or any(choice < 1 or choice > max_index for choice in choices):
        return None
    return [tags[choice - 1] for choice in choices]


def parse_delay_minutes(raw):
    text = str(raw or "").strip()
    if text in PRESET_DELAY_MINUTES:
        return PRESET_DELAY_MINUTES[text]
    if not text.isdigit():
        return None
    value = int(text)
    if value <= 0:
        return None
    return value


def format_target_summary(draft):
    include_tags = [str(item or "").strip() for item in (draft or {}).get("include_tags") or [] if str(item or "").strip()]
    exclude_tags = [str(item or "").strip() for item in (draft or {}).get("exclude_tags") or [] if str(item or "").strip()]
    if include_tags:
        return "仅标签 " + "、".join(include_tags)
    if exclude_tags:
        return "全部，排除 " + "、".join(exclude_tags)
    return "全部"


def build_confirmation_reply(draft):
    material_label = str((draft or {}).get("material_type_label") or "素材").strip()
    delay_minutes = int((draft or {}).get("delay_minutes") or 0)
    scheduled_at = str((draft or {}).get("scheduled_at") or "").strip().replace("T", " ")
    lines = [
        "确认创建这条转发任务吗？",
        "",
        f"素材：{material_label}",
        f"发送范围：{format_target_summary(draft)}",
        f"开始时间：{delay_minutes} 分钟后",
        f"预计发送时间：{scheduled_at}",
        "",
        "回复 1 确认",
        "回复 0 取消",
    ]
    return "\n".join(lines)
