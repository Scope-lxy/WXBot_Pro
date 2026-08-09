import tempfile
import unittest

from core.message_store import MessageStore


class MessageStoreRetentionTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)

    @staticmethod
    def append(store, number, *, conversation="Alice"):
        return store.append_confirmed_outbound_once(
            f"delivery-{conversation}-{number}",
            conversation,
            content=f"message-{number}",
            sent_at=float(number),
        )

    def test_configured_limit_keeps_latest_history_and_removes_old_event(self):
        store = MessageStore(self.temp_dir.name, "account", history_limit=2)

        first_event = None
        for number in range(1, 4):
            result = self.append(store, number)
            if number == 1:
                first_event = result["event_id"]

        history = store.history("Alice", 10)
        self.assertEqual([event["content"] for event in history], ["message-2", "message-3"])
        self.assertIsNone(store.get_event(first_event))

    def test_setting_limit_prunes_existing_history_per_conversation(self):
        store = MessageStore(self.temp_dir.name, "account")
        for number in range(1, 4):
            self.append(store, number, conversation="Alice")
            self.append(store, number, conversation="Bob")

        self.assertEqual(store.set_history_limit(2), 2)
        self.assertEqual(len(store.history("Alice", 10)), 2)
        self.assertEqual(len(store.history("Bob", 10)), 2)

        self.append(store, 4, conversation="Alice")
        self.assertEqual(
            [event["content"] for event in store.history("Alice", 10)],
            ["message-3", "message-4"],
        )

    def test_pending_inbound_is_preserved_for_startup_recovery(self):
        store = MessageStore(self.temp_dir.name, "account", history_limit=2)
        pending = store.append_inbound_once(
            "pending-event",
            "Alice",
            content="needs recovery",
            received_at=1.0,
            expires_at=100.0,
            now=1.0,
        )

        self.append(store, 2)
        self.append(store, 3)

        self.assertEqual(len(store.history("Alice", 10)), 3)
        self.assertEqual(
            [event["event_id"] for event in store.recover_pending_inbound(now=10.0)],
            [pending["event_id"]],
        )


if __name__ == "__main__":
    unittest.main()
