import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from core.contact_profiles import default_directory, load_directory, save_directory
from core.memory import MemoryManager
from core.message_store import MessageStore, SQLiteUIDeliveryJournal
from core.wechat_ui_actions import ActionBatchInterrupted, UIIntent, UIIntentKind, WeChatUIOwner
from feature import relationship_scan
from feature.material_outreach import build_progress_record
from feature.material_outreach_storage import MaterialOutreachStorage


ROOT = Path(__file__).resolve().parents[1]


class RuntimeStorageSafetyTests(unittest.TestCase):
    @staticmethod
    def _delivery_journal(base_dir):
        store = MessageStore(base_dir, "wxid_test")
        return SQLiteUIDeliveryJournal(store)

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
from core.message_store import MessageStore, SQLiteUIDeliveryJournal

journal = SQLiteUIDeliveryJournal(MessageStore({tmp!r}, 'wxid_test'))
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

                    journal = self._delivery_journal(tmp)
                    recovered = journal.freeze_interrupted()
                    records = journal.records()
                    if expected is None:
                        self.assertEqual(recovered, [])
                        self.assertEqual(records, [])
                    else:
                        self.assertEqual(len(records), 1)
                        self.assertEqual(records[0]["kind"], kind)
                        self.assertEqual(records[0]["status"], expected)
                        if expected == "uncertain":
                            self.assertEqual(recovered[0]["status"], "uncertain")
                        else:
                            self.assertEqual(recovered, [])

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

    def test_memory_sqlite_write_failure_keeps_previous_messages(self):
        with tempfile.TemporaryDirectory() as tmp:
            manager = MemoryManager("wxid_test", tmp)
            old_entry = {
                "event_id": "old-message",
                "conversation": "张三",
                "chat_type": "private",
                "direction": "friend",
                "sender": "张三",
                "content": "旧消息",
                "original_content": "旧消息",
                "message_type": "text",
                "native_attr": "friend",
                "received_at": 1.0,
                "metadata": {},
            }
            manager.message_store.append_history([old_entry])

            with patch.object(
                manager.message_store,
                "append_history",
                side_effect=OSError("injected"),
            ):
                with self.assertRaisesRegex(OSError, "injected"):
                    manager.message_store.append_history([
                        dict(old_entry, event_id="new-message", content="新消息", original_content="新消息")
                    ])

            self.assertEqual(
                [item["content"] for item in manager.get_messages(
                    "张三",
                    10,
                    chat_type="private",
                )],
                ["旧消息"],
            )

    def test_ui_delivery_journal_freezes_interrupted_send_and_rejects_same_id(self):
        with tempfile.TemporaryDirectory() as tmp:
            journal = self._delivery_journal(tmp)
            self.assertTrue(journal.begin("delivery-1", "send_audio", {"conversation": "张三"}))

            recovered = self._delivery_journal(tmp).freeze_interrupted()

            self.assertEqual(len(recovered), 1)
            self.assertEqual(recovered[0]["status"], "uncertain")
            self.assertIn("process exited", recovered[0]["error"])
            self.assertFalse(journal.begin("delivery-1", "send_audio", {"conversation": "张三"}))

    def test_ui_delivery_journal_keeps_old_delivery_ids_and_business_context(self):
        with tempfile.TemporaryDirectory() as tmp:
            journal = self._delivery_journal(tmp)
            for index in range(1001):
                self.assertTrue(journal.begin(
                    f"delivery-{index}",
                    "forward",
                    {"conversation": "张三"},
                ))

            self.assertFalse(journal.begin("delivery-0", "forward", {"conversation": "张三"}))
            self.assertTrue(journal.begin("delivery-new", "forward", {
                "conversation": "素材群",
                "request_id": "request-1",
                "run_id": "run-1",
                "batch_id": "batch-1",
                "targets": ["阿英2"],
            }))
            stored = next(
                item for item in journal.records()
                if item["delivery_id"] == "delivery-new"
            )
            self.assertEqual(stored["metadata"], {
                "request_id": "request-1",
                "run_id": "run-1",
                "batch_id": "batch-1",
                "targets": ["阿英2"],
            })

    def test_ui_delivery_journal_records_partial_action_boundary(self):
        with tempfile.TemporaryDirectory() as tmp:
            journal = self._delivery_journal(tmp)
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
