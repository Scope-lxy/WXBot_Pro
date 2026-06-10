"""Configuration helpers shared by runtime and web UI."""

KNOWN_CAPABILITIES = ("vision", "audio")


def api_supports_capability(capability_map, index, capability) -> bool:
    """Return whether the configured API index declares a capability."""
    if not isinstance(capability_map, dict):
        return False
    item = capability_map.get(str(index), capability_map.get(index, {}))
    if not isinstance(item, dict):
        return False
    return bool(item.get(capability, False))


def sanitize_api_capability_map(capability_map):
    """Keep only valid API indexes and known boolean capability flags."""
    if not isinstance(capability_map, dict):
        return {}
    clean = {}
    for key, value in capability_map.items():
        try:
            clean_key = str(int(key))
        except (ValueError, TypeError):
            continue
        if not isinstance(value, dict):
            continue
        item = {}
        for capability in KNOWN_CAPABILITIES:
            if capability in value:
                item[capability] = bool(value.get(capability))
        if item:
            clean[clean_key] = item
    return clean


def set_api_capability(config, index, capability, supported):
    """Set an API capability flag on a config dict in-place and return it."""
    config = config if isinstance(config, dict) else {}
    try:
        key = str(max(0, int(index)))
    except (TypeError, ValueError):
        key = "0"
    capability = str(capability or "").strip()
    if not capability:
        return config
    capability_map = config.get("api_capability_map")
    if not isinstance(capability_map, dict):
        capability_map = {}
    item = capability_map.get(key)
    if not isinstance(item, dict):
        item = {}
    item[capability] = bool(supported)
    capability_map[key] = item
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
