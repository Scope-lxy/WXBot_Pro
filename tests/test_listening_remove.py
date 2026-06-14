import unittest
from types import SimpleNamespace
from unittest import mock

from feature import listening


class RemoveListenChatTests(unittest.TestCase):
    def test_remove_listen_chat_uses_wechat_action_lock(self):
        removed = []
        logs = []
        lock_events = []

        class RecordingLock:
            def __enter__(self):
                lock_events.append("enter")
                return self

            def __exit__(self, *_args):
                lock_events.append("exit")
                return False

        class FakeBot:
            _listen_chats = {"张三": object()}
            wx = SimpleNamespace(
                RemoveListenChat=lambda _nickname: removed.append(_nickname) or {"status": "成功", "message": "ok"},
            )
            lock = RecordingLock()

            def _get_wechat_action_lock(self):
                return self.lock

        with mock.patch.object(listening, "_bot_log", side_effect=lambda _bot, *args, **kwargs: logs.append(kwargs.get("message") or (args[0] if args else ""))):
            result = listening.remove_listen_chat_verified(FakeBot(), "张三")

        self.assertTrue(result)
        self.assertEqual(removed, ["张三"])
        self.assertEqual(lock_events, ["enter", "exit"])
        self.assertEqual(logs.count("监听管理 张三：删除监听完成，已清理运行缓存"), 1)

    def test_remove_listen_chat_logs_wxautox_return_value_and_clears_runtime_cache(self):
        stale_chat = object()
        bot = SimpleNamespace(
            _listen_chats={"张三": stale_chat},
            _material_source_chats={"张三": stale_chat},
            wx=SimpleNamespace(
                RemoveListenChat=lambda _nickname: {"status": "失败", "message": "窗口忙"},
            ),
        )
        logs = []

        with mock.patch.object(listening, "_bot_log", side_effect=lambda _bot, *args, **kwargs: logs.append(kwargs.get("message") or (args[0] if args else ""))):
            result = listening.remove_listen_chat_verified(bot, "张三")

        self.assertTrue(result)
        self.assertTrue(any("监听管理 张三：删除监听结果：窗口忙" in item for item in logs))
        self.assertNotIn("张三", bot._listen_chats)
        self.assertNotIn("张三", bot._material_source_chats)

    def test_remove_listen_chat_does_not_close_residual_window_when_registration_missing(self):
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

        result = listening.remove_listen_chat_verified(bot, "张三")

        self.assertTrue(result)
        self.assertEqual(calls[0], ("RemoveListenChat", "张三", True))
        self.assertNotIn(("Close", "张三"), calls)
        self.assertNotIn("张三", bot._listen_chats)
        self.assertNotIn("张三", bot._material_source_chats)

    def test_remove_listen_chat_does_not_probe_residual_close_result(self):
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

        with mock.patch.object(listening, "_bot_log", side_effect=lambda _bot, *args, **kwargs: logs.append(kwargs.get("message") or (args[0] if args else ""))):
            result = listening.remove_listen_chat_verified(bot, "张三")

        self.assertTrue(result)
        self.assertEqual(calls, [])
        self.assertFalse(any("残留监听子窗口" in item for item in logs))
        self.assertTrue(any("删除监听完成，已清理运行缓存" in item for item in logs))

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
        self.assertFalse(any("残留监听子窗口" in item for item in logs))

    def test_close_dynamic_listener_subwindows_removes_runtime_entry_after_close(self):
        calls = []
        bot = SimpleNamespace(
            all_Mode_listen_list=[["张三", 1], ["李四", 2]],
        )
        bot._remove_listen_chat_verified = lambda name, *, log_success=True: calls.append((name, log_success)) or True

        closed = listening.close_dynamic_listener_subwindows(bot, ["张三", "王五"])

        self.assertEqual(closed, ["张三"])
        self.assertEqual(calls, [("张三", True)])
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

    def test_alllisten_timeout_delete_is_idempotent_when_remove_clears_entry(self):
        removed = []
        logs = []
        bot = SimpleNamespace(
            all_Mode_listen_list=[["张三", 1.0]],
            config=SimpleNamespace(
                AllListen_filter_mute=False,
                global_blacklist=[],
                chat_image_recognition_switch=False,
                chat_voice_recognition_switch=False,
            ),
            wx=SimpleNamespace(
                GetNextNewMessage=lambda **_kwargs: {
                    "chat_name": None,
                    "chat_type": None,
                    "msg": [],
                },
            ),
        )

        def remove_and_clear(name, *, log_success=True):
            removed.append(name)
            listening.remove_dynamic_listener_entries(bot, name)
            return True

        bot._remove_listen_chat_verified = remove_and_clear

        with mock.patch.object(listening.time, "time", return_value=999.0), mock.patch.object(
            listening, "_bot_log", side_effect=lambda _bot, *args, **kwargs: logs.append(kwargs.get("message") or (args[0] if args else ""))
        ):
            new_last_time = listening.alllisten_mode(bot, last_time=1.0, timeout=10)

        self.assertEqual(removed, ["张三"])
        self.assertEqual(bot.all_Mode_listen_list, [])
        self.assertEqual(new_last_time, 999.0)
        self.assertEqual(logs, ["全局监听 张三：对话超时，已停止监听"])

    def test_alllisten_dispatches_first_batch_to_real_subwindow_once(self):
        processed = []
        sub_chat = SimpleNamespace(who="张三", chat_type="private")
        msgs = [
            SimpleNamespace(id="1", type="text", attr="friend", sender="张三", content="第一条"),
            SimpleNamespace(id="2", type="text", attr="friend", sender="张三", content="第二条"),
        ]

        bot = SimpleNamespace(
            all_Mode_listen_list=[],
            config=SimpleNamespace(
                AllListen_filter_mute=False,
                global_blacklist=[],
                chat_image_recognition_switch=False,
                chat_voice_recognition_switch=False,
                memory_switch=False,
                custom_forward_switch=False,
            ),
            wx=SimpleNamespace(
                chat_type="private",
                GetNextNewMessage=lambda **_kwargs: {
                    "chat_name": "张三",
                    "chat_type": "private",
                    "msg": msgs,
                },
            ),
            memory_manager=None,
            add_chat_to_listen=lambda chat: sub_chat,
            is_chat_listened=lambda _chat: False,
            _handle_material_source_message=lambda _chat, _msg: False,
            process_message=lambda chat, msg: processed.append((chat, msg.content)) or True,
        )

        new_last_time = listening.alllisten_mode(bot, last_time=9999999999)

        self.assertEqual(new_last_time, 9999999999)
        self.assertEqual(processed, [(sub_chat, "第一条"), (sub_chat, "第二条")])

    def test_alllisten_reuses_cached_subwindow_when_chat_already_listened(self):
        processed = []
        add_calls = []
        sub_chat = SimpleNamespace(who="张三")
        msg = SimpleNamespace(id="1", type="text", attr="friend", sender="张三", content="你好")
        bot = SimpleNamespace(
            all_Mode_listen_list=[["张三", 1.0]],
            _listen_chats={"张三": sub_chat},
            config=SimpleNamespace(
                AllListen_filter_mute=False,
                global_blacklist=[],
                chat_image_recognition_switch=False,
                chat_voice_recognition_switch=False,
                memory_switch=False,
                custom_forward_switch=False,
            ),
            wx=SimpleNamespace(
                chat_type="private",
                GetNextNewMessage=lambda **_kwargs: {
                    "chat_name": "张三",
                    "chat_type": "private",
                    "msg": [msg],
                },
            ),
            memory_manager=None,
            add_chat_to_listen=lambda chat: add_calls.append(chat),
            is_chat_listened=lambda _chat: True,
            _handle_material_source_message=lambda _chat, _msg: False,
            process_message=lambda chat, message: processed.append((chat, message)),
        )
        logs = []

        with mock.patch.object(listening, "_bot_log", side_effect=lambda _bot, *args, **kwargs: logs.append(kwargs.get("message") or (args[0] if args else ""))):
            listening.alllisten_mode(bot, last_time=9999999999)

        self.assertEqual(add_calls, [])
        self.assertEqual(processed, [(sub_chat, msg)])
        self.assertFalse(any("复用动态监听子窗口处理本批消息" in item for item in logs))

    def test_alllisten_repairs_once_when_listened_subwindow_missing(self):
        processed = []
        add_calls = []
        sub_chat = SimpleNamespace(who="张三")
        msg = SimpleNamespace(id="1", type="text", attr="friend", sender="张三", content="你好")
        bot = SimpleNamespace(
            all_Mode_listen_list=[["张三", 1.0]],
            _listen_chats={},
            config=SimpleNamespace(
                AllListen_filter_mute=False,
                global_blacklist=[],
                chat_image_recognition_switch=False,
                chat_voice_recognition_switch=False,
                memory_switch=False,
                custom_forward_switch=False,
            ),
            wx=SimpleNamespace(
                chat_type="private",
                GetNextNewMessage=lambda **_kwargs: {
                    "chat_name": "张三",
                    "chat_type": "private",
                    "msg": [msg],
                },
            ),
            memory_manager=None,
            add_chat_to_listen=lambda chat: add_calls.append(chat) or sub_chat,
            is_chat_listened=lambda _chat: True,
            _handle_material_source_message=lambda _chat, _msg: False,
            process_message=lambda chat, message: processed.append((chat, message)),
        )
        logs = []

        with mock.patch.object(listening, "_bot_log", side_effect=lambda _bot, *args, **kwargs: logs.append(kwargs.get("message") or (args[0] if args else ""))):
            listening.alllisten_mode(bot, last_time=9999999999)

        self.assertEqual(add_calls, ["张三"])
        self.assertEqual(processed, [(sub_chat, msg)])
        self.assertTrue(any("动态监听子窗口不可用，尝试轻量补一次" in item for item in logs))

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

    def test_dynamic_listener_add_failure_is_warning(self):
        logs = []

        class NoopLock:
            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

        class FakeWeChat:
            def AddListenChat(self, nickname=None, callback=None):
                raise RuntimeError("无效的窗口句柄")

        bot = SimpleNamespace(wx=FakeWeChat(), message_handle_callback=object())
        bot._get_wechat_action_lock = lambda: NoopLock()

        with mock.patch.object(listening, "_bot_log", side_effect=lambda _bot, *args, **kwargs: logs.append((kwargs.get("level", "INFO"), kwargs.get("message") or (args[0] if args else "")))):
            result = listening.add_listen_chat_once(bot, "张三", "动态监听")

        self.assertIsNone(result)
        self.assertTrue(any(level == "WARNING" and "添加动态监听调用异常" in message for level, message in logs))

    def test_manual_listener_add_failure_stays_error(self):
        logs = []

        class NoopLock:
            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

        class FakeWeChat:
            def AddListenChat(self, nickname=None, callback=None):
                raise RuntimeError("无效的窗口句柄")

        bot = SimpleNamespace(wx=FakeWeChat(), message_handle_callback=object())
        bot._get_wechat_action_lock = lambda: NoopLock()

        with mock.patch.object(listening, "_bot_log", side_effect=lambda _bot, *args, **kwargs: logs.append((kwargs.get("level", "INFO"), kwargs.get("message") or (args[0] if args else "")))):
            result = listening.add_listen_chat_once(bot, "张三", "监听")

        self.assertIsNone(result)
        self.assertTrue(any(level == "ERROR" and "添加监听调用异常" in message for level, message in logs))


if __name__ == "__main__":
    unittest.main()
