"""One-time migration from legacy per-chat JSON history to MessageStore."""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.account_storage import account_area_dir
from core.memory import MemoryManager
from core.memory_context_repair import unique_message_key


def _read_json(path: Path):
    return json.loads(path.read_text(encoding="utf-8-sig"))


def _legacy_chat_name(chat_dir: Path) -> str:
    name_path = chat_dir / "name.json"
    if name_path.is_file():
        try:
            payload = _read_json(name_path)
            name = str(payload.get("name", "") if isinstance(payload, dict) else "").strip()
            if name:
                return name
        except Exception:
            pass
    return chat_dir.name


def _legacy_messages(chat_dir: Path) -> list[dict]:
    messages = []
    for path in sorted(chat_dir.glob("*_memory.json")):
        payload = _read_json(path)
        if not isinstance(payload, list):
            raise ValueError(f"legacy history is not a list: {path}")
        messages.extend(item for item in payload if isinstance(item, dict))
    return messages


def _configured_group_names(data_dir: Path) -> set[str]:
    config_path = data_dir / "config" / "config.json"
    if not config_path.is_file():
        return set()
    try:
        payload = _read_json(config_path)
    except Exception:
        return set()
    groups = payload.get("group") if isinstance(payload, dict) else []
    return {
        str(item or "").strip()
        for item in (groups if isinstance(groups, list) else [])
        if str(item or "").strip()
    }


def _chat_type(manager: MemoryManager, chat_name: str, group_names: set[str]) -> str:
    if chat_name in group_names:
        return "group"
    private_names = set(manager.list_chat_names(chat_type="private"))
    group_history_names = set(manager.list_chat_names(chat_type="group"))
    if chat_name in group_history_names and chat_name not in private_names:
        return "group"
    return "private"


def migrate_account(data_dir, account_id, *, dry_run=False) -> dict:
    data_dir = Path(data_dir).resolve()
    account_id = str(account_id or "").strip()
    if not account_id:
        raise ValueError("account_id is required")
    legacy_root = account_area_dir(data_dir, account_id, "memory")
    manager = MemoryManager(account_id, data_dir)
    group_names = _configured_group_names(data_dir)
    result = {
        "account_id": account_id,
        "legacy_root": str(legacy_root),
        "conversations": 0,
        "legacy_rows": 0,
        "eligible_rows": 0,
        "added_rows": 0,
        "existing_rows": 0,
        "invalid_rows": 0,
        "verified": True,
        "dry_run": bool(dry_run),
    }
    if not legacy_root.is_dir():
        return result

    for chat_dir in sorted(path for path in legacy_root.iterdir() if path.is_dir()):
        legacy = _legacy_messages(chat_dir)
        if not legacy:
            continue
        chat_name = _legacy_chat_name(chat_dir)
        chat_type = _chat_type(manager, chat_name, group_names)
        existing = manager.get_messages(chat_name, sys.maxsize, chat_type=chat_type)
        existing_counts = Counter(
            key for item in existing if (key := unique_message_key(item))
        )
        remaining_existing = existing_counts.copy()
        to_append = []
        eligible_counts = Counter()
        for source_index, entry in enumerate(legacy):
            normalized = manager._normalize_entry(entry)
            key = unique_message_key(normalized)
            if not key:
                result["invalid_rows"] += 1
                continue
            result["eligible_rows"] += 1
            eligible_counts[key] += 1
            if remaining_existing[key] > 0:
                remaining_existing[key] -= 1
                result["existing_rows"] += 1
                continue
            normalized["source"] = "legacy_json_migration"
            event_identity = json.dumps(
                [account_id, chat_type, chat_name, source_index, key],
                ensure_ascii=False,
                separators=(",", ":"),
            )
            to_append.append(manager._store_entry(
                chat_name,
                normalized,
                chat_type=chat_type,
                event_id=manager._event_id("legacy_json", event_identity),
            ))

        result["conversations"] += 1
        result["legacy_rows"] += len(legacy)
        if dry_run:
            result["added_rows"] += len(to_append)
            continue
        result["added_rows"] += manager.message_store.append_history(to_append)
        migrated = manager.get_messages(chat_name, sys.maxsize, chat_type=chat_type)
        migrated_counts = Counter(
            key for item in migrated if (key := unique_message_key(item))
        )
        if any(migrated_counts[key] < count for key, count in eligible_counts.items()):
            result["verified"] = False
            raise RuntimeError(f"legacy history verification failed: {chat_name}")
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description="Migrate legacy JSON chat history into SQLite once.")
    parser.add_argument("--data-dir", default="data")
    parser.add_argument("--account", required=True)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    result = migrate_account(args.data_dir, args.account, dry_run=args.dry_run)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["verified"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
