import queue
import sqlite3
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from core import wechat_ui_actions
from core.inbound_coordinator import InboundCoordinator
from core.message_pipeline import ConversationRef, MessageEnvelope
from core.message_store import MessageStore
from core.reply_delivery import (
    DeliveryNotStarted,
    DeliveryStatus,
    ReplyAction,
    ReplyDeliveryCoordinator,
    ReplyEchoTracker,
    ReplyTurn,
)
from core.wechat_ui_runtime import MessageLocateError, WeChatUIRuntime
from wxbot_core import WXBot


class FakeChat:
    def __init__(self, who, send, *, chat_type="private"):
        self.who = who
        self.chat_type = chat_type
        self.SendMsg = send


def make_delivery_bot(data_dir):
    bot = WXBot.__new__(WXBot)
    bot.config = SimpleNamespace(
        chat_split_reply_switch=False,
        chat_split_reply_delay_switch=False,
        group_split_reply_switch=False,
        group_split_reply_delay_switch=False,
        split_long_text=lambda text: [text],
    )
    bot._stop_requested = threading.Event()
    bot._ui_owner = None
    bot._ui_ingress_queue = queue.Queue()
    bot._message_store = MessageStore(data_dir, "wxid_integration")
    bot._inbound_coordinator = InboundCoordinator(bot._message_store)
    bot._reply_echo_tracker = ReplyEchoTracker()
    bot._human_delay_for_reply_part = lambda **_kwargs: None
    bot._reply_delivery_coordinator = ReplyDeliveryCoordinator(
        store=bot._message_store,
        version_provider=lambda conversation, chat_type="private": (
            bot._message_store.conversation_version(conversation, chat_type=chat_type)
        ),
        prepare=bot._prepare_reply_delivery,
        sender=bot._send_reply_delivery,
    )
    return bot


def enqueue_friend(bot, content, native_id):
    message = MessageEnvelope(
        id=native_id,
        type="text",
        attr="friend",
        sender="Alice",
        content=content,
        original_content=content,
        _wxbot_received_at=time.time(),
    )
    bot._enqueue_ui_message(ConversationRef("Alice", "private"), message)
    bot._ui_ingress_queue.get_nowait()
    bot._ui_ingress_queue.task_done()
    return message


