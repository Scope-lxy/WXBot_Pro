"""Runtime summary helpers for task workbench items."""

from copy import deepcopy
import ntpath
import re


def _clean(value):
    return str(value or "").strip()


def _shorten(value, limit=24):
    text = " ".join(_clean(value).split())
    if not text or limit <= 0 or len(text) <= limit:
        return text
    if limit == 1:
        return text[:1]
    return text[: limit - 1].rstrip() + "…"


def _local_path_basename(value):
    text = _clean(value).strip("\"'")
    if not text:
        return ""
    if not (re.match(r"^[A-Za-z]:[\\/]", text) or re.match(r"^/[^/]", text)):
        return ""
    return ntpath.basename(text.replace("/", "\\"))


def summarize_targets(targets):
    names = []
    for item in targets or []:
        name = _target_name(item)
        if name:
            names.append(name)
    if not names:
        return ""
    if len(names) == 1:
        return names[0]
    if len(names) == 2:
        return "、".join(names[:2])
    return f"{names[0]}、{names[1]} 等 {len(names)} 人"


def summarize_messages(messages):
    previews = []
    for item in messages or []:
        preview = _message_preview(item)
        if preview:
            previews.append(preview)
    if not previews:
        return ""
    if len(previews) == 1:
        return previews[0]
    return f"{previews[0]} 等 {len(previews)} 条消息"


def summarize_media(media):
    items = []
    for index, item in enumerate(media or [], start=1):
        preview, is_image = _media_preview(item, index)
        if preview:
            items.append((preview, is_image))
    if not items:
        return ""
    previews = [preview for preview, _ in items]
    if len(previews) == 1:
        if items[0][1]:
            return f"{previews[0]} 共 1 张"
        return previews[0]
    if all(is_image for _, is_image in items):
        return f"{previews[0]}、{previews[1]} 共 {len(previews)} 张"
    return f"{previews[0]}、{previews[1]} 共 {len(previews)} 个附件"


def summarize_material(material):
    material = material if isinstance(material, dict) else {}
    material_type = _clean(material.get("type"))
    title = _shorten(
        material.get("title")
        or material.get("material_title")
        or material.get("name")
        or material.get("display_name")
        or material.get("summary")
    )
    if material_type and title:
        return f"{material_type}：{title}"
    if title:
        return title
    return material_type


def runtime_snapshot(
    *,
    raw_targets=None,
    raw_messages=None,
    raw_media=None,
    raw_material=None,
    targets_summary="",
    content_summary="",
    media_summary="",
    material_summary="",
    batch_summary="",
    result_summary="",
    batch_id="",
    run_id="",
):
    raw_targets_copy = deepcopy(raw_targets) if raw_targets is not None else []
    raw_messages_copy = deepcopy(raw_messages) if raw_messages is not None else []
    raw_media_copy = deepcopy(raw_media) if raw_media is not None else []
    raw_material_copy = deepcopy(raw_material) if raw_material is not None else {}
    computed_targets_summary = summarize_targets(raw_targets_copy)
    computed_content_summary = summarize_messages(raw_messages_copy)
    computed_media_summary = summarize_media(raw_media_copy)
    computed_material_summary = summarize_material(raw_material_copy)
    return {
        "targets_summary": _summary_override(targets_summary, computed_targets_summary),
        "content_summary": _summary_override(content_summary, computed_content_summary),
        "media_summary": _summary_override(media_summary, computed_media_summary),
        "material_summary": _summary_override(material_summary, computed_material_summary),
        "batch_summary": _clean(batch_summary),
        "result_summary": _clean(result_summary),
        "raw_targets": raw_targets_copy,
        "raw_messages": raw_messages_copy,
        "raw_media": raw_media_copy,
        "raw_material": raw_material_copy,
        "batch_id": _clean(batch_id),
        "run_id": _clean(run_id),
    }


def _target_name(item):
    if isinstance(item, dict):
        return _shorten(
            item.get("display_name")
            or item.get("name")
            or item.get("target")
            or item.get("nickname")
            or item.get("remark")
            or item.get("title")
            or item.get("contact_name")
            or item.get("id")
        )
    return _shorten(item)


def _message_preview(item):
    if isinstance(item, dict):
        return _shorten(
            item.get("content")
            or item.get("text")
            or item.get("message")
            or item.get("body")
            or item.get("title")
            or item.get("name")
            or item.get("summary")
            or item.get("type")
        )
    return _shorten(item)


def _summary_override(value, computed):
    override = _clean(value)
    return override or computed


def _media_preview(item, index):
    if isinstance(item, dict):
        path_value = item.get("path")
        preview = _shorten(
            item.get("name")
            or item.get("title")
            or item.get("file_name")
            or item.get("filename")
            or item.get("media_name")
            or _local_path_basename(path_value)
            or item.get("path")
            or item.get("url")
        )
        if preview:
            return preview, _media_is_image(item)
    else:
        preview = _shorten(item)
        if preview:
            return preview, True
    is_image = _media_is_image(item)
    if is_image:
        return f"图片{index}", True
    return f"附件{index}", False


def _media_is_image(item):
    if not isinstance(item, dict):
        return True
    media_type = _clean(
        item.get("type")
        or item.get("media_type")
        or item.get("kind")
        or item.get("mime_type")
    ).lower()
    if not media_type:
        return True
    image_markers = ("image", "img", "photo", "picture", "pic", "screenshot")
    return any(marker in media_type for marker in image_markers)
