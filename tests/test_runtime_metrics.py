import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from core.runtime_metrics import RuntimeMetricsStore
from feature.admin_status import build_status_message
from wxbot_core import WXBot


class RuntimeMetricsStoreTests(unittest.TestCase):
    def test_hourly_and_daily_series_aggregate_core_metrics(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = RuntimeMetricsStore(Path(tmp) / "runtime_metrics.json")

            store.increment("received_messages", amount=4, now="2026-06-20T10:10:00")
            store.increment("api_calls", amount=2, now="2026-06-20T10:15:00")
            store.increment("reply_count", now="2026-06-20T10:20:00")
            store.add_unique("active_private_chats", "阿英2", now="2026-06-20T10:21:00")
            store.add_unique("active_private_chats", "阿英2", now="2026-06-20T11:21:00")
            store.add_unique("active_group_chats", "群1", now="2026-06-20T11:22:00")
            store.increment("keyword_reply_messages", amount=3, now="2026-06-20T11:25:00")
            store.increment("keyword_reply_triggers", now="2026-06-20T11:25:00")
            store.add_unique("keyword_private_targets", "阿英2", now="2026-06-20T11:25:00")
            store.add_unique("keyword_group_targets", "群1", now="2026-06-20T11:25:00")
            store.increment("material_success_count", now="2026-06-20T11:30:00")
            store.add_unique("material_success_targets", "阿英2", now="2026-06-20T11:30:00")
            store.increment("new_friend_accepted_count", now="2026-06-20T11:40:00")
            payload = store.series_payload(now="2026-06-20T12:00:00", days=1)

        today = payload["today"]
        self.assertEqual(today["received_messages"], 4)
        self.assertEqual(today["api_calls"], 2)
        self.assertEqual(today["reply_count"], 1)
        self.assertEqual(today["private_active_count"], 1)
        self.assertEqual(today["group_active_count"], 1)
        self.assertEqual(today["keyword_reply_messages"], 3)
        self.assertEqual(today["keyword_reply_triggers"], 1)
        self.assertEqual(today["keyword_private_target_count"], 1)
        self.assertEqual(today["keyword_group_target_count"], 1)
        self.assertEqual(today["material_success_count"], 1)
        self.assertEqual(today["material_success_target_count"], 1)
        self.assertEqual(today["new_friend_accepted_count"], 1)

    def test_set_today_relationship_counts_replaces_today_values(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = RuntimeMetricsStore(Path(tmp) / "runtime_metrics.json")

            store.set_today_counts(
                {"relationship_blocked_today": 3, "relationship_deleted_today": 2},
                now="2026-06-20T09:00:00",
            )
            store.set_today_counts(
                {"relationship_blocked_today": 4, "relationship_deleted_today": 1},
                now="2026-06-20T11:00:00",
            )
            payload = store.series_payload(now="2026-06-20T12:00:00", days=1)

        self.assertEqual(payload["today"]["relationship_blocked_today"], 4)
        self.assertEqual(payload["today"]["relationship_deleted_today"], 1)

    def test_corrupt_file_returns_empty_payload(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "runtime_metrics.json"
            path.write_text("{broken", encoding="utf-8")
            payload = RuntimeMetricsStore(path).series_payload(now="2026-06-20T12:00:00", days=1)

        self.assertEqual(payload["status"], "success")
        self.assertEqual(payload["today"]["api_calls"], 0)

    def test_default_retention_keeps_monthly_daily_metrics(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = RuntimeMetricsStore(Path(tmp) / "runtime_metrics.json")

            store.increment("api_calls", now="2026-05-24T10:00:00")
            store.increment("api_calls", now="2026-06-22T10:00:00")
            payload = store.series_payload(now="2026-06-22T12:00:00", days=30)

        self.assertEqual(payload["daily"][0]["key"], "2026-05-24")
        self.assertEqual(payload["daily"][0]["api_calls"], 1)
        self.assertEqual(payload["today"]["api_calls"], 1)


class RuntimeMetricsBotTests(unittest.TestCase):
    def test_metric_failures_do_not_escape_record_reply(self):
        bot = WXBot.__new__(WXBot)
        bot.msg_replied_count = 0
        class BrokenStore:
            def increment(self, *_args, **_kwargs):
                raise RuntimeError("boom")

            def add_unique(self, *_args, **_kwargs):
                raise RuntimeError("boom")

        bot._runtime_metrics_store = lambda: BrokenStore()

        bot._record_replied_message_success("阿英2")

        self.assertEqual(bot.msg_replied_count, 1)

    def test_api_counters_write_runtime_metrics_only(self):
        bot = WXBot.__new__(WXBot)
        calls = []
        bot._metric_increment = lambda key, amount=1, now=None: calls.append((key, amount))
        api = SimpleNamespace(chat=lambda message: f"回复：{message}")

        result = bot._wrap_api_request_counter(api, "chat").chat("你好")

        self.assertEqual(result, "回复：你好")
        self.assertEqual(calls, [("api_calls", 1), ("chat_api_calls", 1)])

    def test_ai_outreach_preface_counts_one_api_call(self):
        bot = WXBot.__new__(WXBot)
        calls = []
        bot._metric_increment = lambda key, amount=1, now=None: calls.append((key, amount))
        bot._metric_add_unique = lambda *_args, **_kwargs: None
        bot.config = SimpleNamespace(memory_context_count=20)
        bot.memory_manager = None
        bot._build_material_outreach_preface_prompt = lambda *_args, **_kwargs: "prompt"
        bot._get_other_api = lambda: bot._wrap_api_request_counter(
            SimpleNamespace(chat=lambda *_args, **_kwargs: "附加文案"),
            "other",
        )
        bot._parse_material_outreach_preface_reply = lambda reply: reply

        result = bot._generate_material_outreach_ai_preface(
            {},
            target="阿英2",
            material={"content_preview": "素材内容"},
            send_mode="ai_chat_outreach",
        )

        self.assertEqual(result, "附加文案")
        self.assertEqual(calls, [("ai_outreach_preface_api_calls", 1), ("api_calls", 1)])

    def test_tts_request_counts_api_only(self):
        bot = WXBot.__new__(WXBot)
        calls = []
        bot._metric_increment = lambda key, amount=1, now=None: calls.append((key, amount))

        bot._record_tts_api_request()

        self.assertEqual(calls, [("api_calls", 1)])

    def test_two_stage_image_reply_counts_recognition_as_chat_request(self):
        bot = WXBot.__new__(WXBot)
        calls = []
        bot._metric_increment = lambda key, amount=1, now=None: calls.append((key, amount))
        bot._get_api_instance_by_index = lambda _idx: SimpleNamespace(chat=lambda *_args, **_kwargs: "识别结果")
        bot._get_primary_chat_api_index = lambda: 0
        bot._get_chat_api_index = lambda _chat_name: 0
        bot._get_group_api_index = lambda _chat_name: 0
        bot._remember_visual_notes = lambda *_args, **_kwargs: None

        class FakeImagePipeline:
            def reply(self, request):
                recognition_result = request.recognition_api.chat("识别")
                final_result = request.final_api.chat("回复")
                return f"{recognition_result}/{final_result}"

        bot._get_image_reply_pipeline = lambda: FakeImagePipeline()
        final_api = bot._wrap_api_request_counter(
            SimpleNamespace(chat=lambda *_args, **_kwargs: "最终回复"),
            "chat",
        )

        result = bot._reply_image_message(
            chat_name="阿英2",
            chat_type="private",
            history=[],
            final_api=final_api,
            final_api_supports_vision=False,
            recognition_api_index=1,
            image_path="C:/tmp/a.png",
        )

        self.assertEqual(result, "识别结果/最终回复")
        self.assertEqual(calls, [
            ("image_api_calls", 1),
            ("api_calls", 1),
            ("chat_api_calls", 1),
            ("api_calls", 1),
            ("chat_api_calls", 1),
        ])

    def test_runtime_status_uses_runtime_metrics_today(self):
        bot = WXBot.__new__(WXBot)
        bot.run_flag = True
        bot.start_time = __import__("datetime").datetime.now()
        bot.wx = None
        bot.ver = "test"
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
        bot.runtime_metrics_today = lambda: {
            "received_messages": 3,
            "reply_count": 2,
            "api_calls": 12,
            "chat_api_calls": 5,
        }
        bot._get_current_chat_api_display_name = lambda: "未连接"
        bot._get_active_default_chat_api_index = lambda: 0

        status = bot.get_status()

        self.assertEqual(status["msg_received"], 3)
        self.assertEqual(status["msg_replied"], 2)
        self.assertEqual(status["api_request_count"], 12)
        self.assertEqual(status["chat_api_requests"], 5)
        self.assertEqual(status["other_api_requests"], 7)

    def test_admin_status_message_uses_runtime_metrics_today(self):
        bot = SimpleNamespace(
            start_time=__import__("datetime").datetime.now(),
            _pause_chat_reply_users=set(),
            _ai_outreach_available_material_count=5,
        )
        bot.config = SimpleNamespace(
            get_run_time=lambda _start_time: "0天0时30分23秒",
            default_prompt="瑞东-知己-暧昧型",
            scheduled_message_task_list=[],
            material_outreach_list=[],
        )
        bot.runtime_metrics_today = lambda: {
            "received_messages": 1258,
            "reply_count": 1215,
            "api_calls": 1567,
            "chat_api_calls": 1500,
            "scheduled_fixed_success_targets": 7,
            "scheduled_random_success_targets": 2,
            "material_success_count": 0,
            "ai_material_success_count": 0,
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
            "定时消息：9 次（任务规则：0）",
            "素材转发：0 次（任务规则：0）",
            "自动转发：0 次（可用素材：5）",
        ]))

    def test_closing_reply_uses_chat_request_counter(self):
        bot = WXBot.__new__(WXBot)
        calls = []
        bot._metric_increment = lambda key, amount=1, now=None: calls.append((key, amount))
        bot._resolve_chat_api_selection = lambda _user_name: (0, True)
        bot._get_api_instance_by_index = lambda _idx: SimpleNamespace(chat=lambda *_args, **_kwargs: "先这样啦")
        bot._wrap_chat_api_for_failover = lambda api, **_kwargs: bot._wrap_api_request_counter(api, "chat")
        bot._build_text_reply_limit_ai_prompt = lambda _chat_name: "closing prompt"
        bot._text_reply_limit_history = lambda _chat_name: []

        reply = bot._generate_text_reply_limit_reply(
            SimpleNamespace(who="张三"),
            SimpleNamespace(content="继续聊"),
        )

        self.assertEqual(reply, "先这样啦")
        self.assertEqual(calls, [("api_calls", 1), ("chat_api_calls", 1)])


if __name__ == "__main__":
    unittest.main()
