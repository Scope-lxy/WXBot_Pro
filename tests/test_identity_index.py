import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from core.account_storage import account_area_dir
from core.contact_profiles import merge_directory
from core.identity_index import (
    add_pending,
    default_index,
    dismiss_pending,
    match_identity,
    reconcile_storage_names,
    resolve_chat_name,
    sync_identity_task_names,
    sync_relationship_scan_names,
    update_index_from_directory,
)
from core.memory import resolve_memory_storage_name
import web_server


def snapshot(**kwargs):
    base = {
        "current_chat_name": kwargs.get("current_chat_name", kwargs.get("remark") or kwargs.get("nickname") or ""),
        "wechat_id": kwargs.get("wechat_id", ""),
        "remark": kwargs.get("remark", ""),
        "nickname": kwargs.get("nickname", ""),
        "source": kwargs.get("source", ""),
        "added_at": kwargs.get("added_at", ""),
    }
    base.update(kwargs)
    return base


def directory(*contacts):
    return {
        "wx_id": "wxid_test",
        "subjects": list(contacts),
    }


def contact(**kwargs):
    remark = kwargs.get("remark", "")
    nickname = kwargs.get("nickname", "")
    wechat_id = kwargs.get("wechat_id", "")
    raw = {
        "备注": remark,
        "昵称": nickname,
        "微信号": wechat_id,
        "来源": kwargs.get("source", ""),
        "添加时间": kwargs.get("added_at", ""),
    }
    return {
        "subject_type": "friend",
        "status": "active",
        "remark": remark,
        "nickname": nickname,
        "wechat_id": wechat_id,
        "display_name": remark or nickname or wechat_id,
        "send_name": remark or nickname or wechat_id,
        "raw_detail": raw,
    }


