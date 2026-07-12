import unittest
import tempfile
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import Mock
from core.wechat_ui_actions import IntentCancelled
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
    def test_current_scan_uses_owner_pure_session_intent(self):
        with tempfile.TemporaryDirectory() as data_dir:
            owner = Mock()
            owner.call.return_value = [{"name": "阿英2", "content": "普通消息", "time": "10:30", "info": ""}]
            bot = SimpleNamespace(
                wx=SimpleNamespace(),
                wx_id="wxid_test",
                _ui_owner=owner,
                config=SimpleNamespace(DATA_DIR=data_dir),
            )

            result = scan_current_sessions(bot)

            self.assertEqual(result["scan_source"], "wxauto_ui")
            self.assertEqual(result["sessions"][0]["name"], "阿英2")
            intent = owner.call.call_args.args[0]
            self.assertEqual(intent.kind.value, "relationship_scan")
            self.assertEqual(dict(intent.payload), {"mode": "current"})

    def test_owner_full_scan_marks_safety_cap_as_partial_instead_of_complete(self):
        with tempfile.TemporaryDirectory() as data_dir:
            owner = Mock()

            def owner_call(intent, _timeout):
                if dict(intent.payload).get("mode") == "full":
                    return {
                        "sessions": [{"name": "阿英2", "content": "普通消息", "time": "10:30", "info": ""}],
                        "scrolls": 1,
                        "hit_safety_limit": True,
                    }
                return []

            owner.call.side_effect = owner_call
            bot = SimpleNamespace(
                wx=SimpleNamespace(),
                wx_id="wxid_test",
                _ui_owner=owner,
                config=SimpleNamespace(DATA_DIR=data_dir),
            )

            result = scan_full_sessions(bot, max_scrolls=1)

            progress = result["state"]["runtime"]["full_scan_progress"]
            self.assertEqual(progress["status"], "partial")
            self.assertIn("安全上限", progress["message"])

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
                "contact_key": "remark:1",
                "remark": "阿英2",
                "nickname": "",
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
                    "contact_key": "remark:1",
                    "remark": "张姐",
                    "tags": [],
                },
                {
                    "contact_key": "remark:2",
                    "remark": "张姐",
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
                    "contact_key": "remark:1",
                    "remark": "张姐",
                    "tags": [],
                },
                {
                    "contact_key": "remark:2",
                    "remark": "张姐",
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
        self.assertEqual(cleared["settings"]["wechat_tag_sync_interval_seconds"], 30)
        self.assertNotIn("scan_interval_seconds", cleared["settings"])
        self.assertNotIn("sync_batch_size", cleared["settings"])
        self.assertNotIn("sync_interval_minutes", cleared["settings"])
        self.assertEqual(summary["last_scan_count"], 0)
        self.assertEqual(summary["last_scan_at"], "")
        self.assertEqual(summary["wechat_pending"], 0)

    def test_auto_scan_due_uses_fixed_thirty_second_interval(self):
        now = datetime(2026, 6, 11, 10, 0, 0)
        state = {
            "settings": {"auto_scan_enabled": True, "scan_interval_seconds": 10},
            "runtime": {"last_auto_scan_at": (now - timedelta(seconds=31)).isoformat()},
        }
        self.assertTrue(due_for_auto_scan(state, now=now))
        state["runtime"]["last_auto_scan_at"] = (now - timedelta(seconds=29)).isoformat()
        self.assertFalse(due_for_auto_scan(state, now=now))
        self.assertEqual(
            normalize_settings({"scan_interval_seconds": 10, "sync_batch_size": 9, "sync_interval_minutes": 1}),
            {"auto_scan_enabled": True, "auto_sync_wechat_tags": True, "wechat_tag_sync_interval_seconds": 30},
        )

    def test_auto_scan_without_owner_skips_without_touching_wechat(self):
        with tempfile.TemporaryDirectory() as tmp:
            save_state(tmp, {
                "wx_id": "wxid_test",
                "settings": {"auto_scan_enabled": True, "auto_sync_wechat_tags": False},
            })
            bot = SimpleNamespace(
                wx=SimpleNamespace(),
                wx_id="wxid_test",
                config=SimpleNamespace(DATA_DIR=tmp),
            )

            self.assertFalse(check_auto_scan(bot, now=datetime(2026, 6, 11, 10, 0, 0)))

    def test_auto_scan_owner_cancellation_is_safe(self):
        with tempfile.TemporaryDirectory() as tmp:
            save_state(tmp, {
                "wx_id": "wxid_test",
                "settings": {"auto_scan_enabled": True, "auto_sync_wechat_tags": False},
            })
            owner = Mock()
            owner.call.side_effect = IntentCancelled("settings changed")
            bot = SimpleNamespace(
                wx=SimpleNamespace(),
                wx_id="wxid_test",
                _ui_owner=owner,
                config=SimpleNamespace(DATA_DIR=tmp),
            )

            self.assertFalse(check_auto_scan(bot, now=datetime(2026, 6, 11, 10, 0, 0)))

    def test_auto_scan_does_not_queue_while_full_scan_is_running(self):
        with tempfile.TemporaryDirectory() as tmp:
            save_state(tmp, {
                "wx_id": "wxid_test",
                "settings": {"auto_scan_enabled": True, "auto_sync_wechat_tags": True},
                "runtime": {"full_scan_running": True},
            })
            owner = Mock()
            bot = SimpleNamespace(
                wx=SimpleNamespace(),
                wx_id="wxid_test",
                _ui_owner=owner,
                config=SimpleNamespace(DATA_DIR=tmp),
            )

            self.assertFalse(check_auto_scan(bot, now=datetime(2026, 6, 11, 10, 0, 0)))
            owner.call.assert_not_called()

    def test_auto_scan_reloads_state_after_owner_wait(self):
        now = datetime(2026, 6, 11, 10, 0, 0)
        with tempfile.TemporaryDirectory() as tmp:
            save_state(tmp, {
                "wx_id": "wxid_test",
                "settings": {"auto_scan_enabled": True, "auto_sync_wechat_tags": False},
            })
            owner = Mock()

            def finish_after_full_scan(_intent, _timeout):
                latest = load_state(tmp, "wxid_test")
                latest["runtime"]["full_scan_running"] = False
                latest["runtime"]["full_scan_progress"] = {
                    "status": "completed",
                    "unique_count": 1200,
                }
                save_state(tmp, latest)
                return [{"name": "阿英2", "content": "普通消息", "time": "10:30", "info": ""}]

            owner.call.side_effect = finish_after_full_scan
            bot = SimpleNamespace(
                wx=SimpleNamespace(),
                wx_id="wxid_test",
                _ui_owner=owner,
                config=SimpleNamespace(DATA_DIR=tmp),
            )

            self.assertTrue(check_auto_scan(bot, now=now))
            latest = load_state(tmp, "wxid_test")

        self.assertEqual(latest["runtime"]["full_scan_progress"]["status"], "completed")
        self.assertEqual(latest["runtime"]["full_scan_progress"]["unique_count"], 1200)

    def test_relationship_settings_only_persist_user_decisions(self):
        self.assertEqual(
            normalize_settings({}),
            {"auto_scan_enabled": True, "auto_sync_wechat_tags": True, "wechat_tag_sync_interval_seconds": 30},
        )
        self.assertEqual(normalize_settings({"wechat_tag_sync_interval_seconds": 0})["wechat_tag_sync_interval_seconds"], 1)
        self.assertEqual(normalize_settings({"wechat_tag_sync_interval_seconds": 101})["wechat_tag_sync_interval_seconds"], 100)
        self.assertEqual(normalize_settings({"wechat_tag_sync_interval_seconds": "invalid"})["wechat_tag_sync_interval_seconds"], 30)

    def test_wechat_tag_sync_due_uses_configured_interval(self):
        now = datetime(2026, 6, 11, 10, 0, 0)
        state = {
            "settings": {"auto_sync_wechat_tags": True, "wechat_tag_sync_interval_seconds": 60},
            "runtime": {"last_wechat_tag_sync_at": (now - timedelta(seconds=61)).isoformat()},
        }
        self.assertTrue(due_for_wechat_tag_sync(state, now=now))
        state["runtime"]["last_wechat_tag_sync_at"] = (now - timedelta(seconds=59)).isoformat()
        self.assertFalse(due_for_wechat_tag_sync(state, now=now))

    def test_wechat_tag_sync_processes_only_one_contact_per_run(self):
        calls = []
        now = datetime(2026, 6, 11, 10, 0, 0)
        with tempfile.TemporaryDirectory() as tmp:
            save_state(tmp, {
                "wx_id": "wxid_test",
                "settings": {"auto_sync_wechat_tags": True},
                "records": [
                    {"name": "阿英2", "status": STATUS_BLOCKED, "wechat_sync_status": SYNC_PENDING},
                    {"name": "阿英3", "status": STATUS_DELETED, "wechat_sync_status": SYNC_PENDING},
                ],
            })
            bot = SimpleNamespace(
                wx=object(),
                wx_id="wxid_test",
                config=SimpleNamespace(DATA_DIR=tmp),
            )

            def fake_modify(_bot, targets, **_kwargs):
                calls.append((targets[0]["name"], _kwargs.get("rebind_attempts")))
                return {"status": "success"}

            from feature import relationship_scan

            original = relationship_scan.modify_friend_tags_via_chat_profile
            relationship_scan.modify_friend_tags_via_chat_profile = fake_modify
            try:
                result = process_pending_wechat_tag_sync(bot, now=now)
                too_soon = process_pending_wechat_tag_sync(bot, now=now + timedelta(seconds=29))
                next_result = process_pending_wechat_tag_sync(bot, now=now + timedelta(seconds=31))
                latest = load_state(tmp, "wxid_test")
            finally:
                relationship_scan.modify_friend_tags_via_chat_profile = original

        self.assertEqual(result, {"processed": 1, "success": 1, "failed": 0})
        self.assertEqual(too_soon, {"processed": 0, "success": 0, "failed": 0})
        self.assertEqual(next_result, {"processed": 1, "success": 1, "failed": 0})
        self.assertEqual(calls, [("阿英2", 1), ("阿英3", 1)])
        records = {record["name"]: record for record in latest["records"]}
        self.assertEqual(records["阿英2"]["wechat_sync_status"], "synced")
        self.assertEqual(records["阿英3"]["wechat_sync_status"], "synced")

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


    def test_current_scan_requires_owner(self):
        with tempfile.TemporaryDirectory() as tmp:
            bot = SimpleNamespace(
                wx=SimpleNamespace(),
                wx_id="wxid_test",
                config=SimpleNamespace(DATA_DIR=tmp),
            )

            with self.assertRaisesRegex(RuntimeError, "微信 UI owner"):
                scan_current_sessions(bot)

    def test_full_scan_uses_one_owner_intent_until_completion(self):
        with tempfile.TemporaryDirectory() as tmp:
            owner = Mock()
            owner.call.return_value = {
                "sessions": [{"name": "阿英2", "content": "普通消息"}],
                "scrolls": 12,
                "hit_safety_limit": False,
            }
            bot = SimpleNamespace(
                wx=SimpleNamespace(),
                wx_id="wxid_test",
                _ui_owner=owner,
                config=SimpleNamespace(DATA_DIR=tmp),
            )

            result = scan_full_sessions(bot, max_scrolls=1000)

        owner.call.assert_called_once()
        intent = owner.call.call_args.args[0]
        self.assertEqual(intent.kind.value, "relationship_scan")
        self.assertEqual(dict(intent.payload), {"mode": "full", "max_scrolls": 1000, "stale_rounds": 8})
        progress = result["payload"]["summary"]["full_scan_progress"]
        self.assertEqual(progress["status"], "completed")
        self.assertEqual(progress["scrolled_rounds"], 12)
        self.assertEqual(progress["unique_count"], 1)

    def test_full_scan_requires_owner_without_marking_running(self):
        with tempfile.TemporaryDirectory() as tmp:
            bot = SimpleNamespace(
                wx=SimpleNamespace(),
                wx_id="wxid_test",
                config=SimpleNamespace(DATA_DIR=tmp),
            )

            with self.assertRaisesRegex(RuntimeError, "微信 UI owner"):
                scan_full_sessions(bot)

            state = load_state(tmp, "wxid_test")
        self.assertFalse(state["runtime"]["full_scan_running"])

    def test_full_scan_allow_running_takes_over_starting_state(self):
        with tempfile.TemporaryDirectory() as tmp:
            state = {
                "wx_id": "wxid_test",
                "runtime": {
                    "full_scan_running": True,
                    "full_scan_progress": {"status": "running", "message": "全量扫描正在启动"},
                },
            }
            save_state(tmp, state)
            owner = Mock()
            owner.call.return_value = {
                "sessions": [{"name": "阿英2", "content": "普通消息"}],
                "scrolls": 1,
                "hit_safety_limit": False,
            }
            bot = SimpleNamespace(
                wx=SimpleNamespace(),
                wx_id="wxid_test",
                _ui_owner=owner,
                config=SimpleNamespace(DATA_DIR=tmp),
            )

            blocked = scan_full_sessions(bot, max_scrolls=1)
            result = scan_full_sessions(bot, max_scrolls=1, allow_running=True)

        self.assertTrue(blocked["already_running"])
        self.assertEqual(result["payload"]["summary"]["last_scan_count"], 1)
        self.assertEqual(result["payload"]["summary"]["full_scan_progress"]["status"], "completed")


if __name__ == "__main__":
    unittest.main()
