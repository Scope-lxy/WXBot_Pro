import sqlite3
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from core.message_store import (
    MessageStore,
    MessageStoreConflictError,
    MessageStoreTransitionError,
)


def inbound(
    native_id,
    *,
    content="hello",
    direction="friend",
    received_at=100.0,
    source="callback",
    source_batch="batch-1",
    source_order=0,
    native_hash="",
    native_hash_text="",
    related_delivery_id="",
):
    return {
        "conversation": "Alice",
        "chat_type": "private",
        "content": content,
        "original_content": content,
        "message_type": "text",
        "sender": "Alice" if direction == "friend" else "self",
        "native_attr": "friend" if direction == "friend" else "self",
        "native_id": native_id,
        "native_hash": native_hash,
        "native_hash_text": native_hash_text,
        "native_time": "10:00",
        "received_at": received_at,
        "stored_at": received_at + 0.1,
        "source": source,
        "source_batch": source_batch,
        "source_order": source_order,
        "direction": direction,
        "related_delivery_id": related_delivery_id,
    }


class MessageStoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = MessageStore(self.temp.name, "wxid_test")

    def tearDown(self):
        self.temp.cleanup()

    def record(self, native_id, **kwargs):
        return self.store.record_inbound(inbound(native_id, **kwargs))

    def register(self, turn_id, event_ids, *, version, expires_at=1000.0, count=1):
        return self.store.register_reply_turn(
            turn_id,
            conversation="Alice",
            expected_version=version,
            expires_at=expires_at,
            event_ids=event_ids,
            action_count=count,
            now=110.0,
        )

    def test_store_is_account_scoped(self):
        self.assertEqual(
            self.store.path,
            Path(self.temp.name) / "accounts" / "wxid_test" / "message_store.sqlite3",
        )

    def test_record_inbound_is_idempotent_and_advances_version_once(self):
        first = self.record("msg-1")
        duplicate = self.record("msg-1")

        self.assertTrue(first["is_new"])
        self.assertFalse(duplicate["is_new"])
        self.assertEqual(first["event_id"], duplicate["event_id"])
        self.assertEqual(self.store.conversation_version("Alice"), 1)

    def test_same_native_identity_with_different_fact_fails_closed(self):
        self.record("msg-1", content="one")
        with self.assertRaises(MessageStoreConflictError):
            self.record("msg-1", content="different")
        self.assertEqual(self.store.conversation_version("Alice"), 1)

    def test_later_observation_of_same_native_id_keeps_first_boundary_facts(self):
        first = self.record("msg-1", received_at=100, native_hash="hash-a")
        later = self.record(
            "msg-1",
            received_at=999,
            source="global",
            source_batch="later-poll",
            native_hash="hash-b",
        )

        self.assertFalse(later["is_new"])
        self.assertEqual(later["event_id"], first["event_id"])
        self.assertEqual(self.store.get_event(first["event_id"])["received_at"], 100)
        self.assertEqual(self.store.conversation_version("Alice"), 1)

    def test_observation_identity_keeps_repeated_text_occurrences(self):
        one = self.record(
            "",
            content="same",
            source_order=1,
            native_hash="same-hash",
            native_hash_text="same-hash-text",
        )
        two = self.record(
            "",
            content="same",
            source_order=2,
            native_hash="same-hash",
            native_hash_text="same-hash-text",
        )

        self.assertNotEqual(one["event_id"], two["event_id"])
        self.assertEqual(self.store.conversation_version("Alice"), 2)

    def test_manual_self_advances_but_bot_echo_does_not(self):
        friend = self.record("friend-1")
        manual = self.record("self-1", direction="manual_self")
        echo = self.record("echo-1", direction="bot_echo")

        self.assertEqual(friend["version"], 1)
        self.assertEqual(manual["version"], 2)
        self.assertEqual(echo["version"], 2)
        self.assertEqual(self.store.conversation_version("Alice"), 2)

    def test_concurrent_duplicate_append_has_one_fact_and_one_version_advance(self):
        event = inbound("concurrent-1")
        barrier = threading.Barrier(12)

        def append():
            barrier.wait()
            return self.store.record_inbound(event)

        with ThreadPoolExecutor(max_workers=12) as executor:
            results = list(executor.map(lambda _index: append(), range(12)))

        self.assertEqual(sum(result["is_new"] for result in results), 1)
        self.assertEqual(self.store.conversation_version("Alice"), 1)
        self.assertEqual(len(self.store.history("Alice", 20)), 1)

    def test_history_excludes_active_events_and_outbound_is_unique_by_delivery(self):
        first = self.record("msg-1", content="first", received_at=100.0)
        second = self.record("msg-2", content="current", received_at=101.0)
        outbound_result = self.store.append_confirmed_outbound_once(
            "delivery-1",
            "Alice",
            content="answer",
            sent_at=102.0,
            now=102.1,
        )
        duplicate = self.store.append_confirmed_outbound_once(
            "delivery-1",
            "Alice",
            content="answer",
            sent_at=102.0,
            now=999.0,
        )

        history = self.store.history(
            "Alice",
            20,
            exclude_event_ids=[second["event_id"]],
        )
        self.assertTrue(outbound_result["is_new"])
        self.assertFalse(duplicate["is_new"])
        self.assertEqual([item["content"] for item in history], ["first", "answer"])
        self.assertEqual(history[0]["event_id"], first["event_id"])

    def test_pending_inbound_recovery_expires_stale_and_returns_fresh_fifo(self):
        stale = self.store.append_inbound_once(
            "stale", "Alice", content="old", received_at=10, expires_at=20, now=10
        )
        fresh = self.store.append_inbound_once(
            "fresh", "Alice", content="new", received_at=30, expires_at=50, now=30
        )

        recovered = self.store.recover_pending_inbound(now=40)

        self.assertEqual([item["event_id"] for item in recovered], [fresh["event_id"]])
        self.assertEqual(self.store.get_event(stale["event_id"])["processing_state"], "expired")

    def test_mark_events_is_all_or_nothing(self):
        event = self.record("msg-1")
        with self.assertRaises(MessageStoreConflictError):
            self.store.mark_inbound_events([event["event_id"], "missing"], "handled")
        self.assertEqual(self.store.get_event(event["event_id"])["processing_state"], "pending")

    def test_register_turn_is_idempotent_and_conflicts_on_different_metadata(self):
        event = self.record("msg-1")
        self.assertTrue(self.register("turn-1", [event["event_id"]], version=1, count=2))
        self.assertFalse(self.register("turn-1", [event["event_id"]], version=1, count=2))
        with self.assertRaises(MessageStoreConflictError):
            self.register("turn-1", [event["event_id"]], version=1, count=3)

    def test_claim_is_ordered_and_done_actions_are_skipped(self):
        event = self.record("msg-1")
        self.register("turn-1", [event["event_id"]], version=1, count=2)

        self.assertEqual(
            self.store.conditional_claim(
                "turn-1:1", conversation="Alice", expected_version=1, expires_at=1000, now=120
            ),
            "blocked",
        )
        self.assertEqual(
            self.store.conditional_claim(
                "turn-1:0", conversation="Alice", expected_version=1, expires_at=1000, now=120
            ),
            "claimed",
        )
        self.assertEqual(
            self.store.conditional_claim(
                "turn-1:0", conversation="Alice", expected_version=1, expires_at=1000, now=121
            ),
            "blocked",
        )
        confirmed = self.store.confirm_outbound(
            "turn-1:0", "Alice", content="first bubble", sent_at=122, now=122
        )
        self.assertTrue(confirmed["action_finished"])
        self.assertEqual(self.store.delivery_action_status("turn-1:0"), "done")
        self.assertEqual(self.store.delivery_action("turn-1:0")["action_index"], 0)
        self.assertEqual(
            self.store.conditional_claim(
                "turn-1:0", conversation="Alice", expected_version=1, expires_at=1000, now=123
            ),
            "done",
        )
        self.assertEqual(
            self.store.conditional_claim(
                "turn-1:1", conversation="Alice", expected_version=1, expires_at=1000, now=123
            ),
            "claimed",
        )
        self.store.confirm_outbound(
            "turn-1:1", "Alice", content="second bubble", sent_at=124, now=124
        )
        self.assertEqual(self.store.get_reply_job("turn-1")["status"], "done")
        self.assertEqual(self.store.get_event(event["event_id"])["processing_state"], "handled")

    def test_version_race_terminalizes_all_unclaimed_actions_as_stale(self):
        event = self.record("msg-1")
        self.register("turn-1", [event["event_id"]], version=1, count=2)
        self.record("msg-2", received_at=120)

        outcome = self.store.conditional_claim(
            "turn-1:0",
            conversation="Alice",
            expected_version=1,
            expires_at=1000,
            now=130,
        )

        self.assertEqual(outcome, "stale")
        self.assertEqual(self.store.get_reply_job("turn-1")["status"], "stale")
        self.assertEqual(
            [item["status"] for item in self.store.delivery_actions("turn-1")],
            ["stale", "stale"],
        )

    def test_claim_expiry_is_atomic(self):
        event = self.record("msg-1")
        self.register("turn-1", [event["event_id"]], version=1, expires_at=200, count=2)

        outcome = self.store.conditional_claim(
            "turn-1:0",
            conversation="Alice",
            expected_version=1,
            expires_at=200,
            now=200,
        )

        self.assertEqual(outcome, "expired")
        self.assertEqual(self.store.get_reply_job("turn-1")["status"], "expired")
        self.assertEqual(
            [item["status"] for item in self.store.delivery_actions("turn-1")],
            ["expired", "expired"],
        )

    def test_concurrent_claim_has_exactly_one_winner(self):
        event = self.record("msg-1")
        self.register("turn-1", [event["event_id"]], version=1)
        barrier = threading.Barrier(8)

        def claim():
            barrier.wait()
            return self.store.conditional_claim(
                "turn-1:0",
                conversation="Alice",
                expected_version=1,
                expires_at=1000,
                now=120,
            )

        with ThreadPoolExecutor(max_workers=8) as executor:
            outcomes = list(executor.map(lambda _index: claim(), range(8)))

        self.assertEqual(outcomes.count("claimed"), 1)
        self.assertEqual(outcomes.count("blocked"), 7)

    def test_uncertain_finish_never_releases_remaining_bubbles(self):
        event = self.record("msg-1")
        self.register("turn-1", [event["event_id"]], version=1, count=2)
        self.store.conditional_claim(
            "turn-1:0", conversation="Alice", expected_version=1, expires_at=1000, now=120
        )

        self.store.finish("turn-1:0", "uncertain", "UI timeout", now=121)

        self.assertEqual(self.store.get_reply_job("turn-1")["status"], "uncertain")
        self.assertEqual(
            [item["status"] for item in self.store.delivery_actions("turn-1")],
            ["uncertain", "cancelled"],
        )

    def test_startup_recovery_replays_only_unclaimed_fresh_jobs(self):
        replay_event = self.record("replay")
        self.store.create_reply_job(
            "replay-turn",
            conversation="Alice",
            expected_version=1,
            expires_at=1000,
            event_ids=[replay_event["event_id"]],
            now=101,
        )
        self.store.mark_reply_job_generating("replay-turn", now=102)

        claimed_event = self.record("claimed", received_at=103)
        self.register("claimed-turn", [claimed_event["event_id"]], version=2, count=2)
        self.store.conditional_claim(
            "claimed-turn:0", conversation="Alice", expected_version=2, expires_at=1000, now=110
        )

        expired_event = self.record("expired", received_at=111)
        self.register(
            "expired-turn",
            [expired_event["event_id"]],
            version=3,
            expires_at=150,
        )

        recovered = self.store.recover_startup(now=200)

        self.assertEqual(
            [job["turn_id"] for job in recovered["replay_jobs"]],
            ["replay-turn"],
        )
        self.assertEqual(recovered["uncertain_action_ids"], ["claimed-turn:0"])
        self.assertEqual(recovered["expired_job_ids"], ["expired-turn"])
        self.assertEqual(self.store.get_reply_job("claimed-turn")["status"], "uncertain")
        self.assertEqual(
            [item["status"] for item in self.store.delivery_actions("claimed-turn")],
            ["uncertain", "cancelled"],
        )

    def test_clean_shutdown_cancels_unclaimed_jobs_and_unrouted_inbound(self):
        job_event = self.record("job")
        self.register("turn-1", [job_event["event_id"]], version=1)
        pending_event = self.record("pending", received_at=120)

        result = self.store.cancel_unclaimed_on_shutdown(now=130)

        self.assertEqual(result["cancelled_job_ids"], ["turn-1"])
        self.assertEqual(result["cancelled_pending_events"], 1)
        self.assertEqual(self.store.get_reply_job("turn-1")["status"], "cancelled_shutdown")
        self.assertEqual(
            self.store.get_event(pending_event["event_id"])["processing_state"],
            "cancelled",
        )

    def test_illegal_finish_transition_is_rejected(self):
        event = self.record("msg-1")
        self.register("turn-1", [event["event_id"]], version=1)
        with self.assertRaises(MessageStoreTransitionError):
            self.store.confirm_outbound(
                "turn-1:0", "Alice", content="not claimed", sent_at=120, now=120
            )
        self.assertEqual(len(self.store.history("Alice", 10)), 1)

    def test_related_bot_echo_never_creates_a_second_history_event(self):
        inbound_event = self.record("msg-1")
        self.register("turn-1", [inbound_event["event_id"]], version=1)
        self.store.conditional_claim(
            "turn-1:0", conversation="Alice", expected_version=1, expires_at=1000, now=120
        )
        echo = inbound(
            "echo-native-id",
            direction="bot_echo",
            content="answer",
            received_at=121,
            related_delivery_id="turn-1:0",
        )

        before_confirmation = self.store.record_inbound(echo)
        self.assertFalse(before_confirmation["is_new"])
        self.assertEqual(len(self.store.history("Alice", 10)), 1)

        confirmed = self.store.confirm_outbound(
            "turn-1:0", "Alice", content="answer", sent_at=121, now=122
        )
        after_confirmation = self.store.record_inbound(echo)

        self.assertEqual(before_confirmation["event_id"], confirmed["event_id"])
        self.assertEqual(after_confirmation["event_id"], confirmed["event_id"])
        self.assertFalse(after_confirmation["is_new"])
        self.assertEqual([item["content"] for item in self.store.history("Alice", 10)], ["hello", "answer"])

    def test_history_import_marker_skips_rescan_and_batch_is_atomic(self):
        entry = {
            "event_id": "legacy-1",
            "conversation": "Legacy",
            "chat_type": "private",
            "direction": "friend",
            "sender": "Legacy",
            "content": "old",
            "original_content": "old",
            "message_type": "text",
            "native_attr": "friend",
            "native_time": "09:00",
            "received_at": 10,
            "metadata": {},
        }
        self.assertFalse(self.store.migration_completed("legacy-v1"))
        self.assertEqual(self.store.import_history_once("legacy-v1", [entry], now=20), 1)
        self.assertTrue(self.store.migration_completed("legacy-v1"))
        self.assertEqual(self.store.import_history_once("legacy-v1", [], now=30), 0)

        conflict = dict(entry, content="different")
        new_entry = dict(entry, event_id="legacy-2", content="new")
        with self.assertRaises(MessageStoreConflictError):
            self.store.import_history_once(None, [new_entry, conflict], now=40)
        self.assertIsNone(self.store.get_event("legacy-2"))

    def test_history_visual_notes_listing_and_logical_delete(self):
        entries = [
            {
                "event_id": "image-1",
                "conversation": "Pictures",
                "chat_type": "private",
                "direction": "friend",
                "sender": "Pictures",
                "content": "[图片]",
                "original_content": "[图片]",
                "message_type": "image",
                "native_attr": "friend",
                "native_time": "09:00",
                "received_at": 10,
                "metadata": {"image_paths": ["C:/tmp/a.png"]},
            }
        ]
        self.store.import_history_once(None, entries, now=20)

        self.assertEqual(self.store.list_conversations(), ["Pictures"])
        self.assertTrue(
            self.store.attach_visual_notes(
                "Pictures", ["C:/tmp/a.png"], ["a red cup"]
            )
        )
        event = self.store.history("Pictures", 10, chat_type=None)[0]
        self.assertEqual(event["metadata"]["visual_note"], "a red cup")
        self.assertEqual(self.store.delete_conversation("Pictures", now=30), 1)
        self.assertEqual(self.store.history("Pictures", 10, chat_type=None), [])
        self.assertEqual(self.store.list_conversations(), [])

    def test_corrupt_database_is_not_treated_as_empty(self):
        path = self.store.path
        connection = sqlite3.connect(path)
        try:
            connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        finally:
            connection.close()
        path.write_bytes(b"not a sqlite database")
        with self.assertRaises(sqlite3.DatabaseError):
            MessageStore(self.temp.name, "wxid_test")


if __name__ == "__main__":
    unittest.main()
