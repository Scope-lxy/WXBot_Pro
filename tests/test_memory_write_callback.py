import unittest
import tempfile
from collections import deque
from types import SimpleNamespace
from unittest import mock

from core.chat_history_format import build_model_visible_history, format_history_message, format_memory_record_for_display
from core.message_pipeline import (
    build_merged_private_message,
    format_message_semantic_text,
    format_model_message_text,
    strip_voice_duration_metadata,
)
from core.memory import MemoryManager
from core.sending import clean_ai_reply_text
from wxbot_core import WXBot


class CaptureMemory:
    def __init__(self):
        self.calls = []

    def save_message(self, **kwargs):
        self.calls.append(kwargs)


class InMemoryChatMemory:
    def __init__(self):
        self.messages = []

    def save_message(self, **kwargs):
        message_time = kwargs.get("message_time")
        if hasattr(message_time, "strftime"):
            message_time = message_time.strftime("%Y/%m/%d %H:%M:%S")
        elif not message_time:
            message_time = "2026/07/07 00:00:00"
        entry = {
            "time": str(message_time),
            "type": str(kwargs.get("msg_type", "text")),
            "attr": str(kwargs.get("msg_attr", "friend")),
            "sender": str(kwargs.get("sender", "")),
            "content": str(kwargs.get("content", "")),
        }
        key = tuple(entry.get(name, "") for name in ("time", "type", "attr", "sender", "content"))
        for item in self.messages[-20:]:
            existing_key = tuple(item.get(name, "") for name in ("time", "type", "attr", "sender", "content"))
            if existing_key == key:
                return
        self.messages.append(entry)

    def get_messages(self, _chat_name, limit):
        return list(self.messages)[-int(limit or 0):]


