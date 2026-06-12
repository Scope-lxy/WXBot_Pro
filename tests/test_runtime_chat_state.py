import unittest
from types import SimpleNamespace

from core import runtime_chat_state


class RuntimeChatStateSendTests(unittest.TestCase):
    def test_send_text_revalidates_cached_chat_before_sending(self):
        sends = []

        class WrongChat:
            def SendMsg(self, msg):
                sends.append(("wrong", msg))
                return True

        class RightChat:
            def SendMsg(self, msg):
                sends.append(("right", msg))
                return True

        right_chat = RightChat()

        class Bot:
            _listen_chats = {"阿英2": WrongChat()}

            def _verified_send_chat(self, target, candidate=None):
                self.verified_args = (target, candidate)
                return right_chat

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
        self.assertEqual(sends, [("right", "你好")])
        self.assertIs(bot._listen_chats["阿英2"], right_chat)

    def test_send_text_falls_back_when_cached_chat_is_not_verified(self):
        sends = []

        class WrongChat:
            def SendMsg(self, msg):
                sends.append(("wrong", msg))
                return True

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
