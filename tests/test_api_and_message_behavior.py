import unittest
from types import SimpleNamespace
from unittest import mock

from core.api import API_ERROR_REPLY_TEXT, DusAPI, OpenAIAPI, build_api_config_snapshot
from feature import message_routing
from wxbot_core import WXBot


class ApiBehaviorTests(unittest.TestCase):
    def test_openai_chat_success_still_returns_model_text(self):
        api = OpenAIAPI.__new__(OpenAIAPI)
        api.config = build_api_config_snapshot(
            {"sdk": "OpenAI", "model": "configured-model", "api_protocol": "chat_completions"},
            interface_index=0,
        )
        api.DS_NOW_MOD = api.config.model
        api.last_protocol_status = {"status": "unknown"}
        api._call_chat_completions_api = lambda *_args, **_kwargs: "模型回复"

        result = api.chat("你好")

        self.assertEqual(result, "模型回复")
        self.assertEqual(api.last_protocol_status, {"status": "chat_completions_ok"})

    def test_openai_chat_failure_still_returns_fixed_error_reply(self):
        api = OpenAIAPI.__new__(OpenAIAPI)
        api.config = build_api_config_snapshot(
            {"sdk": "OpenAI", "model": "configured-model", "api_protocol": "chat_completions"},
            interface_index=0,
        )
        api.DS_NOW_MOD = api.config.model
        api.last_protocol_status = {"status": "unknown"}

        def fail_call(*_args, **_kwargs):
            raise RuntimeError("网络失败")

        api._call_chat_completions_api = fail_call

        with mock.patch("core.api.log") as fake_log:
            result = api.chat("你好", model="actual-model")

        self.assertEqual(result, API_ERROR_REPLY_TEXT)
        self.assertEqual(api.last_protocol_status, {"status": "failed"})
        fake_log.assert_called_once()
        self.assertIn("接口1：actual-model", fake_log.call_args.kwargs["message"])

    def test_dusapi_gpt_nonstream_still_returns_text_and_sends_reasoning(self):
        api = DusAPI(
            build_api_config_snapshot(
                {
                    "sdk": "DusAPI",
                    "key": "test-key",
                    "url": "https://example.test",
                    "model": "gpt-test",
                    "reasoning_effort": "high",
                },
                max_retries=0,
                interface_index=1,
            )
        )
        captured_payloads = []

        class FakeResponse:
            status_code = 200
            text = "{}"
            encoding = "utf-8"

            def raise_for_status(self):
                return None

            def json(self):
                return {"output_text": "Dus 回复"}

        def fake_post(_url, headers=None, json=None, timeout=None, stream=False):
            captured_payloads.append(json)
            return FakeResponse()

        with mock.patch("core.api.requests.post", side_effect=fake_post):
            result = api.chat("你好", stream=False)

        self.assertEqual(result, "Dus 回复")
        self.assertEqual(captured_payloads[0]["model"], "gpt-test")
        self.assertEqual(captured_payloads[0]["reasoning"], {"effort": "high"})


class MessageBehaviorTests(unittest.TestCase):
    def test_friend_message_callback_still_dispatches_and_sends_reply(self):
        class FakeChat:
            who = "张三"
            chat_type = "private"

            def __init__(self):
                self.sent = []

            def SendMsg(self, msg=None, message=None, **_kwargs):
                self.sent.append(msg if msg is not None else message)
                return True

        class FakeBot:
            def __init__(self):
                self.config = SimpleNamespace(
                    cmd="管理员",
                    AllListen_switch=False,
                    listen_list=["张三"],
                    group=[],
                    group_switch=False,
                    custom_forward_switch=False,
                    chat_image_recognition_switch=False,
                    chat_voice_recognition_switch=False,
                )
                self.wx = SimpleNamespace(nickname="wxbot")
                self.msg_received_count = 0
                self.last_msg_time = ""
                self.last_msg_sender = ""

            def _record_received_message(self):
                self.msg_received_count += 1

            def _handle_admin_forward_input(self, _chat, _msg):
                return False

            def _handle_admin_moments_input(self, _chat, _msg):
                return False

            def _handle_material_source_message(self, _chat, _msg):
                return False

            def process_message(self, chat, message):
                return chat.SendMsg(msg=f"回复：{message.content}")

            def is_err(self, *_args, **_kwargs):
                raise AssertionError("不应进入错误通知路径")

        bot = FakeBot()
        chat = FakeChat()
        msg = SimpleNamespace(type="text", attr="friend", sender="张三", content="你好")

        result = message_routing.handle_friend_message_callback(bot, msg, chat, text="")

        self.assertIsNone(result)
        self.assertEqual(bot.msg_received_count, 1)
        self.assertEqual(chat.sent, ["回复：你好"])
        self.assertEqual(bot.last_msg_sender, "张三")

    def test_alllisten_timestamp_update_still_refreshes_runtime_entry(self):
        bot = SimpleNamespace(
            config=SimpleNamespace(AllListen_switch=True),
            all_Mode_listen_list=[["张三", 1.0]],
        )

        with (
            mock.patch("feature.message_routing._bot_time_module", return_value=SimpleNamespace(time=lambda: 9.0)),
            mock.patch("feature.message_routing._bot_log"),
        ):
            message_routing._update_alllisten_timestamp(bot, "张三")

        self.assertEqual(bot.all_Mode_listen_list, [["张三", 9.0]])

    def test_private_message_dedupes_same_content_from_different_ingress_sources(self):
        bot = WXBot.__new__(WXBot)
        first = SimpleNamespace(
            type="text",
            attr="friend",
            sender="张三",
            content="你好",
            id="global-id",
            _wxbot_ingress_source="global",
        )
        duplicate = SimpleNamespace(
            type="text",
            attr="friend",
            sender="张三",
            content="你好",
            id="subwindow-id",
            _wxbot_ingress_source="subwindow",
        )

        self.assertTrue(bot._mark_message_content_fingerprint_seen("张三", first))
        self.assertFalse(bot._mark_message_content_fingerprint_seen("张三", duplicate))

    def test_private_message_allows_same_content_from_same_ingress_source(self):
        bot = WXBot.__new__(WXBot)
        first = SimpleNamespace(
            type="text",
            attr="friend",
            sender="张三",
            content="你好",
            id="subwindow-id-1",
            _wxbot_ingress_source="subwindow",
        )
        second = SimpleNamespace(
            type="text",
            attr="friend",
            sender="张三",
            content="你好",
            id="subwindow-id-2",
            _wxbot_ingress_source="subwindow",
        )

        self.assertTrue(bot._mark_message_content_fingerprint_seen("张三", first))
        self.assertTrue(bot._mark_message_content_fingerprint_seen("张三", second))

    def test_verified_send_chat_does_not_probe_wechat_when_candidate_missing(self):
        bot = WXBot.__new__(WXBot)
        bot._get_verified_subwindow = lambda _target: self.fail("不应主动探测微信子窗口")

        self.assertIsNone(bot._verified_send_chat("张三", None))


if __name__ == "__main__":
    unittest.main()
