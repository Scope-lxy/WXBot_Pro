import unittest
from types import SimpleNamespace

from feature import message_routing, takeover_runtime


class FakeAdminChat:
    def __init__(self, who="LXYou"):
        self.who = who
        self.chat_type = "private"
        self.sent = []

    def SendMsg(self, message=None, msg=None, **_kwargs):
        payload = message if message is not None else msg
        self.sent.append(str(payload or ""))
        return True


class FakeBot:
    def __init__(self):
        self.config = SimpleNamespace(
            cmd="LXYou",
            AllListen_switch=False,
            listen_list=[],
            group=[],
            group_switch=False,
            custom_forward_switch=False,
        )
        self.wx = SimpleNamespace(nickname="wxbot")
        self.msg_received_count = 0
        self._pause_chat_reply_users = set()
        self._admin_workspace_state = {"mode": takeover_runtime.IDLE_MODE}

    def _record_received_message(self):
        self.msg_received_count += 1

    def _handle_admin_forward_input(self, _chat, _msg):
        return False

    def _handle_admin_moments_input(self, _chat, _msg):
        return False

    def _handle_material_source_message(self, _chat, _msg):
        return False

    def _mark_message_skip_memory(self, message):
        message._skip_memory = True

    def process_message(self, chat, message):
        return takeover_runtime.route_admin_plain_message(self, chat, message)

    def is_err(self, *_args, **_kwargs):
        raise AssertionError("unexpected error path")

    def _result_error_text(self, result):
        return str(result)

    def _consume_private_reply_runtime_echo(self, *_args, **_kwargs):
        self.consumed_runtime_echo = True

    def message_handle_callback(self, msg, chat):
        if takeover_runtime.consume_admin_chat_echo_message(self, chat, msg):
            self._mark_message_skip_memory(msg)
            self._consume_private_reply_runtime_echo(chat.who, getattr(msg, "content", ""))
            return True
        if msg.attr == "friend":
            return message_routing.handle_friend_message_callback(self, msg, chat, text="")
        return self.process_message(chat, msg)


class AdminEchoFilterTests(unittest.TestCase):
    def test_admin_idle_prompt_sent_from_friend_branch_is_recorded_as_echo(self):
        bot = FakeBot()
        chat = FakeAdminChat()
        msg = SimpleNamespace(type="text", attr="friend", sender="LXYou", content="你好")

        result = message_routing.handle_friend_message_callback(bot, msg, chat, text="")

        self.assertIsNone(result)
        self.assertEqual(bot.msg_received_count, 1)
        self.assertEqual(len(chat.sent), 1)
        self.assertIn("当前无活动会话", chat.sent[0])
        self.assertTrue(takeover_runtime.consume_admin_echo_message(bot, chat.sent[0]))
        self.assertFalse(takeover_runtime.consume_admin_echo_message(bot, chat.sent[0]))

    def test_admin_echo_match_normalizes_newlines_and_spaces(self):
        bot = FakeBot()

        takeover_runtime.remember_admin_echo_message(bot, "看了下后台，运行挺正常的\r\n不过最近忙得有点顾不上你了")

        self.assertTrue(
            takeover_runtime.consume_admin_echo_message(
                bot,
                "  看了下后台，运行挺正常的\n 不过最近忙得有点顾不上你了  ",
            )
        )
        self.assertFalse(
            takeover_runtime.consume_admin_echo_message(
                bot,
                "看了下后台，运行挺正常的\n不过最近忙得有点顾不上你了",
            )
        )

    def test_admin_echo_marked_as_friend_is_consumed_before_admin_routing(self):
        bot = FakeBot()
        chat = FakeAdminChat()
        takeover_runtime.remember_admin_echo_message(bot, "机器人状态\n运行时间：1分钟")

        msg = SimpleNamespace(type="text", attr="friend", sender="LXYou", content="机器人状态\n运行时间：1分钟")

        self.assertTrue(bot.message_handle_callback(msg, chat))
        self.assertEqual(chat.sent, [])
        self.assertTrue(getattr(msg, "_skip_memory", False))
        self.assertTrue(getattr(bot, "consumed_runtime_echo", False))


if __name__ == "__main__":
    unittest.main()
