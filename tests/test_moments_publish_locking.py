import unittest
from types import SimpleNamespace

from core.wechat_ui_actions import UI_CALL_WAIT_TIMEOUT, UIIntentKind
from wxbot_core import WXBot
from feature.moments_publisher import execute_moments_publish_task


class MomentsPublishLockingTests(unittest.TestCase):
    def test_publish_task_does_not_require_removed_legacy_lock(self):
        calls = []

        class Owner:
            def call(self, intent, timeout):
                calls.append((intent, timeout))
                return True

        bot = WXBot.__new__(WXBot)
        bot._ui_owner = Owner()
        bot._ui_identity = {"nickname": "测试微信"}

        result = WXBot._execute_moments_publish_task(bot, {"id": "moments_test"})

        self.assertTrue(result)
        self.assertEqual(len(calls), 1)
        intent, timeout = calls[0]
        self.assertEqual(intent.kind, UIIntentKind.MOMENTS)
        self.assertEqual(intent.payload["task"]["id"], "moments_test")
        self.assertIs(timeout, UI_CALL_WAIT_TIMEOUT)

    def test_publish_task_rejects_missing_owner_before_opening_wechat(self):
        bot = WXBot.__new__(WXBot)
        bot._ui_owner = None

        with self.assertRaisesRegex(RuntimeError, "只能由微信 UI owner"):
            WXBot._execute_moments_publish_task(bot, {"id": "moments_test"})

    def test_publish_moments_post_logs_simple_success_and_failure(self):
        success_logs = []

        class FakeMomentsSuccess:
            def Refresh(self):
                pass

            def GetMoments(self, force_wait=1):
                return [SimpleNamespace(content="今天不错")]

            def Publish(self, text, images, privacy_config):
                pass

            def Close(self):
                pass

        class FakeMomentsFailure:
            def Refresh(self):
                pass

            def GetMoments(self, force_wait=1):
                return [SimpleNamespace(content="别的内容")]

            def Publish(self, text, images, privacy_config):
                pass

            def Close(self):
                pass

        class FakeMomentsReadError:
            def Refresh(self):
                pass

            def GetMoments(self, force_wait=1):
                raise RuntimeError("窗口未响应")

            def Publish(self, text, images, privacy_config):
                pass

            def Close(self):
                pass

        result = execute_moments_publish_task(
            task={"text": "今天不错", "images": [], "privacy": "public", "tags": []},
            open_moments=lambda: FakeMomentsSuccess(),
            sleep=lambda _seconds: None,
            random_delay=lambda _min, _max: 0,
            notify_error=lambda *_args, **_kwargs: None,
            nickname="测试微信",
            log_info=lambda message: success_logs.append(message),
            log_error=lambda message: success_logs.append(f"ERR:{message}"),
        )

        self.assertTrue(result)
        self.assertIn("朋友圈发布开始", success_logs)
        self.assertTrue(any(message.startswith("朋友圈发布成功：") for message in success_logs))
        self.assertFalse(any("校验" in message or "关闭朋友圈" in message for message in success_logs))

        failure_logs = []
        result = execute_moments_publish_task(
            task={"text": "今天不错", "images": [], "privacy": "public", "tags": []},
            open_moments=lambda: FakeMomentsFailure(),
            sleep=lambda _seconds: None,
            random_delay=lambda _min, _max: 0,
            notify_error=lambda *_args, **_kwargs: None,
            nickname="测试微信",
            log_info=lambda message: failure_logs.append(message),
            log_error=lambda message: failure_logs.append(f"ERR:{message}"),
        )

        self.assertFalse(result)
        self.assertIn("朋友圈发布开始", failure_logs)
        self.assertTrue(any(message.startswith("ERR:朋友圈发布失败：") for message in failure_logs))

        read_error_logs = []
        result = execute_moments_publish_task(
            task={"text": "今天不错", "images": [], "privacy": "public", "tags": []},
            open_moments=lambda: FakeMomentsReadError(),
            sleep=lambda _seconds: None,
            random_delay=lambda _min, _max: 0,
            notify_error=lambda *_args, **_kwargs: None,
            nickname="测试微信",
            log_info=lambda message: read_error_logs.append(message),
            log_error=lambda message: read_error_logs.append(f"ERR:{message}"),
        )

        self.assertFalse(result)
        self.assertIn("朋友圈发布开始", read_error_logs)
        self.assertIn("ERR:朋友圈发布失败：读取朋友圈列表失败：窗口未响应", read_error_logs)


if __name__ == "__main__":
    unittest.main()
