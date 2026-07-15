import unittest
import json
import os
import subprocess
import tempfile
import threading
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from core.memory import MemoryManager
from core.wechat_ui_runtime import WeChatUIRuntime
from feature.contacts import analyze_refresh_batch
from feature.contacts import auto_maintenance_is_due
from feature.contacts import contact_auto_maintenance_collect_hard_timeout_seconds
from feature.contacts import contact_auto_maintenance_read_timeout_seconds
from feature.contacts import check_contact_directory_auto_maintenance
from feature.contacts import cleanup_orphaned_contact_auto_collector
from feature.contacts import edit_friend_info_via_chat_profile
from feature.contacts import has_active_contact_maintenance_conflict
from feature.contacts import is_contact_directory_auto_maintenance_idle
from feature.contacts import modify_friend_tags_via_chat_profile
from feature.contacts import prepare_contact_directory_window
from feature.contacts import repair_contact_profile_remarks
from feature.contacts import normalize_auto_maintenance_batch_size
from feature.contacts import refresh_batch_settings
from feature.contacts import refresh_contact_profiles_batch
from feature.contacts import refresh_contact_profiles_single_batch
from feature.contacts import run_contact_auto_maintenance_collector
from feature.contacts import set_contact_profiles_paused
from web_server import (
    _chat_memory_messages_for_user,
    _contact_profiles_browser_contacts,
    _chat_memory_user_sort_key,
    _wechat_name_sort_key,
    app,
    memory_chats,
    memory_data,
    memory_delete_chat,
    memory_delete_wx,
    memory_list,
)
from web_server import _contact_profiles_summary
from core.contact_profiles import load_directory as load_contact_directory
from core.contact_profiles import mark_history_target_status
from core.contact_profiles import save_directory as save_contact_directory


def _contact_owner_for(wx):
    runtime = WeChatUIRuntime(lambda *_args: None)
    runtime._client = wx
    return SimpleNamespace(call=lambda intent, _timeout: runtime.edit_contact(intent.payload))


def append_test_history(manager, chat_name, content, *, message_type="text", metadata=None):
    manager.message_store.append_history([{
        "event_id": f"test:{chat_name}:{content}",
        "conversation": chat_name,
        "chat_type": "private",
        "direction": "friend",
        "sender": chat_name,
        "content": content,
        "original_content": content,
        "message_type": message_type,
        "native_attr": "friend",
        "native_time": "2026/07/14 08:00:00",
        "received_at": 1.0,
        "metadata": dict(metadata or {}),
    }])


