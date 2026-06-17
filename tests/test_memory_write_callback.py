import unittest
import tempfile
from types import SimpleNamespace
from unittest import mock

from core.memory import MemoryManager
from wxbot_core import WXBot


class CaptureMemory:
    def __init__(self):
        self.calls = []

    def save_message(self, **kwargs):
        self.calls.append(kwargs)


class MemoryWriteCallbackTests(unittest.TestCase):
    def test_message_callback_saves_memory_without_external_message_time(self):
        bot = WXBot.__new__(WXBot)
        bot.config = SimpleNamespace(
            memory_switch=True,
            memory_max_count=5000,
            group_welcome=False,
            group=[],
            cmd="admin",
        )
        bot.memory_manager = CaptureMemory()
        bot._should_skip_message_memory = lambda chat, msg: False
        bot.callback_is_die = False
        bot.wx = SimpleNamespace(nickname="bot")
        bot.is_err = lambda *args, **kwargs: self.fail(f"unexpected error: {args}")

        msg = SimpleNamespace(attr="system", sender="system", content="hello", type="text")
        chat = SimpleNamespace(who="chat-a", chat_type="private")

        bot.message_handle_callback(msg, chat)

        self.assertEqual(len(bot.memory_manager.calls), 1)
        self.assertEqual(bot.memory_manager.calls[0]["chat_name"], "chat-a")
        self.assertNotIn("message_time", bot.memory_manager.calls[0])

    def test_friend_message_callback_marks_conversation_memory_dirty_without_sync_update(self):
        bot = WXBot.__new__(WXBot)
        bot.config = SimpleNamespace(
            memory_switch=True,
            memory_max_count=5000,
            group_welcome=False,
            group=[],
            cmd="admin",
        )
        bot.memory_manager = CaptureMemory()
        bot._should_skip_message_memory = lambda chat, msg: False
        bot._handle_admin_forward_input = lambda _chat, _msg: False
        bot._handle_admin_moments_input = lambda _chat, _msg: False
        bot._handle_material_source_message = lambda _chat, _msg: False
        bot._record_received_message = lambda: None
        bot._update_alllisten_timestamp = lambda *_args, **_kwargs: None
        bot.process_message = lambda _chat, _msg: True
        bot.callback_is_die = False
        bot.wx = SimpleNamespace(nickname="bot")
        bot.is_err = lambda *args, **kwargs: self.fail(f"unexpected error: {args}")

        dirty_calls = []
        bot._mark_conversation_memory_dirty = lambda chat, msg: dirty_calls.append((chat.who, msg.content)) or True
        bot._maybe_update_conversation_memory = lambda _chat, _msg: self.fail("不应在消息回调里同步维护会话记忆")

        msg = SimpleNamespace(attr="friend", sender="friend-a", content="hello", type="text")
        chat = SimpleNamespace(who="chat-a", chat_type="private")

        bot.message_handle_callback(msg, chat)

        self.assertEqual(len(bot.memory_manager.calls), 1)
        self.assertEqual(dirty_calls, [("chat-a", "hello")])

    def test_group_voice_text_without_at_is_saved_but_not_replied(self):
        bot = WXBot.__new__(WXBot)
        bot.config = SimpleNamespace(
            memory_switch=True,
            memory_max_count=5000,
            AllListen_switch=False,
            listen_list=[],
            global_blacklist=[],
            group=["测试群"],
            group_switch=True,
            group_reply_at=True,
            group_listen_only=False,
            group_image_recognition_switch=False,
            group_voice_recognition_switch=True,
            group_keyword_switch=False,
            group_keyword_at_only=False,
            keyword_dict={},
            AtMe="@机器人",
            cmd="admin",
        )
        bot.memory_manager = CaptureMemory()
        bot._should_skip_message_memory = lambda chat, msg: False
        bot._handle_admin_forward_input = lambda _chat, _msg: False
        bot._handle_admin_moments_input = lambda _chat, _msg: False
        bot._handle_material_source_message = lambda _chat, _msg: False
        bot._record_received_message = lambda: None
        bot._pause_group_reply = False
        bot.callback_is_die = False
        bot.wx = SimpleNamespace(nickname="bot")
        bot.is_err = lambda *args, **kwargs: self.fail(f"unexpected error: {args}")

        bot._get_group_api = lambda _group: self.fail("没 @ 的群聊语音转文字不应触发 AI 回复")

        msg = SimpleNamespace(
            attr="group",
            sender="B",
            content='语音2"秒B 的语音内容',
            type="voice",
            to_text=lambda: self.fail("不应主动调用微信右键语音转文字"),
        )
        chat = SimpleNamespace(who="测试群", chat_type="group")

        bot.message_handle_callback(msg, chat)

        self.assertEqual(len(bot.memory_manager.calls), 1)
        self.assertEqual(bot.memory_manager.calls[0]["chat_name"], "测试群")
        self.assertEqual(bot.memory_manager.calls[0]["sender"], "B")
        self.assertEqual(bot.memory_manager.calls[0]["content"], '语音2"秒B 的语音内容')
        self.assertEqual(bot.memory_manager.calls[0]["msg_type"], "voice")

    def test_group_image_without_at_is_saved_but_not_replied(self):
        bot = WXBot.__new__(WXBot)
        bot.config = SimpleNamespace(
            memory_switch=True,
            memory_max_count=5000,
            AllListen_switch=False,
            listen_list=[],
            global_blacklist=[],
            group=["测试群"],
            group_switch=True,
            group_reply_at=True,
            group_listen_only=False,
            group_image_recognition_switch=True,
            group_voice_recognition_switch=False,
            group_keyword_switch=False,
            group_keyword_at_only=False,
            keyword_dict={},
            AtMe="@机器人",
            cmd="admin",
        )
        bot.memory_manager = CaptureMemory()
        bot._should_skip_message_memory = lambda chat, msg: False
        bot._handle_admin_forward_input = lambda _chat, _msg: False
        bot._handle_admin_moments_input = lambda _chat, _msg: False
        bot._handle_material_source_message = lambda _chat, _msg: False
        bot._record_received_message = lambda: None
        bot._pause_group_reply = False
        bot.callback_is_die = False
        bot.wx = SimpleNamespace(nickname="bot")
        bot.is_err = lambda *args, **kwargs: self.fail(f"unexpected error: {args}")
        bot._get_group_api = lambda _group: self.fail("没 @ 的群聊图片不应触发 AI 回复")

        msg = SimpleNamespace(
            attr="group",
            sender="B",
            content="",
            type="image",
            download=lambda: r"C:\tmp\group-image.png",
        )
        chat = SimpleNamespace(who="测试群", chat_type="group")

        bot.message_handle_callback(msg, chat)

        self.assertEqual(len(bot.memory_manager.calls), 1)
        self.assertEqual(bot.memory_manager.calls[0]["chat_name"], "测试群")
        self.assertEqual(bot.memory_manager.calls[0]["sender"], "B")
        self.assertEqual(bot.memory_manager.calls[0]["content"], r"C:\tmp\group-image.png")
        self.assertEqual(bot.memory_manager.calls[0]["msg_type"], "image")

    def test_conversation_memory_background_worker_uses_existing_update_logic(self):
        bot = WXBot.__new__(WXBot)
        bot.run_flag = True
        bot._conversation_memory_dirty_lock = mock.MagicMock()
        bot._conversation_memory_dirty_chats = {"张三": 1.0}
        bot._conversation_memory_worker_running = True
        calls = []
        bot._maybe_update_conversation_memory = lambda chat, msg: calls.append((chat.who, msg.attr)) or None

        with mock.patch("wxbot_core.time.sleep", return_value=None):
            bot._conversation_memory_background_worker()

        self.assertEqual(calls, [("张三", "friend")])
        self.assertFalse(bot._conversation_memory_worker_running)

    def test_memory_manager_lists_existing_chat_record_names(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            manager = MemoryManager("wxid", tmpdir)
            manager.save_message("张三", "张三", "你好", "text", "friend", 5000)

            self.assertEqual(manager.list_chat_names(), ["张三"])

    def test_startup_memory_compensation_marks_existing_chats_only(self):
        bot = WXBot.__new__(WXBot)
        bot.config = SimpleNamespace(memory_switch=True, group=["群聊"])
        bot.memory_manager = SimpleNamespace(list_chat_names=lambda: ["张三", "群聊", ""])
        marked = []
        bot._mark_conversation_memory_dirty = lambda chat, msg: marked.append((chat.who, msg.attr)) or True

        with mock.patch("wxbot_core.log") as fake_log:
            count = bot._enqueue_existing_conversation_memory_checks()

        self.assertEqual(count, 1)
        self.assertEqual(marked, [("张三", "friend")])
        self.assertFalse(fake_log.called)

    def test_memory_update_logs_success_without_api_text(self):
        bot = WXBot.__new__(WXBot)
        bot.config = SimpleNamespace(
            memory_switch=True,
            memory_max_count=5000,
            group=[],
            cmd="admin",
        )
        bot.memory_manager = SimpleNamespace(get_messages=lambda *_args, **_kwargs: [{"content": "hello"}])
        bot._should_skip_message_memory = lambda chat, msg: False
        bot._init_prompt_system = lambda: SimpleNamespace(
            auto_memory_enabled_for=lambda *_args, **_kwargs: True,
            update_memory=lambda *_args, **_kwargs: True,
        )
        bot._get_other_api = lambda *_args, **_kwargs: object()
        bot._get_chat_api_index = lambda *_args, **_kwargs: 0

        logs = []
        with mock.patch("wxbot_core.log", side_effect=lambda **kwargs: logs.append((kwargs.get("level", "INFO"), kwargs.get("message", "")))):
            result = bot._maybe_update_conversation_memory(SimpleNamespace(who="B-岁月静好3", chat_type="private"), SimpleNamespace(attr="friend"))

        self.assertTrue(result)
        self.assertTrue(any(level == "INFO" and "会话记忆已更新：B-岁月静好3" == message for level, message in logs))
        self.assertFalse(any("API返回成功" in message for _level, message in logs))


if __name__ == "__main__":
    unittest.main()
