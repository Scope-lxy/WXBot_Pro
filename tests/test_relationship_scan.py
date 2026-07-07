import unittest
import tempfile
from datetime import datetime, timedelta
from types import SimpleNamespace
from feature.relationship_scan import (
    STATUS_BLOCKED,
    STATUS_DELETED,
    STATUS_NORMAL,
    SYNC_PENDING,
    TAG_BLOCKED,
    TAG_DELETED,
    check_auto_scan,
    clear_state,
    due_for_auto_scan,
    due_for_wechat_tag_sync,
    load_state,
    merge_state_into_contact_directory,
    normalize_settings,
    pending_sync_records,
    process_pending_wechat_tag_sync,
    relationship_scan_summary,
    relationship_status_from_preview,
    scan_current_sessions,
    save_state,
    scan_full_sessions,
    update_state_from_sessions,
)


class RelationshipScanTests(unittest.TestCase):
    def test_detects_blocked_deleted_and_normal_previews(self):
        self.assertEqual(
            relationship_status_from_preview("消息已发出，但被对方拒收了。"),
            STATUS_BLOCKED,
        )
        self.assertEqual(
            relationship_status_from_preview("阿英2开启了朋友验证，你还不是他朋友。"),
            STATUS_DELETED,
        )
        self.assertEqual(
            relationship_status_from_preview("姐姐，我今天比较忙"),
            STATUS_NORMAL,
        )

    def test_normal_preview_recovers_existing_abnormal_record(self):
        state = {
            "wx_id": "wxid_test",
            "records": [{
                "name": "阿英2",
                "status": STATUS_BLOCKED,
                "wechat_sync_status": "synced",
            }],
            "events": [],
        }
        updated = update_state_from_sessions(
            state,
            [{"name": "阿英2", "content": "姐姐，我今天比较忙"}],
            now=datetime(2026, 6, 11, 10, 0, 0),
        )
        record = updated["records"][0]
        self.assertEqual(record["status"], STATUS_NORMAL)
        self.assertEqual(record["wechat_sync_status"], SYNC_PENDING)
        self.assertEqual(updated["events"][0]["type"], "recovered")

    def test_missing_from_scan_does_not_recover(self):
        state = {
            "wx_id": "wxid_test",
            "records": [{
                "name": "阿英2",
                "status": STATUS_BLOCKED,
                "wechat_sync_status": "synced",
            }],
            "events": [],
        }
        updated = update_state_from_sessions(
            state,
            [{"name": "阿英3", "content": "普通消息"}],
            now=datetime(2026, 6, 11, 10, 0, 0),
        )
        self.assertEqual(updated["records"][0]["name"], "阿英2")
        self.assertEqual(updated["records"][0]["status"], STATUS_BLOCKED)

    def test_merge_state_into_contact_directory_updates_relation_tags(self):
        directory = {
            "wx_id": "wxid_test",
            "subjects": [{
                "subject_type": "friend",
                "contact_key": "remark:1",
                "remark": "阿英2",
                "nickname": "",
                "display_name": "阿英2",
                "send_name": "阿英2",
                "wechat_id": "",
                "tags": [TAG_BLOCKED],
                "warnings": [],
            }],
        }
        state = {
            "wx_id": "wxid_test",
            "records": [{
                "name": "阿英2",
                "status": STATUS_DELETED,
                "evidence": "阿英2开启了朋友验证，你还不是他朋友。",
            }],
        }
        updated, matched = merge_state_into_contact_directory(
            directory,
            state,
            now=datetime(2026, 6, 11, 10, 0, 0),
        )
        contact = updated["subjects"][0]
        self.assertIn("阿英2", matched)
        self.assertNotIn(TAG_BLOCKED, contact["tags"])
        self.assertIn(TAG_DELETED, contact["tags"])
        self.assertEqual(contact["relationship_status"], STATUS_DELETED)

    def test_merge_state_skips_ambiguous_duplicate_contact_names(self):
        directory = {
            "wx_id": "wxid_test",
            "subjects": [
                {
                    "subject_type": "friend",
                    "contact_key": "remark:1",
                    "remark": "张姐",
                    "display_name": "张姐",
                    "send_name": "张姐",
                    "tags": [],
                },
                {
                    "subject_type": "friend",
                    "contact_key": "remark:2",
                    "remark": "张姐",
                    "display_name": "张姐",
                    "send_name": "张姐",
                    "tags": [],
                },
            ],
        }
        state = {
            "wx_id": "wxid_test",
            "records": [{
                "name": "张姐",
                "status": STATUS_DELETED,
                "evidence": "张姐开启了朋友验证，你还不是她朋友。",
            }],
        }

        updated, matched = merge_state_into_contact_directory(directory, state)

        self.assertEqual(matched, {})
        self.assertNotIn("relationship_status", updated["subjects"][0])
        self.assertNotIn("relationship_status", updated["subjects"][1])

    def test_merge_state_uses_contact_key_for_duplicate_contact_names(self):
        directory = {
            "wx_id": "wxid_test",
            "subjects": [
                {
                    "subject_type": "friend",
                    "contact_key": "remark:1",
                    "remark": "张姐",
                    "display_name": "张姐",
                    "send_name": "张姐",
                    "tags": [],
                },
                {
                    "subject_type": "friend",
                    "contact_key": "remark:2",
                    "remark": "张姐",
                    "display_name": "张姐",
                    "send_name": "张姐",
                    "tags": [],
                },
            ],
        }
        state = {
            "wx_id": "wxid_test",
            "records": [{
                "name": "张姐",
                "contact_key": "remark:2",
                "status": STATUS_DELETED,
                "evidence": "张姐开启了朋友验证，你还不是她朋友。",
            }],
        }

        updated, matched = merge_state_into_contact_directory(directory, state)

        self.assertEqual(matched, {"张姐": "remark:2"})
        self.assertNotIn("relationship_status", updated["subjects"][0])
        self.assertEqual(updated["subjects"][1]["relationship_status"], STATUS_DELETED)

    def test_daily_summary_counts_events_and_pending_sync(self):
        state = {
            "wx_id": "wxid_test",
            "records": [{
                "name": "阿英2",
                "status": STATUS_BLOCKED,
                "wechat_sync_status": SYNC_PENDING,
            }],
            "events": [
                {"at": "2026-06-11T09:00:00", "type": "blocked", "name": "阿英2"},
                {"at": "2026-06-11T09:01:00", "type": "deleted", "name": "阿英3"},
                {"at": "2026-06-11T09:02:00", "type": "recovered", "name": "阿英4"},
                {"at": "2026-06-11T09:03:00", "type": "wechat_synced", "name": "阿英4"},
            ],
        }
        summary = relationship_scan_summary(state, now=datetime(2026, 6, 11, 10, 0, 0))
        self.assertEqual(summary["today_blocked"], 1)
        self.assertEqual(summary["today_deleted"], 1)
        self.assertEqual(summary["today_recovered"], 1)
        self.assertEqual(summary["wechat_synced_today"], 1)
        self.assertEqual(summary["wechat_pending"], 1)

    def test_clear_state_removes_records_events_and_pending_sync(self):
        state = {
            "wx_id": "wxid_test",
            "settings": {"auto_scan_enabled": True, "auto_sync_wechat_tags": True, "scan_interval_seconds": 20},
            "runtime": {
                "last_auto_scan_at": "2026-06-11T09:00:00",
                "last_scan_at": "2026-06-11T09:00:00",
                "last_scan_mode": "auto",
                "last_scan_count": 12,
            },
            "records": [{
                "name": "阿英2",
                "status": STATUS_BLOCKED,
                "wechat_sync_status": SYNC_PENDING,
            }],
            "events": [{"at": "2026-06-11T09:00:00", "type": "blocked", "name": "阿英2"}],
        }
        cleared = clear_state(state)
        summary = relationship_scan_summary(cleared, now=datetime(2026, 6, 11, 10, 0, 0))
        self.assertEqual(cleared["records"], [])
        self.assertEqual(cleared["events"], [])
        self.assertFalse(cleared["settings"]["auto_scan_enabled"])
        self.assertFalse(cleared["settings"]["auto_sync_wechat_tags"])
        self.assertEqual(cleared["settings"]["scan_interval_seconds"], 20)
        self.assertEqual(summary["last_scan_count"], 0)
        self.assertEqual(summary["last_scan_at"], "")
        self.assertEqual(summary["wechat_pending"], 0)

    def test_auto_scan_due_uses_configured_interval(self):
        now = datetime(2026, 6, 11, 10, 0, 0)
        state = {
            "settings": {"auto_scan_enabled": True, "scan_interval_seconds": 10},
            "runtime": {"last_auto_scan_at": (now - timedelta(seconds=11)).isoformat()},
        }
        self.assertTrue(due_for_auto_scan(state, now=now))
        state["runtime"]["last_auto_scan_at"] = (now - timedelta(seconds=5)).isoformat()
        self.assertFalse(due_for_auto_scan(state, now=now))

    def test_default_wechat_tag_sync_interval_is_ten_minutes(self):
        self.assertEqual(normalize_settings({})["sync_interval_minutes"], 10)

    def test_wechat_tag_sync_due_uses_configured_interval(self):
        now = datetime(2026, 6, 11, 10, 0, 0)
        state = {
            "settings": {"auto_sync_wechat_tags": True, "sync_interval_minutes": 10},
            "runtime": {"last_wechat_tag_sync_at": (now - timedelta(minutes=11)).isoformat()},
        }
        self.assertTrue(due_for_wechat_tag_sync(state, now=now))
        state["runtime"]["last_wechat_tag_sync_at"] = (now - timedelta(minutes=5)).isoformat()
        self.assertFalse(due_for_wechat_tag_sync(state, now=now))

    def test_pending_sync_records_skip_future_retry_and_prioritize_unattempted(self):
        now = datetime(2026, 6, 11, 10, 0, 0)
        state = {
            "records": [
                {
                    "name": "已尝试",
                    "status": STATUS_BLOCKED,
                    "wechat_sync_status": SYNC_PENDING,
                    "wechat_sync_attempted_at": "2026-06-11T09:00:00",
                },
                {
                    "name": "稍后重试",
                    "status": STATUS_DELETED,
                    "wechat_sync_status": SYNC_PENDING,
                    "wechat_sync_next_retry_at": "2026-06-11T10:05:00",
                },
                {
                    "name": "未尝试",
                    "status": STATUS_BLOCKED,
                    "wechat_sync_status": SYNC_PENDING,
                },
            ],
        }
        names = [record["name"] for record in pending_sync_records(state, now=now)]
        self.assertEqual(names, ["未尝试", "已尝试"])

    def test_auto_wechat_tag_sync_waits_for_pending_private_outbound_echo(self):
        calls = []

        class FakeLock:
            def acquire(self, blocking=True):
                calls.append("lock_acquire")
                return True

            def release(self):
                calls.append("lock_release")

        now = datetime(2026, 6, 11, 10, 0, 0)
        with tempfile.TemporaryDirectory() as tmp:
            state = {
                "wx_id": "wxid_test",
                "settings": {"auto_sync_wechat_tags": True, "sync_interval_minutes": 10},
                "records": [
                    {
                        "name": "阿英2",
                        "status": STATUS_BLOCKED,
                        "wechat_sync_status": SYNC_PENDING,
                    }
                ],
            }
            save_state(tmp, state)
            bot = SimpleNamespace(
                wx=object(),
                wx_id="wxid_test",
                config=SimpleNamespace(DATA_DIR=tmp),
                _get_wechat_action_lock=lambda: FakeLock(),
                _has_pending_private_outbound_echoes=lambda: True,
            )

            result = process_pending_wechat_tag_sync(bot, now=now)

        self.assertEqual(result, {"processed": 0, "success": 0, "failed": 0})
        self.assertEqual(calls, [])

    def test_same_status_scan_keeps_pending_retry_delay(self):
        state = {
            "wx_id": "wxid_test",
            "records": [{
                "name": "阿英2",
                "status": STATUS_BLOCKED,
                "wechat_sync_status": SYNC_PENDING,
                "wechat_sync_error": "上一轮失败",
                "wechat_sync_next_retry_at": "2026-06-11T10:10:00",
                "wechat_sync_attempted_at": "2026-06-11T10:00:00",
                "wechat_sync_retry_count": 1,
            }],
            "events": [],
        }
        updated = update_state_from_sessions(
            state,
            [{"name": "阿英2", "content": "消息已发出，但被对方拒收了。"}],
            now=datetime(2026, 6, 11, 10, 1, 0),
        )
        record = updated["records"][0]
        self.assertEqual(record["wechat_sync_error"], "上一轮失败")
        self.assertEqual(record["wechat_sync_next_retry_at"], "2026-06-11T10:10:00")
        self.assertEqual(record["wechat_sync_retry_count"], 1)

    def test_tag_sync_does_not_reenable_disabled_settings_after_action(self):
        calls = []

        class FakeLock:
            def acquire(self, blocking=True):
                return True

            def release(self):
                pass

        with tempfile.TemporaryDirectory() as tmp:
            state = {
                "wx_id": "wxid_test",
                "settings": {"auto_sync_wechat_tags": True, "sync_interval_minutes": 10},
                "records": [
                    {
                        "name": "阿英2",
                        "status": STATUS_DELETED,
                        "wechat_sync_status": SYNC_PENDING,
                    }
                ],
            }
            save_state(tmp, state)
            bot = SimpleNamespace(
                wx=object(),
                wx_id="wxid_test",
                config=SimpleNamespace(DATA_DIR=tmp),
                _get_wechat_action_lock=lambda: FakeLock(),
            )

            def fake_modify(_bot, targets, **_kwargs):
                calls.append(targets[0]["name"])
                latest = save_state(tmp, {
                    **state,
                    "settings": {"auto_sync_wechat_tags": False, "sync_interval_minutes": 10},
                })
                return {"status": "success", "payload": latest}

            from feature import relationship_scan

            original = relationship_scan.modify_friend_tags_via_chat_profile
            relationship_scan.modify_friend_tags_via_chat_profile = fake_modify
            try:
                result = process_pending_wechat_tag_sync(bot, now=datetime(2026, 6, 11, 10, 0, 0))
                latest_payload = relationship_scan.relationship_scan_payload(
                    relationship_scan.load_state(tmp, "wxid_test")
                )
            finally:
                relationship_scan.modify_friend_tags_via_chat_profile = original

        self.assertEqual(calls, ["阿英2"])
        self.assertFalse(latest_payload["settings"]["auto_sync_wechat_tags"])
        self.assertEqual(result["processed"], 0)


    def test_full_scan_returns_session_list_to_top_after_finish(self):
        calls = []

        class FakeLock:
            def __enter__(self):
                calls.append("lock_enter")
                return self

            def __exit__(self, exc_type, exc, tb):
                calls.append("lock_exit")

        class FakeSessionBox:
            def go_top(self):
                calls.append("go_top")

            def roll_down(self):
                calls.append("roll_down")

        class FakeWeChat:
            SessionBox = FakeSessionBox()

            def GetSession(self):
                calls.append("get_session")
                return [{"name": "阿英2", "content": "普通消息"}]

        with tempfile.TemporaryDirectory() as tmp:
            bot = SimpleNamespace(
                wx=FakeWeChat(),
                wx_id="wxid_test",
                config=SimpleNamespace(DATA_DIR=tmp),
                _get_wechat_action_lock=lambda: FakeLock(),
            )

            result = scan_full_sessions(bot, max_scrolls=1)

        self.assertEqual([item for item in calls if item == "go_top"], ["go_top", "go_top"])
        self.assertLess(calls.index("get_session"), len(calls) - 1)
        self.assertEqual(result["payload"]["summary"]["last_scan_mode"], "full")
        progress = result["payload"]["summary"]["full_scan_progress"]
        self.assertEqual(progress["status"], "completed")
        self.assertEqual(progress["unique_count"], 1)


    def test_full_scan_keeps_result_when_final_go_top_fails(self):
        calls = []

        class FakeLock:
            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc, tb):
                return None

        class FakeSessionBox:
            go_top_calls = 0

            def go_top(self):
                self.go_top_calls += 1
                calls.append("go_top")
                if self.go_top_calls >= 2:
                    raise RuntimeError("top failed")

            def roll_down(self):
                calls.append("roll_down")

        class FakeWeChat:
            SessionBox = FakeSessionBox()

            def GetSession(self):
                calls.append("get_session")
                return [{"name": "阿英2", "content": "普通消息"}]

        with tempfile.TemporaryDirectory() as tmp:
            bot = SimpleNamespace(
                wx=FakeWeChat(),
                wx_id="wxid_test",
                config=SimpleNamespace(DATA_DIR=tmp),
                _get_wechat_action_lock=lambda: FakeLock(),
            )

            result = scan_full_sessions(bot, max_scrolls=1)

        self.assertEqual(result["payload"]["summary"]["last_scan_mode"], "full")
        self.assertEqual(result["payload"]["summary"]["last_scan_count"], 1)


    def test_full_scan_releases_lock_between_scroll_slices_and_flushes_queue(self):
        calls = []

        class FakeLock:
            def __enter__(self):
                calls.append("lock_enter")
                return self

            def __exit__(self, exc_type, exc, tb):
                calls.append("lock_exit")
                return None

        class FakeSessionBox:
            def go_top(self):
                calls.append("go_top")

            def roll_down(self):
                calls.append("roll_down")

        class FakeWeChat:
            SessionBox = FakeSessionBox()

            def GetSession(self):
                calls.append("get_session")
                index = calls.count("get_session")
                return [{"name": f"阿英{index}", "content": "普通消息"}]

        with tempfile.TemporaryDirectory() as tmp:
            bot = SimpleNamespace(
                wx=FakeWeChat(),
                wx_id="wxid_test",
                config=SimpleNamespace(DATA_DIR=tmp),
                _get_wechat_action_lock=lambda: FakeLock(),
                _flush_lightweight_send_queue=lambda limit=20: calls.append(("flush", limit)),
            )
            from feature import relationship_scan

            old_slice = relationship_scan.FULL_SCAN_LOCK_SLICE_SCROLLS
            old_wait = relationship_scan.FULL_SCAN_SCROLL_SETTLE_SECONDS
            old_release_wait = relationship_scan.FULL_SCAN_LOCK_RELEASE_SETTLE_SECONDS
            relationship_scan.FULL_SCAN_LOCK_SLICE_SCROLLS = 2
            relationship_scan.FULL_SCAN_SCROLL_SETTLE_SECONDS = 0
            relationship_scan.FULL_SCAN_LOCK_RELEASE_SETTLE_SECONDS = 0
            try:
                result = scan_full_sessions(bot, max_scrolls=3)
            finally:
                relationship_scan.FULL_SCAN_LOCK_SLICE_SCROLLS = old_slice
                relationship_scan.FULL_SCAN_SCROLL_SETTLE_SECONDS = old_wait
                relationship_scan.FULL_SCAN_LOCK_RELEASE_SETTLE_SECONDS = old_release_wait

        self.assertIn(("flush", 20), calls)
        self.assertGreaterEqual(calls.count("lock_enter"), 2)
        self.assertEqual(result["payload"]["summary"]["last_scan_count"], 3)


    def test_full_scan_allow_running_takes_over_starting_state(self):
        class FakeLock:
            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc, tb):
                return None

        class FakeSessionBox:
            def go_top(self):
                pass

        class FakeWeChat:
            SessionBox = FakeSessionBox()

            def GetSession(self):
                return [{"name": "阿英2", "content": "普通消息"}]

        with tempfile.TemporaryDirectory() as tmp:
            state = {
                "wx_id": "wxid_test",
                "runtime": {
                    "full_scan_running": True,
                    "full_scan_progress": {"status": "running", "message": "全量扫描正在启动"},
                },
            }
            save_state(tmp, state)
            bot = SimpleNamespace(
                wx=FakeWeChat(),
                wx_id="wxid_test",
                config=SimpleNamespace(DATA_DIR=tmp),
                _get_wechat_action_lock=lambda: FakeLock(),
            )

            blocked = scan_full_sessions(bot, max_scrolls=1)
            result = scan_full_sessions(bot, max_scrolls=1, allow_running=True)

        self.assertTrue(blocked["already_running"])
        self.assertEqual(result["payload"]["summary"]["last_scan_count"], 1)
        self.assertEqual(result["payload"]["summary"]["full_scan_progress"]["status"], "completed")


if __name__ == "__main__":
    unittest.main()
