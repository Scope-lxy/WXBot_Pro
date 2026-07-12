"""Unified Moments task model shared by admin commands and the web panel."""

from copy import deepcopy
import json
import os
import shutil
from datetime import datetime, timedelta
from pathlib import Path
import secrets
import re
from uuid import uuid4


DEFAULT_MOMENTS_RANDOM_WINDOW = "12:00 - 21:30"
AUTO_PREVIEW_SECONDS = 20
MAX_MOMENTS_IMAGES = 9


STATUS_PENDING_CONFIRM = "pending_confirm"
STATUS_PENDING = "pending"
STATUS_RUNNING = "running"
STATUS_EXECUTED = "executed"
MOMENTS_TASK_STATUS_VALUES = {
    STATUS_PENDING_CONFIRM,
    STATUS_PENDING,
    STATUS_RUNNING,
    STATUS_EXECUTED,
}

MOMENTS_TASK_DEFINITION_FIELDS = (
    "id",
    "source",
    "file_storage_mode",
    "enabled",
    "raw_text",
    "images",
    "copy_mode",
    "publish_rule",
    "publish_time",
    "publish_window",
    "visibility_type",
    "tags",
    "candidates",
    "selected_caption",
    "created_at",
    "updated_at",
)

MOMENTS_TASK_RUNTIME_FIELDS = (
    "status",
    "execute_after",
    "queued_at",
    "queued_mode",
    "ai_generation_status",
    "ai_generation_error",
)

MOMENTS_TASK_HISTORY_FIELDS = (
    "executed_at",
    "execution_result",
    "execution_message",
    "execution_snapshot",
)


def _normalize_execution_snapshot(snapshot):
    if not isinstance(snapshot, dict):
        return {}
    return {
        "targets_summary": str(snapshot.get("targets_summary") or "").strip(),
        "content_summary": str(snapshot.get("content_summary") or "").strip(),
        "media_summary": str(snapshot.get("media_summary") or "").strip(),
        "material_summary": str(snapshot.get("material_summary") or "").strip(),
        "batch_summary": str(snapshot.get("batch_summary") or "").strip(),
        "result_summary": str(snapshot.get("result_summary") or "").strip(),
        "raw_targets": deepcopy(snapshot.get("raw_targets")) if isinstance(snapshot.get("raw_targets"), list) else [],
        "raw_messages": deepcopy(snapshot.get("raw_messages")) if isinstance(snapshot.get("raw_messages"), list) else [],
        "raw_media": deepcopy(snapshot.get("raw_media")) if isinstance(snapshot.get("raw_media"), list) else [],
        "raw_material": deepcopy(snapshot.get("raw_material")) if isinstance(snapshot.get("raw_material"), dict) else {},
        "batch_id": str(snapshot.get("batch_id") or "").strip(),
        "run_id": str(snapshot.get("run_id") or "").strip(),
    }


def create_empty_draft(*, draft_id=None, source="admin_command"):
    return {
        "draft_id": str(draft_id or f"moments_{uuid4().hex[:8]}"),
        "status": "collecting",
        "texts": [],
        "images": [],
        "privacy": "public",
        "tags": [],
        "generated_candidates": [],
        "preview_generated_at": "",
        "source": str(source or "admin_command"),
        "auto_cancel_deadline": "",
        "auto_preview_deadline": "",
        "selected_candidate_index": 0,
    }


def load_active_draft(path):
    path = Path(path)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, UnicodeDecodeError):
        return None
    return data if isinstance(data, dict) and data.get("draft_id") else None


def save_active_draft(path, draft):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(draft, ensure_ascii=False, indent=2), encoding="utf-8")
    return draft


def clear_active_draft(path):
    path = Path(path)
    try:
        path.unlink()
    except FileNotFoundError:
        pass
    return None


def invalidate_preview(draft):
    draft["status"] = "collecting"
    draft["generated_candidates"] = []
    draft["preview_generated_at"] = ""
    draft["selected_candidate_index"] = 0
    return draft


def bump_auto_preview_deadline(draft, *, now):
    draft["auto_preview_deadline"] = (
        now + timedelta(seconds=AUTO_PREVIEW_SECONDS)
    ).replace(microsecond=0).isoformat()
    return draft


def append_draft_text(draft, text, *, now=None):
    text = str(text or "").strip()
    if not text:
        return draft
    if draft.get("status") == "preview_ready":
        invalidate_preview(draft)
    draft.setdefault("texts", []).append(text)
    if now is not None:
        bump_auto_preview_deadline(draft, now=now)
    return draft


