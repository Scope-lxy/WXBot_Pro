import base64
import os
import queue
import sqlite3
import time
import unittest
import threading
import tempfile
import uuid
from datetime import datetime, timedelta
from collections import deque
from types import SimpleNamespace
from unittest import mock

from core import wechat_ui_actions
from core.api import API_ERROR_REPLY_TEXT, DusAPI, OpenAIAPI, build_api_config_snapshot
from core.inbound_coordinator import InboundCoordinator
from core.message_pipeline import (
    ConversationRef,
    MessageEnvelope,
    contains_group_mention,
    strip_group_mention,
)
from core.message_store import MessageStore
from core.prompting import build_current_turn_user_message, build_image_user_message
from core.reply_delivery import (
    DeliveryResult,
    DeliveryStatus,
    ReplyAction,
    ReplyDeliveryCoordinator,
    ReplyEchoTracker,
)
from core.reply_pipeline import ImageReplyPipeline, ImageReplyRequest
from core.reply_count_store import ReplyCountStore
from core.vision_bridge import VisionNote
from core.wechat_ui_runtime import OwnedChat
from feature import listening, message_routing
from feature.voice_reply import group_voice_candidate
from feature.scheduled_messages import execute_scheduled_message_task
from wxbot_core import LONG_REPLY_SEGMENT_CHARS, WXBOT_SAVE_DIR_NAME, WXBot, WxParam


def make_message_runtime_bot(data_dir):
    bot = WXBot.__new__(WXBot)
    bot.config = SimpleNamespace(group=[], cmd="管理员")
    bot._ui_ingress_queue = queue.Queue()
    bot._stop_requested_event = threading.Event()
    bot._message_store = MessageStore(data_dir, "test-account")
    bot._inbound_coordinator = InboundCoordinator(bot._message_store)
    bot._reply_echo_tracker = ReplyEchoTracker()
    return bot


def configure_group_reply_runtime(bot, data_dir=None):
    if data_dir is None:
        bot._test_message_store_tempdir = tempfile.TemporaryDirectory()
        data_dir = bot._test_message_store_tempdir.name
    store = MessageStore(data_dir, f"test-group-{uuid.uuid4().hex}")
    bot._message_store = store
    bot._reply_echo_tracker = ReplyEchoTracker()
    bot._reply_delivery_coordinator = ReplyDeliveryCoordinator(
        store=store,
        version_provider=lambda conversation, chat_type="private": store.conversation_version(
            conversation,
            chat_type=chat_type,
        ),
        prepare=bot._prepare_reply_delivery,
        sender=bot._send_reply_delivery,
    )
    bot._ui_owner = None
    return store


def process_persisted_group_message(bot, chat, message):
    if not getattr(chat, "_test_reply_send_wrapped", False):
        send_message = chat.SendMsg
        chat.SendMsg = lambda msg=None, at=None, **_kwargs: send_message(msg, at=at)
        chat._test_reply_send_wrapped = True
    now = time.time()
    stored = bot._message_store.record_inbound({
        "conversation": chat.who,
        "chat_type": "group",
        "direction": "friend",
        "sender": getattr(message, "sender", ""),
        "content": getattr(message, "content", ""),
        "original_content": getattr(message, "content", ""),
        "message_type": getattr(message, "type", "text"),
        "native_attr": getattr(message, "attr", "group"),
        "native_id": f"test-{uuid.uuid4().hex}",
        "native_time": getattr(message, "time", ""),
        "received_at": now,
        "source": "test",
        "source_batch": f"test-{uuid.uuid4().hex}",
        "source_order": 0,
    })
    message._wxbot_event_id = stored["event_id"]
    message._wxbot_event_ids = (stored["event_id"],)
    message._wxbot_event_version = stored["version"]
    message._wxbot_reply_expires_at = now + 15 * 60
    message._wxbot_received_at = now
    return bot.process_message(chat, message)


def configure_persisted_private_reply(bot, chat, message, data_dir):
    store = configure_group_reply_runtime(bot, data_dir)
    persist_private_inbound(store, chat, message)
    return store


def persist_private_inbound(store, chat, message):
    now = time.time()
    stored = store.record_inbound({
        "conversation": chat.who,
        "chat_type": "private",
        "direction": "friend",
        "sender": getattr(message, "sender", chat.who),
        "content": getattr(message, "content", ""),
        "original_content": getattr(message, "content", ""),
        "message_type": getattr(message, "type", "text"),
        "native_attr": getattr(message, "attr", "friend"),
        "native_id": str(getattr(message, "id", "") or f"test-{uuid.uuid4().hex}"),
        "native_time": getattr(message, "time", ""),
        "received_at": now,
        "source": "test",
        "source_batch": f"test-{uuid.uuid4().hex}",
        "source_order": 0,
    })
    message._wxbot_event_id = stored["event_id"]
    message._wxbot_event_ids = (stored["event_id"],)
    message._wxbot_event_version = stored["version"]
    message._wxbot_reply_expires_at = now + 15 * 60
    message._wxbot_received_at = now
    return stored


