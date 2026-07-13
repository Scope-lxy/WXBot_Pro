import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from core.account_storage import account_area_dir
from core.contact_profiles import (
    contact_display_label,
    contact_public_view,
    contact_send_target,
    dismiss_identity_calibration_pending as dismiss_contact_profile_pending,
    load_directory as load_contact_directory,
    merge_directory,
    save_directory as save_contact_directory,
    sync_identity_calibration_from_directory,
)
from core.contact_identity import (
    reconcile_contact_storage,
    sync_contact_task_names,
    sync_relationship_scan_names,
)
from core.memory import resolve_memory_storage_name
import web_server
from wxbot_core import WXBot


def directory(*contacts):
    return {
        "wx_id": "wxid_test",
        "subjects": list(contacts),
    }


def contact(**kwargs):
    remark = kwargs.get("remark", "")
    nickname = kwargs.get("nickname", "")
    wechat_id = kwargs.get("wechat_id", "")
    return {
        "status": "active",
        "remark": remark,
        "nickname": nickname,
        "wechat_id": wechat_id,
        "source": kwargs.get("source", ""),
        "added_at": kwargs.get("added_at", ""),
    }


class ContactIdentityTests(unittest.TestCase):
    def test_reconcile_storage_merges_memory_conversation_config_and_relationship_scan(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            wx_id = "wxid_test"
            old_name = "A0-努力"
            new_name = "B0-我要摸鱼"

            old_memory_dir = account_area_dir(base, wx_id, "memory", create=True) / resolve_memory_storage_name(old_name)
            new_memory_dir = account_area_dir(base, wx_id, "memory", create=True) / resolve_memory_storage_name(new_name)
            old_memory_dir.mkdir(parents=True, exist_ok=True)
            new_memory_dir.mkdir(parents=True, exist_ok=True)
            (old_memory_dir / f"{old_memory_dir.name}_memory.json").write_text(
                json.dumps([{"time": "2026/06/15 10:00:00", "sender": "user", "type": "text", "attr": "friend", "content": "旧"}], ensure_ascii=False),
                encoding="utf-8",
            )
            (new_memory_dir / f"{new_memory_dir.name}_memory.json").write_text(
                json.dumps([{"time": "2026/06/15 10:01:00", "sender": "user", "type": "text", "attr": "friend", "content": "新"}], ensure_ascii=False),
                encoding="utf-8",
            )

            conv_dir = account_area_dir(base, wx_id, "chat_memory", create=True)
            (conv_dir / f"{resolve_memory_storage_name(old_name)}.json").write_text(
                json.dumps({"chat_name": old_name, "memories": [{"type": "状态", "content": "旧记忆", "importance": "中"}]}, ensure_ascii=False),
                encoding="utf-8",
            )
            (conv_dir / f"{resolve_memory_storage_name(new_name)}.json").write_text(
                json.dumps({"chat_name": new_name, "memories": [{"type": "状态", "content": "新记忆", "importance": "中"}]}, ensure_ascii=False),
                encoding="utf-8",
            )

            config_dir = base / "config"
            config_dir.mkdir()
            (config_dir / "config.json").write_text(json.dumps({
                "listen_list": [old_name],
                "global_blacklist": [old_name],
                "chat_memory_exclude_list": [old_name],
                "chat_prompt_map": {old_name: "人设A"},
                "chat_api_map": {old_name: 1},
                "chat_tts_map": {old_name: 2},
            }, ensure_ascii=False), encoding="utf-8")
            (config_dir / "reply_count.json").write_text(json.dumps({
                "users": {
                    old_name: {"count": 2, "window_started_at": "2026-06-15T10:00:00"},
                    new_name: {"count": 1, "window_started_at": "2026-06-15T10:05:00"},
                }
            }, ensure_ascii=False), encoding="utf-8")

            rel_dir = account_area_dir(base, wx_id, "relationship_scan", create=True)
            (rel_dir / "relationships.json").write_text(json.dumps({
                "wx_id": wx_id,
                "records": [{"name": old_name, "status": "blocked"}],
                "events": [{"name": old_name, "type": "blocked"}],
            }, ensure_ascii=False), encoding="utf-8")

            manifest = reconcile_contact_storage(base, wx_id, old_name, new_name, reason="test")

            self.assertTrue(manifest["memory"]["changed"])
            merged_messages = json.loads((new_memory_dir / f"{new_memory_dir.name}_memory.json").read_text(encoding="utf-8"))
            self.assertEqual([item["content"] for item in merged_messages], ["旧", "新"])
            self.assertFalse(old_memory_dir.exists())

            merged_state = json.loads((conv_dir / f"{resolve_memory_storage_name(new_name)}.json").read_text(encoding="utf-8"))
            self.assertEqual({item["content"] for item in merged_state["memories"]}, {"旧记忆", "新记忆"})

            config = json.loads((config_dir / "config.json").read_text(encoding="utf-8"))
            self.assertEqual(config["listen_list"], [new_name])
            self.assertEqual(config["chat_prompt_map"], {new_name: "人设A"})
            reply_count = json.loads((config_dir / "reply_count.json").read_text(encoding="utf-8"))
            self.assertEqual(reply_count["users"][new_name]["count"], 3)

            relationship = json.loads((rel_dir / "relationships.json").read_text(encoding="utf-8"))
            self.assertEqual(relationship["records"][0]["name"], new_name)
            self.assertEqual(relationship["events"][0]["name"], new_name)

    def test_memory_rename_normalizes_internal_memory_file_name(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            wx_id = "wxid_test"
            old_name = "A0-努力"
            new_name = "A0-努力加油"
            old_memory_dir = account_area_dir(base, wx_id, "memory", create=True) / resolve_memory_storage_name(old_name)
            old_memory_dir.mkdir(parents=True, exist_ok=True)
            old_file = old_memory_dir / f"{old_memory_dir.name}_memory.json"
            old_file.write_text(
                json.dumps([{"time": "2026/06/15 10:00:00", "sender": "user", "type": "text", "attr": "friend", "content": "旧"}], ensure_ascii=False),
                encoding="utf-8",
            )

            manifest = reconcile_contact_storage(base, wx_id, old_name, new_name, reason="rename_only")

            new_memory_dir = account_area_dir(base, wx_id, "memory") / resolve_memory_storage_name(new_name)
            canonical_file = new_memory_dir / f"{new_memory_dir.name}_memory.json"
            self.assertTrue(manifest["memory"]["changed"])
            self.assertTrue(canonical_file.exists())
            self.assertEqual([path.name for path in new_memory_dir.glob("*_memory.json")], [canonical_file.name])
            messages = json.loads(canonical_file.read_text(encoding="utf-8"))
            self.assertEqual([item["content"] for item in messages], ["旧"])

    def test_manual_identity_calibration_candidates_include_memory_names(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            wx_id = "wxid_test"
            old_name = "旧备注"
            old_memory_dir = account_area_dir(base, wx_id, "memory", create=True) / resolve_memory_storage_name(old_name)
            old_memory_dir.mkdir(parents=True, exist_ok=True)
            (old_memory_dir / "name.json").write_text(
                json.dumps({"name": old_name}, ensure_ascii=False),
                encoding="utf-8",
            )
            (old_memory_dir / f"{old_memory_dir.name}_memory.json").write_text(
                json.dumps([{"content": "旧消息"}], ensure_ascii=False),
                encoding="utf-8",
            )

            with patch.object(web_server, "DATA_DIR", str(base)):
                app = web_server.app
                app.config["TESTING"] = True
                with app.test_client() as client:
                    with client.session_transaction() as session:
                        session["logged_in"] = True
                    response = client.get(
                        "/contact_profiles/manual_identity_calibration/candidates",
                        query_string={"wx_id": wx_id},
                    )

            payload = response.get_json()
            self.assertEqual(payload["status"], "success")
            self.assertIn(old_name, [item["name"] for item in payload["candidates"]])

    def test_manual_identity_calibration_merges_local_storage(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            wx_id = "wxid_test"
            old_name = "旧备注"
            new_name = "新备注"
            old_memory_dir = account_area_dir(base, wx_id, "memory", create=True) / resolve_memory_storage_name(old_name)
            old_memory_dir.mkdir(parents=True, exist_ok=True)
            (old_memory_dir / "name.json").write_text(
                json.dumps({"name": old_name}, ensure_ascii=False),
                encoding="utf-8",
            )
            (old_memory_dir / f"{old_memory_dir.name}_memory.json").write_text(
                json.dumps([{"content": "旧消息"}], ensure_ascii=False),
                encoding="utf-8",
            )

            with patch.object(web_server, "DATA_DIR", str(base)):
                app = web_server.app
                app.config["TESTING"] = True
                with app.test_client() as client:
                    with client.session_transaction() as session:
                        session["logged_in"] = True
                    response = client.post(
                        "/contact_profiles/manual_identity_calibration",
                        json={"wx_id": wx_id, "old_name": old_name, "new_name": new_name},
                    )

            payload = response.get_json()
            self.assertEqual(payload["status"], "success")
            new_memory_dir = account_area_dir(base, wx_id, "memory") / resolve_memory_storage_name(new_name)
            self.assertTrue(new_memory_dir.exists())
            self.assertFalse(old_memory_dir.exists())
            merged = json.loads((new_memory_dir / f"{new_memory_dir.name}_memory.json").read_text(encoding="utf-8"))
            self.assertEqual([item["content"] for item in merged], ["旧消息"])

    def test_chat_memory_merge_keeps_legacy_profile_items(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            wx_id = "wxid_test"
            old_name = "旧"
            new_name = "新"
            conv_dir = account_area_dir(base, wx_id, "chat_memory", create=True)
            (conv_dir / f"{resolve_memory_storage_name(old_name)}.json").write_text(
                json.dumps({
                    "chat_name": old_name,
                    "profile": {"items": [{"id": "B01", "type": "身份", "content": "旧基础信息"}]},
                    "memories": [{"id": "M01", "type": "状态", "content": "旧记忆", "importance": "中"}],
                }, ensure_ascii=False),
                encoding="utf-8",
            )
            (conv_dir / f"{resolve_memory_storage_name(new_name)}.json").write_text(
                json.dumps({"chat_name": new_name, "memories": [{"id": "M01", "type": "状态", "content": "新记忆", "importance": "中"}]}, ensure_ascii=False),
                encoding="utf-8",
            )

            reconcile_contact_storage(base, wx_id, old_name, new_name, reason="legacy_profile")

            merged = json.loads((conv_dir / f"{resolve_memory_storage_name(new_name)}.json").read_text(encoding="utf-8"))
            contents = {item["content"] for item in merged["memories"]}
            self.assertIn("旧基础信息", contents)
            self.assertIn("旧记忆", contents)
            self.assertIn("新记忆", contents)

    def test_contact_directory_merge_reuses_identity_when_wechat_id_changes(self):
        old_directory = directory(contact(
            wechat_id="wxid_old",
            nickname="努力",
            source="通过扫一扫添加",
            added_at="2024-10-17",
        ))
        merged = merge_directory(
            old_directory,
            [{"微信号": "wxid_new", "昵称": "努力", "来源": "通过扫一扫添加", "添加时间": "2024-10-17"}],
            wx_id="wxid_test",
            mark_missing=False,
        )

        self.assertEqual(len(merged["subjects"]), 1)
        self.assertEqual(merged["subjects"][0]["wechat_id"], "wxid_new")

    def test_sync_relationship_scan_names_handles_missing_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            result = sync_relationship_scan_names(tmp, "wxid_test", "旧", "新")
        self.assertFalse(result["changed"])

    def test_relationship_scan_merge_uses_latest_status_fields(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            wx_id = "wxid_test"
            rel_dir = account_area_dir(base, wx_id, "relationship_scan", create=True)
            (rel_dir / "relationships.json").write_text(json.dumps({
                "wx_id": wx_id,
                "records": [
                    {
                        "name": "旧",
                        "status": "blocked",
                        "evidence": "新证据",
                        "changed_at": "2026-06-15T10:00:00",
                        "last_seen_at": "2026-06-15T10:00:00",
                    },
                    {
                        "name": "新",
                        "status": "normal",
                        "evidence": "旧证据",
                        "changed_at": "2026-06-15T09:00:00",
                        "last_seen_at": "2026-06-15T09:00:00",
                    },
                ],
                "events": [],
            }, ensure_ascii=False), encoding="utf-8")

            result = sync_relationship_scan_names(base, wx_id, "旧", "新")

            self.assertTrue(result["changed"])
            relationship = json.loads((rel_dir / "relationships.json").read_text(encoding="utf-8"))
            self.assertEqual(len(relationship["records"]), 1)
            record = relationship["records"][0]
            self.assertEqual(record["name"], "新")
            self.assertEqual(record["status"], "blocked")
            self.assertEqual(record["evidence"], "新证据")
            self.assertEqual(record["changed_at"], "2026-06-15T10:00:00")

    def test_sync_contact_task_names_replaces_exact_values_only(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            task_dir = base / "accounts" / "wxid_test" / "tasks" / "scheduled_message"
            task_dir.mkdir(parents=True)
            path = task_dir / "tasks.json"
            path.write_text(json.dumps([{
                "manual_target_names": ["新", "旧"],
                "targets": ["旧"],
                "note": "旧同学",
            }], ensure_ascii=False), encoding="utf-8")

            result = sync_contact_task_names(base, "wxid_test", "旧", "新")

            self.assertTrue(result["changed"])
            data = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(data[0]["manual_target_names"], ["新", "新"])
            self.assertEqual(data[0]["targets"], ["新"])
            self.assertEqual(data[0]["note"], "旧同学")

    def test_v2_dismiss_keeps_actions_and_does_not_restore_identity_table(self):
        directory_payload = {
            "schema_version": 2,
            "wx_id": "wxid_test",
            "subjects": [],
            "identity_calibration": {
                "actions": [{"type": "rename", "old_chat_name": "旧", "new_chat_name": "新"}],
                "pending": [{
                    "fingerprint": "fp1",
                    "status": "pending",
                    "old_name": "旧",
                    "new_name": "新",
                }],
                "dismissed_pairs": [],
            },
        }

        updated = dismiss_contact_profile_pending(directory_payload, "fp1")

        state = updated["identity_calibration"]
        self.assertEqual(state["actions"], [{"type": "rename", "old_chat_name": "旧", "new_chat_name": "新"}])
        self.assertNotIn("identities", state)
        self.assertEqual(state["dismissed_pairs"], ["fp1"])
        self.assertEqual(state["pending"][0]["status"], "dismissed")

    def test_v2_load_directory_strips_legacy_contact_archive_fields(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "contacts.json"
            path.write_text(json.dumps({
                "schema_version": 2,
                "wx_id": "wxid_test",
                "identity_calibration": {
                    "actions": [],
                    "pending": [{
                        "fingerprint": "fp1",
                        "status": "pending",
                        "reason": "conflict",
                        "old_name": "旧",
                        "new_name": "新",
                        "old_snapshot": {"current_chat_name": "旧"},
                        "new_snapshot": {"current_chat_name": "新"},
                    }],
                    "dismissed_pairs": [],
                },
                "subjects": [
                    {
                        "subject_type": "group",
                        "contact_key": "legacy:1",
                        "remark": "好友",
                        "display_name": "旧显示名",
                        "send_name": "旧发送名",
                        "raw_detail": {"备注": "旧详情"},
                        "raw_tags": ["旧标签"],
                        "status": "active",
                    },
                ],
            }, ensure_ascii=False), encoding="utf-8")

            directory_payload = load_contact_directory(path, wx_id="wxid_test")

        self.assertEqual(len(directory_payload["subjects"]), 1)
        subject = directory_payload["subjects"][0]
        self.assertEqual(subject["contact_key"], "legacy:1")
        self.assertEqual(subject["remark"], "好友")
        self.assert_no_legacy_contact_keys(directory_payload)

    def test_contact_public_view_and_target_helpers_expose_v2_fields_only(self):
        subject = {
            "contact_key": "wechat_id:wx_zhang",
            "remark": "张三",
            "nickname": "三三",
            "wechat_id": "wx_zhang",
            "display_name": "旧显示名",
            "send_name": "旧发送名",
            "send_target": "接口发送名",
            "name": "接口显示名",
            "tags": ["客户"],
            "warnings": ["duplicate_send_name"],
        }

        view = contact_public_view(subject)

        self.assertEqual(view["name"], "张三")
        self.assertEqual(view["send_target"], "张三")
        self.assertNotIn("display_name", view)
        self.assertNotIn("send_name", view)
        self.assertEqual(contact_send_target(subject), "张三")
        self.assertEqual(contact_display_label(subject), "张三")
        self.assertEqual(contact_send_target(view), "张三")
        self.assertEqual(contact_display_label(view), "张三")

    def test_v2_save_directory_strips_legacy_contact_archive_fields(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "contacts.json"
            save_contact_directory(path, {
                "schema_version": 2,
                "wx_id": "wxid_test",
                "identity_calibration": {
                    "actions": [],
                    "pending": [{
                        "fingerprint": "fp1",
                        "old_name": "旧",
                        "new_name": "新",
                        "old_snapshot": {"current_chat_name": "旧"},
                    }],
                    "dismissed_pairs": [],
                },
                "subjects": [{
                    "contact_key": "wechat_id:wx_zhang",
                    "remark": "张三",
                    "display_name": "旧显示名",
                    "send_name": "旧发送名",
                    "raw_detail": {"备注": "张三"},
                    "status": "active",
                }],
            })

            saved = json.loads(path.read_text(encoding="utf-8"))

        self.assertEqual(saved["schema_version"], 2)
        self.assert_no_legacy_contact_keys(saved)

    def assert_no_legacy_contact_keys(self, payload):
        legacy_keys = {
            "subject_type",
            "display_name",
            "send_name",
            "send_name_source",
            "raw_detail",
            "raw_tags",
            "identity_confidence",
            "identity_source",
            "current_chat_name",
            "storage_name",
            "identities",
            "old_snapshot",
            "new_snapshot",
            "old_identity_id",
            "new_identity_id",
        }

        def walk(value, path="root"):
            if isinstance(value, dict):
                for key, item in value.items():
                    self.assertNotIn(key, legacy_keys, f"{path}.{key}")
                    walk(item, f"{path}.{key}")
            elif isinstance(value, list):
                for index, item in enumerate(value):
                    walk(item, f"{path}[{index}]")

        walk(payload)

    def test_failed_identity_action_is_kept_for_retry(self):
        class FakeBot:
            wx_id = "wxid_test"

            def __init__(self):
                self.saved_directory = None

            def _reconcile_identity_storage(self, *_args, **_kwargs):
                return None

            def _save_contact_profiles_directory(self, directory_payload):
                self.saved_directory = directory_payload

        bot = FakeBot()
        directory_payload = {
            "wx_id": "wxid_test",
            "subjects": [],
            "identity_calibration": {
                "actions": [{"type": "rename", "old_chat_name": "旧", "new_chat_name": "新"}],
                "pending": [],
                "dismissed_pairs": [],
            },
        }

        updated = WXBot._sync_contact_identity_from_contact_directory(bot, directory_payload)

        self.assertIs(updated, bot.saved_directory)
        actions = updated["identity_calibration"]["actions"]
        self.assertEqual(len(actions), 1)
        self.assertEqual(actions[0]["old_chat_name"], "旧")
        self.assertEqual(actions[0]["new_chat_name"], "新")
        self.assertEqual(actions[0]["last_error"], "reconcile_failed")

    def test_successful_identity_action_is_removed_after_storage_merge(self):
        class FakeBot:
            wx_id = "wxid_test"

            def __init__(self):
                self.saved_directory = None

            def _reconcile_identity_storage(self, *_args, **_kwargs):
                return {"changed": True}

            def _save_contact_profiles_directory(self, directory_payload):
                self.saved_directory = directory_payload

        bot = FakeBot()
        directory_payload = {
            "wx_id": "wxid_test",
            "subjects": [],
            "identity_calibration": {
                "actions": [{"type": "rename", "old_chat_name": "旧", "new_chat_name": "新"}],
                "pending": [],
                "dismissed_pairs": [],
            },
        }

        updated = WXBot._sync_contact_identity_from_contact_directory(bot, directory_payload)

        self.assertIs(updated, bot.saved_directory)
        self.assertEqual(updated["identity_calibration"]["actions"], [])


if __name__ == "__main__":
    unittest.main()
