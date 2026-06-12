import unittest
from types import SimpleNamespace
from unittest import mock

from feature import listening


class RemoveListenChatTests(unittest.TestCase):
    def test_remove_listen_chat_does_not_require_outer_wechat_action_lock(self):
        removed = []
        logs = []

        class FakeBot:
            _listen_chats = {"张三": object()}
            wx = SimpleNamespace(
                RemoveListenChat=lambda _nickname: removed.append(_nickname) or {"status": "成功", "message": "ok"},
                GetAllSubWindow=lambda: [],
            )

            def _get_wechat_action_lock(self):
                raise AssertionError("RemoveListenChat should not take the outer bot UI lock")

        with mock.patch.object(listening, "_bot_log", side_effect=lambda _bot, *args, **kwargs: logs.append(kwargs.get("message") or (args[0] if args else ""))):
            result = listening.remove_listen_chat_verified(FakeBot(), "张三")

        self.assertTrue(result)
        self.assertEqual(removed, ["张三"])
        self.assertEqual(logs.count("监听管理 张三：删除监听完成"), 1)

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

        self.assertTrue(result)
        self.assertTrue(any("监听管理 张三：删除监听结果：窗口忙" in item for item in logs))

    def test_remove_listen_chat_clears_cache_when_registration_missing(self):
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
        self.assertNotIn(("Close", "张三"), calls)
        self.assertNotIn("张三", bot._listen_chats)
        self.assertNotIn("张三", bot._material_source_chats)

    def test_remove_listen_chat_does_not_close_residual_window(self):
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
        self.assertEqual(calls, [])
        self.assertTrue(any("暂不强行关闭窗口" in item for item in logs))

    def test_remove_listen_chat_clears_runtime_when_residual_still_exists(self):
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

        self.assertTrue(result)
        self.assertNotIn("张三", bot._listen_chats)
        self.assertTrue(any("暂不强行关闭窗口" in item for item in logs))

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

    def test_cached_listener_subwindow_matches_requires_same_who(self):
        bot = SimpleNamespace(_listen_chats={"张三": SimpleNamespace(who="张三")})

        self.assertTrue(listening.cached_listener_subwindow_matches(bot, "张三"))
        self.assertFalse(listening.cached_listener_subwindow_matches(bot, "李四"))

    def test_schedule_global_listener_fallback_processes_message_later(self):
        processed = []
        bot = SimpleNamespace(
            run_flag=True,
            process_message=lambda chat, msg: processed.append((chat.who, msg.content)) or True,
            _maybe_update_conversation_memory=lambda _chat, _msg: None,
        )
        msg = SimpleNamespace(content="你好")

        with mock.patch.object(listening.threading, "Timer") as fake_timer:
            fake_timer.return_value = SimpleNamespace(daemon=False, start=lambda: None)
            scheduled = listening.schedule_global_listener_fallback(bot, "张三", msg)
            callback = fake_timer.call_args.args[1]

        self.assertTrue(scheduled)
        callback()
        self.assertEqual(processed, [("张三", "你好")])

    def test_add_listen_chat_once_returns_wechat_result(self):
        calls = []
        logs = []

        class NoopLock:
            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

        class FakeWeChat:
            def AddListenChat(self, nickname=None, callback=None):
                calls.append((nickname, callback))
                return {"status": "success"}

        bot = SimpleNamespace(wx=FakeWeChat(), message_handle_callback=object())
        bot._get_wechat_action_lock = lambda: NoopLock()

        with mock.patch.object(listening, "_bot_log", side_effect=lambda _bot, *args, **kwargs: logs.append(kwargs.get("message") or (args[0] if args else ""))):
            result = listening.add_listen_chat_once(bot, "张三", "动态监听")

        self.assertEqual(result, {"status": "success"})
        self.assertEqual(calls, [("张三", bot.message_handle_callback)])
        self.assertTrue(any("监听管理 张三：添加动态监听调用成功" in item for item in logs))


if __name__ == "__main__":
    unittest.main()
