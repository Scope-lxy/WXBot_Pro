import queue
import tempfile
import threading
import time
import unittest
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
from core.wechat_ui_runtime import WeChatUIRuntime
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
                route_source="private_ai",
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

    def test_failed_prepare_retries_in_process_and_sends_once_after_recovery(self):
        with tempfile.TemporaryDirectory() as tmp:
            bot = make_delivery_bot(tmp)
            inbound = enqueue_friend(bot, "question", "friend-1")
            clock = {"now": time.time()}
            inbound._wxbot_reply_expires_at = clock["now"] + 300
            waits = []
            prepares = []
            sends = []

            def prepare(_turn, _action, _action_id, _context):
                prepares.append(True)
                return len(prepares) > 1

            def wait(seconds):
                waits.append(seconds)
                clock["now"] += seconds
                return False

            bot._wait_or_stop_requested = wait
            bot._reply_delivery_coordinator = ReplyDeliveryCoordinator(
                store=bot._message_store,
                version_provider=lambda conversation, chat_type="private": (
                    bot._message_store.conversation_version(
                        conversation,
                        chat_type=chat_type,
                    )
                ),
                prepare=prepare,
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
            self.assertEqual(len(prepares), 2)
            self.assertEqual(sends, ["answer"])
            self.assertEqual(
                bot._message_store.delivery_action_status(f"{inbound._wxbot_reply_turn_id}:0"),
                "done",
            )

    def test_unprepared_turn_keeps_retrying_until_expired(self):
        with tempfile.TemporaryDirectory() as tmp:
            bot = make_delivery_bot(tmp)
            inbound = enqueue_friend(bot, "question", "friend-1")
            clock = {"now": time.time()}
            inbound._wxbot_reply_expires_at = clock["now"] + 95
            waits = []
            prepares = []

            def prepare(*_args):
                prepares.append(True)
                return False

            def wait(seconds):
                waits.append(seconds)
                clock["now"] += seconds
                return False

            bot._wait_or_stop_requested = wait
            bot._reply_delivery_coordinator = ReplyDeliveryCoordinator(
                store=bot._message_store,
                version_provider=lambda conversation, chat_type="private": (
                    bot._message_store.conversation_version(
                        conversation,
                        chat_type=chat_type,
                    )
                ),
                prepare=prepare,
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
            self.assertEqual(len(prepares), 3)
            self.assertEqual(bot._message_store.get_reply_job(turn_id)["status"], "expired")
            self.assertEqual(bot._message_store.delivery_action_status(f"{turn_id}:0"), "expired")
            self.assertEqual(
                bot._message_store.get_event(inbound._wxbot_event_id)["processing_state"],
                "handled",
            )

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

    def test_owner_expiry_rejection_maps_to_not_started_expired(self):
        with tempfile.TemporaryDirectory() as tmp:
            bot = make_delivery_bot(tmp)
            inbound = enqueue_friend(bot, "question", "friend-1")
            inbound._wxbot_reply_expires_at = 100
            action = ReplyAction("text", "answer")
            turn_id = bot._ensure_reply_job(
                FakeChat("Alice", lambda **_kwargs: True),
                inbound,
                route_source="private_ai",
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

        results = runtime.send_actions({
            "conversation": "Alice",
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

    def test_global_image_is_in_sqlite_before_media_download(self):
        with tempfile.TemporaryDirectory() as tmp:
            bot = make_delivery_bot(tmp)
            facts_seen_during_download = []

            class GlobalClient:
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

            batch = runtime.poll_messages({"mode": "next", "download_media": True})

            self.assertEqual(len(facts_seen_during_download), 1)
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
