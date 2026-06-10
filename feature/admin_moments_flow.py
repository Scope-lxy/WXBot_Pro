"""Thin helpers for the admin moments workflow."""

from __future__ import annotations

from datetime import datetime, timedelta


MOMENTS_AUTO_CANCEL_SECONDS = 20
MOMENTS_ACTIVE_STATUSES = {"collecting", "preview_ready", "confirming"}


def start_prompt():
    return "请现在发送文案或图片给我，20秒内未收到会自动取消\n收到后我会自动生成 3 条 AI 文案"


def image_limit_prompt():
    return "最多只能收 9 张图片，请先 /取消发圈，或等待自动生成预览"


def invalid_confirm_prompt():
    return "请回复 1 或 0"


def build_candidate_selection_reply(draft):
    candidates = [
        str(item or "").strip()
        for item in ((draft or {}).get("generated_candidates") or [])
        if str(item or "").strip()
    ]
    lines = ["朋友圈预览", ""]
    for idx, candidate in enumerate(candidates[:3], start=1):
        lines.append(f"{idx}. {candidate}")
    lines.extend(["", "请回复 1/2/3 选择文案"])
    return "\n".join(lines).strip()


def build_confirmation_reply(draft):
    selected_index = int((draft or {}).get("selected_candidate_index") or 0)
    image_count = len((draft or {}).get("images") or [])
    lines = [
        "确认创建这条发圈任务吗？",
        "",
        f"已选文案：第 {selected_index} 条",
        f"图片：{image_count} 张",
        "可见范围：公开",
        "",
        "回复 1 确认",
        "回复 0 取消",
    ]
    return "\n".join(lines)


def arm_auto_cancel(draft, *, now=None):
    draft = draft if isinstance(draft, dict) else {}
    now = now or datetime.now()
    draft["auto_cancel_deadline"] = (
        now + timedelta(seconds=MOMENTS_AUTO_CANCEL_SECONDS)
    ).replace(microsecond=0).isoformat()
    return draft


def clear_auto_cancel(draft):
    draft = draft if isinstance(draft, dict) else {}
    draft["auto_cancel_deadline"] = ""
    return draft


def is_active_draft(draft):
    return bool(isinstance(draft, dict) and str(draft.get("status") or "").strip() in MOMENTS_ACTIVE_STATUSES)


def select_candidate_for_confirmation(draft, candidate_index):
    draft = draft if isinstance(draft, dict) else {}
    draft["status"] = "confirming"
    draft["selected_candidate_index"] = int(candidate_index)
    draft["auto_preview_deadline"] = ""
    return draft
