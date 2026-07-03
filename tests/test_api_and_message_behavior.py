import base64
import os
import unittest
import threading
import tempfile
from datetime import datetime, timedelta
from collections import deque
from types import SimpleNamespace
from unittest import mock

from core.api import API_ERROR_REPLY_TEXT, DusAPI, OpenAIAPI, build_api_config_snapshot
from core.prompting import build_current_turn_user_message, build_image_user_message
from core.reply_pipeline import ImageReplyPipeline, ImageReplyRequest
from core.reply_count_store import ReplyCountStore
from core.vision_bridge import VisionNote
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

    def test_openai_chat_nonstandard_list_response_becomes_api_error(self):
        api = OpenAIAPI.__new__(OpenAIAPI)
        api.config = build_api_config_snapshot(
            {"sdk": "OpenAI", "model": "configured-model", "api_protocol": "chat_completions"},
            prompt="系统提示",
            max_retries=0,
            interface_index=3,
        )
        api.DS_NOW_MOD = api.config.model
        api.last_protocol_status = {"status": "unknown"}
        create = mock.Mock(return_value=[{"message": {"content": "unexpected"}}])
        api.client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))

        with mock.patch("core.api.log") as fake_log:
            result = api.chat("你好")

        self.assertEqual(result, API_ERROR_REPLY_TEXT)
        self.assertEqual(api.last_protocol_status, {"status": "failed"})
        messages = [call.kwargs.get("message", "") for call in fake_log.call_args_list]
        self.assertTrue(any("Chat Completions 响应中没有 choices" in message for message in messages))
        self.assertFalse(any("'list' object has no attribute 'choices'" in message for message in messages))

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

    def test_openai_image_data_url_uses_prepared_ai_image(self):
        with tempfile.TemporaryDirectory() as tmp:
            original_path = os.path.join(tmp, "original.png")
            prepared_path = os.path.join(tmp, "prepared.jpg")
            with open(original_path, "wb") as file:
                file.write(b"original-bytes")
            with open(prepared_path, "wb") as file:
                file.write(b"prepared-bytes")

            with mock.patch("core.api.prepare_ai_image_path", return_value=prepared_path):
                data_url = OpenAIAPI._image_to_data_url(image_path=original_path)

            encoded = data_url.split(",", 1)[1]
            self.assertEqual(base64.standard_b64decode(encoded), b"prepared-bytes")


