"""Account-scoped storage path helpers."""

from __future__ import annotations

import shutil
from pathlib import Path

DEFAULT_ACCOUNT_ID = "default"


def normalize_account_id(wx_id) -> str:
    return str(wx_id or "").strip()


def resolve_account_id(wx_id, *, fallback_default: bool = False) -> str:
    account_id = normalize_account_id(wx_id)
    if account_id:
        return account_id
    return DEFAULT_ACCOUNT_ID if fallback_default else ""


def accounts_root(base_dir: str | Path) -> Path:
    return Path(base_dir) / "accounts"


def account_dir(
    base_dir: str | Path,
    wx_id,
    *,
    create: bool = False,
    fallback_default: bool = False,
) -> Path:
    account_id = resolve_account_id(wx_id, fallback_default=fallback_default)
    if not account_id:
        raise ValueError("wx_id is required")
    path = accounts_root(base_dir) / account_id
    if create:
        path.mkdir(parents=True, exist_ok=True)
    return path


def account_subdir(
    base_dir: str | Path,
    wx_id,
    *parts: str,
    create: bool = False,
    fallback_default: bool = False,
) -> Path:
    path = account_dir(base_dir, wx_id, create=create, fallback_default=fallback_default)
    for part in parts:
        cleaned = str(part or "").strip()
        if cleaned:
            path = path / cleaned
    if create:
        path.mkdir(parents=True, exist_ok=True)
    return path


def account_area_dir(
    base_dir: str | Path,
    wx_id,
    area: str,
    *,
    create: bool = False,
    fallback_default: bool = False,
) -> Path:
    return account_subdir(base_dir, wx_id, area, create=create, fallback_default=fallback_default)


def account_file(
    base_dir: str | Path,
    wx_id,
    *parts: str,
    create_parent: bool = False,
    fallback_default: bool = False,
) -> Path:
    if not parts:
        raise ValueError("file path parts are required")
    parent_parts = parts[:-1]
    filename = str(parts[-1] or "").strip()
    if not filename:
        raise ValueError("filename is required")
    parent = account_subdir(
        base_dir,
        wx_id,
        *parent_parts,
        create=create_parent,
        fallback_default=fallback_default,
    )
    return parent / filename


def account_area_file(
    base_dir: str | Path,
    wx_id,
    area: str,
    filename: str,
    *parts: str,
    create_parent: bool = False,
    fallback_default: bool = False,
) -> Path:
    return account_file(
        base_dir,
        wx_id,
        area,
        *parts,
        filename,
        create_parent=create_parent,
        fallback_default=fallback_default,
    )


def account_module_dir(
    base_dir: str | Path,
    wx_id,
    module: str,
    *,
    create: bool = False,
    fallback_default: bool = False,
) -> Path:
    return account_subdir(base_dir, wx_id, "tasks", module, create=create, fallback_default=fallback_default)


def account_module_file(
    base_dir: str | Path,
    wx_id,
    module: str,
    filename: str,
    *,
    create_parent: bool = False,
    fallback_default: bool = False,
) -> Path:
    return account_file(
        base_dir,
        wx_id,
        "tasks",
        module,
        filename,
        create_parent=create_parent,
        fallback_default=fallback_default,
    )


def ensure_default_account(base_dir: str | Path) -> Path:
    return account_dir(base_dir, DEFAULT_ACCOUNT_ID, create=True)


def migrate_default_account(base_dir: str | Path, wx_id) -> bool:
    target_id = normalize_account_id(wx_id)
    if not target_id or target_id == DEFAULT_ACCOUNT_ID:
        return False
    root = accounts_root(base_dir)
    source = root / DEFAULT_ACCOUNT_ID
    if not source.exists() or not source.is_dir():
        return False
    try:
        has_entries = any(source.iterdir())
    except Exception:
        has_entries = False
    if not has_entries:
        return False
    target = root / target_id
    if target.exists():
        return False
    root.mkdir(parents=True, exist_ok=True)
    shutil.move(str(source), str(target))
    return True


def discover_account_ids(base_dir: str | Path) -> list[str]:
    root = accounts_root(base_dir)
    if not root.exists():
        return []
    items = []
    for child in root.iterdir():
        if child.is_dir():
            account_id = normalize_account_id(child.name)
            if account_id:
                items.append(account_id)
    return sorted(set(items))


def discover_populated_account_ids(base_dir: str | Path) -> list[str]:
    root = accounts_root(base_dir)
    if not root.exists():
        return []
    items = []
    for child in root.iterdir():
        if not child.is_dir():
            continue
        account_id = normalize_account_id(child.name)
        if not account_id:
            continue
        try:
            has_files = any(path.is_file() for path in child.rglob("*"))
        except Exception:
            has_files = False
        if has_files:
            items.append(account_id)
    return sorted(set(items))


def known_account_ids(running_wx_id="", last_wx_id="", existing_ids=None) -> list[str]:
    running = normalize_account_id(running_wx_id)
    last = normalize_account_id(last_wx_id)
    existing = [normalize_account_id(item) for item in (existing_ids or [])]
    existing = sorted(set(item for item in existing if item))

    ordered = []
    if running:
        ordered.append(running)
    if last:
        ordered.append(last)
    ordered.extend(existing)

    result = []
    seen = set()
    for item in ordered:
        if item and item not in seen:
            seen.add(item)
            result.append(item)
    return result


def is_known_account_id(wx_id, running_wx_id="", last_wx_id="", existing_ids=None) -> bool:
    account_id = normalize_account_id(wx_id)
    if not account_id:
        return False
    known = set(known_account_ids(running_wx_id, last_wx_id, existing_ids))
    if account_id in known:
        return True
    return account_id == DEFAULT_ACCOUNT_ID and not known


def preferred_account_id(running_wx_id="", last_wx_id="", existing_ids=None) -> str:
    running = normalize_account_id(running_wx_id)
    if running:
        return running
    last = normalize_account_id(last_wx_id)
    if last:
        return last
    existing = known_account_ids("", "", existing_ids)
    non_default = [item for item in existing if item != DEFAULT_ACCOUNT_ID]
    if non_default:
        return non_default[0]
    return DEFAULT_ACCOUNT_ID