class MessageLoopIntegrationTests(unittest.TestCase):
    def test_ai_history_stops_before_current_event(self):
        with tempfile.TemporaryDirectory() as tmp:
            bot = make_delivery_bot(tmp)

            def record(native_id, content):
                return bot._message_store.record_inbound({
                    "conversation": "Team",
                    "chat_type": "group",
                    "direction": "friend",
                    "sender": "Bob",
                    "content": content,
                    "original_content": content,
                    "message_type": "text",
                    "native_attr": "group",
                    "native_id": native_id,
                    "received_at": time.time(),
                    "source": "test",
                    "source_batch": native_id,
                    "source_order": 0,
                })

            record("before", "earlier context")
            current = record("current", "current question")
            record("future", "later correction")

            history = bot._load_chat_history(
                "Team",
                20,
                chat_type="group",
                event_ids=(current["event_id"],),
            )

            self.assertEqual([item["content"] for item in history], ["earlier context"])

    def test_group_visual_note_remains_in_model_history_without_pending_context(self):
        with tempfile.TemporaryDirectory() as tmp:
            bot = make_delivery_bot(tmp)
            bot.config.memory_context_count = 10
            bot.config.memory_max_count = 100
            bot._message_store.record_inbound({
                "conversation": "Team",
                "chat_type": "group",
                "direction": "friend",
                "sender": "群友A",
                "content": "[图片]",
                "original_content": r"C:\tmp\car.png",
                "message_type": "image",
                "native_attr": "group",
                "native_id": "group-image",
                "received_at": time.time(),
                "source": "test",
                "source_batch": "group-image",
                "source_order": 0,
                "metadata": {"image_paths": [r"C:\tmp\car.png"]},
            })
            self.assertTrue(bot._message_store.attach_visual_notes(
                "Team",
                [r"C:\tmp\car.png"],
                [
                    "图片概览：一辆银色汽车。\n"
                    "可见文字：未提取到明确文字。\n"
                    "关键细节：车身为银色。\n"
                    "不确定项：车型无法确认。"
                ],
                chat_type="group",
            ))
            current = bot._message_store.record_inbound({
                "conversation": "Team",
                "chat_type": "group",
                "direction": "friend",
                "sender": "群友B",
                "content": "刚才那辆车是什么颜色？",
                "original_content": "刚才那辆车是什么颜色？",
                "message_type": "text",
                "native_attr": "group",
                "native_id": "group-question-after-image",
                "received_at": time.time() + 1,
                "source": "test",
                "source_batch": "group-question-after-image",
                "source_order": 0,
            })

            history = bot._get_model_context_history(
                "Team",
                event_ids=(current["event_id"],),
                chat_type="group",
            )

            self.assertEqual(len(history), 1)
            self.assertIn("群友A: [图片]", history[0]["content"])
            self.assertIn("一辆银色汽车", history[0]["content"])

    def test_group_pending_image_context_restores_from_sqlite_after_restart(self):
        with tempfile.TemporaryDirectory() as tmp:
            image_path = Path(tmp) / "group-image.png"
            image_path.write_bytes(b"image")
            bot = make_delivery_bot(tmp)
            received_at = time.time() - 60
            bot._message_store.record_inbound({
                "conversation": "Team",
                "chat_type": "group",
                "direction": "friend",
                "sender": "群友A",
                "content": "[图片]",
                "original_content": str(image_path),
                "message_type": "image",
                "native_attr": "group",
                "native_id": "restart-group-image",
                "received_at": received_at,
                "source": "test",
                "source_batch": "restart-group-image",
                "source_order": 0,
                "metadata": {"image_paths": [str(image_path)]},
            })
            bot._chat_merge_lock = threading.Lock()
            bot._pending_visual_contexts = {}

            restored = bot._get_pending_visual_context("Team", chat_type="group")

            self.assertEqual(restored["image_paths"], [str(image_path)])
            self.assertEqual(restored["image_senders"], ["群友A"])
            self.assertAlmostEqual(
                restored["expires_at"],
                received_at + 7200,
                delta=0.1,
            )

            bot._pending_visual_contexts = {}
            with mock.patch("wxbot_core.time.time", return_value=received_at + 7201):
                self.assertIsNone(
                    bot._get_pending_visual_context("Team", chat_type="group")
                )

    def test_first_group_image_is_not_duplicated_by_sqlite_restore_during_append(self):
        with tempfile.TemporaryDirectory() as tmp:
            image_path = Path(tmp) / "first-group-image.png"
            image_path.write_bytes(b"image")
            bot = make_delivery_bot(tmp)
            bot._message_store.record_inbound({
                "conversation": "Team",
                "chat_type": "group",
                "direction": "friend",
                "sender": "群友A",
                "content": "[图片]",
                "original_content": str(image_path),
                "message_type": "image",
                "native_attr": "group",
                "native_id": "first-group-image",
                "received_at": time.time(),
                "source": "test",
                "source_batch": "first-group-image",
                "source_order": 0,
                "metadata": {"image_paths": [str(image_path)]},
            })
            bot._chat_merge_lock = threading.Lock()
            bot._pending_visual_contexts = {}

            context = bot._set_pending_visual_context(
                "Team",
                [str(image_path)],
                chat_type="group",
                senders=["群友A"],
                append=True,
            )

            self.assertEqual(context["image_paths"], [str(image_path)])
            self.assertEqual(context["image_senders"], ["群友A"])

    def test_group_restart_does_not_merge_analyzed_old_batch_into_new_image(self):
        with tempfile.TemporaryDirectory() as tmp:
            old_path = Path(tmp) / "old.png"
            new_path = Path(tmp) / "new.png"
            old_path.write_bytes(b"old")
            new_path.write_bytes(b"new")
            bot = make_delivery_bot(tmp)
            now = time.time()
            bot._message_store.record_inbound({
                "conversation": "Team",
                "chat_type": "group",
                "direction": "friend",
                "sender": "群友A",
                "content": "[图片]",
                "original_content": str(old_path),
                "message_type": "image",
                "native_attr": "group",
                "native_id": "old-group-image",
                "received_at": now - 40,
                "source": "test",
                "source_batch": "old-group-image",
                "source_order": 0,
                "metadata": {
                    "image_paths": [str(old_path)],
                    "visual_notes": ["图片概览：旧图片。"],
                },
            })
            bot._message_store.record_inbound({
                "conversation": "Team",
                "chat_type": "group",
                "direction": "friend",
                "sender": "群友B",
                "content": "[图片]",
                "original_content": str(new_path),
                "message_type": "image",
                "native_attr": "group",
                "native_id": "new-group-image",
                "received_at": now - 20,
                "source": "test",
                "source_batch": "new-group-image",
                "source_order": 0,
                "metadata": {"image_paths": [str(new_path)]},
            })
            bot._chat_merge_lock = threading.Lock()
            bot._pending_visual_contexts = {}

            restored = bot._get_pending_visual_context("Team", chat_type="group")

            self.assertEqual(restored["image_paths"], [str(new_path)])
            self.assertEqual(restored["image_senders"], ["群友B"])

    def test_runtime_restart_recognizes_late_group_echo(self):
        with tempfile.TemporaryDirectory() as tmp:
            first = WXBot.__new__(WXBot)
            first.config = SimpleNamespace(DATA_DIR=tmp)
            first._ui_owner = None
            first._initialize_message_runtime("account")
            first._reply_echo_tracker.reserve(
                "late-group-echo",
                "Team",
                ReplyAction("text", "answer"),
                chat_type="group",
                confirmable=False,
            )
            first._reply_echo_tracker.activate(("late-group-echo",))

            restarted = WXBot.__new__(WXBot)
            restarted.config = SimpleNamespace(DATA_DIR=tmp)
            restarted._ui_owner = None
            restarted._initialize_message_runtime("account")
            echo = MessageEnvelope(
                id="late-native",
                type="text",
                attr="self",
                sender="self",
                content="answer",
                _wxbot_received_at=time.time(),
            )

            self.assertTrue(
                restarted._persist_ui_message(ConversationRef("Team", "group"), echo)
            )

            history = restarted._message_store.history("Team", 10, chat_type="group")
            self.assertEqual([(item["direction"], item["content"]) for item in history], [
                ("bot_echo", "answer"),
            ])
            self.assertEqual(restarted._message_store.conversation_version("Team", chat_type="group"), 0)
            self.assertEqual(restarted._message_store.reply_echo_expectations(), [])

    def test_group_quote_at_callback_is_one_echo_and_does_not_advance_version(self):
        with tempfile.TemporaryDirectory() as tmp:
            bot = make_delivery_bot(tmp)
            bot._reply_echo_tracker = ReplyEchoTracker(store=bot._message_store)
            conversation = ConversationRef("Team", "group")
            inbound = MessageEnvelope(
                id="group-question",
                type="text",
                attr="group",
                sender="群友A",
                content="@机器人 问题",
                original_content="@机器人 问题",
                _wxbot_received_at=time.time(),
            )
            self.assertTrue(bot._persist_ui_message(conversation, inbound))
            version_before_echo = bot._message_store.conversation_version(
                "Team",
                chat_type="group",
            )

            def quote_message(_chat, _message, _content, **kwargs):
                action_ids = tuple(kwargs.get("echo_delivery_ids") or ())
                bot._reply_echo_tracker.activate(action_ids)
                bot._reply_echo_tracker.complete(action_ids)
                return True

            bot._ui_quote_message = quote_message
            chat = FakeChat("Team", lambda **_kwargs: True, chat_type="group")
            delivery = bot._deliver_reply_actions(
                chat,
                inbound,
                (ReplyAction("quote", "机器人回复"),),
                chat_type="group",
                at_first="群友A",
            )
            self.assertEqual(delivery.status, DeliveryStatus.DONE)

            echo = MessageEnvelope(
                id="group-quote-echo",
                type="text",
                attr="self",
                sender="self",
                content="@群友A\u2005机器人回复",
                original_content="@群友A\u2005机器人回复",
                _wxbot_received_at=time.time(),
            )
            self.assertFalse(bot._persist_ui_message(conversation, echo))

            history = bot._message_store.history("Team", 10, chat_type="group")
            self.assertEqual(
                [(item["direction"], item["content"]) for item in history],
                [("friend", "@机器人 问题"), ("bot_echo", "机器人回复")],
            )
            self.assertEqual(
                bot._message_store.conversation_version("Team", chat_type="group"),
                version_before_echo,
            )
            self.assertEqual(bot._message_store.reply_echo_expectations(), [])

    def test_group_version_zero_is_not_replaced_after_manual_takeover(self):
        with tempfile.TemporaryDirectory() as tmp:
            bot = make_delivery_bot(tmp)
            received_at = time.time()
            inbound = bot._message_store.record_inbound({
                "conversation": "Team",
                "chat_type": "group",
                "direction": "friend",
                "sender": "Bob",
                "content": "question",
                "original_content": "question",
                "message_type": "text",
                "native_attr": "group",
                "native_id": "group-zero-1",
                "received_at": received_at,
                "source": "test",
                "source_batch": "group-zero",
                "source_order": 0,
            })
            message = MessageEnvelope(content="question", sender="Bob", attr="group")
            message._wxbot_event_id = inbound["event_id"]
            message._wxbot_event_ids = (inbound["event_id"],)
            message._wxbot_event_version = 0
            message._wxbot_reply_expires_at = received_at + 600
            bot._message_store.record_inbound({
                "conversation": "Team",
                "chat_type": "group",
                "direction": "manual_self",
                "sender": "self",
                "content": "我来处理",
                "original_content": "我来处理",
                "message_type": "text",
                "native_attr": "self",
                "native_id": "group-manual-1",
                "received_at": received_at + 1,
                "source": "test",
                "source_batch": "group-manual",
                "source_order": 0,
            })

            turn_id = bot._ensure_reply_job(
                FakeChat("Team", lambda **_kwargs: True, chat_type="group"),
                message,
                chat_type="group",
            )

            self.assertEqual(bot._message_store.get_reply_job(turn_id)["expected_version"], 0)
            self.assertEqual(
                bot._message_store.mark_reply_job_generating(turn_id),
                "stale",
            )

    def test_no_reply_state_write_propagates_sqlite_busy(self):
        bot = WXBot.__new__(WXBot)
        bot._message_store = SimpleNamespace(
            mark_inbound_events=lambda *_args: (_ for _ in ()).throw(
                sqlite3.OperationalError("database is locked")
            )
        )
        message = SimpleNamespace(_wxbot_event_ids=("event-1",))

        with self.assertRaisesRegex(sqlite3.OperationalError, "database is locked"):
            bot._mark_inbound_no_reply(message)

    def test_stop_before_group_generation_keeps_event_pending_without_creating_job(self):
        with tempfile.TemporaryDirectory() as tmp:
            bot = make_delivery_bot(tmp)
            received_at = time.time()
            stored = bot._message_store.record_inbound({
                "conversation": "Team",
                "chat_type": "group",
                "direction": "friend",
                "sender": "Bob",
                "content": "question",
                "original_content": "question",
                "message_type": "text",
                "native_attr": "group",
                "native_id": "group-stop-1",
                "received_at": received_at,
                "source": "test",
                "source_batch": "group-stop",
                "source_order": 0,
            })
            message = MessageEnvelope(content="question", sender="Bob", attr="group")
            message._wxbot_event_id = stored["event_id"]
            message._wxbot_event_ids = (stored["event_id"],)
            message._wxbot_event_version = 0
            message._wxbot_reply_expires_at = received_at + 600
            bot._stop_requested.set()

            result = bot._reply_job_can_generate(
                FakeChat("Team", lambda **_kwargs: True, chat_type="group"),
                message,
                chat_type="group",
            )

            self.assertFalse(result)
            self.assertEqual(
                bot._message_store.get_event(stored["event_id"])["processing_state"],
                "pending",
            )

    def test_stop_before_group_queue_keeps_event_pending(self):
        with tempfile.TemporaryDirectory() as tmp:
            bot = make_delivery_bot(tmp)
            received_at = time.time()
            bot._message_store.append_inbound_once(
                "group-stop-queue-1",
                "Team",
                content="question",
                received_at=received_at,
                expires_at=received_at + 600,
                chat_type="group",
                message_attr="group",
            )
            event = bot._message_store.get_event("group-stop-queue-1")
            message = MessageEnvelope(content="question", sender="Bob", attr="group")
            message._wxbot_event_id = event["event_id"]
            message._wxbot_event_ids = (event["event_id"],)
            bot._stop_requested.set()

            self.assertTrue(
                bot._enqueue_group_message_for_business(
                    ConversationRef("Team", "group"), message
                )
            )
            self.assertEqual(
                bot._message_store.get_event(event["event_id"])["processing_state"],
                "pending",
            )

    def test_raw_friend_chat_reaches_private_reply_delivery_end_to_end(self):
        with tempfile.TemporaryDirectory() as tmp:
            bot = make_delivery_bot(tmp)
            sent = []
            received = []
            results = []

            def handle(conversation, message):
                received.append((conversation, message))
                results.append(bot._deliver_reply_actions(
                    FakeChat(
                        conversation.who,
                        lambda msg=None, **_kwargs: sent.append(msg) or True,
                    ),
                    message,
                    (ReplyAction("text", "answer"),),
                    chat_type=conversation.chat_type,
                ))

            runtime = WeChatUIRuntime(
                handle,
                persist_message=bot._persist_ui_message,
            )
            runtime._callback(
                SimpleNamespace(
                    id="friend-raw-1",
                    type="text",
                    attr="friend",
                    sender="Alice",
                    content="question",
                ),
                SimpleNamespace(who="Alice", chat_type="friend"),
            )

            conversation, message = received[0]
            stored = bot._message_store.get_event(message._wxbot_event_id)
            self.assertEqual(conversation.chat_type, "private")
            self.assertEqual(stored["chat_type"], "private")
            self.assertEqual(stored["conversation_version"], 1)
            self.assertEqual(results[0].status, DeliveryStatus.DONE)
            self.assertEqual(sent, ["answer"])
            self.assertEqual(
                bot._message_store.delivery_action_status(
                    f"{message._wxbot_reply_turn_id}:0"
                ),
                "done",
            )

    def test_startup_recovery_merges_only_adjacent_unclaimed_private_events(self):
        items = [
            {"job": None, "events": [{"event_seq": 1, "conversation": "Alice", "chat_type": "private"}]},
            {"job": None, "events": [{"event_seq": 2, "conversation": "Alice", "chat_type": "private"}]},
            {"job": {"turn_id": "turn-1"}, "events": [{"event_seq": 3, "conversation": "Alice", "chat_type": "private"}]},
            {"job": None, "events": [{"event_seq": 4, "conversation": "Alice", "chat_type": "private"}]},
            {"job": None, "events": [{"event_seq": 5, "conversation": "Team", "chat_type": "group"}]},
        ]

        recovery = WXBot._coalesce_message_recovery(reversed(items))

        self.assertEqual(
            [[event["event_seq"] for event in item["events"]] for item in recovery],
            [[1, 2], [3], [4], [5]],
        )

    def test_restored_image_with_local_path_does_not_download_again(self):
        message = WXBot._restore_message_envelope({
            "event_id": "event-1",
            "conversation_version": 1,
            "reply_expires_at": 1000,
            "message_type": "image",
            "content": "[图片]",
            "metadata": {"image_paths": ["C:/cached/image.jpg"]},
        })

        self.assertEqual(message.content, "C:/cached/image.jpg")
        self.assertTrue(message._wxbot_media_prepared)

    def test_recovered_message_uses_the_normal_callback_pipeline(self):
        bot = WXBot.__new__(WXBot)
        bot._stop_requested = threading.Event()
        bot._handle_material_source_message = lambda *_args: False
        bot._mark_chat_memory_dirty = mock.Mock()
        bot.process_message = mock.Mock(side_effect=AssertionError("恢复消息不得跳过私聊合并"))
        message = WXBot._restore_message_envelope({
            "event_id": "event-1",
            "conversation_version": 1,
            "reply_expires_at": time.time() + 600,
            "message_type": "text",
            "content": "question",
            "native_attr": "friend",
        })
        chat = SimpleNamespace(who="Alice", chat_type="private")

        with mock.patch("wxbot_core.message_routing.prepare_message_media"), mock.patch(
            "wxbot_core.message_routing.handle_friend_message_callback",
            return_value=True,
        ) as handle:
            self.assertTrue(bot.message_handle_callback(message, chat))

        self.assertFalse(hasattr(message, "_wxbot_startup_recovery"))
        handle.assert_called_once()
        bot.process_message.assert_not_called()

    def test_recovery_merge_does_not_rebind_unreplyable_media(self):
        with tempfile.TemporaryDirectory() as tmp:
            bot = make_delivery_bot(tmp)
            bot._ui_owner = object()
            bot._ensure_message_runtime_state()
            expires_at = time.time() + 600
            bot._message_store.append_inbound_once(
                "image-1",
                "Alice",
                content="[image]",
                message_type="image",
                received_at=time.time(),
                expires_at=expires_at,
            )
            bot._message_store.append_inbound_once(
                "text-1",
                "Alice",
                content="question",
                received_at=time.time(),
                expires_at=expires_at,
            )
            bot._pending_message_recovery = [{
                "job": None,
                "events": [
                    bot._message_store.get_event("image-1"),
                    bot._message_store.get_event("text-1"),
                ],
            }]
            bot._enrich_persisted_ui_message = lambda *_args: True

            def prepare(_bot, message, _chat):
                message._wxbot_media_prepared = True
                if message.type == "image":
                    message._skip_ai_reply = True

            with mock.patch("wxbot_core.message_routing.prepare_message_media", side_effect=prepare):
                recovered = bot._drain_message_recovery()

            _conversation, message = bot._ui_ingress_queue.get_nowait()
            turn_id = bot._ensure_reply_job(
                FakeChat("Alice", lambda **_kwargs: True),
                message,
            )

            self.assertEqual(recovered, 1)
            self.assertEqual(message._wxbot_event_ids, ("text-1",))
            self.assertEqual(bot._message_store.get_reply_job(turn_id)["event_ids"], ["text-1"])
            self.assertEqual(
                bot._message_store.get_event("image-1")["processing_state"],
                "handled",
            )

    def test_recovery_prepares_pending_voice_before_preserving_batch_order(self):
        with tempfile.TemporaryDirectory() as tmp:
            bot = make_delivery_bot(tmp)
            bot._ui_owner = object()
            bot._ensure_message_runtime_state()
            bot._pending_message_recovery = [{
                "job": None,
                "events": [
                    {
                        "event_seq": 1,
                        "event_id": "text-1",
                        "conversation": "Alice",
                        "chat_type": "private",
                        "conversation_version": 1,
                        "reply_expires_at": time.time() + 600,
                        "message_type": "text",
                        "native_attr": "friend",
                        "content": "first text",
                    },
                    {
                        "event_seq": 2,
                        "event_id": "voice-1",
                        "conversation": "Alice",
                        "chat_type": "private",
                        "conversation_version": 2,
                        "reply_expires_at": time.time() + 600,
                        "message_type": "voice",
                        "native_attr": "friend",
                        "content": '语音8"秒',
                    },
                ],
            }]
            bot._enrich_persisted_ui_message = lambda *_args: True

            def prepare(_bot, message, _chat):
                message._wxbot_media_prepared = True
                if message.type != "voice":
                    return
                message._skip_ai_reply = True
                message._wxbot_pending_voice_key = "voice:Alice:1"
                with bot._chat_merge_lock:
                    bot._private_message_pipeline("Alice")["open_messages"].append(message)

            with mock.patch("wxbot_core.message_routing.prepare_message_media", side_effect=prepare):
                recovered = bot._drain_message_recovery()

            pipeline = bot._private_message_pipelines["Alice"]
            self.assertEqual(recovered, 1)
            self.assertEqual(
                [message.content for message in pipeline["open_messages"]],
                ["first text", '语音8"秒'],
            )
            self.assertTrue(bot._ui_ingress_queue.empty())

    def test_global_scan_recovery_waits_for_the_matching_unread_conversation(self):
        with tempfile.TemporaryDirectory() as tmp:
            bot = make_delivery_bot(tmp)
            bot._ui_owner = object()
            received_at = time.time()
            bot._message_store.append_inbound_once(
                "recovery-1",
                "Alice",
                content="earlier question",
                received_at=received_at,
                expires_at=received_at + 600,
            )
            event = bot._message_store.get_event("recovery-1")
            bot._message_store.create_reply_job(
                "recovery-turn",
                conversation="Alice",
                expected_version=bot._message_store.conversation_version("Alice"),
                expires_at=received_at + 600,
                event_ids=[event["event_id"]],
            )
            bot._stage_message_recovery_for_global_scan([{
                "job": bot._message_store.get_reply_job("recovery-turn"),
                "events": [event],
            }])

            with mock.patch("wxbot_core.message_routing.prepare_message_media"):
                self.assertEqual(
                    bot._release_message_recovery_from_global_scan([
                        {"name": "Alice", "chat_type": "private", "new_count": 1}
                    ]),
                    0,
                )
                self.assertTrue(bot._ui_ingress_queue.empty())
                self.assertIsNotNone(bot._message_store.get_reply_job("recovery-turn"))

                self.assertEqual(
                    bot._release_message_recovery_from_global_scan(
                        [{"name": "Alice", "chat_type": "private", "new_count": 1}],
                        ConversationRef("Alice", "private"),
                    ),
                    1,
                )

            conversation, message = bot._ui_ingress_queue.get_nowait()
            self.assertEqual(conversation, ConversationRef("Alice", "private"))
            self.assertEqual(message.content, "earlier question")
            self.assertIsNone(bot._message_store.get_reply_job("recovery-turn"))
            self.assertEqual(
                bot._message_store.get_event(event["event_id"])["processing_state"],
                "pending",
            )
            self.assertEqual(
                bot._release_message_recovery_from_global_scan(
                    (), ConversationRef("Alice", "private")
                ),
                0,
            )

    def test_global_scan_recovery_without_unread_enters_the_normal_queue_immediately(self):
        with tempfile.TemporaryDirectory() as tmp:
            bot = make_delivery_bot(tmp)
            bot._ui_owner = object()
            received_at = time.time()
            bot._message_store.append_inbound_once(
                "recovery-1",
                "Alice",
                content="earlier question",
                received_at=received_at,
                expires_at=received_at + 600,
            )
            event = bot._message_store.get_event("recovery-1")
            bot._stage_message_recovery_for_global_scan([{
                "job": None,
                "events": [event],
            }])

            with mock.patch("wxbot_core.message_routing.prepare_message_media"):
                self.assertEqual(
                    bot._release_message_recovery_from_global_scan([
                        {"name": "Bob", "chat_type": "private", "new_count": 1}
                    ]),
                    1,
                )

            conversation, message = bot._ui_ingress_queue.get_nowait()
            self.assertEqual(conversation, ConversationRef("Alice", "private"))
            self.assertEqual(message.content, "earlier question")

    def test_global_scan_recovery_releases_remaining_items_after_initial_drain(self):
        with tempfile.TemporaryDirectory() as tmp:
            bot = make_delivery_bot(tmp)
            bot._ui_owner = object()
            received_at = time.time()
            bot._message_store.append_inbound_once(
                "recovery-1",
                "Alice",
                content="earlier question",
                received_at=received_at,
                expires_at=received_at + 600,
            )
            event = bot._message_store.get_event("recovery-1")
            bot._stage_message_recovery_for_global_scan([{
                "job": None,
                "events": [event],
            }])

            with mock.patch("wxbot_core.message_routing.prepare_message_media"):
                self.assertEqual(
                    bot._release_message_recovery_from_global_scan([
                        {"name": "Alice", "chat_type": "private", "new_count": 1}
                    ]),
                    0,
                )
                self.assertEqual(
                    bot._release_message_recovery_from_global_scan([
                        {"name": "Alice", "chat_type": "private", "new_count": 1}
                    ], final=True),
                    0,
                )
                self.assertEqual(bot._release_message_recovery_from_global_scan((), final=True), 1)

            conversation, message = bot._ui_ingress_queue.get_nowait()
            self.assertEqual(conversation, ConversationRef("Alice", "private"))
            self.assertEqual(message.content, "earlier question")

    def test_sqlite_busy_retries_in_process_and_sends_once_after_recovery(self):
        with tempfile.TemporaryDirectory() as tmp:
            bot = make_delivery_bot(tmp)
            inbound = enqueue_friend(bot, "question", "friend-1")
            clock = {"now": time.time()}
            inbound._wxbot_reply_expires_at = clock["now"] + 300
            waits = []
            version_checks = []
            sends = []

            def current_version(conversation, chat_type="private"):
                version_checks.append(True)
                if len(version_checks) == 1:
                    raise sqlite3.OperationalError("database is locked")
                return bot._message_store.conversation_version(
                    conversation,
                    chat_type=chat_type,
                )

            def wait(seconds):
                waits.append(seconds)
                clock["now"] += seconds
                return False

            bot._wait_or_stop_requested = wait
            bot._reply_delivery_coordinator = ReplyDeliveryCoordinator(
                store=bot._message_store,
                version_provider=current_version,
                prepare=bot._prepare_reply_delivery,
                sender=bot._send_reply_delivery,
                clock=lambda: clock["now"],
            )

            with mock.patch("wxbot_core.time.time", side_effect=lambda: clock["now"]):
                result = bot._deliver_reply_actions(
                    FakeChat("Alice", lambda msg=None, **_kwargs: sends.append(msg) or True),
                    inbound,
                    (ReplyAction("text", "answer"),),
                )

            self.assertEqual(result.status, DeliveryStatus.DONE)
            self.assertEqual(waits, [30.0])
            self.assertEqual(len(version_checks), 3)
            self.assertEqual(sends, ["answer"])
            self.assertEqual(
                bot._message_store.delivery_action_status(f"{inbound._wxbot_reply_turn_id}:0"),
                "done",
            )

    def test_sqlite_busy_keeps_retrying_only_until_expired(self):
        with tempfile.TemporaryDirectory() as tmp:
            bot = make_delivery_bot(tmp)
            inbound = enqueue_friend(bot, "question", "friend-1")
            clock = {"now": time.time()}
            inbound._wxbot_reply_expires_at = clock["now"] + 95
            waits = []

            def wait(seconds):
                waits.append(seconds)
                clock["now"] += seconds
                return False

            bot._wait_or_stop_requested = wait
            bot._reply_delivery_coordinator = ReplyDeliveryCoordinator(
                store=bot._message_store,
                version_provider=lambda *_args: (
                    (_ for _ in ()).throw(
                        sqlite3.OperationalError("database is locked")
                    )
                ),
                prepare=bot._prepare_reply_delivery,
                sender=bot._send_reply_delivery,
                clock=lambda: clock["now"],
            )

            with mock.patch("wxbot_core.time.time", side_effect=lambda: clock["now"]):
                result = bot._deliver_reply_actions(
                    FakeChat("Alice", lambda **_kwargs: self.fail("expired reply must not send")),
                    inbound,
                    (ReplyAction("text", "answer"),),
                )

            turn_id = inbound._wxbot_reply_turn_id
            self.assertEqual(result.status, DeliveryStatus.EXPIRED)
            self.assertEqual(waits, [30.0, 60.0, 5.0])
            self.assertEqual(bot._message_store.get_reply_job(turn_id)["status"], "expired")
            self.assertEqual(bot._message_store.delivery_action_status(f"{turn_id}:0"), "expired")
            self.assertEqual(
                bot._message_store.get_event(inbound._wxbot_event_id)["processing_state"],
                "handled",
            )

    def test_private_contract_failure_is_cancelled_without_retry(self):
        with tempfile.TemporaryDirectory() as tmp:
            bot = make_delivery_bot(tmp)
            inbound = enqueue_friend(bot, "question", "friend-1")
            scheduled = []
            bot._schedule_private_message_retry = (
                lambda *_args: scheduled.append(True)
            )
            bot.wx_send_ai = lambda *_args: (_ for _ in ()).throw(
                ValueError("invalid conversation type")
            )
            chat = SimpleNamespace(who="Alice")
            bot._ensure_message_runtime_state()
            with bot._chat_merge_lock:
                pipeline = bot._private_message_pipeline("Alice")
                pipeline["queued_batches"].append([inbound])
                pipeline["worker_running"] = True

            with mock.patch("wxbot_core.log") as log_mock:
                self.assertTrue(bot._run_private_message_pipeline_worker(chat))

            self.assertEqual(scheduled, [])
            self.assertEqual(
                bot._message_store.get_event(inbound._wxbot_event_id)["processing_state"],
                "cancelled",
            )
            errors = [
                call for call in log_mock.call_args_list
                if call.kwargs.get("level") == "ERROR"
            ]
            self.assertEqual(len(errors), 1)

    def test_private_sqlite_busy_failure_is_requeued(self):
        with tempfile.TemporaryDirectory() as tmp:
            bot = make_delivery_bot(tmp)
            inbound = enqueue_friend(bot, "question", "friend-1")
            turn_id = bot._ensure_reply_job(
                FakeChat("Alice", lambda **_kwargs: True),
                inbound,
            )
            scheduled = []
            bot._schedule_private_message_retry = (
                lambda chat, messages, delay: scheduled.append((chat, messages, delay))
            )

            def fail(_chat, merged):
                merged._wxbot_reply_turn_id = turn_id
                raise sqlite3.OperationalError("database is locked")

            bot.wx_send_ai = fail
            chat = SimpleNamespace(who="Alice")
            bot._ensure_message_runtime_state()
            with bot._chat_merge_lock:
                pipeline = bot._private_message_pipeline("Alice")
                pipeline["queued_batches"].append([inbound])
                pipeline["worker_running"] = True

            with (
                mock.patch.object(
                    bot._message_store,
                    "get_reply_job",
                    side_effect=sqlite3.OperationalError("database is locked"),
                ),
                mock.patch("wxbot_core.log") as log_mock,
            ):
                self.assertTrue(bot._run_private_message_pipeline_worker(chat))

            self.assertEqual(len(scheduled), 1)
            self.assertEqual(scheduled[0][1], [inbound])
            self.assertEqual(scheduled[0][2], 1.0)
            self.assertEqual(
                bot._message_store.get_reply_job(turn_id)["status"],
                "pending",
            )
            self.assertFalse(any(
                call.kwargs.get("level") == "ERROR"
                for call in log_mock.call_args_list
            ))

    def test_private_nonbusy_sqlite_error_is_not_retried(self):
        with tempfile.TemporaryDirectory() as tmp:
            bot = make_delivery_bot(tmp)
            inbound = enqueue_friend(bot, "question", "friend-1")
            scheduled = []
            bot._schedule_private_message_retry = (
                lambda *_args: scheduled.append(True)
            )
            bot.wx_send_ai = lambda *_args: (_ for _ in ()).throw(
                sqlite3.OperationalError("no such table: reply_jobs")
            )
            chat = SimpleNamespace(who="Alice")
            bot._ensure_message_runtime_state()
            with bot._chat_merge_lock:
                pipeline = bot._private_message_pipeline("Alice")
                pipeline["queued_batches"].append([inbound])
                pipeline["worker_running"] = True

            with mock.patch("wxbot_core.log"):
                self.assertTrue(bot._run_private_message_pipeline_worker(chat))

            self.assertEqual(scheduled, [])
            self.assertEqual(
                bot._message_store.get_event(inbound._wxbot_event_id)["processing_state"],
                "cancelled",
            )

    def test_group_sqlite_busy_failure_retries_inside_group_pipeline(self):
        with tempfile.TemporaryDirectory() as tmp:
            bot = make_delivery_bot(tmp)
            cancelled = []
            bot._cancel_failed_reply_attempt = (
                lambda *_args: cancelled.append(True) or ""
            )
            bot.message_handle_callback = lambda *_args: (_ for _ in ()).throw(
                sqlite3.OperationalError("database is locked")
            )
            message = MessageEnvelope(
                id="group-1",
                type="text",
                attr="group",
                sender="Bob",
                content="question",
                _wxbot_received_at=time.time(),
            )
            conversation = ConversationRef("Team", "group")
            bot._persist_ui_message(conversation, message)
            with mock.patch.object(bot, "_start_group_message_worker_locked"):
                bot._enqueue_group_message_for_business(conversation, message)

            timer = mock.Mock()
            with mock.patch("wxbot_core.threading.Timer", return_value=timer) as timer_type:
                self.assertTrue(bot._run_group_message_pipeline_worker(conversation))

            timer_type.assert_called_once_with(
                1.0,
                bot._resume_group_message_pipeline,
                args=(conversation,),
            )
            timer.start.assert_called_once_with()
            pipeline = bot._group_message_pipelines[conversation.who]
            self.assertIs(pipeline["retry_timer"], timer)
            self.assertFalse(pipeline["worker_running"])
            self.assertIs(pipeline["messages"][0], message)
            self.assertEqual(cancelled, [])

    def test_empty_group_reply_is_silent_and_cancels_the_job(self):
        with tempfile.TemporaryDirectory() as tmp:
            bot = make_delivery_bot(tmp)
            inbound = MessageEnvelope(
                id="group-empty-1",
                type="text",
                attr="group",
                sender="Bob",
                content="question",
                _wxbot_received_at=time.time(),
            )
            bot._enqueue_ui_message(ConversationRef("Team", "group"), inbound)
            bot._ui_ingress_queue.get_nowait()
            bot._ui_ingress_queue.task_done()
            turn_id = bot._ensure_reply_job(
                FakeChat("Team", lambda **_kwargs: self.fail("empty reply must not send")),
                inbound,
                chat_type="group",
            )

            result = bot._deliver_reply_actions(
                FakeChat("Team", lambda **_kwargs: self.fail("empty reply must not send")),
                inbound,
                (),
                chat_type="group",
            )

            self.assertIsNone(result)
            self.assertEqual(bot._message_store.get_reply_job(turn_id)["status"], "cancelled")

    def test_sync_self_callback_confirms_once_before_send_returns(self):
        with tempfile.TemporaryDirectory() as tmp:
            bot = make_delivery_bot(tmp)
            inbound = enqueue_friend(bot, "question", "friend-1")

            def send(msg=None, **_kwargs):
                bot._reply_echo_tracker.activate(_kwargs.get("echo_delivery_ids") or ())
                echo = MessageEnvelope(
                    id="echo-1",
                    type="text",
                    attr="self",
                    sender="self",
                    content=msg,
                    original_content=msg,
                    _wxbot_received_at=time.time(),
                )
                bot._enqueue_ui_message(ConversationRef("Alice", "private"), echo)
                return True

            result = bot._deliver_reply_actions(
                FakeChat("Alice", send),
                inbound,
                (ReplyAction("text", "answer"),),
            )

            self.assertEqual(result.status, DeliveryStatus.DONE)
            self.assertEqual(result.completed, 1)
            self.assertEqual(
                [item["content"] for item in bot._message_store.history("Alice", 10)],
                ["question", "answer"],
            )

    def test_new_private_message_after_first_bubble_cancels_the_remainder(self):
        with tempfile.TemporaryDirectory() as tmp:
            bot = make_delivery_bot(tmp)
            inbound = enqueue_friend(bot, "first question", "friend-1")
            sends = []

            def send(msg=None, **_kwargs):
                sends.append(msg)
                if len(sends) == 1:
                    message = MessageEnvelope(
                        id="friend-2",
                        type="text",
                        attr="friend",
                        sender="Alice",
                        content="new question",
                        original_content="new question",
                        _wxbot_received_at=time.time(),
                    )
                    bot._enqueue_ui_message(ConversationRef("Alice", "private"), message)
                return True

            result = bot._deliver_reply_actions(
                FakeChat("Alice", send),
                inbound,
                (ReplyAction("text", "bubble one"), ReplyAction("text", "bubble two")),
            )

            self.assertEqual(result.status, DeliveryStatus.STALE)
            self.assertEqual(sends, ["bubble one"])

    def test_group_manual_self_after_first_bubble_cancels_the_remainder(self):
        with tempfile.TemporaryDirectory() as tmp:
            bot = make_delivery_bot(tmp)
            conversation = ConversationRef("Team", "group")
            inbound = MessageEnvelope(
                id="group-friend-1",
                type="text",
                attr="group",
                sender="Alice",
                content="group question",
                original_content="group question",
                _wxbot_received_at=time.time(),
            )
            bot._enqueue_ui_message(conversation, inbound)
            bot._ui_ingress_queue.get_nowait()
            bot._ui_ingress_queue.task_done()
            sends = []
            manual = None

            def send(msg=None, **_kwargs):
                nonlocal manual
                sends.append(msg)
                if len(sends) == 1:
                    manual = MessageEnvelope(
                        id="group-self-1",
                        type="text",
                        attr="self",
                        sender="self",
                        content="manual reply",
                        original_content="manual reply",
                        _wxbot_received_at=time.time(),
                    )
                    bot._enqueue_ui_message(conversation, manual)
                return True

            result = bot._deliver_reply_actions(
                FakeChat("Team", send, chat_type="group"),
                inbound,
                (ReplyAction("text", "bubble one"), ReplyAction("text", "bubble two")),
                chat_type="group",
            )

            turn_id = inbound._wxbot_reply_turn_id
            self.assertEqual(result.status, DeliveryStatus.STALE)
            self.assertEqual(sends, ["bubble one"])
            self.assertEqual(bot._message_store.conversation_version("Team", chat_type="group"), 1)
            self.assertEqual(
                bot._message_store.get_event(manual._wxbot_event_id)["direction"],
                "manual_self",
            )
            self.assertEqual(
                [item["status"] for item in bot._message_store.delivery_actions(turn_id)],
                ["done", "stale"],
            )

    def test_not_started_delivery_is_stale_not_uncertain(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = MessageStore(tmp, "wxid_not_started")
            event = store.append_inbound_once(
                "event-1",
                "Alice",
                content="question",
                received_at=100,
                expires_at=1000,
                now=100,
            )
            turn = SimpleNamespace()
            from core.reply_delivery import ReplyTurn

            turn = ReplyTurn(
                turn_id="turn-1",
                conversation="Alice",
                expected_version=1,
                expires_at=1000,
                event_ids=(event["event_id"],),
                actions=(ReplyAction("text", "answer"),),
            )
            coordinator = ReplyDeliveryCoordinator(
                store=store,
                version_provider=lambda *_args: 1,
                prepare=lambda *_args: True,
                sender=lambda *_args: (_ for _ in ()).throw(
                    DeliveryNotStarted(DeliveryStatus.STALE, "owner rejected before send")
                ),
                clock=lambda: 110,
            )

            result = coordinator.deliver(turn)

            self.assertEqual(result.status, DeliveryStatus.STALE)
            self.assertEqual(store.delivery_action_status("turn-1:0"), "stale")
            self.assertEqual(store.get_reply_job("turn-1")["status"], "stale")

    def test_owner_stop_before_handler_releases_the_delivery_claim(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = MessageStore(tmp, "wxid_owner_stop")
            event = store.append_inbound_once(
                "event-1",
                "Alice",
                content="question",
                received_at=100,
                expires_at=1000,
                now=100,
            )
            turn = ReplyTurn(
                turn_id="turn-1",
                conversation="Alice",
                expected_version=1,
                expires_at=1000,
                event_ids=(event["event_id"],),
                actions=(ReplyAction("text", "answer"),),
            )
            coordinator = ReplyDeliveryCoordinator(
                store=store,
                version_provider=lambda *_args: 1,
                prepare=lambda *_args: True,
                sender=lambda *_args: (_ for _ in ()).throw(
                    DeliveryNotStarted(DeliveryStatus.BLOCKED, "owner stopped before handler")
                ),
                clock=lambda: 110,
            )

            result = coordinator.deliver(turn)

            self.assertEqual(result.status, DeliveryStatus.BLOCKED)
            self.assertEqual(store.delivery_action_status("turn-1:0"), "pending")
            self.assertEqual(store.get_reply_job("turn-1")["status"], "pending")

    def test_owner_expiry_rejection_maps_to_not_started_expired(self):
        with tempfile.TemporaryDirectory() as tmp:
            bot = make_delivery_bot(tmp)
            inbound = enqueue_friend(bot, "question", "friend-1")
            inbound._wxbot_reply_expires_at = 100
            action = ReplyAction("text", "answer")
            turn_id = bot._ensure_reply_job(
                FakeChat("Alice", lambda **_kwargs: True),
                inbound,
            )
            turn = ReplyTurn(
                turn_id=turn_id,
                conversation="Alice",
                expected_version=inbound._wxbot_event_version,
                expires_at=100,
                event_ids=inbound._wxbot_event_ids,
                actions=(action,),
            )
            context = {
                "chat": FakeChat(
                    "Alice",
                    lambda **_kwargs: (_ for _ in ()).throw(
                        wechat_ui_actions.IntentCancelled("expired in owner queue")
                    ),
                ),
                "message": inbound,
                "at_first": "",
            }

            with mock.patch("wxbot_core.time.time", return_value=101):
                with self.assertRaises(DeliveryNotStarted) as caught:
                    bot._send_reply_delivery(turn, action, f"{turn_id}:0", context)

            self.assertEqual(caught.exception.status, DeliveryStatus.EXPIRED)

    def test_owner_lock_rejection_maps_to_retryable_not_started_blocked(self):
        with tempfile.TemporaryDirectory() as tmp:
            bot = make_delivery_bot(tmp)
            inbound = enqueue_friend(bot, "question", "friend-lock-wait")
            action = ReplyAction("text", "answer")
            turn_id = bot._ensure_reply_job(FakeChat("Alice", lambda **_kwargs: True), inbound)
            turn = ReplyTurn(
                turn_id=turn_id,
                conversation="Alice",
                expected_version=inbound._wxbot_event_version,
                expires_at=200,
                event_ids=inbound._wxbot_event_ids,
                actions=(action,),
            )
            context = {
                "chat": FakeChat(
                    "Alice",
                    lambda **_kwargs: (_ for _ in ()).throw(
                        wechat_ui_actions.UIActionNotStarted("lock unavailable")
                    ),
                ),
                "message": inbound,
                "at_first": "",
            }

            with self.assertRaises(DeliveryNotStarted) as caught:
                bot._send_reply_delivery(turn, action, f"{turn_id}:0", context)

            self.assertEqual(caught.exception.status, DeliveryStatus.BLOCKED)

    def test_subwindow_failure_before_send_releases_reply_to_pending(self):
        with tempfile.TemporaryDirectory() as tmp:
            bot = make_delivery_bot(tmp)
            inbound = enqueue_friend(bot, "question", "friend-subwindow-wait")
            action = ReplyAction("text", "answer")
            chat = FakeChat(
                "Alice",
                lambda **_kwargs: (_ for _ in ()).throw(
                    wechat_ui_actions.UIOutboundNotStarted("subwindow unavailable")
                ),
            )
            turn = bot._reply_turn(chat, inbound, (action,))
            context = {
                "chat": chat,
                "message": inbound,
                "at_first": "",
                "delayed_action_ids": set(),
            }

            result = bot._reply_delivery_coordinator.deliver(turn, context)

            self.assertEqual(result.status, DeliveryStatus.BLOCKED)
            self.assertEqual(bot._message_store.delivery_action_status(turn.action_id(0)), "pending")
            self.assertEqual(bot._message_store.get_reply_job(turn.turn_id)["status"], "pending")

    def test_quote_location_failure_falls_back_to_plain_text_once(self):
        with tempfile.TemporaryDirectory() as tmp:
            bot = make_delivery_bot(tmp)
            inbound = enqueue_friend(bot, "question", "friend-quote-fallback")
            action = ReplyAction("quote", "answer")
            turn_id = bot._ensure_reply_job(
                FakeChat("Alice", lambda **_kwargs: True),
                inbound,
            )
            from core.reply_delivery import ReplyTurn

            turn = ReplyTurn(
                turn_id=turn_id,
                conversation="Alice",
                expected_version=inbound._wxbot_event_version,
                expires_at=inbound._wxbot_reply_expires_at,
                event_ids=inbound._wxbot_event_ids,
                actions=(action,),
            )
            sent = []
            chat = FakeChat(
                "Alice",
                lambda msg=None, at=None, **_kwargs: sent.append((msg, at)) or True,
            )
            bot._ui_quote_message = lambda *_args, **_kwargs: (_ for _ in ()).throw(
                MessageLocateError("original message is no longer visible")
            )
            context = {
                "chat": chat,
                "message": inbound,
                "at_first": "Bob",
            }

            self.assertTrue(bot._send_reply_delivery(turn, action, f"{turn_id}:0", context))
            self.assertEqual(sent, [("answer", "Bob")])

    def test_owner_activates_then_completes_echo_around_the_handler(self):
        clock = {"now": 0.0}
        tracker = ReplyEchoTracker(ttl=5, clock=lambda: clock["now"])
        tracker.reserve("delivery-1", "Alice", ReplyAction("text", "answer"))
        clock["now"] = 30.0
        active_matches = []
        owner = wechat_ui_actions.WeChatUIOwner(
            {
                wechat_ui_actions.UIIntentKind.SEND_TEXT: lambda _payload: active_matches.append(
                    tracker.match("Alice", "text", "wrong")
                )
            },
            intent_start_callback=lambda intent: tracker.activate(
                intent.payload.get("echo_delivery_ids")
            ),
            intent_finish_callback=lambda intent: tracker.complete(
                intent.payload.get("echo_delivery_ids")
            ),
        )
        owner.start()
        try:
            owner.call(
                wechat_ui_actions.UIIntent(
                    wechat_ui_actions.UIIntentKind.SEND_TEXT,
                    {"echo_delivery_ids": ["delivery-1"]},
                ),
                1,
            )
        finally:
            owner.stop()

        self.assertIsNone(active_matches[0])
        clock["now"] = 34.0
        self.assertIsNotNone(tracker.match("Alice", "text", "answer"))

    def test_send_actions_activates_each_echo_only_when_its_send_starts(self):
        tracker = ReplyEchoTracker()
        for index in range(2):
            tracker.reserve(
                f"batch:{index}",
                "Alice",
                ReplyAction("file", f"file-{index}"),
                confirmable=False,
            )
        matches = []

        class BottomChat:
            who = "Alice"
            _api = SimpleNamespace(HWND=101)

            def SendFiles(self, **_kwargs):
                for _index in range(2):
                    matched = tracker.match("Alice", "file", "[file]")
                    matches.append(matched.action_id if matched is not None else None)
                return True

        class BottomClient:
            def GetSubWindow(self, nickname):
                return BottomChat() if nickname == "Alice" else None

        runtime = WeChatUIRuntime(
            lambda *_args: None,
            echo_action_start=lambda action_id: tracker.activate((action_id,)),
            echo_action_finish=lambda action_id: tracker.complete((action_id,)),
        )
        runtime._client = BottomClient()

        with mock.patch("core.wechat_ui_runtime.win32gui.IsWindow", return_value=True):
            results = runtime.send_actions({
                "conversation": "Alice",
                "chat_type": "private",
                "actions": [
                    {"type": "file", "path": "a.txt", "echo_delivery_id": "batch:0"},
                    {"type": "file", "path": "b.txt", "echo_delivery_id": "batch:1"},
                ],
            })

        self.assertEqual(results, [True, True])
        self.assertEqual(matches, ["batch:0", None, "batch:1", None])

    def test_subwindow_persists_before_media_download(self):
        order = []

        class RawImage:
            type = "image"
            attr = "friend"
            sender = "Alice"
            content = "[image]"
            id = "image-1"

            def download(self):
                order.append("download")
                raise RuntimeError("download failed")

        def persist(_conversation, envelope):
            order.append("persist")
            envelope._wxbot_persisted = True
            return True

        runtime = WeChatUIRuntime(
            lambda _conversation, _envelope: order.append("dispatch"),
            inbound_media_enabled=lambda *_args: True,
            persist_message=persist,
            enrich_message=lambda *_args: order.append("enrich"),
        )
        runtime.set_owner(SimpleNamespace(
            run_callback_action=lambda _intent, method: method(),
        ))

        runtime._callback(RawImage(), SimpleNamespace(who="Alice", chat_type="private"))

        self.assertEqual(order, ["persist", "download", "enrich", "dispatch"])

    def test_global_image_batch_is_persisted_before_later_media_download(self):
        with tempfile.TemporaryDirectory() as tmp:
            bot = make_delivery_bot(tmp)
            facts_seen_during_download = []

            class GlobalClient:
                @staticmethod
                def GetSession():
                    return []

                def GetNextNewMessage(self, filter_mute=False, callback=None):
                    message = SimpleNamespace(
                        id="global-image-1",
                        type="image",
                        attr="friend",
                        sender="Alice",
                        content="[image]",
                        download=self.download,
                    )
                    if callback:
                        callback(message)
                    return {
                        "chat_name": "Alice",
                        "chat_type": "private",
                        "msg": [message],
                    }

                @staticmethod
                def download():
                    facts_seen_during_download.extend(
                        bot._message_store.history("Alice", 10)
                    )
                    return "C:/temp/global.png"

            runtime = WeChatUIRuntime(
                lambda *_args: None,
                persist_message=bot._persist_ui_message,
                enrich_message=bot._enrich_persisted_ui_message,
            )
            runtime._client = GlobalClient()

            batch = runtime.poll_messages({"mode": "next"})
            self.assertEqual(facts_seen_during_download, [])
            accepted = bot._persist_ui_message_batch(
                ConversationRef("Alice", "private"),
                batch["msg"],
            )
            self.assertEqual(len(accepted), 1)
            self.assertEqual(
                bot._message_store.history("Alice", 10)[0]["content"],
                "[图片]",
            )

            batch["msg"][0].content = GlobalClient.download()
            batch["msg"][0]._wxbot_media_prepared = True
            bot._enrich_persisted_ui_message(
                ConversationRef("Alice", "private"),
                batch["msg"][0],
            )
            self.assertEqual(facts_seen_during_download[0]["content"], "[图片]")
            self.assertEqual(batch["msg"][0].content, "C:/temp/global.png")
            stored = bot._message_store.get_event(batch["msg"][0]._wxbot_event_id)
            self.assertEqual(stored["metadata"]["image_paths"], ["C:/temp/global.png"])

    def test_owner_version_change_blocks_text_voice_file_and_quote_handlers(self):
        version = {"Alice": 1}
        blocker_started = threading.Event()
        release_blocker = threading.Event()
        wxauto_calls = []

        class BottomChat:
            who = "Alice"

            def SendMsg(self, *args, **kwargs):
                wxauto_calls.append(("text", args, kwargs))
                return True

            def SendAudio(self, *args, **kwargs):
                wxauto_calls.append(("voice", args, kwargs))
                return True

            def SendFiles(self, *args, **kwargs):
                wxauto_calls.append(("file", args, kwargs))
                return True

        class BottomClient:
            def __init__(self):
                self.chat = BottomChat()

            def GetSubWindow(self, nickname):
                return self.chat if nickname == "Alice" else None

        runtime = WeChatUIRuntime(
            lambda *_args: None,
            client_factory=lambda _version: BottomClient(),
        )
        handlers = runtime.handlers()

        def block(_payload):
            blocker_started.set()
            release_blocker.wait(1)
            return True

        handlers[wechat_ui_actions.UIIntentKind.CONTACT_EDIT] = block
        owner = wechat_ui_actions.WeChatUIOwner(
            handlers,
            conversation_version_provider=lambda conversation, _chat_type: version[conversation],
        )
        owner.start()
        try:
            owner.call(
                wechat_ui_actions.UIIntent(
                    wechat_ui_actions.UIIntentKind.BOOTSTRAP,
                    {"listeners": []},
                ),
                1,
            )
            blocker = owner.submit(
                wechat_ui_actions.UIIntent(wechat_ui_actions.UIIntentKind.CONTACT_EDIT)
            )
            self.assertTrue(blocker_started.wait(1))
            tickets = [
                owner.submit(wechat_ui_actions.UIIntent(
                    kind,
                    {"conversation": "Alice", **payload},
                    conversation_version=1,
                ))
                for kind, payload in (
                    (wechat_ui_actions.UIIntentKind.SEND_TEXT, {"text": "answer"}),
                    (wechat_ui_actions.UIIntentKind.SEND_AUDIO, {"path": "voice.wav"}),
                    (wechat_ui_actions.UIIntentKind.SEND_FILE, {"path": "file.txt"}),
                    (wechat_ui_actions.UIIntentKind.QUOTE, {"text": "quoted answer"}),
                )
            ]
            version["Alice"] = 2
            release_blocker.set()
            blocker.result(1)
            for ticket in tickets:
                with self.assertRaises(wechat_ui_actions.IntentCancelled):
                    ticket.result(1)
        finally:
            release_blocker.set()
            owner.stop()

        self.assertEqual(wxauto_calls, [])

    def test_group_voice_snapshot_lookup_keeps_group_identity(self):
        bot = WXBot.__new__(WXBot)
        calls = []
        group_chat = SimpleNamespace(
            who="同名会话",
            chat_type="group",
            GetAllMessage=lambda: [],
        )
        bot.wx = SimpleNamespace(
            GetSubWindow=lambda nickname, chat_type: calls.append(
                (nickname, chat_type)
            ) or group_chat,
        )

        self.assertEqual(
            bot._read_pending_voice_snapshot(ConversationRef("同名会话", "group")),
            [],
        )
        self.assertEqual(calls, [("同名会话", "group")])

    def test_group_prepare_keeps_group_scope_after_facade_lookup(self):
        with tempfile.TemporaryDirectory() as tmp:
            bot = make_delivery_bot(tmp)
            bot._ui_owner = object()
            bot.wx = SimpleNamespace(
                GetSubWindow=lambda nickname: SimpleNamespace(who=nickname, chat_type="private"),
            )
            from core.reply_delivery import ReplyTurn

            turn = ReplyTurn(
                turn_id="group-turn",
                conversation="Team",
                expected_version=0,
                expires_at=time.time() + 600,
                event_ids=("event-1",),
                actions=(ReplyAction("text", "answer"),),
                chat_type="group",
            )
            context = {
                "chat": SimpleNamespace(who="Team", chat_type="group"),
            }

            self.assertTrue(bot._prepare_reply_delivery(turn, turn.actions[0], "group-turn:0", context))
            self.assertEqual(context["chat"].chat_type, "group")

    def test_group_attr_uses_normal_friend_business_route(self):
        bot = WXBot.__new__(WXBot)
        bot._stop_requested = threading.Event()
        bot.config = SimpleNamespace(group_welcome=False, group=[])
        bot._mark_chat_memory_dirty = lambda *_args, **_kwargs: True
        calls = []
        message = MessageEnvelope(type="text", attr="group", sender="Alice", content="hello")
        message._wxbot_inbound_direction = "friend"
        message._wxbot_event_id = "event-1"
        chat = SimpleNamespace(who="Group", chat_type="group")

        with (
            mock.patch("feature.message_routing.record_runtime_inbound_event"),
            mock.patch("feature.message_routing.prepare_message_media"),
            mock.patch(
                "feature.message_routing.handle_friend_message_callback",
                side_effect=lambda *_args, **_kwargs: calls.append(True) or True,
            ),
            mock.patch("wxbot_core.log"),
        ):
            result = bot.message_handle_callback(message, chat)

        self.assertTrue(result)
        self.assertEqual(calls, [True])


if __name__ == "__main__":
    unittest.main()