class IdentityIndexTests(unittest.TestCase):
    def test_same_wechat_id_matches_after_name_change(self):
        old = snapshot(current_chat_name="A0-努力", wechat_id="wxid_1", remark="A0-努力", nickname="皖君")
        new = snapshot(current_chat_name="A0-努力加油", wechat_id="wxid_1", remark="A0-努力加油", nickname="皖君")

        matched, reason = match_identity(new, [old])

        self.assertEqual(reason, "wechat_id")
        self.assertEqual(matched["current_chat_name"], "A0-努力")

    def test_unique_remark_matches_but_duplicate_remark_conflicts(self):
        new = snapshot(current_chat_name="A0-努力", wechat_id="wxid_new", remark="A0-努力", nickname="新昵称")
        matched, reason = match_identity(new, [snapshot(current_chat_name="旧名", wechat_id="wxid_old", remark="A0-努力")])
        self.assertEqual(reason, "unique_remark")
        self.assertEqual(matched["remark"], "A0-努力")

        duplicated, duplicate_reason = match_identity(new, [
            snapshot(current_chat_name="旧名1", wechat_id="wxid_1", remark="A0-努力"),
            snapshot(current_chat_name="旧名2", wechat_id="wxid_2", remark="A0-努力"),
        ])
        self.assertIsNone(duplicated)
        self.assertEqual(duplicate_reason, "conflict_remark")

    def test_no_remark_nickname_source_added_matches_only_when_unique_and_only_wechat_id_changed(self):
        old = snapshot(current_chat_name="努力", wechat_id="wxid_old", nickname="努力", source="通过扫一扫添加", added_at="2024-10-17")
        new = snapshot(current_chat_name="努力", wechat_id="wxid_new", nickname="努力", source="通过扫一扫添加", added_at="2024-10-17")

        matched, reason = match_identity(new, [old], incoming_snapshots=[new])

        self.assertEqual(reason, "no_remark_snapshot")
        self.assertEqual(matched["wechat_id"], "wxid_old")

        changed_nickname = snapshot(current_chat_name="摸鱼", wechat_id="wxid_new", nickname="摸鱼", source="通过扫一扫添加", added_at="2024-10-17")
        matched, reason = match_identity(changed_nickname, [old], incoming_snapshots=[changed_nickname])
        self.assertIsNone(matched)
        self.assertEqual(reason, "new_or_pending")

    def test_no_remark_missing_wechat_id_reuses_identical_snapshot_without_pending(self):
        first, actions = update_index_from_directory(default_index("wxid_test"), directory(
            contact(nickname="努力", source="通过扫一扫添加", added_at="2024-10-17"),
        ), wx_id="wxid_test")
        repeated, actions = update_index_from_directory(first, directory(
            contact(nickname="努力", source="通过扫一扫添加", added_at="2024-10-17"),
        ), wx_id="wxid_test")

        self.assertEqual(actions, [])
        self.assertEqual(len(repeated["identities"]), 1)
        self.assertEqual([item for item in repeated["pending"] if item.get("status") == "pending"], [])

    def test_weak_source_added_or_nickname_alone_does_not_create_pending(self):
        index = {
            **default_index("wxid_test"),
            "identities": [
                snapshot(current_chat_name="旧1", wechat_id="wxid_1", nickname="同昵称"),
                snapshot(current_chat_name="旧2", wechat_id="wxid_2", nickname="其他", source="通过扫一扫添加", added_at="2024-10-17"),
            ],
        }
        updated, actions = update_index_from_directory(index, directory(
            contact(wechat_id="wxid_3", nickname="同昵称"),
            contact(wechat_id="wxid_4", nickname="新人", source="通过扫一扫添加", added_at="2024-10-17"),
        ), wx_id="wxid_test")

        self.assertEqual(actions, [])
        self.assertEqual([item for item in updated["pending"] if item.get("status") == "pending"], [])

    def test_conflict_generates_pending_and_dismiss_prevents_reprompt(self):
        index = {
            **default_index("wxid_test"),
            "identities": [
                snapshot(current_chat_name="旧1", wechat_id="wxid_1", remark="同备注"),
                snapshot(current_chat_name="旧2", wechat_id="wxid_2", remark="同备注"),
            ],
        }
        updated, _actions = update_index_from_directory(index, directory(
            contact(wechat_id="wxid_3", remark="同备注", nickname="新"),
        ), wx_id="wxid_test")

        pending = [item for item in updated["pending"] if item.get("status") == "pending"]
        self.assertEqual(len(pending), 2)

        dismissed = dismiss_pending(updated, pending[0]["fingerprint"])
        repeated, _actions = update_index_from_directory(dismissed, directory(
            contact(wechat_id="wxid_4", remark="同备注", nickname="新2"),
        ), wx_id="wxid_test")
        fingerprints = {item["fingerprint"] for item in repeated["pending"] if item.get("status") == "pending"}
        self.assertNotIn(pending[0]["fingerprint"], fingerprints)

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

            conv_dir = account_area_dir(base, wx_id, "conversation_memory", create=True)
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
                "conversation_memory_exclude_list": [old_name],
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

            manifest = reconcile_storage_names(base, wx_id, old_name, new_name, reason="test")

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

            manifest = reconcile_storage_names(base, wx_id, old_name, new_name, reason="rename_only")

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

    def test_conversation_memory_merge_keeps_legacy_profile_items(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            wx_id = "wxid_test"
            old_name = "旧"
            new_name = "新"
            conv_dir = account_area_dir(base, wx_id, "conversation_memory", create=True)
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

            reconcile_storage_names(base, wx_id, old_name, new_name, reason="legacy_profile")

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

    def test_resolve_chat_name_is_hot_path_noop_after_reconcile(self):
        index, actions = update_index_from_directory(default_index("wxid_test"), directory(
            contact(wechat_id="wxid_1", remark="A0-努力", nickname="皖君"),
        ), wx_id="wxid_test")
        index, actions = update_index_from_directory(index, directory(
            contact(wechat_id="wxid_1", remark="A0-努力加油", nickname="皖君"),
        ), wx_id="wxid_test")

        self.assertEqual(actions[0]["old_chat_name"], "A0-努力")
        self.assertEqual(actions[0]["new_chat_name"], "A0-努力加油")
        self.assertEqual(resolve_chat_name(index, "A0-努力加油"), "A0-努力加油")
        self.assertEqual(resolve_chat_name(index, "A0-努力"), "A0-努力")

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

    def test_pending_item_records_new_identity_id_for_manual_merge(self):
        index = default_index("wxid_test")
        index["identities"] = [snapshot(current_chat_name="旧", wechat_id="wxid_old", remark="同备注")]
        index = add_pending(
            index,
            index["identities"][0],
            snapshot(current_chat_name="新", wechat_id="wxid_new", remark="同备注"),
            reason="conflict_remark",
            new_identity_id="person_new",
        )

        self.assertEqual(index["pending"][0]["new_identity_id"], "person_new")

    def test_sync_identity_task_names_replaces_exact_values_only(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            task_dir = base / "accounts" / "wxid_test" / "tasks" / "custom_forward"
            task_dir.mkdir(parents=True)
            path = task_dir / "rules.json"
            path.write_text(json.dumps([{
                "sources": ["旧"],
                "targets": ["新", "旧"],
                "note": "旧同学",
            }], ensure_ascii=False), encoding="utf-8")

            result = sync_identity_task_names(base, "wxid_test", "旧", "新")

            self.assertTrue(result["changed"])
            data = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(data[0]["sources"], ["新"])
            self.assertEqual(data[0]["targets"], ["新", "新"])
            self.assertEqual(data[0]["note"], "旧同学")


if __name__ == "__main__":
    unittest.main()
