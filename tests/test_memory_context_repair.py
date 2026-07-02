import tempfile
import unittest
from types import SimpleNamespace

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
        self.ChatBox = SimpleNamespace(get_msgs_from_history=self.get_msgs_from_history)

    def GetAllMessage(self):
        return self.visible

    def get_msgs_from_history(self, limit, callback=None, interval=0.2, speed=5, goback=True):
        self.history_args = {
            "limit": limit,
            "interval": interval,
            "speed": speed,
            "goback": goback,
        }
        return self.history[:limit]


class WXBotContextRepairTests(unittest.TestCase):
    def make_bot(self, tmp, *, high_enabled=False, lock=None):
        bot = WXBot.__new__(WXBot)
        bot.config = SimpleNamespace(
            memory_switch=True,
            memory_context_switch=True,
            memory_max_count=100,
            memory_context_repair_low_risk_switch=True,
            memory_context_repair_high_risk_switch=high_enabled,
        )
        bot.memory_manager = MemoryManager("wxid", tmp)
        bot._resolve_identity_chat_name = lambda name: name
        bot._mark_chat_memory_dirty = lambda *args, **kwargs: None
        bot.is_stop_requested = lambda: False
        bot._wechat_action_lock = lock or FakeLock()
        bot._get_wechat_action_lock = lambda: bot._wechat_action_lock
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

    def test_high_risk_uses_history_reader_when_low_risk_has_no_anchor(self):
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

            self.assertEqual(chat.history_args, {"limit": 50, "interval": 0.2, "speed": 5, "goback": True})
            self.assertEqual(lock.acquire_calls, [False])
            self.assertTrue(lock.released)
            self.assertEqual(
                [item["content"] for item in bot.memory_manager.get_messages("张三", 10)],
                ["旧锚点", "中间", "新内容"],
            )

    def test_high_risk_lock_busy_does_not_block_or_append(self):
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
            self.assertEqual([item["content"] for item in bot.memory_manager.get_messages("张三", 10)], ["旧锚点"])

    def test_low_risk_without_anchor_and_high_risk_disabled_does_not_append(self):
        with tempfile.TemporaryDirectory() as tmp:
            bot = self.make_bot(tmp, high_enabled=False)
            bot.memory_manager.append_missing_messages(
                "张三",
                [{"time": "1", "attr": "friend", "sender": "张三", "type": "text", "content": "旧锚点"}],
                100,
            )
            chat = FakeChat(visible=[msg("新内容", time="3")])

            bot._repair_private_context_before_ai(chat, msg("新内容", time="3"))

            self.assertEqual([item["content"] for item in bot.memory_manager.get_messages("张三", 10)], ["旧锚点"])

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


if __name__ == "__main__":
    unittest.main()
