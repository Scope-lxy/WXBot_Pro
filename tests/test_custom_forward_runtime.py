import unittest
import queue
import threading
from types import SimpleNamespace
from unittest import mock

from core.message_pipeline import ConversationRef, MessageEnvelope
from feature.custom_forward_runtime import send_custom_forward_action
from wxbot_core import WXBot


class CustomForwardRuntimeTests(unittest.TestCase):
    def test_forward_reserves_echo_before_synchronous_self_callback(self):
        bot = WXBot.__new__(WXBot)
        bot.config = SimpleNamespace(cmd="管理员", group=[], memory_switch=True)
        bot._ui_owner = None
        bot._ui_ingress_queue = queue.Queue()
        bot._ui_ingress_stop = threading.Event()
        bot._get_wechat_action_lock = lambda: threading.Lock()
        bot._schedule_private_outbound_echo_fallback = lambda _target: True
        bot._ensure_message_runtime_state()

        class Message:
            type = "miniapp"
            content = "素材标题"

            def forward(self, _target, message=None):
                bot._enqueue_ui_message(
                    ConversationRef("张三", "private"),
                    MessageEnvelope(type="miniapp", attr="self", sender="self", content="素材标题"),
                )
                return True

        with mock.patch("feature.custom_forward_runtime.time.sleep", return_value=None):
            send_custom_forward_action(
                bot,
                {"kind": "forward", "target": "张三", "source_message": "附加文案"},
                SimpleNamespace(who="素材源"),
                Message(),
            )

        _conversation, callback = bot._ui_ingress_queue.get_nowait()
        self.assertTrue(callback._wxbot_private_outbound_echo)
        self.assertEqual(bot._get_private_message_sequence("张三"), 0)

    def test_forward_action_registers_private_outbound_echoes(self):
        class Lock:
            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc, tb):
                return False

        class Bot:
            def __init__(self):
                self.events = []

            def _get_wechat_action_lock(self):
                return Lock()

            def _remember_material_outbound_echoes(self, targets, msg_type, **kwargs):
                self.events.append(("reserve", list(targets), msg_type, kwargs))
                return "group-1"

            def _schedule_private_outbound_echo_fallback(self, _target):
                self.events.append(("schedule",))

        class Message:
            type = "miniapp"

            def __init__(self):
                self.forward_calls = []

            def forward(self, target, message=None):
                bot.events.append(("forward", target, message))
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
        self.assertEqual([event[0] for event in bot.events], ["reserve", "forward", "schedule"])
        self.assertEqual(bot.events[0][1:3], (["张三"], "miniapp"))
        self.assertEqual(bot.events[0][3]["preface"], "顺手给你看这个")

    def test_failed_forward_action_does_not_register_outbound_echoes(self):
        class Lock:
            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc, tb):
                return False

        class Bot:
            def __init__(self):
                self.events = []

            def _get_wechat_action_lock(self):
                return Lock()

            def _remember_material_outbound_echoes(self, *_args, **_kwargs):
                self.events.append("reserve")
                return "group-1"

            def _mark_private_outbound_echo_group_reported_failed(self, group_id):
                self.events.append(("reported_failed", group_id))

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

        self.assertEqual(bot.events, ["reserve", ("reported_failed", "group-1")])

    def test_owner_forward_never_uses_callback_message_object(self):
        class Bot:
            _ui_owner = object()

            def __init__(self):
                self.calls = []
                self.events = []

            def _ui_forward_message(self, chat, message, target, **kwargs):
                self.calls.append((chat.who, message, target, kwargs))
                return True

            def _remember_material_outbound_echoes(self, *_args, **_kwargs):
                self.events.append("reserve")
                return "group-1"

            def _schedule_private_outbound_echo_fallback(self, _target):
                self.events.append("schedule")

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
        self.assertEqual(bot.events, ["reserve", "schedule"])


if __name__ == "__main__":
    unittest.main()
