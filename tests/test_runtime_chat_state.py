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

            def __init__(self):
                self.remembered = []

            def _verified_send_chat(self, target, candidate=None):
                raise AssertionError("cached send should not touch verifier")

            def _get_chat_send_lock(self, _target):
                class Lock:
                    def __enter__(self):
                        return self

                    def __exit__(self, exc_type, exc, tb):
                        return None

                return Lock()

            def _remember_private_outbound_echo_for_send_result(
                self,
                target,
                result,
                msg_type="text",
                content="",
                *,
                source="",
                path="",
                count=1,
            ):
                self.remembered.append((target, result, msg_type, content, source, path, count))

        bot = Bot()

        result = runtime_chat_state.send_text_to_target(bot, "阿英2", "你好")

        self.assertTrue(result)
        self.assertEqual(sends, [("cached", "你好")])
        self.assertEqual(bot.remembered, [("阿英2", True, "text", "你好", "runtime_send", "", 1)])

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

    def test_send_text_queues_when_wechat_action_lock_is_busy_even_with_cached_chat(self):
        sends = []

        class CachedChat:
            def SendMsg(self, msg):
                sends.append(("cached", msg))
                return True

        class BusyLock:
            def acquire(self, blocking=True):
                return False

            def release(self):
                raise AssertionError("busy lock should not be released")

        bot = SimpleNamespace(
            _listen_chats={"阿英2": CachedChat()},
            _get_wechat_action_lock=lambda: BusyLock(),
            _send_text_to_target_without_child=lambda target, msg: sends.append(("queued", target, msg)) or {
                "status": "queued"
            },
        )

        result = runtime_chat_state.send_text_to_target(bot, "阿英2", "你好")

        self.assertEqual(result, {"status": "queued"})
        self.assertEqual(sends, [("queued", "阿英2", "你好")])

    def test_send_file_queues_when_wechat_action_lock_is_busy_even_with_cached_chat(self):
        sends = []

        class CachedChat:
            def SendFiles(self, filepath=None):
                sends.append(("cached", filepath))
                return True

        class BusyLock:
            def acquire(self, blocking=True):
                return False

            def release(self):
                raise AssertionError("busy lock should not be released")

        bot = SimpleNamespace(
            _listen_chats={"阿英2": CachedChat()},
            _get_wechat_action_lock=lambda: BusyLock(),
            _send_file_to_target_without_child=lambda target, path: sends.append(("queued", target, path)) or {
                "status": "queued"
            },
        )

        result = runtime_chat_state.send_file_to_target(bot, "阿英2", r"C:\tmp\a.pdf")

        self.assertEqual(result, {"status": "queued"})
        self.assertEqual(sends, [("queued", "阿英2", r"C:\tmp\a.pdf")])


if __name__ == "__main__":
    unittest.main()
