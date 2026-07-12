import threading
import unittest
from types import SimpleNamespace

from wxbot_core import WXBot


class FakeStore:
    def __init__(self):
        self.events = []

    def begin(self, conversation, message, *, chat_type="private"):
        self.events.append(("begin", conversation, message.content))
        return "record-1"

    def resolve(self, record_id):
        self.events.append(("resolve", record_id))

    def set_status(self, record_id, status):
        self.events.append(("status", record_id, status))


class UnansweredInboundWrapperTests(unittest.TestCase):
    def test_normal_private_pipeline_completion_resolves_record(self):
        bot = WXBot.__new__(WXBot)
        store = FakeStore()
        bot._unanswered_inbound_store = store
        bot._unanswered_inbound_context = threading.local()
        bot._wx_send_ai_once = lambda _chat, _message: "ok"

        result = WXBot.wx_send_ai(
            bot,
            SimpleNamespace(who="张三"),
            SimpleNamespace(content="你好"),
        )

        self.assertEqual(result, "ok")
        self.assertEqual(store.events, [("begin", "张三", "你好"), ("resolve", "record-1")])

    def test_exception_keeps_record_unresolved_for_restart(self):
        bot = WXBot.__new__(WXBot)
        store = FakeStore()
        bot._unanswered_inbound_store = store
        bot._unanswered_inbound_context = threading.local()

        def fail(_chat, _message):
            raise RuntimeError("injected")

        bot._wx_send_ai_once = fail

        with self.assertRaisesRegex(RuntimeError, "injected"):
            WXBot.wx_send_ai(
                bot,
                SimpleNamespace(who="张三"),
                SimpleNamespace(content="你好"),
            )

        self.assertEqual(store.events, [("begin", "张三", "你好")])

    def test_replay_reuses_original_record_instead_of_creating_a_chain(self):
        bot = WXBot.__new__(WXBot)
        store = FakeStore()
        bot._unanswered_inbound_store = store
        bot._unanswered_inbound_context = threading.local()
        bot._wx_send_ai_once = lambda _chat, _message: "ok"
        message = SimpleNamespace(content="你好", _wxbot_recovery_record_id="old-record")

        result = WXBot.wx_send_ai(bot, SimpleNamespace(who="张三"), message)

        self.assertEqual(result, "ok")
        self.assertEqual(store.events, [
            ("status", "old-record", "replaying"),
            ("resolve", "old-record"),
        ])


if __name__ == "__main__":
    unittest.main()
