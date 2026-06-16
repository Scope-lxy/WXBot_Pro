import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from core.daily_runtime_stats import DailyRuntimeStatsStore
from feature.admin_status import build_status_message
from wxbot_core import WXBot


class DailyRuntimeStatsTests(unittest.TestCase):
    def test_api_request_fields_default_to_zero(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = DailyRuntimeStatsStore(Path(tmp) / "stats.json")

            stats = store.load(now="2026-06-14T10:00:00")

        self.assertEqual(stats["chat_api_requests"], 0)
        self.assertEqual(stats["other_api_requests"], 0)
        self.assertEqual(stats["private_voice_replies"], 0)
        self.assertEqual(stats["group_voice_replies"], 0)
        self.assertEqual(stats["private_split_replies"], 0)
        self.assertEqual(stats["group_split_replies"], 0)
        self.assertEqual(stats["private_merged_messages"], 0)

    def test_api_request_increment_persists_by_day(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = DailyRuntimeStatsStore(Path(tmp) / "stats.json")

            store.increment("chat_api_requests", now="2026-06-14T10:00:00")
            stats = store.increment("other_api_requests", amount=2, now="2026-06-14T11:00:00")

        self.assertEqual(stats["chat_api_requests"], 1)
        self.assertEqual(stats["other_api_requests"], 2)

    def test_reply_behavior_increment_persists_by_day(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = DailyRuntimeStatsStore(Path(tmp) / "stats.json")

            store.increment("private_voice_replies", now="2026-06-14T10:00:00")
            store.increment("group_voice_replies", amount=2, now="2026-06-14T10:01:00")
            store.increment("private_split_replies", amount=3, now="2026-06-14T10:02:00")
            store.increment("group_split_replies", amount=4, now="2026-06-14T10:03:00")
            stats = store.increment("private_merged_messages", amount=5, now="2026-06-14T10:04:00")

        self.assertEqual(stats["private_voice_replies"], 1)
        self.assertEqual(stats["group_voice_replies"], 2)
        self.assertEqual(stats["private_split_replies"], 3)
        self.assertEqual(stats["group_split_replies"], 4)
        self.assertEqual(stats["private_merged_messages"], 5)


class ApiRequestCounterTests(unittest.TestCase):
    def test_chat_api_counter_preserves_return_value(self):
        bot = WXBot.__new__(WXBot)
        calls = []
        bot._increment_daily_runtime_stat = lambda key, amount=1, now=None: calls.append((key, amount))
        api = SimpleNamespace(chat=lambda message: f"回复：{message}")

        result = bot._wrap_api_request_counter(api, "chat").chat("你好")

        self.assertEqual(result, "回复：你好")
        self.assertEqual(calls, [("chat_api_requests", 1)])

    def test_other_api_counter_preserves_return_value(self):
        bot = WXBot.__new__(WXBot)
        calls = []
        bot._increment_daily_runtime_stat = lambda key, amount=1, now=None: calls.append((key, amount))
        api = SimpleNamespace(chat=lambda message: f"结果：{message}")

        result = bot._wrap_api_request_counter(api, "other").chat("生成文案")

        self.assertEqual(result, "结果：生成文案")
        self.assertEqual(calls, [("other_api_requests", 1)])


class StatusSnapshotTests(unittest.TestCase):
    def test_admin_status_message_uses_operational_summary_template(self):
        bot = SimpleNamespace(
            start_time=__import__("datetime").datetime.now(),
            msg_received_count=0,
            msg_replied_count=0,
            _pause_chat_reply_users=set(),
            _ai_outreach_available_material_count=5,
        )
        bot.config = SimpleNamespace(
            get_run_time=lambda _start_time: "0天0时30分23秒",
            default_prompt="瑞东-知己-暧昧型",
            scheduled_message_task_list=[],
            material_outreach_list=[],
        )
        bot.get_daily_runtime_stats = lambda: {
            "received_messages": 1258,
            "replied_messages": 1215,
            "chat_api_requests": 1500,
            "other_api_requests": 67,
            "scheduled_messages_sent": 0,
            "material_forwards_sent": 0,
            "ai_material_forwards_sent": 0,
            "moments_published": 0,
        }
        bot._get_current_chat_api_display_name = lambda: "接口 4（mimo-v2.5）"

        message = build_status_message(bot)

        self.assertEqual(message, "\n".join([
            "机器人状态",
            "",
            "运行时间：0天0时30分23秒",
            "当前接口：接口 4（mimo-v2.5）",
            "当前人设：瑞东-知己-暧昧型",
            "---",
            "API请求：1567 次",
            "已收消息：1258 条",
            "已回复消息：1215 次",
            "人工接管对话：0 个（无）",
            "---",
            "发朋友圈：0次",
            "定时消息：0 次（任务规则：0）",
            "素材转发：0 次（任务规则：0）",
            "自动转发：0 次（可用素材：5）",
        ]))

    def test_runtime_status_uses_daily_message_counts(self):
        bot = WXBot.__new__(WXBot)
        bot.run_flag = True
        bot.start_time = __import__("datetime").datetime.now()
        bot.wx = None
        bot.ver = "test"
        bot.msg_received_count = 99
        bot.msg_replied_count = 88
        bot.last_msg_time = ""
        bot.last_msg_sender = ""
        bot.callback_is_die = False
        bot._pause_chat_reply = False
        bot._pause_group_reply = False
        bot.config = SimpleNamespace(
            api_configs=[],
            group=[],
            global_blacklist=[],
            listen_list=[],
            AllListen_switch=False,
            group_switch=False,
            chat_keyword_switch=False,
            group_keyword_switch=False,
            group_keyword_at_only=False,
            keyword_dict={},
            scheduled_message_task_list=[],
        )
        bot.get_daily_runtime_stats = lambda: {
            "received_messages": 3,
            "replied_messages": 2,
            "chat_api_requests": 5,
            "other_api_requests": 7,
            "private_voice_replies": 11,
            "group_voice_replies": 12,
            "private_split_replies": 13,
            "group_split_replies": 14,
            "private_merged_messages": 15,
        }
        bot._get_current_chat_api_display_name = lambda: "未连接"
        bot._get_active_default_chat_api_index = lambda: 0

        status = bot.get_status()

        self.assertEqual(status["msg_received"], 3)
        self.assertEqual(status["msg_replied"], 2)
        self.assertEqual(status["api_request_count"], 12)
        self.assertEqual(status["private_voice_replies"], 11)
        self.assertEqual(status["group_voice_replies"], 12)
        self.assertEqual(status["private_split_replies"], 13)
        self.assertEqual(status["group_split_replies"], 14)
        self.assertEqual(status["private_merged_messages"], 15)


class MomentsCandidateTests(unittest.TestCase):
    def test_admin_moments_multi_image_uses_chat_image_paths(self):
        bot = WXBot.__new__(WXBot)
        bot.config = SimpleNamespace(api_configs=[{"model": "gpt-test"}], moments_api_index=0, cmd="管理员")
        bot._resolve_admin_moments_api_index = lambda: 0
        bot._get_other_api = lambda _index=None: SimpleNamespace(
            chat=lambda message, **kwargs: kwargs.get("image_paths") and "候选文案"
        )
        bot._build_admin_moments_generation_prompt = lambda _draft: "prompt"
        bot._get_admin_moments_raw_text = lambda _draft: "原始文案"
        bot._parse_admin_moments_candidates = lambda raw: raw

        result = bot._generate_admin_moments_candidates({"images": ["a.png", "b.png"]})

        self.assertEqual(result, "候选文案")


if __name__ == "__main__":
    unittest.main()