def append_draft_image(draft, image_path, *, now=None):
    image_path = str(image_path or "").strip()
    if not image_path:
        return draft
    images = draft.setdefault("images", [])
    if len(images) >= MAX_MOMENTS_IMAGES:
        raise ValueError("最多只能收 9 张图片")
    if draft.get("status") == "preview_ready":
        invalidate_preview(draft)
    images.append(image_path)
    if now is not None:
        bump_auto_preview_deadline(draft, now=now)
    return draft


def moments_uploads_root(data_dir, wx_id, *, create=False):
    root = Path(str(data_dir or "")).expanduser() / "accounts" / str(wx_id or "default") / "tasks" / "moments" / "uploads"
    if create:
        root.mkdir(parents=True, exist_ok=True)
    return root


def _safe_moments_upload_group(value):
    text = str(value or "").strip() or "admin"
    text = re.sub(r"[^A-Za-z0-9_.-]+", "_", text)
    return text.strip("._") or "admin"


def copy_moments_admin_upload(source_path, *, data_dir, wx_id, draft_id):
    source = Path(str(source_path or "").strip())
    if not str(source) or not source.is_file():
        return str(source_path or "").strip()
    upload_root = moments_uploads_root(data_dir, wx_id, create=True)
    try:
        if source.resolve().is_relative_to(upload_root.resolve()):
            return str(source.resolve())
    except OSError:
        pass
    group_dir = upload_root / _safe_moments_upload_group(draft_id)
    group_dir.mkdir(parents=True, exist_ok=True)
    stem = re.sub(r"[^A-Za-z0-9_.-]+", "_", source.stem).strip("._") or "image"
    suffix = source.suffix.lower() if source.suffix else ".jpg"
    target = group_dir / f"{stem}_{uuid4().hex[:8]}{suffix}"
    shutil.copy2(source, target)
    return str(target.resolve())


def delete_managed_moments_uploads(images, *, data_dir, wx_id):
    upload_root = moments_uploads_root(data_dir, wx_id, create=False)
    try:
        root = upload_root.resolve()
    except OSError:
        return 0
    removed = 0
    for image in images or []:
        raw_path = str(image or "").strip()
        if not raw_path:
            continue
        path = Path(raw_path)
        if not path.is_absolute():
            path = Path(str(data_dir or "")) / path
        try:
            resolved = path.resolve()
            if not resolved.is_relative_to(root):
                continue
        except OSError:
            continue
        try:
            if resolved.is_file():
                resolved.unlink()
                removed += 1
        except OSError:
            continue
        parent = resolved.parent
        while parent != root and parent.is_relative_to(root):
            try:
                parent.rmdir()
            except OSError:
                break
            parent = parent.parent
    return removed


def draft_has_material(draft):
    return bool((draft or {}).get("texts") or (draft or {}).get("images"))


def render_preview_reply(draft):
    candidates = [
        str(item or "").strip()
        for item in ((draft or {}).get("generated_candidates") or [])
        if str(item or "").strip()
    ]
    image_count = len((draft or {}).get("images") or [])
    privacy_text = "公开"
    lines = ["朋友圈预览", ""]
    for idx, candidate in enumerate(candidates[:3], start=1):
        lines.append(f"{idx}. {candidate}")
    lines.extend([
        "",
        f"图片：{image_count} 张",
        f"当前可见范围：{privacy_text}",
        "",
        "请回复 1/2/3 选择文案",
    ])
    return "\n".join(lines).strip()


def new_moments_task_id(prefix="moment_task"):
    return f"{prefix}_{int(datetime.now().timestamp() * 1000)}_{secrets.token_hex(4)}"


def clean_moments_string_list(value, *, limit=None):
    if not isinstance(value, list):
        return []
    result = []
    for item in value:
        text = str(item or "").strip()
        if text:
            result.append(text)
        if limit and len(result) >= limit:
            break
    return result


