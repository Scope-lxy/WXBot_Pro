import unittest
from datetime import datetime
from unittest.mock import patch

from feature.contacts import check_contact_directory_auto_maintenance
from feature.contacts import edit_friend_info_via_chat_profile
from feature.contacts import modify_friend_tags_via_chat_profile
from feature.contacts import prepare_contact_directory_window
from feature.contacts import repair_contact_profile_remarks
from feature.contacts import refresh_contact_profiles_batch
from feature.contacts import refresh_contact_profiles_single_batch


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
            contact_directory_auto_maintenance_batch_size = 10

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

    def test_auto_maintenance_waits_for_pending_lightweight_queue(self):
        bot = self._auto_maintenance_bot(pending_queue=True)
        with patch("feature.contacts.is_contact_directory_auto_maintenance_idle", return_value=True):
            result = check_contact_directory_auto_maintenance(bot, now=datetime(2026, 6, 10, 21, 0, 0))

        self.assertFalse(result)
        self.assertIn(("flush_lightweight",), bot.calls)
        self.assertFalse(any(call[0] == "refresh_batch" for call in bot.calls))

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

        with patch("feature.contacts.close_contact_directory_management_windows", return_value=0) as mock_close:
            prepare_contact_directory_window(FakeBot())

        self.assertEqual(calls, ["SwitchToContact"])
        mock_close.assert_not_called()

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

        with (
            patch("feature.contacts.close_contact_directory_management_windows", return_value=0) as mock_close,
            patch("core.wechat_window.rebind_wechat_client", side_effect=fake_rebind),
        ):
            prepare_contact_directory_window(FakeBot())

        self.assertEqual(calls, ["broken", "rebind", "healthy"])
        mock_close.assert_not_called()

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
        self.assertTrue(result["completed"])
        self.assertEqual(result["analysis"]["outcome"], "full_scan_complete")

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

        result = refresh_contact_profiles_batch(FakeBot(), mode="force", run_to_completion=True)

        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0]["mode"], "force")
        self.assertTrue(result["completed"])
        self.assertEqual(result["stopped_reason"], "directory_complete")
        self.assertTrue(result["directory"]["maintenance"]["last_full_scan_completed_at"])

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
            patch("feature.contacts.close_contact_directory_management_windows", return_value=0),
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

        with (
            patch("feature.contacts.close_contact_directory_management_windows", return_value=0),
            patch("feature.contacts.bring_wechat_to_front", return_value=1),
        ):
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

        with (
            patch("feature.contacts.close_contact_directory_management_windows", return_value=0),
            patch("feature.contacts.bring_wechat_to_front", return_value=1),
        ):
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

if __name__ == "__main__":
    unittest.main()
