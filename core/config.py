"""Configuration helpers shared by runtime and web UI."""

from __future__ import annotations

import uuid


KNOWN_CAPABILITIES = ("vision", "audio")


def new_api_config_id() -> str:
    """Create a stable identity for one saved chat API configuration."""
    return f"api_{uuid.uuid4().hex}"


def api_config_by_id(api_configs, api_id):
    """Return the saved API configuration with this stable ID."""
    api_id = str(api_id or "").strip()
    if not api_id or not isinstance(api_configs, list):
        return None
    for item in api_configs:
        if isinstance(item, dict) and str(item.get("id") or "").strip() == api_id:
            return item
    return None


def validate_api_config_references(config):
    """Return a user-facing error when a saved API reference no longer exists."""
    if not isinstance(config, dict):
        return "接口配置格式无效"
    api_configs = config.get("api_configs")
    known_ids = {
        str(item.get("id") or "").strip()
        for item in api_configs or []
        if isinstance(item, dict) and str(item.get("id") or "").strip()
    }
    if not known_ids:
        return "至少需要保留一个接口配置"

    primary_api_id = str(config.get("api_id") or "").strip()
    if primary_api_id not in known_ids:
        return "当前聊天接口不存在，请重新选择"

    backup_api_id = str(config.get("backup_chat_api_id") or "").strip()
    if backup_api_id and backup_api_id not in known_ids:
        return "备用聊天接口不存在，请重新选择"
    if backup_api_id and backup_api_id == primary_api_id:
        return "备用聊天接口不能与当前聊天接口相同"

    for field, label in (
        ("chat_image_recognition_api_id", "私聊辅助识图接口"),
        ("group_image_recognition_api_id", "群聊辅助识图接口"),
    ):
        api_id = str(config.get(field) or "").strip()
        if api_id not in known_ids:
            return f"{label}不存在，请重新选择"

    for field, label in (("chat_api_map", "私聊专属聊天接口"), ("group_api_map", "群聊专属聊天接口")):
        raw_map = config.get(field, {})
        if not isinstance(raw_map, dict):
            return f"{label}配置格式无效"
        if any(str(api_id or "").strip() not in known_ids for api_id in raw_map.values()):
            return f"{label}存在已删除的接口，请重新选择"
    return ""


def api_supports_capability(capability_map, api_id, capability) -> bool:
    """Return whether the stable API ID declares a capability."""
    if not isinstance(capability_map, dict):
        return False
    item = capability_map.get(str(api_id or "").strip(), {})
    if not isinstance(item, dict):
        return False
    return bool(item.get(capability, False))


def sanitize_api_capability_map(capability_map):
    """Keep only stable API IDs and known boolean capability flags."""
    if not isinstance(capability_map, dict):
        return {}
    clean = {}
    for api_id, value in capability_map.items():
        api_id = str(api_id or "").strip()
        if not api_id or not isinstance(value, dict):
            continue
        item = {}
        for capability in KNOWN_CAPABILITIES:
            if capability in value:
                item[capability] = bool(value.get(capability))
        if item:
            clean[api_id] = item
    return clean


def set_api_capability(config, api_id, capability, supported):
    """Set an API capability flag on a config dict in-place and return it."""
    config = config if isinstance(config, dict) else {}
    api_id = str(api_id or "").strip()
    capability = str(capability or "").strip()
    if not api_id or not capability:
        return config
    capability_map = config.get("api_capability_map")
    if not isinstance(capability_map, dict):
        capability_map = {}
    item = capability_map.get(api_id)
    if not isinstance(item, dict):
        item = {}
    item[capability] = bool(supported)
    capability_map[api_id] = item
    config["api_capability_map"] = capability_map
    return config


def coerce_float_range(value, default, min_value, max_value):
    """Convert a value to float and clamp it into the configured range."""
    try:
        number = float(value)
    except (TypeError, ValueError):
        number = float(default)
    return max(float(min_value), min(float(max_value), number))


def coerce_int_range(value, default, min_value, max_value):
    """Convert a value to int and clamp it into the configured range."""
    try:
        number = int(value)
    except (TypeError, ValueError):
        number = int(default)
    return max(int(min_value), min(int(max_value), number))
