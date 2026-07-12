import os
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

from core.contact_profiles import default_directory, load_directory, save_directory
from core.memory import MemoryManager
from core.ui_delivery_journal import UIDeliveryJournal
from core.wechat_ui_actions import ActionBatchInterrupted, UIIntent, UIIntentKind, WeChatUIOwner
from core.unanswered_inbound import UnansweredInboundStore
from feature import relationship_scan
from feature.material_outreach import build_progress_record
from feature.material_outreach_storage import MaterialOutreachStorage


ROOT = Path(__file__).resolve().parents[1]


class RuntimeStorageSafetyTests(unittest.TestCase):
    def test_journaled_delivery_process_exit_matrix(self):
        kinds = ("send_file", "send_audio", "forward", "quote", "send_actions")
        expected_by_phase = {
            "before_begin": None,
            "after_begin": "uncertain",
            "after_done": "done",
        }
        for kind in kinds:
            for phase, expected in expected_by_phase.items():
                with self.subTest(kind=kind, phase=phase), tempfile.TemporaryDirectory() as tmp:
                    script = f"""
import os
from core.ui_delivery_journal import UIDeliveryJournal

journal = UIDeliveryJournal({tmp!r}, 'wxid_test')
if {phase!r} != 'before_begin':
    journal.begin('delivery-1', {kind!r}, {{'conversation': 'fault-target'}})
if {phase!r} == 'after_done':
    journal.finish('delivery-1', 'done')
os._exit(91)
"""
                    env = dict(os.environ)
                    env.update({
                        "PYTHONPATH": str(ROOT),
                        "PYTHONUTF8": "1",
                        "PYTHONIOENCODING": "utf-8",
                    })
                    result = subprocess.run(
                        [sys.executable, "-X", "utf8", "-c", script],
                        cwd=tmp,
                        env=env,
                        capture_output=True,
                        text=True,
                        encoding="utf-8",
                        errors="replace",
                        timeout=15,
                    )
                    self.assertEqual(result.returncode, 91, result.stdout + result.stderr)

                    journal = UIDeliveryJournal(tmp, "wxid_test")
                    journal.freeze_interrupted()
                    records = journal.records()
                    if expected is None:
                        self.assertEqual(records, [])
                    else:
                        self.assertEqual(len(records), 1)
                        self.assertEqual(records[0]["kind"], kind)
                        self.assertEqual(records[0]["status"], expected)

    def test_contact_directory_save_is_atomic_and_round_trips(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "contacts.json"
            directory = default_directory("wxid_test")
            directory["subjects"] = [{"contact_key": "wechat_id:a", "wechat_id": "a", "nickname": "阿英2"}]
            save_directory(path, directory)

            loaded = load_directory(path, wx_id="wxid_test")

            self.assertEqual(loaded["subjects"][0]["nickname"], "阿英2")
            self.assertEqual(list(path.parent.glob(f".{path.name}.*.tmp")), [])

    def test_relationship_state_replace_failure_keeps_previous_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            state = relationship_scan.save_state(tmp, {
                "wx_id": "wxid_test",
                "records": [{"name": "阿英2", "status": "normal"}],
            })
            path = relationship_scan.state_path(tmp, "wxid_test")
            original = path.read_bytes()
            state["records"][0]["status"] = "blocked"

            with patch("feature.relationship_scan.os.replace", side_effect=OSError("injected")):
                with self.assertRaisesRegex(OSError, "injected"):
                    relationship_scan.save_state(tmp, state)

            self.assertEqual(path.read_bytes(), original)
            self.assertEqual(list(path.parent.glob(f".{path.name}.*.tmp")), [])

    def test_memory_replace_failure_keeps_previous_messages(self):
        with tempfile.TemporaryDirectory() as tmp:
            manager = MemoryManager("wxid_test", tmp)
            manager.save_message("张三", "张三", "旧消息", "text", "friend", 100)

            with patch("core.memory.os.replace", side_effect=OSError("injected")):
                with self.assertRaisesRegex(OSError, "injected"):
                    manager.save_message("张三", "张三", "新消息", "text", "friend", 100)

            self.assertEqual([item["content"] for item in manager.get_messages("张三", 10)], ["旧消息"])

    def test_ui_delivery_journal_freezes_interrupted_send_and_rejects_same_id(self):
        with tempfile.TemporaryDirectory() as tmp:
            journal = UIDeliveryJournal(tmp, "wxid_test")
            self.assertTrue(journal.begin("delivery-1", "send_audio", {"conversation": "张三"}))

            recovered = UIDeliveryJournal(tmp, "wxid_test").freeze_interrupted()

            self.assertEqual(len(recovered), 1)
            self.assertEqual(recovered[0]["status"], "uncertain")
            self.assertIn("禁止自动重发", recovered[0]["error"])
            self.assertFalse(journal.begin("delivery-1", "send_audio", {"conversation": "张三"}))

    def test_ui_delivery_journal_keeps_old_delivery_ids_and_business_context(self):
        with tempfile.TemporaryDirectory() as tmp:
            journal = UIDeliveryJournal(tmp, "wxid_test")
            records = [
                {"delivery_id": f"delivery-{index}", "status": "done"}
                for index in range(1001)
            ]
            journal._save_unlocked(records)

            self.assertFalse(journal.begin("delivery-0", "forward", {"conversation": "张三"}))
            self.assertTrue(journal.begin("delivery-new", "forward", {
                "conversation": "素材群",
                "request_id": "request-1",
                "run_id": "run-1",
                "batch_id": "batch-1",
                "targets": ["阿英2"],
            }))
            stored = journal.records()[-1]
            self.assertEqual(stored["request_id"], "request-1")
            self.assertEqual(stored["run_id"], "run-1")
            self.assertEqual(stored["batch_id"], "batch-1")
            self.assertEqual(stored["targets"], ["阿英2"])

    def test_ui_delivery_journal_records_partial_action_boundary(self):
        with tempfile.TemporaryDirectory() as tmp:
            journal = UIDeliveryJournal(tmp, "wxid_test")
            owner = WeChatUIOwner({
                UIIntentKind.SEND_ACTIONS: lambda _payload: (_ for _ in ()).throw(
                    ActionBatchInterrupted([True], 1, RuntimeError("结果丢失"))
                ),
            })
            owner.set_delivery_journal(journal)
            owner.start()
            try:
                with self.assertRaises(ActionBatchInterrupted):
                    owner.call(UIIntent(UIIntentKind.SEND_ACTIONS, {
                        "conversation": "张三",
                        "delivery_id": "batch-1",
                        "actions": [
                            {"type": "text", "text": "第一条"},
                            {"type": "file", "path": "second.pdf"},
                            {"type": "text", "text": "第三条"},
                        ],
                    }), 1)
            finally:
                owner.stop()

            record = journal.records()[0]
            self.assertEqual(record["status"], "uncertain")
            self.assertEqual(record["details"]["failed_index"], 1)
            self.assertEqual(
                [item["status"] for item in record["details"]["actions"]],
                ["done", "uncertain", "pending"],
            )

    def test_unanswered_inbound_replays_only_ai_started_records_once(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = UnansweredInboundStore(tmp, "wxid_test")
            message = type("Message", (), {
                "content": "你好",
                "original_content": "你好",
                "type": "text",
                "sender": "张三",
                "attr": "friend",
                "id": "msg-1",
                "hash": "hash-1",
                "time": "10:00",
                "_wxbot_received_at": 100.0,
            })()
            ai_record = store.begin("张三", message)
            store.set_status(ai_record, "ai_started")
            store.begin("李四", message)

            recovered = UnansweredInboundStore(tmp, "wxid_test").recover_for_replay()
            recovered_again = UnansweredInboundStore(tmp, "wxid_test").recover_for_replay()

            self.assertEqual([item["conversation"] for item in recovered], ["张三"])
            self.assertEqual([item["conversation"] for item in recovered_again], ["张三"])
            statuses = {item["conversation"]: item["status"] for item in store.records()}
            self.assertEqual(statuses, {"张三": "replay_pending", "李四": "uncertain"})

    def test_unanswered_inbound_retains_all_safety_records_when_resolved_history_is_trimmed(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = UnansweredInboundStore(tmp, "wxid_test")
            records = [{"record_id": "critical", "status": "voice_pending"}]
            records.extend(
                {"record_id": f"resolved-{index}", "status": "resolved"}
                for index in range(501)
            )

            store._save_unlocked(records)
            stored = store.records()

            self.assertEqual(len(stored), 501)
            self.assertEqual(stored[0]["record_id"], "critical")
            self.assertNotIn("resolved-0", {item["record_id"] for item in stored})

    def test_voice_pending_record_is_preserved_for_safe_history_only_recovery(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = UnansweredInboundStore(tmp, "wxid_test")
            message = type("Message", (), {
                "content": '语音8"秒',
                "original_content": '语音8"秒',
                "type": "voice",
                "sender": "张三",
                "attr": "friend",
                "id": "voice-1",
                "hash": "voice-hash-1",
                "hash_text": "",
                "time": "10:00",
                "_wxbot_received_at": 100.0,
            })()

            record_id = store.begin("张三", message, status="voice_pending")

            self.assertEqual(store.recover_for_replay(), [])
            self.assertEqual([item["record_id"] for item in store.pending("voice_pending")], [record_id])

    def test_unanswered_group_record_preserves_type_and_datetime(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = UnansweredInboundStore(tmp, "wxid_test")
            received_at = datetime(2026, 7, 11, 8, 0, 0)
            message = type("Message", (), {
                "content": "@机器人 你好",
                "original_content": "@机器人 你好",
                "type": "text",
                "sender": "群成员",
                "attr": "friend",
                "id": "group-msg-1",
                "hash": "group-hash-1",
                "time": "08:00",
                "_wxbot_received_at": received_at,
            })()

            record_id = store.begin("测试群", message, chat_type="group")
            record = next(item for item in store.records() if item["record_id"] == record_id)

            self.assertEqual(record["chat_type"], "group")
            self.assertEqual(record["message"]["sender"], "群成员")
            self.assertEqual(record["received_at"], received_at.timestamp())

    def test_unanswered_send_started_is_frozen_and_never_replayed(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = UnansweredInboundStore(tmp, "wxid_test")
            message = type("Message", (), {
                "content": "你好",
                "original_content": "你好",
                "type": "text",
                "sender": "张三",
                "attr": "friend",
                "id": "msg-1",
                "hash": "hash-1",
                "time": "10:00",
                "_wxbot_received_at": 100.0,
            })()
            record_id = store.begin("张三", message)
            store.set_status(record_id, "send_started")

            recovered = UnansweredInboundStore(tmp, "wxid_test").recover_for_replay()

            self.assertEqual(recovered, [])
            self.assertEqual(store.records()[0]["status"], "uncertain")

    def test_material_inflight_is_frozen_as_uncertain(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = MaterialOutreachStorage(tmp, "wxid_test")
            record = build_progress_record(
                "run-1",
                "task-1",
                {"contact_key": "wechat_id:a", "send_name": "阿英2", "display_name": "阿英2"},
                "inflight",
            )
            store.append_progress_records([record])

            unresolved = store.freeze_interrupted_sends("task-1")

            self.assertEqual(len(unresolved), 1)
            self.assertEqual(unresolved[0]["status"], "uncertain")
            self.assertIn("禁止自动重发", unresolved[0]["detail"])

    def test_material_namespace_load_freezes_all_inflight_records(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = MaterialOutreachStorage(tmp, "wxid_test")
            store.append_progress_records([
                build_progress_record(
                    "run-1",
                    "task-disabled",
                    {"contact_key": "wechat_id:a", "send_name": "阿英2", "display_name": "阿英2"},
                    "inflight",
                ),
                build_progress_record(
                    "run-2",
                    "task-future",
                    {"contact_key": "wechat_id:b", "send_name": "阿英3", "display_name": "阿英3"},
                    "inflight",
                ),
            ])

            recovered = store.freeze_all_interrupted_sends()

            self.assertEqual(len(recovered), 2)
            latest = {}
            for item in store.load_progress_records():
                latest[(item["run_id"], item["contact_key"])] = item
            self.assertEqual({item["status"] for item in latest.values()}, {"uncertain"})


if __name__ == "__main__":
    unittest.main()