class ApiBehaviorTests(unittest.TestCase):
    def test_missing_api_error_reply_log_names_the_conversation(self):
        bot = WXBot.__new__(WXBot)
        bot.config = SimpleNamespace(api_error_reply="")

        with mock.patch("wxbot_core.log") as log_mock:
            parts = bot._api_error_reply_parts(conversation="张三", chat_type="private")

        self.assertEqual(parts, [])
        self.assertEqual(
            log_mock.call_args.kwargs["message"],
            "私聊 张三：AI 回复失败，未配置失败固定回复，本次未发送回复",
        )

    def test_material_source_skip_terminalizes_persisted_inbound(self):
        bot = WXBot.__new__(WXBot)
        bot._test_message_store_tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(bot._test_message_store_tempdir.cleanup)
        bot._message_store = MessageStore(bot._test_message_store_tempdir.name, "test-account")
        bot.config = SimpleNamespace(
            group=["素材群"],
            group_image_recognition_switch=False,
            group_voice_recognition_switch=False,
        )
        bot._handle_material_source_message = lambda *_args: True
        bot._record_received_message = lambda: None
        bot.process_message = lambda *_args: self.fail("素材源静默消息不应进入普通路由")
        bot.last_msg_time = ""
        bot.last_msg_sender = ""
        message = MessageEnvelope(
            id="material-text-1",
            attr="friend",
            sender="群友A",
            type="text",
            content="普通聊天",
        )
        now = time.time()
        stored = bot._message_store.record_inbound({
            "conversation": "素材群",
            "chat_type": "group",
            "direction": "friend",
            "sender": "群友A",
            "content": message.content,
            "original_content": message.content,
            "message_type": message.type,
            "native_attr": message.attr,
            "native_id": message.id,
            "received_at": now,
            "source": "test",
            "source_batch": "material-source",
            "source_order": 0,
        })
        message._wxbot_event_id = stored["event_id"]
        message._wxbot_event_ids = (stored["event_id"],)
        chat = SimpleNamespace(who="素材群", chat_type="group")

        self.assertTrue(
            message_routing.handle_friend_message_callback(
                bot,
                message,
                chat,
                text="素材群收到普通聊天",
            )
        )

        self.assertEqual(
            bot._message_store.get_event(stored["event_id"])["processing_state"],
            "handled",
        )

    def test_private_keyword_delivery_error_is_not_converted_to_api_error(self):
        bot = WXBot.__new__(WXBot)
        bot.config = SimpleNamespace(
            chat_text_reply_limit_switch=False,
            api_error_reply_once=False,
        )
        bot._voice_reply_state = {"loaded": True}
        bot._get_private_message_sequence = lambda _name: 0
        bot._send_keyword_reply_actions = lambda *_args, **_kwargs: (
            (_ for _ in ()).throw(ValueError("claim contract failed"))
        )
        message = SimpleNamespace(type="text", content="关键词")

        with self.assertRaisesRegex(ValueError, "claim contract failed"):
            bot._wx_send_ai_once(
                SimpleNamespace(who="张三"),
                message,
                keyword_plan={"reply": "关键词回复"},
                user_key="",
            )

    def test_skipped_private_media_reaches_not_required_terminal_state(self):
        with tempfile.TemporaryDirectory() as tmp:
            bot = make_message_runtime_bot(tmp)
            bot.config = SimpleNamespace(
                chat_image_recognition_switch=False,
                chat_voice_recognition_switch=False,
            )
            chat = SimpleNamespace(who="张三", chat_type="private")
            message = MessageEnvelope(
                id="image-1",
                attr="friend",
                sender="张三",
                type="image",
                content="[图片]",
            )
            stored = persist_private_inbound(bot._message_store, chat, message)
            message._skip_ai_reply = True

            bot._enqueue_private_message_for_ai(chat, message)

            event = bot._message_store.get_event(stored["event_id"])
            self.assertEqual(event["processing_state"], "handled")
            self.assertEqual(event["processing_state"], "handled")

    def test_pending_private_voice_stays_recoverable(self):
        with tempfile.TemporaryDirectory() as tmp:
            bot = make_message_runtime_bot(tmp)
            bot.config = SimpleNamespace(
                chat_image_recognition_switch=False,
                chat_voice_recognition_switch=True,
            )
            chat = SimpleNamespace(who="张三", chat_type="private")
            message = MessageEnvelope(
                id="voice-1",
                attr="friend",
                sender="张三",
                type="voice",
                content='语音8"秒',
            )
            stored = persist_private_inbound(bot._message_store, chat, message)
            message._skip_ai_reply = True
            message._wxbot_pending_voice_key = "voice:张三:1"

            bot._enqueue_private_message_for_ai(chat, message)

            event = bot._message_store.get_event(stored["event_id"])
            self.assertEqual(event["processing_state"], "pending")
            self.assertEqual(event["processing_state"], "pending")

    def test_paused_private_reply_does_not_survive_as_pending(self):
        with tempfile.TemporaryDirectory() as tmp:
            bot = make_message_runtime_bot(tmp)
            bot.config = SimpleNamespace(chat_listen_only=False)
            bot._pause_chat_reply = True
            chat = SimpleNamespace(who="张三", chat_type="private")
            message = MessageEnvelope(
                id="paused-1",
                attr="friend",
                sender="张三",
                type="text",
                content="暂停期间的消息",
            )
            stored = persist_private_inbound(bot._message_store, chat, message)

            self.assertTrue(bot.wx_send_ai(chat, message))

            event = bot._message_store.get_event(stored["event_id"])
            self.assertEqual(event["processing_state"], "handled")
            self.assertEqual(event["processing_state"], "handled")

    def test_openai_chat_success_still_returns_model_text(self):
        api = OpenAIAPI.__new__(OpenAIAPI)
        api.config = build_api_config_snapshot(
            {"sdk": "OpenAI", "model": "configured-model", "api_protocol": "chat_completions"},
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
        self.assertEqual(fake_log.call_args.kwargs["level"], "WARNING")
        self.assertIn("接口：actual-model", fake_log.call_args.kwargs["message"])

    def test_openai_chat_empty_content_logs_request_and_response_for_debugging(self):
        api = OpenAIAPI.__new__(OpenAIAPI)
        api.config = build_api_config_snapshot(
            {"sdk": "OpenAI", "model": "configured-model", "api_protocol": "chat_completions"},
            prompt="系统提示",
            max_retries=0,
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
        self.assertEqual(
            next(
                call.kwargs["level"]
                for call in fake_log.call_args_list
                if "API空响应诊断" in call.kwargs.get("message", "")
            ),
            "DEBUG",
        )
        self.assertTrue(any(
            call.kwargs.get("level") == "WARNING"
            and "API调用失败" in call.kwargs.get("message", "")
            for call in fake_log.call_args_list
        ))
        self.assertIn("接口：configured-model", debug_messages[0])
        self.assertIn("动画表情 [早上好]", debug_messages[0])
        self.assertIn("chatcmpl-test", debug_messages[0])

    def test_openai_chat_nonstream_uses_reasoning_content_when_content_empty(self):
        api = OpenAIAPI.__new__(OpenAIAPI)
        api.config = build_api_config_snapshot(
            {"sdk": "OpenAI", "model": "configured-model", "api_protocol": "chat_completions"},
            prompt="系统提示",
            max_retries=0,
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
    def test_listener_thread_exception_arms_listener_recovery(self):
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
        self.assertNotIn("消息时间：", result)

    def test_current_turn_user_message_places_one_full_time_before_message_text(self):
        result = build_current_turn_user_message(
            "晚安，早点睡\n谢谢你老公晚安咯\n明天我也会想你的",
            now="2026-07-15 15:20",
            message_time="2026/07/14 23:15:00",
        )

        self.assertIn(
            "[用户消息]\n消息时间：2026-07-14 23:15\n晚安，早点睡\n谢谢你老公晚安咯\n明天我也会想你的",
            result,
        )
        self.assertEqual(result.count("消息时间："), 1)

    def test_current_message_time_lookup_failure_degrades_to_no_time(self):
        def fail_lookup(*_args, **_kwargs):
            raise RuntimeError("lookup failed")

        bot = WXBot.__new__(WXBot)
        bot._message_store = SimpleNamespace(
            current_message_native_time=fail_lookup,
        )
        message = SimpleNamespace(_wxbot_event_ids=("event-1",))

        with mock.patch("wxbot_core.log") as log_mock:
            result = bot._current_message_time_reference(
                SimpleNamespace(who="张三"),
                message,
                chat_type="private",
            )

        self.assertEqual(result, "")
        self.assertIn("已按无时间继续", log_mock.call_args.kwargs["message"])

    def test_two_stage_image_reply_places_visual_note_in_current_user_message(self):
        captured = {}
        logs = []

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
            log_info=logs.append,
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
            message_time="2026-07-14 23:15:00",
        ))

        self.assertEqual(result, "最终回复")
        self.assertIn("[运行信息]", captured["message"])
        self.assertIn("本轮消息包含图片：", captured["message"])
        self.assertIn("[图片]一张蓝色会标。", captured["message"])
        self.assertIn("可见文字：ERC 博济全球慈善互助会", captured["message"])
        self.assertIn("消息内容：这里写的什么？", captured["message"])
        self.assertIn("[用户消息]\n消息时间：2026-07-14 23:15", captured["message"])
        self.assertNotIn("图片概览：", captured["message"])
        self.assertEqual(captured["prompt_kwargs"]["image_parse_block"], "IMAGE_RULES")
        self.assertEqual(logs, ["私聊 张三：AI 正在先识别图片内容，再生成回复"])

    def test_direct_image_reply_uses_prompt_builder_keyword_signature(self):
        captured = {}
        logs = []

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
            log_info=logs.append,
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
        self.assertEqual(logs, ["私聊 张三：AI 正在识别图片并生成回复"])

    def test_private_image_reply_generates_visual_note_before_final_reply(self):
        bot = WXBot.__new__(WXBot)
        bot.config = SimpleNamespace(chat_image_recognition_api_id="api_recognition")
        note = "图片概览：一张自拍。\n可见文字：无。\n关键细节：戴着帽子。\n不确定项：无。"
        generated = []
        remembered = []
        captured = {}
        bot._generate_visual_notes_for_image_paths = (
            lambda chat_type, paths, **kwargs: generated.append((chat_type, list(paths), kwargs)) or [note]
        )
        bot._remember_visual_notes = (
            lambda chat_name, paths, notes, **kwargs: remembered.append(
                (chat_name, list(paths), list(notes), kwargs["chat_type"])
            ) or True
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
        self.assertEqual(remembered, [("张三", [r"C:\tmp\selfie.png"], [note], "private")])
        self.assertEqual(captured["visual_notes"], [note])

    def test_private_pending_visual_context_clears_only_after_notes_exist(self):
        bot = WXBot.__new__(WXBot)
        bot._ensure_message_runtime_state()
        bot._set_pending_visual_context(
            "张三",
            [r"C:\tmp\selfie.png"],
            chat_type="private",
            visual_notes=[""],
        )

        self.assertFalse(bot._pending_visual_context_ready_to_clear("张三", chat_type="private"))

        bot._set_pending_visual_context(
            "张三",
            [r"C:\tmp\selfie.png"],
            chat_type="private",
            visual_notes=["图片概览：一张自拍。\n可见文字：无。\n关键细节：戴着帽子。\n不确定项：无。"],
        )

        self.assertTrue(bot._pending_visual_context_ready_to_clear("张三", chat_type="private"))

    def test_group_image_reply_generates_visual_note_before_final_reply(self):
        bot = WXBot.__new__(WXBot)
        bot.config = SimpleNamespace(group_image_recognition_api_id="api_recognition")
        note = "图片概览：一张活动海报。\n可见文字：周六 19:00。\n关键细节：地点在东门。\n不确定项：无。"
        generated = []
        remembered = []
        captured = {}
        bot._generate_visual_notes_for_image_paths = (
            lambda chat_type, paths, **kwargs: generated.append((chat_type, list(paths), kwargs)) or [note]
        )
        bot._remember_visual_notes = (
            lambda chat_name, paths, notes, **kwargs: remembered.append(
                (chat_name, list(paths), list(notes), kwargs["chat_type"])
            ) or True
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
        self.assertEqual(remembered, [("测试群", [r"C:\tmp\poster.png"], [note], "group")])
        self.assertEqual(captured["visual_notes"], [note])
        self.assertEqual(captured["image_senders"], ["李四"])

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
                    chat_image_recognition_switch=False,
                    chat_voice_recognition_switch=False,
                )
                self.wx = SimpleNamespace(nickname="wxbot")
                self.msg_received_count = 0
                self.last_msg_time = ""
                self.last_msg_sender = ""

            def _record_received_message(self):
                self.msg_received_count += 1

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
            all_Mode_listen_list=[
                ["张三", 1.0],
                ["张三", 2.0, "group"],
            ],
        )

        with (
            mock.patch("feature.message_routing._bot_time_module", return_value=SimpleNamespace(time=lambda: 9.0)),
            mock.patch("feature.message_routing._bot_log"),
        ):
            message_routing._update_alllisten_timestamp(bot, "张三", "private")

        self.assertEqual(
            bot.all_Mode_listen_list,
            [["张三", 9.0], ["张三", 2.0, "group"]],
        )

    def test_private_ingress_advances_version_before_business_queue_runs(self):
        with tempfile.TemporaryDirectory() as tmp:
            bot = make_message_runtime_bot(tmp)
            conversation = ConversationRef("张三", "private")
            message = MessageEnvelope(
                id="message-1",
                type="text",
                attr="friend",
                sender="张三",
                content="新消息",
            )

            self.assertTrue(bot._enqueue_ui_message(conversation, message))

            self.assertEqual(bot._message_store.conversation_version("张三"), 1)
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

    def test_slow_group_business_does_not_block_private_ingress(self):
        bot = WXBot.__new__(WXBot)
        bot._ui_ingress_queue = queue.Queue()
        bot._ui_ingress_stop = threading.Event()
        bot._ui_owner = SimpleNamespace(wait_for_contact_idle=lambda: True)
        bot._stop_requested = threading.Event()
        bot._chat_merge_lock = threading.Lock()
        bot._group_message_pipelines = {}
        group_started = threading.Event()
        release_group = threading.Event()
        private_done = threading.Event()

        def handle(message, chat):
            if chat.chat_type == "group":
                group_started.set()
                release_group.wait()
            else:
                private_done.set()
            return True

        bot.message_handle_callback = handle
        worker = threading.Thread(target=bot._run_ui_ingress)
        worker.start()
        bot._ui_ingress_queue.put((
            ConversationRef("测试群", "group"),
            MessageEnvelope(type="text", attr="group", sender="张三", content="慢回复"),
        ))
        bot._ui_ingress_queue.put((
            ConversationRef("李四", "private"),
            MessageEnvelope(type="text", attr="friend", sender="李四", content="你好"),
        ))
        try:
            self.assertTrue(group_started.wait(1))
            self.assertTrue(private_done.wait(1))
        finally:
            release_group.set()
            bot._ui_ingress_stop.set()
            worker.join(1)

    def test_same_group_business_messages_remain_serial(self):
        bot = WXBot.__new__(WXBot)
        bot._ui_owner = object()
        bot._stop_requested = threading.Event()
        bot._chat_merge_lock = threading.Lock()
        bot._group_message_pipelines = {}
        first_started = threading.Event()
        release_first = threading.Event()
        second_done = threading.Event()
        processed = []

        def handle(message, _chat):
            processed.append(message.content)
            if message.content == "第一条":
                first_started.set()
                release_first.wait()
            else:
                second_done.set()
            return True

        bot.message_handle_callback = handle
        conversation = ConversationRef("测试群", "group")
        bot._enqueue_group_message_for_business(
            conversation,
            MessageEnvelope(content="第一条"),
        )
        self.assertTrue(first_started.wait(1))
        bot._enqueue_group_message_for_business(
            conversation,
            MessageEnvelope(content="第二条"),
        )
        self.assertFalse(second_done.wait(0.05))
        release_first.set()
        self.assertTrue(second_done.wait(1))
        self.assertEqual(processed, ["第一条", "第二条"])

    def test_duplicate_private_ingress_does_not_advance_version_twice(self):
        with tempfile.TemporaryDirectory() as tmp:
            bot = make_message_runtime_bot(tmp)
            conversation = ConversationRef("张三", "private")

            for _index in range(2):
                bot._enqueue_ui_message(conversation, MessageEnvelope(
                    id="same-message",
                    type="text",
                    attr="friend",
                    sender="张三",
                    content="新消息",
                ))

            self.assertEqual(bot._message_store.conversation_version("张三"), 1)
            self.assertEqual(bot._ui_ingress_queue.qsize(), 1)

    def test_manual_self_ingress_invalidates_old_reply_but_bot_echo_does_not(self):
        with tempfile.TemporaryDirectory() as tmp:
            bot = make_message_runtime_bot(tmp)
            conversation = ConversationRef("张三", "private")
            bot._reply_echo_tracker.reserve(
                "turn-1:0",
                "张三",
                ReplyAction("text", "机器人回复"),
                confirmable=False,
            )
            bot._reply_echo_tracker.activate(("turn-1:0",))

            echo = MessageEnvelope(type="text", attr="self", sender="self", content="机器人回复")
            manual = MessageEnvelope(type="text", attr="self", sender="self", content="人工回复")
            bot._enqueue_ui_message(conversation, echo)
            bot._enqueue_ui_message(conversation, manual)

            self.assertEqual(bot._message_store.conversation_version("张三"), 1)
            self.assertEqual(bot._message_store.get_event(echo._wxbot_event_id)["direction"], "bot_echo")
            self.assertEqual(bot._message_store.get_event(manual._wxbot_event_id)["direction"], "manual_self")
            self.assertEqual(bot._ui_ingress_queue.qsize(), 1)

    def test_duplicate_private_self_callback_id_is_ignored(self):
        with tempfile.TemporaryDirectory() as tmp:
            bot = make_message_runtime_bot(tmp)
            conversation = ConversationRef("张三", "private")
            bot._reply_echo_tracker.reserve(
                "turn-1:0",
                "张三",
                ReplyAction("text", "机器人回复"),
                confirmable=False,
            )
            bot._reply_echo_tracker.activate(("turn-1:0",))

            first = MessageEnvelope(id="self-1", type="text", attr="self", sender="self", content="机器人回复")
            duplicate = MessageEnvelope(id="self-1", type="text", attr="self", sender="self", content="机器人回复")
            bot._enqueue_ui_message(conversation, first)
            bot._enqueue_ui_message(conversation, duplicate)

            self.assertEqual(bot._ui_ingress_queue.qsize(), 0)
            self.assertEqual(bot._message_store.conversation_version("张三"), 0)

    def test_uncertain_send_runtime_echo_does_not_invalidate_remaining_bubbles(self):
        with tempfile.TemporaryDirectory() as tmp:
            bot = make_message_runtime_bot(tmp)
            bot._reply_echo_tracker.reserve(
                "runtime-send-1",
                "张三",
                ReplyAction("text", "结果未知的机器人气泡"),
                confirmable=False,
            )
            bot._reply_echo_tracker.activate(("runtime-send-1",))
            conversation = ConversationRef("张三", "private")
            echo = MessageEnvelope(
                type="text",
                attr="self",
                sender="self",
                content="结果未知的机器人气泡",
            )

            bot._enqueue_ui_message(conversation, echo)

            self.assertEqual(bot._message_store.conversation_version("张三"), 0)
            self.assertEqual(bot._message_store.get_event(echo._wxbot_event_id)["direction"], "bot_echo")
            self.assertEqual(bot._ui_ingress_queue.qsize(), 0)

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

    def test_media_download_dir_follows_kernel_save_path(self):
        bot = WXBot.__new__(WXBot)
        original = getattr(WxParam, "DEFAULT_SAVE_PATH", "")
        try:
            WxParam.DEFAULT_SAVE_PATH = r"C:\tmp\wxbot_custom"
            self.assertEqual(bot._media_download_dir(), r"C:\tmp\wxbot_custom")

            WxParam.DEFAULT_SAVE_PATH = ""
            self.assertTrue(bot._media_download_dir().endswith(WXBOT_SAVE_DIR_NAME))
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
        bot._pending_visual_contexts = {}
        bot._chat_memory_dirty_lock = threading.Lock()
        bot._chat_memory_dirty_chats = {}
        bot._chat_memory_worker_running = False
        bot._global_scan_stop = threading.Event()
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

    def test_finish_wxbot_stop_runs_full_cleanup_once_without_explicit_stop(self):
        bot = WXBot.__new__(WXBot)
        bot.run_flag = True
        bot._stop_requested = threading.Event()
        bot._stop_cleanup_lock = threading.Lock()
        bot._stop_cleanup_done = False
        bot._global_scan_stop = threading.Event()
        bot._cancel_pending_private_message_timers = mock.Mock()
        bot._clear_group_message_pipelines = mock.Mock()
        bot._clear_chat_memory_background_state = mock.Mock()
        bot._reply_delivery_coordinator = mock.Mock()
        bot._message_store = mock.Mock()
        bot._ui_owner = mock.Mock()
        bot._ui_watchdog = mock.Mock()
        bot._ui_ingress_stop = threading.Event()
        bot.wx = None
        owner = bot._ui_owner
        watchdog = bot._ui_watchdog

        with mock.patch("wxbot_core.log"):
            self.assertTrue(bot._finish_wxbot_stop())
            self.assertTrue(bot._finish_wxbot_stop())

        self.assertFalse(bot.run_flag)
        self.assertTrue(bot.is_stop_requested())
        bot._cancel_pending_private_message_timers.assert_called_once_with()
        bot._clear_group_message_pipelines.assert_called_once_with()
        bot._clear_chat_memory_background_state.assert_called_once_with()
        bot._reply_delivery_coordinator.stop.assert_called_once_with()
        bot._message_store.cancel_unclaimed_on_shutdown.assert_not_called()
        owner.cancel_pending.assert_called_once_with()
        owner.call_shutdown.assert_called_once_with(wechat_ui_actions.UI_CALL_WAIT_TIMEOUT)
        owner.stop.assert_called_once_with(cancel_pending=True)
        watchdog.stop.assert_called_once_with()
        self.assertTrue(bot._ui_ingress_stop.is_set())

    def test_stop_cleanup_continues_after_one_step_fails(self):
        bot = WXBot.__new__(WXBot)
        bot.run_flag = True
        bot._stop_requested = threading.Event()
        bot._stop_cleanup_lock = threading.Lock()
        bot._stop_cleanup_done = False
        bot._global_scan_stop = threading.Event()
        bot._cancel_pending_private_message_timers = mock.Mock(
            side_effect=RuntimeError("timer cleanup failed")
        )
        bot._clear_group_message_pipelines = mock.Mock()
        bot._clear_chat_memory_background_state = mock.Mock()
        bot._reply_delivery_coordinator = mock.Mock()
        bot._message_store = mock.Mock()
        bot._ui_owner = mock.Mock()

        with self.assertRaisesRegex(RuntimeError, "timer cleanup failed"):
            bot._request_wxbot_stop_cleanup()

        self.assertTrue(bot.is_stop_requested())
        bot._clear_group_message_pipelines.assert_called_once_with()
        bot._clear_chat_memory_background_state.assert_called_once_with()
        bot._reply_delivery_coordinator.stop.assert_called_once_with()
        bot._message_store.cancel_unclaimed_on_shutdown.assert_not_called()
        bot._ui_owner.cancel_pending.assert_called_once_with()
        self.assertFalse(bot._stop_cleanup_done)

    def test_main_always_finishes_after_runtime_exception(self):
        bot = WXBot.__new__(WXBot)
        bot._run_wxbot_main = mock.Mock(side_effect=RuntimeError("main loop failed"))
        bot._finish_wxbot_stop = mock.Mock()
        bot.wx = SimpleNamespace(nickname="bot")
        bot.is_err = mock.Mock()

        with mock.patch("wxbot_core.log"):
            with self.assertRaisesRegex(RuntimeError, "main loop failed"):
                bot.main()

        bot._finish_wxbot_stop.assert_called_once_with()
        bot.is_err.assert_not_called()

    def test_main_always_finishes_after_startup_early_return(self):
        bot = WXBot.__new__(WXBot)
        bot._stop_requested = threading.Event()
        bot._stop_requested.set()
        bot.wxautox_activate_check = mock.Mock(return_value=True)
        bot.init_wx_listeners = mock.Mock()
        bot._notify_startup_status = mock.Mock()
        bot._finish_wxbot_stop = mock.Mock()
        bot.wx = mock.Mock()
        bot.is_err = mock.Mock()

        with mock.patch("wxbot_core.log"):
            self.assertFalse(bot.main())

        bot.wx.StopListening.assert_not_called()
        bot._finish_wxbot_stop.assert_called_once_with()
        bot.is_err.assert_not_called()

    def test_callback_failure_exits_through_unified_stop_cleanup(self):
        bot = WXBot.__new__(WXBot)
        bot._stop_requested = threading.Event()
        bot.wxautox_activate_check = mock.Mock(return_value=True)
        bot.init_wx_listeners = mock.Mock()
        bot._notify_startup_status = mock.Mock()
        bot._finish_wxbot_stop = mock.Mock()
        bot.wx = mock.Mock(nickname="bot")
        bot.callback_is_die = True
        bot.config = SimpleNamespace(new_friend_switch=False, AllListen_switch=False)
        bot._process_listener_auto_recovery = mock.Mock(return_value=None)
        bot._maybe_reconcile_listener_subwindows = mock.Mock()
        bot._process_pending_runtime_task_reload = mock.Mock()
        bot._process_unified_runtime_tasks = mock.Mock()
        bot._process_material_outreach_preface_queue = mock.Mock()
        bot._process_ai_material_outreach_queue = mock.Mock()
        bot._process_ai_material_outreach_detection_scan = mock.Mock()
        bot._check_relationship_auto_scan = mock.Mock()
        bot._check_friend_request_auto_run = mock.Mock()
        bot._contact_directory_auto_maintenance_enabled = mock.Mock(return_value=False)
        bot._wait_or_stop_requested = mock.Mock(return_value=False)
        bot.is_err = mock.Mock()

        with mock.patch("wxbot_core.log"):
            self.assertIsNone(bot.main())

        bot.wx.StopListening.assert_not_called()
        bot._finish_wxbot_stop.assert_called_once_with()
        bot.is_err.assert_not_called()

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

    def test_text_reply_limit_logs_info_when_capacity_is_hit(self):
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
            bot._record_replied_message_success = lambda *_args, **_kwargs: None
            sent = []
            bot._send_private_ai_reply_parts = lambda _chat, parts, **_kwargs: sent.extend(parts) or (True, True)
            chat = SimpleNamespace(who="张三")
            logs = []

            with mock.patch("wxbot_core.log", side_effect=lambda *args, **kwargs: logs.append((kwargs.get("level", "INFO"), kwargs.get("message", "")))):
                handled, result = bot._check_text_reply_limit(chat, "张三")

            self.assertTrue(handled)
            self.assertTrue(result)
            self.assertEqual(sent, ["先休息一下"])
            self.assertTrue(any(level == "INFO" and "触发回复上限" in message for level, message in logs))

    def test_text_reply_limit_info_is_deduped_per_window(self):
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
            bot._record_replied_message_success = lambda *_args, **_kwargs: None
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
            info_logs = [message for level, message in logs if level == "INFO" and "触发回复上限" in message]
            self.assertEqual(len(info_logs), 1)

    def test_text_reply_limit_info_logs_again_for_new_window(self):
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
            bot._record_replied_message_success = lambda *_args, **_kwargs: None
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

            info_logs = [message for level, message in logs if level == "INFO" and "触发回复上限" in message]
            self.assertEqual(len(info_logs), 2)

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
            configure_group_reply_runtime(bot, tmp)
            bot.memory_manager = None
            bot._pause_group_reply = False
            bot.is_stop_requested = lambda: False
            bot._build_prompt_with_context = lambda *_args, **_kwargs: "prompt"
            bot._human_delay_for_reply_part = lambda *_args, **_kwargs: None
            bot._record_replied_message_success = lambda *_args, **_kwargs: None
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

            self.assertTrue(process_persisted_group_message(bot, make_chat("测试群"), make_message("张三")))
            self.assertTrue(process_persisted_group_message(bot, make_chat("测试群"), make_message("李四")))
            self.assertTrue(process_persisted_group_message(bot, make_chat("另一个群"), make_message("张三")))

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

    def test_group_text_reply_limit_treats_stale_as_handled(self):
        bot = WXBot.__new__(WXBot)
        bot.config = SimpleNamespace(group_reply_quote=False, group_reply_at_msg=False)
        bot._deliver_reply_actions = lambda *_args, **_kwargs: DeliveryResult(
            DeliveryStatus.STALE,
            completed=0,
        )

        sent, handled = bot._send_text_reply_limit_parts(
            SimpleNamespace(who="测试群", chat_type="group"),
            SimpleNamespace(sender="张三"),
            ["本轮先聊到这里"],
            chat_type="group",
        )

        self.assertFalse(sent)
        self.assertTrue(handled)

    def test_group_mention_is_removed_as_literal_text(self):
        self.assertEqual(
            strip_group_mention("@研发[一组]+ 请回复", "@研发[一组]+"),
            "请回复",
        )
        self.assertTrue(contains_group_mention("@小帅\u2005请回复", "@小帅"))
        self.assertFalse(contains_group_mention("@小帅哥 请回复", "@小帅"))

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

    def test_group_tts_override_uses_group_binding_and_falls_back_to_default(self):
        bot = WXBot.__new__(WXBot)
        bot.config = SimpleNamespace(
            tts_index=0,
            tts_configs=[{"name": "默认语音"}, {"name": "群聊语音"}],
            group_tts_map={"测试群": 1},
        )

        self.assertEqual(
            bot._active_tts_config("测试群", chat_type="group")["name"],
            "群聊语音",
        )
        self.assertEqual(
            bot._active_tts_config("未配置群", chat_type="group")["name"],
            "默认语音",
        )

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

    def test_keyword_private_reply_sends_text_and_file_actions(self):
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
        with tempfile.TemporaryDirectory() as tmp:
            message = SimpleNamespace(
                type="text",
                attr="friend",
                sender="张三",
                content="关键词",
                id="keyword-1",
            )
            configure_persisted_private_reply(bot, chat, message, tmp)
            image_path = os.path.join(tmp, "a.jpg")
            with open(image_path, "wb") as f:
                f.write(b"img")
            success, result = bot._send_keyword_reply_actions(
                chat,
                [
                    {"type": "text", "content": "关键词回复"},
                    {"type": "file", "path": image_path},
                ],
                message=message,
            )

        self.assertTrue(success)
        self.assertTrue(result)
        self.assertEqual(sent, [("text", "关键词回复"), ("file", image_path)])

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

                with mock.patch("feature.message_routing._bot_log") as bot_log:
                    message_routing.prepare_message_media(bot, msg, chat)

                self.assertTrue(getattr(msg, "_skip_ai_reply", False))
                self.assertEqual(msg.content, "图片")
                level = bot_log.call_args.kwargs.get("level")
                if level is None:
                    level = bot_log.call_args.args[1]
                self.assertEqual(level, "WARNING")

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

        chat = SimpleNamespace(who="张三")
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

        chat = SimpleNamespace(who="张三")
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
        bot._test_message_store_tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(bot._test_message_store_tempdir.cleanup)
        bot._message_store = MessageStore(bot._test_message_store_tempdir.name, "test-account")
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

        chat = SimpleNamespace(who="张三", chat_type="private")

        def reread_after_self(_chat):
            bot._message_store.record_inbound({
                "conversation": "张三",
                "chat_type": "private",
                "direction": "manual_self",
                "sender": "self",
                "content": "我接手",
                "original_content": "我接手",
                "message_type": "text",
                "native_attr": "self",
                "native_id": "manual-1",
                "received_at": time.time(),
                "source": "test",
                "source_batch": "manual-self",
                "source_order": 0,
            })
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

    def test_pending_private_voice_deadline_unblocks_batch_when_voice_stays_unrecognized(self):
        bot = WXBot.__new__(WXBot)
        bot.config = SimpleNamespace(
            memory_switch=False,
            chat_message_merge_delay=1,
            chat_image_recognition_switch=False,
        )
        bot.is_stop_requested = lambda: False
        bot._ensure_message_runtime_state()

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

        chat = SimpleNamespace(who="张三")

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
        bot._schedule_private_message_timer = lambda *_args, **_kwargs: SimpleNamespace(cancel=lambda: None)

        chat = SimpleNamespace(who="张三")
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
        bot._human_delay_for_reply_part = lambda *_args, **_kwargs: None
        bot._record_replied_message_success = lambda *_args, **_kwargs: None

        sent = []
        chat = SimpleNamespace(
            who="张三",
            chat_type="private",
            SendMsg=lambda msg=None, **_kwargs: sent.append(msg) or True,
        )
        message = SimpleNamespace(
            type="text",
            attr="friend",
            sender="张三",
            content="测试",
            id="1",
            time="2026-07-14 23:15:00",
        )

        with tempfile.TemporaryDirectory() as tmp:
            store = configure_group_reply_runtime(bot, tmp)
            persist_private_inbound(store, chat, message)
            with mock.patch("wxbot_core.build_current_turn_user_message", return_value="WRAPPED_CURRENT_TURN") as wrapped:
                self.assertTrue(bot.wx_send_ai(chat, message))

            wrapped.assert_called_once_with(
                "测试",
                message_time="2026-07-14 23:15:00",
            )
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
            bot._human_delay_for_reply_part = lambda *_args, **_kwargs: None
            bot._record_replied_message_success = lambda *_args, **_kwargs: None

            sent = []
            chat = SimpleNamespace(
                who="张三",
                chat_type="private",
                SendMsg=lambda msg=None, **_kwargs: sent.append(msg) or True,
            )
            message = SimpleNamespace(type="text", attr="friend", sender="张三", content="测试", id="1")
            store = configure_persisted_private_reply(bot, chat, message, tmp)

            self.assertTrue(bot.wx_send_ai(chat, message))
            message = SimpleNamespace(type="text", attr="friend", sender="张三", content="测试", id="2")
            persist_private_inbound(store, chat, message)
            self.assertTrue(bot.wx_send_ai(chat, message))
            self.assertEqual(sent, ["换个说法吧"])

            started_at = datetime.now() - timedelta(hours=25)
            bot.reply_count_store.data["users"]["张三"]["window_started_at"] = started_at.isoformat(timespec="seconds")

            message = SimpleNamespace(type="text", attr="friend", sender="张三", content="测试", id="3")
            persist_private_inbound(store, chat, message)
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
        bot._record_replied_message_success = lambda *_args, **_kwargs: None
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
            configure_group_reply_runtime(bot, tmp)
            bot.memory_manager = None
            bot._pause_group_reply = False
            bot.is_stop_requested = lambda: False
            bot._get_group_api = lambda _group: SimpleNamespace(chat=lambda *_args, **_kwargs: "作为AI，我不能这样回复")
            bot._build_prompt_with_context = lambda *_args, **_kwargs: "prompt"
            bot._human_delay_for_reply_part = lambda *_args, **_kwargs: None
            bot._record_replied_message_success = lambda *_args, **_kwargs: None

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

            self.assertTrue(process_persisted_group_message(bot, chat, message))
            self.assertTrue(process_persisted_group_message(bot, chat, message))
            self.assertEqual(sent, [("换个说法吧", None)])

            key = "group:测试群:张三"
            started_at = datetime.now() - timedelta(hours=25)
            bot.reply_count_store.data["users"][key]["window_started_at"] = started_at.isoformat(timespec="seconds")

            self.assertTrue(process_persisted_group_message(bot, chat, message))
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
            configure_group_reply_runtime(bot, tmp)
            bot.memory_manager = None
            bot._pause_group_reply = False
            bot.is_stop_requested = lambda: False
            bot._get_group_api = lambda _group: SimpleNamespace(chat=lambda *_args, **_kwargs: "作为AI，我不能这样回复")
            bot._build_prompt_with_context = lambda *_args, **_kwargs: "prompt"
            bot._human_delay_for_reply_part = lambda *_args, **_kwargs: None
            bot._record_replied_message_success = lambda *_args, **_kwargs: None

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

            self.assertFalse(process_persisted_group_message(bot, chat, message))
            self.assertFalse(process_persisted_group_message(bot, chat, message))
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
        store = configure_group_reply_runtime(bot)
        bot.memory_manager = None
        bot._pause_group_reply = False
        bot.is_stop_requested = lambda: False
        captured_messages = []
        bot._get_group_api = lambda _group: SimpleNamespace(
            chat=lambda message, **_kwargs: captured_messages.append(message) or "群回复"
        )
        bot._build_prompt_with_context = lambda *_args, **_kwargs: "prompt"
        bot._human_delay_for_reply_part = lambda *_args, **_kwargs: None
        bot._record_replied_message_success = lambda *_args, **_kwargs: None

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
            time="2026-07-14 23:15:00",
            quote=lambda text, at=None: sent.append((text, at)) or True,
        )

        with mock.patch("wxbot_core.build_current_turn_user_message", return_value="WRAPPED_GROUP_TURN") as wrapped:
            self.assertTrue(process_persisted_group_message(bot, chat, message))

        wrapped.assert_called_once_with(
            "张三: 测试",
            message_time="2026-07-14 23:15:00",
        )
        self.assertEqual(captured_messages, ["WRAPPED_GROUP_TURN"])
        self.assertEqual(sent, [("群回复", None)])

    def test_group_text_reply_confirms_delivery_in_message_store(self):
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
        configure_group_reply_runtime(bot)
        bot.memory_manager = None
        bot._pause_group_reply = False
        bot.is_stop_requested = lambda: False
        bot._get_group_api = lambda _group: SimpleNamespace(chat=lambda *_args, **_kwargs: "群回复")
        bot._build_prompt_with_context = lambda *_args, **_kwargs: "prompt"
        bot._human_delay_for_reply_part = lambda *_args, **_kwargs: None
        bot._record_replied_message_success = lambda *_args, **_kwargs: None

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
            quote=lambda text, at=None: sent.append((text, at, lock.locked)) or True,
        )

        self.assertTrue(process_persisted_group_message(bot, chat, message))

        self.assertEqual(sent, [("群回复", None)])
        self.assertEqual(
            bot._message_store.delivery_action_status(f"{message._wxbot_reply_turn_id}:0"),
            "done",
        )

    def test_group_sqlite_busy_is_not_converted_to_api_error_reply(self):
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
        configure_group_reply_runtime(bot)
        bot.memory_manager = None
        bot._pause_group_reply = False
        bot.is_stop_requested = lambda: False
        bot._get_group_api = lambda _group: SimpleNamespace(
            chat=lambda *_args, **_kwargs: (_ for _ in ()).throw(
                sqlite3.OperationalError("database is locked")
            )
        )
        bot._build_prompt_with_context = lambda *_args, **_kwargs: "prompt"
        chat = SimpleNamespace(
            who="测试群",
            chat_type="group",
            SendMsg=lambda *_args, **_kwargs: self.fail("数据库锁定时不得发送"),
        )
        message = SimpleNamespace(
            type="text",
            attr="group",
            sender="张三",
            content="测试",
        )

        with self.assertRaisesRegex(sqlite3.OperationalError, "database is locked"):
            process_persisted_group_message(bot, chat, message)

    def test_group_api_error_with_empty_notice_is_handled_silently(self):
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
            api_error_reply="",
            api_error_reply_once=False,
            group_text_reply_limit_hours=24,
            group_voice_reply_switch=False,
            split_long_text=lambda text: [text],
        )
        bot.reply_count_store = ReplyCountStore("")
        configure_group_reply_runtime(bot)
        bot.memory_manager = None
        bot._pause_group_reply = False
        bot.is_stop_requested = lambda: False
        bot._get_group_api = lambda _group: SimpleNamespace(
            chat=lambda *_args, **_kwargs: API_ERROR_REPLY_TEXT
        )
        bot._build_prompt_with_context = lambda *_args, **_kwargs: "prompt"
        chat = SimpleNamespace(
            who="测试群",
            chat_type="group",
            SendMsg=lambda *_args, **_kwargs: self.fail("静默错误回复不应发送"),
        )
        message = SimpleNamespace(
            type="text",
            attr="group",
            sender="张三",
            content="测试",
        )

        self.assertTrue(process_persisted_group_message(bot, chat, message))
        self.assertEqual(
            bot._message_store.get_reply_job(message._wxbot_reply_turn_id)["status"],
            "cancelled",
        )

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
        configure_group_reply_runtime(bot)
        bot.memory_manager = None
        bot._pause_group_reply = False
        bot.is_stop_requested = lambda: False
        replies = iter(["这是一段明显超过限制的回复内容", API_ERROR_REPLY_TEXT])
        bot._get_group_api = lambda _group: SimpleNamespace(chat=lambda *_args, **_kwargs: next(replies))
        bot._build_prompt_with_context = lambda *_args, **_kwargs: "prompt"
        bot._human_delay_for_reply_part = lambda *_args, **_kwargs: None
        bot._record_replied_message_success = lambda *_args, **_kwargs: None
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

        self.assertTrue(process_persisted_group_message(bot, chat, message))
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
            configure_group_reply_runtime(bot, tmp)
            bot.memory_manager = None
            bot._pause_group_reply = False
            bot.is_stop_requested = lambda: False
            bot._get_group_api = lambda _group: SimpleNamespace(chat=lambda *_args, **_kwargs: API_ERROR_REPLY_TEXT)
            bot._build_prompt_with_context = lambda *_args, **_kwargs: "prompt"
            bot._human_delay_for_reply_part = lambda *_args, **_kwargs: None
            bot._record_replied_message_success = lambda *_args, **_kwargs: None

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

            self.assertTrue(process_persisted_group_message(bot, chat, message))
            self.assertTrue(process_persisted_group_message(bot, chat, message))
            self.assertEqual(sent, [("接口忙", None)])

            key = "group:测试群:张三"
            started_at = datetime.now() - timedelta(hours=25)
            bot.reply_count_store.data["users"][key]["window_started_at"] = started_at.isoformat(timespec="seconds")

            self.assertTrue(process_persisted_group_message(bot, chat, message))
            self.assertEqual(sent, [("接口忙", None), ("接口忙", None)])

    def test_group_failed_voice_is_marked_skip_without_fallback_reply(self):
        bot = WXBot.__new__(WXBot)
        bot.config = SimpleNamespace(
            AllListen_switch=False,
            listen_list=[],
            group_switch=True,
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
            group_voice_recognition_switch=True,
        )
        bot = SimpleNamespace(config=config, _pause_group_reply=False)
        chat = SimpleNamespace(who="测试群", chat_type="group")
        message = SimpleNamespace(type="voice", sender="张三", content="普通转写内容")

        self.assertEqual(message_routing.route_process_message(bot, chat, message)["action"], "group_ai")

        config.group_reply_at = True
        self.assertEqual(message_routing.route_process_message(bot, chat, message)["action"], "skip")

        config.group_reply_at = False
        config.group_voice_recognition_switch = False
        self.assertEqual(message_routing.route_process_message(bot, chat, message)["action"], "skip")

    def test_pending_visual_context_reference_intent_is_bilingual(self):
        positives = [
            "看看这是啥意思",
            "帮我看看这票是什么",
            "帮我看看上面那张写的是啥",
            "这张图片里有什么",
            "这发票写的什么",
            "解释一下",
            "帮我看看",
            "what does this mean",
            "explain this",
            "please explain",
            "read the screenshot above",
            "analyze this image",
        ]
        negatives = [
            "你说呢",
            "我看可以",
            "刚才说到哪了",
            "按照这个方案来",
            "这个挺有意思",
            "简单说一下",
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

    def test_group_image_prompt_separates_image_owner_from_asker(self):
        message = build_image_user_message(
            "group",
            sender="群友B",
            attached_text="这是什么？",
            image_count=1,
            image_senders=["群友A"],
            visual_notes=["图片概览：一辆红色汽车。"],
        )

        self.assertIn("群友A发来一张图片", message)
        self.assertIn("群友B说：这是什么？", message)
        self.assertNotIn("群友B发来一张图片", message)

    def test_pending_visual_context_keeps_latest_images_and_conversation_scope(self):
        bot = WXBot.__new__(WXBot)
        bot._chat_merge_lock = threading.Lock()
        bot._pending_visual_contexts = {}
        for index in range(10):
            bot._set_pending_visual_context(
                "同名会话",
                [f"{index}.png"],
                chat_type="group",
                senders=[f"群友{index}"],
                append=True,
            )
        bot._set_pending_visual_context(
            "同名会话",
            ["private.png"],
            chat_type="private",
        )

        group_context = bot._get_pending_visual_context("同名会话", chat_type="group")
        private_context = bot._get_pending_visual_context("同名会话", chat_type="private")
        self.assertEqual(group_context["image_paths"], [f"{index}.png" for index in range(1, 10)])
        self.assertEqual(group_context["image_senders"], [f"群友{index}" for index in range(1, 10)])
        self.assertEqual(private_context["image_paths"], ["private.png"])

    def test_group_visual_context_lives_longer_and_new_batch_replaces_old_images(self):
        bot = WXBot.__new__(WXBot)
        bot._chat_merge_lock = threading.Lock()
        bot._pending_visual_contexts = {}

        with mock.patch("wxbot_core.time.time", return_value=100.0):
            private_context = bot._set_pending_visual_context(
                "同名会话",
                ["private.png"],
                chat_type="private",
            )
            group_context = bot._set_pending_visual_context(
                "同名会话",
                ["group-1.png"],
                chat_type="group",
                senders=["A"],
                append=True,
            )
        self.assertEqual(private_context["expires_at"], 700.0)
        self.assertEqual(group_context["expires_at"], 7300.0)

        with mock.patch("wxbot_core.time.time", return_value=221.0):
            bot._set_pending_visual_context(
                "同名会话",
                ["group-2.png"],
                chat_type="group",
                senders=["B"],
                append=True,
            )
            latest = bot._get_pending_visual_context("同名会话", chat_type="group")
        self.assertEqual(latest["image_paths"], ["group-2.png"])
        self.assertEqual(latest["image_senders"], ["B"])

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
            group_image_recognition_api_id="api_recognition",
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
        configure_group_reply_runtime(bot)
        bot._pause_group_reply = False
        bot.is_stop_requested = lambda: False
        bot._build_prompt_with_context = lambda *_args, **_kwargs: "prompt"
        bot._human_delay_for_reply_part = lambda *_args, **_kwargs: None
        bot._record_replied_message_success = lambda *_args, **_kwargs: None

        image_reply_calls = []
        def fake_group_image_reply(
            _chat,
            _message,
            _history,
            image_paths=None,
            attached_text="",
            image_senders=None,
            message_time="",
        ):
            paths = list(image_paths or [])
            image_reply_calls.append((paths, attached_text, list(image_senders or [])))
            bot._set_pending_visual_context(
                "测试群",
                paths,
                chat_type="group",
                senders=image_senders,
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
        bot._set_pending_visual_context(
            "测试群",
            [r"C:\tmp\a-image.png"],
            chat_type="group",
            senders=["A"],
        )
        message = SimpleNamespace(
            type="text",
            attr="group",
            sender="B",
            content="@机器人 看看这是啥意思",
            quote=lambda text, at=None: sent.append((text, at)) or True,
        )

        self.assertTrue(process_persisted_group_message(bot, chat, message))

        self.assertEqual(
            image_reply_calls,
            [([r"C:\tmp\a-image.png"], "看看这是啥意思", ["A"])],
        )
        self.assertEqual(sent, [("图片答案", None)])
        remaining = bot._get_pending_visual_context("测试群", chat_type="group")
        self.assertEqual(remaining["image_paths"], [r"C:\tmp\a-image.png"])
        self.assertEqual(remaining["image_senders"], ["A"])

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
        configure_group_reply_runtime(bot)
        bot._pause_group_reply = False
        bot.is_stop_requested = lambda: False
        bot._build_prompt_with_context = lambda *_args, **_kwargs: "prompt"
        bot._human_delay_for_reply_part = lambda *_args, **_kwargs: None
        bot._record_replied_message_success = lambda *_args, **_kwargs: None
        bot._reply_group_image_message = lambda *_args, **_kwargs: self.fail("普通 @ 消息不应消费 pending 图片")
        bot._get_group_api = lambda _group: SimpleNamespace(chat=lambda *_args, **_kwargs: "普通回答")

        sent = []
        chat = SimpleNamespace(
            who="测试群",
            chat_type="group",
            SendMsg=lambda msg, at=None: sent.append((msg, at)) or True,
        )
        bot._set_pending_visual_context(
            "测试群",
            [r"C:\tmp\a-image.png"],
            chat_type="group",
            senders=["A"],
        )
        message = SimpleNamespace(
            type="text",
            attr="group",
            sender="B",
            content="@机器人 今天天气怎么样",
            quote=lambda text, at=None: sent.append((text, at)) or True,
        )

        self.assertTrue(process_persisted_group_message(bot, chat, message))

        self.assertEqual(sent, [("普通回答", None)])
        self.assertEqual(
            bot._get_pending_visual_context("测试群", chat_type="group")["image_paths"],
            [r"C:\tmp\a-image.png"],
        )

        def answer_image(
            _chat,
            _message,
            _history,
            image_paths=None,
            attached_text="",
            image_senders=None,
            message_time="",
        ):
            bot._set_pending_visual_context(
                "测试群",
                image_paths,
                chat_type="group",
                senders=image_senders,
                visual_notes=["图片概览：测试图片。"],
            )
            return "图片回答"

        bot._reply_group_image_message = answer_image
        message = SimpleNamespace(
            type="text",
            attr="group",
            sender="C",
            content="@机器人 刚才这张图是什么",
            quote=lambda text, at=None: sent.append((text, at)) or True,
        )

        self.assertTrue(process_persisted_group_message(bot, chat, message))
        self.assertEqual(sent, [("普通回答", None), ("图片回答", None)])
        remaining = bot._get_pending_visual_context("测试群", chat_type="group")
        self.assertEqual(remaining["image_paths"], [r"C:\tmp\a-image.png"])
        self.assertEqual(remaining["image_senders"], ["A"])

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
            bot._get_pending_visual_context("测试群", chat_type="group")["image_paths"],
            [r"C:\tmp\group-image.png"],
        )
        self.assertEqual(
            bot._get_pending_visual_context("测试群", chat_type="group")["visual_notes"],
            [""],
        )

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
            bot._get_pending_visual_context("测试群", chat_type="group")["image_paths"],
            [r"C:\tmp\group-image-1.png", r"C:\tmp\group-image-2.png"],
        )

    def test_pending_group_voice_reread_replaces_group_fifo_head(self):
        bot = WXBot.__new__(WXBot)
        bot._test_message_store_tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(bot._test_message_store_tempdir.cleanup)
        bot._message_store = MessageStore(bot._test_message_store_tempdir.name, "test-account")
        bot._ui_ingress_queue = queue.Queue()
        bot.config = SimpleNamespace(
            memory_switch=False,
            chat_message_merge_delay=20,
            AllListen_switch=False,
            global_blacklist=[],
            group=["测试群"],
            group_switch=True,
            group_image_recognition_switch=False,
            group_voice_recognition_switch=True,
            group_keyword_switch=False,
            group_keyword_at_only=False,
            keyword_dict={},
            group_reply_at=False,
            group_listen_only=False,
        )
        bot._pause_group_reply = False
        bot.is_stop_requested = lambda: False
        bot._ensure_message_runtime_state()
        bot._ui_owner = object()
        bot._schedule_private_message_timer = lambda *_args, **_kwargs: SimpleNamespace(cancel=lambda: None)
        bot._read_pending_voice_snapshot = lambda _name: [MessageEnvelope(
            id="voice-fresh",
            attr="friend",
            sender="群友A",
            type="voice",
            content="识别后的群语音",
        )]
        dispatched = []
        worker_wakeups = []

        def process_group(chat, message):
            dispatched.append((chat.chat_type, message.content, message._wxbot_event_id))
            bot._message_store.mark_inbound_events((message._wxbot_event_id,), "handled")
            return True

        stored = bot._message_store.record_inbound({
            "conversation": "测试群",
            "chat_type": "group",
            "direction": "friend",
            "sender": "群友A",
            "content": '语音2"秒',
            "original_content": '语音2"秒',
            "message_type": "voice",
            "native_attr": "friend",
            "native_id": "voice-original",
            "received_at": time.time(),
            "source": "test",
            "source_batch": "group-voice",
            "source_order": 0,
        })
        message = MessageEnvelope(
            id="voice-original",
            attr="friend",
            sender="群友A",
            type="voice",
            content='语音2"秒',
        )
        message._wxbot_event_id = stored["event_id"]
        message._wxbot_event_ids = (stored["event_id"],)
        message._wxbot_event_version = stored["version"]
        message._wxbot_reply_expires_at = time.time() + 900
        chat = SimpleNamespace(who="测试群", chat_type="group")

        self.assertTrue(bot._queue_pending_private_voice_transcription(chat, message))
        self.assertNotIn("测试群", bot._private_message_pipelines)
        message._skip_ai_reply = True
        self.assertTrue(bot.process_message(chat, message))
        self.assertEqual(
            bot._message_store.get_event(stored["event_id"])["processing_state"],
            "pending",
        )
        bot._group_message_pipelines["测试群"] = {
            "conversation": ConversationRef("测试群", "group"),
            "messages": deque([message]),
            "worker_running": False,
            "retry_timer": None,
        }
        bot._start_group_message_worker_locked = lambda _pipeline: worker_wakeups.append(True)
        bot.process_message = process_group
        self.assertTrue(bot._flush_pending_private_voice_transcription(chat))

        self.assertEqual(dispatched, [])
        self.assertTrue(bot._ui_ingress_queue.empty())
        resolved = bot._group_message_pipelines["测试群"]["messages"][0]
        self.assertTrue(resolved._wxbot_persisted)
        self.assertEqual(worker_wakeups, [True])
        process_group(chat, resolved)
        self.assertEqual(dispatched, [("group", "识别后的群语音", stored["event_id"])])
        self.assertNotIn("测试群", bot._private_message_pipelines)
        event = bot._message_store.get_event(stored["event_id"])
        self.assertEqual(event["content"], "识别后的群语音")
        self.assertEqual(event["processing_state"], "handled")

    def test_group_voice_sqlite_busy_keeps_fifo_head_and_cached_transcription(self):
        bot = WXBot.__new__(WXBot)
        bot._test_message_store_tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(bot._test_message_store_tempdir.cleanup)
        bot._message_store = MessageStore(bot._test_message_store_tempdir.name, "voice-busy")
        bot.config = SimpleNamespace(chat_message_merge_delay=20)
        bot.is_stop_requested = lambda: False
        bot._ensure_message_runtime_state()
        scheduled = []
        bot._schedule_private_message_timer = lambda seconds, callback, chat: (
            scheduled.append((seconds, callback, chat))
            or SimpleNamespace(cancel=lambda: None)
        )
        bot._read_pending_voice_snapshot = lambda _chat: [MessageEnvelope(
            id="voice-fresh",
            attr="friend",
            sender="群友A",
            type="voice",
            content="识别后的群语音",
        )]
        stored = bot._message_store.record_inbound({
            "conversation": "测试群",
            "chat_type": "group",
            "direction": "friend",
            "sender": "群友A",
            "content": '语音2"秒',
            "original_content": '语音2"秒',
            "message_type": "voice",
            "native_attr": "friend",
            "native_id": "voice-busy-original",
            "received_at": time.time(),
            "source": "test",
            "source_batch": "voice-busy",
            "source_order": 0,
        })
        voice = MessageEnvelope(
            id="voice-busy-original",
            attr="friend",
            sender="群友A",
            type="voice",
            content='语音2"秒',
        )
        voice._wxbot_event_id = stored["event_id"]
        voice._wxbot_event_ids = (stored["event_id"],)
        voice._wxbot_event_version = stored["version"]
        voice._wxbot_reply_expires_at = time.time() + 900
        chat = ConversationRef("测试群", "group")
        self.assertTrue(bot._queue_pending_private_voice_transcription(chat, voice))
        later_text = MessageEnvelope(type="text", attr="friend", sender="群友B", content="后来的文字")
        bot._group_message_pipelines["测试群"] = {
            "conversation": chat,
            "messages": deque([voice, later_text]),
            "worker_running": False,
            "retry_timer": None,
        }
        wakeups = []
        bot._start_group_message_worker_locked = lambda _pipeline: wakeups.append(True)

        with mock.patch.object(
            bot._message_store,
            "update_inbound_content",
            side_effect=sqlite3.OperationalError("database is locked"),
        ):
            self.assertTrue(bot._flush_pending_private_voice_transcription(chat))

        pipeline = bot._group_message_pipelines["测试群"]
        self.assertIs(pipeline["messages"][0], voice)
        self.assertIs(pipeline["messages"][1], later_text)
        self.assertEqual(wakeups, [])
        self.assertIn(("group", "测试群"), bot._pending_private_voice_transcription)
        pending_item = next(iter(
            bot._pending_private_voice_transcription[("group", "测试群")]["items"].values()
        ))
        self.assertEqual(pending_item["message"].content, "识别后的群语音")

        self.assertTrue(bot._flush_pending_private_voice_transcription(chat))

        pipeline = bot._group_message_pipelines["测试群"]
        self.assertEqual(
            [message.content for message in pipeline["messages"]],
            ["识别后的群语音", "后来的文字"],
        )
        self.assertEqual(wakeups, [True])

    def test_group_worker_keeps_unresolved_voice_ahead_of_later_text(self):
        bot = WXBot.__new__(WXBot)
        bot._chat_merge_lock = threading.Lock()
        bot._group_message_pipelines = {}
        bot._ui_owner = object()
        bot.is_stop_requested = lambda: False
        conversation = ConversationRef("测试群", "group")
        voice = MessageEnvelope(type="voice", attr="friend", sender="群友A", content='语音2"秒')
        voice._wxbot_pending_voice_key = "voice:测试群:1"
        voice._wxbot_pending_voice_resolved = False
        later_text = MessageEnvelope(type="text", attr="friend", sender="群友B", content="后来的文字")
        bot._group_message_pipelines["测试群"] = {
            "conversation": conversation,
            "messages": deque([voice, later_text]),
            "worker_running": True,
            "retry_timer": None,
        }
        processed = []
        bot.message_handle_callback = lambda message, _chat: processed.append(message.content) or True

        self.assertTrue(bot._run_group_message_pipeline_worker(conversation))
        self.assertEqual(processed, ['语音2"秒'])
        self.assertEqual(
            [message.content for message in bot._group_message_pipelines["测试群"]["messages"]],
            ['语音2"秒', "后来的文字"],
        )

        voice._wxbot_pending_voice_resolved = True
        bot._group_message_pipelines["测试群"]["worker_running"] = True
        self.assertTrue(bot._run_group_message_pipeline_worker(conversation))
        self.assertEqual(processed, ['语音2"秒', '语音2"秒', "后来的文字"])
        self.assertNotIn("测试群", bot._group_message_pipelines)

    def test_expected_delivery_cancellation_is_handled_without_system_error(self):
        for status in (
            DeliveryStatus.DONE,
            DeliveryStatus.STALE,
            DeliveryStatus.CANCELLED,
            DeliveryStatus.EXPIRED,
        ):
            with self.subTest(status=status):
                self.assertTrue(WXBot._delivery_result_is_handled(DeliveryResult(status, 1)))
        self.assertFalse(
            WXBot._delivery_result_is_handled(
                DeliveryResult(DeliveryStatus.UNCERTAIN, 1)
            )
        )

    def test_private_message_pipeline_merges_batch_without_version_cancelling_reply(self):
        bot = WXBot.__new__(WXBot)
        bot.config = SimpleNamespace(chat_message_merge_delay=20)
        bot.is_stop_requested = lambda: False
        sent_to_ai = []
        bot._ui_owner = object()
        bot.wx_send_ai = lambda current_chat, message: sent_to_ai.append((current_chat, message.content)) or True
        chat = ConversationRef("张三")
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

        self.assertEqual([content for _chat, content in sent_to_ai], ["在吗\n我想你", "刚才忘了说"])
        self.assertTrue(all(isinstance(current_chat, OwnedChat) for current_chat, _content in sent_to_ai))
        self.assertTrue(all(current_chat.who == "张三" for current_chat, _content in sent_to_ai))
        self.assertFalse(bot._private_message_pipelines["张三"]["worker_running"])

    def test_private_message_pipeline_rebuilds_owned_chat_before_sending_voice(self):
        bot = WXBot.__new__(WXBot)
        bot.config = SimpleNamespace(chat_message_merge_delay=20)
        bot.is_stop_requested = lambda: False
        intents = []
        bot._ui_owner = SimpleNamespace(
            call=lambda intent, _timeout=None: intents.append(intent) or True,
        )
        bot.wx_send_ai = lambda current_chat, _message: current_chat.SendAudio(filepath="reply.mp3")
        chat = ConversationRef("张三")

        bot._ensure_message_runtime_state()
        with bot._chat_merge_lock:
            pipeline = bot._private_message_pipeline("张三")
            pipeline["queued_batches"].append([
                SimpleNamespace(type="voice", attr="friend", sender="张三", content="识别后的语音", id="1"),
            ])
            pipeline["worker_running"] = True

        self.assertTrue(bot._run_private_message_pipeline_worker(chat))

        self.assertEqual(len(intents), 1)
        self.assertEqual(intents[0].kind, wechat_ui_actions.UIIntentKind.SEND_AUDIO)
        self.assertEqual(intents[0].payload["conversation"], "张三")
        self.assertEqual(intents[0].payload["chat_type"], "private")

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

        pending = bot._get_pending_visual_context("张三", chat_type="private")
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
        bot._ui_owner = object()
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
