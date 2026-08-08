import unittest
import threading
import time
import pywintypes
from types import SimpleNamespace
from unittest.mock import patch
from wxautox4.param import WxResponse

from feature import listening
from core.message_pipeline import ConversationRef, MessageEnvelope
from core.wechat_ui_actions import (
    ActionBatchInterrupted,
    ContactBatchHandle,
    IntentNeedsExclusive,
    UIOutboundNotStarted,
)
from core.wechat_ui_actions import UIIntent, UIIntentKind, WeChatUIOwner
from core.wechat_ui_runtime import (
    ListenSubwindowNotReady,
    MoveWindowListenRecoveryExhausted,
    WeChatUIRuntime,
)


class FakeChat:
    _next_hwnd = 1000

    def __init__(self, who):
        type(self)._next_hwnd += 1
        self.who = who
        self.chat_type = "private"
        self._api = SimpleNamespace(HWND=type(self)._next_hwnd, auto_resize=lambda: None)
        self.sent = []
        self.messages = []
        self.history_messages = None
        self.history_calls = []

    def GetAllMessage(self):
        return list(self.messages)

    def SendMsg(self, msg, at=None):
        self.sent.append((msg, at))
        return True

    def SendFiles(self, filepath):
        return filepath

    def SendAudio(self, filepath, duration=None):
        return filepath

    def GetHistoryMessage(self, limit, callback=None, **_kwargs):
        self.history_calls.append(limit)
        source = self.messages if self.history_messages is None else self.history_messages
        messages = list(source)[-limit:]
        for message in messages:
            if callback and callback(message) == "stop":
                break
        return messages


class FakeClient:
    nickname = "测试账号"

    def __init__(self):
        self.chats = {}
        self.listen = {}
        self.callback = None
        self.stop_count = 0
        self.stop_remove = []
        self.start_count = 0
        self.switch_to_chat_count = 0

    def GetMyInfo(self):
        return {"id": "wxid-test"}

    def IsOnline(self):
        return True

    def StopListening(self, remove=True):
        self.stop_count += 1
        self.stop_remove.append(remove)
        if remove:
            self.listen.clear()
        return True

    def StartListening(self):
        self.start_count += 1
        return True

    def SwitchToChat(self):
        self.switch_to_chat_count += 1
        return True

    def GetSubWindow(self, nickname):
        return self.chats.get(nickname)

    def AddListenChat(self, nickname, callback):
        self.callback = callback
        chat = self.chats.setdefault(nickname, FakeChat(nickname))
        self.listen[nickname] = (chat, callback)
        return chat

    def RemoveListenChat(self, nickname):
        self.chats.pop(nickname, None)
        self.listen.pop(nickname, None)
        return True

    def GetNextNewMessage(self, filter_mute=False, callback=None):
        message = SimpleNamespace(
            id="img-1",
            type="image",
            attr="friend",
            sender="张三",
            content="[图片]",
            download=lambda: "C:/temp/global.png",
        )
        if callback:
            callback(message)
        return {"chat_name": "张三", "chat_type": "friend", "msg": [message]}

    def ChatWith(self, who=None, exact=True):
        self.current_chat = who
        return True

    def ChatInfo(self):
        return {"chat_type": "friend", "chat_name": self.current_chat}

    def EditFriendInfo(self, **kwargs):
        self.edit_kwargs = kwargs
        return {"status": "成功"}

    def GetSession(self):
        return [SimpleNamespace(name="张三", content="你好", time="10:30")]

    def GetAllSubWindow(self):
        return list(self.chats.values())

    def IsOnline(self):
        return True


class FakeMessageControl:
    def __init__(self, *, class_name="", automation_id="message", previous=None):
        self.ClassName = class_name
        self.AutomationId = automation_id
        self.previous = previous

    def GetPreviousSiblingControl(self):
        return self.previous


