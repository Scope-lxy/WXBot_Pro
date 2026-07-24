import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from core.wechat_ui_actions import UI_CALL_WAIT_TIMEOUT, UIIntentKind
from core.wechat_ui_runtime import UIClientFacade
from core.wechat_window import rebind_wechat_client, run_with_wechat_rebind_retry
from wxbot_core import WXBot


class WeChatWindowTests(unittest.TestCase):
    def test_rebind_submits_owner_intent_and_keeps_facade(self):
        submitted = []

        class FakeOwner:
            owner_thread_id = None

            def call(self, intent, timeout):
                submitted.append((intent, timeout))
                return {"nickname": "测试账号", "wx_id": "wxid-test"}

        bot = type("FakeBot", (), {
            "wx": None,
            "_ui_owner": FakeOwner(),
            "_ui_runtime": object(),
        })()
        client = rebind_wechat_client(bot)

        self.assertIsInstance(client, UIClientFacade)
        self.assertIs(client, bot.wx)
        self.assertEqual(submitted[0][0].kind, UIIntentKind.REBIND)
        self.assertIs(submitted[0][1], UI_CALL_WAIT_TIMEOUT)
        self.assertEqual(bot._ui_identity["wx_id"], "wxid-test")

    def test_business_failure_does_not_rebind_or_retry(self):
        action = Mock(side_effect=RuntimeError("Find Control Timeout: EditControl"))
        bot = object()

        with patch("core.wechat_window.rebind_wechat_client") as rebind:
            with self.assertRaisesRegex(RuntimeError, "Find Control Timeout"):
                run_with_wechat_rebind_retry(bot, action, attempts=2)

        action.assert_called_once()
        rebind.assert_not_called()

    def test_invalid_window_handle_rebinds_once_and_retries(self):
        action = Mock(side_effect=[OSError(1400, "MoveWindow", "无效的窗口句柄。"), "ok"])
        bot = object()

        with patch("core.wechat_window.rebind_wechat_client") as rebind:
            result = run_with_wechat_rebind_retry(bot, action, attempts=2)

        self.assertEqual(result, "ok")
        self.assertEqual(action.call_count, 2)
        rebind.assert_called_once_with(bot)

    def test_known_com_error_rebinds_once_and_retries(self):
        action = Mock(side_effect=[OSError(-2147220991, "事件无法调用任何订户"), "ok"])
        bot = object()

        with patch("core.wechat_window.rebind_wechat_client") as rebind:
            result = run_with_wechat_rebind_retry(bot, action, attempts=2)

        self.assertEqual(result, "ok")
        self.assertEqual(action.call_count, 2)
        rebind.assert_called_once_with(bot)

    def test_media_download_rebinds_once_and_retries(self):
        class FakeOwner:
            owner_thread_id = None

            def __init__(self):
                self.intents = []
                self.downloads = 0

            def call(self, intent, _timeout):
                self.intents.append(intent.kind)
                if intent.kind == UIIntentKind.REBIND:
                    return {"nickname": "测试账号", "wx_id": "wxid-test"}
                self.downloads += 1
                if self.downloads == 1:
                    raise OSError(1400, "MoveWindow", "无效的窗口句柄。")
                return r"C:\\temp\\image.png"

        bot = WXBot.__new__(WXBot)
        bot._ui_owner = FakeOwner()
        bot._ui_runtime = object()
        result = bot._ui_download_message(
            SimpleNamespace(who="张三", chat_type="private"),
            SimpleNamespace(type="image", attr="friend", sender="张三", content="图片"),
        )

        self.assertEqual(result, r"C:\\temp\\image.png")
        self.assertEqual(
            bot._ui_owner.intents,
            [UIIntentKind.DOWNLOAD_MEDIA, UIIntentKind.REBIND, UIIntentKind.DOWNLOAD_MEDIA],
        )


if __name__ == "__main__":
    unittest.main()
