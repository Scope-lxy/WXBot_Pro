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

    def test_alllisten_timeout_keeps_configured_listeners(self):
        removed = []
        logs = []
        bot = SimpleNamespace(
            all_Mode_listen_list=[["管理员", 1.0], ["阿英2", 1.0], ["张三", 1.0]],
            config=SimpleNamespace(
                cmd="管理员",
                AllListen_switch=False,
                listen_list=["阿英2"],
                group_switch=False,
                group=[],
                custom_forward_switch=False,
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
            listening.alllisten_mode(bot, last_time=1.0, timeout=10)

        self.assertEqual(removed, ["张三"])
        self.assertEqual(bot.all_Mode_listen_list, [["管理员", 1.0], ["阿英2", 1.0]])
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
        self.assertFalse(any("动态监听子窗口不可用，尝试轻量补一次" in item for item in logs))

    def test_alllisten_dynamic_takeover_failure_logs_once(self):
        msg = SimpleNamespace(
            attr="friend",
            type="text",
            sender="张三",
            content="你好",
            time="2026-06-15 05:34:39",
        )

        class FakeChat:
            who = "张三"

            def GetNewMessage(self):
                return [msg]

        logs = []
        bot = SimpleNamespace(
            config=SimpleNamespace(
                AllListen_switch=True,
                listen_list=[],
                group=[],
                group_switch=False,
                custom_forward_switch=False,
                memory_switch=False,
                memory_max_count=100,
                AllListen_filter_mute=False,
                global_blacklist=[],
            ),
            memory_manager=None,
            wx=SimpleNamespace(
                GetAllListenMessage=lambda: {"张三": FakeChat()},
                GetNextNewMessage=lambda **_kwargs: {"chat_name": "张三", "chat_type": "private", "msg": [msg]},
                chat_type="private",
            ),
            all_Mode_listen_list=[["张三", 1.0]],
            _get_listen_chat=lambda _chat: None,
            _is_chat_in_dynamic_listen=lambda _chat: True,
            add_chat_to_listen=lambda _chat: None,
            _forget_runtime_listener_caches=lambda _chat: None,
            _remove_dynamic_listener_entries=lambda _chat: None,
            _handle_material_source_message=lambda _chat, _msg: False,
            process_message=lambda _chat, _message: self.fail("不应处理消息"),
        )

        with mock.patch.object(listening, "_bot_log", side_effect=lambda _bot, *args, **kwargs: logs.append(kwargs.get("message") or (args[0] if args else ""))):
            listening.alllisten_mode(bot, last_time=9999999999)

        matching = [item for item in logs if "临时接管窗口不可用" in item]
        self.assertEqual(matching, ["全局监听 张三：临时接管窗口不可用，已暂存 1 条并延后 30s 重试"])

    def test_global_listen_image_memory_uses_structured_image_save(self):
        msg = SimpleNamespace(
            id="img-1",
            type="image",
            attr="friend",
            sender="张三",
            content="",
            download=lambda: r"C:\tmp\global-image.png",
        )

        def get_next_new_message(**kwargs):
            callback = kwargs.get("callback")
            if callable(callback):
                callback(msg)
            return {"chat_name": "张三", "chat_type": "private", "msg": [msg]}

        image_saves = []
        direct_saves = []
        processed = []
        bot = SimpleNamespace(
            config=SimpleNamespace(
                AllListen_switch=True,
                listen_list=[],
                group=[],
                group_switch=False,
                custom_forward_switch=False,
                memory_switch=True,
                memory_max_count=100,
                AllListen_filter_mute=False,
                global_blacklist=[],
                chat_image_recognition_switch=True,
            ),
            memory_manager=SimpleNamespace(save_message=lambda **kwargs: direct_saves.append(kwargs)),
            wx=SimpleNamespace(
                GetAllListenMessage=lambda: {},
                GetNextNewMessage=get_next_new_message,
                chat_type="private",
            ),
            all_Mode_listen_list=[],
            is_chat_listened=lambda _chat: False,
            add_chat_to_listen=lambda _chat: SimpleNamespace(who="张三"),
            _forget_runtime_listener_caches=lambda _chat: None,
            _remove_dynamic_listener_entries=lambda _chat: None,
            _handle_material_source_message=lambda _chat, _msg: False,
            _save_incoming_image_memory_message=lambda chat, message: image_saves.append((chat.who, chat.chat_type, message.content)) or True,
            process_message=lambda _chat, message: processed.append(message.content),
        )

        listening.alllisten_mode(bot, last_time=9999999999)

        self.assertEqual(image_saves, [("张三", "private", r"C:\tmp\global-image.png")])
        self.assertEqual(direct_saves, [])
        self.assertEqual(processed, [r"C:\tmp\global-image.png"])

    def test_global_listen_text_is_processed_without_early_memory_save(self):
        msg = SimpleNamespace(
            id="txt-1",
            type="text",
            attr="friend",
            sender="张三",
            content="你好",
        )

        def get_next_new_message(**kwargs):
            callback = kwargs.get("callback")
            if callable(callback):
                callback(msg)
            return {"chat_name": "张三", "chat_type": "private", "msg": [msg]}

        direct_saves = []
        processed = []
        bot = SimpleNamespace(
            config=SimpleNamespace(
                AllListen_switch=True,
                listen_list=[],
                group=[],
                group_switch=False,
                custom_forward_switch=False,
                memory_switch=True,
                memory_max_count=100,
                AllListen_filter_mute=False,
                global_blacklist=[],
                chat_image_recognition_switch=False,
            ),
            memory_manager=SimpleNamespace(save_message=lambda **kwargs: direct_saves.append(kwargs)),
            wx=SimpleNamespace(
                GetAllListenMessage=lambda: {},
                GetNextNewMessage=get_next_new_message,
                chat_type="private",
            ),
            all_Mode_listen_list=[],
            is_chat_listened=lambda _chat: False,
            add_chat_to_listen=lambda _chat: SimpleNamespace(who="张三"),
            _forget_runtime_listener_caches=lambda _chat: None,
            _remove_dynamic_listener_entries=lambda _chat: None,
            _handle_material_source_message=lambda _chat, _msg: False,
            process_message=lambda _chat, message: processed.append(message.content),
        )

        listening.alllisten_mode(bot, last_time=9999999999)

        self.assertEqual(direct_saves, [])
        self.assertEqual(processed, ["你好"])

    def test_global_listen_self_text_is_saved_without_ai_processing(self):
        msg = SimpleNamespace(
            id="self-1",
            type="text",
            attr="self",
            sender="self",
            content="我手机上已经回复了",
            time="2026/07/04 10:00:00",
        )

        def get_next_new_message(**kwargs):
            callback = kwargs.get("callback")
            if callable(callback):
                callback(msg)
            return {"chat_name": "张三", "chat_type": "private", "msg": [msg]}

        direct_saves = []
        dirty = []
        manual_self_interrupts = []
        bot = SimpleNamespace(
            config=SimpleNamespace(
                AllListen_switch=True,
                listen_list=[],
                group=[],
                group_switch=False,
                custom_forward_switch=False,
                memory_switch=True,
                memory_max_count=100,
                AllListen_filter_mute=False,
                global_blacklist=[],
                chat_image_recognition_switch=False,
            ),
            memory_manager=SimpleNamespace(save_message=lambda **kwargs: direct_saves.append(kwargs)),
            wx=SimpleNamespace(
                GetAllListenMessage=lambda: {},
                GetNextNewMessage=get_next_new_message,
                chat_type="private",
            ),
            all_Mode_listen_list=[],
            is_chat_listened=lambda _chat: False,
            add_chat_to_listen=lambda _chat: self.fail("self 消息不应触发动态监听接管"),
            _forget_runtime_listener_caches=lambda _chat: None,
            _remove_dynamic_listener_entries=lambda _chat: None,
            _handle_material_source_message=lambda _chat, _msg: False,
            _should_skip_message_memory=lambda _chat, _message: False,
            _consume_private_reply_runtime_echo=lambda _chat, _content: False,
            _interrupt_private_ai_for_manual_self=lambda chat, message: manual_self_interrupts.append((chat.who, message.content)),
            _mark_chat_memory_dirty=lambda chat, _message: dirty.append(chat.who),
            process_message=lambda _chat, _message: self.fail("self 消息不应进入 AI 处理"),
        )

        listening.alllisten_mode(bot, last_time=9999999999)

        self.assertEqual(len(direct_saves), 1)
        self.assertEqual(direct_saves[0]["chat_name"], "张三")
        self.assertEqual(direct_saves[0]["msg_attr"], "self")
        self.assertEqual(direct_saves[0]["content"], "我手机上已经回复了")
        self.assertEqual(direct_saves[0]["message_time"], "2026/07/04 10:00:00")
        self.assertEqual(dirty, ["张三"])
        self.assertEqual(manual_self_interrupts, [("张三", "我手机上已经回复了")])

    def test_add_and_verify_uses_add_listen_returned_chat_directly(self):
        calls = []
        sub_chat = SimpleNamespace(who="张三", SendMsg=lambda _msg: True)

        class NoopLock:
            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

        class FakeWeChat:
            def AddListenChat(self, nickname=None, callback=None):
                calls.append(("AddListenChat", nickname, callback))
                return sub_chat

            def GetSubWindow(self, nickname=None):
                calls.append(("GetSubWindow", nickname))
                return None

        bot = SimpleNamespace(wx=FakeWeChat(), message_handle_callback=object(), _listen_chats={})
        bot._get_wechat_action_lock = lambda: NoopLock()

        result = listening.add_and_verify_subwindow(bot, "张三")

        self.assertIs(result, sub_chat)
        self.assertEqual(
            calls,
            [("GetSubWindow", "张三"), ("AddListenChat", "张三", bot.message_handle_callback)],
        )
        self.assertIs(bot._listen_chats["张三"], sub_chat)

    def test_stale_listen_registration_queues_lightweight_delayed_listen(self):
        logs = []
        msg = SimpleNamespace(id="1", attr="friend", type="text", sender="张三", content="你好")

        class NoopLock:
            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

        class FakeWeChat:
            chat_type = "private"

            def GetNextNewMessage(self, **_kwargs):
                return {"chat_name": "张三", "chat_type": "private", "msg": [msg]}

            def AddListenChat(self, nickname=None, callback=None):
                return {"status": "失败", "message": "已监听"}

            def GetSubWindow(self, nickname=None):
                return None

        bot = SimpleNamespace(
            all_Mode_listen_list=[],
            _listen_chats={},
            _lightweight_delayed_listen_tasks={},
            _lightweight_delayed_listen_last_rebuild_at={},
            config=SimpleNamespace(
                AllListen_filter_mute=False,
                global_blacklist=[],
                chat_image_recognition_switch=False,
                chat_voice_recognition_switch=False,
                memory_switch=False,
                custom_forward_switch=False,
            ),
            wx=FakeWeChat(),
            memory_manager=None,
            message_handle_callback=object(),
            is_chat_listened=lambda _chat: False,
            _handle_material_source_message=lambda _chat, _msg: False,
            process_message=lambda _chat, _message: self.fail("不应立即处理消息"),
        )
        bot._get_wechat_action_lock = lambda: NoopLock()

        with mock.patch.object(listening.time, "time", return_value=100.0), mock.patch.object(listening, "_bot_sleep"), mock.patch.object(
            listening,
            "_bot_log",
            side_effect=lambda _bot, *args, **kwargs: logs.append((kwargs.get("level", "INFO"), kwargs.get("message") or (args[0] if args else ""))),
        ):
            listening.alllisten_mode(bot, last_time=9999999999)

        task = bot._lightweight_delayed_listen_tasks["张三"]
        self.assertEqual(task["due_at"], 130.0)
        self.assertTrue(task["allow_rebuild"])
        self.assertEqual(task["messages"], [msg])
        self.assertTrue(any(level == "INFO" and "临时接管窗口不可用" in message and "已暂存 1 条并延后 30s 重试" in message for level, message in logs))
        self.assertFalse(any(level == "WARNING" and "临时接管窗口不可用" in message and "已暂存 1 条并延后 30s 重试" in message for level, message in logs))

    def test_ordinary_dynamic_add_failure_queues_lightweight_delayed_listen_without_rebuild(self):
        logs = []
        msg = SimpleNamespace(id="1", attr="friend", type="text", sender="张三", content="你好")

        class NoopLock:
            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

        class FakeWeChat:
            chat_type = "private"

            def GetNextNewMessage(self, **_kwargs):
                return {"chat_name": "张三", "chat_type": "private", "msg": [msg]}

            def AddListenChat(self, nickname=None, callback=None):
                return {"status": "失败", "message": "窗口忙"}

            def GetSubWindow(self, nickname=None):
                return None

        bot = SimpleNamespace(
            all_Mode_listen_list=[],
            _listen_chats={},
            _lightweight_delayed_listen_tasks={},
            _lightweight_delayed_listen_last_rebuild_at={},
            config=SimpleNamespace(
                AllListen_filter_mute=False,
                global_blacklist=[],
                chat_image_recognition_switch=False,
                chat_voice_recognition_switch=False,
                memory_switch=False,
                custom_forward_switch=False,
            ),
            wx=FakeWeChat(),
            memory_manager=None,
            message_handle_callback=object(),
            is_chat_listened=lambda _chat: False,
            _handle_material_source_message=lambda _chat, _msg: False,
            process_message=lambda _chat, _message: self.fail("不应立即处理消息"),
        )
        bot._get_wechat_action_lock = lambda: NoopLock()

        with mock.patch.object(listening.time, "time", return_value=100.0), mock.patch.object(listening, "_bot_sleep"), mock.patch.object(
            listening,
            "_bot_log",
            side_effect=lambda _bot, *args, **kwargs: logs.append((kwargs.get("level", "INFO"), kwargs.get("message") or (args[0] if args else ""))),
        ):
            listening.alllisten_mode(bot, last_time=9999999999)

        task = bot._lightweight_delayed_listen_tasks["张三"]
        self.assertEqual(task["due_at"], 130.0)
        self.assertFalse(task["allow_rebuild"])
        self.assertEqual(task["messages"], [msg])
        self.assertTrue(any(level == "INFO" and "临时接管窗口不可用" in message and "已暂存 1 条并延后 30s 重试" in message for level, message in logs))
        self.assertFalse(any(level == "WARNING" and "临时接管窗口不可用" in message and "已暂存 1 条并延后 30s 重试" in message for level, message in logs))

    def test_alllisten_merges_into_existing_delayed_task_without_takeover_retry(self):
        logs = []
        existing_msg = SimpleNamespace(id="1", attr="friend", type="text", sender="张三", content="旧消息")
        new_msg = SimpleNamespace(id="2", attr="friend", type="text", sender="张三", content="新消息")
        bot = SimpleNamespace(
            all_Mode_listen_list=[],
            _listen_chats={},
            _lightweight_delayed_listen_tasks={
                "张三": {
                    "chat": "张三",
                    "messages": [existing_msg],
                    "message_keys": {"id:张三:1"},
                    "created_at": 100.0,
                    "due_at": 130.0,
                    "allow_rebuild": False,
                    "attempt_index": 0,
                    "message_sequence": 0,
                }
            },
            _lightweight_delayed_listen_last_rebuild_at={},
            _lightweight_delayed_listen_flushing=False,
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
                    "msg": [new_msg],
                },
            ),
            memory_manager=None,
            is_chat_listened=lambda _chat: False,
            _handle_material_source_message=lambda _chat, _msg: False,
            add_chat_to_listen=lambda _chat: self.fail("已有延后任务时不应重试接管窗口"),
            process_message=lambda _chat, _message: self.fail("已有延后任务时不应立即处理消息"),
        )
        bot._get_private_message_sequence = lambda _chat: 0

        with mock.patch.object(listening.time, "time", return_value=110.0), mock.patch.object(
            listening,
            "_bot_log",
            side_effect=lambda _bot, *args, **kwargs: logs.append((kwargs.get("level", "INFO"), kwargs.get("message") or (args[0] if args else ""))),
        ):
            listening.alllisten_mode(bot, last_time=9999999999)

        task = bot._lightweight_delayed_listen_tasks["张三"]
        self.assertEqual(task["messages"], [existing_msg, new_msg])
        self.assertEqual(task["due_at"], 130.0)
        self.assertTrue(any(level == "INFO" and "已有延后接管任务，已合并 1 条新消息" in message for level, message in logs))

    def test_flush_lightweight_delayed_listen_prefers_existing_subwindow(self):
        processed = []
        sub_chat = SimpleNamespace(who="张三", SendMsg=lambda _msg: True)
        msg = SimpleNamespace(id="1", attr="friend", type="text", sender="张三", content="你好")
        bot = SimpleNamespace(
            _listen_chats={"张三": sub_chat},
            _lightweight_delayed_listen_tasks={
                "张三": {
                    "chat": "张三",
                    "messages": [msg],
                    "message_keys": {"id:张三:1"},
                    "created_at": 90.0,
                    "due_at": 100.0,
                    "allow_rebuild": True,
                    "message_sequence": 0,
                }
            },
            _lightweight_delayed_listen_last_rebuild_at={},
            _lightweight_delayed_listen_flushing=False,
            process_message=lambda chat, message: processed.append((chat, message)),
        )
        bot._get_private_message_sequence = lambda _chat: 0
        bot._get_wechat_action_lock = lambda: self.fail("已有子窗口时不应触发微信操作锁")

        with mock.patch.object(listening.time, "time", return_value=101.0):
            flushed = listening.flush_lightweight_delayed_listen_tasks(bot)

        self.assertTrue(flushed)
        self.assertEqual(processed, [(sub_chat, msg)])
        self.assertEqual(bot._lightweight_delayed_listen_tasks, {})

    def test_flush_lightweight_delayed_listen_rebuilds_once_when_due(self):
        calls = []
        processed = []
        rebuilt_chat = SimpleNamespace(who="张三", SendMsg=lambda _msg: True)
        msg = SimpleNamespace(id="1", attr="friend", type="text", sender="张三", content="你好")

        class TryLock:
            def __init__(self):
                self.locked = False

            def acquire(self, blocking=True):
                calls.append(("lock", blocking))
                self.locked = True
                return True

            def release(self):
                calls.append(("unlock",))
                self.locked = False

        class FakeWeChat:
            def GetSubWindow(self, nickname=None):
                calls.append(("GetSubWindow", nickname))
                return None

            def RemoveListenChat(self, nickname, close_window=True):
                calls.append(("RemoveListenChat", nickname, close_window))
                return {"status": "成功"}

            def AddListenChat(self, nickname=None, callback=None):
                calls.append(("AddListenChat", nickname, callback))
                return rebuilt_chat

        lock = TryLock()
        bot = SimpleNamespace(
            wx=FakeWeChat(),
            message_handle_callback=object(),
            all_Mode_listen_list=[],
            _listen_chats={},
            _lightweight_delayed_listen_tasks={
                "张三": {
                    "chat": "张三",
                    "messages": [msg],
                    "message_keys": {"id:张三:1"},
                    "created_at": 90.0,
                    "due_at": 100.0,
                    "allow_rebuild": True,
                    "message_sequence": 0,
                }
            },
            _lightweight_delayed_listen_last_rebuild_at={},
            _lightweight_delayed_listen_flushing=False,
            process_message=lambda chat, message: processed.append((chat, message)),
        )
        bot._get_private_message_sequence = lambda _chat: 0
        bot._get_wechat_action_lock = lambda: lock

        with mock.patch.object(listening.time, "time", return_value=101.0):
            flushed = listening.flush_lightweight_delayed_listen_tasks(bot)

        self.assertTrue(flushed)
        self.assertEqual(processed, [(rebuilt_chat, msg)])
        self.assertEqual(
            [call for call in calls if call[0] in {"RemoveListenChat", "AddListenChat"}],
            [("RemoveListenChat", "张三", True), ("AddListenChat", "张三", bot.message_handle_callback)],
        )

    def test_lightweight_delayed_listen_rebuild_cooldown_skips_remove_add(self):
        calls = []
        msg = SimpleNamespace(id="1", attr="friend", type="text", sender="张三", content="你好")

        class TryLock:
            def acquire(self, blocking=True):
                return True

            def release(self):
                pass

        class FakeWeChat:
            def GetSubWindow(self, nickname=None):
                calls.append(("GetSubWindow", nickname))
                return None

            def RemoveListenChat(self, nickname, close_window=True):
                calls.append(("RemoveListenChat", nickname, close_window))
                return {"status": "成功"}

            def AddListenChat(self, nickname=None, callback=None):
                calls.append(("AddListenChat", nickname))
                return None

        bot = SimpleNamespace(
            wx=FakeWeChat(),
            _listen_chats={},
            _lightweight_delayed_listen_tasks={
                "张三": {
                    "chat": "张三",
                    "messages": [msg],
                    "message_keys": {"id:张三:1"},
                    "created_at": 90.0,
                    "due_at": 100.0,
                    "message_sequence": 0,
                }
            },
            _lightweight_delayed_listen_last_rebuild_at={"张三": 80.0},
            _lightweight_delayed_listen_flushing=False,
            process_message=lambda _chat, _message: self.fail("冷却期不应处理消息"),
        )
        bot._get_private_message_sequence = lambda _chat: 0
        bot._get_wechat_action_lock = lambda: TryLock()

        with mock.patch.object(listening.time, "time", return_value=101.0):
            listening.flush_lightweight_delayed_listen_tasks(bot)

        self.assertNotIn(("RemoveListenChat", "张三", True), calls)
        self.assertFalse(any(call[0] == "AddListenChat" for call in calls))
        task = bot._lightweight_delayed_listen_tasks["张三"]
        self.assertEqual(task["attempt_index"], 1)
        self.assertEqual(task["due_at"], 150.0)

    def test_ordinary_lightweight_delayed_listen_adds_without_remove_and_reschedules(self):
        calls = []
        logs = []
        msg = SimpleNamespace(id="1", attr="friend", type="text", sender="张三", content="你好")

        class TryLock:
            def acquire(self, blocking=True):
                return True

            def release(self):
                pass

        class FakeWeChat:
            def GetSubWindow(self, nickname=None):
                calls.append(("GetSubWindow", nickname))
                return None

            def RemoveListenChat(self, nickname, close_window=True):
                calls.append(("RemoveListenChat", nickname, close_window))
                return {"status": "成功"}

            def AddListenChat(self, nickname=None, callback=None):
                calls.append(("AddListenChat", nickname, callback))
                return None

        bot = SimpleNamespace(
            wx=FakeWeChat(),
            message_handle_callback=object(),
            all_Mode_listen_list=[],
            _listen_chats={},
            _lightweight_delayed_listen_tasks={
                "张三": {
                    "chat": "张三",
                    "messages": [msg],
                    "message_keys": {"id:张三:1"},
                    "created_at": 100.0,
                    "due_at": 130.0,
                    "allow_rebuild": False,
                    "message_sequence": 0,
                }
            },
            _lightweight_delayed_listen_last_rebuild_at={},
            _lightweight_delayed_listen_flushing=False,
            process_message=lambda _chat, _message: self.fail("未恢复子窗口时不应处理消息"),
        )
        bot._get_private_message_sequence = lambda _chat: 0
        bot._get_wechat_action_lock = lambda: TryLock()
        bot._add_and_verify_subwindow = lambda _chat: calls.append(("AddListenChat", _chat, bot.message_handle_callback)) or None

        with mock.patch.object(listening.time, "time", return_value=131.0), mock.patch.object(listening, "_bot_sleep"), mock.patch.object(
            listening,
            "_bot_log",
            side_effect=lambda _bot, *args, **kwargs: logs.append((kwargs.get("level", "INFO"), kwargs.get("message") or (args[0] if args else ""))),
        ):
            flushed = listening.flush_lightweight_delayed_listen_tasks(bot)

        self.assertTrue(flushed)
        self.assertNotIn(("RemoveListenChat", "张三", True), calls)
        self.assertIn(("AddListenChat", "张三", bot.message_handle_callback), calls)
        task = bot._lightweight_delayed_listen_tasks["张三"]
        self.assertEqual(task["attempt_index"], 1)
        self.assertEqual(task["due_at"], 160.0)
        self.assertTrue(any(level == "INFO" and "轻量延后监听第 1 次未恢复" in message for level, message in logs))
        self.assertFalse(any(level == "WARNING" and "轻量延后监听第 1 次未恢复" in message for level, message in logs))

    def test_lightweight_delayed_listen_second_failure_keeps_waiting(self):
        calls = []
        logs = []
        msg = SimpleNamespace(id="1", attr="friend", type="text", sender="张三", content="你好")

        class TryLock:
            def acquire(self, blocking=True):
                return True

            def release(self):
                pass

        bot = SimpleNamespace(
            message_handle_callback=object(),
            all_Mode_listen_list=[],
            _listen_chats={},
            _lightweight_delayed_listen_tasks={
                "张三": {
                    "chat": "张三",
                    "messages": [msg],
                    "message_keys": {"id:张三:1"},
                    "created_at": 100.0,
                    "due_at": 160.0,
                    "attempt_index": 1,
                    "allow_rebuild": False,
                    "message_sequence": 0,
                }
            },
            _lightweight_delayed_listen_last_rebuild_at={},
            _lightweight_delayed_listen_flushing=False,
            process_message=lambda _chat, _message: self.fail("未恢复子窗口时不应处理消息"),
        )
        bot._get_private_message_sequence = lambda _chat: 0
        bot._get_wechat_action_lock = lambda: TryLock()
        bot._add_and_verify_subwindow = lambda _chat: calls.append(("AddListenChat", _chat, bot.message_handle_callback)) or None

        with mock.patch.object(listening.time, "time", return_value=161.0), mock.patch.object(listening, "_bot_sleep"), mock.patch.object(
            listening,
            "_bot_log",
            side_effect=lambda _bot, *args, **kwargs: logs.append((kwargs.get("level", "INFO"), kwargs.get("message") or (args[0] if args else ""))),
        ):
            flushed = listening.flush_lightweight_delayed_listen_tasks(bot)

        self.assertTrue(flushed)
        task = bot._lightweight_delayed_listen_tasks["张三"]
        self.assertEqual(task["attempt_index"], 2)
        self.assertEqual(task["due_at"], 221.0)
        self.assertIn(("AddListenChat", "张三", bot.message_handle_callback), calls)
        self.assertTrue(any(level == "INFO" and "第 2 次未恢复" in message for level, message in logs))
        self.assertFalse(any(level == "WARNING" and "两次恢复失败" in message for level, message in logs))

    def test_lightweight_delayed_listen_expiry_saves_text_fallback(self):
        calls = []
        saves = []
        msg = SimpleNamespace(id="1", attr="friend", type="text", sender="张三", content="你好", time="2026/07/04 10:00:00")
        pending_voice = SimpleNamespace(id="2", attr="friend", type="voice", sender="张三", content='语音8"秒')

        class TryLock:
            def acquire(self, blocking=True):
                return True

            def release(self):
                pass

        bot = SimpleNamespace(
            config=SimpleNamespace(memory_switch=True, memory_max_count=100),
            memory_manager=SimpleNamespace(save_message=lambda **kwargs: saves.append(kwargs)),
            message_handle_callback=object(),
            all_Mode_listen_list=[],
            _listen_chats={},
            _lightweight_delayed_listen_tasks={
                "张三": {
                    "chat": "张三",
                    "messages": [msg, pending_voice],
                    "message_keys": {"id:张三:1", "id:张三:2"},
                    "created_at": 100.0,
                    "due_at": 700.0,
                    "attempt_index": 8,
                    "allow_rebuild": False,
                    "message_sequence": 0,
                }
            },
            _lightweight_delayed_listen_last_rebuild_at={},
            _lightweight_delayed_listen_flushing=False,
            process_message=lambda _chat, _message: self.fail("未恢复子窗口时不应处理消息"),
        )
        bot._get_private_message_sequence = lambda _chat: 0
        bot._get_wechat_action_lock = lambda: TryLock()
        bot._add_and_verify_subwindow = lambda _chat: calls.append(("AddListenChat", _chat, bot.message_handle_callback)) or None

        with mock.patch.object(listening.time, "time", return_value=700.0), mock.patch.object(listening, "_bot_sleep"):
            flushed = listening.flush_lightweight_delayed_listen_tasks(bot)

        self.assertTrue(flushed)
        self.assertEqual(len(saves), 1)
        self.assertEqual(saves[0]["chat_name"], "张三")
        self.assertEqual(saves[0]["content"], "你好")
        self.assertEqual(saves[0]["message_time"], "2026/07/04 10:00:00")

    def test_lightweight_delayed_listen_keeps_task_when_lock_busy(self):
        releases = []
        msg = SimpleNamespace(id="1", attr="friend", type="text", sender="张三", content="你好")

        class BusyLock:
            def acquire(self, blocking=True):
                return False

            def release(self):
                releases.append("release")

        bot = SimpleNamespace(
            _listen_chats={},
            _lightweight_delayed_listen_tasks={
                "张三": {
                    "chat": "张三",
                    "messages": [msg],
                    "message_keys": {"id:张三:1"},
                    "created_at": 90.0,
                    "due_at": 100.0,
                    "message_sequence": 0,
                }
            },
            _lightweight_delayed_listen_last_rebuild_at={},
            _lightweight_delayed_listen_flushing=False,
            process_message=lambda _chat, _message: self.fail("锁忙时不应处理消息"),
        )
        bot._get_private_message_sequence = lambda _chat: 0
        bot._get_wechat_action_lock = lambda: BusyLock()

        with mock.patch.object(listening.time, "time", return_value=101.0):
            flushed = listening.flush_lightweight_delayed_listen_tasks(bot)

        self.assertFalse(flushed)
        self.assertIn("张三", bot._lightweight_delayed_listen_tasks)
        self.assertEqual(releases, [])

    def test_lightweight_delayed_listen_drops_expired_task(self):
        processed = []
        msg = SimpleNamespace(id="1", attr="friend", type="text", sender="张三", content="你好")
        bot = SimpleNamespace(
            _listen_chats={},
            _lightweight_delayed_listen_tasks={
                "张三": {
                    "chat": "张三",
                    "messages": [msg],
                    "message_keys": {"id:张三:1"},
                    "created_at": -500.0,
                    "due_at": 20.0,
                    "allow_rebuild": False,
                    "message_sequence": 0,
                }
            },
            _lightweight_delayed_listen_last_rebuild_at={},
            _lightweight_delayed_listen_flushing=False,
            process_message=lambda chat, message: processed.append((chat, message)),
        )
        bot._get_private_message_sequence = lambda _chat: 0
        bot._get_wechat_action_lock = lambda: self.fail("过期任务不应触发微信 UI")

        with mock.patch.object(listening.time, "time", return_value=101.0):
            flushed = listening.flush_lightweight_delayed_listen_tasks(bot)

        self.assertTrue(flushed)
        self.assertEqual(processed, [])
        self.assertEqual(bot._lightweight_delayed_listen_tasks, {})

    def test_lightweight_delayed_listen_drops_when_message_sequence_changed(self):
        processed = []
        logs = []
        msg = SimpleNamespace(id="1", attr="friend", type="text", sender="张三", content="你好")
        bot = SimpleNamespace(
            _listen_chats={},
            _lightweight_delayed_listen_tasks={
                "张三": {
                    "chat": "张三",
                    "messages": [msg],
                    "message_keys": {"id:张三:1"},
                    "created_at": 90.0,
                    "due_at": 100.0,
                    "message_sequence": 1,
                }
            },
            _lightweight_delayed_listen_last_rebuild_at={},
            _lightweight_delayed_listen_flushing=False,
            process_message=lambda chat, message: processed.append((chat, message)),
        )
        bot._get_private_message_sequence = lambda _chat: 2
        bot._get_wechat_action_lock = lambda: self.fail("消息序号变化丢弃时不应触发微信 UI")

        with mock.patch.object(listening.time, "time", return_value=101.0), mock.patch.object(
            listening,
            "_bot_log",
            side_effect=lambda _bot, *args, **kwargs: logs.append((kwargs.get("level", "INFO"), kwargs.get("message") or (args[0] if args else ""))),
        ):
            flushed = listening.flush_lightweight_delayed_listen_tasks(bot)

        self.assertTrue(flushed)
        self.assertEqual(processed, [])
        self.assertEqual(bot._lightweight_delayed_listen_tasks, {})
        self.assertTrue(any(level == "INFO" and "轻量延后监听期间已有新消息处理" in message for level, message in logs))
        self.assertFalse(any(level == "WARNING" and "轻量延后监听期间已有新消息处理" in message for level, message in logs))

    def test_listener_reconcile_allows_future_lightweight_delayed_listen_pending(self):
        calls = []

        class FreeLock:
            def acquire(self, blocking=True):
                calls.append(("lock", blocking))
                return True

            def release(self):
                calls.append(("unlock",))

        bot = SimpleNamespace(
            wx=object(),
            config=SimpleNamespace(AllListen_switch=True),
            _lightweight_delayed_listen_tasks={"张三": {"chat": "张三", "due_at": 200.0}},
            _lightweight_delayed_listen_last_rebuild_at={},
            _lightweight_delayed_listen_flushing=False,
            _listener_reconcile_interval_seconds=30,
            _listener_reconcile_last_at=0.0,
        )
        bot._get_wechat_action_lock = lambda: FreeLock()

        with mock.patch.object(listening.time, "time", return_value=100.0), mock.patch.object(
            listening,
            "reconcile_listener_subwindows",
            return_value=["管理员"],
        ):
            reopened = listening.maybe_reconcile_listener_subwindows(bot)

        self.assertEqual(reopened, ["管理员"])
        self.assertEqual(calls, [("lock", False), ("unlock",)])
        self.assertEqual(bot._listener_reconcile_last_at, 100.0)

    def test_listener_reconcile_skips_when_lightweight_delayed_listen_due(self):
        bot = SimpleNamespace(
            wx=object(),
            config=SimpleNamespace(AllListen_switch=True),
            _lightweight_delayed_listen_tasks={"张三": {"chat": "张三", "due_at": 90.0}},
            _lightweight_delayed_listen_last_rebuild_at={},
            _lightweight_delayed_listen_flushing=False,
            _listener_reconcile_interval_seconds=30,
            _listener_reconcile_last_at=0.0,
        )
        bot._get_wechat_action_lock = lambda: self.fail("延后补窗到期时不应触发固定监听巡检")

        with mock.patch.object(listening.time, "time", return_value=100.0):
            reopened = listening.maybe_reconcile_listener_subwindows(bot)

        self.assertEqual(reopened, [])

    def test_listener_reconcile_ignores_stale_delayed_tasks_outside_alllisten_mode(self):
        calls = []

        class FreeLock:
            def acquire(self, blocking=True):
                calls.append(("lock", blocking))
                return True

            def release(self):
                calls.append(("unlock",))

        bot = SimpleNamespace(
            wx=object(),
            config=SimpleNamespace(AllListen_switch=False),
            _lightweight_delayed_listen_tasks={"张三": {"chat": "张三"}},
            _lightweight_delayed_listen_last_rebuild_at={},
            _lightweight_delayed_listen_flushing=False,
            _listener_reconcile_interval_seconds=30,
            _listener_reconcile_last_at=0.0,
        )
        bot._get_wechat_action_lock = lambda: FreeLock()

        with mock.patch.object(listening, "reconcile_listener_subwindows", return_value=["管理员"]):
            reopened = listening.maybe_reconcile_listener_subwindows(bot)

        self.assertEqual(reopened, ["管理员"])
        self.assertEqual(calls, [("lock", False), ("unlock",)])

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
        self.assertFalse(any("监听管理 张三：添加动态监听调用成功" in item for item in logs))
        self.assertFalse(any("监听管理 张三：添加动态监听失败" in item for item in logs))

    def test_dynamic_listener_add_failure_is_silent_until_global_retry_log(self):
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
        self.assertEqual(logs, [])

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
