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


if __name__ == "__main__":
    unittest.main()
