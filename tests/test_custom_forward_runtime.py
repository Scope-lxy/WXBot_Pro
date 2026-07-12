import unittest
from types import SimpleNamespace

from feature.custom_forward_runtime import send_custom_forward_action


class CustomForwardRuntimeTests(unittest.TestCase):
    def test_forward_action_registers_private_outbound_echoes(self):
        class Lock:
            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc, tb):
                return False

        class Bot:
            def __init__(self):
                self.echoes = []

            def _get_wechat_action_lock(self):
                return Lock()

            def _remember_private_outbound_echo(
                self,
                target,
                msg_type="text",
                content="",
                *,
                source="",
                path="",
                count=1,
            ):
                self.echoes.append((target, msg_type, content, source, path, count))
                return True

        class Message:
            type = "miniapp"

            def __init__(self):
                self.forward_calls = []

            def forward(self, target, message=None):
                self.forward_calls.append((target, message))

        bot = Bot()
        message = Message()

        send_custom_forward_action(
            bot,
            {
                "kind": "forward",
                "target": "张三",
                "source_message": "顺手给你看这个",
            },
            SimpleNamespace(who="素材源"),
            message,
        )

        self.assertEqual(message.forward_calls, [("张三", "顺手给你看这个")])
        self.assertEqual(
            bot.echoes,
            [
                ("张三", "text", "顺手给你看这个", "custom_forward", "", 1),
                ("张三", "miniapp", "", "custom_forward", "", 1),
            ],
        )

    def test_failed_forward_action_does_not_register_outbound_echoes(self):
        class Lock:
            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc, tb):
                return False

        class Bot:
            def __init__(self):
                self.echoes = []

            def _get_wechat_action_lock(self):
                return Lock()

            def _remember_private_outbound_echo(self, *args, **kwargs):
                self.echoes.append((args, kwargs))
                return True

        class Message:
            type = "miniapp"

            def forward(self, target, message=None):
                return {"status": "failed", "message": "转发失败"}

        bot = Bot()

        send_custom_forward_action(
            bot,
            {
                "kind": "forward",
                "target": "张三",
                "source_message": "顺手给你看这个",
            },
            SimpleNamespace(who="素材源"),
            Message(),
        )

        self.assertEqual(bot.echoes, [])

    def test_owner_forward_never_uses_callback_message_object(self):
        class Bot:
            _ui_owner = object()

            def __init__(self):
                self.calls = []
                self.echoes = []

            def _ui_forward_message(self, chat, message, target, **kwargs):
                self.calls.append((chat.who, message, target, kwargs))
                return True

            def _remember_private_outbound_echo(self, *args, **kwargs):
                self.echoes.append((args, kwargs))

        class Message:
            type = "miniapp"

            def forward(self, *_args, **_kwargs):
                raise AssertionError("owner 模式不得在回调线程直接转发原消息对象")

        bot = Bot()
        message = Message()
        send_custom_forward_action(
            bot,
            {"kind": "forward", "target": "张三", "source_message": "给你看这个"},
            SimpleNamespace(who="素材源"),
            message,
        )

        self.assertEqual(len(bot.calls), 1)
        self.assertEqual(bot.calls[0][2], "张三")
        self.assertEqual(bot.calls[0][3]["preface"], "给你看这个")


if __name__ == "__main__":
    unittest.main()
