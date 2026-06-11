import unittest
from types import SimpleNamespace
from unittest import mock

from feature import listening


class RemoveListenChatTests(unittest.TestCase):
    def test_remove_listen_chat_does_not_require_outer_wechat_action_lock(self):
        removed = []

        class FakeBot:
            _listen_chats = {"张三": object()}
            wx = SimpleNamespace(
                RemoveListenChat=lambda _nickname: removed.append(_nickname) or {"status": "成功", "message": "ok"},
                GetAllSubWindow=lambda: [],
            )

            def _get_wechat_action_lock(self):
                raise AssertionError("RemoveListenChat should not take the outer bot UI lock")

        result = listening.remove_listen_chat_verified(FakeBot(), "张三")

        self.assertTrue(result)
        self.assertEqual(removed, ["张三"])

    def test_remove_listen_chat_logs_wxautox_return_value(self):
        bot = SimpleNamespace(
            _listen_chats={"张三": object()},
            wx=SimpleNamespace(
                RemoveListenChat=lambda _nickname: {"status": "失败", "message": "窗口忙"},
                GetAllSubWindow=lambda: [SimpleNamespace(who="张三")],
            ),
        )
        logs = []

        with mock.patch.object(listening, "_bot_log", side_effect=lambda _bot, *args, **kwargs: logs.append(kwargs.get("message") or (args[0] if args else ""))):
            result = listening.remove_listen_chat_verified(bot, "张三")

        self.assertFalse(result)
        self.assertTrue(any("张三 删除监听返回" in item and "窗口忙" in item for item in logs))


if __name__ == "__main__":
    unittest.main()
