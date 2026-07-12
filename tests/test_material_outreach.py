import unittest
import queue
import threading
import time
from contextlib import nullcontext
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest import mock

from feature.runtime_task_runner import run_due_fixed_material_outreach
from feature.material_outreach import (
    build_target_snapshot,
    collect_material_source_message,
    plan_material_outreach_batches,
    rebuild_material_pool_for_source,
    send_names_from_target_snapshot,
)
from wxbot_core import WXBot
from core.message_pipeline import ConversationRef, MessageEnvelope


def msg(msg_type, content):
    return SimpleNamespace(type=msg_type, content=content)


class MaterialOutreachPoolTests(unittest.TestCase):
    def _material_history_bot(self):
        bot = WXBot.__new__(WXBot)
        bot.config = SimpleNamespace(
            cmd="管理员",
            group=[],
            memory_switch=True,
            memory_max_count=5000,
        )
        calls = []
        bot.memory_manager = SimpleNamespace(save_message=lambda **kwargs: calls.append(kwargs))
        bot._mark_chat_memory_dirty = lambda *_args, **_kwargs: True
        bot._schedule_private_outbound_echo_fallback = lambda _target: True
        bot._ensure_message_runtime_state()
        return bot, calls

    def test_target_snapshot_keeps_v2_contact_names_in_progress_records(self):
        snapshot = build_target_snapshot(
            {"task_id": "task_1"},
            {
                "selected": [
                    {
                        "contact_key": "wechat_id:wx_zhang",
                        "remark": "张三",
                        "nickname": "三三",
                        "wechat_id": "wx_zhang",
                        "tags": ["客户"],
                        "warnings": [],
                    }
                ],
                "excluded": [],
            },
            now=datetime(2026, 7, 10, 10, 0, 0),
        )

        self.assertEqual(send_names_from_target_snapshot(snapshot), ["张三"])
        self.assertEqual(snapshot["targets"][0]["send_name"], "张三")
        self.assertEqual(snapshot["progress_records"][0]["send_name"], "张三")
        self.assertEqual(snapshot["progress_records"][0]["display_name"], "张三")

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

    def test_once_fixed_material_skips_target_already_sent_by_same_task(self):
        task = {
            "task_id": "task_1",
            "repeat_mode": "once",
            "targets": ["阿英2", "阿英3"],
            "material_types": ["all"],
            "batch_material_strategy": "fixed",
            "fixed_material_id": "mat_1",
            "batch_size_fixed": 9,
        }
        material = {
            "id": "mat_1",
            "status": "active",
            "type": "link",
            "type_bucket": "link",
        }

        plan = plan_material_outreach_batches(
            task,
            [material],
            [{"task_id": "task_1", "material_id": "mat_1", "target": "阿英2", "success": True}],
            {"mat_1"},
            now=datetime(2026, 7, 12, 15, 0, 0),
        )

        self.assertEqual(plan["send"][0]["target"], "阿英3")
        self.assertEqual([(item["target"], item["reason"]) for item in plan["skip"]], [("阿英2", "already_sent")])

    def test_preface_queue_resolves_completed_cycle_before_removing_record(self):
        now = datetime(2026, 7, 12, 15, 0, 0)
        queue = [{
            "queue_id": "preface_1",
            "task_id": "task_1",
            "run_id": "run_1",
            "target": "阿英2",
            "status": "pending",
            "preface_status": "success",
            "scheduled_at": now.isoformat(),
        }]
        saved_queues = []
        resolved = []
        bot = SimpleNamespace(
            is_stop_requested=lambda: False,
            _material_outreach_runtime_lock=lambda: nullcontext(),
            _load_material_outreach_preface_queue=lambda: queue,
            _load_material_outreach_materials=lambda: [],
            _send_material_outreach_preface_record=lambda record, now=None: record.update(status="sent") or True,
            _resolve_material_outreach_preface_cycle=lambda task_id, **kwargs: resolved.append(
                (task_id, kwargs["cycle_records"][0]["status"])
            ) or True,
            _save_material_outreach_preface_queue=lambda records: saved_queues.append(list(records)),
        )

        changed = WXBot._process_material_outreach_preface_queue(bot, now=now)

        self.assertTrue(changed)
        self.assertEqual(resolved, [("task_1", "sent")])
        self.assertEqual(saved_queues[-1], [])

    def test_ai_material_outreach_success_registers_outbound_echoes(self):
        bot = WXBot.__new__(WXBot)
        bot.config = SimpleNamespace(
            cmd="文件传输助手",
            group=[],
        )
        bot._ensure_message_runtime_state()
        material = {
            "id": "mat_1",
            "source": "素材源",
            "type": "miniapp",
            "type_bucket": "miniapp",
            "stable_signature": "sig_1",
            "content_preview": "小程序测试作品",
        }
        bot._find_material_by_stable_signature = lambda signature: material if signature == "sig_1" else None
        bot._material_runtime_message = lambda item, refresh_missing=True: (item, object(), [item])
        bot._material_forward_error_needs_refresh = lambda _error: False
        bot._append_material_send_record = lambda *_args, **_kwargs: None
        forwards = []

        def forward(message, targets, **kwargs):
            forwards.append((targets, kwargs))
            bot._remember_material_outbound_echoes(
                targets,
                kwargs.get("material_type"),
                preface=kwargs.get("preface"),
                material_title=kwargs.get("material_title"),
                source=kwargs.get("echo_source"),
            )
            return True, ""

        bot._forward_material_message = forward

        success, error = bot._send_ai_material_outreach_record(
            {
                "stable_signature": "sig_1",
                "target": "张三",
                "preface_enabled": True,
                "preface": "这个你可能会喜欢",
                "material_type": "miniapp",
                "material_title": "小程序测试作品",
            }
        )

        self.assertTrue(success)
        self.assertEqual(error, "")
        self.assertEqual(forwards[0][0], ["张三"])
        echoes = bot._private_outbound_echoes["张三"]
        self.assertEqual([item["type"] for item in echoes], ["miniapp", "text"])
        self.assertEqual(echoes[0]["fallback_content"], "小程序测试作品")
        self.assertEqual(echoes[1]["content"], "这个你可能会喜欢")
        self.assertTrue(all(item["source"] == "ai_material_outreach" for item in echoes))
        self.assertTrue(all(not item["memory_persisted"] for item in echoes))

        chat = SimpleNamespace(who="张三", chat_type="private")
        material_echo = SimpleNamespace(type="miniapp", attr="self", content="小程序测试作品")
        preface_echo = SimpleNamespace(type="text", attr="self", content="这个你可能会喜欢")
        self.assertFalse(bot._should_skip_message_memory(chat, material_echo))
        self.assertFalse(bot._should_skip_message_memory(chat, preface_echo))

    def test_material_callbacks_are_saved_in_actual_callback_order(self):
        for callback_types in (("miniapp", "text"), ("text", "miniapp")):
            with self.subTest(callback_types=callback_types):
                bot, calls = self._material_history_bot()
                bot._remember_material_outbound_echoes(
                    ["张三"],
                    "miniapp",
                    preface="附加文案",
                    material_title="素材标题",
                )
                chat = SimpleNamespace(who="张三", chat_type="private")
                messages = {
                    "miniapp": SimpleNamespace(type="miniapp", attr="self", sender="self", content="微信里的素材"),
                    "text": SimpleNamespace(type="text", attr="self", sender="self", content="附加文案"),
                }

                for kind in callback_types:
                    self.assertTrue(bot._save_incoming_memory_message(chat, messages[kind]))

                self.assertEqual([item["msg_type"] for item in calls], list(callback_types))
                self.assertEqual(
                    [item["content"] for item in calls],
                    [messages[kind].content for kind in callback_types],
                )

    def test_missing_material_callbacks_fall_back_to_material_then_preface(self):
        bot, calls = self._material_history_bot()
        bot._remember_material_outbound_echoes(
            ["张三"],
            "miniapp",
            preface="附加文案",
            material_title="素材标题",
        )
        for echo in bot._private_outbound_echoes["张三"]:
            echo["expires_at"] = 1

        self.assertEqual(bot._flush_expired_private_outbound_echo_fallbacks("张三", now=2), 2)
        self.assertEqual([item["msg_type"] for item in calls], ["miniapp", "text"])
        self.assertEqual([item["content"] for item in calls], ["素材标题", "附加文案"])
        self.assertTrue(all(item["memory_persisted"] for item in bot._private_outbound_echoes["张三"]))

    def test_forward_registers_echo_before_synchronous_self_callback(self):
        bot = WXBot.__new__(WXBot)
        bot.config = SimpleNamespace(cmd="管理员", group=[], memory_switch=False)
        bot._ui_owner = None
        bot._ui_ingress_queue = queue.Queue()
        bot._stop_requested_event = threading.Event()
        bot._ensure_message_runtime_state()
        callback_message = MessageEnvelope(
            id="self-material-1",
            type="miniapp",
            attr="self",
            sender="self",
            content="素材标题",
        )

        class ForwardMessage:
            type = "miniapp"
            content = "素材标题"

            def forward(self, targets, message=""):
                bot._enqueue_ui_message(ConversationRef("张三", "private"), callback_message)
                return True

        with mock.patch("wxbot_core.wechat_ui_actions.hold", return_value=nullcontext()), mock.patch(
            "wxbot_core.warn_slow_wechat_ui_action", return_value=nullcontext()
        ):
            success, error = bot._forward_material_message_unlocked(
                ForwardMessage(),
                ["张三"],
                preface="附加文案",
                material_type="miniapp",
                material_title="素材标题",
            )

        self.assertTrue(success)
        self.assertEqual(error, "")
        self.assertTrue(callback_message._wxbot_private_outbound_echo)
        self.assertFalse(getattr(callback_message, "_wxbot_sequence_advanced", False))

    def test_failed_callback_memory_write_remains_available_for_fallback(self):
        bot, calls = self._material_history_bot()
        bot._remember_material_outbound_echoes(["张三"], "miniapp", material_title="素材标题")
        bot.memory_manager = SimpleNamespace(save_message=lambda **_kwargs: (_ for _ in ()).throw(OSError("disk")))
        chat = SimpleNamespace(who="张三", chat_type="private")
        callback = SimpleNamespace(type="miniapp", attr="self", sender="self", content="素材标题")

        self.assertFalse(bot._save_incoming_memory_message(chat, callback))
        self.assertTrue(bot._private_outbound_echoes["张三"][0].get("reservation_id"))
        bot.memory_manager = SimpleNamespace(save_message=lambda **kwargs: calls.append(kwargs))
        bot._private_outbound_echoes["张三"][0]["expires_at"] = 1

        self.assertEqual(bot._flush_expired_private_outbound_echo_fallbacks("张三", now=2), 1)
        self.assertEqual([item["content"] for item in calls], ["素材标题"])

    def test_duplicate_callback_without_id_is_saved_once(self):
        bot, calls = self._material_history_bot()
        bot._remember_material_outbound_echoes(["张三"], "miniapp", material_title="素材标题")
        chat = SimpleNamespace(who="张三", chat_type="private")

        for _index in range(2):
            callback = SimpleNamespace(type="miniapp", attr="self", sender="self", content="素材标题")
            bot._save_incoming_memory_message(chat, callback)

        self.assertEqual([item["content"] for item in calls], ["素材标题"])

    def test_same_type_material_callbacks_match_titles_when_reversed(self):
        bot, calls = self._material_history_bot()
        bot._remember_material_outbound_echoes(["张三"], "miniapp", material_title="素材A")
        bot._remember_material_outbound_echoes(["张三"], "miniapp", material_title="素材B")
        chat = SimpleNamespace(who="张三", chat_type="private")

        for title in ("[小程序] 素材B", "[小程序] 素材A"):
            callback = SimpleNamespace(type="miniapp", attr="self", sender="self", content=title)
            self.assertTrue(bot._save_incoming_memory_message(chat, callback))

        self.assertEqual([item["content"] for item in calls], ["[小程序] 素材B", "[小程序] 素材A"])

    def test_material_echo_prefers_exact_or_unique_longest_overlapping_title(self):
        bot, _calls = self._material_history_bot()
        bot._remember_material_outbound_echoes(["张三"], "miniapp", material_title="素材A")
        bot._remember_material_outbound_echoes(["张三"], "miniapp", material_title="素材AB")

        exact = bot._consume_private_outbound_echo(
            "张三", msg_type="miniapp", content="素材AB", return_match=True
        )
        self.assertEqual(exact.get("content"), "素材AB")
        bot._commit_private_outbound_echo_reservation(
            SimpleNamespace(_wxbot_outbound_echo_reservation=exact.get("reservation_id"))
        )
        remaining = bot._consume_private_outbound_echo(
            "张三", msg_type="miniapp", content="卡片：素材A", return_match=True
        )
        self.assertEqual(remaining.get("content"), "素材A")

    def test_fallback_flush_is_single_flight_across_threads(self):
        bot, _calls = self._material_history_bot()
        bot._remember_private_outbound_echo(
            "张三", "text", "回复", fallback_content="回复", source="test"
        )
        bot._private_outbound_echoes["张三"][0]["expires_at"] = 1
        started = threading.Event()
        release = threading.Event()
        writes = []

        def save_once(_name, _echo):
            writes.append("write")
            started.set()
            release.wait(1)
            return True

        bot._save_private_outbound_echo_fallback = save_once
        worker = threading.Thread(
            target=lambda: bot._flush_expired_private_outbound_echo_fallbacks("张三", now=2)
        )
        worker.start()
        self.assertTrue(started.wait(1))
        self.assertEqual(bot._flush_expired_private_outbound_echo_fallbacks("张三", now=2), 0)
        release.set()
        worker.join(1)

        self.assertEqual(writes, ["write"])

    def test_stop_cancels_pending_fallback_without_starting_disk_write(self):
        bot, calls = self._material_history_bot()
        bot._remember_private_outbound_echo(
            "张三", "text", "回复", fallback_content="回复", source="test"
        )

        class Timer:
            cancelled = False

            def cancel(self):
                self.cancelled = True

        timer = Timer()
        bot._private_outbound_echo_fallback_timer = timer
        bot._private_outbound_echo_fallback_deadline = time.time() + 10
        bot._finalize_private_outbound_echo_fallbacks_on_stop()

        self.assertTrue(timer.cancelled)
        self.assertIsNone(bot._private_outbound_echo_fallback_timer)
        self.assertEqual(calls, [])
        self.assertEqual(bot._private_outbound_echoes, {})

    def test_stop_waits_for_callback_writer_without_duplicate_fallback(self):
        bot, calls = self._material_history_bot()
        bot._remember_private_outbound_echo(
            "张三", "text", "回复", fallback_content="回复", source="test"
        )
        callback = SimpleNamespace(type="text", attr="self", sender="self", content="回复")
        match = bot._consume_private_outbound_echo(
            "张三", message=callback, return_match=True
        )
        callback._wxbot_private_outbound_echo = True
        callback._wxbot_outbound_echo_reservation = match["reservation_id"]
        started = threading.Event()
        release = threading.Event()

        def save_message(**kwargs):
            started.set()
            release.wait(1)
            calls.append(kwargs)

        bot.memory_manager = SimpleNamespace(save_message=save_message)
        chat = SimpleNamespace(who="张三", chat_type="private")
        callback_writer = threading.Thread(target=lambda: bot._save_incoming_memory_message(chat, callback))
        callback_writer.start()
        self.assertTrue(started.wait(1))
        finalizer = threading.Thread(target=bot._finalize_private_outbound_echo_fallbacks_on_stop)
        finalizer.start()
        self.assertTrue(finalizer.is_alive())
        release.set()
        callback_writer.join(1)
        finalizer.join(1)

        self.assertFalse(finalizer.is_alive())
        self.assertEqual([item["content"] for item in calls], ["回复"])

    def test_running_fallback_timer_cannot_reschedule_after_stop(self):
        bot, _calls = self._material_history_bot()
        delattr(bot, "_schedule_private_outbound_echo_fallback")
        bot._remember_private_outbound_echo(
            "张三", "text", "回复", fallback_content="回复", source="test"
        )
        bot._private_outbound_echoes["张三"][0]["expires_at"] = 0
        started = threading.Event()
        release = threading.Event()

        def save_once(_name, _echo):
            started.set()
            release.wait(1)
            return True

        bot._save_private_outbound_echo_fallback = save_once
        timer_worker = threading.Thread(target=bot._run_private_outbound_echo_fallback_timer)
        timer_worker.start()
        self.assertTrue(started.wait(1))
        finalizer = threading.Thread(target=bot._finalize_private_outbound_echo_fallbacks_on_stop)
        finalizer.start()
        release.set()
        timer_worker.join(1)
        finalizer.join(1)

        self.assertTrue(bot._private_outbound_echo_fallback_stopped)
        self.assertIsNone(bot._private_outbound_echo_fallback_timer)

    def test_unknown_text_send_uses_success_record_fallback(self):
        bot, calls = self._material_history_bot()

        with self.assertRaisesRegex(RuntimeError, "result lost"):
            bot._run_private_outbound_echo_send(
                "张三",
                {"type": "text", "text": "已提交但结果未知"},
                lambda: (_ for _ in ()).throw(RuntimeError("result lost")),
                source="test",
            )
        bot._private_outbound_echoes["张三"][0]["expires_at"] = 0
        self.assertEqual(bot._flush_expired_private_outbound_echo_fallbacks("张三"), 1)
        bot._finalize_private_outbound_echo_fallbacks_on_stop()

        self.assertEqual([item["content"] for item in calls], ["已提交但结果未知"])

    def test_stop_history_write_timeout_does_not_block_exit(self):
        bot, _calls = self._material_history_bot()
        attempts = []

        class BusyLock:
            def acquire(self, *, timeout):
                attempts.append(timeout)
                return False

            def release(self):
                raise AssertionError("未获取锁时不得 release")

        bot._private_outbound_history_write_lock = BusyLock()
        self.assertFalse(bot._finalize_private_outbound_echo_fallbacks_on_stop())
        self.assertEqual(len(attempts), 1)
        self.assertGreater(attempts[0], 0)
        self.assertTrue(bot._private_outbound_echo_fallback_stopped)


if __name__ == "__main__":
    unittest.main()