def parse_moments_candidates(raw_reply, *, cleaner=None):
    text = str(raw_reply or "")
    if cleaner is not None:
        text = cleaner(text)
    text = text.strip()
    if not text:
        raise ValueError("朋友圈接口返回为空")
    code_match = re.search(r"```(?:json)?\s*(.*?)```", text, re.IGNORECASE | re.DOTALL)
    if code_match:
        text = code_match.group(1).strip()
    json_candidates = [text]
    bracket_match = re.search(r"(\[[\s\S]*\])", text)
    if bracket_match:
        json_candidates.insert(0, bracket_match.group(1).strip())
    for payload in json_candidates:
        try:
            data = json.loads(payload)
        except Exception:
            continue
        if isinstance(data, list):
            candidates = [
                str(item or "").strip()
                for item in data
                if str(item or "").strip()
            ]
            if len(candidates) >= 3:
                return candidates[:3]
    lines = [line.strip(" \t\r\n-0123456789.、") for line in text.splitlines()]
    candidates = [line for line in lines if line]
    if len(candidates) >= 3:
        return candidates[:3]
    raise ValueError("朋友圈接口未返回合法的 3 条文案")


def default_moments_publish_time(*, now=None):
    base = now or datetime.now()
    return (base + timedelta(minutes=10)).strftime("%Y-%m-%dT%H:%M")


def latest_moments_random_window(tasks, *, default=DEFAULT_MOMENTS_RANDOM_WINDOW):
    for task in reversed(tasks or []):
        if not isinstance(task, dict):
            continue
        if str(task.get("publish_rule") or "").strip() != "random":
            continue
        window = str(task.get("publish_window") or "").strip()
        if window:
            return window
    return default


def normalize_moments_task(task, *, now=None):
    raw = task if isinstance(task, dict) else {}
    now_text = (now or datetime.now()).strftime("%Y-%m-%d %H:%M:%S")

    status = str(raw.get("status") or STATUS_PENDING_CONFIRM).strip()
    if status not in MOMENTS_TASK_STATUS_VALUES:
        status = STATUS_PENDING_CONFIRM

    copy_mode = str(raw.get("copy_mode") or "ai").strip()
    if copy_mode not in ("ai", "original"):
        copy_mode = "ai"

    publish_rule = str(raw.get("publish_rule") or "random").strip()
    if publish_rule not in ("random", "fixed"):
        publish_rule = "random"

    visibility_type = str(raw.get("visibility_type") or "all").strip()
    if visibility_type not in ("all", "include", "exclude"):
        visibility_type = "all"

    raw_text = str(raw.get("raw_text") or "").strip()
    images = clean_moments_string_list(raw.get("images"), limit=9)
    source = str(raw.get("source") or "web_panel").strip()
    file_storage_mode = str(raw.get("file_storage_mode") or "").strip()
    if file_storage_mode not in ("direct", "managed"):
        file_storage_mode = "managed" if source == "admin_command" else "direct"

    selected_caption = str(raw.get("selected_caption") or raw_text or "无文案").strip() or "无文案"
    ai_generation_status = str(raw.get("ai_generation_status") or "").strip().lower()
    if ai_generation_status not in ("idle", "pending", "done", "failed"):
        ai_generation_status = "done" if clean_moments_string_list(raw.get("candidates"), limit=3) else "idle"

    return {
        "id": str(raw.get("id") or new_moments_task_id()).strip(),
        "source": source,
        "file_storage_mode": file_storage_mode,
        "enabled": bool(raw.get("enabled", True)),
        "status": status,
        "raw_text": raw_text,
        "images": images,
        "copy_mode": copy_mode,
        "publish_rule": publish_rule,
        "publish_time": str(raw.get("publish_time") or "").strip(),
        "publish_window": str(raw.get("publish_window") or "12:00 - 21:30").strip(),
        "visibility_type": visibility_type,
        "tags": clean_moments_string_list(raw.get("tags")),
        "candidates": clean_moments_string_list(raw.get("candidates"), limit=3),
        "selected_caption": selected_caption,
        "ai_generation_status": ai_generation_status,
        "ai_generation_error": str(raw.get("ai_generation_error") or "").strip(),
        "execute_after": str(raw.get("execute_after") or "").strip(),
        "queued_at": str(raw.get("queued_at") or "").strip(),
        "queued_mode": str(raw.get("queued_mode") or "").strip(),
        "executed_at": str(raw.get("executed_at") or "").strip(),
        "execution_result": str(raw.get("execution_result") or "").strip(),
        "execution_message": str(raw.get("execution_message") or "").strip(),
        "execution_snapshot": _normalize_execution_snapshot(raw.get("execution_snapshot")),
        "created_at": str(raw.get("created_at") or now_text).strip(),
        "updated_at": str(raw.get("updated_at") or now_text).strip(),
    }


