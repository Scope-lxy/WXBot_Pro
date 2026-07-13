"""Shared display-title helpers for task cards and runtime rows."""

from __future__ import annotations

import ntpath
import re


_MATERIAL_TYPE_LABELS = {
    "all": "全部",
    "link": "链接",
    "miniapp": "小程序",
    "personal_card": "名片",
    "image": "图片",
    "video": "视频",
    "file": "文件",
    "note": "笔记",
    "location": "位置",
    "merge": "聊天记录",
}


def _clean(value):
    return str(value or "").strip()


def is_likely_local_file_path(value):
    text = _clean(value).strip("\"'")
    if not text or re.match(r"^https?://", text, re.IGNORECASE):
        return False
    return bool(re.match(r"^[A-Za-z]:[\\/]", text) or re.match(r"^/[^/]", text))


def first_text_message(messages):
    for item in messages or []:
        text = _clean(item)
        if text and not is_likely_local_file_path(text):
            return text
    return ""


def first_file_name(messages):
    for item in messages or []:
        text = _clean(item).strip("\"'")
        if text and is_likely_local_file_path(text):
            file_name = ntpath.basename(text.replace("/", "\\"))
            if file_name:
                return file_name
    return ""


def scheduled_message_task_title(task):
    task = task if isinstance(task, dict) else {}
    return first_text_message(task.get("msgs")) or first_file_name(task.get("msgs")) or "无文案"


def material_type_title(material_types):
    values = material_types
    if isinstance(values, str):
        values = [values]
    labels = []
    for raw_value in values or []:
        key = _clean(raw_value)
        if not key:
            continue
        label = _MATERIAL_TYPE_LABELS.get(key, key)
        if label not in labels:
            labels.append(label)
    if not labels or "全部" in labels:
        return "全部"
    if len(labels) == 1:
        return labels[0]
    return "/".join(labels)


def material_outreach_task_title(task, *, materials_by_id=None):
    task = task if isinstance(task, dict) else {}
    repeat_type = _clean(task.get("repeat_type")) or "once"
    if repeat_type != "once":
        source = _clean(task.get("material_source_filter")) or "全部来源"
        return f"{source}+{material_type_title(task.get('material_types'))}"

    fixed_material_id = _clean(task.get("fixed_material_id") or task.get("material_id"))
    if fixed_material_id and isinstance(materials_by_id, dict):
        material = materials_by_id.get(fixed_material_id) or {}
        preview = _clean(material.get("content_preview")) or _clean(material.get("title"))
        if preview:
            return preview

    preview = (
        _clean(task.get("material_title"))
        or _clean(((task.get("raw_material") or {}) if isinstance(task.get("raw_material"), dict) else {}).get("content_preview"))
        or _clean(((task.get("raw_material") or {}) if isinstance(task.get("raw_material"), dict) else {}).get("title"))
    )
    return preview or "未选择素材"


def material_outreach_record_title(record):
    record = record if isinstance(record, dict) else {}
    raw_material = record.get("raw_material") if isinstance(record.get("raw_material"), dict) else {}
    title = (
        _clean(record.get("material_title"))
        or _clean(raw_material.get("content_preview"))
        or _clean(raw_material.get("title"))
    )
    return title or "无素材"
