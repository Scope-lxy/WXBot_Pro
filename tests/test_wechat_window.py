import unittest
from unittest.mock import patch

from core.wechat_window import rebind_wechat_client


class WeChatWindowTests(unittest.TestCase):
    def test_rebind_keeps_wxautox_default_resize_size(self):
        from wxautox4.param import WxParam

        original_size = WxParam.CHAT_WINDOW_SIZE
        WxParam.CHAT_WINDOW_SIZE = (1200, 6000)
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
        self.assertEqual(calls, [({"version": "微信"}, (1200, 6000))])


if __name__ == "__main__":
    unittest.main()