class ContactCollectorOrphanCleanupTests(unittest.TestCase):
    def test_cleanup_kills_only_registry_process_with_matching_command_line(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            registry = Path(temp_dir) / "collector.json"
            script = str(Path(temp_dir) / "contact_auto_collector_worker.py")
            request = str(Path(temp_dir) / "request.json")
            registry.write_text(json.dumps({
                "pid": 4242,
                "script_path": script,
                "request_path": request,
                "created_at": 1,
            }), encoding="utf-8")
            completed = SimpleNamespace(returncode=0, stdout="", stderr="")

            with (
                patch("feature.contacts._contact_auto_collector_registry_path", return_value=str(registry)),
                patch("feature.contacts._windows_process_command_line", return_value=f'python "{script}" --request "{request}"'),
                patch("feature.contacts.subprocess.run", return_value=completed) as run,
            ):
                result = cleanup_orphaned_contact_auto_collector()

            self.assertTrue(result["verified"])
            self.assertTrue(result["terminated"])
            self.assertEqual(run.call_args.args[0], ["taskkill", "/PID", "4242", "/T", "/F"])
            self.assertFalse(registry.exists())

    def test_cleanup_never_kills_reused_pid_with_mismatched_command_line(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            registry = Path(temp_dir) / "collector.json"
            registry.write_text(json.dumps({
                "pid": 4242,
                "script_path": str(Path(temp_dir) / "contact_auto_collector_worker.py"),
                "request_path": str(Path(temp_dir) / "request.json"),
                "created_at": 1,
            }), encoding="utf-8")

            with (
                patch("feature.contacts._contact_auto_collector_registry_path", return_value=str(registry)),
                patch("feature.contacts._windows_process_command_line", return_value="python unrelated.py"),
                patch("feature.contacts.subprocess.run") as run,
            ):
                result = cleanup_orphaned_contact_auto_collector()

            self.assertFalse(result["verified"])
            run.assert_not_called()
            self.assertFalse(registry.exists())


class WeChatNameSortTests(unittest.TestCase):
    def test_wechat_name_sort_order_groups_digits_letters_then_chinese(self):
        names = ["阿风", "B-吴岳英", "112", "A0-努力", "9号", "王玉芹"]

        self.assertEqual(
            sorted(names, key=_wechat_name_sort_key),
            ["112", "9号", "A0-努力", "B-吴岳英", "阿风", "王玉芹"],
        )

    def test_chat_memory_sort_uses_chat_name_without_source_priority(self):
        users = [
            {"chat_name": "B-吴岳英", "source": "chat_memory"},
            {"chat_name": "112", "source": "chat_memory"},
            {"chat_name": "A0-努力", "source": "chat_memory"},
        ]

        ordered = sorted(users, key=_chat_memory_user_sort_key)

        self.assertEqual([item["chat_name"] for item in ordered], ["112", "A0-努力", "B-吴岳英"])

    def test_contact_browser_contacts_sort_by_nickname_not_remark(self):
        contacts = _contact_profiles_browser_contacts({
            "subjects": [
                {
                    "status": "active",
                    "contact_key": "1",
                    "nickname": "王玉芹",
                    "remark": "A0-72王玉芹",
                    "wechat_id": "w1",
                },
                {
                    "status": "active",
                    "contact_key": "2",
                    "nickname": "阿风",
                    "remark": "A0-阿风",
                    "wechat_id": "w2",
                },
            ],
        })

        self.assertEqual([item["nickname"] for item in contacts], ["阿风", "王玉芹"])

    def test_contact_browser_contacts_handles_blank_sort_name(self):
        contacts = _contact_profiles_browser_contacts({
            "subjects": [
                {
                    "status": "active",
                    "contact_key": "blank",
                    "nickname": "",
                    "remark": "",
                    "wechat_id": "",
                },
                {
                    "status": "active",
                    "contact_key": "named",
                    "nickname": "阿风",
                    "remark": "A0-阿风",
                    "wechat_id": "w2",
                },
            ],
        })

        self.assertEqual([item["contact_key"] for item in contacts], ["named", "blank"])

    def test_memory_chats_endpoint_sorts_by_display_name(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            manager = MemoryManager("wx_test", temp_dir)
            for display_name in ["B-吴岳英", "112", "A0-努力"]:
                append_test_history(manager, display_name, "hello")

            with patch("web_server.MEMORY_BASE", temp_dir):
                with app.test_request_context("/memory/chats/wx_test"):
                    response = memory_chats.__wrapped__("wx_test")

            payload = response.get_json()

        self.assertEqual([item["name"] for item in payload["chats"]], ["112", "A0-努力", "B-吴岳英"])

    def test_memory_chats_endpoint_hides_empty_record_dirs(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            manager = MemoryManager("wx_test", temp_dir)
            append_test_history(manager, "112", "hello")
            base = Path(temp_dir) / "accounts" / "wx_test" / "memory"
            empty_dir = base / "empty"
            empty_dir.mkdir(parents=True)
            (empty_dir / "name.json").write_text(json.dumps({"name": "空记录"}, ensure_ascii=False), encoding="utf-8")
            (empty_dir / "empty_memory.json").write_text("[]", encoding="utf-8")

            with patch("web_server.MEMORY_BASE", temp_dir):
                with app.test_request_context("/memory/chats/wx_test"):
                    response = memory_chats.__wrapped__("wx_test")

            payload = response.get_json()

        self.assertEqual([item["name"] for item in payload["chats"]], ["112"])


class MemorySQLiteEndpointTests(unittest.TestCase):
    @staticmethod
    def _save(manager, chat_name, content):
        append_test_history(manager, chat_name, content)

    def test_memory_list_does_not_import_legacy_json(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            memory_dir = Path(temp_dir) / "accounts" / "wx_test" / "memory" / "张三"
            memory_dir.mkdir(parents=True)
            (memory_dir / "张三_memory.json").write_text(
                json.dumps([{"attr": "friend", "sender": "张三", "content": "旧消息"}], ensure_ascii=False),
                encoding="utf-8",
            )

            with (
                patch("web_server.MEMORY_BASE", temp_dir),
                patch("web_server._read_last_wx_id", return_value=""),
                app.test_request_context("/memory/list"),
            ):
                response = memory_list.__wrapped__()

            payload = response.get_json()
            self.assertEqual(payload["status"], "success")
            self.assertIn("wx_test", payload["wx_ids"])
            self.assertTrue((Path(temp_dir) / "accounts" / "wx_test" / "message_store.sqlite3").is_file())
            self.assertEqual(
                MemoryManager("wx_test", temp_dir).get_messages(
                    "张三",
                    10,
                    chat_type="private",
                ),
                [],
            )

    def test_memory_data_and_extraction_helper_are_account_scoped(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            first = MemoryManager("wx_one", temp_dir)
            second = MemoryManager("wx_two", temp_dir)
            self._save(first, "张三", "账号一")
            self._save(second, "张三", "账号二")

            with patch("web_server.MEMORY_BASE", temp_dir):
                with app.test_request_context("/memory/data/wx_one/张三?chat_type=private"):
                    response = memory_data.__wrapped__("wx_one", "张三")
                extracted = _chat_memory_messages_for_user("wx_two", "张三")

            panel_messages = response.get_json()["messages"]
            self.assertEqual([item["content"] for item in panel_messages], ["账号一"])
            self.assertNotIn("event_id", panel_messages[0])
            self.assertEqual([item["content"] for item in extracted], ["账号二"])

    def test_memory_data_hides_inferred_time_and_internal_marker(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            manager = MemoryManager("wx_test", temp_dir)
            append_test_history(
                manager,
                "张三",
                "停机消息",
                metadata={"time_inferred": True, "context_repair": True},
            )

            with patch("web_server.MEMORY_BASE", temp_dir):
                with app.test_request_context(
                    "/memory/data/wx_test/张三?chat_type=private"
                ):
                    response = memory_data.__wrapped__("wx_test", "张三")

            message = response.get_json()["messages"][0]
            self.assertEqual(message["content"], "停机消息")
            self.assertEqual(message["time"], "")
            self.assertNotIn("event_id", message)
            self.assertNotIn("time_inferred", message)

    def test_memory_delete_chat_only_hides_selected_conversation(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            manager = MemoryManager("wx_test", temp_dir)
            self._save(manager, "张三", "消息一")
            self._save(manager, "李四", "消息二")

            with patch("web_server.MEMORY_BASE", temp_dir):
                with app.test_request_context(
                    "/memory/delete_chat/wx_test/张三",
                    method="DELETE",
                    data={"chat_type": "private"},
                ):
                    response = memory_delete_chat.__wrapped__("wx_test", "张三")

            reloaded = MemoryManager("wx_test", temp_dir)
            self.assertEqual(response.get_json()["status"], "success")
            self.assertEqual(reloaded.get_messages("张三", 10, chat_type="private"), [])
            self.assertEqual(
                [item["content"] for item in reloaded.get_messages(
                    "李四",
                    10,
                    chat_type="private",
                )],
                ["消息二"],
            )

    def test_memory_delete_wx_hides_all_account_history(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            manager = MemoryManager("wx_test", temp_dir)
            self._save(manager, "张三", "消息一")
            self._save(manager, "李四", "消息二")

            with patch("web_server.MEMORY_BASE", temp_dir):
                with app.test_request_context("/memory/delete_wx/wx_test", method="DELETE"):
                    response = memory_delete_wx.__wrapped__("wx_test")

            reloaded = MemoryManager("wx_test", temp_dir)
            self.assertEqual(response.get_json()["status"], "success")
            self.assertEqual(reloaded.list_chat_names(chat_type="private"), [])
            self.assertEqual(reloaded.list_chat_names(chat_type="group"), [])


class ContactMaintenancePrepareTests(unittest.TestCase):
    def test_mark_history_target_status_updates_named_existing_contact(self):
        directory = {
            "wx_id": "scope_rui",
            "subjects": [{
                "status": "active",
                "contact_key": "wechat_id:wxid_old",
                "wechat_id": "wxid_old",
                "wxid": "wxid_old",
                "remark": "张三",
                "nickname": "三三",
                "warnings": [],
            }],
            "maintenance": {},
        }

        updated = mark_history_target_status(
            directory,
            "张三",
            "wxid_new",
            wx_id="scope_rui",
            now=datetime(2026, 6, 10, 21, 0, 0),
        )

        self.assertEqual(len(updated["subjects"]), 1)
        self.assertEqual(updated["subjects"][0]["wxid"], "wxid_new")

    def _auto_maintenance_bot(self):
        calls = []

        class FakeBot:
            wx = object()
            start_time = "2026-06-10 20:00:00"
            last_msg_time = "2026-06-10 20:59:30"
            contact_directory_auto_maintenance_switch = True
            contact_directory_auto_maintenance_interval_minutes = 5
            contact_directory_auto_maintenance_full_scan_interval_days = 1
            contact_directory_auto_maintenance_window_start = "00:00"
            contact_directory_auto_maintenance_window_end = "23:59"
            contact_directory_auto_maintenance_batch_size = 50

            def __init__(self):
                self.calls = calls

            def _load_contact_profiles_directory(self):
                return {"maintenance": {}}, "ignored.json", "scope_rui"

            def _write_contact_directory_auto_cycle_state(self, directory, **updates):
                calls.append(("write_cycle", updates))
                directory = dict(directory or {})
                directory.setdefault("maintenance", {}).update(updates)
                return directory

            def _save_contact_profiles_directory(self, directory):
                calls.append(("save_directory",))

            def refresh_contact_profiles_batch(self, **kwargs):
                calls.append(("refresh_batch", kwargs))
                return {
                    "directory": {"maintenance": {}},
                    "analysis": {"outcome": "advanced"},
                    "next_start_name": "B",
                    "backup_start_name": "A",
                    "completed": False,
                }

        return FakeBot()

    def test_auto_maintenance_recent_message_no_longer_blocks(self):
        bot = self._auto_maintenance_bot()
        with patch("feature.contacts.is_contact_directory_auto_maintenance_idle", return_value=True):
            result = check_contact_directory_auto_maintenance(bot, now=datetime(2026, 6, 10, 21, 0, 0))

        self.assertTrue(result)
        self.assertTrue(any(call[0] == "refresh_batch" for call in bot.calls))
        success_updates = [call[1] for call in bot.calls if call[0] == "write_cycle"][-1]
        self.assertEqual(success_updates["auto_cycle_next_start_name"], "B")
        self.assertEqual(success_updates["auto_cycle_backup_start_name"], "A")

    def test_auto_maintenance_waits_for_active_private_pipeline(self):
        bot = self._auto_maintenance_bot()
        bot._private_message_pipelines = {
            "张三": {
                "open_messages": [object()],
                "queued_batches": [],
                "worker_running": False,
            }
        }

        with patch("feature.contacts.is_contact_directory_auto_maintenance_idle", return_value=True):
            result = check_contact_directory_auto_maintenance(bot, now=datetime(2026, 6, 10, 21, 0, 0))

        self.assertFalse(result)
        self.assertFalse(any(call[0] == "refresh_batch" for call in bot.calls))

    def test_active_contact_maintenance_conflict_uses_runtime_ingress_timestamp(self):
        bot = SimpleNamespace(_last_incoming_message_at=1000.0)

        self.assertTrue(has_active_contact_maintenance_conflict(bot, now_ts=1008.0))
        self.assertFalse(has_active_contact_maintenance_conflict(bot, now_ts=1011.0))

    def test_active_contact_maintenance_conflict_includes_listener_window_recovery(self):
        supervisor = SimpleNamespace(snapshot=lambda: [{"conversation": "张三"}])
        bot = SimpleNamespace(
            _last_incoming_message_at=0.0,
            _listener_window_supervisor=supervisor,
        )

        self.assertTrue(has_active_contact_maintenance_conflict(bot, now_ts=1008.0))

    def test_active_contact_maintenance_conflict_includes_group_reply(self):
        bot = SimpleNamespace(
            _last_incoming_message_at=0.0,
            _group_message_pipelines={
                "测试群": {
                    "open_messages": [],
                    "queued_batches": [],
                    "worker_running": True,
                }
            },
        )

        self.assertTrue(has_active_contact_maintenance_conflict(bot, now_ts=1008.0))

    def test_active_contact_maintenance_conflict_includes_running_tail_repair(self):
        bot = SimpleNamespace(
            _last_incoming_message_at=0.0,
            _memory_context_repair_state={
                "private:张三": {
                    "generation": 1,
                    "inflight_generation": 1,
                    "retry_at": 0.0,
                }
            },
        )

        self.assertTrue(has_active_contact_maintenance_conflict(bot, now_ts=1008.0))

    def test_auto_maintenance_idle_requires_a_clean_empty_global_scan(self):
        bot = SimpleNamespace(
            config=SimpleNamespace(AllListen_switch=True),
            _global_scan_state={
                "initial_drain_complete": True,
                "last_scan_empty": True,
                "degraded_conversations": {},
                "fail_stopped": False,
            },
            _global_scan_state_lock=threading.Lock(),
            _ui_owner=SimpleNamespace(is_idle=lambda: True),
            _memory_context_repair_state={},
            _last_incoming_message_at=0.0,
        )

        self.assertTrue(is_contact_directory_auto_maintenance_idle(bot))
        bot._global_scan_state["degraded_conversations"] = {
            "private:张三": {"conversation": "张三", "expected_count": 3, "actual_count": 2}
        }
        self.assertFalse(is_contact_directory_auto_maintenance_idle(bot))

    def test_auto_maintenance_idle_ignores_dormant_tail_marker_without_work(self):
        bot = SimpleNamespace(
            config=SimpleNamespace(AllListen_switch=False),
            _ui_owner=SimpleNamespace(is_idle=lambda: True),
            _memory_context_repair_state={
                "private:张三": {
                    "generation": 1,
                    "inflight_generation": None,
                    "retry_at": 0.0,
                }
            },
            _last_incoming_message_at=0.0,
        )

        self.assertTrue(is_contact_directory_auto_maintenance_idle(bot))

    def test_auto_maintenance_runs_batch_without_legacy_lock(self):
        calls = []

        class GuardLock:
            held = False

            def acquire(self, blocking=True):
                calls.append(("lock_acquire", blocking))
                if self.held:
                    return False
                self.held = True
                return True

            def release(self):
                calls.append(("lock_release",))
                self.held = False

        lock = GuardLock()

        class FakeBot:
            wx = object()
            start_time = "2026-06-10 20:00:00"
            contact_directory_auto_maintenance_switch = True
            contact_directory_auto_maintenance_interval_minutes = 5
            contact_directory_auto_maintenance_full_scan_interval_days = 1
            contact_directory_auto_maintenance_window_start = "00:00"
            contact_directory_auto_maintenance_window_end = "23:59"
            contact_directory_auto_maintenance_batch_size = 50

            def _load_contact_profiles_directory(self):
                return {"maintenance": {}}, "ignored.json", "scope_rui"

            def _write_contact_directory_auto_cycle_state(self, directory, **updates):
                directory = dict(directory or {})
                directory.setdefault("maintenance", {}).update(updates)
                return directory

            def _save_contact_profiles_directory(self, _directory):
                pass

            def refresh_contact_profiles_batch(self, **kwargs):
                calls.append(("batch_lock_held", lock.held))
                return {
                    "directory": {"maintenance": {}},
                    "analysis": {"outcome": "advanced"},
                    "next_start_name": "B",
                    "backup_start_name": "A",
                    "completed": False,
                }

        with patch("feature.contacts.is_contact_directory_auto_maintenance_idle", return_value=True):
            self.assertTrue(check_contact_directory_auto_maintenance(FakeBot(), now=datetime(2026, 6, 10, 21, 0, 0)))

        self.assertEqual(calls, [("batch_lock_held", False)])

    def test_auto_maintenance_falls_back_to_backup_cursor_after_primary_stalls(self):
        calls = []

        class FakeBot:
            wx = object()
            start_time = "2026-06-10 20:00:00"
            contact_directory_auto_maintenance_switch = True
            contact_directory_auto_maintenance_interval_minutes = 5
            contact_directory_auto_maintenance_full_scan_interval_days = 1
            contact_directory_auto_maintenance_window_start = "00:00"
            contact_directory_auto_maintenance_window_end = "23:59"
            contact_directory_auto_maintenance_batch_size = 50

            def _load_contact_profiles_directory(self):
                return {
                    "maintenance": {
                        "auto_cycle_status": "running",
                        "auto_cycle_next_start_name": "主游标",
                        "auto_cycle_next_start_identity": "wechat_id:primary",
                        "auto_cycle_backup_start_name": "备用游标",
                        "auto_cycle_backup_start_identity": "wechat_id:backup",
                        "auto_cycle_retry_count": 0,
                        "last_attempted_at": "2026-06-10 20:00:00",
                    }
                }, "ignored.json", "scope_rui"

            def refresh_contact_profiles_batch(self, **kwargs):
                calls.append(("refresh_batch", kwargs))
                return {
                    "directory": {
                        "maintenance": {
                            "auto_cycle_retry_count": 0,
                            "auto_cycle_batches_completed": 3,
                        }
                    },
                    "analysis": {"outcome": "empty_batch", "completed": False},
                    "next_start_name": "",
                    "backup_start_name": "",
                    "completed": False,
                }

            def _write_contact_directory_auto_cycle_state(self, directory, **updates):
                calls.append(("write_cycle", updates))
                directory = dict(directory or {})
                directory.setdefault("maintenance", {}).update(updates)
                return directory

            def _save_contact_profiles_directory(self, _directory):
                pass

        with patch("feature.contacts.is_contact_directory_auto_maintenance_idle", return_value=True):
            result = check_contact_directory_auto_maintenance(FakeBot(), now=datetime(2026, 6, 10, 21, 0, 0))

        self.assertTrue(result)
        self.assertEqual(calls[0], ("refresh_batch", {
            "mode": "standard",
            "start_name": "主游标",
            "start_identity": "wechat_id:primary",
            "use_saved_position": True,
            "count_override": 50,
            "run_to_completion": False,
            "automatic": True,
        }))
        fallback_updates = [call[1] for call in calls if call[0] == "write_cycle"][-1]
        self.assertEqual(fallback_updates["auto_cycle_status"], "stalled")
        self.assertEqual(fallback_updates["auto_cycle_next_start_name"], "备用游标")
        self.assertEqual(fallback_updates["auto_cycle_backup_start_name"], "")
        self.assertEqual(fallback_updates["auto_cycle_retry_count"], 1)

    def test_auto_maintenance_timeout_persists_backup_cursor_before_reraising(self):
        bot = self._auto_maintenance_bot()
        bot._load_contact_profiles_directory = lambda: ({
            "maintenance": {
                "auto_cycle_status": "running",
                "auto_cycle_next_start_name": "主游标",
                "auto_cycle_next_start_identity": "wechat_id:primary",
                "auto_cycle_backup_start_name": "备用游标",
                "auto_cycle_backup_start_identity": "wechat_id:backup",
                "auto_cycle_retry_count": 0,
                "last_attempted_at": "2026-06-10 20:00:00",
            }
        }, "ignored.json", "scope_rui")

        def fail_batch(**kwargs):
            bot.calls.append(("refresh_batch", kwargs))
            raise RuntimeError("通讯录采集超过 300s，已终止本批次")

        bot.refresh_contact_profiles_batch = fail_batch
        with (
            patch("feature.contacts.is_contact_directory_auto_maintenance_idle", return_value=True),
            self.assertRaisesRegex(RuntimeError, "超过 300s"),
        ):
            check_contact_directory_auto_maintenance(bot, now=datetime(2026, 6, 10, 21, 0, 0))

        saved = [call[1] for call in bot.calls if call[0] == "write_cycle"][-1]
        self.assertEqual(saved["auto_cycle_status"], "stalled")
        self.assertEqual(saved["auto_cycle_next_start_name"], "备用游标")
        self.assertEqual(saved["auto_cycle_backup_start_name"], "")
        self.assertEqual(saved["auto_cycle_last_outcome"], "primary_cursor_failed")
        self.assertEqual(saved["auto_cycle_retry_count"], 1)

    def test_auto_maintenance_timeout_on_fallback_cursor_resets_to_head(self):
        bot = self._auto_maintenance_bot()
        bot._load_contact_profiles_directory = lambda: ({
            "maintenance": {
                "auto_cycle_status": "stalled",
                "auto_cycle_next_start_name": "备用游标",
                "auto_cycle_next_start_identity": "wechat_id:backup",
                "auto_cycle_backup_start_name": "",
                "auto_cycle_retry_count": 1,
                "last_attempted_at": "2026-06-10 20:00:00",
            }
        }, "ignored.json", "scope_rui")
        bot.refresh_contact_profiles_batch = lambda **_kwargs: (_ for _ in ()).throw(RuntimeError("collector timeout"))

        with (
            patch("feature.contacts.is_contact_directory_auto_maintenance_idle", return_value=True),
            self.assertRaisesRegex(RuntimeError, "collector timeout"),
        ):
            check_contact_directory_auto_maintenance(bot, now=datetime(2026, 6, 10, 21, 0, 0))

        saved = [call[1] for call in bot.calls if call[0] == "write_cycle"][-1]
        self.assertEqual(saved["auto_cycle_status"], "reset_required")
        self.assertEqual(saved["auto_cycle_next_start_name"], "")
        self.assertEqual(saved["auto_cycle_backup_start_name"], "")
        self.assertEqual(saved["auto_cycle_last_outcome"], "cursor_batch_failed")
        self.assertEqual(saved["auto_cycle_retry_count"], 2)

    def test_auto_maintenance_tail_probe_timeout_keeps_same_cursor(self):
        bot = self._auto_maintenance_bot()
        bot._load_contact_profiles_directory = lambda: ({
            "maintenance": {
                "auto_cycle_status": "running",
                "auto_cycle_next_start_name": "最后联系人",
                "auto_cycle_next_start_identity": "wechat_id:last",
                "auto_cycle_backup_start_name": "备用游标",
                "auto_cycle_backup_start_identity": "wechat_id:backup",
                "auto_cycle_last_outcome": "short_advanced",
                "auto_cycle_retry_count": 0,
                "last_attempted_at": "2026-06-10 20:00:00",
            }
        }, "ignored.json", "scope_rui")

        def fail_batch(**kwargs):
            bot.calls.append(("refresh_batch", kwargs))
            raise RuntimeError("通讯录采集超过 300s，已终止本批次")

        bot.refresh_contact_profiles_batch = fail_batch
        with (
            patch("feature.contacts.is_contact_directory_auto_maintenance_idle", return_value=True),
            self.assertRaisesRegex(RuntimeError, "超过 300s"),
        ):
            check_contact_directory_auto_maintenance(bot, now=datetime(2026, 6, 10, 21, 0, 0))

        refresh_kwargs = [call[1] for call in bot.calls if call[0] == "refresh_batch"][0]
        saved = [call[1] for call in bot.calls if call[0] == "write_cycle"][-1]
        self.assertEqual(refresh_kwargs["count_override"], 2)
        self.assertEqual(saved["auto_cycle_status"], "running")
        self.assertEqual(saved["auto_cycle_next_start_name"], "最后联系人")
        self.assertEqual(saved["auto_cycle_next_start_identity"], "wechat_id:last")
        self.assertEqual(saved["auto_cycle_backup_start_name"], "备用游标")
        self.assertEqual(saved["auto_cycle_last_outcome"], "tail_confirm_pending")
        self.assertNotIn("last_full_scan_completed_at", saved)

    def test_auto_maintenance_resets_when_primary_cursor_stalls_without_backup(self):
        calls = []

        class FakeBot:
            wx = object()
            start_time = "2026-06-10 20:00:00"
            contact_directory_auto_maintenance_switch = True
            contact_directory_auto_maintenance_interval_minutes = 5
            contact_directory_auto_maintenance_full_scan_interval_days = 1
            contact_directory_auto_maintenance_window_start = "00:00"
            contact_directory_auto_maintenance_window_end = "23:59"
            contact_directory_auto_maintenance_batch_size = 50

            def _load_contact_profiles_directory(self):
                return {
                    "maintenance": {
                        "auto_cycle_status": "running",
                        "auto_cycle_next_start_name": "主游标",
                        "auto_cycle_next_start_identity": "wechat_id:primary",
                        "auto_cycle_backup_start_name": "",
                        "auto_cycle_retry_count": 0,
                        "last_attempted_at": "2026-06-10 20:00:00",
                    }
                }, "ignored.json", "scope_rui"

            def refresh_contact_profiles_batch(self, **kwargs):
                calls.append(("refresh_batch", kwargs))
                return {
                    "directory": {
                        "maintenance": {
                            "auto_cycle_retry_count": 0,
                            "auto_cycle_batches_completed": 3,
                        }
                    },
                    "analysis": {"outcome": "empty_batch", "completed": False},
                    "next_start_name": "",
                    "backup_start_name": "",
                    "completed": False,
                }

            def _write_contact_directory_auto_cycle_state(self, directory, **updates):
                calls.append(("write_cycle", updates))
                directory = dict(directory or {})
                directory.setdefault("maintenance", {}).update(updates)
                return directory

            def _save_contact_profiles_directory(self, _directory):
                pass

        with patch("feature.contacts.is_contact_directory_auto_maintenance_idle", return_value=True):
            result = check_contact_directory_auto_maintenance(FakeBot(), now=datetime(2026, 6, 10, 21, 0, 0))

        self.assertTrue(result)
        self.assertEqual(calls[0], ("refresh_batch", {
            "mode": "standard",
            "start_name": "主游标",
            "start_identity": "wechat_id:primary",
            "use_saved_position": True,
            "count_override": 50,
            "run_to_completion": False,
            "automatic": True,
        }))
        fallback_updates = [call[1] for call in calls if call[0] == "write_cycle"][-1]
        self.assertEqual(fallback_updates["auto_cycle_status"], "reset_required")
        self.assertEqual(fallback_updates["auto_cycle_next_start_name"], "")
        self.assertEqual(fallback_updates["auto_cycle_backup_start_name"], "")
        self.assertEqual(fallback_updates["auto_cycle_retry_count"], 1)

    def test_auto_maintenance_logs_cursor_usage_and_progress(self):
        calls = []
        log_messages = []

        class FakeBot:
            wx = object()
            start_time = "2026-06-10 20:00:00"
            contact_directory_auto_maintenance_switch = True
            contact_directory_auto_maintenance_interval_minutes = 5
            contact_directory_auto_maintenance_full_scan_interval_days = 1
            contact_directory_auto_maintenance_window_start = "00:00"
            contact_directory_auto_maintenance_window_end = "23:59"
            contact_directory_auto_maintenance_batch_size = 50

            def _load_contact_profiles_directory(self):
                return {
                    "maintenance": {
                        "auto_cycle_status": "running",
                        "auto_cycle_next_start_name": "主游标",
                        "auto_cycle_next_start_identity": "wechat_id:primary",
                        "auto_cycle_backup_start_name": "备用游标",
                        "auto_cycle_backup_start_identity": "wechat_id:backup",
                        "auto_cycle_retry_count": 0,
                        "last_attempted_at": "2026-06-10 20:00:00",
                    }
                }, "ignored.json", "scope_rui"

            def refresh_contact_profiles_batch(self, **kwargs):
                calls.append(("refresh_batch", kwargs))
                return {
                    "directory": {
                        "maintenance": {
                            "auto_cycle_retry_count": 0,
                            "auto_cycle_batches_completed": 1,
                        }
                    },
                    "analysis": {"outcome": "advanced", "completed": False},
                    "next_start_name": "新主游标",
                    "next_start_identity": "wechat_id:new-primary",
                    "backup_start_name": "新备用游标",
                    "backup_start_identity": "wechat_id:new-backup",
                    "completed": False,
                }

            def _write_contact_directory_auto_cycle_state(self, directory, **updates):
                directory = dict(directory or {})
                directory.setdefault("maintenance", {}).update(updates)
                return directory

            def _save_contact_profiles_directory(self, _directory):
                pass

        with (
            patch("feature.contacts.is_contact_directory_auto_maintenance_idle", return_value=True),
            patch("feature.contacts.log", side_effect=lambda **kwargs: log_messages.append(kwargs.get("message", ""))),
        ):
            self.assertTrue(check_contact_directory_auto_maintenance(FakeBot(), now=datetime(2026, 6, 10, 21, 0, 0)))

        self.assertIn("[通讯录维护] 自动维护使用游标：主游标，备用游标：备用游标", log_messages)
        self.assertIn("[通讯录维护] 自动维护游标推进：主游标 -> 新主游标，备用游标：新备用游标", log_messages)

    def test_auto_maintenance_short_batch_keeps_cycle_running(self):
        analysis = analyze_refresh_batch(
            raw_details=[{"备注": "阿英1"}, {"备注": "阿英2"}],
            requested_count=50,
            current_start_name="",
            allow_short_batch_complete=False,
        )

        self.assertEqual(analysis["outcome"], "short_advanced")
        self.assertFalse(analysis["completed"])

    def _tail_confirmation_updates(
        self,
        retry_count,
        *,
        raw_identities=None,
        outcome="not_advanced",
        next_name="最后联系人",
        next_identity="wechat_id:last",
        result_completed=False,
    ):
        calls = []
        raw_identities = list(
            ["wechat_id:last", "wechat_id:last"]
            if raw_identities is None
            else raw_identities
        )

        class FakeBot:
            wx = object()
            start_time = "2026-06-10 20:00:00"
            contact_directory_auto_maintenance_switch = True
            contact_directory_auto_maintenance_interval_minutes = 5
            contact_directory_auto_maintenance_full_scan_interval_days = 1
            contact_directory_auto_maintenance_window_start = "00:00"
            contact_directory_auto_maintenance_window_end = "23:59"
            contact_directory_auto_maintenance_batch_size = 50

            def _load_contact_profiles_directory(self):
                return {
                    "maintenance": {
                        "auto_cycle_status": "running",
                        "auto_cycle_next_start_name": "最后联系人",
                        "auto_cycle_next_start_identity": "wechat_id:last",
                        "auto_cycle_last_outcome": "short_advanced" if retry_count == 0 else "tail_confirm_pending",
                        "auto_cycle_retry_count": retry_count,
                        "last_attempted_at": "2026-06-10 20:00:00",
                    }
                }, "ignored.json", "scope_rui"

            def refresh_contact_profiles_batch(self, **kwargs):
                calls.append(("refresh_batch", kwargs))
                return {
                    "directory": {
                        "maintenance": {
                            "auto_cycle_retry_count": retry_count,
                            "auto_cycle_batches_completed": 8,
                        }
                    },
                    "analysis": {"outcome": outcome, "completed": False},
                    "next_start_name": next_name,
                    "next_start_identity": next_identity,
                    "backup_start_name": "",
                    "raw_result_count": len(raw_identities),
                    "raw_result_identities": raw_identities,
                    "completed": result_completed,
                }

            def _write_contact_directory_auto_cycle_state(self, directory, **updates):
                calls.append(("write_cycle", updates))
                directory = dict(directory or {})
                directory.setdefault("maintenance", {}).update(updates)
                return directory

            def _save_contact_profiles_directory(self, _directory):
                pass

        with patch("feature.contacts.is_contact_directory_auto_maintenance_idle", return_value=True):
            self.assertTrue(check_contact_directory_auto_maintenance(FakeBot(), now=datetime(2026, 6, 10, 21, 0, 0)))

        refresh_kwargs = [call[1] for call in calls if call[0] == "refresh_batch"][0]
        self.assertEqual(refresh_kwargs["count_override"], 2)
        return [call[1] for call in calls if call[0] == "write_cycle"][-1]

    def test_auto_maintenance_confirms_short_batch_tail_once(self):
        final_updates = self._tail_confirmation_updates(retry_count=0)

        self.assertEqual(final_updates["auto_cycle_status"], "completed")
        self.assertEqual(final_updates["auto_cycle_last_outcome"], "completed")
        self.assertTrue(final_updates["last_full_scan_completed_at"])

    def test_auto_maintenance_confirms_completion_without_repeating_tail_probe(self):
        final_updates = self._tail_confirmation_updates(retry_count=2)

        self.assertEqual(final_updates["auto_cycle_status"], "completed")
        self.assertEqual(final_updates["auto_cycle_last_outcome"], "completed")
        self.assertTrue(final_updates["last_full_scan_completed_at"])

    def test_auto_maintenance_single_anchor_tail_probe_stays_pending(self):
        final_updates = self._tail_confirmation_updates(
            retry_count=0,
            raw_identities=["wechat_id:last"],
        )

        self.assertEqual(final_updates["auto_cycle_status"], "running")
        self.assertEqual(final_updates["auto_cycle_last_outcome"], "tail_confirm_pending")
        self.assertEqual(final_updates["auto_cycle_next_start_name"], "最后联系人")
        self.assertNotIn("last_full_scan_completed_at", final_updates)

    def test_auto_maintenance_tail_probe_ignores_unproven_completed_flag(self):
        final_updates = self._tail_confirmation_updates(
            retry_count=0,
            raw_identities=["wechat_id:last"],
            result_completed=True,
        )

        self.assertEqual(final_updates["auto_cycle_status"], "running")
        self.assertEqual(final_updates["auto_cycle_last_outcome"], "tail_confirm_pending")
        self.assertNotIn("last_full_scan_completed_at", final_updates)

    def test_auto_maintenance_tail_probe_rejects_missing_or_conflicting_completion_evidence(self):
        for raw_identities in ([], ["wechat_id:last", "wechat_id:other"]):
            with self.subTest(raw_identities=raw_identities):
                final_updates = self._tail_confirmation_updates(
                    retry_count=0,
                    raw_identities=raw_identities,
                    result_completed=True,
                )

                self.assertEqual(final_updates["auto_cycle_status"], "running")
                self.assertEqual(final_updates["auto_cycle_last_outcome"], "tail_confirm_pending")
                self.assertNotIn("last_full_scan_completed_at", final_updates)

    def test_auto_maintenance_tail_probe_with_next_identity_advances(self):
        final_updates = self._tail_confirmation_updates(
            retry_count=0,
            raw_identities=["wechat_id:last", "wechat_id:next"],
            outcome="advanced",
            next_name="下一位",
            next_identity="wechat_id:next",
        )

        self.assertEqual(final_updates["auto_cycle_status"], "running")
        self.assertEqual(final_updates["auto_cycle_last_outcome"], "advanced")
        self.assertEqual(final_updates["auto_cycle_next_start_name"], "下一位")
        self.assertNotIn("last_full_scan_completed_at", final_updates)

    def test_auto_maintenance_empty_batch_does_not_confirm_short_batch_tail(self):
        calls = []

        class FakeBot:
            wx = object()
            start_time = "2026-06-10 20:00:00"
            contact_directory_auto_maintenance_switch = True
            contact_directory_auto_maintenance_interval_minutes = 5
            contact_directory_auto_maintenance_full_scan_interval_days = 1
            contact_directory_auto_maintenance_window_start = "00:00"
            contact_directory_auto_maintenance_window_end = "23:59"
            contact_directory_auto_maintenance_batch_size = 50

            def _load_contact_profiles_directory(self):
                return {
                    "maintenance": {
                        "auto_cycle_status": "running",
                        "auto_cycle_next_start_name": "最后联系人",
                        "auto_cycle_last_outcome": "tail_confirm_pending",
                        "auto_cycle_retry_count": 2,
                        "last_attempted_at": "2026-06-10 20:00:00",
                    }
                }, "ignored.json", "scope_rui"

            def refresh_contact_profiles_batch(self, **kwargs):
                calls.append(("refresh_batch", kwargs))
                return {
                    "directory": {"maintenance": {"auto_cycle_retry_count": 2}},
                    "analysis": {"outcome": "empty_batch", "completed": False},
                    "next_start_name": "",
                    "backup_start_name": "",
                    "completed": False,
                }

            def _write_contact_directory_auto_cycle_state(self, directory, **updates):
                calls.append(("write_cycle", updates))
                directory = dict(directory or {})
                directory.setdefault("maintenance", {}).update(updates)
                return directory

            def _save_contact_profiles_directory(self, _directory):
                pass

        with patch("feature.contacts.is_contact_directory_auto_maintenance_idle", return_value=True):
            self.assertTrue(check_contact_directory_auto_maintenance(FakeBot(), now=datetime(2026, 6, 10, 21, 0, 0)))

        final_updates = [call[1] for call in calls if call[0] == "write_cycle"][-1]
        self.assertNotEqual(final_updates["auto_cycle_status"], "completed")
        self.assertEqual(final_updates["auto_cycle_status"], "reset_required")
        self.assertFalse(final_updates["auto_cycle_next_start_name"])

    def test_auto_maintenance_resets_legacy_tail_complete_cycle_before_waiting_days(self):
        calls = []

        class FakeBot:
            wx = object()
            start_time = "2026-06-10 20:00:00"
            contact_directory_auto_maintenance_switch = True
            contact_directory_auto_maintenance_interval_minutes = 5
            contact_directory_auto_maintenance_full_scan_interval_days = 7
            contact_directory_auto_maintenance_window_start = "00:00"
            contact_directory_auto_maintenance_window_end = "23:59"
            contact_directory_auto_maintenance_batch_size = 50

            def _load_contact_profiles_directory(self):
                return {
                    "maintenance": {
                        "auto_cycle_status": "completed",
                        "auto_cycle_last_outcome": "completed",
                        "last_batch_outcome": "tail_complete",
                        "last_full_scan_completed_at": "2026-06-10 20:00:00",
                        "last_attempted_at": "2026-06-10 20:00:00",
                    }
                }, "ignored.json", "scope_rui"

            def refresh_contact_profiles_batch(self, **kwargs):
                calls.append(("refresh_batch", kwargs))
                return {
                    "directory": {"maintenance": {"auto_cycle_batches_completed": 0, "auto_cycle_retry_count": 0}},
                    "analysis": {"outcome": "advanced", "completed": False},
                    "next_start_name": "阿英50",
                    "backup_start_name": "阿英49",
                    "completed": False,
                }

            def _write_contact_directory_auto_cycle_state(self, directory, **updates):
                calls.append(("write_cycle", updates))
                directory = dict(directory or {})
                directory.setdefault("maintenance", {}).update(updates)
                return directory

            def _save_contact_profiles_directory(self, directory):
                calls.append(("save_directory", (directory.get("maintenance") or {}).get("auto_cycle_status")))

        with patch("feature.contacts.is_contact_directory_auto_maintenance_idle", return_value=True):
            self.assertTrue(check_contact_directory_auto_maintenance(FakeBot(), now=datetime(2026, 6, 11, 21, 0, 0)))

        self.assertIn(("save_directory", "reset_required"), calls)
        self.assertTrue(any(call[0] == "refresh_batch" for call in calls))

    def test_prepare_switches_contact_without_show(self):
        calls = []

        class FakeWeChat:
            def Show(self):
                calls.append("Show")
                raise AssertionError("Show should not be called")

            def SwitchToContact(self):
                calls.append("SwitchToContact")

        class FakeBot:
            wx = FakeWeChat()

        prepare_contact_directory_window(FakeBot())

        self.assertEqual(calls, ["SwitchToContact"])

    def test_prepare_does_not_rebind_after_business_switch_failure(self):
        calls = []

        class BrokenWeChat:
            def SwitchToContact(self):
                calls.append("broken")
                raise RuntimeError("missing contact tab")

        class FakeBot:
            wx = BrokenWeChat()

        with patch("core.wechat_window.rebind_wechat_client") as rebind:
            with self.assertRaisesRegex(RuntimeError, "missing contact tab"):
                prepare_contact_directory_window(FakeBot())

        self.assertEqual(calls, ["broken"])
        rebind.assert_not_called()


    def test_run_to_completion_passes_previous_tail_as_next_batch_start(self):
        calls = []
        results = [
            {
                "count_returned": 10,
                "next_start_name": "阿英10",
                "analysis": {"outcome": "advanced", "completed": False},
                "directory": {"maintenance": {"status": "idle"}},
            },
            {
                "count_returned": 8,
                "next_start_name": "阿英18",
                "analysis": {"outcome": "tail_complete", "completed": True},
                "directory": {"maintenance": {"status": "idle"}},
            },
        ]

        class FakeBot:
            def _load_contact_profiles_directory(self):
                return {"maintenance": {}}, "ignored.json", "scope_rui"

            def _refresh_contact_profiles_single_batch(self, **kwargs):
                calls.append(kwargs)
                return results.pop(0)

            def _summarize_directory_growth(self, _before, _after):
                return {"new_unique_count": 0, "directory_total_unique_count": 18}

            def _write_contact_directory_auto_cycle_state(self, directory, **updates):
                directory = dict(directory or {})
                directory.setdefault("maintenance", {}).update(updates)
                return directory

            def _save_contact_profiles_directory(self, _directory):
                pass

        result = refresh_contact_profiles_batch(FakeBot(), mode="standard", run_to_completion=True)

        self.assertEqual([call["start_name"] for call in calls], ["", "阿英10"])
        self.assertEqual([call["logical_start_name"] for call in calls], ["", "阿英10"])
        self.assertEqual([call["switch_back_to_chat"] for call in calls], [False, False])
        self.assertEqual(result["count_returned"], 18)
        self.assertTrue(result["completed"])

    def test_standard_refresh_reads_fifty_contacts_per_batch(self):
        settings = refresh_batch_settings("standard")

        self.assertEqual(settings["count"], 50)

    def test_repair_remarks_does_not_rebind_after_business_failure(self):
        calls = []

        class FakeOwner:
            owner_thread_id = None

            def call(self, intent, _timeout):
                if intent.kind.value == "rebind":
                    calls.append(("rebind",))
                    return {"nickname": "测试账号", "wx_id": "scope_rui"}
                if intent.kind.value == "contact_edit":
                    calls.append(("EditFriendInfo", intent.payload.get("remark")))
                    raise RuntimeError("desktop busy")
                raise AssertionError(intent.kind)

        class FakeBot:
            def __init__(self):
                self.wx = SimpleNamespace()
                self._ui_owner = FakeOwner()
                self._ui_runtime = object()
                self._ui_identity = {"nickname": "测试账号", "wx_id": "scope_rui"}

            def _load_contact_profiles_directory(self):
                return (
                    {
                        "wx_id": "scope_rui",
                        "subjects": [
                            {
                                "contact_key": "k1",
                                "nickname": "阿英2",
                                "display_name": "阿英2",
                                "remark": "",
                                "send_name": "阿英2",
                                "status": "active",
                                "warnings": ["duplicate_send_name"],
                            }
                        ],
                    },
                    "ignored.json",
                    "scope_rui",
                )

            def _contact_profiles_remark_repair_records_file(self):
                return "ignored-records.json"

            def _close_dynamic_listener_subwindows(self, _names):
                return []

        with (
            patch("feature.contacts.contact_repair_candidates", return_value=[{
                "contact_key": "k1",
                "display_name": "阿英2",
                "current_remark": "",
                "suggested_remark": "阿英2_test",
                "reasons": ["duplicate_send_name"],
                "warnings": ["duplicate_send_name"],
            }]),
            patch("feature.contacts.append_bounded_record"),
            patch("feature.contacts.save_contact_directory"),
            patch("feature.contacts.apply_repaired_remark", side_effect=lambda directory, contact_key, new_remark, now=None: directory),
        ):
            result = repair_contact_profile_remarks(FakeBot())

        self.assertEqual(result["success_count"], 0)
        self.assertEqual(result["failed_count"], 1)
        self.assertNotIn(("rebind",), calls)
        self.assertEqual(calls, [("EditFriendInfo", "阿英2_test")])


    def test_auto_maintenance_batch_size_policy(self):
        self.assertEqual(normalize_auto_maintenance_batch_size(20), 50)
        self.assertEqual(normalize_auto_maintenance_batch_size(50), 50)
        self.assertEqual(normalize_auto_maintenance_batch_size(80), 50)
        self.assertEqual(normalize_auto_maintenance_batch_size(10), 50)
        self.assertEqual(normalize_auto_maintenance_batch_size("bad"), 50)

    def test_auto_maintenance_due_prefers_finished_time(self):
        directory = {
            "maintenance": {
                "last_attempted_at": "2026-06-10 20:00:00",
                "last_batch_finished_at": "2026-06-10 20:59:00",
            }
        }

        self.assertFalse(auto_maintenance_is_due(
            directory,
            interval_minutes=30,
            now=datetime(2026, 6, 10, 21, 10, 0),
        ))
        self.assertTrue(auto_maintenance_is_due(
            directory,
            interval_minutes=30,
            now=datetime(2026, 6, 10, 21, 30, 0),
        ))

    def test_auto_maintenance_running_state_becomes_due_after_stale_interval(self):
        directory = {
            "maintenance": {
                "status": "running",
                "last_attempted_at": "2026-06-10 20:00:00",
            }
        }

        self.assertFalse(auto_maintenance_is_due(
            directory,
            interval_minutes=30,
            now=datetime(2026, 6, 10, 20, 10, 0),
        ))
        self.assertTrue(auto_maintenance_is_due(
            directory,
            interval_minutes=30,
            now=datetime(2026, 6, 10, 20, 31, 0),
        ))

    def test_auto_maintenance_legacy_read_timeout_is_ten_minutes(self):
        self.assertEqual(contact_auto_maintenance_read_timeout_seconds(50), 600)

    def test_auto_maintenance_collect_hard_timeout_is_five_minutes(self):
        self.assertEqual(contact_auto_maintenance_collect_hard_timeout_seconds(50), 300)

    def test_auto_maintenance_collector_timeout_attempts_taskkill_when_process_survives_kill(self):
        calls = []

        class FakeProcess:
            pid = 4242
            returncode = None

            def communicate(self, timeout=None):
                calls.append(("communicate", timeout))
                raise subprocess.TimeoutExpired(cmd="collector", timeout=timeout)

            def kill(self):
                calls.append(("kill", self.pid))

            def poll(self):
                calls.append(("poll",))
                return None

        def fake_taskkill(cmd, **_kwargs):
            calls.append(("taskkill", cmd))
            return SimpleNamespace(returncode=0, stdout="SUCCESS", stderr="")

        with tempfile.TemporaryDirectory() as temp_dir:
            with (
                patch("feature.contacts._acquire_contact_auto_collector_process_lock", return_value=lambda: calls.append(("release",))),
                patch("feature.contacts._runtime_base_dir", return_value=temp_dir),
                patch("feature.contacts._contact_auto_collector_python_executable", return_value="python"),
                patch("feature.contacts._contact_auto_collector_script_path", return_value="collector.py"),
                patch("feature.contacts.subprocess.Popen", return_value=FakeProcess()),
                patch("feature.contacts.subprocess.run", side_effect=fake_taskkill),
                patch("feature.contacts.os.name", "nt"),
            ):
                with self.assertRaisesRegex(RuntimeError, "PID 4242.*taskkill.*进程仍未退出"):
                    run_contact_auto_maintenance_collector(
                        start_name="阿英2",
                        count=50,
                        timeout_seconds=1,
                    )

        self.assertIn(("kill", 4242), calls)
        self.assertIn(("taskkill", ["taskkill", "/PID", "4242", "/T", "/F"]), calls)
        self.assertIn(("release",), calls)

    def test_contact_summary_continue_start_uses_existing_contact_tail(self):
        summary = _contact_profiles_summary({
            "maintenance": {"next_start_name": "旧游标"},
            "subjects": [
                {"status": "active", "remark": "阿英2"},
                {"status": "missing", "remark": "阿英3"},
                {"status": "active", "remark": "阿英4"},
            ],
        })

        self.assertEqual(summary["continue_start_name"], "阿英4")
        self.assertNotIn("next_start_name", summary)

    def test_auto_maintenance_single_batch_uses_minimal_collector(self):
        calls = []
        runtime_events = []

        def flaky_runtime_log(*_args, **kwargs):
            message = str(kwargs.get("message", ""))
            if "运行事件：" in message:
                runtime_events.append((kwargs.get("level"), message))
                raise OSError("log unavailable")

        class FakeWeChat:
            def SwitchToChat(self):
                calls.append(("SwitchToChat",))
                pass

        class FakeBot:
            wx = FakeWeChat()
            _runtime_instance_id = "a" * 32

            def _load_contact_profiles_directory(self):
                return {"subjects": [], "maintenance": {}}, "ignored.json", "scope_rui"

            def _prepare_contact_directory_window(self):
                pass

            def _run_contact_auto_maintenance_collector(self, **kwargs):
                calls.append(("collector", kwargs))
                return {
                    "ok": True,
                    "result": [{"备注": "阿英2"}],
                    "callback_names": ["阿英2"],
                    "matched_name": "阿英2",
                }

        with (
            patch("feature.contacts.save_contact_directory"),
            patch("feature.contacts.load_contact_directory", return_value={"subjects": [], "maintenance": {}}),
            patch("feature.contacts.log", side_effect=flaky_runtime_log),
        ):
            refresh_contact_profiles_single_batch(
                FakeBot(),
                mode="standard",
                start_name="阿英2",
                use_saved_position=True,
                run_kind="auto_maintenance",
            )

        collector_calls = [call for call in calls if call[0] == "collector"]
        self.assertEqual(runtime_events, [
            ("DEBUG", f"运行事件：通讯录批次完成 runtime_id={'a' * 32}"),
        ])
        self.assertEqual(collector_calls[0][1], {
            "start_name": "阿英2",
            "start_identity": "",
            "count": 50,
            "timeout_seconds": 300,
            "run_kind": "auto_maintenance",
        })
        self.assertIn(("SwitchToChat",), calls)

    def test_manual_batch_uses_fixed_minimal_collector_when_owner_is_active(self):
        calls = []

        class FakeWeChat:
            def GetFriendDetails(self, **_kwargs):
                raise AssertionError("owner 模式不得在主进程调用 GetFriendDetails")

            def SwitchToChat(self):
                calls.append(("SwitchToChat",))

        class FakeBot:
            wx = FakeWeChat()
            _ui_owner = object()

            def _load_contact_profiles_directory(self):
                return {"subjects": [], "maintenance": {}}, "ignored.json", "scope_rui"

            def _run_contact_auto_maintenance_collector(self, **kwargs):
                calls.append(("collector", kwargs))
                return {
                    "ok": True,
                    "result": [{"备注": "阿英2"}],
                    "callback_names": ["阿英2"],
                    "matched_name": "阿英2",
                }

        with (
            patch("feature.contacts.save_contact_directory"),
            patch("feature.contacts.load_contact_directory", return_value={"subjects": [], "maintenance": {}}),
        ):
            refresh_contact_profiles_single_batch(
                FakeBot(),
                mode="standard",
                count_override=20,
                run_kind="manual_standard",
            )

        self.assertEqual(calls[0], ("collector", {
            "start_name": "",
            "start_identity": "",
            "count": 50,
            "timeout_seconds": 300,
            "run_kind": "manual_standard",
        }))

    def test_contact_read_logs_from_callback_without_duplicate_result_logs(self):
        log_messages = []

        class FakeWeChat:
            def GetFriendDetails(self, **kwargs):
                details = [{"昵称": f"阿英{index}"} for index in range(1, 22)]
                for detail in details:
                    kwargs["callback"](detail)
                return details

            def SwitchToChat(self):
                pass

        class FakeBot:
            wx = FakeWeChat()

            def _load_contact_profiles_directory(self):
                return {"subjects": [], "maintenance": {}}, "ignored.json", "scope_rui"

            def _prepare_contact_directory_window(self):
                pass

            def _run_contact_auto_maintenance_collector(self, **_kwargs):
                names = [f"阿英{index}" for index in range(1, 22)]
                return {
                    "ok": True,
                    "result": [{"昵称": name} for name in names],
                    "callback_names": names,
                }

        with (
            patch("feature.contacts.save_contact_directory"),
            patch("feature.contacts.load_contact_directory", return_value={"subjects": [], "maintenance": {}}),
            patch("feature.contacts.log", side_effect=lambda **kwargs: log_messages.append(kwargs.get("message", ""))),
        ):
            refresh_contact_profiles_single_batch(FakeBot(), mode="standard")

        read_logs = [message for message in log_messages if "已读取联系人" in message]
        self.assertEqual(read_logs, [
            "[通讯录维护] 已读取联系人 20 人，当前：阿英20",
        ])

    def test_refresh_batch_uses_relationship_synced_directory_when_available(self):
        calls = []

        class FakeWeChat:
            def SwitchToChat(self):
                pass

        class FakeBot:
            wx = FakeWeChat()

            def _load_contact_profiles_directory(self):
                return {"subjects": [], "maintenance": {}}, "ignored.json", "scope_rui"

            def _prepare_contact_directory_window(self):
                pass

            def _run_contact_auto_maintenance_collector(self, **kwargs):
                return {
                    "ok": True,
                    "result": [{"备注": "阿英2"}],
                    "callback_names": ["阿英2"],
                    "matched_name": "阿英2",
                }

            def _sync_relationship_state_from_contact_directory(self, directory):
                calls.append(("relationship_sync", directory))
                updated = dict(directory or {})
                subjects = list(updated.get("subjects") or [])
                subjects[0] = dict(subjects[0], relationship_status="deleted")
                updated["subjects"] = subjects
                return updated

        with (
            patch("feature.contacts.save_contact_directory"),
            patch("feature.contacts.load_contact_directory", return_value={"subjects": [], "maintenance": {}}),
        ):
            result = refresh_contact_profiles_single_batch(FakeBot(), mode="standard", run_kind="auto_maintenance")

        self.assertEqual(len(calls), 1)
        self.assertEqual(result["directory"]["subjects"][0]["relationship_status"], "deleted")

    def test_contact_positioning_does_not_log_every_scanned_callback_item(self):
        log_messages = []

        class FakeWeChat:
            def GetFriendDetails(self, **kwargs):
                callback = kwargs["callback"]
                for index in range(1, 101):
                    callback(f"路人{index}")
                callback("阿英2")
                return [{"昵称": "阿英2"}]

            def SwitchToChat(self):
                pass

        class FakeBot:
            wx = FakeWeChat()

            def _load_contact_profiles_directory(self):
                return {"subjects": [], "maintenance": {}}, "ignored.json", "scope_rui"

            def _prepare_contact_directory_window(self):
                pass

            def _run_contact_auto_maintenance_collector(self, **_kwargs):
                return {
                    "ok": True,
                    "result": [{"昵称": "阿英2"}],
                    "callback_names": ["阿英2"],
                    "matched_name": "阿英2",
                }

        with (
            patch("feature.contacts.save_contact_directory"),
            patch("feature.contacts.load_contact_directory", return_value={"subjects": [], "maintenance": {}}),
            patch("feature.contacts.log", side_effect=lambda **kwargs: log_messages.append(kwargs.get("message", ""))),
            patch("feature.contacts.time.sleep"),
        ):
            result = refresh_contact_profiles_single_batch(FakeBot(), mode="standard", start_name="阿英2")

        read_logs = [message for message in log_messages if "已读取联系人" in message]
        self.assertEqual(read_logs, [])
        self.assertEqual(result["callback_names"], ["阿英2"])

    def test_standard_callback_finishes_current_batch_when_pause_requested(self):
        calls = []
        state = {"directory": {"subjects": [], "maintenance": {}}}

        class FakeWeChat:
            def GetFriendDetails(self, **kwargs):
                state["directory"]["maintenance"]["paused"] = True
                calls.append(("callback_return", kwargs["callback"]({"昵称": "阿英2"})))
                return [{"昵称": "阿英2"}]

            def SwitchToChat(self):
                pass

        class FakeBot:
            wx = FakeWeChat()

            def _load_contact_profiles_directory(self):
                return {"subjects": [], "maintenance": {}}, "ignored.json", "scope_rui"

            def _prepare_contact_directory_window(self):
                pass

            def _run_contact_auto_maintenance_collector(self, **_kwargs):
                state["directory"]["maintenance"]["paused"] = True
                return {
                    "ok": True,
                    "result": [{"昵称": "阿英2"}],
                    "callback_names": ["阿英2"],
                }

        with (
            patch("feature.contacts.save_contact_directory"),
            patch("feature.contacts.load_contact_directory", side_effect=lambda *_args, **_kwargs: state["directory"]),
        ):
            result = refresh_contact_profiles_single_batch(FakeBot(), mode="standard")

        self.assertTrue(result["stopped_early"])
        self.assertFalse(result["completed"])
        self.assertEqual(result["stopped_reason"], "paused")

    def test_pause_only_marks_state_without_interrupting_current_batch(self):
        calls = []
        saved = {}

        class FakeBot:
            wx = object()

            def _load_contact_profiles_directory(self):
                return saved.get("directory", {"maintenance": {}}), "ignored.json", "scope_rui"

        def fake_save(_path, directory):
            saved["directory"] = directory

        with patch("feature.contacts.save_contact_directory", side_effect=fake_save):
            bot = FakeBot()
            bot._contact_profiles_reading_active = True
            set_contact_profiles_paused(bot, True)

        self.assertEqual(calls, [])
        self.assertTrue(saved["directory"]["maintenance"]["paused"])
        self.assertEqual(saved["directory"]["maintenance"]["status"], "paused")

    def test_resume_only_clears_paused_state(self):
        saved = {}

        class FakeBot:
            wx = object()

            def _load_contact_profiles_directory(self):
                return saved.get("directory", {"maintenance": {}}), "ignored.json", "scope_rui"

        def fake_save(_path, directory):
            saved["directory"] = directory

        with patch("feature.contacts.save_contact_directory", side_effect=fake_save):
            bot = FakeBot()
            set_contact_profiles_paused(bot, True)
            set_contact_profiles_paused(bot, False)

        self.assertFalse(saved["directory"]["maintenance"]["paused"])
        self.assertEqual(saved["directory"]["maintenance"]["status"], "idle")

    def test_pause_preserves_contact_fields_added_after_initial_load(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "contacts.json")
            initial = {
                "wx_id": "scope_rui",
                "subjects": [{"contact_key": "k1", "nickname": "阿英2", "tags": []}],
                "maintenance": {},
            }
            save_contact_directory(path, initial)

            class FakeBot:
                wx = object()

                def _load_contact_profiles_directory(self):
                    return load_contact_directory(path, wx_id="scope_rui"), path, "scope_rui"

            stale, _path, _wx_id = FakeBot()._load_contact_profiles_directory()
            latest = load_contact_directory(path, wx_id="scope_rui")
            latest["subjects"][0]["tags"] = ["关系扫描新标签"]
            save_contact_directory(path, latest)

            with patch.object(FakeBot, "_load_contact_profiles_directory", side_effect=[
                (stale, path, "scope_rui"),
                (latest, path, "scope_rui"),
            ]):
                set_contact_profiles_paused(FakeBot(), True)

            saved = load_contact_directory(path, wx_id="scope_rui")
            self.assertEqual(saved["subjects"][0]["tags"], ["关系扫描新标签"])
            self.assertTrue(saved["maintenance"]["paused"])

    def test_edit_friend_info_requires_explicit_success(self):
        calls = []

        class FakeWeChat:
            def ChatWith(self, who, exact=True):
                calls.append(("ChatWith", who, exact))

            def ChatInfo(self):
                calls.append(("ChatInfo",))
                return {"chat_type": "friend", "chat_name": "阿英2"}

            def EditFriendInfo(self, **kwargs):
                calls.append(("EditFriendInfo", kwargs))
                return {}

        class FakeBot:
            wx = FakeWeChat()

            def __init__(self):
                self._ui_owner = _contact_owner_for(self.wx)

        with self.assertRaisesRegex(RuntimeError, "未返回明确成功"):
            edit_friend_info_via_chat_profile(
                FakeBot(),
                "阿英2",
                expected_names={"阿英2"},
                add_tags=["付费用户"],
            )

        self.assertEqual(calls[0], ("ChatWith", "阿英2", True))
        self.assertEqual(calls[1], ("ChatInfo",))

    def test_edit_friend_info_rejects_missing_owner_before_ui(self):
        calls = []
        bot = SimpleNamespace(
            wx=SimpleNamespace(ChatWith=lambda *_args, **_kwargs: calls.append("ChatWith")),
            _ui_owner=None,
        )

        with self.assertRaisesRegex(RuntimeError, "只能由微信 UI owner"):
            edit_friend_info_via_chat_profile(bot, "阿英2", add_tags=["付费用户"])

        self.assertEqual(calls, [])

    def test_modify_friend_tags_uses_generic_chat_profile_path(self):
        calls = []

        class FakeWeChat:
            def ChatWith(self, who, exact=True):
                calls.append(("ChatWith", who, exact))

            def ChatInfo(self):
                calls.append(("ChatInfo",))
                return {"chat_type": "friend", "chat_name": "阿英2"}

            def EditFriendInfo(self, **kwargs):
                calls.append(("EditFriendInfo", kwargs))
                return {"status": "成功", "message": None, "data": None}

        class FakeBot:
            wx = FakeWeChat()

            def __init__(self):
                self._ui_owner = _contact_owner_for(self.wx)

        result = modify_friend_tags_via_chat_profile(
            FakeBot(),
            [{"name": "阿英2"}],
            add_tags=["付费用户"],
        )

        self.assertEqual(result["status"], "success")
        self.assertEqual(result["success_count"], 1)
        self.assertEqual(calls[0], ("ChatWith", "阿英2", True))
        self.assertEqual(calls[1], ("ChatInfo",))
        self.assertEqual(calls[2][0], "EditFriendInfo")
        self.assertEqual(calls[2][1]["add_tags"], ["付费用户"])

    def test_relationship_tag_failure_does_not_rebind_wechat_client(self):
        owner = Mock()
        owner.call.side_effect = RuntimeError("标签页面暂时不可用")
        bot = SimpleNamespace(wx=object(), _ui_owner=owner)

        with patch("core.wechat_window.rebind_wechat_client") as rebind:
            result = modify_friend_tags_via_chat_profile(
                bot,
                [{"name": "阿英2"}],
                add_tags=["删除我的人"],
                log_prefix="[关系扫描]",
                rebind_attempts=1,
            )

        self.assertEqual(result["status"], "failed")
        self.assertIn("标签页面暂时不可用", result["message"])
        owner.call.assert_called_once()
        rebind.assert_not_called()

    def test_modify_friend_tags_closes_dynamic_listener_after_success(self):
        calls = []

        class FakeWeChat:
            def ChatWith(self, who, exact=True):
                calls.append(("ChatWith", who, exact))

            def ChatInfo(self):
                calls.append(("ChatInfo",))
                return {"chat_type": "friend", "chat_name": "阿英2"}

            def EditFriendInfo(self, **kwargs):
                calls.append(("EditFriendInfo", kwargs))
                return {"status": "成功", "message": None, "data": None}

        class FakeBot:
            wx = FakeWeChat()

            def __init__(self):
                self._ui_owner = _contact_owner_for(self.wx)
                self.all_Mode_listen_list = [["阿英2", 1]]

            def _close_dynamic_listener_subwindows(self, names):
                calls.append(("CloseDynamic", list(names)))
                self.all_Mode_listen_list.clear()
                return ["阿英2"]

        result = modify_friend_tags_via_chat_profile(
            FakeBot(),
            [{"name": "阿英2"}],
            add_tags=["付费用户"],
            log_prefix="[关系扫描]",
        )

        self.assertEqual(result["status"], "success")
        self.assertIn(("CloseDynamic", ["阿英2"]), calls)

    def test_modify_friend_tags_treats_noop_as_success_without_retry(self):
        calls = []

        class FakeWeChat:
            def ChatWith(self, who, exact=True):
                calls.append(("ChatWith", who, exact))

            def ChatInfo(self):
                calls.append(("ChatInfo",))
                return {"chat_type": "friend", "chat_name": "阿英2"}

            def EditFriendInfo(self, **kwargs):
                calls.append(("EditFriendInfo", kwargs))
                return {"status": "失败", "message": "未进行任何修改", "data": None}

        class FakeBot:
            wx = FakeWeChat()

            def __init__(self):
                self._ui_owner = _contact_owner_for(self.wx)

        result = modify_friend_tags_via_chat_profile(
            FakeBot(),
            [{"name": "阿英2"}],
            add_tags=["删除我的人"],
        )

        edit_calls = [item for item in calls if item[0] == "EditFriendInfo"]
        self.assertEqual(result["status"], "success")
        self.assertEqual(result["success_count"], 1)
        self.assertEqual(result["records"][0]["response"]["noop"], True)
        self.assertEqual(len(edit_calls), 1)

    def test_modify_friend_tags_skips_edit_when_chat_info_tags_already_match(self):
        calls = []

        class FakeWeChat:
            def ChatWith(self, who, exact=True):
                calls.append(("ChatWith", who, exact))

            def ChatInfo(self):
                calls.append(("ChatInfo",))
                return {"chat_type": "friend", "chat_name": "阿英2", "标签": "删除我的人"}

            def EditFriendInfo(self, **kwargs):
                calls.append(("EditFriendInfo", kwargs))
                return {"status": "成功", "message": None, "data": None}

        class FakeBot:
            wx = FakeWeChat()

            def __init__(self):
                self._ui_owner = _contact_owner_for(self.wx)

        result = modify_friend_tags_via_chat_profile(
            FakeBot(),
            [{"name": "阿英2"}],
            add_tags=["删除我的人"],
        )

        self.assertEqual(result["status"], "success")
        self.assertEqual(result["success_count"], 1)
        self.assertEqual(result["records"][0]["response"]["noop"], True)
        self.assertNotIn("EditFriendInfo", [item[0] for item in calls])

if __name__ == "__main__":
    unittest.main()
