import tempfile
import unittest

from core.message_store import MessageStore


class MessageStoreChatTypeContractTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = MessageStore(self.temp.name, "wxid_test")

    def tearDown(self):
        self.temp.cleanup()

    def test_all_persistence_entry_points_reject_wxautox_friend_type(self):
        inbound = {
            "conversation": "张三",
            "chat_type": "friend",
            "direction": "friend",
            "content": "你好",
            "native_id": "msg-1",
            "received_at": 100,
        }
        history = {
            "event_id": "history-1",
            "conversation": "张三",
            "chat_type": "friend",
            "direction": "friend",
            "content": "你好",
            "received_at": 100,
        }
        writes = (
            lambda: self.store.record_inbound(inbound),
            lambda: self.store.append_inbound_once(
                "event-1", "张三", content="你好", received_at=100, chat_type="friend"
            ),
            lambda: self.store.append_confirmed_outbound_once(
                "delivery-1", "张三", content="收到", sent_at=100, chat_type="friend"
            ),
            lambda: self.store.append_history([history], now=100),
            lambda: self.store.create_reply_job(
                "turn-1",
                conversation="张三",
                expected_version=0,
                expires_at=200,
                event_ids=["event-1"],
                chat_type="friend",
                now=100,
            ),
            lambda: self.store.register_reply_turn(
                "turn-1",
                conversation="张三",
                expected_version=0,
                expires_at=200,
                event_ids=["event-1"],
                action_count=1,
                chat_type="friend",
                now=100,
            ),
            lambda: self.store.confirm_outbound(
                "turn-1:0", "张三", content="收到", sent_at=100, chat_type="friend"
            ),
        )

        for write in writes:
            with self.subTest(write=write):
                with self.assertRaisesRegex(ValueError, "unsupported chat_type: friend"):
                    write()

        self.assertEqual(self.store.history("张三", 10), [])


if __name__ == "__main__":
    unittest.main()
