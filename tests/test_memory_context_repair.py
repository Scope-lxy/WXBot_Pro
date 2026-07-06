import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from core.memory import MemoryManager
from core.memory_context_repair import (
    build_repair_plan,
    current_message_found_near_tail,
)
from wxbot_core import WXBot


def msg(content, *, attr="friend", sender="张三", msg_type="text", time="2026/07/03 05:00:00"):
    return SimpleNamespace(attr=attr, sender=sender, type=msg_type, content=content, time=time)


class MemoryContextRepairCoreTests(unittest.TestCase):
    def test_build_repair_plan_appends_after_anchor(self):
        local = [
            {"time": "1", "attr": "friend", "sender": "张三", "type": "text", "content": "早"},
            {"time": "2", "attr": "self", "sender": "self", "type": "text", "content": "早呀"},
        ]
        remote = local + [
            {"time": "3", "attr": "friend", "sender": "张三", "type": "text", "content": "今天不舒服"},
            {"time": "4", "attr": "self", "sender": "self", "type": "text", "content": "那多休息"},
        ]

        plan = build_repair_plan(local, remote, anchor_recent_count=5)

        self.assertTrue(plan.anchor_found)
        self.assertEqual([item["content"] for item in plan.messages_to_append], ["今天不舒服", "那多休息"])

    def test_build_repair_plan_appends_missing_messages_before_anchor(self):
        local = [
            {"time": "1", "attr": "friend", "sender": "张三", "type": "text", "content": "早"},
            {"time": "4", "attr": "friend", "sender": "张三", "type": "text", "content": "后来呢"},
        ]
        remote = [
            {"time": "1", "attr": "friend", "sender": "张三", "type": "text", "content": "早"},
            {"time": "2", "attr": "self", "sender": "self", "type": "text", "content": "手机发的第一条"},
            {"time": "3", "attr": "self", "sender": "self", "type": "text", "content": "手机发的第二条"},
            {"time": "4", "attr": "friend", "sender": "张三", "type": "text", "content": "后来呢"},
        ]

        plan = build_repair_plan(local, remote, anchor_recent_count=5)

        self.assertTrue(plan.anchor_found)
        self.assertEqual(
            [item["content"] for item in plan.messages_to_append],
            ["手机发的第一条", "手机发的第二条"],
        )

    def test_anchor_matching_tolerates_self_sender_format_mismatch(self):
        local = [
            {"time": "2026/07/04 08:00:00", "attr": "friend", "sender": "张三", "type": "text", "content": "早"},
            {"time": "2026/07/04 08:01:00", "attr": "self", "sender": "self", "type": "text", "content": "早呀"},
        ]
        remote = [
            {"time": "2026/07/04 08:00:00", "attr": "friend", "sender": "张三", "type": "text", "content": "早"},
            {"time": "2026/07/04 08:01:02", "attr": "self", "sender": "me", "type": "text", "content": "早呀"},
            {"time": "2026/07/04 08:02:00", "attr": "friend", "sender": "张三", "type": "text", "content": "那就好"},
        ]

        plan = build_repair_plan(local, remote, anchor_recent_count=5)

        self.assertTrue(plan.anchor_found)
        self.assertEqual([item["content"] for item in plan.messages_to_append], ["那就好"])

    def test_anchor_matching_skips_voice_messages_as_unstable_anchors(self):
        local = [
            {"time": "2026/07/04 08:00:00", "attr": "friend", "sender": "张三", "type": "text", "content": "第一句"},
            {"time": "2026/07/04 08:01:00", "attr": "self", "sender": "self", "type": "text", "content": "第二句"},
            {"time": "2026/07/04 08:02:00", "attr": "friend", "sender": "张三", "type": "voice", "content": "微信转写内容一"},
            {"time": "2026/07/04 08:03:00", "attr": "self", "sender": "self", "type": "voice", "content": "微信转写内容二"},
            {"time": "2026/07/04 08:04:00", "attr": "friend", "sender": "张三", "type": "voice", "content": "微信转写内容三"},
            {"time": "2026/07/04 08:05:00", "attr": "self", "sender": "self", "type": "voice", "content": "微信转写内容四"},
            {"time": "2026/07/04 08:06:00", "attr": "friend", "sender": "张三", "type": "voice", "content": "微信转写内容五"},
        ]
        remote = [
            {"time": "2026/07/04 08:00:00", "attr": "friend", "sender": "张三", "type": "text", "content": "第一句"},
            {"time": "2026/07/04 08:01:02", "attr": "self", "sender": "me", "type": "text", "content": "第二句"},
            {"time": "2026/07/04 08:02:00", "attr": "friend", "sender": "张三", "type": "voice", "content": "一条语音消息（未识别出文字）"},
            {"time": "2026/07/04 08:03:00", "attr": "self", "sender": "me", "type": "voice", "content": "一条语音消息（未识别出文字）"},
            {"time": "2026/07/04 08:04:00", "attr": "friend", "sender": "张三", "type": "voice", "content": "一条语音消息（未识别出文字）"},
            {"time": "2026/07/04 08:05:00", "attr": "self", "sender": "me", "type": "voice", "content": "一条语音消息（未识别出文字）"},
            {"time": "2026/07/04 08:06:00", "attr": "friend", "sender": "张三", "type": "voice", "content": "一条语音消息（未识别出文字）"},
            {"time": "2026/07/04 08:07:00", "attr": "friend", "sender": "张三", "type": "text", "content": "新内容"},
        ]

        plan = build_repair_plan(local, remote, anchor_recent_count=5)

        self.assertTrue(plan.anchor_found)
        self.assertEqual([item["content"] for item in plan.messages_to_append], ["新内容"])

    def test_build_repair_plan_skips_self_unrecognized_voice_placeholder(self):
        local = [
            {
                "time": "2026/07/04 17:12:31",
                "attr": "self",
                "sender": "self",
                "type": "voice",
                "content": "梅姐，你赢的钱自己留着用，我有饭吃的。",
            },
        ]
        remote = [
            {
                "time": "2026/07/04 17:12:00",
                "attr": "self",
                "sender": "me",
                "type": "voice",
                "content": "一条语音消息（未识别出文字）",
            },
            {
                "time": "2026/07/04 17:12:36",
                "attr": "friend",
                "sender": "张三",
                "type": "voice",
                "content": "你真好。",
            },
        ]

        plan = build_repair_plan(local, remote, anchor_recent_count=5)

        self.assertEqual([item["content"] for item in plan.messages_to_append], ["你真好。"])

    def test_build_repair_plan_skips_nearby_duplicate_from_cli(self):
        local = [
            {
                "time": "2026/07/04 17:13:02",
                "attr": "self",
                "sender": "self",
                "type": "text",
                "content": "梅姐，我知道你是真心的，但你赚钱辛苦，我不能要你的钱。",
            },
        ]
        remote = [
            {
                "time": "2026/07/04 17:13:00",
                "attr": "self",
                "sender": "me",
                "type": "text",
                "content": "梅姐，我知道你是真心的，但你赚钱辛苦，我不能要你的钱。",
            },
            {
                "time": "2026/07/04 17:13:03",
                "attr": "friend",
                "sender": "张三",
                "type": "voice",
                "content": "你那么关心我啊。",
            },
        ]

        plan = build_repair_plan(local, remote, anchor_recent_count=5)

        self.assertEqual([item["content"] for item in plan.messages_to_append], ["你那么关心我啊。"])

    def test_build_repair_plan_skips_nearby_friend_duplicate_from_cli(self):
        local = [
            {
                "time": "2026/07/04 17:11:57",
                "attr": "friend",
                "sender": "张三",
                "type": "voice",
                "content": "那你怎么不收钱啊？快点收吧。",
            },
        ]
        remote = [
            {
                "time": "2026/07/04 17:12:00",
                "attr": "friend",
                "sender": "张三",
                "type": "voice",
                "content": "那你怎么不收钱啊？快点收吧。",
            },
            {
                "time": "2026/07/04 17:12:36",
                "attr": "friend",
                "sender": "张三",
                "type": "voice",
                "content": "你真好。",
            },
        ]

        plan = build_repair_plan(local, remote, anchor_recent_count=5)

        self.assertEqual([item["content"] for item in plan.messages_to_append], ["你真好。"])

    def test_build_repair_plan_keeps_same_content_after_duplicate_window(self):
        local = [
            {
                "time": "2026/07/04 17:00:00",
                "attr": "friend",
                "sender": "张三",
                "type": "text",
                "content": "你在吗",
            },
        ]
        remote = [
            {
                "time": "2026/07/04 17:11:00",
                "attr": "friend",
                "sender": "张三",
                "type": "text",
                "content": "你在吗",
            },
        ]

        plan = build_repair_plan(local, remote, anchor_recent_count=5)

        self.assertEqual([item["content"] for item in plan.messages_to_append], ["你在吗"])

    def test_group_anchor_matching_keeps_sender_identity(self):
        local = [
            {"time": "2026/07/04 08:00:00", "attr": "group", "sender": "A", "type": "text", "content": "收到"},
            {"time": "2026/07/04 08:01:00", "attr": "group", "sender": "B", "type": "text", "content": "收到"},
        ]
        remote = [
            {"time": "2026/07/04 08:00:00", "attr": "friend", "sender": "A", "type": "text", "content": "收到"},
            {"time": "2026/07/04 08:01:00", "attr": "friend", "sender": "B", "type": "text", "content": "收到"},
            {"time": "2026/07/04 08:02:00", "attr": "friend", "sender": "C", "type": "text", "content": "新内容"},
        ]

        plan = build_repair_plan(local, remote, anchor_recent_count=5, chat_type="group")

        self.assertTrue(plan.anchor_found)
        self.assertEqual([(item["sender"], item["content"]) for item in plan.messages_to_append], [("C", "新内容")])

    def test_group_repair_does_not_dedupe_same_content_from_different_members(self):
        local = [
            {
                "time": "2026/07/04 17:00:00",
                "attr": "group",
                "sender": "A",
                "type": "text",
                "content": "好的",
            },
        ]
        remote = [
            {
                "time": "2026/07/04 17:01:00",
                "attr": "friend",
                "sender": "B",
                "type": "text",
                "content": "好的",
            },
        ]

        plan = build_repair_plan(local, remote, anchor_recent_count=5, chat_type="group")

        self.assertEqual([(item["sender"], item["content"]) for item in plan.messages_to_append], [("B", "好的")])

    def test_repeated_short_text_without_sequence_anchor_is_conservative(self):
        local = [
            {"time": "1", "attr": "friend", "sender": "张三", "type": "text", "content": "好"},
        ]
        remote = [
            {"time": "2", "attr": "friend", "sender": "张三", "type": "text", "content": "好"},
            {"time": "3", "attr": "friend", "sender": "张三", "type": "text", "content": "好"},
        ]

        plan = build_repair_plan(local, remote, anchor_recent_count=5)

        self.assertFalse(plan.anchor_found)

    def test_current_message_found_near_tail(self):
        local = [
            {"time": "1", "attr": "self", "sender": "self", "type": "text", "content": "好"},
            {"time": "2", "attr": "friend", "sender": "张三", "type": "text", "content": "来了"},
        ]

        self.assertTrue(current_message_found_near_tail(local, msg("来了", time="2")))
        self.assertFalse(current_message_found_near_tail(local, msg("新消息", time="3")))