class WeChatUIRuntimeTests(unittest.TestCase):
    def setUp(self):
        window_patch = patch("core.wechat_ui_runtime.win32gui.IsWindow", return_value=True)
        window_patch.start()
        self.addCleanup(window_patch.stop)

    @staticmethod
    def _poll_global_messages(messages, *, client=None):
        client = client or FakeClient()
        client.GetSession = lambda: []
        client.GetNextNewMessage = lambda **_kwargs: {
            "chat_name": "张三",
            "chat_type": "friend",
            "msg": messages,
        }
        runtime = WeChatUIRuntime(lambda *_args: None, client_factory=lambda _version: client)
        runtime.bootstrap({"listeners": []})
        return runtime.poll_messages({"mode": "next"})

    def test_subwindow_callback_downloads_exact_original_image_before_copying(self):
        received = []
        downloads = []
        runtime = WeChatUIRuntime(
            lambda conversation, message: received.append((conversation, message)),
            inbound_media_enabled=lambda _conversation, _message_type: True,
        )
        runtime.set_owner(SimpleNamespace(run_callback_action=lambda _intent, action: action()))
        original = SimpleNamespace(
            type="image",
            attr="friend",
            sender="瑞东（私人号）",
            content="图片",
            id="",
            hash="",
            hash_text="",
            time="",
            download=lambda: downloads.append(True) or "C:/temp/exact.png",
        )

        runtime._callback(original, SimpleNamespace(who="瑞东（私人号）", chat_type="friend"))

        self.assertEqual(downloads, [True])
        self.assertEqual(received[0][0].chat_type, "private")
        self.assertEqual(received[0][1].content, "C:/temp/exact.png")
        self.assertTrue(received[0][1]._wxbot_media_prepared)
        self.assertFalse(hasattr(received[0][1], "download"))

    def test_subwindow_callback_contains_original_image_download_failure(self):
        received = []
        runtime = WeChatUIRuntime(
            lambda _conversation, message: received.append(message),
            inbound_media_enabled=lambda _conversation, _message_type: True,
        )
        runtime.set_owner(SimpleNamespace(run_callback_action=lambda _intent, action: action()))

        runtime._callback(
            SimpleNamespace(
                type="image", attr="friend", sender="张三", content="图片",
                download=lambda: (_ for _ in ()).throw(RuntimeError("download failed")),
            ),
            SimpleNamespace(who="张三", chat_type="private"),
        )

        self.assertEqual(len(received), 1)
        self.assertTrue(received[0]._wxbot_media_prepared)
        self.assertTrue(received[0]._skip_ai_reply)

    def test_subwindow_image_callback_waits_for_contact_recovery_before_download(self):
        contact_done = threading.Event()
        downloads = []
        received = []
        runtime = WeChatUIRuntime(
            lambda _conversation, message: received.append(message),
            inbound_media_enabled=lambda _conversation, _message_type: True,
        )
        handlers = runtime.handlers()
        handlers[UIIntentKind.CONTACT_START] = lambda _payload: ContactBatchHandle(
            poll=lambda: (contact_done.is_set(), True),
        )
        handlers[UIIntentKind.CONTACT_RECOVER] = lambda _payload: True
        owner = WeChatUIOwner(handlers, poll_interval=0.01)
        owner.start()
        runtime.set_owner(owner)
        contact = owner.submit(UIIntent(UIIntentKind.CONTACT_START))
        deadline = time.time() + 1
        while not owner.contact_active and time.time() < deadline:
            time.sleep(0.01)

        callback = threading.Thread(target=lambda: runtime._callback(
            SimpleNamespace(
                type="image", attr="friend", sender="张三", content="图片",
                download=lambda: downloads.append(True) or "C:/temp/after-contact.png",
            ),
            SimpleNamespace(who="张三", chat_type="private"),
        ))
        callback.start()
        try:
            time.sleep(0.03)
            self.assertEqual(downloads, [])
            self.assertEqual(received, [])
            contact_done.set()
            contact.result(1)
            callback.join(1)
        finally:
            owner.stop()

        self.assertFalse(callback.is_alive())
        self.assertEqual(downloads, [True])
        self.assertEqual(received[0].content, "C:/temp/after-contact.png")

    def test_contact_batch_pauses_listener_without_removing_windows_until_recovery(self):
        client = FakeClient()
        runtime = WeChatUIRuntime(lambda *_args: None, client_factory=lambda _version: client)
        runtime.bootstrap({"listeners": []})
        client.stop_count = 0
        client.stop_remove.clear()
        client.start_count = 0
        process = SimpleNamespace(poll=lambda: (False, None), terminate=lambda: None)

        with patch(
            "feature.contacts.start_contact_auto_maintenance_collector",
            return_value=process,
        ):
            handle = runtime.start_contact_batch({"start_name": "张三"})

        self.assertIsInstance(handle, ContactBatchHandle)
        self.assertEqual(client.stop_remove, [False])
        self.assertEqual(client.start_count, 0)
        self.assertTrue(runtime.recover_chat_page({}))
        self.assertEqual(client.switch_to_chat_count, 1)
        self.assertEqual(client.start_count, 1)

    def test_contact_recovery_does_not_restart_listener_when_returning_to_chat_fails(self):
        client = FakeClient()
        runtime = WeChatUIRuntime(lambda *_args: None, client_factory=lambda _version: client)
        runtime.bootstrap({"listeners": []})
        client.start_count = 0
        process = SimpleNamespace(poll=lambda: (False, None), terminate=lambda: None)

        with patch(
            "feature.contacts.start_contact_auto_maintenance_collector",
            return_value=process,
        ):
            runtime.start_contact_batch({"start_name": "张三"})
        with patch.object(client, "SwitchToChat", side_effect=RuntimeError("chat page unavailable")):
            with self.assertRaisesRegex(RuntimeError, "chat page unavailable"):
                runtime.recover_chat_page({})

        self.assertEqual(client.start_count, 0)
        self.assertTrue(runtime._listener_paused_for_contact)

        self.assertTrue(runtime.recover_chat_page({}))
        self.assertEqual(client.switch_to_chat_count, 1)
        self.assertEqual(client.start_count, 1)
        self.assertFalse(runtime._listener_paused_for_contact)

    def test_listener_auto_recovery_rebuilds_listener_without_rebinding_client(self):
        first = FakeClient()
        clients = iter([first])
        runtime = WeChatUIRuntime(lambda *_args: None, client_factory=lambda _version: next(clients))
        owner = WeChatUIOwner(runtime.handlers())
        owner.start()
        runtime.set_heartbeat(owner.heartbeat_current_action)
        identity = owner.call(UIIntent(UIIntentKind.BOOTSTRAP, {"listeners": ["管理员"]}), 1)

        bot = SimpleNamespace(
            _ui_owner=owner,
            _ui_runtime=runtime,
            _ui_identity=identity,
            wx=None,
            config=SimpleNamespace(
                cmd="管理员",
                AllListen_switch=False,
                listen_list=[],
                group_switch=False,
                group=[],
            ),
            all_Mode_listen_list=[],
            _listen_chats={},
            _listener_reconcile_last_at=0.0,
            callback_is_die=False,
            message_handle_callback=lambda *_args: None,
        )
        from core.wechat_ui_runtime import UIClientFacade
        bot.wx = UIClientFacade(owner, identity)
        recovery = listening.listener_recovery_coordinator(bot)
        recovery.arm(RuntimeError("Find Control Timeout: desktop unavailable"), source="test", now=0)
        try:
            with patch("feature.listening.time.sleep", return_value=None):
                result = listening.process_listener_auto_recovery(bot)
        finally:
            owner.stop()

        self.assertEqual(result, "recovered")
        self.assertIsInstance(bot.wx, UIClientFacade)
        self.assertIs(runtime._client, first)
        self.assertIn("管理员", first.chats)
        self.assertFalse(recovery.state_snapshot().active)

    def test_listener_auto_recovery_rebinds_after_invalid_client_handle(self):
        bot = SimpleNamespace(
            wx=object(),
            callback_is_die=False,
        )
        recovery = listening.listener_recovery_coordinator(bot)
        recovery.arm(RuntimeError("Find Control Timeout: invalid handle"), source="test", now=0)
        rebound = object()
        with (
            patch(
                "feature.listening.probe_listener_recovery_client",
                side_effect=[OSError(1400, "MoveWindow", "无效的窗口句柄。"), rebound],
            ) as probe,
            patch("feature.listening.rebuild_listener_runtime", return_value=True),
            patch("feature.listening._bot_log") as recovery_log,
        ):
            result = listening.process_listener_auto_recovery(bot)

        self.assertEqual(result, "recovered")
        self.assertIs(bot.wx, rebound)
        self.assertEqual(probe.call_count, 2)
        self.assertEqual(probe.call_args_list[1].kwargs, {"force_rebind": True})
        self.assertTrue(recovery.state_snapshot().after_rebind)
        messages = [call.kwargs["message"] for call in recovery_log.call_args_list]
        self.assertTrue(any("自恢复【微信客户端重绑】开始" in message for message in messages))
        self.assertTrue(any("自恢复【微信客户端重绑】成功" in message for message in messages))

    def test_listener_auto_recovery_keeps_waiting_after_transient_rebuild_error(self):
        bot = SimpleNamespace(
            wx=object(),
            callback_is_die=False,
        )
        recovery = listening.listener_recovery_coordinator(bot)
        recovery.arm(RuntimeError("Find Control Timeout: desktop unavailable"), source="test", now=0)
        with (
            patch("feature.listening.probe_listener_recovery_client", return_value=bot.wx),
            patch(
                "feature.listening.rebuild_listener_runtime",
                side_effect=RuntimeError("事件无法调用任何订户"),
            ),
        ):
            result = listening.process_listener_auto_recovery(bot)

        self.assertEqual(result, "waiting")
        self.assertTrue(recovery.state_snapshot().active)
        self.assertGreater(recovery.state_snapshot().probe_after, 0)

    def test_rebind_recreates_client_and_restores_listener_names(self):
        first = FakeClient()
        second = FakeClient()
        clients = iter([first, second])
        runtime = WeChatUIRuntime(lambda *_args: None, client_factory=lambda _version: next(clients))
        owner = WeChatUIOwner(runtime.handlers())
        owner.start()
        runtime.set_heartbeat(owner.heartbeat_current_action)
        try:
            owner.call(UIIntent(UIIntentKind.BOOTSTRAP, {"listeners": ["阿英4"]}), 1)
            identity = owner.call(UIIntent(UIIntentKind.REBIND), 1)
        finally:
            owner.stop()

        self.assertEqual(identity["listeners"], ["阿英4"])
        self.assertIn("阿英4", second.chats)
        self.assertEqual(first.stop_count, 2)
        self.assertEqual(second.stop_count, 1)
        self.assertEqual(second.start_count, 1)

    def test_rebind_with_explicit_empty_listeners_does_not_restore_runtime_windows(self):
        first = FakeClient()
        second = FakeClient()
        clients = iter([first, second])
        runtime = WeChatUIRuntime(lambda *_args: None, client_factory=lambda _version: next(clients))
        runtime.bootstrap({"listeners": ["阿英4"]})

        identity = runtime.rebind({"listeners": []})

        self.assertEqual(identity["listeners"], [])
        self.assertEqual(second.listen, {})

    def test_listener_rebuild_is_one_owner_intent_and_restores_fixed_listeners(self):
        client = FakeClient()
        runtime = WeChatUIRuntime(lambda *_args: None, client_factory=lambda _version: client)
        owner = WeChatUIOwner(runtime.handlers())
        owner.start()
        runtime.set_heartbeat(owner.heartbeat_current_action)
        try:
            owner.call(UIIntent(UIIntentKind.BOOTSTRAP, {"listeners": []}), 1)
            client.stop_count = 0
            client.start_count = 0
            restored = owner.call(UIIntent(
                UIIntentKind.REBUILD_LISTENER,
                {"listeners": [{"name": "管理员", "chat_type": "private"}]},
            ), 1)
        finally:
            owner.stop()

        self.assertEqual(client.stop_count, 1)
        self.assertEqual(client.start_count, 1)
        self.assertEqual(restored["listeners"], [{"name": "管理员", "chat_type": "private"}])

    def test_owner_frozen_listener_payload_bootstraps_real_runtime_shape(self):
        client = FakeClient()
        runtime = WeChatUIRuntime(lambda *_args: None, client_factory=lambda _version: client)
        owner = WeChatUIOwner(runtime.handlers())
        owner.start()
        runtime.set_heartbeat(owner.heartbeat_current_action)
        try:
            identity = owner.call(UIIntent(
                UIIntentKind.BOOTSTRAP,
                {"listeners": [{"name": "阿英4"}]},
            ), 1)
        finally:
            owner.stop()

        self.assertEqual(identity["listeners"], ["阿英4"])
        self.assertIn("阿英4", client.chats)

    def test_bootstrap_and_callback_expose_only_pure_records(self):
        received = []
        client = FakeClient()
        runtime = WeChatUIRuntime(lambda chat, msg: received.append((chat, msg)), client_factory=lambda _version: client)

        identity = runtime.bootstrap({"listeners": [{"name": "张三"}]})
        client.callback(
            SimpleNamespace(type="text", attr="friend", sender="张三", content="你好", download=lambda: None),
            client.chats["张三"],
        )

        self.assertEqual(identity["wx_id"], "wxid-test")
        self.assertIsInstance(received[0][0], ConversationRef)
        self.assertIsInstance(received[0][1], MessageEnvelope)
        self.assertFalse(hasattr(received[0][1], "download"))

    def test_light_send_requires_exclusive_retry_before_adding_missing_chat(self):
        client = FakeClient()
        runtime = WeChatUIRuntime(lambda *_args: None, client_factory=lambda _version: client)
        runtime.bootstrap({"listeners": []})

        with self.assertRaises(IntentNeedsExclusive):
            runtime.send_text({"conversation": "张三", "chat_type": "private", "text": "你好"})

        result = runtime.send_text({"conversation": "张三", "chat_type": "private", "text": "你好", "_exclusive_retry": True})

        self.assertTrue(result)
        self.assertEqual(client.chats["张三"].sent, [("你好", None)])

    def test_same_name_private_and_group_windows_use_requested_chat_type(self):
        private_chat = FakeChat("同名会话")
        group_chat = FakeChat("同名会话")
        group_chat.chat_type = "group"

        class SameNameClient(FakeClient):
            def GetAllSubWindow(self):
                return [private_chat, group_chat]

            def GetSubWindow(self, nickname):
                return private_chat if nickname == "同名会话" else None

        runtime = WeChatUIRuntime(
            lambda *_args: None,
            client_factory=lambda _version: SameNameClient(),
        )
        runtime.bootstrap({"listeners": []})

        runtime.send_text({
            "conversation": "同名会话",
            "chat_type": "private",
            "text": "私聊回复",
        })
        runtime.send_text({
            "conversation": "同名会话",
            "chat_type": "group",
            "text": "群聊回复",
        })

        self.assertEqual(private_chat.sent, [("私聊回复", None)])
        self.assertEqual(group_chat.sent, [("群聊回复", None)])

    def test_untyped_same_name_window_lookup_refuses_to_guess(self):
        private_chat = FakeChat("同名会话")
        group_chat = FakeChat("同名会话")
        group_chat.chat_type = "group"

        class SameNameClient(FakeClient):
            def GetAllSubWindow(self):
                return [private_chat, group_chat]

        runtime = WeChatUIRuntime(
            lambda *_args: None,
            client_factory=lambda _version: SameNameClient(),
        )
        runtime.bootstrap({"listeners": []})

        with self.assertRaisesRegex(RuntimeError, "拒绝猜测"):
            runtime.main_window({
                "operation": "subwindow_identity",
                "conversation": "同名会话",
            })

    def test_duplicate_same_type_windows_refuse_to_send(self):
        first = FakeChat("重复群")
        second = FakeChat("重复群")
        first.chat_type = "group"
        second.chat_type = "group"

        class DuplicateGroupClient(FakeClient):
            def GetAllSubWindow(self):
                return [first, second]

        runtime = WeChatUIRuntime(
            lambda *_args: None,
            client_factory=lambda _version: DuplicateGroupClient(),
        )
        runtime.bootstrap({"listeners": []})

        with self.assertRaisesRegex(RuntimeError, "无法区分"):
            runtime.send_text({
                "conversation": "重复群",
                "chat_type": "group",
                "text": "不能猜目标",
            })
        self.assertEqual(first.sent, [])
        self.assertEqual(second.sent, [])

    def test_send_actions_reports_completed_boundary_when_middle_action_raises(self):
        client = FakeClient()
        runtime = WeChatUIRuntime(lambda *_args: None, client_factory=lambda _version: client)
        runtime.bootstrap({"listeners": [{"name": "张三"}]})
        chat = client.chats["张三"]

        def send_msg(msg, at=None):
            if msg == "第二条":
                raise RuntimeError("结果丢失")
            chat.sent.append((msg, at))
            return True

        chat.SendMsg = send_msg
        with self.assertRaises(ActionBatchInterrupted) as caught:
            runtime.send_actions({
                "conversation": "张三",
                "chat_type": "private",
                "actions": [
                    {"type": "text", "text": "第一条"},
                    {"type": "text", "text": "第二条"},
                    {"type": "text", "text": "第三条"},
                ],
            })

        self.assertEqual(caught.exception.completed_results, [True])
        self.assertEqual(caught.exception.failed_index, 1)
        self.assertEqual(chat.sent, [("第一条", None)])

    def test_send_actions_reports_explicit_false_at_the_exact_action(self):
        client = FakeClient()
        runtime = WeChatUIRuntime(lambda *_args: None, client_factory=lambda _version: client)
        runtime.bootstrap({"listeners": [{"name": "张三"}]})
        chat = client.chats["张三"]

        def send_msg(msg, at=None):
            if msg == "第二条":
                return False
            chat.sent.append((msg, at))
            return True

        chat.SendMsg = send_msg
        with self.assertRaises(ActionBatchInterrupted) as caught:
            runtime.send_actions({
                "conversation": "张三",
                "chat_type": "private",
                "actions": [
                    {"type": "text", "text": "第一条"},
                    {"type": "text", "text": "第二条"},
                    {"type": "text", "text": "第三条"},
                ],
            })

        self.assertEqual(caught.exception.completed_results, [True])
        self.assertEqual(caught.exception.failed_index, 1)
        self.assertEqual(chat.sent, [("第一条", None)])

    def test_quote_rolls_original_message_into_view_before_action(self):
        client = FakeClient()
        chat = client.chats.setdefault("测试群", FakeChat("测试群"))
        chat.chat_type = "group"
        events = []
        source = SimpleNamespace(
            type="text",
            attr="friend",
            sender="群成员",
            content="原消息",
            hash="hash-roll",
            roll_into_view=lambda: events.append("roll"),
            quote=lambda _text, at=None: events.append(("quote", at)) or True,
        )
        chat.messages = [source]
        reported = []
        runtime = WeChatUIRuntime(
            lambda *_args: None,
            client_factory=lambda _version: client,
            listener_operation_succeeded=reported.append,
        )
        runtime.bootstrap({"listeners": [{"name": "测试群", "chat_type": "group"}]})
        reported.clear()

        runtime.quote_message({
            "conversation": "测试群",
            "chat_type": "group",
            "message_type": "text",
            "message_attr": "friend",
            "message_sender": "群成员",
            "message_content": "原消息",
            "message_hash": "hash-roll",
            "text": "回复",
            "at": "群成员",
        })

        self.assertEqual(events, ["roll", ("quote", "群成员")])
        self.assertEqual(reported, [ConversationRef("测试群", "group")])

    def test_message_snapshot_does_not_return_wxautox_message(self):
        client = FakeClient()
        client.chats["张三"] = FakeChat("张三")
        client.chats["张三"].messages = [SimpleNamespace(type="voice", attr="friend", sender="张三", content="正文")]
        runtime = WeChatUIRuntime(lambda *_args: None, client_factory=lambda _version: client)
        runtime.bootstrap({"listeners": [{"name": "张三"}]})

        messages = runtime.get_messages({"conversation": "张三", "chat_type": "private"})

        self.assertEqual([message.content for message in messages], ["正文"])
        self.assertTrue(all(isinstance(message, MessageEnvelope) for message in messages))

    def test_voice_snapshot_uses_visible_wechat_transcription_inside_owner(self):
        client = FakeClient()
        client.chats["LXYou"] = FakeChat("LXYou")
        client.chats["LXYou"].messages = [SimpleNamespace(
            type="voice",
            attr="friend",
            sender="LXYou",
            content='语音4"秒',
            control=SimpleNamespace(Name='语音4"秒私聊验收，C语语音测试。'),
        )]
        runtime = WeChatUIRuntime(lambda *_args: None, client_factory=lambda _version: client)
        runtime.bootstrap({"listeners": [{"name": "LXYou"}]})

        messages = runtime.get_messages({"conversation": "LXYou", "chat_type": "private"})

        self.assertEqual(messages[0].content, '语音4"秒私聊验收，C语语音测试。')
        self.assertEqual(messages[0].original_content, '语音4"秒私聊验收，C语语音测试。')
        self.assertFalse(hasattr(messages[0], "control"))

    def test_snapshot_read_does_not_emit_history_side_effect_callbacks(self):
        received = []
        client = FakeClient()
        chat = client.chats.setdefault("瑞东（私人号）", FakeChat("瑞东（私人号）"))
        old_message = SimpleNamespace(
            type="text",
            attr="self",
            sender="self",
            content="旧的自己消息",
        )
        def get_all_messages():
            client.callback(old_message, chat)
            return [old_message]

        chat.GetAllMessage = get_all_messages
        runtime = WeChatUIRuntime(
            lambda conversation, message: received.append((conversation, message)),
            client_factory=lambda _version: client,
        )
        runtime.bootstrap({"listeners": [{"name": "瑞东（私人号）"}]})

        messages = runtime.get_messages({"conversation": "瑞东（私人号）", "chat_type": "private"})

        self.assertEqual([message.content for message in messages], ["旧的自己消息"])
        self.assertEqual(received, [])

    def test_snapshot_read_keeps_real_callback_from_another_thread(self):
        received = []
        client = FakeClient()
        chat = client.chats.setdefault("瑞东（私人号）", FakeChat("瑞东（私人号）"))
        getter_started = threading.Event()
        callback_finished = threading.Event()
        real_message = SimpleNamespace(
            id="new-1", type="text", attr="friend", sender="瑞东（私人号）", content="刚发的新消息",
        )

        def get_all_messages():
            getter_started.set()
            self.assertTrue(callback_finished.wait(1))
            return []

        def emit_real_message():
            self.assertTrue(getter_started.wait(1))
            client.callback(real_message, chat)
            callback_finished.set()

        chat.GetAllMessage = get_all_messages
        runtime = WeChatUIRuntime(
            lambda conversation, message: received.append((conversation, message)),
            client_factory=lambda _version: client,
        )
        runtime.bootstrap({"listeners": [{"name": "瑞东（私人号）"}]})
        thread = threading.Thread(target=emit_real_message)
        thread.start()

        runtime.get_messages({"conversation": "瑞东（私人号）", "chat_type": "private"})
        thread.join(1)

        self.assertEqual([message.content for _conversation, message in received], ["刚发的新消息"])

    def test_snapshot_callback_suppression_releases_after_getter_error(self):
        received = []
        client = FakeClient()
        chat = client.chats.setdefault("张三", FakeChat("张三"))
        chat.GetAllMessage = lambda: (_ for _ in ()).throw(RuntimeError("snapshot failed"))
        runtime = WeChatUIRuntime(
            lambda conversation, message: received.append((conversation, message)),
            client_factory=lambda _version: client,
        )
        runtime.bootstrap({"listeners": [{"name": "张三"}]})

        with self.assertRaisesRegex(RuntimeError, "snapshot failed"):
            runtime.get_messages({"conversation": "张三", "chat_type": "private"})
        client.callback(
            SimpleNamespace(id="new-1", type="text", attr="friend", sender="张三", content="错误后的新消息"),
            chat,
        )

        self.assertEqual([message.content for _conversation, message in received], ["错误后的新消息"])

    def test_global_poll_returns_unpersisted_pure_batch_and_scan_metadata(self):
        client = FakeClient()
        client.GetSession = lambda: [SimpleNamespace(
            name="张三",
            chat_type="friend",
            isnew=True,
            new_count=3,
            ismute=False,
        )]
        runtime = WeChatUIRuntime(lambda *_args: None, client_factory=lambda _version: client)
        runtime.bootstrap({"listeners": []})

        batch = runtime.poll_messages({"mode": "next"})

        self.assertEqual(batch["chat_name"], "张三")
        self.assertEqual(batch["chat_type"], "private")
        self.assertEqual(batch["msg"][0].content, "[图片]")
        self.assertFalse(hasattr(batch["msg"][0], "_wxbot_persisted"))
        self.assertEqual(batch["unread_before"][0]["new_count"], 3)
        self.assertEqual(batch["max_quantity"], 30)
        self.assertEqual(batch["max_runtime_seconds"], 10.0)
        self.assertFalse(hasattr(batch["msg"][0], "download"))

    def test_global_poll_skips_unsupported_chat_type_before_internal_conversion(self):
        client = FakeClient()
        raw_message = SimpleNamespace(
            id="official-1",
            type="text",
            attr="system",
            sender="服务通知",
            content="通知内容",
        )
        client.GetSession = lambda: [SimpleNamespace(
            name="服务通知",
            chat_type="official",
            isnew=True,
            new_count=1,
            ismute=False,
        )]
        client.GetNextNewMessage = lambda **_kwargs: {
            "chat_name": "服务通知",
            "chat_type": "official",
            "msg": [raw_message],
        }
        runtime = WeChatUIRuntime(lambda *_args: None, client_factory=lambda _version: client)
        runtime.bootstrap({"listeners": []})

        batch = runtime.poll_messages({"mode": "next"})

        self.assertEqual(batch["ignored_unsupported_chat_type"], "official")
        self.assertEqual(batch["raw_message_count"], 1)
        self.assertEqual(batch["msg"], [])
        self.assertEqual(batch["unread_before"][0]["chat_type"], "")

    def test_global_poll_returns_empty_batch_when_no_messages(self):
        client = FakeClient()
        client.chat_type = None
        client.GetSession = lambda: []
        client.GetNextNewMessage = lambda **_kwargs: {}
        runtime = WeChatUIRuntime(lambda *_args: None, client_factory=lambda _version: client)
        runtime.bootstrap({"listeners": []})

        batch = runtime.poll_messages({"mode": "next"})

        self.assertEqual(batch["chat_name"], "")
        self.assertEqual(batch["chat_type"], "")
        self.assertEqual(batch["msg"], [])
        self.assertNotIn("ignored_unsupported_chat_type", batch)

    def test_global_poll_prefers_preceding_time_from_returned_batch(self):
        messages = [
            SimpleNamespace(type="time", attr="system", content="00:07", time="2026-07-16 00:07:00"),
            SimpleNamespace(type="time", attr="system", content="00:20", time="2026-07-16 00:20:00"),
            SimpleNamespace(type="text", attr="friend", sender="张三", content="刚发的消息", time=""),
            SimpleNamespace(type="time", attr="system", content="01:53", time="2026-07-16 01:53:00"),
        ]

        with patch("core.wechat_ui_runtime.parse_msg") as parse_mock:
            batch = self._poll_global_messages(messages)

        self.assertEqual(batch["msg"][2].time, "2026-07-16 00:20:00")
        parse_mock.assert_not_called()

    def test_global_poll_resolves_missing_time_from_preceding_sibling(self):
        parent = object()
        time_control = FakeMessageControl(class_name="mmui::ChatItemView", automation_id="")
        message_control = FakeMessageControl(previous=time_control)
        message = SimpleNamespace(
            type="text",
            attr="friend",
            sender="张三",
            content="昨晚积压的消息",
            time="",
            control=message_control,
            parent=parent,
        )
        client = FakeClient()
        client.GetAllMessage = lambda: self.fail("取消息时间不得读取完整聊天记录")

        with patch(
            "core.wechat_ui_runtime.parse_msg",
            return_value=SimpleNamespace(type="time", time="2026-07-15 20:55:00"),
        ) as parse_mock:
            batch = self._poll_global_messages([message], client=client)

        self.assertEqual(batch["msg"][0].time, "2026-07-15 20:55:00")
        parse_mock.assert_called_once_with(time_control, parent)

    def test_global_poll_uses_nearest_preceding_time_and_only_marks_last_inbound(self):
        parent = object()
        old_time = FakeMessageControl(class_name="mmui::ChatItemView", automation_id="")
        first_message_control = FakeMessageControl(previous=old_time)
        nearest_time = FakeMessageControl(
            class_name="mmui::ChatItemView",
            automation_id="",
            previous=first_message_control,
        )
        last_message_control = FakeMessageControl(previous=nearest_time)
        messages = [
            SimpleNamespace(
                type="text", attr="friend", sender="张三", content="第一条", time="",
                control=first_message_control, parent=parent,
            ),
            SimpleNamespace(
                type="text", attr="friend", sender="张三", content="最后一条", time="",
                control=last_message_control, parent=parent,
            ),
        ]

        def parse(control, _parent):
            timestamp = (
                "2026-07-16 00:20:00"
                if control is nearest_time
                else "2026-07-16 00:07:00"
            )
            return SimpleNamespace(type="time", time=timestamp)

        with patch("core.wechat_ui_runtime.parse_msg", side_effect=parse) as parse_mock:
            batch = self._poll_global_messages(messages)

        self.assertEqual(batch["msg"][0].time, "")
        self.assertEqual(batch["msg"][1].time, "2026-07-16 00:20:00")
        parse_mock.assert_called_once_with(nearest_time, parent)

    def test_global_poll_stops_time_lookup_after_thirty_siblings(self):
        hidden_time = FakeMessageControl(class_name="mmui::ChatItemView", automation_id="")
        previous = hidden_time
        for _index in range(30):
            previous = FakeMessageControl(previous=previous)
        message = SimpleNamespace(
            type="text", attr="friend", sender="张三", content="当前消息", time="",
            control=FakeMessageControl(previous=previous), parent=object(),
        )

        with patch("core.wechat_ui_runtime.parse_msg") as parse_mock:
            batch = self._poll_global_messages([message])

        self.assertEqual(batch["msg"][0].time, "")
        parse_mock.assert_not_called()

    def test_global_poll_time_lookup_failure_does_not_fail_message_poll(self):
        time_control = FakeMessageControl(class_name="mmui::ChatItemView", automation_id="")
        message = SimpleNamespace(
            type="text", attr="friend", sender="张三", content="当前消息", time="",
            control=FakeMessageControl(previous=time_control), parent=object(),
        )
        messages = [
            SimpleNamespace(type="time", attr="system", content="00:07", time="2026-07-16 00:07:00"),
            SimpleNamespace(type="time", attr="system", content="00:20", time=""),
            message,
        ]

        with patch("core.wechat_ui_runtime.parse_msg", side_effect=RuntimeError("parse failed")):
            batch = self._poll_global_messages(messages)

        self.assertEqual(batch["msg"][2].content, "当前消息")
        self.assertEqual(batch["msg"][2].time, "")

    def test_global_poll_does_not_clear_unread_when_snapshot_fails(self):
        client = FakeClient()
        get_next_called = []
        client.GetSession = lambda: (_ for _ in ()).throw(RuntimeError("snapshot failed"))
        client.GetNextNewMessage = lambda **_kwargs: get_next_called.append(True)
        runtime = WeChatUIRuntime(lambda *_args: None, client_factory=lambda _version: client)
        runtime.bootstrap({"listeners": []})

        with self.assertRaisesRegex(RuntimeError, "snapshot failed"):
            runtime.poll_messages({"mode": "next"})

        self.assertEqual(get_next_called, [])

    def test_add_chat_reuses_current_runtime_registration(self):
        client = FakeClient()
        add_calls = []
        original_add = client.AddListenChat

        def add(nickname, callback):
            add_calls.append(nickname)
            return original_add(nickname, callback)

        client.AddListenChat = add
        runtime = WeChatUIRuntime(lambda *_args: None, client_factory=lambda _version: client)
        runtime.bootstrap({"listeners": []})

        payload = {
            "conversation": "张三",
            "chat_type": "private",
        }
        first = runtime.add_listen(payload)
        second = runtime.add_listen(payload)

        self.assertTrue(first["registration_changed"])
        self.assertFalse(second["registration_changed"])
        self.assertEqual(first["name"], second["name"])
        self.assertEqual(first["chat_type"], second["chat_type"])
        self.assertEqual(add_calls, ["张三"])

    def test_registered_listener_snapshot_requires_valid_window_and_matching_callback(self):
        client = FakeClient()
        runtime = WeChatUIRuntime(lambda *_args: None, client_factory=lambda _version: client)
        runtime.bootstrap({"listeners": []})
        healthy = FakeChat("健康会话")
        wrong_callback = FakeChat("错误回调")
        invalid_window = FakeChat("失效窗口")
        invalid_window._api.HWND = 0
        client.listen = {
            "健康会话": (healthy, runtime._callback),
            "错误回调": (wrong_callback, object()),
            "失效窗口": (invalid_window, runtime._callback),
        }

        identities = runtime.main_window({
            "operation": "registered_listeners",
            "listeners": [
                {"name": "健康会话", "chat_type": "private"},
                {"name": "错误回调", "chat_type": "private"},
                {"name": "失效窗口", "chat_type": "private"},
            ],
        })

        self.assertEqual(identities, [{"name": "健康会话", "chat_type": "private"}])

    def test_registered_listener_snapshot_does_not_mix_same_name_chat_types(self):
        client = FakeClient()
        runtime = WeChatUIRuntime(lambda *_args: None, client_factory=lambda _version: client)
        runtime.bootstrap({"listeners": []})
        group_chat = FakeChat("同名会话")
        group_chat.chat_type = "group"
        client.listen["同名会话"] = (group_chat, runtime._callback)

        identities = runtime.main_window({
            "operation": "registered_listeners",
            "listeners": [
                {"name": "同名会话", "chat_type": "private"},
                {"name": "同名会话", "chat_type": "group"},
            ],
        })

        self.assertEqual(identities, [{"name": "同名会话", "chat_type": "group"}])

    def test_facade_preserves_listener_registration_change_status(self):
        from core.wechat_ui_runtime import UIClientFacade

        submitted = []
        owner = SimpleNamespace(
            call=lambda intent, _timeout: submitted.append(intent) or {
                "name": "张三",
                "chat_type": "private",
                "registration_changed": True,
            },
        )

        chat = UIClientFacade(owner, {}).AddListenChat("张三", chat_type="private")

        self.assertTrue(chat._listener_registration_changed)
        self.assertEqual(submitted[0].payload["chat_type"], "private")

    def test_facade_reads_registered_listeners_in_one_owner_call(self):
        from core.wechat_ui_runtime import UIClientFacade

        submitted = []
        owner = SimpleNamespace(
            call=lambda intent, _timeout: submitted.append(intent) or [
                {"name": "张三", "chat_type": "private"},
            ],
        )

        chats = UIClientFacade(owner, {}).GetRegisteredListenChats([
            {"name": "张三", "chat_type": "private"},
            {"name": "李四", "chat_type": "private"},
        ])

        self.assertEqual([(chat.who, chat.chat_type) for chat in chats], [("张三", "private")])
        self.assertEqual(len(submitted), 1)
        self.assertEqual(submitted[0].payload["operation"], "registered_listeners")

    def test_remove_chat_cleans_registered_listener_after_window_disappears(self):
        client = FakeClient()
        remove_calls = []
        runtime = WeChatUIRuntime(lambda *_args: None, client_factory=lambda _version: client)
        runtime.bootstrap({"listeners": []})
        chat = FakeChat("张三")
        chat._api.HWND = 0
        client.listen["张三"] = (chat, runtime._callback)
        client.GetAllSubWindow = lambda: self.fail("清理脏登记不应依赖枚举物理窗口")

        def remove(nickname):
            remove_calls.append(nickname)
            client.listen.pop(nickname, None)
            return True

        client.RemoveListenChat = remove

        result = runtime.remove_listen({"conversation": "张三", "chat_type": "private"})

        self.assertTrue(result)
        self.assertEqual(remove_calls, ["张三"])
        self.assertNotIn("张三", client.listen)

    def test_add_chat_recovers_created_subwindow_after_move_window_1400(self):
        client = FakeClient()
        chat = FakeChat("张三")
        resize_calls = []
        chat._api = SimpleNamespace(
            HWND=101,
            auto_resize=lambda: resize_calls.append(True),
        )
        add_calls = []

        def add(nickname, callback):
            add_calls.append(nickname)
            client.chats[nickname] = chat
            if len(add_calls) == 1:
                raise pywintypes.error(1400, "MoveWindow", "无效的窗口句柄。")
            client.callback = callback
            client.listen[nickname] = (chat, callback)
            return WxResponse.success("ok")

        client.AddListenChat = add
        runtime = WeChatUIRuntime(lambda *_args: None, client_factory=lambda _version: client)
        runtime.bootstrap({"listeners": []})

        with patch("core.wechat_ui_runtime.log") as recovery_log:
            identity = runtime.add_listen({"conversation": "张三", "chat_type": "private"})

        self.assertEqual(
            identity,
            {"name": "张三", "chat_type": "private", "registration_changed": True},
        )
        self.assertEqual(add_calls, ["张三", "张三"])
        self.assertEqual(resize_calls, [True])
        self.assertIsNotNone(client.callback)
        recovery_log.assert_called_once()
        self.assertEqual(recovery_log.call_args.kwargs["level"], "SUCCESS")
        self.assertIn(
            "自恢复【局部找回】成功",
            recovery_log.call_args.kwargs["message"],
        )

    def test_add_chat_reuses_registration_completed_before_move_window_1400(self):
        client = FakeClient()
        chat = FakeChat("张三")
        resize_calls = []
        chat._api = SimpleNamespace(
            HWND=101,
            auto_resize=lambda: resize_calls.append(True),
        )
        add_calls = []

        def add(nickname, callback):
            add_calls.append(nickname)
            client.chats[nickname] = chat
            client.listen[nickname] = (chat, callback)
            raise OSError(1400, "MoveWindow", "无效的窗口句柄。")

        client.AddListenChat = add
        runtime = WeChatUIRuntime(lambda *_args: None, client_factory=lambda _version: client)
        runtime.bootstrap({"listeners": []})

        identity = runtime.add_listen({"conversation": "张三", "chat_type": "private"})

        self.assertEqual(
            identity,
            {"name": "张三", "chat_type": "private", "registration_changed": True},
        )
        self.assertEqual(add_calls, ["张三"])
        self.assertEqual(resize_calls, [True])

    def test_add_chat_retries_discovery_before_one_replacement_add(self):
        client = FakeClient()
        add_calls = []
        discovery_calls = []

        def get_all_subwindows():
            discovery_calls.append(True)
            return list(client.chats.values())

        def add(nickname, callback):
            add_calls.append(nickname)
            if len(add_calls) == 1:
                raise OSError(1400, "MoveWindow", "无效的窗口句柄。")
            client.callback = callback
            chat = FakeChat(nickname)
            client.chats[nickname] = chat
            client.listen[nickname] = (chat, callback)
            return WxResponse.success("ok")

        client.GetAllSubWindow = get_all_subwindows
        client.AddListenChat = add
        runtime = WeChatUIRuntime(lambda *_args: None, client_factory=lambda _version: client)
        runtime.bootstrap({"listeners": []})

        with patch("core.wechat_ui_runtime.time.sleep") as sleep, patch(
            "core.wechat_ui_runtime.log"
        ) as recovery_log:
            identity = runtime.add_listen({"conversation": "张三", "chat_type": "private"})

        self.assertEqual(
            identity,
            {"name": "张三", "chat_type": "private", "registration_changed": True},
        )
        self.assertEqual(add_calls, ["张三", "张三"])
        self.assertEqual(len(discovery_calls), 3)
        sleep.assert_called_once()
        recovery_log.assert_not_called()

    def test_add_chat_stops_after_second_add_failure(self):
        client = FakeClient()
        add_calls = []

        def add(nickname, callback):
            add_calls.append(nickname)
            if len(add_calls) == 1:
                raise OSError(1400, "MoveWindow", "无效的窗口句柄。")
            raise RuntimeError("replacement add failed")

        client.AddListenChat = add
        runtime = WeChatUIRuntime(lambda *_args: None, client_factory=lambda _version: client)
        runtime.bootstrap({"listeners": []})

        with patch("core.wechat_ui_runtime.time.sleep"):
            with self.assertRaisesRegex(RuntimeError, "replacement add failed"):
                runtime.add_listen({"conversation": "张三", "chat_type": "private"})

        self.assertEqual(add_calls, ["张三", "张三"])

    def test_add_chat_rejects_failed_response_even_when_subwindow_exists(self):
        client = FakeClient()
        chat = FakeChat("张三")
        add_calls = []

        def add(nickname, callback):
            add_calls.append(nickname)
            client.chats[nickname] = chat
            if len(add_calls) == 1:
                raise OSError(1400, "MoveWindow", "无效的窗口句柄。")
            return WxResponse.failure("callback registration failed")

        client.AddListenChat = add
        runtime = WeChatUIRuntime(lambda *_args: None, client_factory=lambda _version: client)
        runtime.bootstrap({"listeners": []})

        with self.assertRaises(MoveWindowListenRecoveryExhausted):
            runtime.add_listen({"conversation": "张三", "chat_type": "private"})

        self.assertEqual(add_calls, ["张三", "张三"])
        self.assertEqual(client.listen, {})

    def test_add_chat_rejects_success_response_without_callback_registration(self):
        client = FakeClient()
        chat = FakeChat("张三")
        add_calls = []

        def add(nickname, callback):
            add_calls.append(nickname)
            client.chats[nickname] = chat
            if len(add_calls) == 1:
                raise OSError(1400, "MoveWindow", "无效的窗口句柄。")
            return WxResponse.success("ok")

        client.AddListenChat = add
        runtime = WeChatUIRuntime(lambda *_args: None, client_factory=lambda _version: client)
        runtime.bootstrap({"listeners": []})

        with self.assertRaises(ListenSubwindowNotReady):
            runtime.add_listen({"conversation": "张三", "chat_type": "private"})

        self.assertEqual(add_calls, ["张三", "张三"])
        self.assertEqual(client.listen, {})

    def test_add_chat_propagates_client_disconnect_during_recovery(self):
        class ClientDisconnected(RuntimeError):
            hresult = -2147023174

        client = FakeClient()

        def add(nickname, callback):
            raise OSError(1400, "MoveWindow", "无效的窗口句柄。")

        client.AddListenChat = add
        client.GetAllSubWindow = lambda: (_ for _ in ()).throw(
            ClientDisconnected("RPC server is unavailable")
        )
        runtime = WeChatUIRuntime(lambda *_args: None, client_factory=lambda _version: client)
        runtime.bootstrap({"listeners": []})

        with self.assertRaises(ClientDisconnected):
            runtime.add_listen({"conversation": "张三", "chat_type": "private"})

    def test_send_text_does_not_recover_other_window_errors(self):
        client = FakeClient()
        add_calls = []

        def add(nickname, callback):
            add_calls.append(nickname)
            raise OSError(1400, "SetWindowPos", "无效的窗口句柄。")

        client.AddListenChat = add
        runtime = WeChatUIRuntime(lambda *_args: None, client_factory=lambda _version: client)
        runtime.bootstrap({"listeners": []})

        with self.assertRaises(OSError) as raised:
            runtime.send_text({
                "conversation": "张三",
                "chat_type": "private",
                "text": "不会发送",
                "_exclusive_retry": True,
            })

        self.assertEqual(raised.exception.args[0:2], (1400, "SetWindowPos"))
        self.assertEqual(add_calls, ["张三"])

    def test_add_chat_recovers_matching_type_from_same_name_subwindows(self):
        client = FakeClient()
        private = FakeChat("同名会话")
        group = FakeChat("同名会话")
        group.chat_type = "group"
        resized = []
        private._api = SimpleNamespace(HWND=201, auto_resize=lambda: resized.append("private"))
        group._api = SimpleNamespace(HWND=202, auto_resize=lambda: resized.append("group"))
        add_calls = []
        client.GetAllSubWindow = lambda: [group, private]

        def add(nickname, callback):
            add_calls.append(nickname)
            if len(add_calls) == 1:
                raise OSError(1400, "MoveWindow", "无效的窗口句柄。")
            client.callback = callback
            client.listen[nickname] = (private, callback)
            return WxResponse.success("ok")

        client.AddListenChat = add
        runtime = WeChatUIRuntime(lambda *_args: None, client_factory=lambda _version: client)
        runtime.bootstrap({"listeners": []})

        identity = runtime.add_listen({"conversation": "同名会话", "chat_type": "private"})

        self.assertEqual(identity["chat_type"], "private")
        self.assertEqual(add_calls, ["同名会话", "同名会话"])
        self.assertEqual(resized, ["private"])

    def test_send_text_reports_not_started_when_subwindow_recovery_fails(self):
        client = FakeClient()
        send_calls = []
        client.GetAllSubWindow = lambda: []

        def add(nickname, callback):
            if not send_calls:
                send_calls.append("first add")
                raise OSError(1400, "MoveWindow", "无效的窗口句柄。")
            send_calls.append("second add")
            raise OSError(1400, "MoveWindow", "无效的窗口句柄。")

        client.AddListenChat = add
        exhausted = []
        runtime = WeChatUIRuntime(
            lambda *_args: None,
            client_factory=lambda _version: client,
            listener_recovery_exhausted=exhausted.append,
        )
        runtime.bootstrap({"listeners": []})

        with patch("core.wechat_ui_runtime.time.sleep"):
            with self.assertRaises(UIOutboundNotStarted):
                runtime.send_text({
                    "conversation": "张三",
                    "chat_type": "private",
                    "text": "不会发送",
                    "_exclusive_retry": True,
                })

        self.assertEqual(send_calls, ["first add", "second add"])
        self.assertEqual(exhausted, [ConversationRef("张三", "private")])

    def test_add_chat_without_type_discovers_group_identity(self):
        client = FakeClient()

        def add(nickname, callback):
            client.callback = callback
            chat = FakeChat(nickname)
            chat.chat_type = "group"
            client.chats[nickname] = chat
            client.listen[nickname] = (chat, callback)
            return WxResponse.success("ok")

        client.AddListenChat = add
        runtime = WeChatUIRuntime(lambda *_args: None, client_factory=lambda _version: client)
        runtime.bootstrap({"listeners": []})

        identity = runtime.add_listen({"conversation": "【姐姐】素材库"})

        self.assertEqual(
            identity,
            {
                "name": "【姐姐】素材库",
                "chat_type": "group",
                "registration_changed": True,
            },
        )

    def test_find_chat_uses_valid_cache_before_enumerating_subwindows(self):
        client = FakeClient()
        runtime = WeChatUIRuntime(lambda *_args: None, client_factory=lambda _version: client)
        runtime.bootstrap({"listeners": [{"name": "张三", "chat_type": "private"}]})
        client.GetAllSubWindow = lambda: self.fail("有效缓存不应重复枚举全部子窗口")

        result = runtime.send_text({
            "conversation": "张三",
            "chat_type": "private",
            "text": "你好",
        })

        self.assertTrue(result)

    def test_stop_listening_invalidates_runtime_window_registrations(self):
        client = FakeClient()
        add_calls = []
        original_add = client.AddListenChat

        def add(nickname, callback):
            add_calls.append(nickname)
            return original_add(nickname, callback)

        client.AddListenChat = add
        runtime = WeChatUIRuntime(lambda *_args: None, client_factory=lambda _version: client)
        runtime.bootstrap({"listeners": []})
        runtime.add_listen({"conversation": "张三", "chat_type": "private"})

        runtime.main_window({"operation": "stop_listening"})
        runtime.main_window({"operation": "start_listening"})
        runtime.add_listen({"conversation": "张三", "chat_type": "private"})

        self.assertEqual(add_calls, ["张三", "张三"])

    def test_contact_edit_verifies_target_and_returns_pure_result(self):
        client = FakeClient()
        runtime = WeChatUIRuntime(lambda *_args: None, client_factory=lambda _version: client)
        runtime.bootstrap({"listeners": []})

        result = runtime.edit_contact({
            "target": "张三",
            "expected_names": ["张三"],
            "remark": "客户-张三",
            "add_tags": ["客户"],
            "remove_tags": [],
        })

        self.assertEqual(result["status"], "成功")
        self.assertEqual(client.edit_kwargs["remark"], "客户-张三")

    def test_contact_edit_waits_until_target_friend_page_is_ready(self):
        class DelayedClient(FakeClient):
            def __init__(self):
                super().__init__()
                self.chat_info_calls = 0

            def ChatInfo(self):
                self.chat_info_calls += 1
                if self.chat_info_calls < 3:
                    return {"chat_type": "friend", "chat_name": "旧会话"}
                return {"chat_type": "friend", "chat_name": self.current_chat}

        client = DelayedClient()
        runtime = WeChatUIRuntime(lambda *_args: None, client_factory=lambda _version: client)
        runtime.bootstrap({"listeners": []})

        with patch("core.wechat_ui_runtime.time.sleep", return_value=None):
            result = runtime.edit_contact({
                "target": "张三",
                "expected_names": ["张三"],
                "add_tags": ["客户"],
                "remove_tags": [],
            })

        self.assertEqual(result["status"], "成功")
        self.assertEqual(client.chat_info_calls, 3)
        self.assertEqual(client.edit_kwargs["add_tags"], ["客户"])

    def test_contact_edit_falls_back_to_wechat_id_when_name_search_does_not_switch(self):
        class WechatIdClient(FakeClient):
            def __init__(self):
                super().__init__()
                self.current_chat = "旧会话"
                self.searches = []

            def ChatWith(self, who=None, exact=True, **_kwargs):
                self.searches.append(who)
                if who == "wxid_zhangsan":
                    self.current_chat = "张三"
                return True

        client = WechatIdClient()
        runtime = WeChatUIRuntime(lambda *_args: None, client_factory=lambda _version: client)
        runtime.bootstrap({"listeners": []})

        with patch("core.wechat_ui_runtime.time.sleep", return_value=None):
            result = runtime.edit_contact({
                "target": "张三",
                "contact_key": "wechat_id:wxid_zhangsan",
                "expected_names": ["张三"],
                "add_tags": ["客户"],
                "remove_tags": [],
            })

        self.assertEqual(result["status"], "成功")
        self.assertEqual(client.searches, ["张三", "wxid_zhangsan"])

    def test_contact_edit_never_edits_when_target_friend_page_cannot_be_confirmed(self):
        class MissingTargetClient(FakeClient):
            def ChatWith(self, who=None, exact=True, **_kwargs):
                return True

            def ChatInfo(self):
                return {}

            def EditFriendInfo(self, **kwargs):
                raise AssertionError("目标好友页未确认时不得修改标签")

        client = MissingTargetClient()
        runtime = WeChatUIRuntime(lambda *_args: None, client_factory=lambda _version: client)
        runtime.bootstrap({"listeners": []})

        with patch("core.wechat_ui_runtime.time.sleep", return_value=None):
            with self.assertRaisesRegex(RuntimeError, "未能打开并确认目标好友页面"):
                runtime.edit_contact({
                    "target": "张三",
                    "expected_names": ["张三"],
                    "add_tags": ["客户"],
                    "remove_tags": [],
                })

    def test_contact_edit_preserves_legacy_window_focus_sequence_inside_owner(self):
        events = []

        class TrackingClient(FakeClient):
            def ChatWith(self, who=None, exact=True):
                events.append(("chat_with", who, exact))
                return super().ChatWith(who=who, exact=exact)

            def ChatInfo(self):
                events.append(("chat_info",))
                return super().ChatInfo()

            def EditFriendInfo(self, **kwargs):
                events.append(("edit", kwargs.get("add_tags")))
                return super().EditFriendInfo(**kwargs)

        client = TrackingClient()
        runtime = WeChatUIRuntime(lambda *_args: None, client_factory=lambda _version: client)
        runtime.bootstrap({"listeners": []})

        with (
            patch(
                "core.wechat_window.bring_wechat_main_window_to_front",
                side_effect=lambda **_kwargs: events.append(("front",)),
            ),
            patch(
                "core.wechat_window.move_cursor_to_wechat_main_window_center",
                side_effect=lambda **_kwargs: events.append(("cursor",)),
            ),
        ):
            runtime.edit_contact({
                "target": "张三",
                "expected_names": ["张三"],
                "add_tags": ["客户"],
                "remove_tags": [],
            })

        self.assertEqual(events, [
            ("front",),
            ("chat_with", "张三", True),
            ("front",),
            ("cursor",),
            ("chat_info",),
            ("front",),
            ("cursor",),
            ("edit", ["客户"]),
        ])

    def test_relationship_scan_returns_normalized_pure_sessions(self):
        client = FakeClient()
        runtime = WeChatUIRuntime(lambda *_args: None, client_factory=lambda _version: client)
        runtime.bootstrap({"listeners": []})

        sessions = runtime.scan_relationship_sessions({"mode": "current"})

        self.assertEqual(sessions, [{"name": "张三", "content": "你好", "time": "10:30", "info": ""}])

    def test_relationship_full_scan_is_one_uninterrupted_transaction_with_safety_cap(self):
        client = FakeClient()
        rolls = []
        client.SessionBox = SimpleNamespace(roll_down=lambda: rolls.append(True), go_top=lambda: None)
        runtime = WeChatUIRuntime(lambda *_args: None, client_factory=lambda _version: client)
        runtime.bootstrap({"listeners": []})

        with patch("core.wechat_ui_runtime.time.sleep", return_value=None) as sleep:
            result = runtime.scan_relationship_sessions({"mode": "full", "max_scrolls": 20, "stale_rounds": 999})

        self.assertEqual(result["scrolls"], 20)
        self.assertEqual(len(rolls), 20)
        self.assertTrue(result["hit_safety_limit"])
        self.assertEqual(sleep.call_count, 2)

    def test_relationship_full_scan_returns_to_top_before_and_after_scan(self):
        client = FakeClient()
        calls = []
        client.SessionBox = SimpleNamespace(
            roll_down=lambda: calls.append("roll_down"),
            go_top=lambda: calls.append("go_top"),
        )
        runtime = WeChatUIRuntime(lambda *_args: None, client_factory=lambda _version: client)
        runtime.bootstrap({"listeners": []})

        with patch("core.wechat_ui_runtime.time.sleep", return_value=None):
            runtime.scan_relationship_sessions({"mode": "full", "max_scrolls": 1, "stale_rounds": 999})

        self.assertEqual(calls, ["go_top", "roll_down", "go_top"])

    def test_relationship_full_scan_keeps_result_when_final_go_top_fails(self):
        client = FakeClient()
        go_top_calls = 0

        def go_top():
            nonlocal go_top_calls
            go_top_calls += 1
            if go_top_calls == 2:
                raise RuntimeError("top failed")

        client.SessionBox = SimpleNamespace(roll_down=lambda: None, go_top=go_top)
        runtime = WeChatUIRuntime(lambda *_args: None, client_factory=lambda _version: client)
        runtime.bootstrap({"listeners": []})

        with patch("core.wechat_ui_runtime.time.sleep", return_value=None):
            result = runtime.scan_relationship_sessions({"mode": "full", "max_scrolls": 1, "stale_rounds": 999})

        self.assertEqual(result["sessions"][0]["name"], "张三")
        self.assertEqual(result["scrolls"], 1)

    def test_friend_request_runs_whole_ui_transaction_inside_runtime(self):
        client = FakeClient()
        runtime = WeChatUIRuntime(lambda *_args: None, client_factory=lambda _version: client)
        runtime.bootstrap({"listeners": []})

        with patch("feature.friend_request_senders.ConversationVerifySender.send", return_value={
            "status": "sent",
            "message": "好友验证申请已提交",
            "data": {"target": "张三"},
        }) as send:
            result = runtime.send_friend_request({
                "target": "张三",
                "addmsg": "你好",
                "remark": "张三",
                "tags": ["客户"],
                "max_attempts": 2,
            })

        self.assertEqual(result["status"], "sent")
        self.assertIs(send.call_args.args[0].wx, client)

    def test_friend_request_history_read_suppresses_same_thread_old_callbacks(self):
        received = []
        client = FakeClient()
        chat = client.chats.setdefault("张三", FakeChat("张三"))
        runtime = WeChatUIRuntime(
            lambda conversation, message: received.append((conversation, message)),
            client_factory=lambda _version: client,
        )
        runtime.bootstrap({"listeners": [{"name": "张三"}]})

        def send(*_args, **_kwargs):
            client.callback(
                SimpleNamespace(id="old-1", type="text", attr="friend", sender="张三", content="旧历史"),
                chat,
            )
            return {"status": "skipped", "message": "未找到发送朋友验证入口", "data": {}}

        with patch("feature.friend_request_senders.ConversationVerifySender.send", side_effect=send):
            runtime.send_friend_request({"target": "张三"})

        self.assertEqual(received, [])

    def test_material_history_is_copied_before_leaving_runtime(self):
        client = FakeClient()
        chat = FakeChat("素材源")
        chat.messages = [SimpleNamespace(
            id="file-1",
            hash="hash-1",
            type="file",
            attr="friend",
            sender="素材源",
            content="C:/docs/a.pdf",
            forward=lambda *_args, **_kwargs: True,
        )]
        client.chats["素材源"] = chat
        reported = []
        runtime = WeChatUIRuntime(
            lambda *_args: None,
            client_factory=lambda _version: client,
            listener_operation_succeeded=reported.append,
        )
        runtime.bootstrap({"listeners": [{"name": "素材源"}]})
        reported.clear()

        result = runtime.read_material_messages({"conversation": "素材源", "chat_type": "private", "limit": 20})

        self.assertEqual(result["strategy"], "子窗口公开 GetHistoryMessage")
        self.assertIsInstance(result["messages"][0], MessageEnvelope)
        self.assertFalse(hasattr(result["messages"][0], "forward"))
        self.assertEqual(reported, [ConversationRef("素材源", "private")])

    def test_material_history_read_suppresses_same_thread_old_callbacks(self):
        received = []
        client = FakeClient()
        chat = FakeChat("素材源")
        material = SimpleNamespace(
            id="file-1", hash="hash-1", type="file", attr="friend",
            sender="素材源", content="C:/docs/a.pdf",
        )

        def read_history(*_args, **_kwargs):
            client.callback(
                SimpleNamespace(id="old-1", type="text", attr="friend", sender="素材源", content="旧历史"),
                chat,
            )
            return [material]

        chat.GetHistoryMessage = read_history
        client.chats["素材源"] = chat
        runtime = WeChatUIRuntime(
            lambda conversation, message: received.append((conversation, message)),
            client_factory=lambda _version: client,
        )
        runtime.bootstrap({"listeners": [{"name": "素材源"}]})

        result = runtime.read_material_messages({"conversation": "素材源", "chat_type": "private", "limit": 20})

        self.assertEqual(result["messages"][0].content, "C:/docs/a.pdf")
        self.assertEqual(received, [])

    def test_material_history_preserves_legacy_reader_fallback_order(self):
        events = []
        material = SimpleNamespace(
            id="file-1", hash="hash-1", type="file", attr="friend",
            sender="素材源", content="C:/docs/a.pdf",
        )

        class BrokenChatBox:
            def get_msgs_from_history(self, *_args, **_kwargs):
                events.append("internal")
                raise RuntimeError("内部读取失效")

        class TrackingClient(FakeClient):
            def ChatWith(self, who=None, exact=True):
                events.append("main_chat_with")
                return super().ChatWith(who=who, exact=exact)

        client = TrackingClient()
        chat = FakeChat("素材源")
        chat.ChatBox = BrokenChatBox()

        def public_history(*_args, **_kwargs):
            events.append("public")
            return [material]

        chat.GetHistoryMessage = public_history
        client.chats["素材源"] = chat
        runtime = WeChatUIRuntime(lambda *_args: None, client_factory=lambda _version: client)
        runtime.bootstrap({"listeners": [{"name": "素材源"}]})

        result = runtime.read_material_messages({"conversation": "素材源", "chat_type": "private", "limit": 20})

        self.assertEqual(result["strategy"], "子窗口公开 GetHistoryMessage")
        self.assertEqual(result["messages"][0].content, "C:/docs/a.pdf")
        self.assertEqual(events, ["internal", "public"])

    def test_material_history_uses_main_window_only_after_subwindow_readers_fail(self):
        events = []
        material = SimpleNamespace(
            id="file-1", hash="hash-1", type="file", attr="friend",
            sender="素材源", content="C:/docs/main.pdf",
        )

        class BrokenChatBox:
            def get_msgs_from_history(self, *_args, **_kwargs):
                events.append("internal")
                raise RuntimeError("内部读取失效")

        class TrackingClient(FakeClient):
            def ChatWith(self, who=None, exact=True):
                events.append("main_chat_with")
                return super().ChatWith(who=who, exact=exact)

            def GetHistoryMessage(self, *_args, **_kwargs):
                events.append("main_history")
                return [material]

        client = TrackingClient()
        chat = FakeChat("素材源")
        chat.ChatBox = BrokenChatBox()

        def broken_public(*_args, **_kwargs):
            events.append("public")
            raise RuntimeError("公开读取失效")

        chat.GetHistoryMessage = broken_public
        client.chats["素材源"] = chat
        reported = []
        runtime = WeChatUIRuntime(
            lambda *_args: None,
            client_factory=lambda _version: client,
            listener_operation_succeeded=reported.append,
        )
        runtime.bootstrap({"listeners": [{"name": "素材源"}]})
        reported.clear()

        result = runtime.read_material_messages({"conversation": "素材源", "chat_type": "private", "limit": 20})

        self.assertEqual(result["strategy"], "主窗口公开 GetHistoryMessage")
        self.assertEqual(result["messages"][0].content, "C:/docs/main.pdf")
        self.assertEqual(events, ["internal", "public", "main_chat_with", "main_history"])
        self.assertEqual(reported, [])

    def test_forward_rolls_relocated_message_into_view_before_forwarding(self):
        events = []
        client = FakeClient()
        chat = FakeChat("素材源")
        message = SimpleNamespace(
            id="file-1", hash="hash-1", type="file", attr="friend",
            sender="素材源", content="C:/docs/a.pdf",
            roll_into_view=lambda: events.append("roll"),
            forward=lambda targets: events.append(("forward", targets)) or {"status": "成功"},
        )
        chat.messages = [message]
        client.chats["素材源"] = chat
        reported = []
        runtime = WeChatUIRuntime(
            lambda *_args: None,
            client_factory=lambda _version: client,
            listener_operation_succeeded=reported.append,
        )
        runtime.bootstrap({"listeners": [{"name": "素材源"}]})
        reported.clear()

        runtime.forward_message({
            "conversation": "素材源",
            "chat_type": "private",
            "message_type": "file",
            "message_attr": "friend",
            "message_sender": "素材源",
            "message_content": "C:/docs/a.pdf",
            "message_id": "file-1",
            "targets": ["阿英2"],
        })

        self.assertEqual(events, ["roll", ("forward", ["阿英2"])])
        self.assertEqual(reported, [ConversationRef("素材源", "private")])

    def test_message_locator_rejects_ambiguous_duplicates_without_known_order(self):
        client = FakeClient()
        chat = FakeChat("张三")
        chat.messages = [
            SimpleNamespace(type="text", attr="friend", sender="张三", content="相同", hash="same"),
            SimpleNamespace(type="text", attr="friend", sender="张三", content="相同", hash="same"),
        ]
        client.chats["张三"] = chat
        runtime = WeChatUIRuntime(lambda *_args: None, client_factory=lambda _version: client)
        runtime.bootstrap({"listeners": [{"name": "张三"}]})

        with self.assertRaisesRegex(RuntimeError, "拒绝猜测"):
            runtime._locate_message({
                "conversation": "张三",
                "chat_type": "private",
                "message_type": "text",
                "message_attr": "friend",
                "message_sender": "张三",
                "message_content": "相同",
                "message_hash": "same",
            })

    def test_message_locator_uses_exact_snapshot_order_for_duplicates(self):
        client = FakeClient()
        chat = FakeChat("张三")
        first = SimpleNamespace(type="text", attr="friend", sender="张三", content="相同", hash="same")
        second = SimpleNamespace(type="text", attr="friend", sender="张三", content="相同", hash="same")
        chat.messages = [first, second]
        client.chats["张三"] = chat
        runtime = WeChatUIRuntime(lambda *_args: None, client_factory=lambda _version: client)
        runtime.bootstrap({"listeners": [{"name": "张三"}]})

        located = runtime._locate_message({
            "conversation": "张三",
            "chat_type": "private",
            "message_type": "text",
            "message_attr": "friend",
            "message_sender": "张三",
            "message_content": "相同",
            "message_hash": "same",
            "message_window_order_known": True,
            "message_window_order": 1,
        })

        self.assertIs(located, second)

    def test_message_locator_uses_message_id_for_duplicate_images(self):
        client = FakeClient()
        chat = FakeChat("瑞东（私人号）")
        first = SimpleNamespace(
            id="image-1", hash="same", type="image", attr="friend",
            sender="瑞东（私人号）", content="图片", download=lambda: "C:/temp/first.png",
        )
        second = SimpleNamespace(
            id="image-2", hash="same", type="image", attr="friend",
            sender="瑞东（私人号）", content="图片", download=lambda: "C:/temp/second.png",
        )
        chat.messages = [first, second]
        client.chats["瑞东（私人号）"] = chat
        runtime = WeChatUIRuntime(lambda *_args: None, client_factory=lambda _version: client)
        runtime.bootstrap({"listeners": [{"name": "瑞东（私人号）"}]})

        result = runtime.download_media({
            "conversation": "瑞东（私人号）",
            "chat_type": "private",
            "message_type": "image",
            "message_attr": "friend",
            "message_sender": "瑞东（私人号）",
            "message_content": "图片",
            "message_id": "image-2",
            "message_hash": "same",
        })

        self.assertEqual(result, "C:/temp/second.png")

    def test_message_locator_uses_native_hash_text_for_duplicate_images(self):
        client = FakeClient()
        chat = FakeChat("瑞东（私人号）")
        first = SimpleNamespace(
            id="", hash="", hash_text="image-row-1", type="image", attr="friend",
            sender="瑞东（私人号）", content="图片", download=lambda: "C:/temp/first.png",
        )
        second = SimpleNamespace(
            id="", hash="", hash_text="image-row-2", type="image", attr="friend",
            sender="瑞东（私人号）", content="图片", download=lambda: "C:/temp/second.png",
        )
        chat.messages = [first, second]
        client.chats["瑞东（私人号）"] = chat
        runtime = WeChatUIRuntime(lambda *_args: None, client_factory=lambda _version: client)
        runtime.bootstrap({"listeners": [{"name": "瑞东（私人号）"}]})

        result = runtime.download_media({
            "conversation": "瑞东（私人号）",
            "chat_type": "private",
            "message_type": "image",
            "message_attr": "friend",
            "message_sender": "瑞东（私人号）",
            "message_content": "图片",
            "message_hash_text": "image-row-2",
        })

        self.assertEqual(result, "C:/temp/second.png")

    def test_message_locator_reads_one_bounded_recent_history_on_visible_miss(self):
        client = FakeClient()
        chat = FakeChat("张三")
        target = SimpleNamespace(type="image", attr="friend", sender="张三", content="[图片]", hash="history-1")
        chat.history_messages = [target]
        client.chats["张三"] = chat
        runtime = WeChatUIRuntime(lambda *_args: None, client_factory=lambda _version: client)
        runtime.bootstrap({"listeners": [{"name": "张三"}]})

        located = runtime._locate_message({
            "conversation": "张三",
            "chat_type": "private",
            "message_type": "image",
            "message_attr": "friend",
            "message_sender": "张三",
            "message_content": "[图片]",
            "message_hash": "history-1",
        })

        self.assertIs(located, target)
        self.assertEqual(chat.history_calls, [50])

    def test_realtime_media_locator_does_not_scroll_history_on_visible_miss(self):
        client = FakeClient()
        chat = FakeChat("张三")
        chat.history_messages = [SimpleNamespace(
            id="old-image", type="image", attr="friend", sender="张三", content="图片", hash="old",
        )]
        client.chats["张三"] = chat
        runtime = WeChatUIRuntime(lambda *_args: None, client_factory=lambda _version: client)
        runtime.bootstrap({"listeners": [{"name": "张三"}]})

        with self.assertRaisesRegex(RuntimeError, "停止历史翻页定位"):
            runtime._locate_message({
                "conversation": "张三",
                "chat_type": "private",
                "message_type": "image",
                "message_attr": "friend",
                "message_sender": "张三",
                "message_content": "图片",
                "message_id": "new-image",
                "allow_history_fallback": False,
            })

        self.assertEqual(chat.history_calls, [])

    def test_new_friend_accept_reloads_and_uniquely_locates_candidate(self):
        calls = []

        class Candidate:
            name = "阿英2"
            content = "我是阿英"
            acceptable = True

            def __init__(self, generation):
                self.generation = generation

            def accept(self, **kwargs):
                calls.append((self.generation, kwargs))

        client = FakeClient()
        generation = 0

        def get_new_friends(acceptable=True):
            nonlocal generation
            generation += 1
            return [Candidate(generation)]

        client.GetNewFriends = get_new_friends
        runtime = WeChatUIRuntime(lambda *_args: None, client_factory=lambda _version: client)
        runtime.bootstrap({"listeners": []})

        result = runtime.process_new_friends({"remark_rules": {"enabled": True}, "tags": []})

        self.assertEqual(len(result), 1)
        self.assertEqual(calls[0][0], 2)

    def test_new_friend_accept_refuses_indistinguishable_duplicates(self):
        calls = []

        class Candidate:
            name = "同名"
            content = "你好"
            acceptable = True

            def accept(self, **_kwargs):
                calls.append(True)

        client = FakeClient()
        client.GetNewFriends = lambda acceptable=True: [Candidate(), Candidate()]
        runtime = WeChatUIRuntime(lambda *_args: None, client_factory=lambda _version: client)
        runtime.bootstrap({"listeners": []})

        result = runtime.process_new_friends({"remark_rules": {"enabled": True}, "tags": []})

        self.assertEqual(result, [])
        self.assertEqual(calls, [])

    def test_new_friend_owner_action_accepts_only_one_candidate_per_run(self):
        calls = []

        class Candidate:
            content = "你好"
            acceptable = True

            def __init__(self, name):
                self.name = name

            def accept(self, **_kwargs):
                calls.append(self.name)

        client = FakeClient()
        client.GetNewFriends = lambda acceptable=True: [Candidate("阿英2"), Candidate("阿英3")]
        runtime = WeChatUIRuntime(lambda *_args: None, client_factory=lambda _version: client)
        runtime.bootstrap({"listeners": []})

        result = runtime.process_new_friends({"remark_rules": {"enabled": False}, "tags": []})

        self.assertEqual(calls, ["阿英2"])
        self.assertEqual([item["name"] for item in result], ["阿英2"])


if __name__ == "__main__":
    unittest.main()
