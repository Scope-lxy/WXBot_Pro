import unittest
import queue
import tempfile
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
from core import wechat_ui_actions
from core.inbound_coordinator import InboundCoordinator
from core.message_pipeline import ConversationRef, MessageEnvelope
from core.message_store import MessageStore
from core.reply_delivery import ReplyEchoTracker


def msg(msg_type, content):
    return SimpleNamespace(type=msg_type, content=content)


class MaterialOutreachPoolTests(unittest.TestCase):
    def test_manual_target_remains_runnable_when_contact_directory_is_empty(self):
        bot = WXBot.__new__(WXBot)
        bot._contact_profiles_directory_file = lambda: ("ignored.json", "scope_rui")
        bot._append_material_progress_records = lambda *_args, **_kwargs: None
        bot._append_material_skip_record = lambda *_args, **_kwargs: self.fail("手动目标不应因通讯录为空被阻断")
        task = {
            "task_id": "task_manual",
            "target_selector": {"mode": "include", "base": "manual"},
            "manual_target_names": ["阿英2"],
        }

        with mock.patch("wxbot_core.load_contact_directory", return_value={"subjects": []}):
            resolved = bot._resolve_material_outreach_directory_task(task, [])

        self.assertEqual(resolved["targets"], ["阿英2"])
        self.assertEqual(resolved["_outreach_target_snapshot"]["targets"][0]["contact_key"], "")

    def test_material_forward_passes_business_ids_to_ui_journal_payload(self):
        bot, _store = self._message_runtime_bot()
        bot._ui_owner = object()
        captured = []
        bot._ui_forward_message = lambda *_args, **kwargs: captured.append(kwargs) or True

        with mock.patch("wxbot_core.wechat_ui_actions.hold", return_value=nullcontext()):
            success, error = bot._forward_material_message_unlocked(
                SimpleNamespace(type="link", content="素材"),
                ["阿英2"],
                material_source="素材源",
                delivery_id="delivery-1",
                request_id="request-1",
                run_id="run-1",
                batch_id="batch-1",
            )

        self.assertTrue(success)
        self.assertEqual(error, "")
        self.assertEqual(captured[0]["delivery_id"], "delivery-1")
        self.assertEqual(captured[0]["request_id"], "request-1")
        self.assertEqual(captured[0]["run_id"], "run-1")
        self.assertEqual(captured[0]["batch_id"], "batch-1")

    def test_material_duplicate_delivery_is_reported_as_uncertain_not_cancelled(self):
        bot, _store = self._message_runtime_bot()
        bot._ui_owner = object()
        bot._ui_forward_message = mock.Mock(
            side_effect=wechat_ui_actions.DeliveryAlreadySubmitted("already submitted")
        )

        with mock.patch("wxbot_core.wechat_ui_actions.hold", return_value=nullcontext()):
            with self.assertRaises(wechat_ui_actions.DeliveryAlreadySubmitted):
                bot._forward_material_message_unlocked(
                    SimpleNamespace(type="link", content="素材"),
                    ["阿英2"],
                    material_source="素材源",
                    delivery_id="delivery-1",
                )

    def test_uncertain_material_result_stops_outer_retry_loop(self):
        bot = WXBot.__new__(WXBot)
        snapshot = {"run_id": "run-1", "targets": []}
        resolved_task = {"task_id": "task-1", "_outreach_target_snapshot": snapshot}
        bot._load_material_send_records = lambda: []
        bot._resolve_material_outreach_directory_task = lambda _task, _records: resolved_task
        bot._log_material_outreach_run_start = lambda *_args, **_kwargs: None
        calls = []
        uncertain = {"status": "uncertain", "message": "result lost"}
        bot._attempt_material_outreach_batches = lambda *_args, **_kwargs: calls.append("attempt") or uncertain

        result = bot._send_material_outreach_locked({"task_id": "task-1"})

        self.assertIs(result, uncertain)
        self.assertEqual(calls, ["attempt"])

    def test_material_task_finish_is_warning_when_any_target_failed(self):
        bot = WXBot.__new__(WXBot)
        bot._material_outreach_progress_summary = lambda _snapshot: {
            "targets": 2,
            "success": 1,
            "failed": 1,
            "skipped": 0,
            "pending": 0,
        }

        with mock.patch("wxbot_core.log") as log_mock:
            bot._log_material_outreach_run_finish({"name": "测试任务"}, {"run_id": "run-1"})

        self.assertEqual(log_mock.call_args.kwargs["level"], "WARNING")

    def _message_runtime_bot(self):
        temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(temp_dir.cleanup)
        bot = WXBot.__new__(WXBot)
        bot.config = SimpleNamespace(
            cmd="管理员",
            group=[],
        )
        bot._ui_ingress_queue = queue.Queue()
        bot._message_store = MessageStore(temp_dir.name, "material-test")
        bot._inbound_coordinator = InboundCoordinator(bot._message_store)
        bot._reply_echo_tracker = ReplyEchoTracker()
        return bot, bot._message_store

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

    def test_ai_material_outreach_forwards_material_and_preface(self):
        bot, _store = self._message_runtime_bot()
        bot.config.cmd = "文件传输助手"
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
        self.assertEqual(forwards[0][1]["preface"], "这个你可能会喜欢")
        self.assertEqual(forwards[0][1]["material_type"], "miniapp")
        self.assertEqual(forwards[0][1]["material_title"], "小程序测试作品")
        self.assertEqual(forwards[0][1]["echo_source"], "ai_material_outreach")

    def test_material_callbacks_are_saved_in_actual_callback_order(self):
        for callback_types in (("file", "text"), ("text", "file")):
            with self.subTest(callback_types=callback_types):
                bot, store = self._message_runtime_bot()
                messages = {
                    "file": MessageEnvelope(
                        id="self-material",
                        type="file",
                        attr="self",
                        sender="self",
                        content="素材标题",
                    ),
                    "text": MessageEnvelope(
                        id="self-preface",
                        type="text",
                        attr="self",
                        sender="self",
                        content="附加文案",
                    ),
                }

                class ForwardMessage:
                    type = "file"
                    content = "素材标题"

                    def forward(self, targets, message=""):
                        self.targets = targets
                        self.preface = message
                        for kind in callback_types:
                            bot._enqueue_ui_message(
                                ConversationRef("张三", "private"),
                                messages[kind],
                            )
                        return True

                forwarded = ForwardMessage()
                with mock.patch("wxbot_core.wechat_ui_actions.hold", return_value=nullcontext()), mock.patch(
                    "wxbot_core.warn_slow_wechat_ui_action", return_value=nullcontext()
                ):
                    success, error = bot._forward_material_message_unlocked(
                        forwarded,
                        ["张三"],
                        preface="附加文案",
                        material_type="file",
                        material_title="素材标题",
                    )

                history = store.history("张三", 10)
                self.assertTrue(success)
                self.assertEqual(error, "")
                self.assertEqual(forwarded.targets, ["张三"])
                self.assertEqual(forwarded.preface, "附加文案")
                self.assertEqual([item["message_type"] for item in history], list(callback_types))
                self.assertEqual(
                    [item["content"] for item in history],
                    [messages[kind].content for kind in callback_types],
                )
                self.assertTrue(all(message._wxbot_inbound_direction == "bot_echo" for message in messages.values()))
                self.assertEqual(store.conversation_version("张三"), 0)

    def test_forward_registers_echo_before_synchronous_self_callback(self):
        bot, store = self._message_runtime_bot()
        bot._ui_owner = None
        callback_message = MessageEnvelope(
            id="self-material-1",
            type="file",
            attr="self",
            sender="self",
            content="素材标题",
        )

        class ForwardMessage:
            type = "file"
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
                material_type="file",
                material_title="素材标题",
            )

        self.assertTrue(success)
        self.assertEqual(error, "")
        self.assertEqual(callback_message._wxbot_inbound_direction, "bot_echo")
        self.assertFalse(getattr(callback_message, "_wxbot_sequence_advanced", False))
        self.assertEqual([item["content"] for item in store.history("张三", 10)], ["素材标题"])
        self.assertEqual(store.conversation_version("张三"), 0)

    def test_material_self_callback_does_not_duplicate_confirmed_history(self):
        bot, store = self._message_runtime_bot()

        class ForwardMessage:
            type = "file"
            content = "素材标题"

            def forward(self, _targets, message=""):
                return True

        with mock.patch("wxbot_core.wechat_ui_actions.hold", return_value=nullcontext()), mock.patch(
            "wxbot_core.warn_slow_wechat_ui_action", return_value=nullcontext()
        ):
            success, error = bot._forward_material_message_unlocked(
                ForwardMessage(),
                ["张三"],
                material_type="file",
                material_title="素材标题",
            )

        callback = MessageEnvelope(
            id="self-material-late",
            type="file",
            attr="self",
            sender="self",
            content="素材标题",
        )
        bot._enqueue_ui_message(ConversationRef("张三", "private"), callback)

        self.assertTrue(success)
        self.assertEqual(error, "")
        self.assertEqual(callback._wxbot_inbound_direction, "bot_echo")
        self.assertEqual([item["content"] for item in store.history("张三", 10)], ["素材标题"])
        self.assertEqual(store.conversation_version("张三"), 0)

    def test_repeated_callbacks_without_native_id_are_distinct_history_entries(self):
        bot, store = self._message_runtime_bot()
        callbacks = []

        for _index in range(2):
            callback = MessageEnvelope(
                type="text",
                attr="self",
                sender="self",
                content="同一句人工消息",
                hash="same-hash",
                hash_text="same-hash-text",
            )
            bot._enqueue_ui_message(ConversationRef("张三", "private"), callback)
            callbacks.append(callback)

        history = store.history("张三", 10)
        self.assertEqual([item["content"] for item in history], ["同一句人工消息", "同一句人工消息"])
        self.assertEqual([item["direction"] for item in history], ["manual_self", "manual_self"])
        self.assertEqual(store.conversation_version("张三"), 2)
        self.assertNotEqual(callbacks[0]._wxbot_event_id, callbacks[1]._wxbot_event_id)

if __name__ == "__main__":
    unittest.main()
