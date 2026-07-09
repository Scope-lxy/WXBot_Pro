import unittest
from types import SimpleNamespace

from core import wechat_ui_actions


class WechatUiActionsTests(unittest.TestCase):
    def test_hold_acquires_and_releases_threading_style_lock(self):
        events = []

        class Lock:
            def acquire(self, blocking=True):
                events.append(("acquire", blocking))
                return True

            def release(self):
                events.append(("release",))

        bot = SimpleNamespace(_get_wechat_action_lock=lambda: Lock())

        with wechat_ui_actions.hold(bot):
            events.append(("inside",))

        self.assertEqual(events, [("acquire", True), ("inside",), ("release",)])

    def test_try_acquire_returns_none_when_lock_is_busy(self):
        released = []
        test_case = self

        class BusyLock:
            def acquire(self, blocking=True):
                test_case.assertFalse(blocking)
                return False

            def release(self):
                released.append(True)

        bot = SimpleNamespace(_get_wechat_action_lock=lambda: BusyLock())

        self.assertIsNone(wechat_ui_actions.try_acquire(bot))
        self.assertEqual(released, [])
        self.assertTrue(wechat_ui_actions.is_busy(bot))

    def test_context_manager_lock_is_supported_for_test_doubles(self):
        events = []

        class ContextLock:
            def __enter__(self):
                events.append(("enter",))
                return self

            def __exit__(self, exc_type, exc, tb):
                events.append(("exit", exc_type))
                return False

        bot = SimpleNamespace(_get_wechat_action_lock=lambda: ContextLock())

        release = wechat_ui_actions.acquire(bot)
        self.assertIsNotNone(release)
        events.append(("inside",))
        release()

        self.assertEqual(events, [("enter",), ("inside",), ("exit", None)])

    def test_missing_lock_getter_is_treated_as_noop_for_pure_test_doubles(self):
        bot = SimpleNamespace()

        release = wechat_ui_actions.try_acquire(bot)
        self.assertIsNotNone(release)
        release()
        self.assertFalse(wechat_ui_actions.is_busy(bot))

    def test_invalid_configured_lock_fails_loudly(self):
        bot = SimpleNamespace(_get_wechat_action_lock=lambda: object())

        with self.assertRaises(RuntimeError):
            wechat_ui_actions.acquire(bot)


if __name__ == "__main__":
    unittest.main()
