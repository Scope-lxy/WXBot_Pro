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

    def test_remove_listen_chat_closes_residual_window_when_registration_missing(self):
        calls = []
        windows = []

        class ResidualChat:
            who = "张三"

            def Close(self):
                calls.append(("Close", self.who))
                windows.clear()

        class FakeWeChat:
            def RemoveListenChat(self, nickname, close_window=True):
                calls.append(("RemoveListenChat", nickname, close_window))
                return {"status": "失败", "message": "未找到监听对象"}

            def GetAllSubWindow(self):
                return list(windows)

            def GetSubWindow(self, nickname=None):
                calls.append(("GetSubWindow", nickname))
                return windows[0] if windows else None

        windows.append(ResidualChat())
        bot = SimpleNamespace(
            _listen_chats={"张三": windows[0]},
            _material_source_chats={"张三": windows[0]},
            wx=FakeWeChat(),
        )

        with mock.patch.object(listening, "_bot_sleep", return_value=None):
            result = listening.remove_listen_chat_verified(bot, "张三")

        self.assertTrue(result)
        self.assertEqual(calls[0], ("RemoveListenChat", "张三", True))
        self.assertIn(("Close", "张三"), calls)
        self.assertNotIn("张三", bot._listen_chats)
        self.assertNotIn("张三", bot._material_source_chats)

    def test_remove_listen_chat_logs_residual_close_result(self):
        calls = []
        logs = []
        windows = []

        class ResidualChat:
            who = "张三"

            def Close(self):
                calls.append("Close")
                windows.clear()

        class FakeWeChat:
            def RemoveListenChat(self, nickname, close_window=True):
                return {"status": "失败", "message": "窗口仍在"}

            def GetAllSubWindow(self):
                return list(windows)

            def GetSubWindow(self, nickname=None):
                return windows[0] if windows else None

        windows.append(ResidualChat())
        bot = SimpleNamespace(_listen_chats={"张三": windows[0]}, wx=FakeWeChat())

        with (
            mock.patch.object(listening, "_bot_sleep", return_value=None),
            mock.patch.object(listening, "_bot_log", side_effect=lambda _bot, *args, **kwargs: logs.append(kwargs.get("message") or (args[0] if args else ""))),
        ):
            result = listening.remove_listen_chat_verified(bot, "张三")

        self.assertTrue(result)
        self.assertEqual(calls, ["Close"])
        self.assertTrue(any("张三 残留监听子窗口直接关闭已执行，正在复查" in item for item in logs))
        self.assertTrue(any("张三 残留监听子窗口已关闭" in item for item in logs))

    def test_remove_listen_chat_logs_residual_close_not_successful(self):
        logs = []
        stale_chat = SimpleNamespace(who="张三")
        bot = SimpleNamespace(
            _listen_chats={"张三": stale_chat},
            wx=SimpleNamespace(
                RemoveListenChat=lambda _nickname, close_window=True: {"status": "失败", "message": "窗口仍在"},
                GetAllSubWindow=lambda: [stale_chat],
                GetSubWindow=lambda nickname=None: stale_chat,
            ),
        )

        with mock.patch.object(listening, "_bot_log", side_effect=lambda _bot, *args, **kwargs: logs.append(kwargs.get("message") or (args[0] if args else ""))):
            result = listening.remove_listen_chat_verified(bot, "张三")

        self.assertFalse(result)
        self.assertTrue(any("张三 残留监听子窗口直接关闭未成功" in item for item in logs))
        self.assertTrue(any("张三 删除监听校验失败，子窗口仍存在" in item for item in logs))

    def test_close_dynamic_listener_subwindows_removes_runtime_entry_after_close(self):
        calls = []
        bot = SimpleNamespace(
            all_Mode_listen_list=[["张三", 1], ["李四", 2]],
        )
        bot._remove_listen_chat_verified = lambda name: calls.append(name) or True

        closed = listening.close_dynamic_listener_subwindows(bot, ["张三", "王五"])

        self.assertEqual(closed, ["张三"])
        self.assertEqual(calls, ["张三"])
        self.assertEqual(bot.all_Mode_listen_list, [["李四", 2]])

    def test_touch_dynamic_listener_entry_updates_existing_timestamp(self):
        bot = SimpleNamespace(all_Mode_listen_list=[["阿英2", 1.0], ["阿英3", 2.0]])

        touched = listening.touch_dynamic_listener_entry(bot, "阿英2", timestamp=9.0)

        self.assertTrue(touched)
        self.assertEqual(bot.all_Mode_listen_list, [["阿英2", 9.0], ["阿英3", 2.0]])

    def test_touch_dynamic_listener_entry_adds_missing_entry(self):
        bot = SimpleNamespace(all_Mode_listen_list=[])

        touched = listening.touch_dynamic_listener_entry(bot, "阿英2", timestamp=9.0)

        self.assertTrue(touched)
        self.assertEqual(bot.all_Mode_listen_list, [["阿英2", 9.0]])


if __name__ == "__main__":
    unittest.main()
