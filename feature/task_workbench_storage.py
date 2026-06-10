"""Task workbench storage adapter."""

from __future__ import annotations

import json
import threading
from copy import deepcopy

from core.account_storage import account_module_file, resolve_account_id
from feature.task_workbench_contract import MODULES


_FILE_LOCKS = {}
_FILE_LOCKS_GUARD = threading.Lock()


def file_lock_for_path(path):
    key = str(path)
    with _FILE_LOCKS_GUARD:
        lock = _FILE_LOCKS.get(key)
        if lock is None:
            lock = threading.RLock()
            _FILE_LOCKS[key] = lock
        return lock


class TaskWorkbenchStorage:
    def __init__(self, data_dir, wx_id, module):
        if module not in MODULES:
            raise ValueError("invalid module: %r" % module)
        self.data_dir = data_dir
        self.wx_id = resolve_account_id(wx_id, fallback_default=True)
        self.module = module

    def module_file(self, filename, create_parent=False):
        return account_module_file(
            self.data_dir,
            self.wx_id,
            self.module,
            filename,
            create_parent=create_parent,
        )

    def load_tasks(self):
        return self._load_json_file("tasks.json", list, [])

    def save_tasks(self, tasks):
        items = [self._canonical_copy(item) for item in list(tasks or []) if isinstance(item, dict)]
        self._save_json_file("tasks.json", items)
        return self._canonical_copy(items)

    def load_runtime(self):
        return self._load_json_file("runtime.json", dict, {})

    def save_runtime(self, runtime):
        payload = self._canonical_copy(runtime) if isinstance(runtime, dict) else {}
        self._save_json_file("runtime.json", payload)
        return self._canonical_copy(payload)

    def mutate_runtime(self, mutator):
        return self._mutate_json_file("runtime.json", dict, {}, mutator)

    def load_history(self):
        return self._load_json_file("history.json", dict, {})

    def save_history(self, history):
        payload = self._canonical_copy(history) if isinstance(history, dict) else {}
        self._save_json_file("history.json", payload)
        return self._canonical_copy(payload)

    def mutate_history(self, mutator):
        return self._mutate_json_file("history.json", dict, {}, mutator)

    def _load_json_file(self, filename, expected_type, default):
        path = self.module_file(filename, create_parent=False)
        if not path.exists():
            return self._canonical_default(default)
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            return self._canonical_default(default)
        if not isinstance(value, expected_type):
            return self._canonical_default(default)
        return value

    def _save_json_file(self, filename, value):
        path = self.module_file(filename, create_parent=True)
        with file_lock_for_path(path):
            self._save_json_file_unlocked(path, value)

    def _save_json_file_unlocked(self, path, value):
        tmp_path = path.with_name(path.name + ".tmp")
        tmp_path.write_text(self._json_text(value), encoding="utf-8")
        tmp_path.replace(path)

    def _mutate_json_file(self, filename, expected_type, default, mutator):
        path = self.module_file(filename, create_parent=True)
        lock = file_lock_for_path(path)
        with lock:
            current = self._load_json_file(filename, expected_type, default)
            working = self._canonical_copy(current)
            result = mutator(working)
            if result is None:
                result = working
            if not isinstance(result, expected_type):
                result = self._canonical_default(default)
            payload = self._canonical_copy(result)
            self._save_json_file_unlocked(path, payload)
            return self._canonical_copy(payload)

    def _json_text(self, value):
        return json.dumps(value, ensure_ascii=False, indent=2)

    def _canonical_copy(self, value):
        return json.loads(self._json_text(value))

    def _canonical_default(self, default):
        return deepcopy(default)
