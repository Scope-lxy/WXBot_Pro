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
            new_friend_remark_use_nickname=True,
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


if __name__ == "__main__":
    unittest.main()
