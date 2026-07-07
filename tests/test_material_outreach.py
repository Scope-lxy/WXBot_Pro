import unittest
from datetime import datetime, timedelta
from types import SimpleNamespace

from feature.runtime_task_runner import run_due_fixed_material_outreach
from feature.material_outreach import (
    collect_material_source_message,
    rebuild_material_pool_for_source,
)
from wxbot_core import WXBot


def msg(msg_type, content):
    return SimpleNamespace(type=msg_type, content=content)


class MaterialOutreachPoolTests(unittest.TestCase):
    def test_collect_refreshes_same_source_and_stable_signature(self):
        existing = {
            "id": "mat_old",
            "source": "文件传输助手",
            "type": "link",
            "type_bucket": "link",
            "content_preview": "[链接]相同标题",
            "stable_signature": "link|[链接]相同标题",
            "created_at": "2026-06-01T10:00:00",
            "status": "disabled",
            "ownership": "第三方作品",
            "copy_note": "旧备注",
            "forward_test_status": "failed",
            "last_error": "旧错误",
        }

        pool, entry, material_id = collect_material_source_message(
            [existing],
            "文件传输助手",
            msg("link", "[链接]相同标题"),
            material_id_factory=lambda: "mat_new",
            limit_map={"文件传输助手": 10},
        )

        self.assertEqual(material_id, "mat_old")
        self.assertEqual(len(pool), 1)
        self.assertEqual(entry["id"], "mat_old")
        self.assertEqual(entry["status"], "disabled")
        self.assertEqual(entry["ownership"], "第三方作品")
        self.assertEqual(entry["copy_note"], "旧备注")
        self.assertEqual(entry["forward_test_status"], "failed")
        self.assertEqual(entry["last_error"], "旧错误")

    def test_collect_keeps_same_signature_from_different_source(self):
        existing = {
            "id": "mat_other",
            "source": "其他来源",
            "type": "link",
            "type_bucket": "link",
            "content_preview": "[链接]相同标题",
            "stable_signature": "link|[链接]相同标题",
            "created_at": "2026-06-01T10:00:00",
            "status": "active",
        }

        pool, entry, material_id = collect_material_source_message(
            [existing],
            "文件传输助手",
            msg("link", "[链接]相同标题"),
            material_id_factory=lambda: "mat_new",
            limit_map={"文件传输助手": 10, "其他来源": 10},
        )

        self.assertEqual(material_id, "mat_new")
        self.assertEqual(len(pool), 2)
        self.assertEqual(entry["id"], "mat_new")

    def test_stable_signature_refresh_reuses_existing_material_identity(self):
        existing = {
            "id": "mat_old",
            "source": "文件传输助手",
            "type": "link",
            "type_bucket": "link",
            "content_preview": "[链接]相同标题",
            "stable_signature": "link|[链接]相同标题",
            "created_at": "2026-06-01T10:00:00",
            "status": "active",
            "ownership": "我的作品",
            "copy_note": "保留这条转发备注",
        }

        pool, entry, material_id = collect_material_source_message(
            [existing],
            "文件传输助手",
            msg("link", "[链接]相同标题"),
            material_id_factory=lambda: "mat_new",
            limit_map={"文件传输助手": 10},
        )

        self.assertEqual(material_id, "mat_old")
        self.assertEqual(entry["id"], "mat_old")
        self.assertEqual(entry["copy_note"], "保留这条转发备注")
        self.assertEqual([item["id"] for item in pool], ["mat_old"])

    def test_rebuild_keeps_latest_duplicate_message_and_old_material_id(self):
        first = msg("link", "[链接]相同标题")
        other = msg("miniapp", "小程序冷亦文集相同标题")
        latest = msg("link", "[链接]相同标题")
        existing = {
            "id": "mat_old",
            "source": "文件传输助手",
            "type": "link",
            "type_bucket": "link",
            "content_preview": "[链接]相同标题",
            "stable_signature": "link|[链接]相同标题",
            "created_at": "2026-06-01T10:00:00",
            "status": "active",
            "copy_note": "沿用备注",
        }

        pool, runtime_messages, rebuilt = rebuild_material_pool_for_source(
            [existing],
            "文件传输助手",
            [first, other, latest],
            limit=10,
            limit_map={"文件传输助手": 10},
            material_id_factory=iter(["mat_a", "mat_b", "mat_c"]).__next__,
        )

        self.assertEqual(len(rebuilt), 2)
        self.assertEqual([item["type"] for item in rebuilt], ["miniapp", "link"])
        self.assertEqual(rebuilt[-1]["id"], "mat_old")
        self.assertEqual(rebuilt[-1]["copy_note"], "沿用备注")
        self.assertIs(runtime_messages["mat_old"], latest)
        self.assertEqual([item["id"] for item in pool], ["mat_b", "mat_old"])

    def test_rebuild_uses_latest_existing_duplicate_metadata(self):
        older = {
            "id": "mat_older",
            "source": "文件传输助手",
            "type": "link",
            "type_bucket": "link",
            "content_preview": "[链接]相同标题",
            "stable_signature": "link|[链接]相同标题",
            "created_at": "2026-06-01T10:00:00",
            "status": "active",
            "copy_note": "旧卡片",
        }
        newer = {
            **older,
            "id": "mat_newer",
            "created_at": "2026-06-02T10:00:00",
            "status": "disabled",
            "copy_note": "新卡片",
        }

        _pool, runtime_messages, rebuilt = rebuild_material_pool_for_source(
            [older, newer],
            "文件传输助手",
            [msg("link", "[链接]相同标题")],
            limit=10,
            limit_map={"文件传输助手": 10},
            material_id_factory=lambda: "mat_fresh",
        )

        self.assertEqual(len(rebuilt), 1)
        self.assertEqual(rebuilt[0]["id"], "mat_newer")
        self.assertEqual(rebuilt[0]["status"], "disabled")
        self.assertEqual(rebuilt[0]["copy_note"], "新卡片")
        self.assertIn("mat_newer", runtime_messages)

    def test_material_outreach_batch_defers_when_wechat_lock_is_busy(self):
        bot = WXBot.__new__(WXBot)
        bot.config = SimpleNamespace(material_source_list=["素材源"])
        bot.is_stop_requested = lambda: False
        bot._material_runtime_messages = {"mat_1": object()}
        bot._load_material_outreach_materials = lambda: [
            {
                "id": "mat_1",
                "source": "素材源",
                "status": "active",
                "type": "link",
                "type_bucket": "link",
            }
        ]
        bot._append_material_skip_record = lambda *_args, **_kwargs: self.fail("锁忙时不应写跳过记录")
        bot._append_material_outreach_skip_progress = lambda *_args, **_kwargs: self.fail("锁忙时不应写进度")
        bot._send_material_outreach_action = lambda *_args, **_kwargs: self.fail("锁忙时不应进入批次发送")

        class BusyLock:
            def __init__(self):
                self.acquire_calls = []

            def acquire(self, blocking=True):
                self.acquire_calls.append(blocking)
                return False

            def release(self):
                self.fail("未拿到锁时不应释放")

        lock = BusyLock()
        bot._get_wechat_action_lock = lambda: lock

        result = bot._attempt_material_outreach_batches(
            {
                "task_id": "task_1",
                "targets": ["阿英2"],
                "material_types": ["all"],
                "batch_size_fixed": 1,
            },
            [],
            allow_rebuild=False,
        )

        self.assertEqual(result["status"], "deferred_lock_busy")
        self.assertEqual(lock.acquire_calls, [False])
        self.assertTrue(bot._material_outreach_is_deferred(result))
        self.assertFalse(bot._material_outreach_result_failed(result))

    def test_material_outreach_batch_runs_action_while_holding_wechat_lock(self):
        bot = WXBot.__new__(WXBot)
        bot.config = SimpleNamespace(material_source_list=["素材源"])
        bot.is_stop_requested = lambda: False
        bot._material_runtime_messages = {"mat_1": object()}
        bot._load_material_outreach_materials = lambda: [
            {
                "id": "mat_1",
                "source": "素材源",
                "status": "active",
                "type": "link",
                "type_bucket": "link",
            }
        ]
        bot._append_material_skip_record = lambda *_args, **_kwargs: None
        bot._append_material_outreach_skip_progress = lambda *_args, **_kwargs: None
        bot._flush_lightweight_send_queue = lambda: None

        class RecordingLock:
            def __init__(self):
                self.held = False
                self.events = []

            def acquire(self, blocking=True):
                self.events.append(("acquire", blocking))
                self.held = True
                return True

            def release(self):
                self.events.append(("release", self.held))
                self.held = False

        class SourceLock:
            def __init__(self, events):
                self.events = events

            def __enter__(self):
                self.events.append(("source_enter", True))
                return self

            def __exit__(self, exc_type, exc, tb):
                self.events.append(("source_exit", True))
                return False

        lock = RecordingLock()
        bot._get_wechat_action_lock = lambda: lock
        bot._get_material_source_read_lock = lambda _source: SourceLock(lock.events)
        action_events = []
        bot._send_material_outreach_action = (
            lambda *_args, **_kwargs: action_events.append(("action", lock.held)) or True
        )

        result = bot._attempt_material_outreach_batches(
            {
                "task_id": "task_1",
                "targets": ["阿英2"],
                "material_types": ["all"],
                "batch_size_fixed": 1,
            },
            [],
            allow_rebuild=False,
        )

        self.assertTrue(result)
        self.assertEqual(action_events, [("action", True)])
        self.assertEqual(
            lock.events,
            [("source_enter", True), ("acquire", False), ("release", True), ("source_exit", True)],
        )

    def test_fixed_material_runner_keeps_due_task_when_batch_is_deferred(self):
        now = datetime.now().replace(microsecond=0)
        raw_task = {
            "id": "task_1",
            "enabled": True,
            "trigger_strategy": "fixed",
            "targets": ["阿英2"],
            "next_fire_at": (now - timedelta(minutes=1)).isoformat(),
            "repeat_mode": "once",
        }
        disabled = []
        saved = []
        bot = SimpleNamespace()
        bot.config = SimpleNamespace(material_outreach_list=[raw_task])
        bot._compile_fixed_runtime_plan = lambda _task, now=None: {
            "next_fire_at": (now - timedelta(minutes=1)).isoformat(),
            "status": "pending",
            "repeat_mode": "once",
        }
        bot._sync_runtime_plan_fields = lambda *_args, **_kwargs: False
        bot._material_outreach_queue_time_due = lambda *_args, **_kwargs: False
        bot.send_material_outreach = lambda _task: {"status": "deferred_lock_busy"}
        bot._material_outreach_result_failed = lambda _result: False
        bot._material_outreach_is_deferred = (
            lambda result: isinstance(result, dict) and result.get("status") == "deferred_lock_busy"
        )
        bot._material_outreach_preface_is_queued = lambda _result: False
        bot._resolve_material_outreach_direct_failure = lambda *_args, **_kwargs: False
        bot._disable_once_material_outreach_task = lambda task_id: disabled.append(task_id)
        bot._set_runtime_task_list = lambda *_args, **_kwargs: saved.append("set")
        bot._save_material_outreach_task_definitions_only = lambda *_args, **_kwargs: saved.append("save")

        run_due_fixed_material_outreach(bot, now=now)

        self.assertEqual(disabled, [])
        self.assertEqual(saved, [])

    def test_ai_material_outreach_success_registers_outbound_echoes(self):
        bot = WXBot.__new__(WXBot)
        bot.config = SimpleNamespace(cmd="文件传输助手", group=[])
        bot._ensure_message_runtime_state()
        material = {
            "id": "mat_1",
            "source": "素材源",
            "type": "miniapp",
            "type_bucket": "miniapp",
            "stable_signature": "sig_1",
        }
        bot._find_material_by_stable_signature = lambda signature: material if signature == "sig_1" else None
        bot._material_runtime_message = lambda item, refresh_missing=True: (item, object(), [item])
        bot._material_forward_error_needs_refresh = lambda _error: False
        bot._append_material_send_record = lambda *_args, **_kwargs: None
        forwards = []
        bot._forward_material_message = (
            lambda message, targets, **kwargs: forwards.append((targets, kwargs)) or (True, "")
        )

        success, error = bot._send_ai_material_outreach_record(
            {
                "stable_signature": "sig_1",
                "target": "张三",
                "preface_enabled": True,
                "preface": "这个你可能会喜欢",
                "material_type": "miniapp",
            }
        )

        self.assertTrue(success)
        self.assertEqual(error, "")
        self.assertEqual(forwards[0][0], ["张三"])
        echoes = bot._private_outbound_echoes["张三"]
        self.assertEqual([item["type"] for item in echoes], ["text", "miniapp"])
        self.assertEqual(echoes[0]["content"], "这个你可能会喜欢")
        self.assertTrue(all(item["source"] == "ai_material_outreach" for item in echoes))


if __name__ == "__main__":
    unittest.main()
