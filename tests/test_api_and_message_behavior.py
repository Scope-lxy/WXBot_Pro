import base64
import os
import queue
import time
import unittest
import threading
import tempfile
from datetime import datetime, timedelta
from collections import deque
from types import SimpleNamespace
from unittest import mock

from core import wechat_ui_actions
from core.api import API_ERROR_REPLY_TEXT, DusAPI, OpenAIAPI, build_api_config_snapshot
from core.message_pipeline import ConversationRef, MessageEnvelope
from core.prompting import build_current_turn_user_message, build_image_user_message
from core.reply_pipeline import ImageReplyPipeline, ImageReplyRequest
from core.reply_count_store import ReplyCountStore
from core.vision_bridge import VisionNote
from feature import listening, message_routing
from feature.voice_reply import group_voice_candidate
from feature.scheduled_messages import execute_scheduled_message_task
from wxbot_core import LONG_REPLY_SEGMENT_CHARS, WXAUTO_SAVE_DIR_NAME, WXBot, WxParam


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
    def test_listener_thread_exception_arms_lightweight_recovery(self):
        bot = WXBot.__new__(WXBot)
        bot.is_stop_requested = lambda: False
        calls = []
        bot._arm_listener_auto_recovery = lambda exc, source="": calls.append((exc, source)) or True
        bot.callback_is_die = False
        error = RuntimeError("事件无法调用任何订户")

        handled = bot._handle_background_thread_exception(
            SimpleNamespace(thread=SimpleNamespace(name="Thread-1 (_listener_listen)"), exc_value=error)
        )

        self.assertTrue(handled)
        self.assertEqual(calls, [(error, "wxautox监听线程")])
        self.assertFalse(bot.callback_is_die)

    def test_listener_find_control_timeout_arms_recovery_instead_of_stopping_bot(self):
        bot = WXBot.__new__(WXBot)
        bot.is_stop_requested = lambda: False
        bot.callback_is_die = False
        bot._arm_listener_auto_recovery = lambda exc, source="": listening.arm_listener_auto_recovery(
            bot, exc, source=source
        )

        handled = bot._handle_background_thread_exception(SimpleNamespace(
            thread=SimpleNamespace(name="Thread-1 (_listener_listen)"),
            exc_value=LookupError("Find Control Timeout: ListItemControl"),
        ))

        self.assertTrue(handled)
        self.assertTrue(bot._listener_auto_recovery_active)
        self.assertFalse(bot.callback_is_die)

    def test_reply_count_store_loads_utf8_bom_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = f"{tmp}/reply_count.json"
            with open(path, "w", encoding="utf-8-sig") as f:
                f.write('{"users":{"张三":{"count":2,"window_started_at":"2026-07-08T05:00:00"}}}')

            store = ReplyCountStore(path)

            self.assertEqual(store.get_user("张三")["count"], 2)

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

    def test_direct_image_reply_uses_prompt_builder_keyword_signature(self):
        captured = {}

        class FakeFinalApi:
            def chat(self, message, **kwargs):
                captured["message"] = message
                captured["kwargs"] = kwargs
                return "视觉回复"

        def build_prompt(chat_name, *, base_prompt=None, chat_type="private", image_parse_block="", prompt_extra=""):
            captured["prompt_args"] = {
                "chat_name": chat_name,
                "base_prompt": base_prompt,
                "chat_type": chat_type,
                "image_parse_block": image_parse_block,
                "prompt_extra": prompt_extra,
            }
            return "prompt"

        pipeline = ImageReplyPipeline(
            prompt_builder=build_prompt,
            image_parse_block_builder=lambda: "IMAGE_RULES",
            user_message_builder=build_image_user_message,
            vision_bridge=SimpleNamespace(),
        )

        result = pipeline.reply(ImageReplyRequest(
            chat_name="张三",
            chat_type="private",
            attached_text="看看这张图",
            sender="张三",
            history=[{"content": "前文"}],
            final_api=FakeFinalApi(),
            recognition_api=SimpleNamespace(),
            final_api_supports_vision=True,
            image_path=r"C:\tmp\photo.png",
        ))

        self.assertEqual(result, "视觉回复")
        self.assertEqual(captured["prompt_args"]["chat_name"], "张三")
        self.assertEqual(captured["prompt_args"]["chat_type"], "private")
        self.assertEqual(captured["prompt_args"]["image_parse_block"], "IMAGE_RULES")
        self.assertIn("看看这张图", captured["message"])
        self.assertEqual(captured["kwargs"]["prompt"], "prompt")
        self.assertEqual(captured["kwargs"]["history"], [{"content": "前文"}])
        self.assertEqual(captured["kwargs"]["image_path"], r"C:\tmp\photo.png")

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

    def test_private_message_keeps_distinct_ids_with_same_content(self):
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
        self.assertTrue(bot._mark_message_content_fingerprint_seen("张三", duplicate))

    def test_private_message_keeps_repeated_send_with_distinct_ids(self):
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

    def test_private_message_without_stable_identity_preserves_repeated_content(self):
        bot = WXBot.__new__(WXBot)
        first = SimpleNamespace(type="text", attr="friend", sender="张三", content="你好", id="", hash="", time="")
        duplicate = SimpleNamespace(type="text", attr="friend", sender="张三", content="你好", id="", hash="", time="")

        self.assertTrue(bot._mark_message_content_fingerprint_seen("张三", first))
        self.assertTrue(bot._mark_message_content_fingerprint_seen("张三", duplicate))

    def test_private_message_native_hash_text_keeps_repeated_content(self):
        bot = WXBot.__new__(WXBot)
        first = SimpleNamespace(
            type="image", attr="friend", sender="张三", content="图片",
            id="", hash="", hash_text="image-row-1", time="",
        )
        second = SimpleNamespace(
            type="image", attr="friend", sender="张三", content="图片",
            id="", hash="", hash_text="image-row-2", time="",
        )

        self.assertTrue(bot._mark_message_content_fingerprint_seen("张三", first))
        self.assertTrue(bot._mark_message_content_fingerprint_seen("张三", second))

    def test_private_ingress_advances_version_before_business_queue_runs(self):
        bot = WXBot.__new__(WXBot)
        bot.config = SimpleNamespace(group=[], cmd="管理员")
        bot._ui_ingress_queue = queue.Queue()
        bot._stop_requested_event = threading.Event()
        conversation = ConversationRef("张三", "private")
        message = MessageEnvelope(
            id="message-1",
            type="text",
            attr="friend",
            sender="张三",
            content="新消息",
        )

        self.assertTrue(bot._enqueue_ui_message(conversation, message))

        self.assertEqual(bot._get_private_message_sequence("张三"), 1)
        self.assertTrue(message._wxbot_seen_at_ingress)
        self.assertTrue(message._wxbot_sequence_advanced)
        self.assertEqual(bot._ui_ingress_queue.qsize(), 1)

    def test_contact_barrier_blocks_entire_message_business_pipeline(self):
        contact_done = threading.Event()
        processed = []
        bot = WXBot.__new__(WXBot)
        bot._ui_ingress_queue = queue.Queue()
        bot._ui_ingress_stop = threading.Event()
        bot._ui_owner = SimpleNamespace(
            wait_for_contact_idle=lambda: contact_done.wait(1),
        )
        bot.message_handle_callback = lambda message, chat: processed.append((message.content, chat.who))
        worker = threading.Thread(target=bot._run_ui_ingress)
        worker.start()
        bot._ui_ingress_queue.put((
            ConversationRef("张三", "private"),
            MessageEnvelope(type="text", attr="friend", sender="张三", content="维护期间消息"),
        ))
        try:
            time.sleep(0.03)
            self.assertEqual(processed, [])
            contact_done.set()
            bot._ui_ingress_queue.join()
        finally:
            bot._ui_ingress_stop.set()
            worker.join(1)

        self.assertEqual(processed, [("维护期间消息", "张三")])

    def test_duplicate_private_ingress_does_not_advance_version_twice(self):
        bot = WXBot.__new__(WXBot)
        bot.config = SimpleNamespace(group=[], cmd="管理员")
        bot._ui_ingress_queue = queue.Queue()
        bot._stop_requested_event = threading.Event()
        conversation = ConversationRef("张三", "private")

        for _index in range(2):
            bot._enqueue_ui_message(conversation, MessageEnvelope(
                id="same-message",
                type="text",
                attr="friend",
                sender="张三",
                content="新消息",
            ))

        self.assertEqual(bot._get_private_message_sequence("张三"), 1)
        self.assertEqual(bot._ui_ingress_queue.qsize(), 1)

    def test_manual_self_ingress_invalidates_old_reply_but_bot_echo_does_not(self):
        bot = WXBot.__new__(WXBot)
        bot.config = SimpleNamespace(group=[], cmd="管理员")
        bot._ui_ingress_queue = queue.Queue()
        bot._stop_requested_event = threading.Event()
        conversation = ConversationRef("张三", "private")
        bot._remember_private_outbound_echo("张三", "text", "机器人回复", source="test")

        echo = MessageEnvelope(type="text", attr="self", sender="self", content="机器人回复")
        manual = MessageEnvelope(type="text", attr="self", sender="self", content="人工回复")
        bot._enqueue_ui_message(conversation, echo)
        bot._enqueue_ui_message(conversation, manual)

        self.assertEqual(bot._get_private_message_sequence("张三"), 1)
        self.assertTrue(echo._wxbot_private_outbound_echo)
        self.assertFalse(getattr(echo, "_wxbot_private_reply_persisted_echo", False))
        self.assertTrue(manual._wxbot_sequence_advanced)

    def test_duplicate_private_self_callback_id_is_ignored(self):
        bot = WXBot.__new__(WXBot)
        bot.config = SimpleNamespace(group=[], cmd="管理员")
        bot._ui_ingress_queue = queue.Queue()
        bot._stop_requested_event = threading.Event()
        conversation = ConversationRef("张三", "private")
        bot._remember_private_outbound_echo("张三", "text", "机器人回复", source="test")

        first = MessageEnvelope(id="self-1", type="text", attr="self", sender="self", content="机器人回复")
        duplicate = MessageEnvelope(id="self-1", type="text", attr="self", sender="self", content="机器人回复")
        bot._enqueue_ui_message(conversation, first)
        bot._enqueue_ui_message(conversation, duplicate)

        self.assertEqual(bot._ui_ingress_queue.qsize(), 1)

    def test_uncertain_send_runtime_echo_does_not_invalidate_remaining_bubbles(self):
        bot = WXBot.__new__(WXBot)
        bot.config = SimpleNamespace(group=[], cmd="管理员")
        bot._ui_ingress_queue = queue.Queue()
        bot._stop_requested_event = threading.Event()
        bot._ensure_message_runtime_state()
        turn = bot._begin_private_reply_runtime_turn("张三")
        bot._append_private_reply_runtime_part(turn, "结果未知的机器人气泡")
        bot._finish_private_reply_runtime_turn("张三", turn)
        conversation = ConversationRef("张三", "private")
        echo = MessageEnvelope(
            type="text",
            attr="self",
            sender="self",
            content="结果未知的机器人气泡",
        )

        bot._enqueue_ui_message(conversation, echo)

        self.assertEqual(bot._get_private_message_sequence("张三"), 0)
        self.assertTrue(echo._wxbot_runtime_reply_echo)

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
        bot._send_lightweight_actions_to_child = lambda _target, _actions, **_kwargs: False
        logs = []
        with mock.patch("wxbot_core.log", side_effect=lambda **kwargs: logs.append(kwargs.get("message", ""))):
            bot._queue_lightweight_send("李四", [{"type": "text", "text": "你好"}], source="text")
            self.assertFalse(bot._flush_lightweight_send_queue())

        self.assertEqual(logs, ["[轻量发送队列] 李四 待发送任务暂未发出，保留队列"])

    def test_queued_private_ai_reply_logs_confirmed_contents_as_info(self):
        bot = WXBot.__new__(WXBot)
        bot._get_wechat_action_lock = lambda: threading.RLock()
        bot._get_chat_send_lock = lambda _target: threading.RLock()
        bot._wechat_action_lock_is_busy = lambda: False
        bot._ensure_target_listen_chat_for_send = lambda _target: SimpleNamespace(
            SendMsg=lambda _text: True
        )

        logs = []
        with mock.patch(
            "wxbot_core.log",
            side_effect=lambda **kwargs: logs.append((kwargs.get("level", "INFO"), kwargs.get("message", ""))),
        ):
            bot._queue_text_reply_until_target_verified(
                "张三",
                ["你好", "晚安"],
                source="private_ai_reply",
            )
            self.assertTrue(bot._flush_lightweight_send_queue())

        self.assertEqual(logs, [("INFO", "私聊 张三：本轮回复（2条）：你好 ｜ 晚安")])

    def test_reply_content_log_marks_voice_and_uses_info_level(self):
        logs = []

        with mock.patch(
            "wxbot_core.log",
            side_effect=lambda **kwargs: logs.append((kwargs.get("level"), kwargs.get("message"))),
        ):
            WXBot._log_reply_contents(
                "私聊",
                "诗意&清欢",
                ["你好", WXBot._format_reply_log_item("姐姐，我也在想你哦", kind="voice")],
            )

        self.assertEqual(
            logs,
            [("INFO", "私聊 诗意&清欢：本轮回复（2条）：你好 ｜ [语音]姐姐，我也在想你哦")],
        )

    def test_stale_private_ai_reply_is_dropped_from_lightweight_send_queue(self):
        bot = WXBot.__new__(WXBot)
        bot._get_wechat_action_lock = lambda: threading.RLock()
        bot._wechat_action_lock_is_busy = lambda: False
        bot._send_lightweight_actions_to_child = lambda _target, _actions, **_kwargs: self.fail("旧回复不应继续发送")
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

    def test_owner_lightweight_actions_are_individually_versioned(self):
        calls = []

        class Owner:
            def call(self, intent, timeout):
                calls.append((intent, timeout))
                return [True, True, True]

        bot = WXBot.__new__(WXBot)
        bot._ui_owner = Owner()
        bot.config = SimpleNamespace(
            chat_split_reply_switch=True,
            chat_split_reply_delay_switch=True,
        )
        actions = [
            {"type": "text", "text": "第一条"},
            {"type": "file", "path": "a.png"},
            {"type": "voice", "path": "a.wav"},
        ]

        result = bot._send_lightweight_actions_to_child(
            "张三",
            actions,
            source="ai_reply",
            expected_sequence=7,
            delivery_id="delivery-1",
        )

        self.assertTrue(result)
        self.assertEqual(len(calls), 3)
        for index, (intent, timeout) in enumerate(calls):
            self.assertEqual(intent.kind, wechat_ui_actions.UIIntentKind.SEND_ACTIONS)
            self.assertEqual(intent.conversation_version, 7)
            self.assertEqual(intent.payload["delivery_id"], f"delivery-1:{index}")
            self.assertEqual([dict(item) for item in intent.payload["actions"]], [actions[index]])
            self.assertIs(timeout, wechat_ui_actions.UI_CALL_WAIT_TIMEOUT)
        self.assertEqual(len(bot._private_outbound_echoes["张三"]), 3)

    def test_owner_lightweight_actions_stop_before_unsent_items_after_new_message(self):
        calls = []

        class Owner:
            def call(self, intent, _timeout):
                calls.append(intent)
                if len(calls) == 2:
                    raise wechat_ui_actions.IntentCancelled("会话已有新消息")
                return [True]

        bot = WXBot.__new__(WXBot)
        bot._ui_owner = Owner()

        with self.assertRaises(wechat_ui_actions.IntentCancelled):
            bot._send_lightweight_actions_to_child(
                "张三",
                [
                    {"type": "text", "text": "第一条"},
                    {"type": "text", "text": "第二条"},
                    {"type": "text", "text": "第三条"},
                ],
                source="ai_reply",
                expected_sequence=7,
                delivery_id="delivery-2",
            )

        self.assertEqual(len(calls), 2)
        self.assertEqual(len(bot._private_outbound_echoes["张三"]), 1)

    def test_owner_text_send_reserves_echo_before_synchronous_self_callback(self):
        bot = WXBot.__new__(WXBot)
        bot.config = SimpleNamespace(cmd="管理员", group=[], memory_switch=True)
        bot._ui_ingress_queue = queue.Queue()
        bot._ui_ingress_stop = threading.Event()
        bot._ensure_message_runtime_state()

        class Owner:
            is_running = True

            def call(self, _intent, _timeout):
                bot._enqueue_ui_message(
                    ConversationRef("张三", "private"),
                    MessageEnvelope(type="text", attr="self", sender="self", content="同步回复"),
                )
                return True

        bot._ui_owner = Owner()
        self.assertTrue(bot._send_text_to_target_without_child("张三", "同步回复"))

        _conversation, callback = bot._ui_ingress_queue.get_nowait()
        self.assertTrue(callback._wxbot_private_outbound_echo)
        self.assertEqual(bot._get_private_message_sequence("张三"), 0)

    def test_synchronous_primary_echo_is_released_to_callback_when_result_is_unknown(self):
        bot = WXBot.__new__(WXBot)
        bot.config = SimpleNamespace(cmd="管理员", group=[], memory_switch=True)
        bot._ui_ingress_queue = queue.Queue()
        bot._ui_ingress_stop = threading.Event()
        bot._ensure_message_runtime_state()
        group_id = bot._reserve_private_outbound_echo_for_send(
            "张三",
            "text",
            "结果未知",
            source="private_ai_reply",
            fallback_content="结果未知",
            primary_memory_writer=True,
        )
        bot._enqueue_ui_message(
            ConversationRef("张三", "private"),
            MessageEnvelope(type="text", attr="self", sender="self", content="结果未知"),
        )
        bot._release_private_outbound_echo_group_to_callback(group_id)

        _conversation, callback = bot._ui_ingress_queue.get_nowait()
        chat = SimpleNamespace(who="张三", chat_type="private")
        self.assertTrue(callback._wxbot_private_outbound_echo_duplicate)
        self.assertFalse(bot._should_skip_message_memory(chat, callback))

    def test_reported_failed_send_still_recognizes_late_self_callback(self):
        bot = WXBot.__new__(WXBot)
        bot.config = SimpleNamespace(cmd="管理员", group=[], memory_switch=True)
        bot._ui_ingress_queue = queue.Queue()
        bot._ui_ingress_stop = threading.Event()
        bot._ensure_message_runtime_state()
        group_id = bot._reserve_private_outbound_echo_for_send(
            "张三", "text", "晚到回调", source="test", fallback_content="晚到回调"
        )
        self.assertFalse(bot._settle_private_outbound_echo_send(group_id, False))

        bot._enqueue_ui_message(
            ConversationRef("张三", "private"),
            MessageEnvelope(type="text", attr="self", sender="self", content="晚到回调"),
        )

        _conversation, callback = bot._ui_ingress_queue.get_nowait()
        self.assertTrue(callback._wxbot_private_outbound_echo)
        self.assertEqual(bot._get_private_message_sequence("张三"), 0)
        self.assertEqual(bot._private_outbound_echoes["张三"][0]["fallback_content"], "")

    def test_queued_private_ai_reply_skips_first_delay_but_keeps_split_delays(self):
        events = []

        class Owner:
            def call(self, intent, _timeout):
                events.append(("send", intent.payload["actions"][0]["text"]))
                return [True]

        bot = WXBot.__new__(WXBot)
        bot._ui_owner = Owner()
        bot.config = SimpleNamespace(
            chat_split_reply_switch=True,
            chat_split_reply_delay_switch=True,
        )
        bot._get_private_message_sequence = lambda _target: 7
        bot._human_delay_for_reply_part = lambda **kwargs: events.append((
            "delay",
            kwargs["part_text"],
            kwargs["split_continuation"],
            kwargs["is_last"],
        ))
        bot._remember_private_outbound_echo_for_send_result = lambda *_args, **_kwargs: True

        result = bot._send_lightweight_actions_to_child(
            "张三",
            [
                {"type": "text", "text": "第一条"},
                {"type": "text", "text": "第二条"},
                {"type": "text", "text": "第三条"},
            ],
            source="private_ai_reply",
            expected_sequence=7,
            delivery_id="delivery-delay",
        )

        self.assertTrue(result)
        self.assertEqual(events, [
            ("send", "第一条"),
            ("delay", "第二条", True, False), ("send", "第二条"),
            ("delay", "第三条", True, True), ("send", "第三条"),
        ])

    def test_queued_private_ai_reply_preserves_long_text_bubble_split(self):
        bot = WXBot.__new__(WXBot)
        bot.config = SimpleNamespace(split_long_text=lambda _text: ["长回复上半段", "长回复下半段"])
        captured = []
        bot._queue_lightweight_send = lambda target, actions, **kwargs: captured.append(
            (target, actions, kwargs)
        ) or True

        self.assertTrue(bot._queue_text_reply_until_target_verified(
            "张三",
            ["长" * LONG_REPLY_SEGMENT_CHARS, "第三条"],
            source="private_ai_reply",
            expected_sequence=9,
        ))

        self.assertEqual(captured[0][1], [
            {"type": "text", "text": "长回复上半段"},
            {"type": "text", "text": "长回复下半段"},
            {"type": "text", "text": "第三条"},
        ])

    def test_split_reply_delay_switch_can_disable_only_the_bubble_wait(self):
        bot = WXBot.__new__(WXBot)
        waits = []
        bot._wait_or_stop_requested = lambda seconds: waits.append(seconds) or False
        bot._split_reply_delay_seconds = lambda *_args, **_kwargs: 4.0

        bot._human_delay_for_reply_part(
            part_text="第二条",
            split_continuation=True,
            delay_enabled=False,
        )
        self.assertEqual(waits, [])

        bot._human_delay_for_reply_part(
            part_text="第二条",
            split_continuation=True,
            delay_enabled=True,
        )
        self.assertEqual(waits, [4.0])

    def test_owner_rechecks_lightweight_queue_version_at_execution(self):
        sequence_reads = iter([1, 2])
        sent = []
        bot = WXBot.__new__(WXBot)
        bot._get_private_message_sequence = lambda _target: next(sequence_reads)
        bot._remember_private_outbound_echo_for_send_result = lambda *_args, **_kwargs: True
        owner = wechat_ui_actions.WeChatUIOwner(
            {wechat_ui_actions.UIIntentKind.SEND_ACTIONS: lambda payload: sent.append(dict(payload)) or [True]},
            conversation_version_provider=bot._get_private_message_sequence,
            poll_interval=0.01,
        )
        bot._ui_owner = owner
        owner.start()
        try:
            bot._queue_lightweight_send(
                "张三",
                [{"type": "text", "text": "旧回复"}],
                source="ai_reply",
                expected_sequence=1,
            )
            self.assertFalse(bot._flush_lightweight_send_queue())
        finally:
            owner.stop()

        self.assertEqual(sent, [])
        self.assertEqual(bot._lightweight_send_queue, {})

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
        self.assertFalse(bot.wx.stopped)

        self.assertTrue(WXBot._finish_wxbot_stop(bot))
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
            chat_message_merge_delay=20,
            chat_voice_recognition_switch=False,
            chat_image_recognition_switch=False,
        )
        bot.is_stop_requested = lambda: False
        chat = SimpleNamespace(who="张三")
        first = SimpleNamespace(type="text", attr="friend", sender="张三", content="第一条", id="1")
        second = SimpleNamespace(type="text", attr="friend", sender="张三", content="第二条", id="2")

        with mock.patch("wxbot_core.threading.Timer", side_effect=fake_timer):
            self.assertTrue(bot._enqueue_private_message_for_ai(chat, first))
            self.assertEqual(timers[0].seconds, 20)
            self.assertAlmostEqual(timers[1].seconds, 60, places=2)
            self.assertTrue(bot._enqueue_private_message_for_ai(chat, second))

        self.assertEqual(len(timers), 3)
        self.assertEqual(timers[2].seconds, 20)
        self.assertTrue(timers[0].cancelled)
        pipeline = bot._private_message_pipelines["张三"]
        self.assertEqual([msg.content for msg in pipeline["open_messages"]], ["第一条", "第二条"])
        self.assertEqual(bot._get_private_message_sequence("张三"), 2)

    def test_private_message_merge_supports_minimum_one_second_delay(self):
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
            chat_message_merge_delay=1,
            chat_voice_recognition_switch=False,
            chat_image_recognition_switch=False,
        )
        bot.is_stop_requested = lambda: False
        chat = SimpleNamespace(who="张三")
        message = SimpleNamespace(type="text", attr="friend", sender="张三", content="马上回", id="1")

        with mock.patch("wxbot_core.threading.Timer", side_effect=fake_timer):
            self.assertTrue(bot._enqueue_private_message_for_ai(chat, message))

        pipeline = bot._private_message_pipelines["张三"]
        self.assertEqual(timers[0].seconds, 1.0)
        self.assertAlmostEqual(timers[1].seconds, 3.0, places=2)
        self.assertEqual([msg.content for msg in pipeline["open_messages"]], ["马上回"])
        self.assertEqual(bot._get_private_message_sequence("张三"), 1)

    def test_failed_private_voice_is_silently_ignored_without_batch_timer(self):
        bot = WXBot.__new__(WXBot)
        bot.config = SimpleNamespace(chat_message_merge_delay=3)
        bot.is_stop_requested = lambda: False
        bot._schedule_private_message_timer = lambda *_args, **_kwargs: self.fail("失败语音不应进入等待或回复队列")
        chat = SimpleNamespace(who="张三")
        message = SimpleNamespace(
            type="voice",
            attr="friend",
            sender="张三",
            content="语音未能转换",
            _voice_transcription_failed=True,
            id="v1",
        )

        with mock.patch("wxbot_core.log") as log_mock:
            self.assertTrue(bot._enqueue_private_message_for_ai(chat, message))

        self.assertNotIn("张三", getattr(bot, "_private_message_pipelines", {}))
        log_messages = [str(call.kwargs.get("message", "")) for call in log_mock.call_args_list]
        self.assertTrue(any("已静默忽略" in message for message in log_messages))

    def test_valid_private_message_after_failed_voice_is_processed_normally(self):
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
            chat_message_merge_delay=3,
            chat_voice_recognition_switch=True,
            chat_image_recognition_switch=False,
        )
        bot.is_stop_requested = lambda: False
        chat = SimpleNamespace(who="张三")
        failed_voice = SimpleNamespace(
            type="voice",
            attr="friend",
            sender="张三",
            content="语音未能转换",
            _voice_transcription_failed=True,
            id="v1",
        )
        valid_voice = SimpleNamespace(
            type="voice",
            attr="friend",
            sender="张三",
            content='语音8"秒我刚说的是这个',
            id="v2",
        )

        with mock.patch("wxbot_core.threading.Timer", side_effect=fake_timer):
            self.assertTrue(bot._enqueue_private_message_for_ai(chat, failed_voice))
            self.assertTrue(bot._enqueue_private_message_for_ai(chat, valid_voice))

        pipeline = bot._private_message_pipelines["张三"]
        self.assertEqual([msg.content for msg in pipeline["open_messages"]], ['语音8"秒我刚说的是这个'])
        self.assertEqual(len(timers), 2)

    def test_failed_private_voice_after_valid_message_does_not_join_batch(self):
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
            chat_message_merge_delay=3,
            chat_voice_recognition_switch=True,
            chat_image_recognition_switch=False,
        )
        bot.is_stop_requested = lambda: False
        chat = SimpleNamespace(who="张三")
        valid_voice = SimpleNamespace(
            type="voice",
            attr="friend",
            sender="张三",
            content='语音8"秒我刚说的是这个',
            id="v1",
        )
        failed_voice = SimpleNamespace(
            type="voice",
            attr="friend",
            sender="张三",
            content="语音未能转换",
            _voice_transcription_failed=True,
            id="v2",
        )

        with mock.patch("wxbot_core.threading.Timer", side_effect=fake_timer):
            self.assertTrue(bot._enqueue_private_message_for_ai(chat, valid_voice))
            self.assertTrue(bot._enqueue_private_message_for_ai(chat, failed_voice))

        self.assertEqual(len(timers), 2)
        pipeline = bot._private_message_pipelines["张三"]
        self.assertEqual([msg.content for msg in pipeline["open_messages"]], ['语音8"秒我刚说的是这个'])

    def test_text_reply_limit_logs_warning_when_capacity_is_hit(self):
        with tempfile.TemporaryDirectory() as tmp:
            bot = WXBot.__new__(WXBot)
            bot.config = SimpleNamespace(
                chat_text_reply_limit_switch=True,
                chat_text_reply_limit_count=1,
                chat_text_reply_limit_hours=24,
                chat_text_reply_limit_reply_once=True,
                chat_text_reply_limit_ai_reply=False,
                chat_text_reply_limit_reply="先休息一下",
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

    def test_text_reply_limit_warning_is_deduped_per_window(self):
        with tempfile.TemporaryDirectory() as tmp:
            bot = WXBot.__new__(WXBot)
            bot.config = SimpleNamespace(
                chat_text_reply_limit_switch=True,
                chat_text_reply_limit_count=1,
                chat_text_reply_limit_hours=24,
                chat_text_reply_limit_reply_once=False,
                chat_text_reply_limit_ai_reply=False,
                chat_text_reply_limit_reply="先休息一下",
            )
            bot.reply_count_store = ReplyCountStore(f"{tmp}/reply_count.json")
            bot.reply_count_store.increment_ai_count("张三", limit_hours=24)
            bot._record_replied_message_success = lambda: None
            sent = []
            bot._send_private_ai_reply_parts = lambda _chat, parts, **_kwargs: sent.extend(parts) or (True, True)
            chat = SimpleNamespace(who="张三")
            logs = []

            with mock.patch("wxbot_core.log", side_effect=lambda *args, **kwargs: logs.append((kwargs.get("level", "INFO"), kwargs.get("message", "")))):
                for _ in range(2):
                    handled, result = bot._check_text_reply_limit(chat, "张三")
                    self.assertTrue(handled)
                    self.assertTrue(result)

            self.assertEqual(sent, ["先休息一下", "先休息一下"])
            warning_logs = [message for level, message in logs if level == "WARNING" and "触发回复上限" in message]
            self.assertEqual(len(warning_logs), 1)

    def test_text_reply_limit_warning_logs_again_for_new_window(self):
        with tempfile.TemporaryDirectory() as tmp:
            bot = WXBot.__new__(WXBot)
            bot.config = SimpleNamespace(
                chat_text_reply_limit_switch=True,
                chat_text_reply_limit_count=1,
                chat_text_reply_limit_hours=24,
                chat_text_reply_limit_reply_once=False,
                chat_text_reply_limit_ai_reply=False,
                chat_text_reply_limit_reply="先休息一下",
            )
            bot.reply_count_store = ReplyCountStore(f"{tmp}/reply_count.json")
            bot.reply_count_store.increment_ai_count("张三", limit_hours=24)
            bot._record_replied_message_success = lambda: None
            bot._send_private_ai_reply_parts = lambda _chat, parts, **_kwargs: (True, True)
            chat = SimpleNamespace(who="张三")
            logs = []

            with mock.patch("wxbot_core.log", side_effect=lambda *args, **kwargs: logs.append((kwargs.get("level", "INFO"), kwargs.get("message", "")))):
                handled, result = bot._check_text_reply_limit(chat, "张三")
                self.assertTrue(handled)
                self.assertTrue(result)
                user_data = bot.reply_count_store.get_user("张三", limit_hours=24)
                user_data["window_started_at"] = "2099-01-01T00:00:00"
                handled, result = bot._check_text_reply_limit(chat, "张三")
                self.assertTrue(handled)
                self.assertTrue(result)

            warning_logs = [message for level, message in logs if level == "WARNING" and "触发回复上限" in message]
            self.assertEqual(len(warning_logs), 2)

    def test_group_text_reply_limit_isolated_by_group_and_sender(self):
        with tempfile.TemporaryDirectory() as tmp:
            bot = WXBot.__new__(WXBot)
            bot.config = SimpleNamespace(
                AtMe="",
                cmd="文件传输助手",
                AllListen_switch=False,
                listen_list=[],
                global_blacklist=[],
                group=["测试群", "另一个群"],
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
                reply_preprocess_fallback_reply="",
                reply_preprocess_fallback_once=False,
                group_voice_reply_switch=False,
                group_text_reply_limit_switch=True,
                group_text_reply_limit_count=1,
                group_text_reply_limit_hours=24,
                group_text_reply_limit_reply_once=False,
                group_text_reply_limit_ai_reply=False,
                group_text_reply_limit_reply="本轮先聊到这里",
                split_long_text=lambda text: [text],
            )
            bot.reply_count_store = ReplyCountStore(f"{tmp}/reply_count.json")
            bot.memory_manager = None
            bot._pause_group_reply = False
            bot.is_stop_requested = lambda: False
            bot._build_prompt_with_context = lambda *_args, **_kwargs: "prompt"
            bot._get_chat_send_lock = lambda _name: threading.Lock()
            bot._human_delay_for_reply_part = lambda *_args, **_kwargs: None
            bot._record_replied_message_success = lambda: None
            api_calls = []
            bot._get_group_api = lambda group: SimpleNamespace(
                chat=lambda *_args, **_kwargs: api_calls.append(group) or "正常群回复"
            )

            sent = []
            def make_chat(group_name):
                return SimpleNamespace(
                    who=group_name,
                    chat_type="group",
                    SendMsg=lambda msg, at=None: sent.append((group_name, msg, at)) or True,
                )

            def make_message(sender):
                return SimpleNamespace(
                    type="text",
                    attr="group",
                    sender=sender,
                    content="继续聊",
                    quote=lambda text, at=None: sent.append(("quote", text, at)) or True,
                )

            bot.reply_count_store.increment_ai_count("group:测试群:张三", limit_hours=24)

            self.assertTrue(bot.process_message(make_chat("测试群"), make_message("张三")))
            self.assertTrue(bot.process_message(make_chat("测试群"), make_message("李四")))
            self.assertTrue(bot.process_message(make_chat("另一个群"), make_message("张三")))

            self.assertEqual(
                sent,
                [
                    ("测试群", "本轮先聊到这里", None),
                    ("测试群", "正常群回复", None),
                    ("另一个群", "正常群回复", None),
                ],
            )
            self.assertEqual(api_calls, ["测试群", "另一个群"])
            self.assertEqual(bot.reply_count_store.get_user("group:测试群:李四")["count"], 1)
            self.assertEqual(bot.reply_count_store.get_user("group:另一个群:张三")["count"], 1)

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

        bot._voice_reply_state = VoiceReplyState(limits=state["limits"])
        bot._private_reply_can_continue = lambda *_args, **_kwargs: True
        logs = []
        chat = SimpleNamespace(who="张三")

        with mock.patch("wxbot_core.log", side_effect=lambda *args, **kwargs: logs.append((kwargs.get("level", "INFO"), kwargs.get("message", "")))):
            result = bot._try_send_voice_reply(
                chat,
                "你好",
                state_key="private:张三",
                limit_count=1,
                limit_hours=24,
            )

        self.assertFalse(result)
        self.assertTrue(any(level == "WARNING" and "语音回复触发上限" in message for level, message in logs))

    def test_voice_reply_limit_warning_is_deduped_per_window(self):
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

        bot._voice_reply_state = VoiceReplyState(limits=state["limits"])
        bot._private_reply_can_continue = lambda *_args, **_kwargs: True
        logs = []
        chat = SimpleNamespace(who="张三")

        with mock.patch("wxbot_core.log", side_effect=lambda *args, **kwargs: logs.append((kwargs.get("level", "INFO"), kwargs.get("message", "")))):
            for _ in range(2):
                result = bot._try_send_voice_reply(
                    chat,
                    "你好",
                    state_key="private:张三",
                    limit_count=1,
                    limit_hours=24,
                )
                self.assertFalse(result)

        warning_logs = [message for level, message in logs if level == "WARNING" and "语音回复触发上限" in message]
        self.assertEqual(len(warning_logs), 1)

    def test_voice_reply_limit_warning_logs_again_for_new_window(self):
        bot = WXBot.__new__(WXBot)
        bot.config = SimpleNamespace(cmd="文件传输助手", DATA_DIR="", group=[])
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

        bot._voice_reply_state = VoiceReplyState(limits=state["limits"])
        bot._private_reply_can_continue = lambda *_args, **_kwargs: True
        logs = []
        chat = SimpleNamespace(who="张三")

        with mock.patch("wxbot_core.log", side_effect=lambda *args, **kwargs: logs.append((kwargs.get("level", "INFO"), kwargs.get("message", "")))):
            result = bot._try_send_voice_reply(
                chat,
                "你好",
                state_key="private:张三",
                limit_count=1,
                limit_hours=24,
            )
            self.assertFalse(result)
            bot._voice_reply_state.limits["private:张三"]["window_started_at"] = "2099-01-01T00:00:00"
            result = bot._try_send_voice_reply(
                chat,
                "你好",
                state_key="private:张三",
                limit_count=1,
                limit_hours=24,
            )
            self.assertFalse(result)

        warning_logs = [message for level, message in logs if level == "WARNING" and "语音回复触发上限" in message]
        self.assertEqual(len(warning_logs), 2)

    def test_voice_reply_does_not_send_audio_when_wechat_ui_lock_is_busy(self):
        class BusyLock:
            def acquire(self, blocking=True):
                return False

            def release(self):
                raise AssertionError("busy lock should not be released")

        bot = WXBot.__new__(WXBot)
        bot.config = SimpleNamespace(cmd="文件传输助手", DATA_DIR="")
        bot._voice_reply_state = None
        bot._private_reply_can_continue = lambda *_args, **_kwargs: True
        bot._active_tts_config = lambda _user="": {"provider": "fake"}
        bot._record_tts_api_request = lambda: None
        bot._remove_temp_audio_file = lambda _path: None
        bot._get_wechat_action_lock = lambda: BusyLock()

        synth_calls = []

        class FakeTtsClient:
            def synthesize(self, text, audio_path):
                synth_calls.append((text, audio_path))

        chat = SimpleNamespace(
            who="张三",
            SendAudio=lambda *_args, **_kwargs: self.fail("微信 UI 锁忙时不应发送语音"),
        )

        with mock.patch("wxbot_core.create_tts_client", return_value=FakeTtsClient()):
            result = bot._try_send_voice_reply(
                chat,
                "你好",
                state_key="private:张三",
                limit_count=99,
                limit_hours=24,
            )

        self.assertFalse(result)
        self.assertEqual(len(synth_calls), 1)

    def test_voice_reply_sends_audio_while_holding_wechat_ui_lock(self):
        events = []

        class RecordingLock:
            locked = False

            def acquire(self, blocking=True):
                events.append(("acquire", blocking))
                self.locked = True
                return True

            def release(self):
                events.append(("release",))
                self.locked = False

        lock = RecordingLock()
        bot = WXBot.__new__(WXBot)
        bot.config = SimpleNamespace(cmd="文件传输助手", DATA_DIR="")
        bot._voice_reply_state = None
        bot._private_reply_can_continue = lambda *_args, **_kwargs: True
        bot._active_tts_config = lambda _user="": {"provider": "fake"}
        bot._record_tts_api_request = lambda: None
        bot._remove_temp_audio_file = lambda _path: None
        bot._get_wechat_action_lock = lambda: lock
        saved = []
        bot._save_private_reply_memory_message = lambda *_args, **_kwargs: saved.append(True) or True
        bot._ui_ingress_queue = queue.Queue()
        bot._ui_ingress_stop = threading.Event()
        bot._ensure_message_runtime_state()

        class FakeTtsClient:
            def synthesize(self, _text, _audio_path):
                pass

        def send_audio(**_kwargs):
            events.append(("send_audio", lock.locked))
            bot._enqueue_ui_message(
                ConversationRef("张三", "private"),
                MessageEnvelope(type="voice", attr="self", sender="self", content="[语音]"),
            )
            return True

        chat = SimpleNamespace(who="张三", SendAudio=send_audio)

        with mock.patch("wxbot_core.create_tts_client", return_value=FakeTtsClient()):
            result = bot._try_send_voice_reply(
                chat,
                "你好",
                state_key="private:张三",
                limit_count=99,
                limit_hours=24,
            )

        self.assertTrue(result)
        self.assertIn(("send_audio", True), events)
        self.assertEqual(events[-1], ("release",))
        self.assertEqual(saved, [True])
        _conversation, callback = bot._ui_ingress_queue.get_nowait()
        self.assertTrue(callback._wxbot_private_outbound_echo_duplicate)
        self.assertEqual(bot._get_private_message_sequence("张三"), 0)

    def test_voice_reply_does_not_fallback_after_send_result_becomes_uncertain(self):
        class RecordingLock:
            def acquire(self, blocking=True):
                return True

            def release(self):
                pass

        statuses = []
        bot = WXBot.__new__(WXBot)
        bot.config = SimpleNamespace(cmd="文件传输助手", DATA_DIR="", group=[], memory_switch=True)
        bot._voice_reply_state = None
        bot._private_reply_can_continue = lambda *_args, **_kwargs: True
        bot._active_tts_config = lambda _user="": {"provider": "fake"}
        bot._record_tts_api_request = lambda: None
        bot._remove_temp_audio_file = lambda _path: None
        bot._get_wechat_action_lock = lambda: RecordingLock()
        bot._mark_unanswered_send_started = lambda record_id="": statuses.append((record_id, "send_started"))
        bot._mark_unanswered_uncertain = lambda record_id="": statuses.append((record_id, "uncertain"))

        class FakeTtsClient:
            def synthesize(self, _text, _audio_path):
                pass

        def send_audio(**_kwargs):
            raise RuntimeError("send result lost")

        chat = SimpleNamespace(who="张三", SendAudio=send_audio)
        with mock.patch("wxbot_core.create_tts_client", return_value=FakeTtsClient()):
            with self.assertRaisesRegex(RuntimeError, "send result lost"):
                bot._try_send_voice_reply(
                    chat,
                    "你好",
                    state_key="private:张三",
                    limit_count=99,
                    limit_hours=24,
                    unanswered_record_id="record-1",
                )

        self.assertEqual(statuses, [("record-1", "send_started"), ("record-1", "uncertain")])

    def test_keyword_private_reply_registers_outbound_echoes(self):
        bot = WXBot.__new__(WXBot)
        bot.config = SimpleNamespace(cmd="文件传输助手", group=[])
        bot._ensure_message_runtime_state()
        bot._private_reply_can_continue = lambda *_args, **_kwargs: True
        sent = []

        class Chat:
            who = "张三"
            chat_type = "private"

            def SendMsg(self, msg=None, **_kwargs):
                sent.append(("text", msg))
                return True

            def SendFiles(self, filepath=None, **_kwargs):
                sent.append(("file", filepath))
                return True

        chat = Chat()
        bot._verified_send_chat = lambda _target, candidate=None: candidate
        bot._ensure_target_listen_chat_for_send = lambda _target: chat

        with tempfile.TemporaryDirectory() as tmp:
            image_path = os.path.join(tmp, "a.jpg")
            with open(image_path, "wb") as f:
                f.write(b"img")
            success, result = bot._send_keyword_reply_actions(
                chat,
                [
                    {"type": "text", "content": "关键词回复"},
                    {"type": "file", "path": image_path},
                ],
            )

        self.assertTrue(success)
        self.assertTrue(result)
        self.assertEqual(sent[0], ("text", "关键词回复"))
        echoes = bot._private_outbound_echoes["张三"]
        self.assertEqual([item["type"] for item in echoes], ["text", "image"])
        self.assertEqual(echoes[0]["content"], "关键词回复")
        self.assertTrue(all(item["source"] == "keyword_reply" for item in echoes))

    def test_known_material_echo_types_are_not_normalized_to_unknown(self):
        bot = WXBot.__new__(WXBot)

        self.assertEqual(bot._normalize_private_outbound_echo_type("note"), "note")
        self.assertEqual(bot._normalize_private_outbound_echo_type("location"), "location")
        self.assertEqual(bot._normalize_private_outbound_echo_type("personal_card"), "personal_card")

    def test_private_outbound_echo_ttl_uses_short_known_non_text_window(self):
        bot = WXBot.__new__(WXBot)

        self.assertEqual(bot._private_outbound_echo_ttl("text"), 60)
        self.assertEqual(bot._private_outbound_echo_ttl("image"), 60)
        self.assertEqual(bot._private_outbound_echo_ttl("voice"), 60)
        self.assertEqual(bot._private_outbound_echo_ttl("unknown"), 30)

    def test_persisted_private_outbound_echo_keeps_original_match_window(self):
        bot = WXBot.__new__(WXBot)
        bot.config = SimpleNamespace(cmd="管理员", group=[])
        bot._ensure_message_runtime_state()

        with mock.patch("wxbot_core.time.time", return_value=100.0):
            group_id = bot._reserve_private_outbound_echo_for_send(
                "张三", "text", "机器人回复", source="private_ai_reply"
            )
        original_expiry = bot._private_outbound_echoes["张三"][0]["expires_at"]

        with mock.patch("wxbot_core.time.time", return_value=101.0):
            self.assertTrue(bot._mark_private_outbound_echo_group_persisted(group_id))

        echo = bot._private_outbound_echoes["张三"][0]
        self.assertTrue(echo["memory_persisted"])
        self.assertEqual(echo["expires_at"], original_expiry)

    def test_persisted_echo_matches_once_then_same_text_new_id_is_manual(self):
        bot = WXBot.__new__(WXBot)
        bot.config = SimpleNamespace(cmd="管理员", group=[])
        bot._ensure_message_runtime_state()

        with mock.patch("wxbot_core.time.time", return_value=100.0):
            group_id = bot._reserve_private_outbound_echo_for_send(
                "张三", "text", "相同内容", source="private_ai_reply"
            )
            bot._mark_private_outbound_echo_group_persisted(group_id)

        with mock.patch("wxbot_core.time.time", return_value=106.0):
            robot_echo = bot._consume_private_outbound_echo(
                "张三",
                message=MessageEnvelope(
                    id="robot-echo",
                    type="text",
                    attr="self",
                    sender="self",
                    content="相同内容",
                ),
                return_match=True,
            )
        with mock.patch("wxbot_core.time.time", return_value=107.0):
            manual_message = bot._consume_private_outbound_echo(
                "张三",
                message=MessageEnvelope(
                    id="manual-message",
                    type="text",
                    attr="self",
                    sender="self",
                    content="相同内容",
                ),
                return_match=True,
            )

        self.assertTrue(robot_echo.get("dedupe_only"))
        self.assertIsNone(manual_message)

    def test_private_outbound_echoes_keep_last_twenty_per_chat(self):
        bot = WXBot.__new__(WXBot)
        bot.config = SimpleNamespace(cmd="文件传输助手", group=[])
        bot._ensure_message_runtime_state()

        for index in range(25):
            bot._remember_private_outbound_echo("张三", "text", f"消息{index}", source="test")

        echoes = bot._private_outbound_echoes["张三"]
        self.assertEqual(len(echoes), 20)
        self.assertEqual(echoes[0]["content"], "消息5")
        self.assertEqual(echoes[-1]["content"], "消息24")

    def test_pending_private_outbound_echo_query_prunes_expired_echoes(self):
        bot = WXBot.__new__(WXBot)
        bot.config = SimpleNamespace(cmd="文件传输助手", group=[])
        bot._ensure_message_runtime_state()
        bot._private_outbound_echoes = {
            "张三": [
                {"type": "text", "content": "过期", "source": "test", "remaining": 1, "expires_at": 99.0},
                {"type": "text", "content": "未过期", "source": "test", "remaining": 1, "expires_at": 101.0},
            ],
            "李四": [
                {"type": "image", "content": "", "source": "test", "remaining": 1, "expires_at": 98.0},
            ],
        }

        with mock.patch("wxbot_core.time.time", return_value=100.0):
            self.assertTrue(bot._has_pending_private_outbound_echoes())

        self.assertEqual(list(bot._private_outbound_echoes.keys()), ["张三"])
        self.assertEqual(bot._private_outbound_echoes["张三"][0]["content"], "未过期")

        with mock.patch("wxbot_core.time.time", return_value=102.0):
            self.assertFalse(bot._has_pending_private_outbound_echoes())
        self.assertEqual(bot._private_outbound_echoes, {})

    def test_claimed_private_outbound_echo_stays_pending_until_callback_settles(self):
        bot = WXBot.__new__(WXBot)
        bot.config = SimpleNamespace(cmd="管理员", group=[])
        bot._ensure_message_runtime_state()

        with mock.patch("wxbot_core.time.time", return_value=100.0):
            bot._remember_private_outbound_echo("张三", "text", "机器人回复", source="test")
            match = bot._consume_private_outbound_echo(
                "张三",
                message=MessageEnvelope(
                    id="echo-1",
                    type="text",
                    attr="self",
                    sender="self",
                    content="机器人回复",
                ),
                return_match=True,
            )
            self.assertTrue(bot._has_pending_private_outbound_echoes())

        callback = MessageEnvelope()
        callback._wxbot_outbound_echo_reservation = match["reservation_id"]
        with mock.patch("wxbot_core.time.time", return_value=101.0):
            self.assertTrue(bot._commit_private_outbound_echo_reservation(callback))
            self.assertFalse(bot._has_pending_private_outbound_echoes())

    def test_prepare_voice_uses_existing_wechat_auto_text_without_reconverting(self):
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
            to_text=lambda: self.fail("已有语音正文时不应重复转文字"),
        )
        chat = SimpleNamespace(who="张三", chat_type="private")

        message_routing.prepare_message_media(bot, msg, chat)

        self.assertFalse(getattr(msg, "_skip_ai_reply", False))
        self.assertFalse(getattr(msg, "_voice_transcription_failed", False))
        self.assertEqual(msg.content, '语音2"秒你好')

    def test_prepare_image_download_failure_skips_ai_and_memory(self):
        bot = WXBot.__new__(WXBot)
        bot.config = SimpleNamespace(
            AllListen_switch=False,
            listen_list=["瑞东（私人号）"],
            group=[],
            chat_image_recognition_switch=True,
            chat_voice_recognition_switch=False,
        )
        bot._mark_message_skip_memory = lambda message: setattr(message, "_skip_memory", True)
        chat = SimpleNamespace(who="瑞东（私人号）", chat_type="private")

        for download in (
            lambda: "",
            lambda: (_ for _ in ()).throw(RuntimeError("当前窗口存在多条相同消息")),
        ):
            with self.subTest(download=download):
                bot._ui_download_message = lambda _chat, _msg, quote_image=False: download()
                msg = SimpleNamespace(
                    attr="friend",
                    sender="瑞东（私人号）",
                    type="image",
                    content="图片",
                )

                message_routing.prepare_message_media(bot, msg, chat)

                self.assertTrue(getattr(msg, "_skip_ai_reply", False))
                self.assertTrue(getattr(msg, "_skip_memory", False))
                self.assertEqual(msg.content, "图片")

    def test_prepare_pending_voice_never_calls_to_text_and_queues_snapshot_reread(self):
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
        msg = SimpleNamespace(
            attr="friend",
            sender="张三",
            type="voice",
            content='语音2"秒',
            to_text=lambda: self.fail("生产路径不得调用 to_text"),
        )
        chat = SimpleNamespace(who="张三", chat_type="private")

        message_routing.prepare_message_media(bot, msg, chat)

        self.assertTrue(getattr(msg, "_skip_ai_reply", False))
        self.assertTrue(getattr(msg, "_skip_memory", False))
        self.assertEqual(msg.content, '语音2"秒')
        self.assertEqual(queued, [("张三", '语音2"秒')])

    def test_prepare_pending_voice_does_not_trust_to_text_failure(self):
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
        msg = SimpleNamespace(
            attr="friend",
            sender="张三",
            type="voice",
            content='语音1"秒',
            to_text=lambda: self.fail("生产路径不得调用 to_text"),
        )
        chat = SimpleNamespace(who="张三", chat_type="private")

        message_routing.prepare_message_media(bot, msg, chat)

        self.assertTrue(getattr(msg, "_skip_ai_reply", False))
        self.assertTrue(getattr(msg, "_skip_memory", False))
        self.assertFalse(getattr(msg, "_voice_transcription_failed", False))
        self.assertEqual(msg.content, '语音1"秒')
        self.assertEqual(queued, [("张三", '语音1"秒')])

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

    def test_prepare_pending_private_voice_does_not_invoke_converter(self):
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
        msg = SimpleNamespace(
            attr="friend",
            sender="张三",
            type="voice",
            content='语音2"秒',
            id="v1",
            to_text=lambda: self.fail("生产路径不得调用 to_text"),
        )
        chat = SimpleNamespace(who="张三", chat_type="private")

        with mock.patch("feature.message_routing.log") as log_mock:
            message_routing.prepare_message_media(bot, msg, chat)

        self.assertEqual(queued, [("张三", '语音2"秒')])
        log_messages = [str(call.kwargs.get("message", "")) for call in log_mock.call_args_list]
        self.assertFalse(any("等待后续重读" in message for message in log_messages))

    def test_prepare_failed_private_voice_uses_exact_status_texts(self):
        bot = WXBot.__new__(WXBot)
        bot.config = SimpleNamespace(
            AllListen_switch=False,
            listen_list=["张三"],
            group=[],
            chat_image_recognition_switch=False,
            chat_voice_recognition_switch=True,
        )
        chat = SimpleNamespace(who="张三", chat_type="private")

        for content in ("语音未能转换", '语音2"秒语音未能转换', "语音转换失败", "语音识别失败"):
            with self.subTest(content=content):
                msg = SimpleNamespace(attr="friend", sender="张三", type="voice", content=content)
                message_routing.prepare_message_media(bot, msg, chat)
                self.assertTrue(getattr(msg, "_voice_transcription_failed", False))
                self.assertTrue(getattr(msg, "_skip_ai_reply", False))
                self.assertTrue(getattr(msg, "_skip_memory", False))

    def test_voice_failure_detection_does_not_use_broad_contains(self):
        self.assertEqual(message_routing.voice_content_state("语音未能转换"), "failed")
        self.assertEqual(message_routing.voice_content_state('语音1"秒语音未能转换'), "failed")
        self.assertEqual(message_routing.voice_content_state("一条语音消息（未识别出文字）"), "pending")
        self.assertEqual(message_routing.voice_content_state("<msg><voicemsg /></msg>"), "pending")
        self.assertEqual(
            message_routing.voice_content_state('语音2"秒我这边显示语音未能转换怎么办'),
            "valid",
        )
        self.assertEqual(message_routing.voice_content_state("未能转换"), "valid")
        self.assertEqual(message_routing.voice_content_state("转换失败"), "valid")

    def test_pending_private_voice_reread_replaces_placeholder_with_resolved_text(self):
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
        timer_calls = []
        bot._schedule_private_message_timer = lambda seconds, callback, chat: timer_calls.append((seconds, callback, chat)) or SimpleNamespace(cancel=lambda: None)
        bot._read_pending_voice_snapshot = lambda _name: [SimpleNamespace(
            id="fresh-v1", attr="friend", sender="张三", type="voice", content="你好"
        )]
        original = SimpleNamespace(
            id="v1",
            attr="friend",
            sender="张三",
            type="voice",
            content='语音2"秒',
            to_text=lambda: "你好",
        )
        chat = SimpleNamespace(who="张三")

        self.assertTrue(bot._queue_pending_private_voice_transcription(chat, original))
        bot._flush_pending_private_voice_transcription(chat)

        self.assertTrue(any(seconds == 5 for seconds, callback, _chat in timer_calls if callback.__name__ == "_flush_pending_private_voice_transcription"))
        pipeline = bot._private_message_pipelines["张三"]
        self.assertEqual([msg.content for msg in pipeline["open_messages"]], ["你好"])
        self.assertNotIn("张三", bot._pending_private_voice_transcription)

    def test_pending_private_voice_reread_waits_until_text_is_ready(self):
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
        bot._save_private_incoming_memory_message = lambda *_args, **_kwargs: True
        timer_calls = []
        bot._schedule_private_message_timer = lambda seconds, callback, chat: timer_calls.append((seconds, callback, chat)) or SimpleNamespace(cancel=lambda: None)
        snapshots = iter([
            [SimpleNamespace(id="fresh-v1", attr="friend", sender="张三", type="voice", content='语音4"秒')],
            [SimpleNamespace(id="fresh-v2", attr="friend", sender="张三", type="voice", content="快写吧")],
        ])
        bot._read_pending_voice_snapshot = lambda _name: next(snapshots)
        original = SimpleNamespace(
            id="v1",
            attr="friend",
            sender="张三",
            type="voice",
            content='语音4"秒',
            to_text=lambda: to_text_results.pop(0) if to_text_results else "",
        )
        chat = SimpleNamespace(who="张三")

        self.assertTrue(bot._queue_pending_private_voice_transcription(chat, original))
        bot._flush_pending_private_voice_transcription(chat)

        self.assertIn("张三", bot._pending_private_voice_transcription)
        self.assertEqual(timer_calls[-1][0], 5)

        bot._flush_pending_private_voice_transcription(chat)

        pipeline = bot._private_message_pipelines["张三"]
        self.assertEqual([msg.content for msg in pipeline["open_messages"]], ["快写吧"])
        self.assertNotIn("张三", bot._pending_private_voice_transcription)

    def test_pending_private_voice_only_logs_final_unrecognized_result(self):
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
        bot._enqueue_private_message_for_ai = lambda *_args, **_kwargs: self.fail("未识别语音不应进 AI")
        bot._schedule_private_message_timer = lambda *_args, **_kwargs: SimpleNamespace(cancel=lambda: None)
        bot._read_pending_voice_snapshot = lambda _name: [SimpleNamespace(
            id="fresh-v1", attr="friend", sender="张三", type="voice", content='语音4"秒'
        )]
        original = SimpleNamespace(
            id="v1",
            attr="friend",
            sender="张三",
            type="voice",
            content='语音4"秒',
            to_text=lambda: "",
        )
        chat = SimpleNamespace(who="张三")

        with mock.patch("wxbot_core.log") as log_mock:
            self.assertTrue(bot._queue_pending_private_voice_transcription(chat, original))
            item = next(iter(bot._pending_private_voice_transcription["张三"]["items"].values()))
            item["reread_attempts"] = 1
            bot._flush_pending_private_voice_transcription(chat)

        log_messages = [str(call.kwargs.get("message", "")) for call in log_mock.call_args_list]
        self.assertFalse(any("语音识别结果暂未就绪" in message for message in log_messages))
        self.assertFalse(any("后继续重读" in message for message in log_messages))
        self.assertEqual(
            sum("重读 2 次仍未得到有效文字" in message for message in log_messages),
            1,
        )

    def test_pending_private_voice_reread_uses_fresh_window_snapshot(self):
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
        bot._save_private_incoming_memory_message = lambda *_args, **_kwargs: True
        bot._schedule_private_message_timer = lambda *_args, **_kwargs: SimpleNamespace(cancel=lambda: None)
        bot._read_pending_voice_snapshot = lambda _name: [SimpleNamespace(
            id="fresh-v1", attr="friend", sender="张三", type="voice", content="重读时转出来了"
        )]
        original = SimpleNamespace(
            id="v1",
            attr="friend",
            sender="张三",
            type="voice",
            content='语音4"秒',
            to_text=lambda: "重读时转出来了",
        )
        chat = SimpleNamespace(who="张三")

        self.assertTrue(bot._queue_pending_private_voice_transcription(chat, original))
        bot._flush_pending_private_voice_transcription(chat)

        pipeline = bot._private_message_pipelines["张三"]
        self.assertEqual([msg.content for msg in pipeline["open_messages"]], ["重读时转出来了"])
        self.assertNotIn("张三", bot._pending_private_voice_transcription)

    def test_pending_private_voice_reread_does_not_need_id_hash_match(self):
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
        bot._save_private_incoming_memory_message = lambda *_args, **_kwargs: True
        bot._schedule_private_message_timer = lambda *_args, **_kwargs: SimpleNamespace(cancel=lambda: None)
        bot._read_pending_voice_snapshot = lambda _name: [SimpleNamespace(
            id="new-ui-id", hash="new-hash", attr="friend", sender="张三", type="voice", content="原消息直接转出来"
        )]
        original = SimpleNamespace(
            id="old-ui-id",
            hash="old-hash",
            attr="friend",
            sender="张三",
            type="voice",
            content='语音10"秒',
            to_text=lambda: "原消息直接转出来",
        )
        chat = SimpleNamespace(who="张三")

        self.assertTrue(bot._queue_pending_private_voice_transcription(chat, original))
        bot._flush_pending_private_voice_transcription(chat)

        pipeline = bot._private_message_pipelines["张三"]
        self.assertEqual([msg.content for msg in pipeline["open_messages"]], ["原消息直接转出来"])
        self.assertNotIn("张三", bot._pending_private_voice_transcription)

    def test_pending_private_voice_reread_waits_on_unrecognized_placeholder(self):
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
        bot._enqueue_private_message_for_ai = lambda *_args, **_kwargs: self.fail("未识别占位不应立即进 AI")
        timer_calls = []
        bot._schedule_private_message_timer = lambda seconds, callback, chat: timer_calls.append((seconds, callback, chat)) or SimpleNamespace(cancel=lambda: None)
        bot._read_pending_voice_snapshot = lambda _name: [SimpleNamespace(
            id="fresh-v1", attr="friend", sender="张三", type="voice", content="一条语音消息（未识别出文字）"
        )]
        original = SimpleNamespace(
            id="v1",
            attr="friend",
            sender="张三",
            type="voice",
            content='语音4"秒',
            to_text=lambda: "一条语音消息（未识别出文字）",
        )
        chat = SimpleNamespace(who="张三")

        self.assertTrue(bot._queue_pending_private_voice_transcription(chat, original))
        bot._flush_pending_private_voice_transcription(chat)

        self.assertIn("张三", bot._pending_private_voice_transcription)
        self.assertEqual(timer_calls[-1][0], 5)

    def test_pending_private_voice_second_reread_uses_resolved_text_before_expiring(self):
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
        bot._save_private_incoming_memory_message = lambda *_args, **_kwargs: True
        bot._schedule_private_message_timer = lambda *_args, **_kwargs: SimpleNamespace(cancel=lambda: None)
        bot._read_pending_voice_snapshot = lambda _name: [SimpleNamespace(
            id="fresh-v1", attr="friend", sender="张三", type="voice", content="第三次终于识别出来"
        )]
        original = SimpleNamespace(
            id="v1",
            attr="friend",
            sender="张三",
            type="voice",
            content='语音4"秒',
            to_text=lambda: "第三次终于识别出来",
        )
        chat = SimpleNamespace(who="张三")

        self.assertTrue(bot._queue_pending_private_voice_transcription(chat, original))
        item = next(iter(bot._pending_private_voice_transcription["张三"]["items"].values()))
        item["reread_attempts"] = 1

        bot._flush_pending_private_voice_transcription(chat)

        pipeline = bot._private_message_pipelines["张三"]
        self.assertEqual([msg.content for msg in pipeline["open_messages"]], ["第三次终于识别出来"])
        self.assertNotIn("张三", bot._pending_private_voice_transcription)

    def test_private_batch_waits_for_pending_voice_when_merge_delay_is_short(self):
        bot = WXBot.__new__(WXBot)
        bot.config = SimpleNamespace(
            memory_switch=False,
            chat_message_merge_delay=1,
            chat_image_recognition_switch=False,
        )
        bot.is_stop_requested = lambda: False
        bot._chat_merge_lock = threading.Lock()
        bot._pending_private_voice_transcription = {}
        bot._private_message_pipelines = {}
        bot._private_message_sequence_by_chat = {}
        bot._incoming_seen_lock = threading.Lock()
        bot._incoming_seen_ids = {}
        bot._incoming_seen_fingerprints = {}

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
        bot._schedule_private_message_timer = lambda seconds, callback, chat: timers.append(FakeTimer(seconds, callback, (chat,))) or timers[-1]
        bot._start_private_message_worker_locked = lambda _chat, _pipeline: True
        bot._save_private_incoming_memory_message = lambda *_args, **_kwargs: True

        class TryLock:
            def acquire(self, blocking=True):
                return True

            def release(self):
                pass

        chat = SimpleNamespace(who="张三")
        bot._get_wechat_action_lock = lambda: TryLock()
        bot._read_pending_voice_snapshot = lambda _name: [SimpleNamespace(
            id="fresh-v1", attr="friend", sender="张三", type="voice", content="中间这句识别出来了"
        )]

        self.assertTrue(bot._enqueue_private_message_for_ai(
            chat,
            SimpleNamespace(id="t1", attr="friend", sender="张三", type="text", content="AAA"),
        ))
        voice = SimpleNamespace(
            id="v1",
            attr="friend",
            sender="张三",
            type="voice",
            content='语音4"秒',
            to_text=lambda: "中间这句识别出来了",
        )
        self.assertTrue(bot._queue_pending_private_voice_transcription(
            chat,
            voice,
        ))
        self.assertTrue(bot._enqueue_private_message_for_ai(
            chat,
            SimpleNamespace(id="t2", attr="friend", sender="张三", type="text", content="BBB"),
        ))

        idle_timer = next(timer for timer in reversed(timers) if timer.callback.__name__ == "_close_private_message_batch_by_idle")
        idle_timer.callback(*idle_timer.args)

        pipeline = bot._private_message_pipelines["张三"]
        self.assertTrue(pipeline.get("pending_voice_blocked_close"))
        self.assertEqual(len(pipeline["queued_batches"]), 0)

        bot._flush_pending_private_voice_transcription(chat)
        wake_timer = timers[-1]
        self.assertEqual(wake_timer.seconds, 0)
        wake_timer.callback(*wake_timer.args)

        queued = list(bot._private_message_pipelines["张三"]["queued_batches"][0])
        merged = bot._build_merged_private_message(queued)
        self.assertEqual(merged.content, "AAA\n中间这句识别出来了\nBBB")

    def test_private_batch_does_not_guess_between_same_duration_pending_voices(self):
        bot = WXBot.__new__(WXBot)
        bot.config = SimpleNamespace(
            memory_switch=False,
            chat_message_merge_delay=1,
            chat_image_recognition_switch=True,
        )
        bot.is_stop_requested = lambda: False
        bot._ensure_message_runtime_state()
        bot._save_private_incoming_memory_message = lambda *_args, **_kwargs: True
        bot._start_private_message_worker_locked = lambda _chat, _pipeline: True

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
        bot._schedule_private_message_timer = lambda seconds, callback, chat: timers.append(FakeTimer(seconds, callback, (chat,))) or timers[-1]

        chat = SimpleNamespace(who="张三")
        bot._get_wechat_action_lock = lambda: threading.RLock()
        bot._read_pending_voice_snapshot = lambda _name: [
            SimpleNamespace(id=f"fresh-v{index}", attr="friend", sender="张三", type="voice", content=text)
            for index, text in enumerate(["第一条。", "第二条。", "第三条。"], start=1)
        ]

        self.assertTrue(bot._enqueue_private_message_for_ai(
            chat,
            SimpleNamespace(id="t1", attr="friend", sender="张三", type="text", content="AAA"),
        ))
        for index, text in enumerate(["第一条。", "第二条。", "第三条。"], start=1):
            voice = SimpleNamespace(
                id=f"v{index}",
                attr="friend",
                sender="张三",
                type="voice",
                content='语音2"秒',
                to_text=lambda text=text: text,
            )
            self.assertTrue(bot._queue_pending_private_voice_transcription(chat, voice))
        self.assertTrue(bot._enqueue_private_message_for_ai(
            chat,
            SimpleNamespace(id="t2", attr="friend", sender="张三", type="text", content="BBB"),
        ))
        self.assertTrue(bot._enqueue_private_message_for_ai(
            chat,
            SimpleNamespace(id="img1", attr="friend", sender="张三", type="image", content=r"C:\tmp\after.png"),
        ))

        idle_timer = next(timer for timer in reversed(timers) if timer.callback.__name__ == "_close_private_message_batch_by_idle")
        idle_timer.callback(*idle_timer.args)

        pipeline = bot._private_message_pipelines["张三"]
        self.assertTrue(pipeline.get("pending_voice_blocked_close"))
        self.assertEqual(len(pipeline["queued_batches"]), 0)

        bot._flush_pending_private_voice_transcription(chat)
        wake_timer = timers[-1]
        self.assertEqual(wake_timer.seconds, 5)
        self.assertEqual(len(bot._pending_private_voice_transcription["张三"]["items"]), 3)
        self.assertTrue(pipeline.get("pending_voice_blocked_close"))
        self.assertEqual(len(pipeline["queued_batches"]), 0)

    def test_private_batch_drops_pending_voice_after_retry_limit_then_closes(self):
        bot = WXBot.__new__(WXBot)
        bot.config = SimpleNamespace(
            memory_switch=False,
            chat_message_merge_delay=1,
            chat_image_recognition_switch=False,
        )
        bot.is_stop_requested = lambda: False
        bot._chat_merge_lock = threading.Lock()
        bot._pending_private_voice_transcription = {}
        bot._private_message_pipelines = {}
        bot._private_message_sequence_by_chat = {}
        bot._incoming_seen_lock = threading.Lock()
        bot._incoming_seen_ids = {}
        bot._incoming_seen_fingerprints = {}

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
        bot._schedule_private_message_timer = lambda seconds, callback, chat: timers.append(FakeTimer(seconds, callback, (chat,))) or timers[-1]
        bot._start_private_message_worker_locked = lambda _chat, _pipeline: True
        bot._save_private_incoming_memory_message = lambda *_args, **_kwargs: True

        class TryLock:
            def acquire(self, blocking=True):
                return True

            def release(self):
                pass

        chat = SimpleNamespace(who="张三")
        bot._get_wechat_action_lock = lambda: TryLock()

        self.assertTrue(bot._enqueue_private_message_for_ai(
            chat,
            SimpleNamespace(id="t1", attr="friend", sender="张三", type="text", content="AAA"),
        ))
        self.assertTrue(bot._queue_pending_private_voice_transcription(
            chat,
            SimpleNamespace(id="v1", attr="friend", sender="张三", type="voice", content='语音4"秒'),
        ))
        self.assertTrue(bot._enqueue_private_message_for_ai(
            chat,
            SimpleNamespace(id="t2", attr="friend", sender="张三", type="text", content="BBB"),
        ))

        idle_timer = next(timer for timer in reversed(timers) if timer.callback.__name__ == "_close_private_message_batch_by_idle")
        idle_timer.callback(*idle_timer.args)
        self.assertTrue(bot._private_message_pipelines["张三"].get("pending_voice_blocked_close"))

        item = next(iter(bot._pending_private_voice_transcription["张三"]["items"].values()))
        item["reread_attempts"] = 2
        bot._flush_pending_private_voice_transcription(chat)
        wake_timer = timers[-1]
        self.assertEqual(wake_timer.seconds, 0)
        wake_timer.callback(*wake_timer.args)

        queued = list(bot._private_message_pipelines["张三"]["queued_batches"][0])
        merged = bot._build_merged_private_message(queued)
        self.assertEqual(merged.content, "AAA\nBBB")

    def test_pending_private_voice_result_is_ignored_if_self_clears_placeholder_during_reread(self):
        bot = WXBot.__new__(WXBot)
        bot.config = SimpleNamespace(
            memory_switch=False,
            chat_message_merge_delay=20,
            cmd="admin",
            group=[],
            chat_image_recognition_switch=False,
        )
        bot.is_stop_requested = lambda: False
        bot._ensure_message_runtime_state()
        bot._schedule_private_message_timer = lambda *_args, **_kwargs: SimpleNamespace(cancel=lambda: None)
        bot._enqueue_private_message_for_ai = lambda *_args, **_kwargs: self.fail("self 介入后过期语音不应重新入队")
        bot._save_private_incoming_memory_message = lambda *_args, **_kwargs: self.fail("self 介入后过期语音不应写入 history")

        class TryLock:
            def acquire(self, blocking=True):
                return True

            def release(self):
                pass

        chat = SimpleNamespace(who="张三", chat_type="private")
        bot._get_wechat_action_lock = lambda: TryLock()

        def reread_after_self(_chat):
            bot._handle_private_self_message_boundary(
                chat,
                SimpleNamespace(attr="self", sender="self", type="text", content="我接手"),
            )
            return "已经识别出来"

        bot._read_pending_voice_snapshot = lambda _name: [SimpleNamespace(
            id="fresh-v1",
            attr="friend",
            sender="张三",
            type="voice",
            content=reread_after_self(chat),
        )]

        self.assertTrue(bot._queue_pending_private_voice_transcription(
            chat,
            SimpleNamespace(
                id="v1",
                attr="friend",
                sender="张三",
                type="voice",
                content='语音4"秒',
                to_text=lambda: reread_after_self(chat),
            ),
        ))
        with mock.patch("wxbot_core.log") as log_mock:
            self.assertTrue(bot._flush_pending_private_voice_transcription(chat))

        self.assertNotIn("张三", bot._pending_private_voice_transcription)
        self.assertNotIn("张三", bot._private_message_pipelines)
        log_messages = [str(call.kwargs.get("message", "")) for call in log_mock.call_args_list]
        self.assertTrue(any("语音识别结果已过期" in message for message in log_messages))

    def test_pending_private_voice_deadline_unblocks_batch_when_wechat_lock_stays_busy(self):
        bot = WXBot.__new__(WXBot)
        bot.config = SimpleNamespace(
            memory_switch=False,
            chat_message_merge_delay=1,
            chat_image_recognition_switch=False,
        )
        bot.is_stop_requested = lambda: False
        bot._ensure_message_runtime_state()
        bot._save_private_incoming_memory_message = lambda *_args, **_kwargs: True

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
        bot._schedule_private_message_timer = lambda seconds, callback, chat: timers.append(FakeTimer(seconds, callback, (chat,))) or timers[-1]
        bot._start_private_message_worker_locked = lambda _chat, _pipeline: True

        class BusyLock:
            def acquire(self, blocking=True):
                return False

        chat = SimpleNamespace(who="张三")
        bot._get_wechat_action_lock = lambda: BusyLock()

        self.assertTrue(bot._enqueue_private_message_for_ai(
            chat,
            SimpleNamespace(id="t1", attr="friend", sender="张三", type="text", content="AAA"),
        ))
        self.assertTrue(bot._queue_pending_private_voice_transcription(
            chat,
            SimpleNamespace(id="v1", attr="friend", sender="张三", type="voice", content='语音4"秒'),
        ))
        self.assertTrue(bot._enqueue_private_message_for_ai(
            chat,
            SimpleNamespace(id="t2", attr="friend", sender="张三", type="text", content="BBB"),
        ))
        idle_timer = next(timer for timer in reversed(timers) if timer.callback.__name__ == "_close_private_message_batch_by_idle")
        idle_timer.callback(*idle_timer.args)
        item = next(iter(bot._pending_private_voice_transcription["张三"]["items"].values()))
        item["first_seen_at"] = time.time() - 16

        self.assertTrue(bot._flush_pending_private_voice_transcription(chat))
        wake_timer = timers[-1]
        self.assertEqual(wake_timer.seconds, 0)
        wake_timer.callback(*wake_timer.args)

        queued = list(bot._private_message_pipelines["张三"]["queued_batches"][0])
        merged = bot._build_merged_private_message(queued)
        self.assertEqual(merged.content, "AAA\nBBB")

    def test_pending_private_voice_deadline_still_accepts_text_if_final_reread_succeeds(self):
        bot = WXBot.__new__(WXBot)
        bot.config = SimpleNamespace(memory_switch=False)
        bot.is_stop_requested = lambda: False
        bot._ensure_message_runtime_state()
        bot._save_private_incoming_memory_message = lambda *_args, **_kwargs: True
        bot._schedule_private_message_timer = lambda *_args, **_kwargs: SimpleNamespace(cancel=lambda: None)

        class TryLock:
            def acquire(self, blocking=True):
                return True

            def release(self):
                pass

        chat = SimpleNamespace(who="张三")
        bot._get_wechat_action_lock = lambda: TryLock()
        bot._read_pending_voice_snapshot = lambda _name: [SimpleNamespace(
            id="fresh-v1", attr="friend", sender="张三", type="voice", content="最后一刻识别成功"
        )]

        self.assertTrue(bot._queue_pending_private_voice_transcription(
            chat,
            SimpleNamespace(
                id="v1",
                attr="friend",
                sender="张三",
                type="voice",
                content='语音4"秒',
                to_text=lambda: "最后一刻识别成功",
            ),
        ))
        item = next(iter(bot._pending_private_voice_transcription["张三"]["items"].values()))
        item["first_seen_at"] = time.time() - 16

        self.assertTrue(bot._flush_pending_private_voice_transcription(chat))

        pipeline = bot._private_message_pipelines["张三"]
        self.assertEqual([msg.content for msg in pipeline["open_messages"]], ["最后一刻识别成功"])
        self.assertNotIn("张三", bot._pending_private_voice_transcription)

    def test_pending_private_voice_reread_silently_ignores_unrecognized_after_max_attempts(self):
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
        bot._enqueue_private_message_for_ai = lambda *_args, **_kwargs: self.fail("未识别占位不应进 AI")
        bot._schedule_private_message_timer = lambda *_args, **_kwargs: SimpleNamespace(cancel=lambda: None)
        bot._read_pending_voice_snapshot = lambda _name: [SimpleNamespace(
            id="fresh-v1", attr="friend", sender="张三", type="voice", content="<msg><voicemsg /></msg>"
        )]
        original = SimpleNamespace(
            id="v1",
            attr="friend",
            sender="张三",
            type="voice",
            content='语音4"秒',
            to_text=lambda: "<msg><voicemsg /></msg>",
        )
        chat = SimpleNamespace(who="张三")

        self.assertTrue(bot._queue_pending_private_voice_transcription(chat, original))
        item = next(iter(bot._pending_private_voice_transcription["张三"]["items"].values()))
        item["reread_attempts"] = 2

        with mock.patch("wxbot_core.log") as log_mock:
            bot._flush_pending_private_voice_transcription(chat)

        self.assertNotIn("张三", bot._pending_private_voice_transcription)
        log_messages = [str(call.kwargs.get("message", "")) for call in log_mock.call_args_list]
        self.assertTrue(any("重读 2 次仍未得到有效文字" in message for message in log_messages))

    def test_pending_private_voice_does_not_guess_when_resolved_snapshot_lacks_identity(self):
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
        bot._save_private_incoming_memory_message = lambda *_args, **_kwargs: True
        bot._schedule_private_message_timer = lambda *_args, **_kwargs: SimpleNamespace(cancel=lambda: None)
        chat = SimpleNamespace(who="张三")
        bot._read_pending_voice_snapshot = lambda _name: [
            SimpleNamespace(id="fresh-v1", attr="friend", sender="张三", type="voice", content="这是识别成功的内容"),
            SimpleNamespace(id="fresh-v2", attr="friend", sender="张三", type="voice", content="一条语音消息（未识别出文字）"),
        ]

        first_voice = SimpleNamespace(
            id="v1",
            attr="friend",
            sender="张三",
            type="voice",
            content='语音3"秒',
            to_text=lambda: "这是识别成功的内容",
        )
        second_voice = SimpleNamespace(
            id="v2",
            attr="friend",
            sender="张三",
            type="voice",
            content='语音5"秒',
            to_text=lambda: "一条语音消息（未识别出文字）",
        )
        self.assertTrue(bot._queue_pending_private_voice_transcription(
            chat,
            first_voice,
        ))
        self.assertTrue(bot._queue_pending_private_voice_transcription(
            chat,
            second_voice,
        ))
        for item in bot._pending_private_voice_transcription["张三"]["items"].values():
            if (item.get("signature") or {}).get("duration") == 5:
                item["reread_attempts"] = 2

        bot._flush_pending_private_voice_transcription(chat)

        pipeline = bot._private_message_pipelines["张三"]
        self.assertEqual([msg.content for msg in pipeline["open_messages"]], ['语音3"秒'])
        self.assertIn("张三", bot._pending_private_voice_transcription)

    def test_pending_private_voice_reread_silently_ignores_empty_voice_after_max_attempts(self):
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
        bot._enqueue_private_message_for_ai = lambda *_args, **_kwargs: self.fail("空语音不应进 AI")
        bot._schedule_private_message_timer = lambda *_args, **_kwargs: SimpleNamespace(cancel=lambda: None)
        bot._read_pending_voice_snapshot = lambda _name: [SimpleNamespace(
            id="fresh-v1", attr="friend", sender="张三", type="voice", content=""
        )]
        original = SimpleNamespace(
            id="v1",
            attr="friend",
            sender="张三",
            type="voice",
            content='语音4"秒',
            to_text=lambda: "",
        )
        chat = SimpleNamespace(who="张三")

        self.assertTrue(bot._queue_pending_private_voice_transcription(chat, original))
        item = next(iter(bot._pending_private_voice_transcription["张三"]["items"].values()))
        item["reread_attempts"] = 2

        with mock.patch("wxbot_core.log") as log_mock:
            bot._flush_pending_private_voice_transcription(chat)

        self.assertNotIn("张三", bot._pending_private_voice_transcription)
        log_messages = [str(call.kwargs.get("message", "")) for call in log_mock.call_args_list]
        self.assertTrue(any("重读 2 次仍未得到有效文字" in message for message in log_messages))

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
            reply_preprocess_fallback_reply="",
            reply_preprocess_fallback_once=False,
            api_error_reply_once=False,
            chat_text_reply_limit_switch=False,
            chat_text_reply_limit_hours=24,
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

    def test_reply_preprocess_fallback_once_resets_with_reply_window(self):
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
                reply_preprocess_fallback_reply="换个说法吧",
                reply_preprocess_fallback_once=True,
                api_error_reply_once=False,
                chat_text_reply_limit_switch=False,
                chat_text_reply_limit_hours=24,
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
            reply_preprocess_fallback_reply="",
            reply_preprocess_fallback_once=False,
            api_error_reply="接口忙，稍后再聊",
            api_error_reply_once=False,
            chat_text_reply_limit_switch=False,
            chat_text_reply_limit_hours=24,
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

    def test_group_reply_preprocess_fallback_once_uses_sender_window(self):
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
                reply_preprocess_fallback_reply="换个说法吧",
                reply_preprocess_fallback_once=True,
                group_text_reply_limit_hours=24,
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

    def test_group_reply_preprocess_fallback_once_does_not_mark_failed_send(self):
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
                reply_preprocess_fallback_reply="换个说法吧",
                reply_preprocess_fallback_once=True,
                group_text_reply_limit_hours=24,
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
            self.assertFalse(user_data.get("preprocess_fallback_notified"))

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
            reply_preprocess_fallback_reply="",
            reply_preprocess_fallback_once=False,
            group_text_reply_limit_hours=24,
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

    def test_group_text_reply_sends_while_holding_wechat_ui_lock(self):
        class RecordingLock:
            locked = False

            def __enter__(self):
                self.locked = True
                return self

            def __exit__(self, exc_type, exc, tb):
                self.locked = False
                return False

        lock = RecordingLock()
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
            reply_preprocess_fallback_reply="",
            reply_preprocess_fallback_once=False,
            group_text_reply_limit_hours=24,
            group_voice_reply_switch=False,
            split_long_text=lambda text: [text],
        )
        bot.reply_count_store = ReplyCountStore("")
        bot.memory_manager = None
        bot._pause_group_reply = False
        bot.is_stop_requested = lambda: False
        bot._get_group_api = lambda _group: SimpleNamespace(chat=lambda *_args, **_kwargs: "群回复")
        bot._build_prompt_with_context = lambda *_args, **_kwargs: "prompt"
        bot._get_chat_send_lock = lambda _name: threading.Lock()
        bot._get_wechat_action_lock = lambda: lock
        bot._human_delay_for_reply_part = lambda *_args, **_kwargs: None
        bot._record_replied_message_success = lambda: None

        sent = []
        chat = SimpleNamespace(
            who="测试群",
            chat_type="group",
            SendMsg=lambda msg, at=None: sent.append((msg, at, lock.locked)) or True,
        )
        message = SimpleNamespace(
            type="text",
            attr="group",
            sender="张三",
            content="测试",
            quote=lambda text, at=None: sent.append((text, at, lock.locked)) or True,
        )

        self.assertTrue(bot.process_message(chat, message))

        self.assertEqual(sent, [("群回复", None, True)])

    def test_group_preprocess_rewrite_api_error_skips_voice_reply(self):
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
            reply_preprocess_max_chars=10,
            reply_preprocess_fallback_reply="",
            reply_preprocess_fallback_once=False,
            api_error_reply="接口忙",
            api_error_reply_once=False,
            group_text_reply_limit_hours=24,
            group_voice_reply_switch=True,
            group_voice_reply_request_keywords=["语音"],
            split_long_text=lambda text: [text],
        )
        bot.reply_count_store = ReplyCountStore("")
        bot.memory_manager = None
        bot._pause_group_reply = False
        bot.is_stop_requested = lambda: False
        replies = iter(["这是一段明显超过限制的回复内容", API_ERROR_REPLY_TEXT])
        bot._get_group_api = lambda _group: SimpleNamespace(chat=lambda *_args, **_kwargs: next(replies))
        bot._build_prompt_with_context = lambda *_args, **_kwargs: "prompt"
        bot._get_chat_send_lock = lambda _name: threading.Lock()
        bot._human_delay_for_reply_part = lambda *_args, **_kwargs: None
        bot._record_replied_message_success = lambda: None
        bot._try_send_voice_reply = lambda *_args, **_kwargs: self.fail("接口错误回复不应进入语音发送")

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
            content="发语音说一下",
            quote=lambda text, at=None: sent.append((text, at)) or True,
        )

        self.assertTrue(bot.process_message(chat, message))
        self.assertEqual(sent, [("接口忙", None)])

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
                reply_preprocess_fallback_reply="",
                reply_preprocess_fallback_once=False,
                api_error_reply="接口忙",
                api_error_reply_once=True,
                group_text_reply_limit_hours=24,
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

    def test_group_failed_voice_is_marked_skip_without_fallback_reply(self):
        bot = WXBot.__new__(WXBot)
        bot.config = SimpleNamespace(
            AllListen_switch=False,
            listen_list=[],
            group=["测试群"],
            group_image_recognition_switch=False,
            group_voice_recognition_switch=True,
        )
        chat = SimpleNamespace(who="测试群", chat_type="group")
        message = SimpleNamespace(
            type="voice",
            attr="group",
            sender="张三",
            content="语音识别失败",
        )

        message_routing.prepare_message_media(bot, message, chat)

        self.assertTrue(getattr(message, "_voice_transcription_failed", False))
        self.assertTrue(getattr(message, "_skip_ai_reply", False))
        self.assertTrue(getattr(message, "_skip_memory", False))

    def test_group_voice_candidate_supports_selected_trigger_modes(self):
        keyword_config = SimpleNamespace(
            group_voice_reply_switch=True,
            group_voice_reply_trigger_modes=["keyword"],
            group_voice_reply_request_keywords=["语音"],
        )
        incoming_voice_config = SimpleNamespace(
            group_voice_reply_switch=True,
            group_voice_reply_trigger_modes=["incoming_voice"],
            group_voice_reply_request_keywords=["语音"],
        )

        self.assertTrue(group_voice_candidate(
            keyword_config,
            SimpleNamespace(type="text", content="请用语音回复"),
        ))
        self.assertFalse(group_voice_candidate(
            keyword_config,
            SimpleNamespace(type="voice", content="普通转写内容"),
        ))
        self.assertTrue(group_voice_candidate(
            incoming_voice_config,
            SimpleNamespace(type="voice", content="普通转写内容"),
        ))
        incoming_voice_config.group_voice_reply_trigger_modes = []
        self.assertFalse(group_voice_candidate(
            incoming_voice_config,
            SimpleNamespace(type="voice", content="普通转写内容"),
        ))

    def test_group_incoming_voice_obeys_group_at_only_route(self):
        config = SimpleNamespace(
            AtMe="@机器人",
            AllListen_switch=False,
            global_blacklist=[],
            group=["测试群"],
            group_switch=True,
            group_keyword_switch=False,
            group_keyword_at_only=False,
            keyword_dict={},
            group_reply_at=False,
            group_listen_only=False,
            group_image_recognition_switch=False,
        )
        bot = SimpleNamespace(config=config, _pause_group_reply=False)
        chat = SimpleNamespace(who="测试群", chat_type="group")
        message = SimpleNamespace(type="voice", sender="张三", content="普通转写内容")

        self.assertEqual(message_routing.route_process_message(bot, chat, message)["action"], "group_ai")

        config.group_reply_at = True
        self.assertEqual(message_routing.route_process_message(bot, chat, message)["action"], "skip")

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
            reply_preprocess_fallback_reply="",
            reply_preprocess_fallback_once=False,
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
            reply_preprocess_fallback_reply="",
            reply_preprocess_fallback_once=False,
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
        bot.config = SimpleNamespace(chat_message_merge_delay=20)
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
            chat_message_merge_delay=20,
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
            chat_message_merge_delay=20,
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
        self.assertEqual(scheduled[0][1], 40.0)
        self.assertEqual(scheduled[1][0], "_close_private_message_batch_by_max_wait")
        self.assertAlmostEqual(scheduled[1][1], 60.0, delta=0.1)

        self.assertTrue(bot._enqueue_private_message_for_ai(chat, text_msg))

        self.assertEqual(scheduled[-1][0], "_close_private_message_batch_by_idle")
        self.assertEqual(scheduled[-1][1], 40.0)
        self.assertEqual(
            [name for name, _seconds in scheduled].count("_close_private_message_batch_by_max_wait"),
            1,
        )

    def test_private_image_batch_can_wait_up_to_double_max_base_delay(self):
        bot = WXBot.__new__(WXBot)
        bot.config = SimpleNamespace(
            chat_message_merge_delay=60,
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

        bot._ensure_message_runtime_state()
        self.assertTrue(bot._enqueue_private_message_for_ai(chat, image_msg))

        self.assertEqual(scheduled[0][0], "_close_private_message_batch_by_idle")
        self.assertEqual(scheduled[0][1], 120.0)
        self.assertEqual(scheduled[1][0], "_close_private_message_batch_by_max_wait")
        self.assertAlmostEqual(scheduled[1][1], 180.0, delta=0.1)

    def test_private_image_sets_pending_context_before_ai_worker_runs(self):
        bot = WXBot.__new__(WXBot)
        bot.config = SimpleNamespace(
            chat_message_merge_delay=20,
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

    def test_private_repeated_image_sends_with_distinct_ids_are_both_queued(self):
        bot = WXBot.__new__(WXBot)
        bot.config = SimpleNamespace(
            chat_message_merge_delay=20,
            chat_image_recognition_switch=True,
        )
        bot.is_stop_requested = lambda: False
        bot._schedule_private_message_timer = lambda *_args, **_kwargs: SimpleNamespace(cancel=lambda: None)
        bot._existing_local_image_path = lambda path: path
        bot._start_private_message_worker_locked = lambda _chat, _pipeline: True
        chat = SimpleNamespace(who="张三")
        first = SimpleNamespace(
            type="image", attr="friend", sender="张三", content=r"C:\tmp\same.png", id="image-1", hash="",
        )
        second = SimpleNamespace(
            type="image", attr="friend", sender="张三", content=r"C:\tmp\same.png", id="image-2", hash="",
        )

        bot._ensure_message_runtime_state()
        self.assertTrue(bot._enqueue_private_message_for_ai(chat, first))
        self.assertTrue(bot._enqueue_private_message_for_ai(chat, second))

        pipeline = bot._private_message_pipelines["张三"]
        self.assertEqual([message.id for message in pipeline["open_messages"]], ["image-1", "image-2"])

    def test_private_image_only_batch_reaches_ai_after_idle_close(self):
        bot = WXBot.__new__(WXBot)
        bot.config = SimpleNamespace(chat_message_merge_delay=20)
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

            def SendMsgBatch(self, *_args, **_kwargs):
                raise AssertionError("私聊 AI 气泡不得作为不可中断批次发送")

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
        sent_items = []

        with mock.patch("wxbot_core.log") as log_mock:
            self.assertEqual(
                bot._send_private_ai_reply_parts(
                    chat,
                    ["第一段", "第二段", "第三段"],
                    expected_sequence=expected_sequence,
                    sent_items=sent_items,
                ),
                (True, True),
            )
        log_messages = [str(call.kwargs.get("message", "")) for call in log_mock.call_args_list]
        self.assertFalse(any("已停止发送上一轮剩余回复" in message for message in log_messages))
        self.assertEqual(chat.sent, ["第一段"])
        self.assertEqual(sent_items, ["第一段"])

    def test_private_reply_waits_checks_and_sends_each_bubble_in_order(self):
        events = []

        class FakeChat:
            who = "张三"
            chat_type = "private"

            def SendMsg(self, text):
                events.append(("send", text))
                return True

            def SendMsgBatch(self, *_args, **_kwargs):
                raise AssertionError("私聊 AI 气泡不得作为不可中断批次发送")

        bot = WXBot.__new__(WXBot)
        bot.config = SimpleNamespace(
            cmd="管理员",
            chat_listen_only=False,
            chat_split_reply_switch=True,
            chat_split_reply_delay_switch=True,
        )
        bot.is_stop_requested = lambda: False
        bot._pause_chat_reply = False
        bot._verified_send_chat = lambda _target, chat: chat
        bot._get_chat_send_lock = lambda _name: threading.Lock()
        bot._human_delay_for_reply_part = lambda **kwargs: events.append(("wait", kwargs["part_text"]))
        bot._save_private_reply_memory_message = lambda _chat, text: events.append(("history", text)) or True
        bot._ensure_message_runtime_state()
        bot._next_private_message_sequence("张三")
        expected_sequence = bot._get_private_message_sequence("张三")

        self.assertEqual(
            bot._send_private_ai_reply_parts(
                FakeChat(),
                ["第一段", "第二段", "第三段"],
                expected_sequence=expected_sequence,
            ),
            (True, True),
        )

        self.assertEqual(events, [
            ("wait", "第一段"), ("send", "第一段"), ("history", "第一段"),
            ("wait", "第二段"), ("send", "第二段"), ("history", "第二段"),
            ("wait", "第三段"), ("send", "第三段"), ("history", "第三段"),
        ])

    def test_private_reply_none_result_waits_for_self_echo_before_memory_save(self):
        chat = SimpleNamespace(who="张三", chat_type="private", SendMsg=lambda _text: None)
        bot = WXBot.__new__(WXBot)
        bot.config = SimpleNamespace(cmd="管理员", chat_listen_only=False)
        bot.is_stop_requested = lambda: False
        bot._pause_chat_reply = False
        bot._verified_send_chat = lambda _target, candidate: candidate
        bot._get_chat_send_lock = lambda _name: threading.Lock()
        bot._human_delay_for_reply_part = lambda *_args, **_kwargs: None
        saved = []
        bot._save_private_reply_memory_message = lambda _chat, text: saved.append(text) or True
        bot._ensure_message_runtime_state()

        success, result = bot._send_private_ai_reply_parts(chat, ["结果未知的回复"])

        self.assertFalse(success)
        self.assertIsNone(result)
        self.assertEqual(saved, [])
        self.assertTrue(bot._consume_private_reply_runtime_echo("张三", "结果未知的回复"))
        self.assertFalse(bot._consume_private_reply_runtime_echo("张三", "结果未知的回复"))

    def test_private_reply_self_callback_before_send_return_does_not_duplicate_memory(self):
        bot = WXBot.__new__(WXBot)
        bot.config = SimpleNamespace(cmd="管理员", group=[], chat_listen_only=False)
        bot.is_stop_requested = lambda: False
        bot._pause_chat_reply = False
        bot._verified_send_chat = lambda _target, candidate: candidate
        bot._get_chat_send_lock = lambda _name: threading.Lock()
        bot._human_delay_for_reply_part = lambda *_args, **_kwargs: None
        saved = []
        callback_skips = []
        bot._save_private_reply_memory_message = lambda _chat, text: saved.append(text) or True
        bot._ensure_message_runtime_state()

        class FakeChat:
            who = "张三"
            chat_type = "private"

            def SendMsg(self, text):
                callback = SimpleNamespace(attr="self", sender="self", type="text", content=text)
                callback_skips.append(bot._should_skip_message_memory(self, callback))
                return True

        self.assertEqual(bot._send_private_ai_reply_parts(FakeChat(), ["只保存一次"]), (True, True))
        self.assertEqual(callback_skips, [True])
        self.assertEqual(saved, ["只保存一次"])

    def test_reset_stop_request_allows_next_start(self):
        bot = WXBot.__new__(WXBot)
        bot._ensure_stop_requested_event().set()

        bot._reset_stop_request()

        self.assertFalse(bot.is_stop_requested())

    def test_alllisten_rejects_raw_wechat_client(self):
        bot = WXBot.__new__(WXBot)
        bot.wx = SimpleNamespace()

        with self.assertRaisesRegex(RuntimeError, "必须通过微信 UI owner"):
            bot.ALLListen_mode(last_time=0)

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
