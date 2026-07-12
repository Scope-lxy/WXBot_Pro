import json
import tempfile
import unittest
from argparse import Namespace
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from feature.message_routing import record_runtime_inbound_event

from tools.probe_production_acceptance import (
    AcceptanceObserver,
    automatic_acceptance_status,
    classify_log_line,
    log_line_is_current,
    metric_delta,
    metric_totals,
    parse_args,
    state_event_markers,
    state_schema_error,
)

RUNTIME_ID = "a" * 32


class ProductionAcceptanceProbeTests(unittest.TestCase):
    def test_classifies_events_without_storing_message_content(self):
        events, failures = classify_log_line(
            "[07-11 12:00:00] [INFO]: 运行事件：入站消息 scope=private type=text"
        )

        self.assertEqual(events, {"private_inbound"})
        self.assertEqual(failures, set())

    def test_does_not_treat_broad_business_logs_as_success(self):
        for line in (
            "群组 测试群：图片摘要尚未回写",
            "[素材转发] 微信 UI 正忙，本批次稍后重试",
            "关系扫描完成前发生错误",
            "普通发送成功",
            "后台状态 type=voice",
        ):
            events, _failures = classify_log_line(line)
            self.assertEqual(events, set(), line)

    def test_structured_inbound_events_record_scope_and_type(self):
        events, _failures = classify_log_line(
            "运行事件：入站消息 scope=group type=voice"
        )

        self.assertEqual(events, {"group_inbound", "voice_inbound"})

    def test_runtime_inbound_event_is_sanitized_and_recorded_once(self):
        message = SimpleNamespace(
            attr="friend",
            type="image",
            sender="敏感发送人",
            content="敏感正文",
        )
        with patch("feature.message_routing.log") as log_mock:
            bot = SimpleNamespace(_runtime_instance_id=RUNTIME_ID)
            record_runtime_inbound_event(bot, message, "group")
            record_runtime_inbound_event(bot, message, "group")

        self.assertEqual(log_mock.call_count, 1)
        logged = str(log_mock.call_args)
        self.assertIn("scope=group type=image", logged)
        self.assertNotIn("敏感发送人", logged)
        self.assertNotIn("敏感正文", logged)

        fresh_message = SimpleNamespace(attr="friend", type="text")
        with patch("feature.message_routing.log", side_effect=OSError("log unavailable")):
            record_runtime_inbound_event(
                SimpleNamespace(_runtime_instance_id=RUNTIME_ID),
                fresh_message,
                "private",
            )
        self.assertFalse(getattr(fresh_message, "_wxbot_runtime_inbound_logged", False))

    def test_classifies_failure_signals(self):
        events, failures = classify_log_line("[ERROR] 微信 UI 卡死，检测到重复回复")

        self.assertEqual(events, set())
        self.assertEqual(failures, {"error_log", "ui_stuck", "duplicate_send"})

    def test_log_line_time_must_be_inside_current_run(self):
        self.assertTrue(log_line_is_current("[07-11 10:00:00] [INFO]: ok\n", "2026-07-11T10:00:00"))
        self.assertFalse(log_line_is_current("[07-11 09:59:59] [INFO]: old\n", "2026-07-11T10:00:00"))

    def test_metric_totals_and_delta(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "metrics.json"
            path.write_text(json.dumps({
                "hours": {
                    "2026-07-11T10": {"received_messages": 2, "reply_count": 1},
                    "2026-07-11T11": {"received_messages": 3, "reply_count": 2},
                }
            }), encoding="utf-8")

            totals = metric_totals(path)

        self.assertEqual(totals, {"received_messages": 5, "reply_count": 3})
        self.assertEqual(metric_delta({"received_messages": 3}, totals), {
            "received_messages": 2,
            "reply_count": 3,
        })

    def test_automatic_status_requires_duration_events_and_clean_runtime(self):
        status = automatic_acceptance_status(
            duration_seconds=7200,
            required_events=("private_inbound", "contact_batch"),
            event_counts={"private_inbound": 2, "contact_batch": 1},
            failure_counts={},
            panel_failures=0,
            bot_unhealthy_samples=0,
            json_errors=[],
            process_failures=0,
            max_panel_processes=1,
            max_collectors=1,
            final_collectors=0,
            max_test_processes=0,
            runtime_metric_delta={"received_messages": 2, "reply_count": 1},
            interrupted=False,
            smoke_run=False,
            panel_zero_samples=0,
            panel_pid_count=1,
            json_suspicious_changes=[],
            max_panel_sample_gap=5.0,
            activity_buckets=[0, 1, 2, 3, 5, 7],
            max_web_server_families=1,
            runtime_id_count=1,
        )

        self.assertTrue(status["automatic_checks_passed"])
        self.assertTrue(status["manual_review_required"])

    def test_automatic_status_reports_each_failed_gate(self):
        status = automatic_acceptance_status(
            duration_seconds=30,
            required_events=("private_inbound",),
            event_counts={},
            failure_counts={"ui_stuck": 1},
            panel_failures=1,
            bot_unhealthy_samples=1,
            json_errors=["bad.json"],
            process_failures=1,
            max_panel_processes=2,
            max_collectors=2,
            final_collectors=1,
            max_test_processes=1,
            runtime_metric_delta={},
            interrupted=True,
            smoke_run=True,
            panel_zero_samples=1,
            panel_pid_count=2,
            json_suspicious_changes=["tasks:size_collapse"],
            max_panel_sample_gap=30.0,
            activity_buckets=[],
            max_web_server_families=2,
            runtime_id_count=0,
        )

        self.assertFalse(status["automatic_checks_passed"])
        self.assertEqual(set(status["reasons"]), {
            "duration_too_short",
            "interrupted",
            "smoke_run",
            "required_events_missing",
            "failure_log_detected",
            "panel_unhealthy",
            "bot_not_continuously_running",
            "panel_process_missing",
            "json_validation_failed",
            "json_state_suspicious_change",
            "process_snapshot_failed",
            "multiple_panel_processes",
            "extra_web_server_family",
            "panel_pid_changed",
            "multiple_collectors",
            "collector_left_running",
            "test_process_detected",
            "metric_received_messages_missing",
            "metric_reply_count_missing",
            "activity_not_sustained",
            "panel_sample_gap_too_large",
            "runtime_instance_not_stable",
        })

    def test_state_events_use_successful_records_after_start_only(self):
        started = "2026-07-11T10:00:00"
        voice_events = state_event_markers(
            "data/config/voice_reply_state.json",
            {"limits": {"private:test": {"last_sent_at": "2026-07-11T10:01:00"}}},
            started,
        )
        relationship_events = state_event_markers(
            "data/accounts/a/relationship_scan/relationships.json",
            {"runtime": {"last_scan_mode": "full", "last_scan_at": "2026-07-11T10:02:00"}},
            started,
        )
        material_events = state_event_markers(
            "data/accounts/a/tasks/material_outreach/history.json",
            {"send_records": [{"success": True, "sent_at": "2026-07-11T10:03:00"}]},
            started,
        )

        self.assertEqual(voice_events, {"voice_outbound"})
        self.assertEqual(relationship_events, {"relationship_scan"})
        self.assertEqual(material_events, {"material_outreach"})

    def test_state_events_ignore_old_or_failed_records(self):
        started = "2026-07-11T10:00:00"

        events = state_event_markers(
            "data/accounts/a/tasks/material_outreach/history.json",
            {"send_records": [
                {"success": True, "sent_at": "2026-07-11T09:59:59"},
                {"success": False, "sent_at": "2026-07-11T10:01:00"},
            ]},
            started,
        )

        self.assertEqual(events, set())

    def test_known_state_schemas_reject_wrong_root_shape(self):
        self.assertEqual(
            state_schema_error("data/accounts/a/ui_delivery/journal.json", {}),
            "ui_delivery_schema",
        )
        self.assertEqual(
            state_schema_error("data/accounts/a/contact_profiles/contacts.json", {"subjects": []}),
            "",
        )
        self.assertEqual(
            state_schema_error("data/accounts/a/unanswered_inbound/records.json", {}),
            "unanswered_inbound_schema",
        )
        self.assertEqual(
            state_schema_error("data/accounts/a/chat_memory/test.json", {}),
            "chat_memory_schema",
        )
        self.assertEqual(
            state_schema_error("data/config/voice_reply_state.json", {}),
            "voice_reply_state_schema",
        )

    def test_formal_short_run_is_rejected_and_smoke_is_explicit(self):
        with self.assertRaises(SystemExit):
            parse_args(["--duration-seconds", "30"])

        args = parse_args(["--duration-seconds", "30", "--smoke"])

        self.assertTrue(args.smoke)
        self.assertEqual(args.duration_seconds, 30)

        with self.assertRaises(SystemExit):
            parse_args(["--duration-seconds", "7200", "--sample-seconds", "6"])

    def test_report_evidence_does_not_contain_log_message_or_hash(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            log_dir = root / "wxbot_logs"
            log_dir.mkdir(parents=True)
            log_path = log_dir / "log_test.txt"
            log_path.write_text("existing\n", encoding="utf-8")
            observer = AcceptanceObserver(Namespace(
                root=str(root),
                output=str(root / "report.json"),
                panel_url="http://127.0.0.1:1/",
                duration_seconds=1.0,
                sample_seconds=0.2,
                process_sample_seconds=0.2,
                smoke=True,
            ))
            stamp = datetime.now().strftime("%m-%d %H:%M:%S")
            observer.runtime_ids.add(RUNTIME_ID)
            with log_path.open("a", encoding="utf-8") as handle:
                handle.write(
                    f"[{stamp}] [INFO]: 运行事件：入站消息 scope=private type=text "
                    f"runtime_id={'b' * 32}；测试污染\n"
                )
                handle.write(
                    f"[{stamp}] [INFO]: 运行事件：入站消息 scope=private type=text "
                    f"runtime_id={RUNTIME_ID}；敏感昵称；敏感正文\n"
                )

            observer._read_new_logs()
            serialized = json.dumps(observer._build_report(), ensure_ascii=False)

        self.assertEqual(observer.event_counts["private_inbound"], 1)
        self.assertNotIn("敏感昵称", serialized)
        self.assertNotIn("敏感正文", serialized)
        self.assertNotIn("hash", serialized.casefold())


if __name__ == "__main__":
    unittest.main()
