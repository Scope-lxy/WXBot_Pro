import unittest
from unittest import mock

from core.wechat_observability import log_wechat_ui_action_timing, warn_slow_wechat_ui_action


class WeChatObservabilityTests(unittest.TestCase):
    def test_segmented_ui_timing_uses_debug_for_fast_completed_action(self):
        with mock.patch("core.wechat_observability.log") as log_mock:
            log_wechat_ui_action_timing(
                "添加“测试会话”",
                total_seconds=3.0,
                owner_queue_seconds=1.0,
                ui_lock_seconds=0.5,
                handler_seconds=1.5,
                handler_invoked=True,
            )

        call = log_mock.call_args.kwargs
        self.assertEqual(call["level"], "DEBUG")
        for phase in ("排队", "等待微信", "实际操作"):
            self.assertIn(phase, call["message"])
        self.assertIn("添加“测试会话”完成", call["message"])

    def test_segmented_ui_timing_warns_when_handler_never_started(self):
        error = RuntimeError("等待锁超时")
        with mock.patch("core.wechat_observability.log") as log_mock:
            log_wechat_ui_action_timing(
                "关闭“测试会话”",
                total_seconds=30.0,
                owner_queue_seconds=0.1,
                ui_lock_seconds=29.9,
                handler_seconds=0.0,
                handler_invoked=False,
                error=error,
            )

        call = log_mock.call_args.kwargs
        self.assertEqual(call["level"], "WARNING")
        self.assertIn("关闭“测试会话”未开始", call["message"])
        self.assertIn(str(error), call["message"])

    def test_segmented_ui_timing_uses_agreed_slow_thresholds(self):
        cases = (
            (29.9, "DEBUG"),
            (30.0, "INFO"),
            (60.0, "INFO"),
            (60.1, "WARNING"),
        )
        for elapsed, expected_level in cases:
            with self.subTest(elapsed=elapsed), mock.patch("core.wechat_observability.log") as log_mock:
                log_wechat_ui_action_timing(
                    "添加“测试会话”",
                    total_seconds=elapsed,
                    owner_queue_seconds=elapsed,
                    ui_lock_seconds=0.0,
                    handler_seconds=0.0,
                    handler_invoked=True,
                )

            self.assertEqual(log_mock.call_args.kwargs["level"], expected_level)

    def test_slow_ui_actions_use_three_log_levels(self):
        cases = (
            (29.9, None),
            (30.0, "INFO"),
            (60.0, "INFO"),
            (60.1, "WARNING"),
        )
        for elapsed, expected_level in cases:
            with self.subTest(elapsed=elapsed), mock.patch(
                "core.wechat_observability.time.perf_counter",
                side_effect=(100.0, 100.0 + elapsed),
            ), mock.patch("core.wechat_observability.log") as log_mock:
                with warn_slow_wechat_ui_action("测试操作"):
                    pass

            if expected_level is None:
                log_mock.assert_not_called()
            else:
                self.assertEqual(log_mock.call_args.kwargs["level"], expected_level)
                self.assertIn(f"耗时 {elapsed:.1f}s", log_mock.call_args.kwargs["message"])

    def test_slow_ui_observation_does_not_suppress_action_error(self):
        with mock.patch(
            "core.wechat_observability.time.perf_counter",
            side_effect=(100.0, 130.0),
        ), mock.patch("core.wechat_observability.log") as log_mock:
            with self.assertRaisesRegex(RuntimeError, "微信调用失败"):
                with warn_slow_wechat_ui_action("测试操作"):
                    raise RuntimeError("微信调用失败")

        self.assertEqual(log_mock.call_args.kwargs["level"], "INFO")
