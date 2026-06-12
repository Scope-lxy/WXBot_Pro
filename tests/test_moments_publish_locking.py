import threading
import unittest
from types import SimpleNamespace

from wxbot_core import WXBot


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


if __name__ == "__main__":
    unittest.main()
