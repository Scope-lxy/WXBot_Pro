#!/usr/bin/env python3
"""Migrate readable chat storage onto the identity-index mechanism."""

from __future__ import annotations

import argparse
import json
import shutil
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.account_storage import account_area_dir
from core.contact_profiles import directory_path as contact_directory_path, load_directory
from core.identity_index import (
    default_index,
    list_conversation_memory_names,
    list_memory_chat_names,
    reconcile_storage_names,
    save_index,
    update_index_from_directory,
)


def _json_dump(path: Path, payload) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def _backup_path(data_dir: Path, wx_id: str) -> Path:
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    return data_dir / "backups" / f"identity_migration_{wx_id}_{stamp}"


def _copy_if_exists(src: Path, dst: Path) -> bool:
    if not src.exists():
        return False
    if src.is_dir():
        shutil.copytree(src, dst)
    else:
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dst)
    return True


def build_report(data_dir: Path, wx_id: str) -> dict:
    contact_file = contact_directory_path(data_dir, wx_id)
    directory = load_directory(contact_file, wx_id=wx_id)
    index, actions = update_index_from_directory(default_index(wx_id), directory, wx_id=wx_id)
    memory_names = list_memory_chat_names(data_dir, wx_id)
    conversation_names = list_conversation_memory_names(data_dir, wx_id)
    current_names = {
        str(item.get("current_chat_name") or "").strip()
        for item in index.get("identities", [])
        if str(item.get("current_chat_name") or "").strip()
    }
    known_names = set(memory_names) | set(conversation_names)
    matched = sorted(name for name in known_names if name in current_names)
    unmatched = sorted(name for name in known_names if name not in current_names)
    return {
        "wx_id": wx_id,
        "contact_file": str(contact_file),
        "identity_count": len(index.get("identities") or []),
        "memory_count": len(memory_names),
        "conversation_memory_count": len(conversation_names),
        "matched_names": matched,
        "unmatched_names": unmatched,
        "actions": actions,
        "index": index,
    }


def apply_migration(data_dir: Path, wx_id: str, report: dict, *, backup: bool = True) -> dict:
    backup_dir = _backup_path(data_dir, wx_id)
    copied = {}
    if backup:
        copied["memory"] = _copy_if_exists(account_area_dir(data_dir, wx_id, "memory"), backup_dir / "memory")
        copied["conversation_memory"] = _copy_if_exists(account_area_dir(data_dir, wx_id, "conversation_memory"), backup_dir / "conversation_memory")
        copied["contact_profiles"] = _copy_if_exists(account_area_dir(data_dir, wx_id, "contact_profiles"), backup_dir / "contact_profiles")
        config_dir = data_dir / "config"
        copied["config"] = _copy_if_exists(config_dir, backup_dir / "config")

    index = save_index(data_dir, wx_id, report.get("index") or default_index(wx_id))
    manifests = []
    for action in report.get("actions") or []:
        if action.get("type") != "rename":
            continue
        manifest = reconcile_storage_names(
            data_dir,
            wx_id,
            action.get("old_chat_name"),
            action.get("new_chat_name"),
            reason=action.get("reason", "migration"),
            backup_base=backup_dir / "renames",
        )
        manifests.append(manifest)
    result = {
        "wx_id": wx_id,
        "backup_dir": str(backup_dir) if backup else "",
        "backup": copied,
        "identity_count": len(index.get("identities") or []),
        "renamed_count": len(manifests),
        "manifests": manifests,
    }
    if backup:
        _json_dump(backup_dir / "migration_report.json", report)
        _json_dump(backup_dir / "migration_result.json", result)
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description="Migrate WXBot identity storage.")
    parser.add_argument("--wx-id", default="scope_rui", help="Account wx_id namespace")
    parser.add_argument("--data-dir", default=str(ROOT / "data"), help="Project data directory")
    parser.add_argument("--apply", action="store_true", help="Apply migration after dry-run report")
    parser.add_argument("--no-backup", action="store_true", help="Skip backup when applying")
    parser.add_argument("--report", default="", help="Optional report output path")
    args = parser.parse_args()

    data_dir = Path(args.data_dir)
    report = build_report(data_dir, args.wx_id)
    report_path = Path(args.report) if args.report else data_dir / "backups" / f"identity_migration_dry_run_{args.wx_id}.json"
    _json_dump(report_path, {k: v for k, v in report.items() if k != "index"})
    print(json.dumps({
        "status": "dry-run",
        "report": str(report_path),
        "wx_id": report["wx_id"],
        "identity_count": report["identity_count"],
        "memory_count": report["memory_count"],
        "conversation_memory_count": report["conversation_memory_count"],
        "matched_count": len(report["matched_names"]),
        "unmatched_count": len(report["unmatched_names"]),
        "rename_actions": len(report["actions"]),
    }, ensure_ascii=False, indent=2))

    if not args.apply:
        return 0
    result = apply_migration(data_dir, args.wx_id, report, backup=not args.no_backup)
    print(json.dumps({"status": "applied", **result}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