class MemoryManagerContextRepairTests(unittest.TestCase):
    def test_save_message_keeps_repeated_text_without_explicit_time(self):
        with tempfile.TemporaryDirectory() as tmp:
            manager = MemoryManager("wxid", tmp)

            manager.save_message("张三", "张三", "好", "text", "friend", 100)
            manager.save_message("张三", "张三", "好", "text", "friend", 100)

            self.assertEqual([item["content"] for item in manager.get_messages("张三", 10)], ["好", "好"])

    def test_save_message_dedupes_explicit_same_message_time(self):
        with tempfile.TemporaryDirectory() as tmp:
            manager = MemoryManager("wxid", tmp)

            manager.save_message("张三", "张三", "好", "text", "friend", 100, message_time="2026/07/03 05:00:00")
            manager.save_message("张三", "张三", "好", "text", "friend", 100, message_time="2026/07/03 05:00:00")

            self.assertEqual([item["content"] for item in manager.get_messages("张三", 10)], ["好"])

    def test_append_missing_messages_dedupes_by_time_and_fingerprint(self):
        with tempfile.TemporaryDirectory() as tmp:
            manager = MemoryManager("wxid", tmp)
            manager.save_message("张三", "张三", "早", "text", "friend", 100, message_time="2026/07/03 05:00:00")

            result = manager.append_missing_messages(
                "张三",
                [
                    {"time": "2026/07/03 05:00:00", "attr": "friend", "sender": "张三", "type": "text", "content": "早"},
                    {"time": "2026/07/03 05:01:00", "attr": "self", "sender": "self", "type": "text", "content": "早呀"},
                ],
                100,
            )

            self.assertEqual(result["added"], 1)
            self.assertEqual([item["content"] for item in manager.get_messages("张三", 10)], ["早", "早呀"])

    def test_append_missing_messages_sorts_entries_by_time(self):
        with tempfile.TemporaryDirectory() as tmp:
            manager = MemoryManager("wxid", tmp)

            result = manager.append_missing_messages(
                "张三",
                [
                    {"time": "2026/07/03 05:02:00", "attr": "friend", "sender": "张三", "type": "text", "content": "第三条"},
                    {"time": "2026/07/03 05:01:00", "attr": "self", "sender": "self", "type": "text", "content": "第二条"},
                    {"time": "2026/07/03 05:00:00", "attr": "friend", "sender": "张三", "type": "text", "content": "第一条"},
                ],
                100,
            )

            self.assertEqual(result["added"], 3)
            self.assertEqual(
                [item["content"] for item in manager.get_messages("张三", 10)],
                ["第一条", "第二条", "第三条"],
            )