class MessageBehaviorTests(unittest.TestCase):
    def test_current_turn_user_message_places_runtime_time_next_to_message(self):
        result = build_current_turn_user_message("早，姐姐", now="2026-07-03 13:57")

        self.assertIn("[运行信息]", result)
        self.assertIn("处理时间：2026-07-03 13:57", result)
        self.assertIn("[用户消息]\n早，姐姐", result)

    def test_two_stage_image_reply_places_visual_note_in_current_user_message(self):
        captured = {}

        class FakeFinalApi:
            def chat(self, message, **kwargs):
                captured["message"] = message
                captured["kwargs"] = kwargs
                return "最终回复"

        def build_prompt(*_args, **kwargs):
            captured["prompt_kwargs"] = kwargs
            return "prompt"

        pipeline = ImageReplyPipeline(
            prompt_builder=build_prompt,
            image_parse_block_builder=lambda: "IMAGE_RULES",
            user_message_builder=build_image_user_message,
            vision_bridge=SimpleNamespace(
                analyze=lambda **_kwargs: VisionNote(
                    overview="一张蓝色会标。",
                    visible_text="ERC 博济全球慈善互助会；EXTENSIVE RELIEVE CHARITY MUTUAL AID",
                    key_details="圆形徽章，中间是地球和绿色叶片。",
                    uncertainty="顶部部分字形略模糊。",
                )
            ),
        )

        result = pipeline.reply(ImageReplyRequest(
            chat_name="张三",
            chat_type="private",
            attached_text="这里写的什么？",
            sender="张三",
            history=[],
            final_api=FakeFinalApi(),
            recognition_api=SimpleNamespace(),
            final_api_supports_vision=False,
            image_path=r"C:\tmp\logo.png",
        ))

        self.assertEqual(result, "最终回复")
        self.assertIn("[运行信息]", captured["message"])
        self.assertIn("本轮消息包含图片：", captured["message"])
        self.assertIn("[图片]一张蓝色会标。", captured["message"])
        self.assertIn("可见文字：ERC 博济全球慈善互助会", captured["message"])
        self.assertIn("消息内容：这里写的什么？", captured["message"])
        self.assertNotIn("图片概览：", captured["message"])
        self.assertEqual(captured["prompt_kwargs"]["image_parse_block"], "IMAGE_RULES")

    def test_private_image_reply_generates_visual_note_before_final_reply(self):
        bot = WXBot.__new__(WXBot)
        bot.config = SimpleNamespace(chat_image_recognition_api=0)
        note = "图片概览：一张自拍。\n可见文字：无。\n关键细节：戴着帽子。\n不确定项：无。"
        generated = []
        remembered = []
        captured = {}
        bot._generate_visual_notes_for_image_paths = (
            lambda chat_type, paths, **kwargs: generated.append((chat_type, list(paths), kwargs)) or [note]
        )
        bot._remember_visual_notes = (
            lambda chat_name, paths, notes: remembered.append((chat_name, list(paths), list(notes))) or True
        )
        bot._reply_image_message = lambda **kwargs: captured.update(kwargs) or "图片回复"
        bot._get_chat_api = lambda _chat: SimpleNamespace()
        bot._chat_reply_api_supports_vision = lambda _chat: True

        result = bot._reply_private_image_message(
            SimpleNamespace(who="张三"),
            history=[],
            image_paths=[r"C:\tmp\selfie.png"],
        )

        self.assertEqual(result, "图片回复")
        self.assertEqual(generated[0][0], "private")
        self.assertEqual(generated[0][1], [r"C:\tmp\selfie.png"])
        self.assertEqual(remembered, [("张三", [r"C:\tmp\selfie.png"], [note])])
        self.assertEqual(captured["visual_notes"], [note])

    def test_private_pending_visual_context_clears_only_after_notes_exist(self):
        bot = WXBot.__new__(WXBot)
        bot._ensure_message_runtime_state()
        bot._set_pending_visual_context("张三", [r"C:\tmp\selfie.png"], visual_notes=[""])

        self.assertFalse(bot._pending_visual_context_ready_to_clear("张三"))

        bot._set_pending_visual_context(
            "张三",
            [r"C:\tmp\selfie.png"],
            visual_notes=["图片概览：一张自拍。\n可见文字：无。\n关键细节：戴着帽子。\n不确定项：无。"],
        )

        self.assertTrue(bot._pending_visual_context_ready_to_clear("张三"))

    def test_group_image_reply_generates_visual_note_before_final_reply(self):
        bot = WXBot.__new__(WXBot)
        bot.config = SimpleNamespace(group_image_recognition_api=0)
        note = "图片概览：一张活动海报。\n可见文字：周六 19:00。\n关键细节：地点在东门。\n不确定项：无。"
        generated = []
        remembered = []
        captured = {}
        bot._generate_visual_notes_for_image_paths = (
            lambda chat_type, paths, **kwargs: generated.append((chat_type, list(paths), kwargs)) or [note]
        )
        bot._remember_visual_notes = (
            lambda chat_name, paths, notes: remembered.append((chat_name, list(paths), list(notes))) or True
        )
        bot._reply_image_message = lambda **kwargs: captured.update(kwargs) or "群图回复"
        bot._get_group_api = lambda _group: SimpleNamespace()
        bot._group_reply_api_supports_vision = lambda _group: True

        result = bot._reply_group_image_message(
            SimpleNamespace(who="测试群"),
            SimpleNamespace(sender="李四"),
            history=[],
            image_paths=[r"C:\tmp\poster.png"],
            attached_text="几点开始？",
        )

        self.assertEqual(result, "群图回复")
        self.assertEqual(generated[0][0], "group")
        self.assertEqual(generated[0][1], [r"C:\tmp\poster.png"])
        self.assertEqual(remembered, [("测试群", [r"C:\tmp\poster.png"], [note])])
        self.assertEqual(captured["visual_notes"], [note])

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

    def test_private_message_dedupes_same_content_from_same_ingress_source(self):
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
        self.assertFalse(bot._mark_message_content_fingerprint_seen("张三", second))

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

    def test_stale_private_ai_reply_is_dropped_from_lightweight_send_queue(self):
        bot = WXBot.__new__(WXBot)
        bot._get_wechat_action_lock = lambda: threading.RLock()
        bot._wechat_action_lock_is_busy = lambda: False
        bot._send_lightweight_actions_to_child = lambda _target, _actions: self.fail("旧回复不应继续发送")
        bot._ensure_message_runtime_state()
        bot._next_private_message_sequence("张三")
        expected_sequence = bot._get_private_message_sequence("张三")
        bot._queue_text_reply_until_target_verified(
            "张三",
            ["旧回复"],
            source="private_ai_reply",
            expected_sequence=expected_sequence,
        )
        bot._next_private_message_sequence("张三")

        logs = []
        with mock.patch("wxbot_core.log", side_effect=lambda **kwargs: logs.append(kwargs.get("message", ""))):
            self.assertFalse(bot._flush_lightweight_send_queue())

        self.assertEqual(bot._lightweight_send_queue, {})
        self.assertEqual(logs, ["[轻量发送队列] 张三 已有新消息，丢弃上一轮过期回复"])

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

    def test_stop_wxbot_cancels_pending_private_message_pipeline_timers(self):
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
        bot._private_message_pipelines = {
            "张三": {
                "open_messages": [SimpleNamespace(content="你好")],
                "open_started_at": 1.0,
                "idle_timer": timer,
                "max_timer": timer,
                "queued_batches": deque([[SimpleNamespace(content="下一批")]]),
                "worker_running": True,
            }
        }
        bot._private_message_sequence_by_chat = {"张三": 1}
        bot._incoming_seen_lock = threading.Lock()
        bot._incoming_seen_ids = {}
        bot._incoming_seen_fingerprints = {}
        bot._chat_send_locks = {}
        bot._private_reply_runtime_turns = {}
        bot._private_reply_persisted_echoes = {}
        bot._pending_visual_contexts = {}
        bot._chat_memory_dirty_lock = threading.Lock()
        bot._chat_memory_dirty_chats = {}
        bot._chat_memory_worker_running = False
        bot.is_err = lambda *_args, **_kwargs: self.fail("停止不应报错")

        self.assertTrue(WXBot.stop_wxbot(bot))

        self.assertFalse(bot.run_flag)
        self.assertTrue(bot.is_stop_requested())
        self.assertTrue(timer.cancelled)
        pipeline = bot._private_message_pipelines["张三"]
        self.assertEqual(pipeline["open_messages"], [])
        self.assertEqual(list(pipeline["queued_batches"]), [])
        self.assertFalse(pipeline["worker_running"])
        self.assertTrue(bot.wx.stopped)

    def test_private_message_pipeline_uses_idle_and_max_wait_timers(self):
        class FakeTimer:
            def __init__(self, seconds, callback, args):
                self.seconds = seconds
                self.callback = callback
                self.args = args
                self.cancelled = False
                self.daemon = False

            def start(self):
                return None

            def cancel(self):
                self.cancelled = True

        timers = []

        def fake_timer(seconds, callback, args=()):
            timer = FakeTimer(seconds, callback, args)
            timers.append(timer)
            return timer

        bot = WXBot.__new__(WXBot)
        bot.config = SimpleNamespace(
            chat_message_merge_delay=4,
            chat_voice_recognition_switch=False,
            chat_image_recognition_switch=False,
        )
        bot.is_stop_requested = lambda: False
        chat = SimpleNamespace(who="张三")
        first = SimpleNamespace(type="text", attr="friend", sender="张三", content="第一条", id="1")
        second = SimpleNamespace(type="text", attr="friend", sender="张三", content="第二条", id="2")

        with mock.patch("wxbot_core.threading.Timer", side_effect=fake_timer):
            self.assertTrue(bot._enqueue_private_message_for_ai(chat, first))
            self.assertEqual(timers[0].seconds, 4)
            self.assertAlmostEqual(timers[1].seconds, 12, places=2)
            self.assertTrue(bot._enqueue_private_message_for_ai(chat, second))

        self.assertEqual(len(timers), 3)
        self.assertEqual(timers[2].seconds, 4)
        self.assertTrue(timers[0].cancelled)
        pipeline = bot._private_message_pipelines["张三"]
        self.assertEqual([msg.content for msg in pipeline["open_messages"]], ["第一条", "第二条"])
        self.assertEqual(bot._get_private_message_sequence("张三"), 2)

    def test_voice_transcription_fallback_once_resets_with_reply_window(self):
        with tempfile.TemporaryDirectory() as tmp:
            bot = WXBot.__new__(WXBot)
            bot.config = SimpleNamespace(
                voice_transcription_fallback_text="刚才那条语音，我有点没听清",
                voice_transcription_fallback_reply_once=True,
                text_reply_limit_hours=24,
            )
            bot.reply_count_store = ReplyCountStore(f"{tmp}/reply_count.json")
            bot.wx = None
            bot.is_stop_requested = lambda: False

            sent = []
            logs = []
            chat = SimpleNamespace(who="张三", SendMsg=lambda text: sent.append(text) or True)

            with mock.patch("wxbot_core.log", side_effect=lambda *args, **kwargs: logs.append((kwargs.get("level", "INFO"), kwargs.get("message", "")))):
                self.assertTrue(bot._send_private_voice_transcription_fallback(chat))
                self.assertTrue(bot._send_private_voice_transcription_fallback(chat))
            self.assertEqual(sent, ["刚才那条语音，我有点没听清"])
            self.assertTrue(any(level == "INFO" and "已发送兜底提示" in message for level, message in logs))
            self.assertTrue(any(level == "INFO" and "已跳过兜底提示" in message for level, message in logs))
            self.assertFalse(any(level == "WARNING" and "已发送兜底提示" in message for level, message in logs))

            started_at = datetime.now() - timedelta(hours=25)
            bot.reply_count_store.data["users"]["张三"]["window_started_at"] = started_at.isoformat(timespec="seconds")

            self.assertTrue(bot._send_private_voice_transcription_fallback(chat))
            self.assertEqual(sent, ["刚才那条语音，我有点没听清", "刚才那条语音，我有点没听清"])

    def test_empty_voice_transcription_fallback_is_silent(self):
        with tempfile.TemporaryDirectory() as tmp:
            bot = WXBot.__new__(WXBot)
            bot.config = SimpleNamespace(
                voice_transcription_fallback_text="",
                voice_transcription_fallback_reply_once=True,
                text_reply_limit_hours=24,
            )
            bot.reply_count_store = ReplyCountStore(f"{tmp}/reply_count.json")
            bot.wx = None

            sent = []
            logs = []
            chat = SimpleNamespace(who="张三", SendMsg=lambda text: sent.append(text) or True)

            with mock.patch("wxbot_core.log", side_effect=lambda *args, **kwargs: logs.append((kwargs.get("level", "INFO"), kwargs.get("message", "")))):
                self.assertTrue(bot._send_private_voice_transcription_fallback(chat))
            self.assertEqual(sent, [])
            self.assertTrue(any(level == "INFO" and "未配置兜底提示" in message for level, message in logs))
            self.assertFalse(any(level == "WARNING" and "未配置兜底提示" in message for level, message in logs))

    def test_text_reply_limit_logs_warning_when_capacity_is_hit(self):
        with tempfile.TemporaryDirectory() as tmp:
            bot = WXBot.__new__(WXBot)
            bot.config = SimpleNamespace(
                text_reply_limit_switch=True,
                text_reply_limit_count=1,
                text_reply_limit_hours=24,
                text_reply_limit_reply_once=True,
                text_reply_limit_ai_reply=False,
                text_reply_limit_reply="先休息一下",
            )
            bot.reply_count_store = ReplyCountStore(f"{tmp}/reply_count.json")
            bot.reply_count_store.increment_ai_count("张三", limit_hours=24)
            bot._record_replied_message_success = lambda: None
            sent = []
            bot._send_private_ai_reply_parts = lambda _chat, parts, **_kwargs: sent.extend(parts) or (True, True)
            chat = SimpleNamespace(who="张三")
            logs = []

            with mock.patch("wxbot_core.log", side_effect=lambda *args, **kwargs: logs.append((kwargs.get("level", "INFO"), kwargs.get("message", "")))):
                handled, result = bot._check_text_reply_limit(chat, "张三")

            self.assertTrue(handled)
            self.assertTrue(result)
            self.assertEqual(sent, ["先休息一下"])
            self.assertTrue(any(level == "WARNING" and "触发回复上限" in message for level, message in logs))

    def test_voice_reply_limit_logs_warning_and_falls_back_to_text(self):
        bot = WXBot.__new__(WXBot)
        bot.config = SimpleNamespace(cmd="文件传输助手", DATA_DIR="")
        state = {
            "limits": {
                "private:张三": {
                    "count": 1,
                    "window_started_at": datetime.now().isoformat(timespec="seconds"),
                }
            },
            "private_sessions": {},
        }
        from feature.voice_reply import VoiceReplyState

        bot._voice_reply_state = VoiceReplyState(limits=state["limits"], private_sessions={})
        bot._private_reply_can_continue = lambda *_args, **_kwargs: True
        logs = []
        chat = SimpleNamespace(who="张三")

        with mock.patch("wxbot_core.log", side_effect=lambda *args, **kwargs: logs.append((kwargs.get("level", "INFO"), kwargs.get("message", "")))):
            result = bot._try_send_voice_reply(
                chat,
                "你好",
                state_key="private:张三",
                cooldown_minutes=0,
                limit_count=1,
                limit_hours=24,
            )

        self.assertFalse(result)
        self.assertTrue(any(level == "WARNING" and "语音回复触发上限" in message for level, message in logs))

    def test_prepare_voice_uses_wechat_auto_text_without_to_text(self):
        bot = WXBot.__new__(WXBot)
        bot.config = SimpleNamespace(
            AllListen_switch=False,
            listen_list=["张三"],
            group=[],
            chat_image_recognition_switch=False,
            chat_voice_recognition_switch=True,
        )
        msg = SimpleNamespace(
            attr="friend",
            sender="张三",
            type="voice",
            content='语音2"秒你好',
            to_text=lambda: self.fail("不应主动调用微信右键语音转文字"),
        )
        chat = SimpleNamespace(who="张三", chat_type="private")

        message_routing.prepare_message_media(bot, msg, chat)

        self.assertFalse(getattr(msg, "_skip_ai_reply", False))
        self.assertFalse(getattr(msg, "_voice_transcription_failed", False))
        self.assertEqual(msg.content, '语音2"秒你好')

    def test_prepare_pending_private_voice_queues_delayed_reread(self):
        queued = []
        bot = WXBot.__new__(WXBot)
        bot.config = SimpleNamespace(
            AllListen_switch=False,
            listen_list=["张三"],
            group=[],
            chat_image_recognition_switch=False,
            chat_voice_recognition_switch=True,
        )
        bot._queue_pending_private_voice_transcription = lambda chat, msg: queued.append((chat.who, msg.content)) or True
        msg = SimpleNamespace(attr="friend", sender="张三", type="voice", content='语音2"秒', id="v1")
        chat = SimpleNamespace(who="张三", chat_type="private")

        message_routing.prepare_message_media(bot, msg, chat)

        self.assertTrue(getattr(msg, "_skip_ai_reply", False))
        self.assertTrue(getattr(msg, "_skip_memory", False))
        self.assertEqual(queued, [("张三", '语音2"秒')])

    def test_pending_private_voice_reread_enqueues_resolved_text(self):
        bot = WXBot.__new__(WXBot)
        bot.config = SimpleNamespace(memory_switch=False)
        bot._chat_merge_lock = threading.Lock()
        bot._pending_private_voice_transcription = {}
        bot.is_stop_requested = lambda: False

        class TryLock:
            def acquire(self, blocking=True):
                return True

            def release(self):
                pass

        bot._get_wechat_action_lock = lambda: TryLock()
        enqueued = []
        bot._enqueue_private_message_for_ai = lambda chat, msg: enqueued.append((chat.who, msg.content)) or True
        timer_calls = []
        bot._schedule_private_message_timer = lambda seconds, callback, chat: timer_calls.append((seconds, callback, chat)) or SimpleNamespace(cancel=lambda: None)
        original = SimpleNamespace(id="v1", attr="friend", sender="张三", type="voice", content='语音2"秒')
        resolved = SimpleNamespace(id="v1", attr="friend", sender="张三", type="voice", content='语音2"秒你好')
        chat = SimpleNamespace(who="张三", GetAllMessage=lambda: [resolved])

        self.assertTrue(bot._queue_pending_private_voice_transcription(chat, original))
        bot._flush_pending_private_voice_transcription(chat)

        self.assertEqual(timer_calls[0][0], 5)
        self.assertEqual(enqueued, [("张三", '语音2"秒你好')])

    def test_private_text_reply_sends_current_turn_context_to_api(self):
        bot = WXBot.__new__(WXBot)
        bot.config = SimpleNamespace(
            chat_keyword_switch=False,
            keyword_dict={},
            memory_switch=False,
            memory_context_switch=False,
            chat_image_recognition_switch=False,
            chat_split_reply_switch=False,
            chat_split_max_count=4,
            chat_split_max_chars=100,
            clean_ai_reply_switch=False,
            meta_reply_blocked_reply="",
            meta_reply_blocked_reply_once=False,
            api_error_reply_once=False,
            text_reply_limit_switch=False,
            text_reply_limit_hours=24,
            chat_voice_reply_switch=False,
            cmd="文件传输助手",
            split_long_text=lambda text: [text],
        )
        bot.reply_count_store = ReplyCountStore("")
        bot.memory_manager = None
        bot._voice_reply_state = {}
        bot._private_reply_can_continue = lambda *_args, **_kwargs: True
        bot._current_ai_material_outreach_config = lambda: {"ai_material_outreach_switch": False}
        bot._build_prompt_with_context = lambda *_args, **_kwargs: "prompt"
        captured_messages = []
        bot._get_chat_api = lambda _user: SimpleNamespace(
            chat=lambda message, **_kwargs: captured_messages.append(message) or "正常回复"
        )
        bot._verified_send_chat = lambda _target, chat: chat
        bot._ensure_target_listen_chat_for_send = lambda _target: None
        bot._get_chat_send_lock = lambda _name: threading.Lock()
        bot._human_delay_for_reply_part = lambda *_args, **_kwargs: None
        bot._begin_private_reply_runtime_turn = lambda _name: []
        bot._finish_private_reply_runtime_turn = lambda *_args, **_kwargs: None
        bot._save_private_reply_memory_message = lambda *_args, **_kwargs: True
        bot._record_replied_message_success = lambda: None

        sent = []
        chat = SimpleNamespace(who="张三", SendMsg=lambda text: sent.append(text) or True)
        message = SimpleNamespace(type="text", attr="friend", sender="张三", content="测试", id="1")

        with mock.patch("wxbot_core.build_current_turn_user_message", return_value="WRAPPED_CURRENT_TURN") as wrapped:
            self.assertTrue(bot.wx_send_ai(chat, message))

        wrapped.assert_called_once_with("测试")
        self.assertEqual(captured_messages, ["WRAPPED_CURRENT_TURN"])
        self.assertEqual(sent, ["正常回复"])

    def test_meta_reply_blocked_reply_once_resets_with_reply_window(self):
        with tempfile.TemporaryDirectory() as tmp:
            bot = WXBot.__new__(WXBot)
            bot.config = SimpleNamespace(
                chat_keyword_switch=False,
                keyword_dict={},
                memory_switch=False,
                memory_context_switch=False,
                chat_image_recognition_switch=False,
                chat_split_reply_switch=False,
                chat_split_max_count=4,
                chat_split_max_chars=100,
                clean_ai_reply_switch=True,
                meta_reply_blocked_reply="换个说法吧",
                meta_reply_blocked_reply_once=True,
                api_error_reply_once=False,
                text_reply_limit_switch=False,
                text_reply_limit_hours=24,
                chat_voice_reply_switch=False,
                cmd="文件传输助手",
                split_long_text=lambda text: [text],
            )
            bot.reply_count_store = ReplyCountStore(f"{tmp}/reply_count.json")
            bot.memory_manager = None
            bot._voice_reply_state = {}
            bot._private_reply_can_continue = lambda *_args, **_kwargs: True
            bot._current_ai_material_outreach_config = lambda: {"ai_material_outreach_switch": False}
            bot._build_prompt_with_context = lambda *_args, **_kwargs: "prompt"
            bot._get_chat_api = lambda _user: SimpleNamespace(chat=lambda *_args, **_kwargs: "作为AI，我不能这样回复")
            bot._verified_send_chat = lambda _target, chat: chat
            bot._ensure_target_listen_chat_for_send = lambda _target: None
            bot._get_chat_send_lock = lambda _name: threading.Lock()
            bot._human_delay_for_reply_part = lambda *_args, **_kwargs: None
            bot._begin_private_reply_runtime_turn = lambda _name: []
            bot._finish_private_reply_runtime_turn = lambda *_args, **_kwargs: None
            bot._save_private_reply_memory_message = lambda *_args, **_kwargs: True
            bot._record_replied_message_success = lambda: None
            bot._private_reply_send_allows_memory_save = lambda _result: False

            sent = []
            chat = SimpleNamespace(who="张三", SendMsg=lambda text: sent.append(text) or True)
            message = SimpleNamespace(type="text", attr="friend", sender="张三", content="测试", id="1")

            self.assertTrue(bot.wx_send_ai(chat, message))
            self.assertTrue(bot.wx_send_ai(chat, message))
            self.assertEqual(sent, ["换个说法吧"])

            started_at = datetime.now() - timedelta(hours=25)
            bot.reply_count_store.data["users"]["张三"]["window_started_at"] = started_at.isoformat(timespec="seconds")

            self.assertTrue(bot.wx_send_ai(chat, message))
            self.assertEqual(sent, ["换个说法吧", "换个说法吧"])

    def test_private_api_error_reply_is_sent_after_new_message_arrives(self):
        bot = WXBot.__new__(WXBot)
        bot.config = SimpleNamespace(
            chat_keyword_switch=False,
            keyword_dict={},
            memory_switch=False,
            memory_context_switch=False,
            chat_image_recognition_switch=False,
            chat_split_reply_switch=False,
            chat_split_max_count=4,
            chat_split_max_chars=100,
            clean_ai_reply_switch=False,
            meta_reply_blocked_reply="",
            meta_reply_blocked_reply_once=False,
            api_error_reply="接口忙，稍后再聊",
            api_error_reply_once=False,
            text_reply_limit_switch=False,
            text_reply_limit_hours=24,
            chat_voice_reply_switch=False,
            cmd="文件传输助手",
            split_long_text=lambda text: [text],
        )
        bot.memory_manager = None
        bot._voice_reply_state = {"private_sessions": {}, "limits": {}}
        bot._current_ai_material_outreach_config = lambda: {"ai_material_outreach_switch": False}
        bot._build_prompt_with_context = lambda *_args, **_kwargs: "prompt"
        bot._get_chat_api = lambda _user: SimpleNamespace(
            chat=lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("boom"))
        )
        continue_calls = []

        def can_continue(_chat, **kwargs):
            continue_calls.append(kwargs)
            return len(continue_calls) == 1

        bot._private_reply_can_continue = can_continue
        sent = []
        bot._send_private_ai_reply_parts = (
            lambda _chat, parts, **kwargs: sent.append((parts, kwargs.get("expected_sequence"))) or (True, True)
        )
        bot._record_reply_metric_success = lambda *_args, **_kwargs: None
        bot._record_replied_message_success = lambda: None
        bot._get_private_message_sequence = lambda _name: 1

        chat = SimpleNamespace(who="张三")
        message = SimpleNamespace(type="text", attr="friend", sender="张三", content="测试", id="1")

        self.assertTrue(bot.wx_send_ai(chat, message))
        self.assertEqual(sent, [(["接口忙，稍后再聊"], None)])

    def test_group_meta_reply_blocked_reply_once_uses_sender_window(self):
        with tempfile.TemporaryDirectory() as tmp:
            bot = WXBot.__new__(WXBot)
            bot.config = SimpleNamespace(
                AtMe="",
                cmd="文件传输助手",
                AllListen_switch=False,
                listen_list=[],
                global_blacklist=[],
                group=["测试群"],
                group_switch=True,
                group_keyword_switch=False,
                group_keyword_at_only=False,
                keyword_dict={},
                group_reply_at=False,
                group_listen_only=False,
                group_image_recognition_switch=False,
                group_split_reply_switch=False,
                group_split_max_count=4,
                group_split_max_chars=100,
                group_reply_at_msg=False,
                group_reply_quote=False,
                memory_switch=False,
                memory_context_switch=False,
                clean_ai_reply_switch=True,
                meta_reply_blocked_reply="换个说法吧",
                meta_reply_blocked_reply_once=True,
                text_reply_limit_hours=24,
                split_long_text=lambda text: [text],
            )
            bot.reply_count_store = ReplyCountStore(f"{tmp}/reply_count.json")
            bot.memory_manager = None
            bot._pause_group_reply = False
            bot.is_stop_requested = lambda: False
            bot._get_group_api = lambda _group: SimpleNamespace(chat=lambda *_args, **_kwargs: "作为AI，我不能这样回复")
            bot._build_prompt_with_context = lambda *_args, **_kwargs: "prompt"
            bot._get_chat_send_lock = lambda _name: threading.Lock()
            bot._human_delay_for_reply_part = lambda *_args, **_kwargs: None
            bot._record_replied_message_success = lambda: None

            sent = []
            chat = SimpleNamespace(
                who="测试群",
                chat_type="group",
                SendMsg=lambda msg, at=None: sent.append((msg, at)) or True,
            )
            message = SimpleNamespace(
                type="text",
                attr="group",
                sender="张三",
                content="测试",
                quote=lambda text, at=None: sent.append((text, at)) or True,
            )

            self.assertTrue(bot.process_message(chat, message))
            self.assertTrue(bot.process_message(chat, message))
            self.assertEqual(sent, [("换个说法吧", None)])

            key = "group:测试群:张三"
            started_at = datetime.now() - timedelta(hours=25)
            bot.reply_count_store.data["users"][key]["window_started_at"] = started_at.isoformat(timespec="seconds")

            self.assertTrue(bot.process_message(chat, message))
            self.assertEqual(sent, [("换个说法吧", None), ("换个说法吧", None)])

    def test_group_meta_reply_blocked_reply_once_does_not_mark_failed_send(self):
        with tempfile.TemporaryDirectory() as tmp:
            bot = WXBot.__new__(WXBot)
            bot.config = SimpleNamespace(
                AtMe="",
                cmd="文件传输助手",
                AllListen_switch=False,
                listen_list=[],
                global_blacklist=[],
                group=["测试群"],
                group_switch=True,
                group_keyword_switch=False,
                group_keyword_at_only=False,
                keyword_dict={},
                group_reply_at=False,
                group_listen_only=False,
                group_image_recognition_switch=False,
                group_split_reply_switch=False,
                group_split_max_count=4,
                group_split_max_chars=100,
                group_reply_at_msg=False,
                group_reply_quote=False,
                memory_switch=False,
                memory_context_switch=False,
                clean_ai_reply_switch=True,
                meta_reply_blocked_reply="换个说法吧",
                meta_reply_blocked_reply_once=True,
                text_reply_limit_hours=24,
                split_long_text=lambda text: [text],
            )
            bot.reply_count_store = ReplyCountStore(f"{tmp}/reply_count.json")
            bot.memory_manager = None
            bot._pause_group_reply = False
            bot.is_stop_requested = lambda: False
            bot._get_group_api = lambda _group: SimpleNamespace(chat=lambda *_args, **_kwargs: "作为AI，我不能这样回复")
            bot._build_prompt_with_context = lambda *_args, **_kwargs: "prompt"
            bot._get_chat_send_lock = lambda _name: threading.Lock()
            bot._human_delay_for_reply_part = lambda *_args, **_kwargs: None
            bot._record_replied_message_success = lambda: None

            attempts = []
            chat = SimpleNamespace(
                who="测试群",
                chat_type="group",
                SendMsg=lambda msg, at=None: attempts.append((msg, at)) or False,
            )
            message = SimpleNamespace(
                type="text",
                attr="group",
                sender="张三",
                content="测试",
                quote=lambda text, at=None: attempts.append((text, at)) or False,
            )

            self.assertFalse(bot.process_message(chat, message))
            self.assertFalse(bot.process_message(chat, message))
            self.assertEqual(attempts, [("换个说法吧", None), ("换个说法吧", None)])
            user_data = bot.reply_count_store.get_user("group:测试群:张三")
            self.assertFalse(user_data.get("meta_reply_blocked_notified"))

    def test_group_text_reply_sends_current_turn_context_to_api(self):
        bot = WXBot.__new__(WXBot)
        bot.config = SimpleNamespace(
            AtMe="",
            cmd="文件传输助手",
            AllListen_switch=False,
            listen_list=[],
            global_blacklist=[],
            group=["测试群"],
            group_switch=True,
            group_keyword_switch=False,
            group_keyword_at_only=False,
            keyword_dict={},
            group_reply_at=False,
            group_listen_only=False,
            group_image_recognition_switch=False,
            group_split_reply_switch=False,
            group_split_max_count=4,
            group_split_max_chars=100,
            group_reply_at_msg=False,
            group_reply_quote=False,
            memory_switch=False,
            memory_context_switch=False,
            clean_ai_reply_switch=False,
            meta_reply_blocked_reply="",
            meta_reply_blocked_reply_once=False,
            text_reply_limit_hours=24,
            group_voice_reply_switch=False,
            split_long_text=lambda text: [text],
        )
        bot.reply_count_store = ReplyCountStore("")
        bot.memory_manager = None
        bot._pause_group_reply = False
        bot.is_stop_requested = lambda: False
        captured_messages = []
        bot._get_group_api = lambda _group: SimpleNamespace(
            chat=lambda message, **_kwargs: captured_messages.append(message) or "群回复"
        )
        bot._build_prompt_with_context = lambda *_args, **_kwargs: "prompt"
        bot._get_chat_send_lock = lambda _name: threading.Lock()
        bot._human_delay_for_reply_part = lambda *_args, **_kwargs: None
        bot._record_replied_message_success = lambda: None

        sent = []
        chat = SimpleNamespace(
            who="测试群",
            chat_type="group",
            SendMsg=lambda msg, at=None: sent.append((msg, at)) or True,
        )
        message = SimpleNamespace(
            type="text",
            attr="group",
            sender="张三",
            content="测试",
            quote=lambda text, at=None: sent.append((text, at)) or True,
        )

        with mock.patch("wxbot_core.build_current_turn_user_message", return_value="WRAPPED_GROUP_TURN") as wrapped:
            self.assertTrue(bot.process_message(chat, message))

        wrapped.assert_called_once_with("张三: 测试")
        self.assertEqual(captured_messages, ["WRAPPED_GROUP_TURN"])
        self.assertEqual(sent, [("群回复", None)])

    def test_group_api_error_reply_once_resets_with_reply_window(self):
        with tempfile.TemporaryDirectory() as tmp:
            bot = WXBot.__new__(WXBot)
            bot.config = SimpleNamespace(
                AtMe="",
                cmd="文件传输助手",
                AllListen_switch=False,
                listen_list=[],
                global_blacklist=[],
                group=["测试群"],
                group_switch=True,
                group_keyword_switch=False,
                group_keyword_at_only=False,
                keyword_dict={},
                group_reply_at=False,
                group_listen_only=False,
                group_image_recognition_switch=False,
                group_split_reply_switch=False,
                group_split_max_count=4,
                group_split_max_chars=100,
                group_reply_at_msg=False,
                group_reply_quote=False,
                memory_switch=False,
                memory_context_switch=False,
                clean_ai_reply_switch=True,
                meta_reply_blocked_reply="",
                meta_reply_blocked_reply_once=False,
                api_error_reply="接口忙",
                api_error_reply_once=True,
                text_reply_limit_hours=24,
                split_long_text=lambda text: [text],
            )
            bot.reply_count_store = ReplyCountStore(f"{tmp}/reply_count.json")
            bot.memory_manager = None
            bot._pause_group_reply = False
            bot.is_stop_requested = lambda: False
            bot._get_group_api = lambda _group: SimpleNamespace(chat=lambda *_args, **_kwargs: API_ERROR_REPLY_TEXT)
            bot._build_prompt_with_context = lambda *_args, **_kwargs: "prompt"
            bot._get_chat_send_lock = lambda _name: threading.Lock()
            bot._human_delay_for_reply_part = lambda *_args, **_kwargs: None
            bot._record_replied_message_success = lambda: None

            sent = []
            chat = SimpleNamespace(
                who="测试群",
                chat_type="group",
                SendMsg=lambda msg, at=None: sent.append((msg, at)) or True,
            )
            message = SimpleNamespace(
                type="text",
                attr="group",
                sender="张三",
                content="测试",
                quote=lambda text, at=None: sent.append((text, at)) or True,
            )

            self.assertTrue(bot.process_message(chat, message))
            self.assertTrue(bot.process_message(chat, message))
            self.assertEqual(sent, [("接口忙", None)])

            key = "group:测试群:张三"
            started_at = datetime.now() - timedelta(hours=25)
            bot.reply_count_store.data["users"][key]["window_started_at"] = started_at.isoformat(timespec="seconds")

            self.assertTrue(bot.process_message(chat, message))
            self.assertEqual(sent, [("接口忙", None), ("接口忙", None)])

    def test_group_voice_transcription_fallback_once_uses_sender_window(self):
        with tempfile.TemporaryDirectory() as tmp:
            bot = WXBot.__new__(WXBot)
            bot.config = SimpleNamespace(
                AtMe="",
                cmd="文件传输助手",
                AllListen_switch=False,
                listen_list=[],
                global_blacklist=[],
                group=["测试群"],
                group_switch=True,
                group_keyword_switch=False,
                group_keyword_at_only=False,
                keyword_dict={},
                group_reply_at=False,
                group_listen_only=False,
                group_image_recognition_switch=False,
                group_voice_recognition_switch=True,
                group_reply_at_msg=True,
                group_reply_quote=True,
                voice_transcription_fallback_text="刚才那条语音，我有点没听清",
                voice_transcription_fallback_reply_once=True,
                text_reply_limit_hours=24,
            )
            bot.reply_count_store = ReplyCountStore(f"{tmp}/reply_count.json")
            bot._pause_group_reply = False
            bot.is_stop_requested = lambda: False

            sent = []
            chat = SimpleNamespace(
                who="测试群",
                chat_type="group",
                SendMsg=lambda msg, at=None: sent.append((msg, at)) or True,
            )
            message = SimpleNamespace(
                type="voice",
                attr="group",
                sender="张三",
                content="",
                _voice_transcription_failed=True,
                quote=lambda text, at=None: sent.append((text, at)) or True,
            )

            self.assertTrue(bot.process_message(chat, message))
            self.assertTrue(bot.process_message(chat, message))
            self.assertEqual(sent, [("刚才那条语音，我有点没听清", "张三")])

            key = "group:测试群:张三"
            started_at = datetime.now() - timedelta(hours=25)
            bot.reply_count_store.data["users"][key]["window_started_at"] = started_at.isoformat(timespec="seconds")

            self.assertTrue(bot.process_message(chat, message))
            self.assertEqual(sent, [("刚才那条语音，我有点没听清", "张三"), ("刚才那条语音，我有点没听清", "张三")])

    def test_group_voice_transcription_fallback_respects_at_only_groups(self):
        with tempfile.TemporaryDirectory() as tmp:
            bot = WXBot.__new__(WXBot)
            bot.config = SimpleNamespace(
                AtMe="@机器人",
                cmd="文件传输助手",
                AllListen_switch=False,
                listen_list=[],
                global_blacklist=[],
                group=["测试群"],
                group_switch=True,
                group_keyword_switch=False,
                group_keyword_at_only=False,
                keyword_dict={},
                group_reply_at=True,
                group_listen_only=False,
                group_image_recognition_switch=False,
                group_voice_recognition_switch=True,
                group_reply_at_msg=True,
                group_reply_quote=True,
                voice_transcription_fallback_text="刚才那条语音，我有点没听清",
                voice_transcription_fallback_reply_once=True,
                text_reply_limit_hours=24,
            )
            bot.reply_count_store = ReplyCountStore(f"{tmp}/reply_count.json")
            bot._pause_group_reply = False
            bot.is_stop_requested = lambda: False

            sent = []
            chat = SimpleNamespace(
                who="测试群",
                chat_type="group",
                SendMsg=lambda msg, at=None: sent.append((msg, at)) or True,
            )
            message = SimpleNamespace(
                type="voice",
                attr="group",
                sender="张三",
                content="",
                _voice_transcription_failed=True,
                quote=lambda text, at=None: sent.append((text, at)) or True,
            )

            self.assertTrue(bot.process_message(chat, message))
            self.assertEqual(sent, [])

    def test_pending_visual_context_reference_intent_is_bilingual(self):
        positives = [
            "看看这是啥意思",
            "帮我看看这票是什么",
            "帮我看看上面那张写的是啥",
            "这张图片里有什么",
            "这发票写的什么",
            "解释一下",
            "刚才说到哪了",
            "what does this mean",
            "explain this",
            "read the screenshot above",
            "analyze this image",
        ]
        negatives = [
            "this is a normal message",
            "what is the plan today",
            "give me more context",
        ]

        for text in positives:
            with self.subTest(text=text):
                self.assertTrue(WXBot._text_references_pending_visual_context(text))
        for text in negatives:
            with self.subTest(text=text):
                self.assertFalse(WXBot._text_references_pending_visual_context(text))

    def test_group_pending_image_context_can_be_used_by_later_sender(self):
        bot = WXBot.__new__(WXBot)
        bot.config = SimpleNamespace(
            AtMe="@机器人",
            cmd="文件传输助手",
            AllListen_switch=False,
            listen_list=[],
            global_blacklist=[],
            group=["测试群"],
            group_switch=True,
            group_keyword_switch=False,
            group_keyword_at_only=False,
            keyword_dict={},
            group_reply_at=True,
            group_listen_only=False,
            group_image_recognition_switch=True,
            group_image_recognition_api=0,
            group_split_reply_switch=False,
            group_split_max_count=4,
            group_split_max_chars=100,
            group_reply_at_msg=False,
            group_reply_quote=False,
            memory_switch=False,
            memory_context_switch=False,
            clean_ai_reply_switch=False,
            meta_reply_blocked_reply="",
            meta_reply_blocked_reply_once=False,
            group_voice_reply_switch=False,
            split_long_text=lambda text: [text],
        )
        bot.memory_manager = None
        bot.reply_count_store = ReplyCountStore("")
        bot._pause_group_reply = False
        bot.is_stop_requested = lambda: False
        bot._build_prompt_with_context = lambda *_args, **_kwargs: "prompt"
        bot._get_chat_send_lock = lambda _name: threading.Lock()
        bot._human_delay_for_reply_part = lambda *_args, **_kwargs: None
        bot._record_replied_message_success = lambda: None

        image_reply_calls = []
        def fake_group_image_reply(_chat, _message, _history, image_paths=None, attached_text=""):
            paths = list(image_paths or [])
            image_reply_calls.append((paths, attached_text))
            bot._set_pending_visual_context(
                "测试群",
                paths,
                visual_notes=["图片概览：一张测试图。\n可见文字：无。\n关键细节：用于测试。\n不确定项：无。"],
            )
            return "图片答案"

        bot._reply_group_image_message = fake_group_image_reply
        bot._get_group_api = lambda _group: self.fail("问图时应走图片回复管线，而不是普通群聊 AI")

        chat = SimpleNamespace(
            who="测试群",
            chat_type="group",
            SendMsg=lambda msg, at=None: sent.append((msg, at)) or True,
        )
        sent = []
        bot._set_pending_visual_context("测试群", [r"C:\tmp\a-image.png"])
        message = SimpleNamespace(
            type="text",
            attr="group",
            sender="B",
            content="@机器人 看看这是啥意思",
            quote=lambda text, at=None: sent.append((text, at)) or True,
        )

        self.assertTrue(bot.process_message(chat, message))

        self.assertEqual(image_reply_calls, [([r"C:\tmp\a-image.png"], "看看这是啥意思")])
        self.assertEqual(sent, [("图片答案", None)])
        self.assertIsNone(bot._get_pending_visual_context("测试群"))

    def test_group_pending_image_context_ignores_unrelated_at_message(self):
        bot = WXBot.__new__(WXBot)
        bot.config = SimpleNamespace(
            AtMe="@机器人",
            cmd="文件传输助手",
            AllListen_switch=False,
            listen_list=[],
            global_blacklist=[],
            group=["测试群"],
            group_switch=True,
            group_keyword_switch=False,
            group_keyword_at_only=False,
            keyword_dict={},
            group_reply_at=True,
            group_listen_only=False,
            group_image_recognition_switch=True,
            group_split_reply_switch=False,
            group_split_max_count=4,
            group_split_max_chars=100,
            group_reply_at_msg=False,
            group_reply_quote=False,
            memory_switch=False,
            memory_context_switch=False,
            clean_ai_reply_switch=False,
            meta_reply_blocked_reply="",
            meta_reply_blocked_reply_once=False,
            group_voice_reply_switch=False,
            split_long_text=lambda text: [text],
        )
        bot.memory_manager = None
        bot.reply_count_store = ReplyCountStore("")
        bot._pause_group_reply = False
        bot.is_stop_requested = lambda: False
        bot._build_prompt_with_context = lambda *_args, **_kwargs: "prompt"
        bot._get_chat_send_lock = lambda _name: threading.Lock()
        bot._human_delay_for_reply_part = lambda *_args, **_kwargs: None
        bot._record_replied_message_success = lambda: None
        bot._reply_group_image_message = lambda *_args, **_kwargs: self.fail("普通 @ 消息不应消费 pending 图片")
        bot._get_group_api = lambda _group: SimpleNamespace(chat=lambda *_args, **_kwargs: "普通回答")

        sent = []
        chat = SimpleNamespace(
            who="测试群",
            chat_type="group",
            SendMsg=lambda msg, at=None: sent.append((msg, at)) or True,
        )
        bot._set_pending_visual_context("测试群", [r"C:\tmp\a-image.png"])
        message = SimpleNamespace(
            type="text",
            attr="group",
            sender="B",
            content="@机器人 今天天气怎么样",
            quote=lambda text, at=None: sent.append((text, at)) or True,
        )

        self.assertTrue(bot.process_message(chat, message))

        self.assertEqual(sent, [("普通回答", None)])
        self.assertEqual(
            bot._get_pending_visual_context("测试群")["image_paths"],
            [r"C:\tmp\a-image.png"],
        )

    def test_group_image_message_only_sets_pending_context_when_not_at_only(self):
        bot = WXBot.__new__(WXBot)
        bot.config = SimpleNamespace(
            AtMe="@机器人",
            cmd="文件传输助手",
            AllListen_switch=False,
            listen_list=[],
            global_blacklist=[],
            group=["测试群"],
            group_switch=True,
            group_keyword_switch=False,
            group_keyword_at_only=False,
            keyword_dict={},
            group_reply_at=False,
            group_listen_only=False,
            group_image_recognition_switch=True,
        )
        bot._pause_group_reply = False
        bot._reply_group_image_message = lambda *_args, **_kwargs: self.fail("群图片本身不应立即识图")
        bot._get_group_api = lambda _group: self.fail("群图片本身不应触发普通群聊 AI")

        chat = SimpleNamespace(who="测试群", chat_type="group")
        message = SimpleNamespace(
            type="image",
            attr="group",
            sender="A",
            content=r"C:\tmp\group-image.png",
        )

        self.assertTrue(bot.process_message(chat, message))
        self.assertEqual(
            bot._get_pending_visual_context("测试群")["image_paths"],
            [r"C:\tmp\group-image.png"],
        )
        self.assertEqual(bot._get_pending_visual_context("测试群")["visual_notes"], [""])

    def test_group_consecutive_images_accumulate_pending_context(self):
        bot = WXBot.__new__(WXBot)
        bot.config = SimpleNamespace(
            AtMe="@机器人",
            cmd="文件传输助手",
            AllListen_switch=False,
            listen_list=[],
            global_blacklist=[],
            group=["测试群"],
            group_switch=True,
            group_keyword_switch=False,
            group_keyword_at_only=False,
            keyword_dict={},
            group_reply_at=True,
            group_listen_only=False,
            group_image_recognition_switch=True,
        )
        bot._pause_group_reply = False
        bot._reply_group_image_message = lambda *_args, **_kwargs: self.fail("群图片本身不应立即识图")
        bot._get_group_api = lambda _group: self.fail("群图片本身不应触发普通群聊 AI")

        chat = SimpleNamespace(who="测试群", chat_type="group")

        self.assertTrue(bot.process_message(chat, SimpleNamespace(
            type="image",
            attr="group",
            sender="A",
            content=r"C:\tmp\group-image-1.png",
        )))
        self.assertTrue(bot.process_message(chat, SimpleNamespace(
            type="image",
            attr="group",
            sender="A",
            content=r"C:\tmp\group-image-2.png",
        )))

        self.assertEqual(
            bot._get_pending_visual_context("测试群")["image_paths"],
            [r"C:\tmp\group-image-1.png", r"C:\tmp\group-image-2.png"],
        )

    def test_private_message_pipeline_merges_batch_without_version_cancelling_reply(self):
        bot = WXBot.__new__(WXBot)
        bot.config = SimpleNamespace(chat_message_merge_delay=3)
        bot.is_stop_requested = lambda: False
        sent_to_ai = []
        bot.wx_send_ai = lambda _chat, message: sent_to_ai.append(message.content) or True
        chat = SimpleNamespace(who="张三")
        first_batch = [
            SimpleNamespace(type="text", attr="friend", sender="张三", content="在吗", id="1"),
            SimpleNamespace(type="text", attr="friend", sender="张三", content="我想你", id="2"),
        ]
        second_batch = [
            SimpleNamespace(type="text", attr="friend", sender="张三", content="刚才忘了说", id="3")
        ]

        bot._ensure_message_runtime_state()
        with bot._chat_merge_lock:
            pipeline = bot._private_message_pipeline("张三")
            pipeline["queued_batches"].append(first_batch)
            pipeline["queued_batches"].append(second_batch)
            pipeline["worker_running"] = True

        self.assertTrue(bot._run_private_message_pipeline_worker(chat))

        self.assertEqual(sent_to_ai, ["在吗\n我想你", "刚才忘了说"])
        self.assertFalse(bot._private_message_pipelines["张三"]["worker_running"])

    def test_private_message_pipeline_merges_image_batch_with_followup_text(self):
        bot = WXBot.__new__(WXBot)
        bot.config = SimpleNamespace(
            chat_message_merge_delay=3,
            chat_image_recognition_switch=True,
        )
        bot.is_stop_requested = lambda: False
        bot._schedule_private_message_timer = lambda *_args, **_kwargs: SimpleNamespace(cancel=lambda: None)
        bot._start_private_message_worker_locked = lambda _chat, _pipeline: True

        chat = SimpleNamespace(who="张三")
        image_msg = SimpleNamespace(type="image", attr="friend", sender="张三", content=r"C:\tmp\a.png", id="1")
        text_msg = SimpleNamespace(type="text", attr="friend", sender="张三", content="猜猜哪个是我？", id="2")

        bot._ensure_message_runtime_state()
        self.assertTrue(bot._enqueue_private_message_for_ai(chat, image_msg))
        self.assertTrue(bot._enqueue_private_message_for_ai(chat, text_msg))

        pipeline = bot._private_message_pipelines["张三"]
        self.assertEqual([msg.content for msg in pipeline["open_messages"]], [r"C:\tmp\a.png", "猜猜哪个是我？"])
        self.assertEqual(pipeline["open_kind"], "mixed")
        merged = bot._build_merged_private_message(pipeline["open_messages"])
        self.assertEqual(merged.type, "text")
        self.assertIn("猜猜哪个是我？", merged.content)
        self.assertIn("+引用的图片:", merged.content)
        self.assertIn(r"C:\tmp\a.png", merged.content)

    def test_private_image_batch_uses_double_idle_and_base_triple_max_wait(self):
        bot = WXBot.__new__(WXBot)
        bot.config = SimpleNamespace(
            chat_message_merge_delay=3,
            chat_image_recognition_switch=True,
        )
        bot.is_stop_requested = lambda: False
        scheduled = []

        def capture_timer(seconds, callback, _chat):
            scheduled.append((callback.__name__, seconds))
            return SimpleNamespace(cancel=lambda: None)

        bot._schedule_private_message_timer = capture_timer
        bot._existing_local_image_path = lambda path: path
        bot._start_private_message_worker_locked = lambda _chat, _pipeline: True

        chat = SimpleNamespace(who="张三")
        image_msg = SimpleNamespace(type="image", attr="friend", sender="张三", content=r"C:\tmp\a.png", id="1")
        text_msg = SimpleNamespace(type="text", attr="friend", sender="张三", content="这里写的什么？", id="2")

        bot._ensure_message_runtime_state()
        self.assertTrue(bot._enqueue_private_message_for_ai(chat, image_msg))

        self.assertEqual(scheduled[0][0], "_close_private_message_batch_by_idle")
        self.assertEqual(scheduled[0][1], 6.0)
        self.assertEqual(scheduled[1][0], "_close_private_message_batch_by_max_wait")
        self.assertAlmostEqual(scheduled[1][1], 9.0, delta=0.1)

        self.assertTrue(bot._enqueue_private_message_for_ai(chat, text_msg))

        self.assertEqual(scheduled[-1][0], "_close_private_message_batch_by_idle")
        self.assertEqual(scheduled[-1][1], 6.0)
        self.assertEqual(
            [name for name, _seconds in scheduled].count("_close_private_message_batch_by_max_wait"),
            1,
        )

    def test_private_image_sets_pending_context_before_ai_worker_runs(self):
        bot = WXBot.__new__(WXBot)
        bot.config = SimpleNamespace(
            chat_message_merge_delay=3,
            chat_image_recognition_switch=True,
        )
        bot.is_stop_requested = lambda: False
        bot._schedule_private_message_timer = lambda *_args, **_kwargs: SimpleNamespace(cancel=lambda: None)
        bot._existing_local_image_path = lambda path: path
        bot._start_private_message_worker_locked = lambda _chat, _pipeline: True

        chat = SimpleNamespace(who="张三")
        image_msg = SimpleNamespace(type="image", attr="friend", sender="张三", content=r"C:\tmp\logo.png", id="1")

        bot._ensure_message_runtime_state()
        self.assertTrue(bot._enqueue_private_message_for_ai(chat, image_msg))

        pending = bot._get_pending_visual_context("张三")
        self.assertEqual(pending["image_paths"], [r"C:\tmp\logo.png"])

    def test_private_image_only_batch_reaches_ai_after_idle_close(self):
        bot = WXBot.__new__(WXBot)
        bot.config = SimpleNamespace(chat_message_merge_delay=3)
        bot.is_stop_requested = lambda: False
        sent_to_ai = []
        bot.wx_send_ai = lambda _chat, message: sent_to_ai.append((message.type, message.content)) or True

        chat = SimpleNamespace(who="张三")
        image_msg = SimpleNamespace(type="image", attr="friend", sender="张三", content=r"C:\tmp\only.png", id="1")

        bot._ensure_message_runtime_state()
        with bot._chat_merge_lock:
            pipeline = bot._private_message_pipeline("张三")
            pipeline["open_messages"] = [image_msg]
            pipeline["open_started_at"] = 1.0
            pipeline["open_kind"] = "image"

        self.assertTrue(bot._close_private_message_batch_by_idle(chat))
        self.assertTrue(bot._run_private_message_pipeline_worker(chat))

        self.assertEqual(sent_to_ai, [("image", r"C:\tmp\only.png")])

    def test_private_reply_stops_after_pause_even_when_reply_already_generated(self):
        class FakeChat:
            who = "张三"
            chat_type = "private"

            def __init__(self):
                self.sent = []

            def SendMsg(self, msg=None, message=None, **_kwargs):
                self.sent.append(msg if msg is not None else message)
                return True

        bot = WXBot.__new__(WXBot)
        chat = FakeChat()
        bot.config = SimpleNamespace(cmd="管理员", chat_listen_only=False)
        bot.is_stop_requested = lambda: False
        bot._pause_chat_reply = False
        bot._pause_chat_reply_users = {"张三"}

        self.assertEqual(bot._send_private_ai_reply_parts(chat, ["已经生成的回复"]), (False, True))
        self.assertEqual(chat.sent, [])

    def test_private_reply_stops_remaining_parts_when_new_message_arrives(self):
        class FakeChat:
            who = "张三"
            chat_type = "private"

            def __init__(self, bot):
                self.bot = bot
                self.sent = []

            def SendMsg(self, msg=None, message=None, **_kwargs):
                text = msg if msg is not None else message
                self.sent.append(text)
                if len(self.sent) == 1:
                    self.bot._next_private_message_sequence(self.who)
                return True

        bot = WXBot.__new__(WXBot)
        bot.config = SimpleNamespace(cmd="管理员", chat_listen_only=False)
        bot.is_stop_requested = lambda: False
        bot._pause_chat_reply = False
        bot._verified_send_chat = lambda _target, chat: chat
        bot._get_chat_send_lock = lambda _name: threading.Lock()
        bot._human_delay_for_reply_part = lambda *_args, **_kwargs: None
        bot._save_private_reply_memory_message = lambda *_args, **_kwargs: True
        bot._ensure_message_runtime_state()
        bot._next_private_message_sequence("张三")
        expected_sequence = bot._get_private_message_sequence("张三")
        chat = FakeChat(bot)

        self.assertEqual(
            bot._send_private_ai_reply_parts(
                chat,
                ["第一段", "第二段", "第三段"],
                expected_sequence=expected_sequence,
            ),
            (True, True),
        )
        self.assertEqual(chat.sent, ["第一段"])

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
