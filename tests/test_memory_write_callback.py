import unittest
from types import SimpleNamespace

from wxbot_core import WXBot


class CaptureMemory:
    def __init__(self):
        self.calls = []

    def save_message(self, **kwargs):
        self.calls.append(kwargs)


class MemoryWriteCallbackTests(unittest.TestCase):
    def test_message_callback_saves_memory_without_external_message_time(self):
        bot = WXBot.__new__(WXBot)
        bot.config = SimpleNamespace(
            memory_switch=True,
            memory_max_count=5000,
            group_welcome=False,
            group=[],
            cmd="admin",
        )
        bot.memory_manager = CaptureMemory()
        bot._should_skip_message_memory = lambda chat, msg: False
        bot._maybe_update_conversation_memory = lambda chat, msg: None
        bot.callback_is_die = False
        bot.wx = SimpleNamespace(nickname="bot")
        bot.is_err = lambda *args, **kwargs: self.fail(f"unexpected error: {args}")

        msg = SimpleNamespace(attr="system", sender="system", content="hello", type="text")
        chat = SimpleNamespace(who="chat-a", chat_type="private")

        bot.message_handle_callback(msg, chat)

        self.assertEqual(len(bot.memory_manager.calls), 1)
        self.assertEqual(bot.memory_manager.calls[0]["chat_name"], "chat-a")
        self.assertNotIn("message_time", bot.memory_manager.calls[0])


if __name__ == "__main__":
    unittest.main()
