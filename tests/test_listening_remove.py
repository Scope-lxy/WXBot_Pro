import unittest
from types import SimpleNamespace
from unittest import mock

from core.message_pipeline import ConversationRef
from feature import listening
from wxbot_core import WXBot


class RemoveListenChatTests(unittest.TestCase):
    def test_listener_init_queues_recovery_before_registering_live_callbacks(self):
        order = []
        config = SimpleNamespace(
            DATA_DIR="data",
            AtMe="",
            bind_account_wx_id=lambda _wx_id: order.append("bind"),
        )
        bot = SimpleNamespace(
            config=config,
            _bootstrap_ui_owner=lambda _listeners: {
                "nickname": "机器人",
                "wx_id": "wxid-test",
            },
            _voice_reply_state_path=lambda: "voice-state.json",
            _set_material_outreach_namespace=lambda _wx_id: order.append("namespace"),
            _initialize_message_runtime=lambda _wx_id: order.append("runtime"),
            _init_prompt_system=lambda _path: order.append("prompt"),
            _drain_message_recovery=lambda: order.append("recovery"),
            _register_ui_listener_names=lambda _listeners: order.append("register"),
            _register_runtime_task_schedules=lambda: order.append("schedules"),
            _ui_ingress_ready=SimpleNamespace(set=lambda: order.append("ready")),
            _ui_owner=object(),
            _listen_chats={},
        )
        specs = [("群聊", ConversationRef("测试群", "group"))]

        with mock.patch.object(listening, "listener_registration_specs", return_value=specs), mock.patch.object(
            listening, "migrate_default_account", return_value=False
        ), mock.patch.object(
            listening, "load_voice_reply_state", return_value={}
        ), mock.patch.object(
            listening, "account_area_dir", return_value="memory"
        ), mock.patch.object(listening, "_bot_log"):
            self.assertTrue(listening.init_wx_listeners(bot))

        self.assertLess(order.index("recovery"), order.index("register"))
        self.assertLess(order.index("register"), order.index("ready"))

    def test_listener_specs_keep_same_named_private_and_group_distinct(self):
        bot = SimpleNamespace(
            config=SimpleNamespace(
                AllListen_switch=False,
                listen_list=["同名会话"],
                group_switch=True,
                group=["同名会话"],
            ),
            all_Mode_listen_list=[],
            _material_source_runtime_enabled=lambda: False,
        )

        specs = listening.listener_registration_specs(bot)

        self.assertEqual(
            [(label, ref.chat_type, ref.who) for label, ref in specs],
            [
                ("用户", "private", "同名会话"),
                ("群组", "group", "同名会话"),
            ],
        )

    def test_ui_listener_registration_payload_always_includes_chat_type(self):
        bot = WXBot.__new__(WXBot)
        intents = []
        bot._ui_owner = SimpleNamespace(
            call=lambda intent, _timeout: intents.append(intent) or True,
        )

        bot._register_ui_listener_names([
            ConversationRef("同名会话", "private"),
            ConversationRef("同名会话", "group"),
        ])

        self.assertEqual(
            [intent.payload for intent in intents],
            [
                {"conversation": "同名会话", "chat_type": "private"},
                {"conversation": "同名会话", "chat_type": "group"},
            ],
        )

    def test_global_message_enters_ingress_before_window_repair(self):
        received = []
        message = SimpleNamespace(
            id="m1",
            type="text",
            attr="friend",
            sender="张三",
            content="你好",
        )
        bot = SimpleNamespace(
            all_Mode_listen_list=[],
            _listen_chats={},
            config=SimpleNamespace(
                AllListen_filter_mute=False,
                global_blacklist=[],
                chat_image_recognition_switch=False,
                chat_voice_recognition_switch=False,
            ),
            wx=SimpleNamespace(GetNextNewMessage=lambda **_kwargs: {
                "chat_name": "张三",
                "chat_type": "private",
                "msg": [message],
            }),
            _enqueue_ui_message=lambda conversation, envelope: received.append(
                (conversation.who, envelope.content)
            ) or True,
            is_chat_listened=lambda _name, **_kwargs: False,
            add_chat_to_listen=lambda _name, **_kwargs: None,
        )

        with mock.patch.object(listening.time, "time", return_value=100.0):
            listening.alllisten_mode(bot, last_time=9999999999)

        self.assertEqual(received, [("张三", "你好")])
        state = bot._listener_window_supervisor.snapshot()[0]
        self.assertEqual(state["conversation"], "张三")
        self.assertEqual(state["next_retry_at"], 130.0)
        self.assertNotIn("messages", state)

    def test_global_group_media_uses_group_recognition_switches(self):
        received = []
        poll_kwargs = []
        message = SimpleNamespace(
            id="group-voice",
            type="voice",
            attr="friend",
            sender="群友A",
            content='语音3"秒',
        )

        def get_next_message(**kwargs):
            poll_kwargs.append(kwargs)
            return {
                "chat_name": "测试群",
                "chat_type": "group",
                "msg": [message],
            }

        bot = SimpleNamespace(
            all_Mode_listen_list=[],
            _listen_chats={},
            config=SimpleNamespace(
                AllListen_filter_mute=False,
                global_blacklist=["测试群"],
                chat_image_recognition_switch=False,
                group_image_recognition_switch=True,
                chat_voice_recognition_switch=False,
                group_voice_recognition_switch=True,
            ),
            wx=SimpleNamespace(GetNextNewMessage=get_next_message),
            _enqueue_ui_message=lambda conversation, envelope: received.append(
                (conversation, envelope)
            ) or True,
        )

        with mock.patch.object(listening.time, "time", return_value=100.0):
            listening.alllisten_mode(bot, last_time=9999999999)

        self.assertTrue(poll_kwargs[0]["download_media"])
        self.assertEqual(received[0][0], ConversationRef("测试群", "group"))
        self.assertFalse(getattr(received[0][1], "_skip_ai_reply", False))

    def test_process_listen_message_prepares_media_before_routing(self):
        calls = []
        message = SimpleNamespace(type="voice", attr="friend", sender="张三", content='语音8"秒')
        chat = SimpleNamespace(who="张三", chat_type="private")
        bot = SimpleNamespace(process_message=lambda _chat, _message: calls.append("process") or True)

        with mock.patch.object(listening, "prepare_message_media", side_effect=lambda _bot, _msg, _chat: calls.append("prepare")):
            result = listening.process_listen_message(bot, chat, message)

        self.assertTrue(result)
        self.assertEqual(calls, ["prepare", "process"])

    def test_remove_listen_chat_clears_runtime_cache(self):
        removed = []
        logs = []

        class FakeBot:
            _listen_chats = {"张三": object()}
            wx = SimpleNamespace(
                RemoveListenChat=lambda nickname=None, chat_type=None: removed.append((nickname, chat_type)) or {"status": "成功", "message": "ok"},
            )

        with mock.patch.object(listening, "_bot_log", side_effect=lambda _bot, *args, **kwargs: logs.append(kwargs.get("message") or (args[0] if args else ""))):
            result = listening.remove_listen_chat_verified(FakeBot(), "张三")

        self.assertTrue(result)
        self.assertEqual(removed, [("张三", "private")])
        self.assertEqual(logs.count("监听管理 张三：删除监听完成，已清理运行缓存"), 1)

    def test_remove_listen_chat_failure_keeps_runtime_state_for_retry(self):
        stale_chat = SimpleNamespace(who="张三", chat_type="private")
        bot = SimpleNamespace(
            _listen_chats={("private", "张三"): stale_chat},
            wx=SimpleNamespace(
                RemoveListenChat=lambda nickname=None, chat_type=None: {"status": "失败", "message": "窗口忙"},
            ),
        )
        logs = []

        with mock.patch.object(listening, "_bot_log", side_effect=lambda _bot, *args, **kwargs: logs.append(kwargs.get("message") or (args[0] if args else ""))):
            result = listening.remove_listen_chat_verified(bot, "张三")

        self.assertFalse(result)
        self.assertTrue(any("删除监听失败，已保留运行状态等待重试" in item for item in logs))
        self.assertIs(bot._listen_chats[("private", "张三")], stale_chat)

    def test_remove_listen_chat_does_not_close_residual_window_when_registration_missing(self):
        calls = []
        windows = []

        class ResidualChat:
            who = "张三"

            def Close(self):
                calls.append(("Close", self.who))
                windows.clear()

        class FakeWeChat:
            def RemoveListenChat(self, nickname=None, chat_type=None):
                calls.append(("RemoveListenChat", nickname, chat_type))
                return {"status": "失败", "message": "未找到监听对象"}

            def GetAllSubWindow(self):
                return list(windows)

            def GetSubWindow(self, nickname=None, chat_type=None):
                calls.append(("GetSubWindow", nickname, chat_type))
                return windows[0] if windows else None

        windows.append(ResidualChat())
        bot = SimpleNamespace(
            _listen_chats={"张三": windows[0]},
            wx=FakeWeChat(),
        )

        result = listening.remove_listen_chat_verified(bot, "张三")

        self.assertTrue(result)
        self.assertEqual(calls[0], ("RemoveListenChat", "张三", "private"))
        self.assertNotIn(("Close", "张三"), calls)
        self.assertNotIn("张三", bot._listen_chats)

    def test_remove_listen_chat_does_not_probe_or_clear_when_window_remains(self):
        calls = []
        logs = []
        windows = []

        class ResidualChat:
            who = "张三"

            def Close(self):
                calls.append("Close")
                windows.clear()

        class FakeWeChat:
            def RemoveListenChat(self, nickname=None, chat_type=None):
                return {"status": "失败", "message": "窗口仍在"}

            def GetAllSubWindow(self):
                return list(windows)

            def GetSubWindow(self, nickname=None, chat_type=None):
                return windows[0] if windows else None

        windows.append(ResidualChat())
        bot = SimpleNamespace(
            _listen_chats={("private", "张三"): windows[0]},
            wx=FakeWeChat(),
        )

        with mock.patch.object(listening, "_bot_log", side_effect=lambda _bot, *args, **kwargs: logs.append(kwargs.get("message") or (args[0] if args else ""))):
            result = listening.remove_listen_chat_verified(bot, "张三")

        self.assertFalse(result)
        self.assertEqual(calls, [])
        self.assertFalse(any("残留监听子窗口" in item for item in logs))
        self.assertTrue(any("删除监听失败，已保留运行状态等待重试" in item for item in logs))
        self.assertIn(("private", "张三"), bot._listen_chats)

    def test_remove_listen_chat_keeps_runtime_when_residual_still_exists(self):
        logs = []
        stale_chat = SimpleNamespace(who="张三", chat_type="private")
        bot = SimpleNamespace(
            _listen_chats={("private", "张三"): stale_chat},
            wx=SimpleNamespace(
                RemoveListenChat=lambda nickname=None, chat_type=None: {"status": "失败", "message": "窗口仍在"},
                GetAllSubWindow=lambda: [stale_chat],
                GetSubWindow=lambda nickname=None, chat_type=None: stale_chat,
            ),
        )

        with mock.patch.object(listening, "_bot_log", side_effect=lambda _bot, *args, **kwargs: logs.append(kwargs.get("message") or (args[0] if args else ""))):
            result = listening.remove_listen_chat_verified(bot, "张三")

        self.assertFalse(result)
        self.assertIs(bot._listen_chats[("private", "张三")], stale_chat)
        self.assertFalse(any("残留监听子窗口" in item for item in logs))

    def test_ambiguous_same_name_remove_failure_preserves_each_listener(self):
        private_chat = SimpleNamespace(who="同名会话", chat_type="private")
        group_chat = SimpleNamespace(who="同名会话", chat_type="group")
        bot = SimpleNamespace(
            _listen_chats={
                ("private", "同名会话"): private_chat,
                ("group", "同名会话"): group_chat,
            },
            all_Mode_listen_list=[["同名会话", 1.0, "group"]],
            wx=SimpleNamespace(
                RemoveListenChat=lambda **_kwargs: (_ for _ in ()).throw(
                    RuntimeError("存在多个同名微信窗口，无法安全删除监听")
                ),
            ),
        )

        result = listening.remove_listen_chat_verified(
            bot,
            "同名会话",
            chat_type="group",
        )

        self.assertFalse(result)
        self.assertIs(bot._listen_chats[("private", "同名会话")], private_chat)
        self.assertIs(bot._listen_chats[("group", "同名会话")], group_chat)
        self.assertEqual(bot.all_Mode_listen_list, [["同名会话", 1.0, "group"]])

    def test_listener_rebuild_stops_when_existing_registration_cannot_be_removed(self):
        add_calls = []
        bot = SimpleNamespace(
            wx=SimpleNamespace(
                AddListenChat=lambda **kwargs: add_calls.append(kwargs) or True,
            ),
        )

        with (
            mock.patch.object(listening, "get_cached_or_verified_subwindow", return_value=None),
            mock.patch.object(listening, "_remove_listen_chat_verified_locked", return_value=False),
        ):
            result = listening._rebuild_listener_window(
                bot,
                "同名会话",
                chat_type="group",
            )

        self.assertIsNone(result)
        self.assertEqual(add_calls, [])

    def test_close_dynamic_listener_subwindows_removes_runtime_entry_after_close(self):
        calls = []
        bot = SimpleNamespace(
            all_Mode_listen_list=[["张三", 1], ["李四", 2]],
        )
        bot._remove_listen_chat_verified = lambda name, *, chat_type=None, log_success=True: calls.append((name, chat_type, log_success)) or True

        closed = listening.close_dynamic_listener_subwindows(bot, ["张三", "王五"])

        self.assertEqual(closed, ["张三"])
        self.assertEqual(calls, [("张三", "private", True)])
        self.assertEqual(bot.all_Mode_listen_list, [["李四", 2]])

    def test_touch_dynamic_listener_entry_updates_existing_timestamp(self):
        bot = SimpleNamespace(all_Mode_listen_list=[["阿英2", 1.0], ["阿英3", 2.0]])

        touched = listening.touch_dynamic_listener_entry(bot, "阿英2", timestamp=9.0)

        self.assertTrue(touched)
        self.assertEqual(bot.all_Mode_listen_list, [["阿英2", 9.0, "private"], ["阿英3", 2.0]])

    def test_touch_dynamic_listener_entry_adds_missing_entry(self):
        bot = SimpleNamespace(all_Mode_listen_list=[])

        touched = listening.touch_dynamic_listener_entry(bot, "阿英2", timestamp=9.0)

        self.assertTrue(touched)
        self.assertEqual(bot.all_Mode_listen_list, [["阿英2", 9.0, "private"]])

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

        def remove_and_clear(name, *, chat_type=None, log_success=True):
            removed.append(name)
            listening.remove_dynamic_listener_entries(bot, name, chat_type=chat_type)
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

        def remove_and_clear(name, *, chat_type=None, log_success=True):
            removed.append(name)
            listening.remove_dynamic_listener_entries(bot, name, chat_type=chat_type)
            return True

        bot._remove_listen_chat_verified = remove_and_clear

        with mock.patch.object(listening.time, "time", return_value=999.0), mock.patch.object(
            listening, "_bot_log", side_effect=lambda _bot, *args, **kwargs: logs.append(kwargs.get("message") or (args[0] if args else ""))
        ):
            listening.alllisten_mode(bot, last_time=1.0, timeout=10)

        self.assertEqual(removed, ["管理员", "张三"])
        self.assertEqual(bot.all_Mode_listen_list, [["阿英2", 1.0]])
        self.assertEqual(logs, [
            "全局监听 管理员：对话超时，已停止监听",
            "全局监听 张三：对话超时，已停止监听",
        ])

    def test_add_and_verify_uses_add_listen_returned_chat_directly(self):
        calls = []
        sub_chat = SimpleNamespace(who="张三", chat_type="private", SendMsg=lambda _msg: True)

        class FakeWeChat:
            def AddListenChat(self, nickname=None, chat_type=None):
                calls.append(("AddListenChat", nickname, chat_type))
                return sub_chat

            def GetSubWindow(self, nickname=None, chat_type=None):
                calls.append(("GetSubWindow", nickname, chat_type))
                return None

        bot = SimpleNamespace(wx=FakeWeChat(), _listen_chats={})

        result = listening.add_and_verify_subwindow(bot, "张三")

        self.assertIs(result, sub_chat)
        self.assertEqual(
            calls,
            [("GetSubWindow", "张三", "private"), ("AddListenChat", "张三", "private")],
        )
        self.assertIs(bot._listen_chats[("private", "张三")], sub_chat)

    def test_listener_reconcile_allows_future_window_recovery_pending(self):
        bot = SimpleNamespace(
            wx=object(),
            config=SimpleNamespace(AllListen_switch=True),
            _listener_reconcile_interval_seconds=30,
            _listener_reconcile_last_at=0.0,
        )
        listening.ensure_listener_window_recovery_state(bot).request("张三", now=200.0)

        with mock.patch.object(listening.time, "time", return_value=100.0), mock.patch.object(
            listening,
            "reconcile_listener_subwindows",
            return_value=["管理员"],
        ):
            reopened = listening.maybe_reconcile_listener_subwindows(bot)

        self.assertEqual(reopened, ["管理员"])
        self.assertEqual(bot._listener_reconcile_last_at, 100.0)

    def test_listener_reconcile_ignores_window_recovery_outside_alllisten_mode(self):
        bot = SimpleNamespace(
            wx=object(),
            config=SimpleNamespace(AllListen_switch=False),
            _listener_reconcile_interval_seconds=30,
            _listener_reconcile_last_at=0.0,
        )
        listening.ensure_listener_window_recovery_state(bot).request("张三", now=0.0)

        with mock.patch.object(listening, "reconcile_listener_subwindows", return_value=["管理员"]):
            reopened = listening.maybe_reconcile_listener_subwindows(bot)

        self.assertEqual(reopened, ["管理员"])

    def test_add_listen_chat_once_returns_wechat_result(self):
        calls = []
        logs = []

        class FakeWeChat:
            def AddListenChat(self, nickname=None, chat_type=None):
                calls.append((nickname, chat_type))
                return {"status": "success"}

        bot = SimpleNamespace(wx=FakeWeChat())

        with mock.patch.object(listening, "_bot_log", side_effect=lambda _bot, *args, **kwargs: logs.append(kwargs.get("message") or (args[0] if args else ""))):
            result = listening.add_listen_chat_once(bot, "张三", "动态监听")

        self.assertEqual(result, {"status": "success"})
        self.assertEqual(calls, [("张三", "private")])
        self.assertFalse(any("监听管理 张三：添加动态监听调用成功" in item for item in logs))
        self.assertFalse(any("监听管理 张三：添加动态监听失败" in item for item in logs))

    def test_dynamic_listener_add_failure_is_silent_until_global_retry_log(self):
        logs = []

        class FakeWeChat:
            def AddListenChat(self, nickname=None, chat_type=None):
                raise RuntimeError("无效的窗口句柄")

        bot = SimpleNamespace(wx=FakeWeChat())

        with mock.patch.object(listening, "_bot_log", side_effect=lambda _bot, *args, **kwargs: logs.append((kwargs.get("level", "INFO"), kwargs.get("message") or (args[0] if args else "")))):
            result = listening.add_listen_chat_once(bot, "张三", "动态监听")

        self.assertIsNone(result)
        self.assertEqual(logs, [])

    def test_manual_listener_add_failure_stays_error(self):
        logs = []

        class FakeWeChat:
            def AddListenChat(self, nickname=None, chat_type=None):
                raise RuntimeError("无效的窗口句柄")

        bot = SimpleNamespace(wx=FakeWeChat())

        with mock.patch.object(listening, "_bot_log", side_effect=lambda _bot, *args, **kwargs: logs.append((kwargs.get("level", "INFO"), kwargs.get("message") or (args[0] if args else "")))):
            result = listening.add_listen_chat_once(bot, "张三", "监听")

        self.assertIsNone(result)
        self.assertTrue(any(level == "ERROR" and "添加监听调用异常" in message for level, message in logs))


if __name__ == "__main__":
    unittest.main()
