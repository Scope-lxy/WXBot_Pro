import unittest
from unittest.mock import patch

from core.wechat_window import WECHAT_AUTO_RESIZE_SIZE, rebind_wechat_client


class WeChatWindowTests(unittest.TestCase):
    def test_rebind_sets_wxautox_resize_size_before_constructing_client(self):
        from wxautox4.param import WxParam

        original_size = WxParam.CHAT_WINDOW_SIZE
        calls = []

        class FakeWeChat:
            def __init__(self, **kwargs):
                calls.append((kwargs, WxParam.CHAT_WINDOW_SIZE))

        class FakeBot:
            wx = None

        try:
            with patch("wxautox4.WeChat", FakeWeChat):
                bot = FakeBot()
                client = rebind_wechat_client(bot, versions=("微信",))
        finally:
            WxParam.CHAT_WINDOW_SIZE = original_size

        self.assertIs(client, bot.wx)
        self.assertEqual(calls, [({"version": "微信"}, WECHAT_AUTO_RESIZE_SIZE)])


if __name__ == "__main__":
    unittest.main()
