import unittest
from datetime import datetime, timedelta

from feature.relationship_scan import (
    STATUS_BLOCKED,
    STATUS_DELETED,
    STATUS_NORMAL,
    SYNC_PENDING,
    TAG_BLOCKED,
    TAG_DELETED,
    clear_state,
    due_for_auto_scan,
    merge_state_into_contact_directory,
    relationship_scan_summary,
    relationship_status_from_preview,
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
            "settings": {"auto_scan_enabled": False, "scan_interval_seconds": 20},
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


if __name__ == "__main__":
    unittest.main()
