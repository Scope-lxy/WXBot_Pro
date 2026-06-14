import unittest
import threading
from types import SimpleNamespace
from unittest import mock

from core.api import API_ERROR_REPLY_TEXT, DusAPI, OpenAIAPI, build_api_config_snapshot
from feature import message_routing
from feature.scheduled_messages import execute_scheduled_message_task
from wxbot_core import WXAUTO_SAVE_DIR_NAME, WXBot, WxParam


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

    def test_openai_chat_empty_content_logs_request_and_response_for_debugging(self):
        api = OpenAIAPI.__new__(OpenAIAPI)
        api.config = build_api_config_snapshot(
            {"sdk": "OpenAI", "model": "configured-model", "api_protocol": "chat_completions"},
            prompt="系统提示",
            max_retries=0,
            interface_index=3,
        )
        api.DS_NOW_MOD = api.config.model
        api.last_protocol_status = {"status": "unknown"}

        message_obj = SimpleNamespace(content="")
        choice = SimpleNamespace(message=message_obj, finish_reason="stop")
        response = SimpleNamespace(id="chatcmpl-test", model="mimo-v2.5", choices=[choice])
        create = mock.Mock(return_value=response)
        api.client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))

        with mock.patch("core.api.log") as fake_log:
            result = api.chat("动画表情 [早上好]")

        self.assertEqual(result, API_ERROR_REPLY_TEXT)
        debug_messages = [
            call.kwargs["message"]
            for call in fake_log.call_args_list
            if "API空响应诊断" in call.kwargs.get("message", "")
        ]
        self.assertEqual(len(debug_messages), 1)
        self.assertIn("接口4：configured-model", debug_messages[0])
        self.assertIn("动画表情 [早上好]", debug_messages[0])
        self.assertIn("chatcmpl-test", debug_messages[0])

    def test_openai_chat_nonstream_uses_reasoning_content_when_content_empty(self):
        api = OpenAIAPI.__new__(OpenAIAPI)
        api.config = build_api_config_snapshot(
            {"sdk": "OpenAI", "model": "configured-model", "api_protocol": "chat_completions"},
            prompt="系统提示",
            max_retries=0,
            interface_index=3,
        )
        api.DS_NOW_MOD = api.config.model
        api.last_protocol_status = {"status": "unknown"}

        message_obj = SimpleNamespace(content="", reasoning_content='{"add":[],"update":[],"delete":[]}')
        choice = SimpleNamespace(message=message_obj, finish_reason="stop")
        response = SimpleNamespace(id="chatcmpl-test", model="mimo-v2.5", choices=[choice])
        create = mock.Mock(return_value=response)
        api.client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))

        with mock.patch("core.api.log") as fake_log:
            result = api.chat("请输出 JSON")

        self.assertEqual(result, '{"add":[],"update":[],"delete":[]}')
        self.assertEqual(api.last_protocol_status, {"status": "chat_completions_ok"})
        self.assertFalse(
            any("API返回成功" in call.kwargs.get("message", "") for call in fake_log.call_args_list)
        )

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

    def test_lightweight_send_queue_logs_only_final_outcome(self):
        bot = WXBot.__new__(WXBot)
        bot._get_wechat_action_lock = lambda: threading.RLock()
        bot._get_chat_send_lock = lambda _target: threading.RLock()
        bot._wechat_action_lock_is_busy = lambda: False
        bot._ensure_target_listen_chat_for_send = lambda _target: SimpleNamespace(
            SendMsg=lambda _text: True
        )

        logs = []
        with mock.patch("wxbot_core.log", side_effect=lambda **kwargs: logs.append(kwargs.get("message", ""))):
            self.assertEqual(
                bot._queue_lightweight_send("张三", [{"type": "text", "text": "你好"}], source="text")["status"],
                "queued",
            )
            self.assertTrue(bot._flush_lightweight_send_queue())

        self.assertEqual(logs, ["[轻量发送队列] 张三 延后发送已完成"])

        bot._ensure_lightweight_send_queue_state()
        bot._lightweight_send_queue.clear()
        bot._send_lightweight_actions_to_child = lambda _target, _actions: False
        logs = []
        with mock.patch("wxbot_core.log", side_effect=lambda **kwargs: logs.append(kwargs.get("message", ""))):
            bot._queue_lightweight_send("李四", [{"type": "text", "text": "你好"}], source="text")
            self.assertFalse(bot._flush_lightweight_send_queue())

        self.assertEqual(logs, ["[轻量发送队列] 李四 待发送任务暂未发出，保留队列"])

    def test_wxauto_download_dir_follows_kernel_save_path(self):
        bot = WXBot.__new__(WXBot)
        original = getattr(WxParam, "DEFAULT_SAVE_PATH", "")
        try:
            WxParam.DEFAULT_SAVE_PATH = r"C:\tmp\wxauto_custom"
            self.assertEqual(bot._wxauto_download_dir(), r"C:\tmp\wxauto_custom")

            WxParam.DEFAULT_SAVE_PATH = ""
            self.assertTrue(bot._wxauto_download_dir().endswith(WXAUTO_SAVE_DIR_NAME))
        finally:
            WxParam.DEFAULT_SAVE_PATH = original

    def test_stop_wxbot_cancels_pending_private_merge_timer(self):
        class FakeTimer:
            def __init__(self):
                self.cancelled = False

            def cancel(self):
                self.cancelled = True

        class FakeListener:
            def __init__(self):
                self.stopped = False

            def StopListening(self):
                self.stopped = True

        bot = WXBot.__new__(WXBot)
        timer = FakeTimer()
        bot.run_flag = True
        bot.wx = FakeListener()
        bot._chat_merge_lock = threading.Lock()
        bot._chat_merge_timers = {"张三": timer}
        bot._chat_merge_buffers = {"张三": [SimpleNamespace(content="你好")]}
        bot._chat_reply_versions = {"张三": 1}
        bot._incoming_seen_lock = threading.Lock()
        bot._incoming_seen_ids = {}
        bot._incoming_seen_fingerprints = {}
        bot._chat_send_locks = {}
        bot._private_reply_runtime_turns = {}
        bot._private_reply_persisted_echoes = {}
        bot._pending_visual_contexts = {}
        bot._conversation_memory_dirty_lock = threading.Lock()
        bot._conversation_memory_dirty_chats = {}
        bot._conversation_memory_worker_running = False
        bot.is_err = lambda *_args, **_kwargs: self.fail("停止不应报错")

        self.assertTrue(WXBot.stop_wxbot(bot))

        self.assertFalse(bot.run_flag)
        self.assertTrue(bot.is_stop_requested())
        self.assertTrue(timer.cancelled)
        self.assertEqual(bot._chat_merge_timers, {})
        self.assertEqual(bot._chat_merge_buffers, {})
        self.assertTrue(bot.wx.stopped)

    def test_reset_stop_request_allows_next_start(self):
        bot = WXBot.__new__(WXBot)
        bot._ensure_stop_requested_event().set()

        bot._reset_stop_request()

        self.assertFalse(bot.is_stop_requested())

    def test_scheduled_message_stops_before_next_send(self):
        sends = []
        stop_after_first_send = {"value": False}

        def send_text(target, msg):
            sends.append((target, msg))
            stop_after_first_send["value"] = True
            return True

        result = execute_scheduled_message_task(
            task={"targets": ["张三", "李四"], "msgs": ["你好"]},
            send_text=send_text,
            send_file=lambda *_args: True,
            is_image_path=lambda _path: False,
            human_delay=lambda: None,
            should_stop=lambda: stop_after_first_send["value"],
            notify_error=lambda *_args: None,
            nickname="wxbot",
            scheduled_tasks=[],
            config_data={},
            save_config=None,
        )

        self.assertEqual(sends, [("张三", "你好")])
        self.assertEqual(result["result_type"], "manual_stop")


if __name__ == "__main__":
    unittest.main()