class MemoryWriteCallbackTests(unittest.TestCase):
    def test_message_callback_saves_memory_with_callback_receive_time(self):
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
        self.assertIn("message_time", bot.memory_manager.calls[0])

    def test_friend_message_callback_marks_chat_memory_dirty_without_sync_update(self):
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
        bot._mark_chat_memory_dirty = lambda chat, msg: dirty_calls.append((chat.who, msg.content)) or True
        bot._maybe_update_chat_memory = lambda _chat, _msg: self.fail("不应在消息回调里同步维护会话记忆")

        msg = SimpleNamespace(attr="friend", sender="friend-a", content="hello", type="text")
        chat = SimpleNamespace(who="chat-a", chat_type="private")

        bot.message_handle_callback(msg, chat)

        self.assertEqual(len(bot.memory_manager.calls), 1)
        self.assertEqual(dirty_calls, [("chat-a", "hello")])

    def test_manual_private_self_callback_saves_memory_and_interrupts_ai_reply(self):
        class FakeTimer:
            def __init__(self):
                self.cancelled = False

            def cancel(self):
                self.cancelled = True

        bot = WXBot.__new__(WXBot)
        bot.config = SimpleNamespace(
            memory_switch=True,
            memory_max_count=5000,
            group_welcome=False,
            group=[],
            cmd="admin",
            AllListen_switch=False,
            listen_list=[],
            global_blacklist=[],
            chat_image_recognition_switch=False,
            chat_voice_recognition_switch=False,
        )
        bot.memory_manager = CaptureMemory()
        bot._resolve_identity_chat_name = lambda name: name
        bot._handle_material_source_message = lambda _chat, _msg: False
        bot.callback_is_die = False
        bot.wx = SimpleNamespace(nickname="bot")
        bot.is_err = lambda *args, **kwargs: self.fail(f"unexpected error: {args}")
        dirty_calls = []
        bot._mark_chat_memory_dirty = lambda chat, msg: dirty_calls.append((chat.who, msg.content)) or True

        timer = FakeTimer()
        bot._ensure_message_runtime_state()
        bot._private_message_sequence_by_chat["张三"] = 1
        bot._private_message_pipelines["张三"] = {
            "open_messages": [SimpleNamespace(content="上一条")],
            "open_started_at": 1.0,
            "open_kind": "text",
            "idle_timer": timer,
            "max_timer": timer,
            "queued_batches": deque([[SimpleNamespace(content="待回复")]]),
            "worker_running": True,
        }
        bot._ensure_lightweight_send_queue_state()
        bot._lightweight_send_queue["张三"] = {
            "target": "张三",
            "actions": [{"type": "text", "text": "旧 AI 回复"}],
            "source": "private_ai_reply",
        }

        msg = SimpleNamespace(attr="self", sender="self", content="我手机上已经回复了", type="text")
        chat = SimpleNamespace(who="张三", chat_type="private")

        with mock.patch("wxbot_core.log"):
            bot.message_handle_callback(msg, chat)

        self.assertEqual(len(bot.memory_manager.calls), 1)
        self.assertEqual(bot.memory_manager.calls[0]["msg_attr"], "self")
        self.assertEqual(bot.memory_manager.calls[0]["content"], "我手机上已经回复了")
        self.assertEqual(dirty_calls, [("张三", "我手机上已经回复了")])
        self.assertEqual(bot._get_private_message_sequence("张三"), 2)
        self.assertNotIn("张三", bot._private_message_pipelines)
        self.assertTrue(timer.cancelled)
        self.assertNotIn("张三", bot._lightweight_send_queue)

    def test_private_reply_echo_self_callback_does_not_interrupt_ai_reply(self):
        bot = WXBot.__new__(WXBot)
        bot.config = SimpleNamespace(
            memory_switch=True,
            memory_max_count=5000,
            group_welcome=False,
            group=[],
            cmd="admin",
            AllListen_switch=False,
            listen_list=[],
            global_blacklist=[],
            chat_image_recognition_switch=False,
            chat_voice_recognition_switch=False,
        )
        bot.memory_manager = CaptureMemory()
        bot._resolve_identity_chat_name = lambda name: name
        bot._handle_material_source_message = lambda _chat, _msg: False
        bot.callback_is_die = False
        bot.wx = SimpleNamespace(nickname="bot")
        bot.is_err = lambda *args, **kwargs: self.fail(f"unexpected error: {args}")
        bot._mark_chat_memory_dirty = lambda _chat, _msg: self.fail("机器人回显不应重复写入记忆")
        bot._ensure_message_runtime_state()
        bot._private_message_sequence_by_chat["张三"] = 1
        bot._private_message_pipelines["张三"] = {
            "open_messages": [SimpleNamespace(content="上一条")],
            "open_started_at": 1.0,
            "open_kind": "text",
            "idle_timer": None,
            "max_timer": None,
            "queued_batches": deque(),
            "worker_running": True,
        }
        bot._remember_persisted_private_reply_echo("张三", "机器人刚发")

        msg = SimpleNamespace(attr="self", sender="self", content="机器人刚发", type="text")
        chat = SimpleNamespace(who="张三", chat_type="private")

        bot.message_handle_callback(msg, chat)

        self.assertEqual(bot.memory_manager.calls, [])
        self.assertEqual(bot._get_private_message_sequence("张三"), 1)
        self.assertIn("张三", bot._private_message_pipelines)
        self.assertTrue(getattr(msg, "_wxbot_private_reply_persisted_echo", False))

    def test_friend_message_after_manual_self_starts_new_ai_with_manual_self_in_history(self):
        bot = WXBot.__new__(WXBot)
        bot.config = SimpleNamespace(
            memory_switch=True,
            memory_context_switch=True,
            memory_context_count=10,
            memory_max_count=5000,
            group_welcome=False,
            group=[],
            group_switch=False,
            cmd="admin",
            AllListen_switch=False,
            listen_list=["张三"],
            global_blacklist=[],
            custom_forward_switch=False,
            chat_image_recognition_switch=False,
            chat_voice_recognition_switch=False,
            chat_keyword_switch=False,
            keyword_dict={},
            chat_split_reply_switch=False,
            chat_split_max_count=4,
            chat_split_max_chars=100,
            clean_ai_reply_switch=False,
            meta_reply_blocked_reply="",
            meta_reply_blocked_reply_once=False,
            api_error_reply_once=False,
            text_reply_limit_switch=False,
            text_reply_limit_hours=24,
            chat_voice_reply_switch=False,
            chat_message_merge_delay=20,
            chat_listen_only=False,
            split_long_text=lambda text: [text],
        )
        bot.memory_manager = InMemoryChatMemory()
        bot._resolve_identity_chat_name = lambda name: name
        bot._handle_admin_forward_input = lambda _chat, _msg: False
        bot._handle_admin_moments_input = lambda _chat, _msg: False
        bot._handle_material_source_message = lambda _chat, _msg: False
        bot._record_received_message = lambda: None
        bot._mark_chat_memory_dirty = lambda _chat, _msg: True
        bot._repair_private_context_before_ai = lambda _chat, _msg: False
        bot._current_ai_material_outreach_config = lambda: {"ai_material_outreach_switch": False}
        bot._build_prompt_with_context = lambda *_args, **_kwargs: "prompt"
        bot._send_private_ai_reply_parts = lambda *_args, **_kwargs: (True, True)
        bot._record_reply_metric_success = lambda *_args, **_kwargs: None
        bot._voice_reply_state = {"private_sessions": {}, "limits": {}}
        bot._pause_chat_reply = False
        bot._pause_chat_reply_users = set()
        bot.is_stop_requested = lambda: False
        bot.callback_is_die = False
        bot.wx = SimpleNamespace(nickname="bot")
        bot.is_err = lambda *args, **kwargs: self.fail(f"unexpected error: {args}")
        bot._schedule_private_message_timer = lambda *_args, **_kwargs: SimpleNamespace(cancel=lambda: None)

        def mark_worker_ready(_chat, pipeline):
            pipeline["worker_running"] = True
            return True

        bot._start_private_message_worker_locked = mark_worker_ready
        captured_requests = []
        bot._get_chat_api = lambda _user: SimpleNamespace(
            chat=lambda message, **kwargs: captured_requests.append({
                "message": message,
                "history": kwargs.get("history") or [],
            }) or "新的 AI 回复"
        )

        chat = SimpleNamespace(who="张三", chat_type="private")
        manual_self = SimpleNamespace(attr="self", sender="self", content="我手机上已经回复了", type="text")
        friend_message = SimpleNamespace(attr="friend", sender="张三", content="那我再问一句", type="text", id="friend-1")

        with mock.patch("wxbot_core.log"):
            bot.message_handle_callback(manual_self, chat)
            bot.message_handle_callback(friend_message, chat)
            self.assertTrue(bot._close_private_message_batch_by_idle(chat))
            self.assertTrue(bot._run_private_message_pipeline_worker(chat))

        self.assertEqual(len(captured_requests), 1)
        history = captured_requests[0]["history"]
        self.assertIn(
            {"role": "assistant", "content": "我手机上已经回复了"},
            history,
        )
        self.assertTrue(
            any(item["role"] == "user" and "那我再问一句" in item["content"] for item in history)
        )

    def test_voice_duration_only_message_is_not_saved_as_chat_content(self):
        bot = WXBot.__new__(WXBot)
        bot.config = SimpleNamespace(cmd="admin")

        chat = SimpleNamespace(who="chat-a", chat_type="private")
        duration_only = SimpleNamespace(attr="self", sender="self", content='语音10"秒', type="voice")
        failed = SimpleNamespace(attr="friend", sender="friend-a", content="语音未能转换", type="voice")
        failed_with_duration = SimpleNamespace(attr="friend", sender="friend-a", content='语音1"秒语音未能转换', type="voice")
        failed_conversion = SimpleNamespace(attr="friend", sender="friend-a", content="语音转换失败", type="voice")
        failed_recognition = SimpleNamespace(attr="friend", sender="friend-a", content="语音识别失败", type="voice")
        transcribed = SimpleNamespace(
            attr="self",
            sender="self",
            content='语音10"秒\n\n这么想听啊，那等白天嗓子开了给你哼两句。',
            type="voice",
        )

        self.assertTrue(bot._should_skip_message_memory(chat, duration_only))
        self.assertTrue(bot._should_skip_message_memory(chat, failed))
        self.assertTrue(bot._should_skip_message_memory(chat, failed_with_duration))
        self.assertTrue(bot._should_skip_message_memory(chat, failed_conversion))
        self.assertTrue(bot._should_skip_message_memory(chat, failed_recognition))
        self.assertFalse(bot._should_skip_message_memory(chat, transcribed))

    def test_voice_label_is_display_only_not_model_context(self):
        record = {
            "time": "2026/07/02 05:05:51",
            "type": "voice",
            "attr": "self",
            "sender": "self",
            "content": "这么想听啊，那等白天嗓子开了给你哼两句。",
        }

        self.assertEqual(format_memory_record_for_display(record)["content"], "[语音]这么想听啊，那等白天嗓子开了给你哼两句。")
        self.assertEqual(
            format_history_message(record),
            {"role": "assistant", "content": "这么想听啊，那等白天嗓子开了给你哼两句。"},
        )

    def test_voice_duration_metadata_is_removed_when_transcription_exists(self):
        self.assertEqual(strip_voice_duration_metadata('语音10"秒\n\n这么想听啊'), "这么想听啊")
        self.assertEqual(strip_voice_duration_metadata('语音10"秒'), '语音10"秒')

        merged = build_merged_private_message([
            SimpleNamespace(type="voice", attr="friend", sender="张三", content='语音8"秒我刚说的是这个'),
        ])
        self.assertEqual(merged.content, "我刚说的是这个")

    def test_current_voice_message_uses_transcription_without_voice_label_for_model(self):
        self.assertEqual(
            format_model_message_text({"type": "voice", "content": '语音8"秒我刚说的是这个'}),
            "我刚说的是这个",
        )
        self.assertEqual(
            format_model_message_text({"type": "voice", "content": ""}),
            "一条语音消息（未识别出文字）",
        )
        self.assertEqual(format_model_message_text({"type": "voice", "content": '语音1"秒语音未能转换'}), "")
        self.assertEqual(format_model_message_text({"type": "voice", "content": "一条语音消息（未识别出文字）"}), "")

    def test_failed_voice_does_not_pollute_merged_private_message(self):
        merged = build_merged_private_message([
            SimpleNamespace(type="text", attr="friend", sender="张三", content="前一句"),
            SimpleNamespace(type="voice", attr="friend", sender="张三", content='语音1"秒语音未能转换'),
            SimpleNamespace(type="text", attr="friend", sender="张三", content="后一句"),
        ])

        self.assertEqual(merged.content, "前一句\n后一句")
        self.assertFalse(getattr(merged, "_contains_voice_message", False))

    def test_mixed_failed_and_transcribed_voices_keep_valid_voice_content(self):
        merged = build_merged_private_message([
            SimpleNamespace(type="voice", attr="friend", sender="张三", content='语音1"秒语音未能转换'),
            SimpleNamespace(type="voice", attr="friend", sender="张三", content='语音8"秒我刚说的是这个'),
            SimpleNamespace(type="voice", attr="friend", sender="张三", content="语音识别失败"),
        ])

        self.assertEqual(merged.content, "我刚说的是这个")
        self.assertTrue(getattr(merged, "_contains_voice_message", False))

    def test_existing_text_voice_label_is_removed_from_model_context(self):
        record = {
            "time": "2026/07/02 05:05:51",
            "type": "text",
            "attr": "self",
            "sender": "self",
            "content": "[语音]你这些话我都听进去了。",
        }

        self.assertEqual(
            format_history_message(record),
            {"role": "assistant", "content": "你这些话我都听进去了。"},
        )
        self.assertEqual(clean_ai_reply_text("[语音]你这些话我都听进去了。"), "你这些话我都听进去了。")

    def test_emotion_history_preserves_readable_meaning(self):
        record = {
            "time": "2026/07/02 05:05:51",
            "type": "emotion",
            "attr": "friend",
            "sender": "张三",
            "content": "动画表情 [好的]",
        }

        self.assertEqual(format_memory_record_for_display(record)["content"], "[微信表情][好的]")
        self.assertEqual(
            format_history_message(record),
            {"role": "user", "content": "张三: [微信表情][好的]"},
        )

        merged = build_merged_private_message([
            SimpleNamespace(type="emotion", attr="friend", sender="张三", content="动画表情 [好的]"),
        ])
        self.assertEqual(merged.content, "[微信表情][好的]")

    def test_empty_emotion_history_uses_stable_description(self):
        record = {
            "time": "2026/07/02 05:05:51",
            "type": "emotion",
            "attr": "friend",
            "sender": "张三",
            "content": "",
        }

        self.assertEqual(format_memory_record_for_display(record)["content"], "[微信表情]")
        self.assertEqual(
            format_history_message(record),
            {"role": "user", "content": "张三: [微信表情]"},
        )

    def test_system_time_separator_marks_next_real_message_time(self):
        history = [
            {"time": "2026/07/02 05:05:53", "type": "time", "attr": "system", "sender": "system", "content": "05:05"},
            {"time": "2026/07/02 05:06:30", "type": "text", "attr": "friend", "sender": "张三", "content": "什么意思"},
            {"time": "2026/07/02 05:06:44", "type": "text", "attr": "self", "sender": "self", "content": "没唱，就是想让你听听我的声音。"},
            {"time": "2026/07/02 05:10:00", "type": "time", "attr": "system", "sender": "system", "content": "05:10"},
            {"time": "2026/07/02 05:10:10", "type": "text", "attr": "friend", "sender": "张三", "content": "知道了"},
        ]

        visible = build_model_visible_history(history)

        self.assertEqual(
            visible,
            [
                {"role": "user", "content": "发送时间：05:05\n张三: 什么意思"},
                {"role": "assistant", "content": "没唱，就是想让你听听我的声音。"},
                {"role": "user", "content": "发送时间：05:10\n张三: 知道了"},
            ],
        )

    def test_system_text_time_separator_marks_next_real_message_time(self):
        history = [
            {"time": "2026/07/02 05:05:53", "type": "text", "attr": "system", "sender": "system", "content": "05:05"},
            {"time": "2026/07/02 05:06:30", "type": "text", "attr": "friend", "sender": "张三", "content": "什么意思"},
            {"time": "2026/07/02 05:07:00", "type": "text", "attr": "system", "sender": "system", "content": "你撤回了一条消息"},
            {"time": "2026/07/02 05:07:30", "type": "text", "attr": "friend", "sender": "张三", "content": "没事"},
        ]

        visible = build_model_visible_history(history)

        self.assertEqual(
            visible,
            [
                {"role": "user", "content": "发送时间：05:05\n张三: 什么意思"},
                {"role": "user", "content": "张三: 没事"},
            ],
        )

    def test_message_limit_counts_real_messages_not_time_separators(self):
        history = [
            {"time": "2026/07/02 05:00:00", "type": "time", "attr": "system", "sender": "system", "content": "05:00"},
            {"time": "2026/07/02 05:00:10", "type": "text", "attr": "friend", "sender": "张三", "content": "第一句"},
            {"time": "2026/07/02 05:01:00", "type": "time", "attr": "system", "sender": "system", "content": "05:01"},
            {"time": "2026/07/02 05:01:10", "type": "text", "attr": "self", "sender": "self", "content": "第二句"},
            {"time": "2026/07/02 05:02:00", "type": "time", "attr": "system", "sender": "system", "content": "05:02"},
            {"time": "2026/07/02 05:02:10", "type": "text", "attr": "friend", "sender": "张三", "content": "第三句"},
        ]

        visible = build_model_visible_history(history, message_limit=2)

        self.assertEqual(
            visible,
            [
                {"role": "assistant", "content": "发送时间：05:01\n第二句"},
                {"role": "user", "content": "发送时间：05:02\n张三: 第三句"},
            ],
        )

    def test_semantic_messages_are_normalized_consistently(self):
        self.assertEqual(format_message_semantic_text({"type": "link", "content": "[链接]青藏高原(Live)\n来自优优的作品\n全民K歌"}), "[链接]青藏高原(Live)\n来自优优的作品\n全民K歌")
        self.assertEqual(format_message_semantic_text({"type": "miniapp", "content": "小程序冷亦文集有一种执念，生命不老，此情不变"}), "[小程序]冷亦文集有一种执念，生命不老，此情不变")
        self.assertEqual(format_message_semantic_text({"type": "video", "content": "视频 下载0:09"}), "[视频]0:09")
        self.assertEqual(format_message_semantic_text({"type": "personal_card", "content": "名片张三"}), "[个人名片]张三")
        self.assertEqual(format_message_semantic_text({"type": "location", "content": "[位置] 福建省福州市鼓楼区"}), "[位置]福建省福州市鼓楼区")
        self.assertEqual(format_message_semantic_text({"type": "merge", "content": "[聊天记录] A与B的聊天记录"}), "[聊天记录]A与B的聊天记录")
        self.assertEqual(format_message_semantic_text({"type": "image", "content": "C:/temp/a.png"}), "[图片]")

    def test_image_history_preserves_image_shell_and_summary(self):
        record = {
            "time": "2026/07/02 05:05:51",
            "type": "image",
            "attr": "friend",
            "sender": "张三",
            "content": "[图片]",
            "image_paths": [r"C:\tmp\a.png"],
            "visual_notes": [
                "图片概览：一张聊天截图。\n可见文字：有。\n关键细节：像是在讨论出发时间。\n不确定项：部分字看不清。",
            ],
        }

        self.assertEqual(
            format_memory_record_for_display(record)["content"],
            "[图片] 一张聊天截图。 可见文字：有。 关键细节：像是在讨论出发时间。",
        )
        self.assertEqual(
            format_history_message(record),
            {"role": "user", "content": "张三: [图片]\n一张聊天截图。\n可见文字：有。\n关键细节：像是在讨论出发时间。"},
        )

    def test_image_history_does_not_drop_older_media_records(self):
        history = [
            {
                "time": f"2026/07/02 05:0{index}:00",
                "type": "image",
                "attr": "friend",
                "sender": "张三",
                "content": "[图片]",
                "image_paths": [fr"C:\tmp\{index}.png"],
                "visual_notes": [
                    f"图片概览：第{index}张图。\n可见文字：无。\n关键细节：测试图片{index}。\n不确定项：无。",
                ],
            }
            for index in range(1, 5)
        ]

        visible = build_model_visible_history(history, message_limit=4)

        self.assertEqual(len(visible), 4)
        self.assertEqual(
            [item["content"] for item in visible],
            [
                "张三: [图片]\n第1张图。\n可见文字：无。\n关键细节：测试图片1。",
                "张三: [图片]\n第2张图。\n可见文字：无。\n关键细节：测试图片2。",
                "张三: [图片]\n第3张图。\n可见文字：无。\n关键细节：测试图片3。",
                "张三: [图片]\n第4张图。\n可见文字：无。\n关键细节：测试图片4。",
            ],
        )

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
        self.assertEqual(bot.memory_manager.calls[0]["content"], "B 的语音内容")
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
        bot._generate_visual_notes_for_image_paths = (
            lambda *_args, **_kwargs: self.fail("群聊图片只保存记录时不应同步调用视觉模型")
        )

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
        self.assertEqual(bot.memory_manager.calls[0]["content"], "[图片]")
        self.assertEqual(bot.memory_manager.calls[0]["msg_type"], "image")
        self.assertEqual(bot.memory_manager.calls[0]["image_paths"], [r"C:\tmp\group-image.png"])
        self.assertNotIn("visual_notes", bot.memory_manager.calls[0])

    def test_chat_memory_background_worker_uses_existing_update_logic(self):
        bot = WXBot.__new__(WXBot)
        bot.run_flag = True
        bot._chat_memory_dirty_lock = mock.MagicMock()
        bot._chat_memory_dirty_chats = {"张三": 1.0}
        bot._chat_memory_worker_running = True
        calls = []
        bot._maybe_update_chat_memory = lambda chat, msg: calls.append((chat.who, msg.attr)) or None

        with mock.patch("wxbot_core.time.sleep", return_value=None):
            bot._chat_memory_background_worker()

        self.assertEqual(calls, [("张三", "friend")])
        self.assertFalse(bot._chat_memory_worker_running)

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
        bot._mark_chat_memory_dirty = lambda chat, msg: marked.append((chat.who, msg.attr)) or True

        with mock.patch("wxbot_core.log") as fake_log:
            count = bot._enqueue_existing_chat_memory_checks()

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
            result = bot._maybe_update_chat_memory(SimpleNamespace(who="B-岁月静好3", chat_type="private"), SimpleNamespace(attr="friend"))

        self.assertTrue(result)
        self.assertTrue(any(level == "INFO" and "会话记忆已更新：B-岁月静好3" == message for level, message in logs))
        self.assertFalse(any("API返回成功" in message for _level, message in logs))


if __name__ == "__main__":
    unittest.main()