class FakeLock:
    def __init__(self, acquired=True):
        self.acquired = acquired
        self.acquire_calls = []
        self.released = False

    def acquire(self, blocking=True):
        self.acquire_calls.append(blocking)
        return self.acquired

    def release(self):
        self.released = True


class FakeChat:
    who = "张三"
    chat_type = "private"

    def __init__(self, visible=None, history=None):
        self.visible = visible or []
        self.history = history or []
        self.get_all_calls = 0
        self.ChatBox = SimpleNamespace(get_msgs_from_history=self.get_msgs_from_history)

    def GetAllMessage(self):
        self.get_all_calls += 1
        return self.visible

    def get_msgs_from_history(self, limit, callback=None, interval=0.2, speed=5, goback=True):
        self.history_args = {
            "limit": limit,
            "interval": interval,
            "speed": speed,
            "goback": goback,
        }
        return self.history[:limit]


class FailingHistoryChat(FakeChat):
    def get_msgs_from_history(self, limit, callback=None, interval=0.2, speed=5, goback=True):
        raise RuntimeError("history boom")


class WXBotContextRepairTests(unittest.TestCase):
    def make_bot(self, tmp, *, high_enabled=False, lock=None, local_reader_enabled=False, memory_context_count=50):
        bot = WXBot.__new__(WXBot)
        bot.config = SimpleNamespace(
            memory_switch=True,
            memory_context_switch=True,
            memory_max_count=100,
            memory_context_count=memory_context_count,
            memory_context_repair_low_risk_switch=True,
            memory_context_repair_high_risk_switch=high_enabled,
        )
        bot.memory_manager = MemoryManager("wxid", tmp)
        bot._resolve_identity_chat_name = lambda name: name
        bot._mark_chat_memory_dirty = lambda *args, **kwargs: None
        bot.is_stop_requested = lambda: False
        bot._wechat_action_lock = lock or FakeLock()
        bot._get_wechat_action_lock = lambda: bot._wechat_action_lock
        bot._local_wechat_reader_enabled = local_reader_enabled
        bot._incoming_seen_lock = None
        bot._ensure_message_runtime_state()
        return bot

    def test_low_risk_repairs_visible_gap(self):
        with tempfile.TemporaryDirectory() as tmp:
            bot = self.make_bot(tmp)
            bot.memory_manager.append_missing_messages(
                "张三",
                [
                    {"time": "1", "attr": "friend", "sender": "张三", "type": "text", "content": "早"},
                    {"time": "2", "attr": "self", "sender": "self", "type": "text", "content": "早呀"},
                ],
                100,
            )
            chat = FakeChat(visible=[
                msg("早", time="1"),
                msg("早呀", attr="self", sender="self", time="2"),
                msg("新内容", time="3"),
            ])

            bot._repair_private_context_before_ai(chat, msg("新内容", time="3"))

            self.assertEqual(
                [item["content"] for item in bot.memory_manager.get_messages("张三", 10)],
                ["早", "早呀", "新内容"],
            )

    def test_low_risk_prefers_local_history_without_wechat_lock_and_skips_unrecognized_voice(self):
        with tempfile.TemporaryDirectory() as tmp:
            lock = FakeLock(acquired=False)
            bot = self.make_bot(tmp, lock=lock, local_reader_enabled=True)
            bot.memory_manager.append_missing_messages(
                "张三",
                [{"time": "1", "attr": "friend", "sender": "张三", "type": "text", "content": "早"}],
                100,
            )
            chat = FakeChat(visible=[msg("微信界面消息", time="3")])
            local_messages = [
                SimpleNamespace(type="text", attr="friend", sender="张三", content="早", time="1"),
                SimpleNamespace(type="voice", attr="friend", sender="张三", content="一条语音消息（未识别出文字）", time="2"),
            ]

            with patch("wxbot_core.read_local_history_messages_with_status") as read_local:
                read_local.return_value = SimpleNamespace(ok=True, items=local_messages, error="")
                repaired = bot._repair_private_context_before_ai(chat, msg("新内容", time="3"))

            self.assertTrue(repaired)
            self.assertEqual(read_local.call_args.kwargs["limit"], 60)
            self.assertEqual(lock.acquire_calls, [])
            self.assertEqual(chat.get_all_calls, 0)
            self.assertEqual(
                [item["content"] for item in bot.memory_manager.get_messages("张三", 10)],
                ["早"],
            )

    def test_successful_cli_repair_logs_only_added_count(self):
        with tempfile.TemporaryDirectory() as tmp:
            bot = self.make_bot(tmp, local_reader_enabled=True)
            bot.memory_manager.append_missing_messages(
                "张三",
                [{"time": "1", "attr": "friend", "sender": "张三", "type": "text", "content": "旧锚点"}],
                100,
            )
            chat = FakeChat(visible=[msg("微信界面消息", time="2")])
            local_messages = [
                msg("旧锚点", time="1"),
                msg("CLI 补入消息", time="2"),
            ]

            with patch("wxbot_core.read_local_history_messages_with_status") as read_local, \
                    patch("wxbot_core.log") as log_mock:
                read_local.return_value = SimpleNamespace(ok=True, items=local_messages, error="", diagnostic={})
                repaired = bot._repair_private_context_before_ai(chat, msg("当前消息", time="3"))

            self.assertTrue(repaired)
            messages = [call.kwargs.get("message", "") for call in log_mock.call_args_list]
            self.assertEqual(messages, ["私聊 张三：上下文 CLI 补洞完成，补入 1 条"])
            self.assertFalse(any("读取" in message or "诊断" in message for message in messages))

    def test_first_cli_final_failure_skips_ui_fallback_until_next_needed_attempt(self):
        with tempfile.TemporaryDirectory() as tmp:
            lock = FakeLock()
            bot = self.make_bot(tmp, lock=lock, local_reader_enabled=True)
            bot.memory_manager.append_missing_messages(
                "张三",
                [{"time": "1", "attr": "friend", "sender": "张三", "type": "text", "content": "旧锚点"}],
                100,
            )
            chat = FakeChat(visible=[msg("微信界面消息", time="2")])

            with patch("wxbot_core.read_local_history_messages_with_status") as read_local:
                read_local.return_value = SimpleNamespace(ok=False, items=[], error="boom", diagnostic={})
                repaired = bot._repair_private_context_before_ai(chat, msg("当前消息", time="3"))

            self.assertFalse(repaired)
            self.assertEqual(lock.acquire_calls, [])
            self.assertEqual(chat.get_all_calls, 0)
            self.assertEqual(bot._memory_context_repair_cli_failures["private:张三"], 1)

    def test_second_consecutive_cli_final_failure_uses_low_risk_ui_fallback(self):
        with tempfile.TemporaryDirectory() as tmp:
            lock = FakeLock()
            bot = self.make_bot(tmp, lock=lock, local_reader_enabled=True)
            bot._memory_context_repair_cli_failures["private:张三"] = 1
            bot.memory_manager.append_missing_messages(
                "张三",
                [{"time": "1", "attr": "friend", "sender": "张三", "type": "text", "content": "旧锚点"}],
                100,
            )
            chat = FakeChat(visible=[
                msg("旧锚点", time="1"),
                msg("微信界面消息", time="2"),
            ])

            with patch("wxbot_core.read_local_history_messages_with_status") as read_local:
                read_local.return_value = SimpleNamespace(ok=False, items=[], error="boom", diagnostic={})
                repaired = bot._repair_private_context_before_ai(chat, msg("当前消息", time="3"))

            self.assertTrue(repaired)
            self.assertEqual(lock.acquire_calls, [False])
            self.assertEqual(chat.get_all_calls, 1)
            self.assertNotIn("private:张三", bot._memory_context_repair_cli_failures)
            self.assertEqual(
                [item["content"] for item in bot.memory_manager.get_messages("张三", 10)],
                ["旧锚点", "微信界面消息"],
            )

    def test_cli_failure_with_ui_fallback_logs_reason_and_fallback_result(self):
        with tempfile.TemporaryDirectory() as tmp:
            lock = FakeLock()
            bot = self.make_bot(tmp, lock=lock, local_reader_enabled=True)
            bot._memory_context_repair_cli_failures["private:张三"] = 1
            bot.memory_manager.append_missing_messages(
                "张三",
                [{"time": "1", "attr": "friend", "sender": "张三", "type": "text", "content": "旧锚点"}],
                100,
            )
            chat = FakeChat(visible=[
                msg("旧锚点", time="1"),
                msg("微信界面消息", time="2"),
            ])

            with patch("wxbot_core.read_local_history_messages_with_status") as read_local, \
                    patch("wxbot_core.log") as log_mock:
                read_local.return_value = SimpleNamespace(ok=False, items=[], error="boom", diagnostic={})
                repaired = bot._repair_private_context_before_ai(chat, msg("当前消息", time="3"))

            self.assertTrue(repaired)
            calls = [(call.kwargs.get("level", "INFO"), call.kwargs.get("message", "")) for call in log_mock.call_args_list]
            self.assertEqual(
                calls,
                [
                    ("WARNING", "私聊 张三：上下文 CLI 补洞失败，原因：boom；已切换微信 UI 兜底"),
                    ("INFO", "私聊 张三：上下文 UI 兜底补洞完成，补入 1 条"),
                ],
            )

    def test_cli_failure_reason_uses_readable_chinese(self):
        with tempfile.TemporaryDirectory() as tmp:
            lock = FakeLock()
            bot = self.make_bot(tmp, lock=lock, local_reader_enabled=True)
            bot.memory_manager.append_missing_messages(
                "张三",
                [{"time": "1", "attr": "friend", "sender": "张三", "type": "text", "content": "旧锚点"}],
                100,
            )
            chat = FakeChat(visible=[msg("微信界面消息", time="2")])

            with patch("wxbot_core.read_local_history_messages_with_status") as read_local, \
                    patch("wxbot_core.log") as log_mock:
                read_local.return_value = SimpleNamespace(
                    ok=False,
                    items=[],
                    error="wechat-cli config not initialized",
                    diagnostic={},
                )
                repaired = bot._repair_private_context_before_ai(chat, msg("当前消息", time="3"))

            self.assertFalse(repaired)
            messages = [call.kwargs.get("message", "") for call in log_mock.call_args_list]
            self.assertEqual(
                messages,
                ["私聊 张三：上下文 CLI 补洞失败，原因：wechat-cli 尚未初始化；等待下次补洞再启用微信 UI 兜底"],
            )

    def test_successful_repair_sets_three_hundred_second_ttl(self):
        with tempfile.TemporaryDirectory() as tmp:
            bot = self.make_bot(tmp)
            bot.memory_manager.append_missing_messages(
                "张三",
                [{"time": "1", "attr": "friend", "sender": "张三", "type": "text", "content": "旧锚点"}],
                100,
            )
            chat = FakeChat(visible=[
                msg("旧锚点", time="1"),
                msg("新内容", time="2"),
            ])

            repaired = bot._repair_private_context_before_ai(chat, msg("新内容", time="2"))

            self.assertTrue(repaired)
            self.assertIn("private:张三", bot._memory_context_repair_last_low_risk_at)
            self.assertFalse(bot._context_repair_success_ttl_allows("private:张三", 300))

    def test_local_context_repair_limit_follows_context_count_bounds(self):
        with tempfile.TemporaryDirectory() as tmp:
            low = self.make_bot(tmp, local_reader_enabled=True, memory_context_count=10)
            default = self.make_bot(tmp, local_reader_enabled=True, memory_context_count=50)
            high = self.make_bot(tmp, local_reader_enabled=True, memory_context_count=190)
            maxed = self.make_bot(tmp, local_reader_enabled=True, memory_context_count=200)

            self.assertEqual(low._local_context_repair_limit(), 60)
            self.assertEqual(default._local_context_repair_limit(), 60)
            self.assertEqual(high._local_context_repair_limit(), 200)
            self.assertEqual(maxed._local_context_repair_limit(), 210)

    def test_local_history_without_anchor_still_allows_high_risk_fallback(self):
        with tempfile.TemporaryDirectory() as tmp:
            lock = FakeLock()
            bot = self.make_bot(tmp, high_enabled=True, lock=lock, local_reader_enabled=True)
            saved_directories = []
            bot._load_contact_profiles_directory = lambda: ({"subjects": [], "maintenance": {}}, "ignored.json", "scope_rui")
            bot._save_contact_profiles_directory = lambda directory: saved_directories.append(directory)
            bot.memory_manager.append_missing_messages(
                "张三",
                [{"time": "1", "attr": "friend", "sender": "张三", "type": "text", "content": "旧锚点"}],
                100,
            )
            chat = FakeChat(
                visible=[msg("微信界面消息", time="3")],
                history=[msg("旧锚点", time="1"), msg("中间", time="2"), msg("新内容", time="3")],
            )
            local_messages = [
                SimpleNamespace(type="text", attr="friend", sender="张三", content="另一个聊天窗口", time="9"),
            ]

            with patch("wxbot_core.read_local_history_messages_with_status") as read_local:
                read_local.return_value = SimpleNamespace(
                    ok=True,
                    items=local_messages,
                    error="",
                    diagnostic={"history_target": "wxid_wrong"},
                )
                repaired = bot._repair_private_context_before_ai(chat, msg("新内容", time="3"))

            self.assertTrue(repaired)
            self.assertEqual(saved_directories, [])
            self.assertEqual(chat.history_args["limit"], 50)
            self.assertEqual(lock.acquire_calls, [False])
            self.assertEqual(
                [item["content"] for item in bot.memory_manager.get_messages("张三", 10)],
                ["旧锚点", "中间", "新内容"],
            )

    def test_local_history_marks_wxid_verified_only_after_anchor_match(self):
        with tempfile.TemporaryDirectory() as tmp:
            bot = self.make_bot(tmp, local_reader_enabled=True)
            saved_directories = []
            bot._load_contact_profiles_directory = lambda: (
                {
                    "subjects": [{
                        "subject_type": "friend",
                        "status": "active",
                        "contact_key": "wechat_id:wxid_zhangsan",
                        "wechat_id": "wxid_zhangsan",
                        "wxid": "wxid_zhangsan",
                        "remark": "张三",
                        "nickname": "张三",
                        "display_name": "张三",
                        "send_name": "张三",
                        "warnings": [],
                        "raw_detail": {"wxid": "wxid_zhangsan", "备注": "张三"},
                    }],
                    "maintenance": {},
                },
                "ignored.json",
                "scope_rui",
            )
            bot._save_contact_profiles_directory = lambda directory: saved_directories.append(directory)
            bot.memory_manager.append_missing_messages(
                "张三",
                [{"time": "1", "attr": "friend", "sender": "张三", "type": "text", "content": "早"}],
                100,
            )
            chat = FakeChat(visible=[msg("微信界面消息", time="3")])
            local_messages = [
                SimpleNamespace(type="text", attr="friend", sender="张三", content="早", time="1"),
                SimpleNamespace(type="text", attr="friend", sender="张三", content="新内容", time="2"),
            ]

            with patch("wxbot_core.read_local_history_messages_with_status") as read_local:
                read_local.return_value = SimpleNamespace(
                    ok=True,
                    items=local_messages,
                    error="",
                    diagnostic={"history_target": "wxid_zhangsan"},
                )
                repaired = bot._repair_private_context_before_ai(chat, msg("新内容", time="2"))

            self.assertTrue(repaired)
            self.assertEqual(saved_directories[-1]["subjects"][0]["wxid_status"], "verified")
            self.assertEqual(saved_directories[-1]["subjects"][0]["last_history_success_at"], saved_directories[-1]["updated_at"])

    def test_high_risk_reads_history_when_enabled_and_low_risk_has_no_anchor(self):
        with tempfile.TemporaryDirectory() as tmp:
            lock = FakeLock()
            bot = self.make_bot(tmp, high_enabled=True, lock=lock)
            bot.memory_manager.append_missing_messages(
                "张三",
                [{"time": "1", "attr": "friend", "sender": "张三", "type": "text", "content": "旧锚点"}],
                100,
            )
            chat = FakeChat(
                visible=[msg("新内容", time="3")],
                history=[msg("旧锚点", time="1"), msg("中间", time="2"), msg("新内容", time="3")],
            )

            bot._repair_private_context_before_ai(chat, msg("新内容", time="3"))

            self.assertEqual(chat.history_args["limit"], 50)
            self.assertEqual(lock.acquire_calls, [False, False])
            self.assertTrue(lock.released)
            self.assertEqual(
                [item["content"] for item in bot.memory_manager.get_messages("张三", 10)],
                ["旧锚点", "中间", "新内容"],
            )

    def test_low_risk_lock_busy_skips_visible_read(self):
        with tempfile.TemporaryDirectory() as tmp:
            lock = FakeLock(acquired=False)
            bot = self.make_bot(tmp, high_enabled=True, lock=lock)
            bot.memory_manager.append_missing_messages(
                "张三",
                [{"time": "1", "attr": "friend", "sender": "张三", "type": "text", "content": "旧锚点"}],
                100,
            )
            chat = FakeChat(visible=[msg("新内容", time="3")], history=[msg("新内容", time="3")])

            bot._repair_private_context_before_ai(chat, msg("新内容", time="3"))

            self.assertEqual(lock.acquire_calls, [False])
            self.assertFalse(lock.released)
            self.assertEqual(chat.get_all_calls, 0)
            self.assertEqual([item["content"] for item in bot.memory_manager.get_messages("张三", 10)], ["旧锚点"])
            self.assertNotIn("张三", bot._memory_context_repair_startup_done)
            self.assertNotIn("张三", bot._memory_context_repair_last_low_risk_at)

    def test_high_risk_without_anchor_appends_read_messages_when_enabled(self):
        with tempfile.TemporaryDirectory() as tmp:
            bot = self.make_bot(tmp, high_enabled=True)
            bot.memory_manager.append_missing_messages(
                "张三",
                [{"time": "1", "attr": "friend", "sender": "张三", "type": "text", "content": "旧锚点"}],
                100,
            )
            chat = FakeChat(
                visible=[msg("新内容", time="3")],
                history=[msg("更深历史", time="2"), msg("新内容", time="3")],
            )

            repaired = bot._repair_private_context_before_ai(chat, msg("新内容", time="3"))

            self.assertTrue(repaired)
            self.assertEqual(chat.history_args["limit"], 50)
            self.assertEqual(
                [item["content"] for item in bot.memory_manager.get_messages("张三", 10)],
                ["旧锚点", "更深历史", "新内容"],
            )

    def test_high_risk_failure_falls_back_to_visible_messages(self):
        with tempfile.TemporaryDirectory() as tmp:
            bot = self.make_bot(tmp, high_enabled=True)
            bot.memory_manager.append_missing_messages(
                "张三",
                [{"time": "1", "attr": "friend", "sender": "张三", "type": "text", "content": "旧锚点"}],
                100,
            )
            chat = FailingHistoryChat(visible=[msg("新内容", time="3")])

            repaired = bot._repair_private_context_before_ai(chat, msg("新内容", time="3"))

            self.assertTrue(repaired)
            self.assertEqual([item["content"] for item in bot.memory_manager.get_messages("张三", 10)], ["旧锚点", "新内容"])

    def test_low_risk_without_anchor_and_high_risk_disabled_appends_visible(self):
        with tempfile.TemporaryDirectory() as tmp:
            bot = self.make_bot(tmp, high_enabled=False)
            bot.memory_manager.append_missing_messages(
                "张三",
                [{"time": "1", "attr": "friend", "sender": "张三", "type": "text", "content": "旧锚点"}],
                100,
            )
            chat = FakeChat(visible=[msg("新内容", time="3")])

            bot._repair_private_context_before_ai(chat, msg("新内容", time="3"))

            self.assertEqual([item["content"] for item in bot.memory_manager.get_messages("张三", 10)], ["旧锚点", "新内容"])

    def test_scheduled_low_risk_runs_after_cooldown_without_strong_reason(self):
        with tempfile.TemporaryDirectory() as tmp:
            bot = self.make_bot(tmp)
            bot.memory_manager.append_missing_messages(
                "张三",
                [
                    {"time": "2026/07/03 05:00:00", "attr": "friend", "sender": "张三", "type": "text", "content": "早"},
                    {"time": "2026/07/03 05:03:00", "attr": "friend", "sender": "张三", "type": "text", "content": "后来呢"},
                ],
                100,
            )
            bot._memory_context_repair_startup_done.add("张三")
            bot._memory_context_repair_last_low_risk_at["张三"] = 0
            chat = FakeChat(visible=[
                msg("早", time="2026/07/03 05:00:00"),
                msg("手机发的第一条", attr="self", sender="self", time="2026/07/03 05:01:00"),
                msg("手机发的第二条", attr="self", sender="self", time="2026/07/03 05:02:00"),
                msg("后来呢", time="2026/07/03 05:03:00"),
            ])

            repaired = bot._repair_private_context_before_ai(chat, msg("后来呢", time="2026/07/03 05:03:00"))

            self.assertTrue(repaired)
            self.assertEqual(
                [item["content"] for item in bot.memory_manager.get_messages("张三", 10)],
                ["早", "手机发的第一条", "手机发的第二条", "后来呢"],
            )

    def test_scheduled_low_risk_can_trigger_high_risk_when_enabled_and_no_anchor(self):
        with tempfile.TemporaryDirectory() as tmp:
            lock = FakeLock()
            bot = self.make_bot(tmp, high_enabled=True, lock=lock)
            bot.memory_manager.append_missing_messages(
                "张三",
                [
                    {"time": "2026/07/03 05:00:00", "attr": "friend", "sender": "张三", "type": "text", "content": "本地旧消息"},
                    {"time": "2026/07/03 05:10:00", "attr": "friend", "sender": "张三", "type": "text", "content": "当前消息"},
                ],
                100,
            )
            bot._memory_context_repair_startup_done.add("张三")
            bot._memory_context_repair_last_low_risk_at["张三"] = 0
            chat = FakeChat(
                visible=[msg("另一个可见消息", time="2026/07/03 05:11:00")],
                history=[
                    msg("本地旧消息", time="2026/07/03 05:00:00"),
                    msg("历史中间", time="2026/07/03 05:01:00"),
                    msg("当前消息", time="2026/07/03 05:10:00"),
                ],
            )

            bot._repair_private_context_before_ai(chat, msg("当前消息", time="2026/07/03 05:10:00"))

            self.assertEqual(lock.acquire_calls, [False, False])
            self.assertEqual(chat.history_args["limit"], 50)
            self.assertEqual(
                [item["content"] for item in bot.memory_manager.get_messages("张三", 10)],
                ["本地旧消息", "历史中间", "当前消息", "另一个可见消息"],
            )

    def test_group_configured_chat_does_not_repair(self):
        with tempfile.TemporaryDirectory() as tmp:
            bot = self.make_bot(tmp, high_enabled=True)
            bot.config.group = ["测试群"]
            bot.memory_manager.append_missing_messages(
                "测试群",
                [{"time": "1", "attr": "friend", "sender": "张三", "type": "text", "content": "旧锚点"}],
                100,
            )
            chat = FakeChat(visible=[msg("新内容", time="3")])
            chat.who = "测试群"

            repaired = bot._repair_private_context_before_ai(chat, msg("新内容", time="3"))

            self.assertFalse(repaired)
            self.assertEqual([item["content"] for item in bot.memory_manager.get_messages("测试群", 10)], ["旧锚点"])

    def test_group_chat_type_does_not_repair(self):
        with tempfile.TemporaryDirectory() as tmp:
            bot = self.make_bot(tmp, high_enabled=True)
            bot.memory_manager.append_missing_messages(
                "未配置群",
                [{"time": "1", "attr": "friend", "sender": "张三", "type": "text", "content": "旧锚点"}],
                100,
            )
            chat = FakeChat(visible=[msg("新内容", time="3")])
            chat.who = "未配置群"
            chat.chat_type = "group"

            repaired = bot._repair_private_context_before_ai(chat, msg("新内容", time="3"))

            self.assertFalse(repaired)
            self.assertEqual([item["content"] for item in bot.memory_manager.get_messages("未配置群", 10)], ["旧锚点"])

    def test_group_context_repair_uses_local_history_with_sender_anchors(self):
        with tempfile.TemporaryDirectory() as tmp:
            lock = FakeLock(acquired=False)
            bot = self.make_bot(tmp, lock=lock, local_reader_enabled=True)
            bot.config.group = ["测试群"]
            bot.memory_manager.append_missing_messages(
                "测试群",
                [
                    {
                        "time": "2026/07/04 08:00:00",
                        "attr": "group",
                        "sender": "A",
                        "type": "text",
                        "content": "收到",
                    },
                    {
                        "time": "2026/07/04 08:01:00",
                        "attr": "group",
                        "sender": "B",
                        "type": "text",
                        "content": "明白",
                    },
                ],
                100,
            )
            chat = FakeChat()
            chat.who = "测试群"
            chat.chat_type = "group"
            current = msg("新问题", attr="group", sender="C", time="2026/07/04 08:03:00")
            local_messages = [
                SimpleNamespace(type="text", attr="friend", sender="A", content="收到", time="2026/07/04 08:00:00"),
                SimpleNamespace(type="text", attr="friend", sender="B", content="明白", time="2026/07/04 08:01:00"),
                SimpleNamespace(type="text", attr="friend", sender="C", content="新问题", time="2026/07/04 08:03:00"),
            ]

            with patch("wxbot_core.read_local_history_messages_with_status") as read_local:
                read_local.return_value = SimpleNamespace(ok=True, items=local_messages, error="")
                repaired = bot._repair_group_context_before_ai(chat, current)

            self.assertTrue(repaired)
            self.assertEqual(read_local.call_args.kwargs["chat_type"], "group")
            self.assertEqual(lock.acquire_calls, [])
            self.assertEqual(
                [(item["sender"], item["content"]) for item in bot.memory_manager.get_messages("测试群", 10)],
                [("A", "收到"), ("B", "明白"), ("C", "新问题")],
            )

    def test_group_context_repair_skips_append_without_anchor(self):
        with tempfile.TemporaryDirectory() as tmp:
            bot = self.make_bot(tmp, local_reader_enabled=True)
            bot.config.group = ["测试群"]
            bot.memory_manager.append_missing_messages(
                "测试群",
                [{"time": "1", "attr": "group", "sender": "A", "type": "text", "content": "旧锚点"}],
                100,
            )
            chat = FakeChat()
            chat.who = "测试群"
            chat.chat_type = "group"
            local_messages = [
                SimpleNamespace(type="text", attr="friend", sender="X", content="另一个群", time="9"),
                SimpleNamespace(type="text", attr="friend", sender="Y", content="新内容", time="10"),
            ]

            with patch("wxbot_core.read_local_history_messages_with_status") as read_local:
                read_local.return_value = SimpleNamespace(ok=True, items=local_messages, error="")
                repaired = bot._repair_group_context_before_ai(chat, msg("新内容", attr="group", sender="Y", time="10"))

            self.assertFalse(repaired)
            self.assertEqual(
                [(item["sender"], item["content"]) for item in bot.memory_manager.get_messages("测试群", 10)],
                [("A", "旧锚点")],
            )


if __name__ == "__main__":
    unittest.main()
