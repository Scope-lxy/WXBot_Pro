import unittest
from types import SimpleNamespace

from core import runtime_chat_state


class RuntimeChatStateSendTests(unittest.TestCase):
    def test_send_text_uses_cached_chat_without_revalidation(self):
        sends = []

        class CachedChat:
            def SendMsg(self, msg):
                sends.append(("cached", msg))
                return True

        class Bot:
            _listen_chats = {"阿英2": CachedChat()}

            def _verified_send_chat(self, target, candidate=None):
                raise AssertionError("cached send should not touch verifier")

            def _get_chat_send_lock(self, _target):
                class Lock:
                    def __enter__(self):
                        return self

                    def __exit__(self, exc_type, exc, tb):
                        return None

                return Lock()

        bot = Bot()

        result = runtime_chat_state.send_text_to_target(bot, "阿英2", "你好")

        self.assertTrue(result)
        self.assertEqual(sends, [("cached", "你好")])

    def test_send_text_falls_back_when_cached_chat_is_not_verified(self):
        sends = []

        class WrongChat:
            who = "别人"

        bot = SimpleNamespace(
            _listen_chats={"阿英2": WrongChat()},
            _verified_send_chat=lambda _target, _candidate=None: None,
            _send_text_to_target_without_child=lambda target, msg: sends.append(("fallback", target, msg)) or True,
        )

        result = runtime_chat_state.send_text_to_target(bot, "阿英2", "你好")

        self.assertTrue(result)
        self.assertEqual(sends, [("fallback", "阿英2", "你好")])
        self.assertNotIn("阿英2", bot._listen_chats)


if __name__ == "__main__":
    unittest.main()