def split_moments_task_storage(task, *, now=None):
    normalized = normalize_moments_task(task, now=now)
    definition = {field: normalized.get(field) for field in MOMENTS_TASK_DEFINITION_FIELDS}
    runtime = {field: normalized.get(field) for field in MOMENTS_TASK_RUNTIME_FIELDS}
    history = {field: normalized.get(field) for field in MOMENTS_TASK_HISTORY_FIELDS}
    return definition, runtime, history


def merge_moments_task_storage(definition, runtime=None, history=None, *, now=None):
    payload = {}
    if isinstance(definition, dict):
        payload.update(definition)
    if isinstance(runtime, dict):
        payload.update(runtime)
    if isinstance(history, dict):
        payload.update(history)
    return normalize_moments_task(payload, now=now)


def serialize_moments_task_collection(tasks, *, now=None):
    definitions = []
    runtime_map = {}
    history_map = {}
    for task in tasks or []:
        if not isinstance(task, dict):
            continue
        definition, runtime, history = split_moments_task_storage(task, now=now)
        task_id = str(definition.get("id") or "").strip()
        if not task_id:
            continue
        definitions.append(definition)
        runtime_map[task_id] = runtime
        history_map[task_id] = history
    return definitions, runtime_map, history_map


def deserialize_moments_task_collection(definitions, runtime_map=None, history_map=None, *, now=None):
    runtime_map = runtime_map if isinstance(runtime_map, dict) else {}
    history_map = history_map if isinstance(history_map, dict) else {}
    tasks = []
    for definition in definitions or []:
        if not isinstance(definition, dict):
            continue
        task_id = str(definition.get("id") or "").strip()
        tasks.append(
            merge_moments_task_storage(
                definition,
                runtime_map.get(task_id, {}),
                history_map.get(task_id, {}),
                now=now,
            )
        )
    return tasks


def moments_task_counts(tasks):
    counts = {
        STATUS_PENDING_CONFIRM: 0,
        STATUS_PENDING: 0,
        STATUS_RUNNING: 0,
        STATUS_EXECUTED: 0,
    }
    for task in tasks or []:
        status = (task or {}).get("status")
        if status in counts:
            counts[status] += 1
    return counts


def moments_task_publish_text(task):
    task = task if isinstance(task, dict) else {}
    if str(task.get("copy_mode") or "").strip() == "original":
        text = str(task.get("raw_text") or "").strip()
    else:
        candidates = clean_moments_string_list(task.get("candidates"), limit=3)
        selected = str(task.get("selected_caption") or "").strip()
        if candidates:
            text = selected if selected in candidates else candidates[0]
        else:
            text = selected or str(task.get("raw_text") or "").strip()
    return "" if text == "无文案" else text


def moments_task_has_ai_candidates(task):
    task = task if isinstance(task, dict) else {}
    if str(task.get("copy_mode") or "").strip() != "ai":
        return True
    return bool(clean_moments_string_list(task.get("candidates"), limit=3))


def moments_task_from_admin_draft(draft, *, candidate_index=1, now=None):
    candidates = clean_moments_string_list((draft or {}).get("generated_candidates"), limit=3)
    try:
        candidate_index = int(candidate_index or 1)
    except (TypeError, ValueError):
        candidate_index = 1
    if candidate_index < 1 or candidate_index > min(3, len(candidates)):
        raise ValueError("invalid_candidate_index")
    raw_text = "\n".join(
        str(item or "").strip()
        for item in (draft or {}).get("texts", [])
        if str(item or "").strip()
    ).strip()
    selected_caption = candidates[candidate_index - 1]
    return normalize_moments_task(
        {
            "id": new_moments_task_id("admin_moment_task"),
            "source": str((draft or {}).get("source") or "admin_command"),
            "file_storage_mode": "managed",
            "status": STATUS_PENDING_CONFIRM,
            "raw_text": raw_text,
            "images": clean_moments_string_list((draft or {}).get("images"), limit=9),
            "copy_mode": "ai",
            "publish_rule": "fixed",
            "visibility_type": "all",
            "tags": [],
            "candidates": candidates,
            "selected_caption": selected_caption,
        },
        now=now,
    )


def parse_moments_datetime(value):
    text = str(value or "").strip()
    if not text:
        return None
    if len(text) == 16 and "T" in text:
        text = f"{text}:00"
    try:
        return datetime.fromisoformat(text)
    except ValueError:
        return None


