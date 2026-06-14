import threading
import unittest
from types import SimpleNamespace

from wxbot_core import WXBot
from feature.moments_publisher import execute_moments_publish_task


class MomentsPublishLockingTests(unittest.TestCase):
    def test_publish_task_holds_wechat_action_lock_until_publish_finishes(self):
        bot = WXBot.__new__(WXBot)
        bot.wx = SimpleNamespace(nickname="测试微信")
        bot._wechat_action_lock = threading.RLock()
        bot._get_wechat_action_lock = WXBot._get_wechat_action_lock.__get__(bot, WXBot)
        lock_busy_during_publish = []

        def fake_execute(**kwargs):
            result_holder = []

            def contend_for_lock():
                lock = bot._get_wechat_action_lock()
                acquired = lock.acquire(blocking=False)
                result_holder.append(acquired)
                if acquired:
                    lock.release()

            thread = threading.Thread(target=contend_for_lock)
            thread.start()
            thread.join(timeout=2)
            lock_busy_during_publish.append(result_holder == [False])
            return True

        original = WXBot._execute_moments_publish_task.__globals__["execute_moments_publish_task"]
        WXBot._execute_moments_publish_task.__globals__["execute_moments_publish_task"] = fake_execute
        try:
            result = WXBot._execute_moments_publish_task(bot, {"id": "moments_test"})
        finally:
            WXBot._execute_moments_publish_task.__globals__["execute_moments_publish_task"] = original

        self.assertTrue(result)
        self.assertEqual(lock_busy_during_publish, [True])

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
