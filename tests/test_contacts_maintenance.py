import unittest
import json
import tempfile
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from feature.contacts import analyze_refresh_batch
from feature.contacts import auto_maintenance_is_due
from feature.contacts import contact_auto_maintenance_read_timeout_seconds
from feature.contacts import check_contact_directory_auto_maintenance
from feature.contacts import edit_friend_info_via_chat_profile
from feature.contacts import has_active_contact_maintenance_conflict
from feature.contacts import modify_friend_tags_via_chat_profile
from feature.contacts import prepare_contact_directory_window
from feature.contacts import repair_contact_profile_remarks
from feature.contacts import normalize_auto_maintenance_batch_size
from feature.contacts import refresh_batch_settings
from feature.contacts import refresh_contact_profiles_batch
from feature.contacts import refresh_contact_profiles_single_batch
from feature.contacts import set_contact_profiles_paused
from web_server import (
    _contact_profiles_browser_contacts,
    _chat_memory_user_sort_key,
    _wechat_name_sort_key,
    app,
    memory_chats,
)
from web_server import _contact_profiles_summary


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
                    "subject_type": "friend",
                    "status": "active",
                    "contact_key": "1",
                    "nickname": "王玉芹",
                    "remark": "A0-72王玉芹",
                    "wechat_id": "w1",
                },
                {
                    "subject_type": "friend",
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
                    "subject_type": "friend",
                    "status": "active",
                    "contact_key": "blank",
                    "nickname": "",
                    "remark": "",
                    "wechat_id": "",
                },
                {
                    "subject_type": "friend",
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
            base = Path(temp_dir) / "accounts" / "wx_test" / "memory"
            for storage_name, display_name in [
                ("b", "B-吴岳英"),
                ("n", "112"),
                ("a", "A0-努力"),
            ]:
                chat_dir = base / storage_name
                chat_dir.mkdir(parents=True)
                (chat_dir / "name.json").write_text(
                    json.dumps({"name": display_name}, ensure_ascii=False),
                    encoding="utf-8",
                )
                (chat_dir / f"{storage_name}_memory.json").write_text(
                    json.dumps([{"content": "hello"}], ensure_ascii=False),
                    encoding="utf-8",
                )

            with patch("web_server._account_memory_dir", return_value=str(base)):
                with app.test_request_context("/memory/chats/wx_test"):
                    response = memory_chats.__wrapped__("wx_test")

            payload = response.get_json()

        self.assertEqual([item["name"] for item in payload["chats"]], ["112", "A0-努力", "B-吴岳英"])

    def test_memory_chats_endpoint_hides_empty_record_dirs(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            base = Path(temp_dir) / "accounts" / "wx_test" / "memory"
            empty_dir = base / "empty"
            full_dir = base / "full"
            empty_dir.mkdir(parents=True)
            full_dir.mkdir(parents=True)
            (empty_dir / "name.json").write_text(json.dumps({"name": "空记录"}, ensure_ascii=False), encoding="utf-8")
            (empty_dir / "empty_memory.json").write_text("[]", encoding="utf-8")
            (full_dir / "name.json").write_text(json.dumps({"name": "112"}, ensure_ascii=False), encoding="utf-8")
            (full_dir / "full_memory.json").write_text(
                json.dumps([{"content": "hello"}], ensure_ascii=False),
                encoding="utf-8",
            )

            with patch("web_server._account_memory_dir", return_value=str(base)):
                with app.test_request_context("/memory/chats/wx_test"):
                    response = memory_chats.__wrapped__("wx_test")

            payload = response.get_json()

        self.assertEqual([item["name"] for item in payload["chats"]], ["112"])


class ContactMaintenancePrepareTests(unittest.TestCase):
    def _auto_maintenance_bot(self, *, pending_queue=False):
        calls = []

        class FreeLock:
            def acquire(self, blocking=True):
                calls.append(("lock_acquire", blocking))
                return True

            def release(self):
                calls.append(("lock_release",))

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
                self._lightweight_send_queue = {"阿英2": {"actions": []}} if pending_queue else {}
                self._lightweight_send_queue_lock = FreeLock()

            def _load_contact_profiles_directory(self):
                return {"maintenance": {}}, "ignored.json", "scope_rui"

            def _get_wechat_action_lock(self):
                return FreeLock()

            def _flush_lightweight_send_queue(self):
                calls.append(("flush_lightweight",))
                if not pending_queue:
                    self._lightweight_send_queue.clear()
                return not pending_queue

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
        bot = self._auto_maintenance_bot(pending_queue=False)
        with patch("feature.contacts.is_contact_directory_auto_maintenance_idle", return_value=True):
            result = check_contact_directory_auto_maintenance(bot, now=datetime(2026, 6, 10, 21, 0, 0))

        self.assertTrue(result)
        self.assertIn(("flush_lightweight",), bot.calls)
        self.assertTrue(any(call[0] == "refresh_batch" for call in bot.calls))
        success_updates = [call[1] for call in bot.calls if call[0] == "write_cycle"][-1]
        self.assertEqual(success_updates["auto_cycle_next_start_name"], "B")
        self.assertEqual(success_updates["auto_cycle_backup_start_name"], "A")

    def test_auto_maintenance_local_snapshot_skips_batch_cursor_and_wechat_lock(self):
        bot = self._auto_maintenance_bot(pending_queue=False)
        bot._local_wechat_reader_enabled = True
        saved = []
        local_contacts = [
            {
                "微信号": "aying2",
                "wxid": "wxid_aying2",
                "昵称": "阿英2",
                "备注": "A0-阿英2",
                "wechat_id": "aying2",
                "nickname": "阿英2",
                "remark": "A0-阿英2",
            }
        ]

        bot._save_contact_profiles_directory = lambda directory: saved.append(directory)
        with (
            patch("feature.contacts.is_contact_directory_auto_maintenance_idle", return_value=True),
            patch("feature.contacts.read_local_contacts_with_status") as read_local,
            patch("feature.contacts.save_contact_directory"),
            patch("feature.contacts.load_contact_directory", return_value={"subjects": [], "maintenance": {}}),
        ):
            read_local.return_value = SimpleNamespace(ok=True, items=local_contacts, error="")
            result = check_contact_directory_auto_maintenance(bot, now=datetime(2026, 6, 10, 21, 0, 0))

        self.assertTrue(result)
        self.assertEqual(read_local.call_args.kwargs["limit"], 10000)
        self.assertFalse(any(call[0] == "refresh_batch" for call in bot.calls))
        self.assertFalse(any(call == ("lock_acquire", False) for call in bot.calls))
        self.assertEqual(saved[-1]["maintenance"]["auto_cycle_status"], "completed")

    def test_auto_maintenance_local_snapshot_uses_6000_second_interval(self):
        bot = self._auto_maintenance_bot(pending_queue=False)
        bot._local_wechat_reader_enabled = True
        bot._load_contact_profiles_directory = lambda: (
            {"maintenance": {"last_local_snapshot_completed_at": "2026-06-10 20:00:01"}},
            "ignored.json",
            "scope_rui",
        )

        with patch("feature.contacts.read_local_contacts_with_status") as read_local:
            result = check_contact_directory_auto_maintenance(bot, now=datetime(2026, 6, 10, 21, 40, 0))

        self.assertFalse(result)
        read_local.assert_not_called()
        self.assertFalse(any(call[0] == "refresh_batch" for call in bot.calls))

    def test_auto_maintenance_local_snapshot_failure_does_not_fallback_to_ui(self):
        bot = self._auto_maintenance_bot(pending_queue=False)
        bot._local_wechat_reader_enabled = True

        with (
            patch("feature.contacts.is_contact_directory_auto_maintenance_idle", return_value=True),
            patch("feature.contacts.read_local_contacts_with_status") as read_local,
        ):
            read_local.return_value = SimpleNamespace(ok=False, items=[], error="cli boom")
            result = check_contact_directory_auto_maintenance(bot, now=datetime(2026, 6, 10, 21, 0, 0))

        self.assertFalse(result)
        self.assertFalse(any(call == ("lock_acquire", False) for call in bot.calls))
        self.assertFalse(any(call[0] == "refresh_batch" for call in bot.calls))

    def test_local_snapshot_exception_log_does_not_claim_ui_fallback(self):
        bot = self._auto_maintenance_bot(pending_queue=False)
        bot._local_wechat_reader_enabled = True
        log_messages = []

        local_contacts = [{
            "微信号": "aying2",
            "wxid": "wxid_aying2",
            "昵称": "阿英2",
            "备注": "A0-阿英2",
            "wechat_id": "aying2",
            "nickname": "阿英2",
            "remark": "A0-阿英2",
        }]

        with (
            patch("feature.contacts.is_contact_directory_auto_maintenance_idle", return_value=True),
            patch("feature.contacts.read_local_contacts_with_status", return_value=SimpleNamespace(ok=True, items=local_contacts, error="")),
            patch("feature.contacts.merge_contact_directory", side_effect=RuntimeError("merge boom")),
            patch("feature.contacts.log", side_effect=lambda **kwargs: log_messages.append(kwargs.get("message", ""))),
        ):
            result = check_contact_directory_auto_maintenance(bot, now=datetime(2026, 6, 10, 21, 0, 0))

        self.assertFalse(result)
        joined = "\n".join(log_messages)
        self.assertIn("本轮未使用微信界面回退", joined)
        self.assertNotIn("已回退微信界面读取", joined)

    def test_auto_maintenance_waits_for_active_private_pipeline(self):
        bot = self._auto_maintenance_bot(pending_queue=False)
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

    def test_auto_maintenance_waits_for_pending_lightweight_queue(self):
        bot = self._auto_maintenance_bot(pending_queue=True)
        with patch("feature.contacts.is_contact_directory_auto_maintenance_idle", return_value=True):
            result = check_contact_directory_auto_maintenance(bot, now=datetime(2026, 6, 10, 21, 0, 0))

        self.assertFalse(result)
        self.assertIn(("flush_lightweight",), bot.calls)
        self.assertFalse(any(call[0] == "refresh_batch" for call in bot.calls))

    def test_auto_maintenance_holds_wechat_lock_through_batch(self):
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

            def _get_wechat_action_lock(self):
                return lock

            def _flush_lightweight_send_queue(self):
                return True

            def _has_pending_lightweight_send_queue(self):
                return False

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

        self.assertIn(("batch_lock_held", True), calls)
        self.assertEqual(calls[0], ("lock_acquire", False))
        self.assertEqual(calls[-1], ("lock_release",))

    def test_auto_maintenance_falls_back_to_backup_cursor_after_primary_stalls(self):
        calls = []

        class FreeLock:
            def acquire(self, blocking=True):
                return True

            def release(self):
                pass

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
                        "auto_cycle_backup_start_name": "备用游标",
                        "auto_cycle_retry_count": 0,
                        "last_attempted_at": "2026-06-10 20:00:00",
                    }
                }, "ignored.json", "scope_rui"

            def _get_wechat_action_lock(self):
                return FreeLock()

            def _flush_lightweight_send_queue(self):
                return True

            def _has_pending_lightweight_send_queue(self):
                return False

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

    def test_auto_maintenance_resets_when_primary_cursor_stalls_without_backup(self):
        calls = []

        class FreeLock:
            def acquire(self, blocking=True):
                return True

            def release(self):
                pass

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
                        "auto_cycle_backup_start_name": "",
                        "auto_cycle_retry_count": 0,
                        "last_attempted_at": "2026-06-10 20:00:00",
                    }
                }, "ignored.json", "scope_rui"

            def _get_wechat_action_lock(self):
                return FreeLock()

            def _flush_lightweight_send_queue(self):
                return True

            def _has_pending_lightweight_send_queue(self):
                return False

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

        class FreeLock:
            def acquire(self, blocking=True):
                return True

            def release(self):
                pass

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
                        "auto_cycle_backup_start_name": "备用游标",
                        "auto_cycle_retry_count": 0,
                        "last_attempted_at": "2026-06-10 20:00:00",
                    }
                }, "ignored.json", "scope_rui"

            def _get_wechat_action_lock(self):
                return FreeLock()

            def _flush_lightweight_send_queue(self):
                return True

            def _has_pending_lightweight_send_queue(self):
                return False

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
                    "backup_start_name": "新备用游标",
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

    def _tail_confirmation_updates(self, retry_count):
        calls = []

        class FreeLock:
            def acquire(self, blocking=True):
                return True

            def release(self):
                pass

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
                        "auto_cycle_last_outcome": "short_advanced" if retry_count == 0 else "tail_confirm_pending",
                        "auto_cycle_retry_count": retry_count,
                        "last_attempted_at": "2026-06-10 20:00:00",
                    }
                }, "ignored.json", "scope_rui"

            def _get_wechat_action_lock(self):
                return FreeLock()

            def _flush_lightweight_send_queue(self):
                return True

            def _has_pending_lightweight_send_queue(self):
                return False

            def refresh_contact_profiles_batch(self, **kwargs):
                calls.append(("refresh_batch", kwargs))
                return {
                    "directory": {
                        "maintenance": {
                            "auto_cycle_retry_count": retry_count,
                            "auto_cycle_batches_completed": 8,
                        }
                    },
                    "analysis": {"outcome": "not_advanced", "completed": False},
                    "next_start_name": "最后联系人",
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

        return [call[1] for call in calls if call[0] == "write_cycle"][-1]

    def test_auto_maintenance_waits_before_confirming_short_batch_tail(self):
        final_updates = self._tail_confirmation_updates(retry_count=0)

        self.assertEqual(final_updates["auto_cycle_status"], "running")
        self.assertEqual(final_updates["auto_cycle_last_outcome"], "tail_confirm_pending")
        self.assertEqual(final_updates["auto_cycle_next_start_name"], "最后联系人")
        self.assertEqual(final_updates["auto_cycle_retry_count"], 1)

    def test_auto_maintenance_confirms_completion_after_repeated_short_batch_stalls(self):
        final_updates = self._tail_confirmation_updates(retry_count=2)

        self.assertEqual(final_updates["auto_cycle_status"], "completed")
        self.assertEqual(final_updates["auto_cycle_last_outcome"], "completed")
        self.assertTrue(final_updates["last_full_scan_completed_at"])

    def test_auto_maintenance_empty_batch_does_not_confirm_short_batch_tail(self):
        calls = []

        class FreeLock:
            def acquire(self, blocking=True):
                return True

            def release(self):
                pass

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

            def _get_wechat_action_lock(self):
                return FreeLock()

            def _flush_lightweight_send_queue(self):
                return True

            def _has_pending_lightweight_send_queue(self):
                return False

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

        class FreeLock:
            def acquire(self, blocking=True):
                return True

            def release(self):
                pass

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

            def _get_wechat_action_lock(self):
                return FreeLock()

            def _flush_lightweight_send_queue(self):
                return True

            def _has_pending_lightweight_send_queue(self):
                return False

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

    def test_prepare_rebinds_and_retries_after_switch_failure(self):
        calls = []

        class BrokenWeChat:
            def SwitchToContact(self):
                calls.append("broken")
                raise RuntimeError("missing contact tab")

        class HealthyWeChat:
            def SwitchToContact(self):
                calls.append("healthy")

        class FakeBot:
            wx = BrokenWeChat()

        def fake_rebind(bot):
            calls.append("rebind")
            bot.wx = HealthyWeChat()
            return bot.wx

        with patch("core.wechat_window.rebind_wechat_client", side_effect=fake_rebind):
            prepare_contact_directory_window(FakeBot())

        self.assertEqual(calls, ["broken", "rebind", "healthy"])

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

    def test_auto_maintenance_batch_size_policy(self):
        self.assertEqual(normalize_auto_maintenance_batch_size(20), 20)
        self.assertEqual(normalize_auto_maintenance_batch_size(50), 50)
        self.assertEqual(normalize_auto_maintenance_batch_size(80), 80)
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

    def test_auto_maintenance_read_timeout_is_ten_minutes(self):
        self.assertEqual(contact_auto_maintenance_read_timeout_seconds(50), 600)

    def test_contact_summary_continue_start_uses_existing_contact_tail(self):
        summary = _contact_profiles_summary({
            "maintenance": {"next_start_name": "旧游标"},
            "subjects": [
                {"subject_type": "friend", "status": "active", "send_name": "阿英2"},
                {"subject_type": "friend", "status": "missing", "send_name": "阿英3"},
                {"subject_type": "friend", "status": "active", "remark": "阿英4"},
            ],
        })

        self.assertEqual(summary["continue_start_name"], "阿英4")
        self.assertNotIn("next_start_name", summary)

    def test_force_refresh_runs_single_full_get_friend_details_call(self):
        calls = []

        class FakeLock:
            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc, tb):
                return False

        class FakeWeChat:
            def GetFriendDetails(self, **kwargs):
                calls.append(("GetFriendDetails", kwargs))
                kwargs["callback"]({"昵称": "阿英2"})
                kwargs["callback"]({"备注": "阿英3"})
                return [{"昵称": "阿英2"}, {"昵称": "阿英3"}]

            def SwitchToChat(self):
                calls.append(("SwitchToChat",))

        class FakeBot:
            wx = FakeWeChat()

            def _get_wechat_action_lock(self):
                return FakeLock()

            def _load_contact_profiles_directory(self):
                return {"subjects": [], "maintenance": {}}, "ignored.json", "scope_rui"

            def _prepare_contact_directory_window(self):
                calls.append(("prepare",))

        with (
            patch("feature.contacts.save_contact_directory"),
            patch("feature.contacts.load_contact_directory", return_value={"subjects": [], "maintenance": {}}),
        ):
            result = refresh_contact_profiles_single_batch(FakeBot(), mode="force")

        get_calls = [call for call in calls if call[0] == "GetFriendDetails"]
        self.assertEqual(len(get_calls), 1)
        self.assertIsNone(get_calls[0][1]["n"])
        self.assertEqual(get_calls[0][1]["interval"], 0.5)
        self.assertIn("callback", get_calls[0][1])
        self.assertTrue(result["completed"])
        self.assertEqual(result["analysis"]["outcome"], "full_scan_complete")

    def test_auto_maintenance_single_batch_uses_ten_minute_timeout(self):
        calls = []

        class FakeLock:
            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc, tb):
                return False

        class FakeWeChat:
            def GetFriendDetails(self, **kwargs):
                calls.append(("GetFriendDetails", kwargs))
                detail = {"备注": "阿英2"}
                kwargs["callback"](detail)
                return [detail]

            def SwitchToChat(self):
                pass

        class FakeBot:
            wx = FakeWeChat()

            def _get_wechat_action_lock(self):
                return FakeLock()

            def _load_contact_profiles_directory(self):
                return {"subjects": [], "maintenance": {}}, "ignored.json", "scope_rui"

            def _prepare_contact_directory_window(self):
                pass

        with (
            patch("feature.contacts.save_contact_directory"),
            patch("feature.contacts.load_contact_directory", return_value={"subjects": [], "maintenance": {}}),
        ):
            refresh_contact_profiles_single_batch(FakeBot(), mode="standard", run_kind="auto_maintenance")

        get_calls = [call for call in calls if call[0] == "GetFriendDetails"]
        self.assertEqual(get_calls[0][1]["timeout"], 600)

    def test_single_batch_prefers_local_contacts_without_wechat_ui(self):
        calls = []

        class FakeLock:
            def acquire(self, blocking=True):
                calls.append(("lock_acquire", blocking))
                return False

            def release(self):
                calls.append(("lock_release",))

        class FakeWeChat:
            def GetFriendDetails(self, **kwargs):
                calls.append(("GetFriendDetails", kwargs))
                return []

            def SwitchToChat(self):
                calls.append(("SwitchToChat",))

        class FakeBot:
            wx = FakeWeChat()
            _local_wechat_reader_enabled = True

            def _get_wechat_action_lock(self):
                return FakeLock()

            def _load_contact_profiles_directory(self):
                return {"subjects": [], "maintenance": {}}, "ignored.json", "scope_rui"

            def _prepare_contact_directory_window(self):
                calls.append(("prepare",))

        local_contacts = [
            {
                "微信号": "aying2",
                "wxid": "wxid_aying2",
                "昵称": "阿英2",
                "备注": "A0-阿英2",
                "wechat_id": "aying2",
                "nickname": "阿英2",
                "remark": "A0-阿英2",
            }
        ]

        with (
            patch("feature.contacts.read_local_contacts_with_status") as read_local,
            patch("feature.contacts.save_contact_directory"),
            patch("feature.contacts.load_contact_directory", return_value={"subjects": [], "maintenance": {}}),
        ):
            read_local.return_value = SimpleNamespace(ok=True, items=local_contacts, error="")
            result = refresh_contact_profiles_single_batch(FakeBot(), mode="standard")

        self.assertTrue(result["completed"])
        self.assertEqual(read_local.call_args.kwargs["limit"], 10000)
        self.assertEqual(result["analysis"]["outcome"], "local_full_scan_complete")
        self.assertEqual(result["next_start_name"], "")
        self.assertEqual([call[0] for call in calls], [])
        raw_detail = result["directory"]["subjects"][0]["raw_detail"]
        self.assertNotIn("source", raw_detail)

    def test_contact_read_logs_from_callback_without_duplicate_result_logs(self):
        log_messages = []

        class FakeLock:
            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc, tb):
                return False

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

            def _get_wechat_action_lock(self):
                return FakeLock()

            def _load_contact_profiles_directory(self):
                return {"subjects": [], "maintenance": {}}, "ignored.json", "scope_rui"

            def _prepare_contact_directory_window(self):
                pass

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

        class FakeLock:
            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc, tb):
                return False

        class FakeWeChat:
            def GetFriendDetails(self, **kwargs):
                detail = {"备注": "阿英2"}
                kwargs["callback"](detail)
                return [detail]

            def SwitchToChat(self):
                pass

        class FakeBot:
            wx = FakeWeChat()

            def _get_wechat_action_lock(self):
                return FakeLock()

            def _load_contact_profiles_directory(self):
                return {"subjects": [], "maintenance": {}}, "ignored.json", "scope_rui"

            def _prepare_contact_directory_window(self):
                pass

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

        class FakeLock:
            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc, tb):
                return False

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

            def _get_wechat_action_lock(self):
                return FakeLock()

            def _load_contact_profiles_directory(self):
                return {"subjects": [], "maintenance": {}}, "ignored.json", "scope_rui"

            def _prepare_contact_directory_window(self):
                pass

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

    def test_force_callback_stops_when_pause_requested(self):
        calls = []
        state = {"directory": {"subjects": [], "maintenance": {}}}

        class FakeLock:
            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc, tb):
                return False

        class FakeWeChat:
            def GetFriendDetails(self, **kwargs):
                state["directory"]["maintenance"]["paused"] = True
                calls.append(("callback_return", kwargs["callback"]({"昵称": "阿英2"})))
                return [{"昵称": "阿英2"}]

            def SwitchToChat(self):
                pass

        class FakeBot:
            wx = FakeWeChat()

            def _get_wechat_action_lock(self):
                return FakeLock()

            def _load_contact_profiles_directory(self):
                return {"subjects": [], "maintenance": {}}, "ignored.json", "scope_rui"

            def _prepare_contact_directory_window(self):
                pass

        with (
            patch("feature.contacts.save_contact_directory"),
            patch("feature.contacts.load_contact_directory", side_effect=lambda *_args, **_kwargs: state["directory"]),
        ):
            result = refresh_contact_profiles_single_batch(FakeBot(), mode="force")

        self.assertIn(("callback_return", False), calls)
        self.assertTrue(result["stopped_early"])
        self.assertFalse(result["completed"])
        self.assertEqual(result["stopped_reason"], "paused")

    def test_force_run_to_completion_uses_single_batch(self):
        calls = []

        class FakeBot:
            def _refresh_contact_profiles_single_batch(self, **kwargs):
                calls.append(kwargs)
                return {
                    "count_returned": 2,
                    "read_item_count": 2,
                    "analysis": {"outcome": "full_scan_complete", "completed": True},
                    "directory": {"maintenance": {"status": "idle"}},
                }

            def _save_contact_profiles_directory(self, _directory):
                pass

        result = refresh_contact_profiles_batch(
            FakeBot(),
            mode="force",
            start_name="阿英2",
            use_saved_position=True,
            count_override=9,
            run_to_completion=True,
        )

        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0]["mode"], "force")
        self.assertEqual(calls[0]["start_name"], "")
        self.assertFalse(calls[0]["use_saved_position"])
        self.assertIsNone(calls[0]["count_override"])
        self.assertTrue(result["completed"])
        self.assertEqual(result["stopped_reason"], "directory_complete")
        self.assertTrue(result["directory"]["maintenance"]["last_full_scan_completed_at"])

    def test_force_run_to_completion_does_not_mark_paused_result_complete(self):
        calls = []

        class FakeBot:
            def _refresh_contact_profiles_single_batch(self, **kwargs):
                calls.append(kwargs)
                return {
                    "count_returned": 1,
                    "read_item_count": 1,
                    "stopped_early": True,
                    "stopped_reason": "paused",
                    "analysis": {"outcome": "full_scan_complete", "completed": True},
                    "directory": {"maintenance": {"status": "paused", "paused": True}},
                }

            def _save_contact_profiles_directory(self, _directory):
                raise AssertionError("paused full refresh must not be marked completed")

        result = refresh_contact_profiles_batch(FakeBot(), mode="force", run_to_completion=True)

        self.assertEqual(len(calls), 1)
        self.assertFalse(result["completed"])
        self.assertEqual(result["stopped_reason"], "paused")
        self.assertNotIn("last_full_scan_completed_at", result["directory"]["maintenance"])

    def test_pause_schedules_return_to_chat_after_delay(self):
        calls = []
        saved = {}

        class FakeTimer:
            def __init__(self, delay, callback):
                calls.append(("timer", delay))
                self.callback = callback
                self.daemon = False
                self.cancelled = False

            def start(self):
                calls.append(("timer_start", self.daemon))

            def cancel(self):
                self.cancelled = True
                calls.append(("timer_cancel",))

        class FakeWeChat:
            def SwitchToChat(self):
                calls.append(("SwitchToChat",))

        class FakeBot:
            wx = FakeWeChat()

            def _load_contact_profiles_directory(self):
                return saved.get("directory", {"maintenance": {}}), "ignored.json", "scope_rui"

        def fake_save(_path, directory):
            saved["directory"] = directory

        with (
            patch("feature.contacts.save_contact_directory", side_effect=fake_save),
            patch("feature.contacts.threading.Timer", FakeTimer),
            patch("feature.contacts.time.sleep"),
        ):
            bot = FakeBot()
            bot._contact_profiles_reading_active = True
            with patch("feature.contacts.click_wechat_main_window_chat_nav", side_effect=lambda: calls.append(("interrupt_click",)) or setattr(bot, "_contact_profiles_reading_active", False) or True):
                set_contact_profiles_paused(bot, True)
                timer = bot._contact_profiles_stop_return_timer
                timer.callback()

        self.assertIn(("timer", 0.6), calls)
        self.assertIn(("timer_start", True), calls)
        self.assertIn(("interrupt_click",), calls)
        self.assertEqual(calls.count(("interrupt_click",)), 1)
        self.assertNotIn(("SwitchToChat",), calls)

    def test_resume_cancels_pending_return_to_chat(self):
        calls = []
        saved = {}

        class FakeTimer:
            def __init__(self, _delay, _callback):
                self.daemon = False

            def start(self):
                calls.append(("timer_start",))

            def cancel(self):
                calls.append(("timer_cancel",))

        class FakeBot:
            wx = object()

            def _load_contact_profiles_directory(self):
                return saved.get("directory", {"maintenance": {}}), "ignored.json", "scope_rui"

        def fake_save(_path, directory):
            saved["directory"] = directory

        with (
            patch("feature.contacts.save_contact_directory", side_effect=fake_save),
            patch("feature.contacts.threading.Timer", FakeTimer),
        ):
            bot = FakeBot()
            set_contact_profiles_paused(bot, True)
            self.assertIsNotNone(bot._contact_profiles_stop_return_timer)
            set_contact_profiles_paused(bot, False)

        self.assertIn(("timer_cancel",), calls)
        self.assertIsNone(bot._contact_profiles_stop_return_timer)

    def test_repair_remarks_retries_single_contact_after_rebind(self):
        calls = []

        class BrokenWeChat:
            def ChatWith(self, who, exact=True):
                calls.append(("ChatWith", who, exact))
                raise RuntimeError("desktop busy")

            def EditFriendInfo(self, remark=None, **_kwargs):
                calls.append(("EditFriendInfo", remark))
                return {"status": "成功"}

        class HealthyWeChat:
            def ChatWith(self, who, exact=True):
                calls.append(("ChatWithRetry", who, exact))

            def EditFriendInfo(self, remark=None, **_kwargs):
                calls.append(("EditFriendInfoRetry", remark))
                return {"status": "成功"}

        class FakeBot:
            def __init__(self):
                self.wx = BrokenWeChat()
                self._wechat_action_lock = None

            def _get_wechat_action_lock(self):
                class DummyLock:
                    def __enter__(self_inner):
                        return self_inner

                    def __exit__(self_inner, exc_type, exc, tb):
                        return False

                return DummyLock()

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

        def fake_rebind(bot):
            calls.append(("rebind",))
            bot.wx = HealthyWeChat()
            return bot.wx

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
            patch("core.wechat_window.rebind_wechat_client", side_effect=fake_rebind),
        ):
            result = repair_contact_profile_remarks(FakeBot())

        self.assertEqual(result["success_count"], 1)
        self.assertEqual(result["failed_count"], 0)
        self.assertIn(("rebind",), calls)
        self.assertIn(("EditFriendInfoRetry", "阿英2_test"), calls)

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

        with patch("feature.contacts.bring_wechat_to_front", return_value=1):
            with self.assertRaisesRegex(RuntimeError, "未返回明确成功"):
                edit_friend_info_via_chat_profile(
                    FakeBot(),
                    "阿英2",
                    expected_names={"阿英2"},
                    add_tags=["付费用户"],
                )

        self.assertEqual(calls[0], ("ChatWith", "阿英2", True))
        self.assertEqual(calls[1], ("ChatInfo",))

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

        with patch("feature.contacts.bring_wechat_to_front", return_value=1):
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
                self.all_Mode_listen_list = [["阿英2", 1]]

            def _close_dynamic_listener_subwindows(self, names):
                calls.append(("CloseDynamic", list(names)))
                self.all_Mode_listen_list.clear()
                return ["阿英2"]

        with patch("feature.contacts.bring_wechat_to_front", return_value=1):
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

        with patch("feature.contacts.bring_wechat_to_front", return_value=1):
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

        with (
            patch("feature.contacts.bring_wechat_to_front", return_value=1),
            patch("feature.contacts.move_cursor_to_wechat_main_window_center", return_value=True),
        ):
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