def _parse_hhmm(value, default):
    text = str(value or default).strip() or default
    match = re.search(r"(\d{1,2}):(\d{1,2})", text)
    if not match:
        match = re.search(r"(\d{1,2}):(\d{1,2})", default)
    hour = max(0, min(23, int(match.group(1))))
    minute = max(0, min(59, int(match.group(2))))
    return hour, minute


def resolve_moments_execute_after(task, *, mode="queue", now=None):
    now = now or datetime.now()
    if mode == "immediate":
        return (now + timedelta(seconds=45)).replace(microsecond=0).isoformat()

    if (task or {}).get("publish_rule") == "fixed":
        parsed = parse_moments_datetime((task or {}).get("publish_time"))
        if parsed is None:
            parsed = now + timedelta(minutes=10)
        return parsed.replace(microsecond=0).isoformat()

    window = str((task or {}).get("publish_window") or "12:00 - 21:30")
    parts = re.findall(r"\d{1,2}:\d{1,2}", window)
    start_hour, start_minute = _parse_hhmm(parts[0] if parts else "", "12:00")
    end_hour, end_minute = _parse_hhmm(parts[1] if len(parts) > 1 else "", "21:30")
    start = now.replace(hour=start_hour, minute=start_minute, second=0, microsecond=0)
    end = now.replace(hour=end_hour, minute=end_minute, second=0, microsecond=0)
    if end <= start:
        end = end + timedelta(days=1)
    total_seconds = max(0, int((end - start).total_seconds()))
    offset = secrets.randbelow(total_seconds + 1) if total_seconds else 0
    planned = start + timedelta(seconds=offset)
    if planned < now:
        planned = now + timedelta(minutes=10)
    return planned.replace(microsecond=0).isoformat()


def queue_moments_task(task, *, mode="queue", now=None):
    now = now or datetime.now()
    mode = mode if mode in ("queue", "immediate") else "queue"
    execute_after = resolve_moments_execute_after(task, mode=mode, now=now)
    return normalize_moments_task(
        {
            **(task or {}),
            "status": STATUS_PENDING,
            "execute_after": execute_after,
            "queued_at": now.replace(microsecond=0).isoformat(),
            "queued_mode": mode,
            "executed_at": "",
            "execution_result": "",
            "execution_message": "",
            "updated_at": now.strftime("%Y-%m-%d %H:%M:%S"),
        },
        now=now,
    )


def mark_moments_task_running(task, *, now=None):
    now = now or datetime.now()
    return normalize_moments_task(
        {
            **(task or {}),
            "status": STATUS_RUNNING,
            "updated_at": now.strftime("%Y-%m-%d %H:%M:%S"),
        },
        now=now,
    )


def recover_interrupted_moments_task(task, *, now=None):
    now = now or datetime.now()
    normalized = normalize_moments_task(task, now=now)
    if normalized.get("status") != STATUS_RUNNING:
        return normalized
    normalized.update({
        "status": STATUS_PENDING_CONFIRM,
        "enabled": True,
        "execute_after": "",
        "queued_at": "",
        "queued_mode": "",
        "executed_at": now.replace(microsecond=0).isoformat(),
        "execution_result": "uncertain",
        "execution_message": "发布过程被异常中断，结果待核实，不会自动重发",
        "updated_at": now.strftime("%Y-%m-%d %H:%M:%S"),
    })
    snapshot = normalized.get("execution_snapshot")
    if isinstance(snapshot, dict):
        normalized["execution_snapshot"] = {
            **snapshot,
            "result_summary": normalized["execution_message"],
        }
    return normalized


def cancel_queued_moments_task(task, *, now=None):
    now = now or datetime.now()
    return normalize_moments_task(
        {
            **(task or {}),
            "status": STATUS_PENDING_CONFIRM,
            "execute_after": "",
            "queued_at": "",
            "queued_mode": "",
            "executed_at": "",
            "execution_result": "",
            "execution_message": "",
            "updated_at": now.strftime("%Y-%m-%d %H:%M:%S"),
        },
        now=now,
    )


def moments_visibility_to_privacy(visibility_type):
    visibility_type = str(visibility_type or "all").strip()
    if visibility_type == "include":
        return "whitelist"
    if visibility_type == "exclude":
        return "blacklist"
    return "public"
