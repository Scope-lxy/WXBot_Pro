import threading
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from feature import listening


class FakeNewFriend:
    name = "阿英2"

    def __init__(self, calls):
        self.calls = calls

    def accept(self, **kwargs):
        self.calls.append(("accept", kwargs))


class FakeWeChat:
    def __init__(self, calls):
        self.calls = calls

    def GetNewFriends(self, acceptable=True):
        self.calls.append(("GetNewFriends", acceptable))
        return [FakeNewFriend(self.calls)]

    def SwitchToChat(self):
        self.calls.append(("SwitchToChat",))

    def SendMsg(self, who=None, msg=None, **_kwargs):
        self.calls.append(("SendMsg", who, msg))

    def SendFiles(self, who=None, filepath=None, **_kwargs):
        self.calls.append(("SendFiles", who, filepath))

    def ChatWith(self, who=None, **_kwargs):
        self.calls.append(("ChatWith", who))

    def SwitchToContact(self):
        self.calls.append(("SwitchToContact",))


class FakeBot:
    def __init__(self, *, reply_switch=True):
        self.calls = []
        self.wx = FakeWeChat(self.calls)
        self.config = SimpleNamespace(
            new_friend_archive_switch=True,
            new_friend_remark_prefix="",
            new_friend_remark_suffix="",
            new_friend_remark_prefix_timestamp=False,
            new_friend_remark_suffix_timestamp=False,
            new_friend_tags=[],
            new_friend_reply_switch=reply_switch,
            new_friend_msg={"text": "欢迎", "files": ["C:\\Docs\\a.pdf"]},
            human_delay=lambda: None,
        )
        self._lock = threading.Lock()

    def _get_wechat_action_lock(self):
        return self._lock


class PassNewFriendsTests(unittest.TestCase):
    def test_pass_new_friends_uses_configured_welcome_message(self):
        bot = FakeBot(reply_switch=True)

        with patch("feature.listening.time.sleep", return_value=None):
            self.assertTrue(listening.pass_new_friends(bot))

        self.assertIn(("SendMsg", "阿英2", "欢迎"), bot.calls)
        self.assertIn(("SendFiles", "阿英2", "C:\\Docs\\a.pdf"), bot.calls)

    def test_pass_new_friends_respects_reply_switch(self):
        bot = FakeBot(reply_switch=False)

        with patch("feature.listening.time.sleep", return_value=None):
            self.assertTrue(listening.pass_new_friends(bot))

        self.assertFalse(any(call[0] in {"SendMsg", "SendFiles"} for call in bot.calls))

    def test_owner_new_friend_welcome_submits_each_action_separately(self):
        intents = []
        delays = []

        class Owner:
            def call(self, intent, _timeout):
                intents.append(intent)
                if intent.kind == listening.wechat_ui_actions.UIIntentKind.NEW_FRIEND:
                    return [{"name": "阿英2", "send_name": "阿英2_新好友"}]
                return [True]

        bot = SimpleNamespace(
            _ui_owner=Owner(),
            config=SimpleNamespace(
                new_friend_archive_switch=True,
                new_friend_remark_prefix="",
                new_friend_remark_suffix="_新好友",
                new_friend_remark_prefix_timestamp=False,
                new_friend_remark_suffix_timestamp=False,
                new_friend_tags=["新好友"],
                new_friend_reply_switch=True,
                new_friend_msg={"text": "欢迎", "files": [r"C:\Docs\a.pdf"]},
            ),
            _config_ui_task_guard=lambda _category: ("new_friend", 3),
            _inter_message_delay_or_stop=lambda: delays.append("delay"),
        )

        with patch("feature.listening.time.sleep", return_value=None):
            self.assertTrue(listening.pass_new_friends(bot))

        send_intents = [
            intent for intent in intents
            if intent.kind == listening.wechat_ui_actions.UIIntentKind.SEND_ACTIONS
        ]
        self.assertEqual(len(send_intents), 2)
        self.assertEqual([len(intent.payload["actions"]) for intent in send_intents], [1, 1])
        self.assertEqual(send_intents[0].payload["actions"][0]["type"], "text")
        self.assertEqual(send_intents[1].payload["actions"][0]["type"], "file")
        self.assertEqual(delays, ["delay"])


class GroupWelcomeTests(unittest.TestCase):
    def test_group_welcome_sends_while_holding_wechat_ui_lock(self):
        class RecordingLock:
            locked = False

            def __enter__(self):
                self.locked = True
                return self

            def __exit__(self, exc_type, exc, tb):
                self.locked = False
                return False

        lock = RecordingLock()
        sent = []
        bot = SimpleNamespace(
            config=SimpleNamespace(group_welcome_msg="欢迎"),
            _get_wechat_action_lock=lambda: lock,
            _get_chat_send_lock=lambda _name: threading.Lock(),
        )
        chat = SimpleNamespace(
            who="测试群",
            SendMsg=lambda msg=None, at=None: sent.append((msg, at, lock.locked)) or True,
        )
        messages = (
            ('"张三"加入群聊', "张三"),
            ('"李四"通过扫描二维码加入了群聊', "李四"),
        )

        with patch("feature.listening.time.sleep", return_value=None):
            for content, expected_name in messages:
                with self.subTest(content=content):
                    result = listening.send_group_welcome_msg(
                        bot,
                        chat,
                        SimpleNamespace(content=content),
                    )
                    self.assertTrue(result)
                    self.assertEqual(sent[-1], ("欢迎", expected_name, True))

        self.assertEqual(len(sent), 2)


if __name__ == "__main__":
    unittest.main()
