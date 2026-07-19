import unittest
from unittest import mock

from core.wechat_observability import warn_slow_wechat_ui_action


class WeChatObservabilityTests(unittest.TestCase):
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
