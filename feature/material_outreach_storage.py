"""Shared storage helpers for material-outreach runtime state."""

from __future__ import annotations

import json
from datetime import datetime

from core.account_storage import account_module_file, resolve_account_id
from feature.material_outreach import (
    build_progress_record,
    load_json_list,
    load_json_object,
    normalize_material_outreach_history_payload,
    normalize_material_outreach_runtime_payload,
    normalize_material_record,
    save_json_list,
    save_json_object,
)
from feature.material_outreach_preface import normalize_preface_pending_queue
from feature.task_workbench_storage import file_lock_for_path


class MaterialOutreachStorage:
    def __init__(self, data_dir, wx_id):
        self.data_dir = str(data_dir or "").strip()
        self.wx_id = resolve_account_id(wx_id, fallback_default=True)

    def tasks_file(self, *, create_parent=False):
        return self._module_file("tasks.json", create_parent=create_parent)

    def runtime_file(self, *, create_parent=False):
        return self._module_file("runtime.json", create_parent=create_parent)

    def history_file(self, *, create_parent=False):
        return self._module_file("history.json", create_parent=create_parent)

    def materials_file(self, *, create_parent=False):
        return self._module_file("materials.json", create_parent=create_parent)

    def load_runtime(self):
        runtime_file = self.runtime_file()
        if not runtime_file:
            return normalize_material_outreach_runtime_payload({})
        return normalize_material_outreach_runtime_payload(load_json_object(runtime_file))

    def save_runtime(self, payload):
        runtime_file = self.runtime_file(create_parent=True)
        normalized = normalize_material_outreach_runtime_payload(payload)
        if not runtime_file:
            return normalize_material_outreach_runtime_payload({})
        self._save_json_object_atomic(runtime_file, normalized)
        return self._canonical_copy(normalized)

    def mutate_runtime(self, mutator):
        runtime_file = self.runtime_file(create_parent=True)
        if not runtime_file:
            return normalize_material_outreach_runtime_payload({})
        return self._mutate_json_object_atomic(
            runtime_file,
            normalize_material_outreach_runtime_payload,
            mutator,
        )

    def load_history(self):
        history_file = self.history_file()
        if not history_file:
            return normalize_material_outreach_history_payload({})
        return normalize_material_outreach_history_payload(load_json_object(history_file))

    def save_history(self, payload):
        history_file = self.history_file(create_parent=True)
        normalized = normalize_material_outreach_history_payload(payload)
        if not history_file:
            return normalize_material_outreach_history_payload({})
        self._save_json_object_atomic(history_file, normalized)
        return self._canonical_copy(normalized)

    def mutate_history(self, mutator):
        history_file = self.history_file(create_parent=True)
        if not history_file:
            return normalize_material_outreach_history_payload({})
        return self._mutate_json_object_atomic(
            history_file,
            normalize_material_outreach_history_payload,
            mutator,
        )

    def load_materials(self):
        materials_file = self.materials_file()
        if not materials_file:
            return []
        return load_json_list(materials_file)

    def save_materials(self, materials):
        materials_file = self.materials_file(create_parent=True)
        normalized = [normalize_material_record(item) for item in (materials or []) if isinstance(item, dict)]
        normalized = [item for item in normalized if item]
        if not materials_file:
            return []
        save_json_list(materials_file, normalized)
        return normalized

    def load_send_records(self):
        return self.load_history().get("send_records", [])

    def save_send_records(self, records):
        history = self.mutate_history(
            lambda payload: {
                **(payload if isinstance(payload, dict) else {}),
                "send_records": [item for item in (records or []) if isinstance(item, dict)],
            }
        )
        return history["send_records"]

    def append_send_record(self, record, *, limit=1000):
        history = self.mutate_history(
            lambda payload: self._append_records(payload, "send_records", [record], limit=limit)
        )
        return history["send_records"]

    def load_skip_records(self):
        return self.load_history().get("skip_records", [])

    def save_skip_records(self, records):
        history = self.mutate_history(
            lambda payload: {
                **(payload if isinstance(payload, dict) else {}),
                "skip_records": [item for item in (records or []) if isinstance(item, dict)],
            }
        )
        return history["skip_records"]

    def append_skip_record(self, record, *, limit=1000):
        history = self.mutate_history(
            lambda payload: self._append_records(payload, "skip_records", [record], limit=limit)
        )
        return history["skip_records"]

    def load_progress_records(self):
        return self.load_history().get("progress_records", [])

    def save_progress_records(self, records):
        history = self.mutate_history(
            lambda payload: {
                **(payload if isinstance(payload, dict) else {}),
                "progress_records": [item for item in (records or []) if isinstance(item, dict)],
            }
        )
        return history["progress_records"]

    def append_progress_records(self, records, *, limit=1000):
        history = self.mutate_history(
            lambda payload: self._append_records(payload, "progress_records", records, limit=limit)
        )
        return history["progress_records"]

    def update_progress_records_for_send(self, snapshot, targets, *, success=False, status="", error="", now=None, limit=1000):
        by_name = {}
        for contact in (snapshot or {}).get("targets") or []:
            send_name = str((contact or {}).get("send_name") or "").strip()
            if send_name and send_name not in by_name:
                by_name[send_name] = contact
        status = str(status or "").strip() or ("success" if success else "failed")
        records = []
        for target in targets or []:
            send_name = str(target or "").strip()
            if not send_name:
                continue
            contact = by_name.get(send_name) or {
                "contact_key": "",
                "send_name": send_name,
                "display_name": send_name,
                "warnings": [],
            }
            records.append(
                build_progress_record(
                    (snapshot or {}).get("run_id"),
                    (snapshot or {}).get("task_id"),
                    contact,
                    status,
                    detail=error,
                    now=now,
                )
            )
        return self.append_progress_records(records, limit=limit)

    def freeze_interrupted_sends(self, task_id, *, now=None, limit=1000):
        """Turn latest inflight deliveries into uncertain and report unresolved sends."""
        task_id = str(task_id or "").strip()
        if not task_id:
            return []
        stamp = (now or datetime.now()).replace(microsecond=0).isoformat()

        def mutate(payload):
            items = [item for item in (payload.get("progress_records") or []) if isinstance(item, dict)]
            latest = {}
            for item in items:
                if str(item.get("task_id") or "").strip() != task_id:
                    continue
                key = (
                    str(item.get("run_id") or "").strip(),
                    str(item.get("contact_key") or item.get("send_name") or "").strip(),
                )
                latest[key] = item
            recovered = []
            for item in latest.values():
                if str(item.get("status") or "").strip() != "inflight":
                    continue
                frozen = {
                    **item,
                    "status": "uncertain",
                    "status_label": "待人工确认",
                    "detail": "上次运行在微信提交后中断，已禁止自动重发",
                    "created_at": stamp,
                }
                items.append(frozen)
                recovered.append(frozen)
            if limit and len(items) > int(limit):
                items = items[-int(limit):]
            payload["progress_records"] = items
            payload["_recovered_unresolved"] = recovered
            return payload

        history = self.mutate_history(mutate)
        recovered = list(history.pop("_recovered_unresolved", []) or [])
        latest = {}
        for item in history.get("progress_records", []) or []:
            if str(item.get("task_id") or "").strip() != task_id:
                continue
            key = (
                str(item.get("run_id") or "").strip(),
                str(item.get("contact_key") or item.get("send_name") or "").strip(),
            )
            latest[key] = item
        return recovered or [item for item in latest.values() if str(item.get("status") or "").strip() == "uncertain"]

    def freeze_all_interrupted_sends(self, *, now=None, limit=1000):
        """Freeze every latest inflight delivery when an account namespace loads."""
        task_ids = {
            str(item.get("task_id") or "").strip()
            for item in self.load_progress_records()
            if isinstance(item, dict)
            and str(item.get("status") or "").strip() == "inflight"
            and str(item.get("task_id") or "").strip()
        }
        recovered = []
        for task_id in sorted(task_ids):
            recovered.extend(self.freeze_interrupted_sends(task_id, now=now, limit=limit))
        return recovered

    def load_ai_pending_queue(self):
        return self.load_runtime().get("ai_pending_queue", [])

    def save_ai_pending_queue(self, records):
        runtime = self.mutate_runtime(
            lambda payload: {
                **(payload if isinstance(payload, dict) else {}),
                "ai_pending_queue": [item for item in (records or []) if isinstance(item, dict)],
            }
        )
        return runtime["ai_pending_queue"]

    def load_preface_queue(self):
        return normalize_preface_pending_queue(self.load_runtime().get("preface_pending_queue", []))

    def save_preface_queue(self, records):
        runtime = self.mutate_runtime(
            lambda payload: {
                **(payload if isinstance(payload, dict) else {}),
                "preface_pending_queue": normalize_preface_pending_queue(records),
            }
        )
        return runtime["preface_pending_queue"]

    def _module_file(self, filename, *, create_parent=False):
        if not self.data_dir or not self.wx_id:
            return None
        return account_module_file(
            self.data_dir,
            self.wx_id,
            "material_outreach",
            filename,
            create_parent=create_parent,
        )

    def _save_json_object_atomic(self, path, payload):
        with file_lock_for_path(path):
            save_json_object(path, self._canonical_copy(payload if isinstance(payload, dict) else {}))

    def _mutate_json_object_atomic(self, path, normalizer, mutator):
        with file_lock_for_path(path):
            current = normalizer(load_json_object(path))
            working = self._canonical_copy(current)
            result = mutator(working)
            next_payload = result if isinstance(result, dict) else working
            normalized = normalizer(next_payload)
            save_json_object(path, normalized)
            return self._canonical_copy(normalized)

    def _append_records(self, payload, key, records, *, limit=1000):
        next_payload = payload if isinstance(payload, dict) else {}
        items = [item for item in (next_payload.get(key) or []) if isinstance(item, dict)]
        for record in records or []:
            if isinstance(record, dict):
                items.append(record)
        if limit and len(items) > limit:
            items = items[-int(limit):]
        next_payload[key] = items
        return next_payload

    def _canonical_copy(self, value):
        return json.loads(json.dumps(value, ensure_ascii=False, indent=2))
