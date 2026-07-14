import sqlite3
import threading
import unittest
from dataclasses import FrozenInstanceError

from core.reply_delivery import (
    ClaimStatus,
    DeliveryStatus,
    ReplyAction,
    ReplyDeliveryCoordinator,
    ReplyEchoTracker,
    ReplyKind,
    ReplySource,
    ReplyTurn,
)


class FakeStore:
    def __init__(self):
        self.turns = {}
        self.actions = {}
        self.claim_calls = []
        self.finish_calls = []
        self.confirm_calls = []
        self.register_payloads = []

    def register_reply_turn(self, turn_id, **payload):
        self.register_payloads.append(dict(payload))
        previous = self.turns.setdefault(turn_id, dict(payload))
        if previous != payload:
            raise ValueError("turn metadata changed")
        for index in range(payload["action_count"]):
            self.actions.setdefault(f"{turn_id}:{index}", "pending")
        return previous is payload

    def conditional_claim(self, action_id, **conditions):
        self.claim_calls.append((action_id, dict(conditions)))
        status = self.actions[action_id]
        if status == "pending":
            self.actions[action_id] = "inflight"
            return ClaimStatus.CLAIMED
        if status == "done":
            return ClaimStatus.DONE
        return ClaimStatus(status) if status in ClaimStatus._value2member_map_ else ClaimStatus.BLOCKED

    def finish(self, action_id, status, error=""):
        self.finish_calls.append((action_id, status, error))
        self.actions[action_id] = status

    def confirm_outbound(self, action_id, conversation, **payload):
        self.confirm_calls.append((action_id, conversation, dict(payload)))
        self.actions[action_id] = "done"
        return {"action_finished": True}

    def delivery_action_status(self, action_id):
        return self.actions.get(action_id, "")

    def cancel_pending(self, turn_id, status="cancelled", error=""):
        for action_id, current in tuple(self.actions.items()):
            if action_id.startswith(f"{turn_id}:") and current == "pending":
                self.actions[action_id] = status


def make_turn(*actions, version=3, expires_at=200.0):
    return ReplyTurn(
        turn_id="turn-1",
        conversation="contact-1",
        expected_version=version,
        expires_at=expires_at,
        event_ids=("event-1", "event-2"),
        actions=actions or (ReplyAction("text", "hello"),),
    )


def make_coordinator(store, *, versions=None, prepare=None, sender=None, now=None):
    versions = versions or {"contact-1": 3}
    return ReplyDeliveryCoordinator(
        store=store,
        version_provider=lambda conversation, _chat_type: versions[conversation],
        prepare=prepare or (lambda _turn, _action, _action_id, _context: True),
        sender=sender or (lambda _turn, _action, _action_id, _context: True),
        clock=now or (lambda: 100.0),
    )


class ReplyDeliveryTests(unittest.TestCase):
    def test_actions_and_turns_are_immutable_and_have_stable_action_ids(self):
        action = ReplyAction("voice", "audio.wav", "keyword")
        turn = make_turn(action)

        self.assertEqual(action.kind, ReplyKind.VOICE)
        self.assertEqual(action.source, ReplySource.KEYWORD)
        self.assertEqual(turn.action_id(0), "turn-1:0")
        with self.assertRaises(FrozenInstanceError):
            action.content = "changed.wav"
        with self.assertRaises(FrozenInstanceError):
            turn.expected_version = 4

    def test_ai_keyword_error_and_voice_share_one_delivery_path(self):
        store = FakeStore()
        actions = (
            ReplyAction("text", "ai", "ai"),
            ReplyAction("text", "keyword", "keyword"),
            ReplyAction("text", "error", "error"),
            ReplyAction("voice", "voice.wav", "ai"),
        )
        sent = []
        result = make_coordinator(
            store,
            sender=lambda _turn, action, action_id, _context: sent.append(
                (action_id, action.kind.value, action.source.value, action.content)
            ) or True,
        ).deliver(make_turn(*actions))

        self.assertEqual(result.status, DeliveryStatus.DONE)
        self.assertEqual(result.completed, 4)
        self.assertEqual([item[0] for item in sent], [f"turn-1:{index}" for index in range(4)])
        self.assertEqual(set(store.actions.values()), {"done"})

    def test_store_receives_metadata_but_never_generated_content(self):
        store = FakeStore()
        turn = make_turn(ReplyAction("text", "private generated reply"))

        make_coordinator(store).deliver(turn)

        persisted = repr(store.register_payloads) + repr(store.claim_calls)
        self.assertNotIn("private generated reply", persisted)
        self.assertNotIn("content", persisted)
        self.assertEqual(store.register_payloads[0]["event_ids"], ("event-1", "event-2"))
        self.assertEqual(store.confirm_calls[0][2]["content"], "private generated reply")

    def test_prepare_false_cancels_before_claim(self):
        store = FakeStore()
        sent = []
        result = make_coordinator(
            store,
            prepare=lambda _turn, _action, _action_id, _context: False,
            sender=lambda *_args: sent.append(True),
        ).deliver(make_turn())

        self.assertEqual(result.status, DeliveryStatus.CANCELLED)
        self.assertEqual(store.actions["turn-1:0"], "cancelled")
        self.assertEqual(store.claim_calls, [])
        self.assertEqual(sent, [])

    def test_prepare_exception_propagates_instead_of_becoming_retry(self):
        store = FakeStore()

        def fail(*_args):
            raise RuntimeError("window busy")

        with self.assertRaisesRegex(RuntimeError, "window busy"):
            make_coordinator(store, prepare=fail).deliver(make_turn())

        self.assertEqual(store.actions["turn-1:0"], "pending")

    def test_version_provider_exception_propagates_instead_of_becoming_retry(self):
        store = FakeStore()
        coordinator = ReplyDeliveryCoordinator(
            store=store,
            version_provider=lambda *_args: (_ for _ in ()).throw(
                ValueError("invalid conversation type")
            ),
            prepare=lambda *_args: True,
            sender=lambda *_args: True,
            clock=lambda: 100.0,
        )

        with self.assertRaisesRegex(ValueError, "invalid conversation type"):
            coordinator.deliver(make_turn())

        self.assertEqual(store.actions["turn-1:0"], "pending")

    def test_claim_exception_propagates_before_sender_runs(self):
        store = FakeStore()
        sent = []
        store.conditional_claim = lambda *_args, **_kwargs: (_ for _ in ()).throw(
            RuntimeError("claim contract failed")
        )

        with self.assertRaisesRegex(RuntimeError, "claim contract failed"):
            make_coordinator(store, sender=lambda *_args: sent.append(True)).deliver(make_turn())

        self.assertEqual(store.actions["turn-1:0"], "pending")
        self.assertEqual(sent, [])

    def test_sqlite_busy_during_registration_is_retryable(self):
        store = FakeStore()
        store.register_reply_turn = lambda *_args, **_kwargs: (_ for _ in ()).throw(
            sqlite3.OperationalError("database is locked")
        )

        result = make_coordinator(store).deliver(make_turn())

        self.assertEqual(result.status, DeliveryStatus.RETRY)
        self.assertEqual(result.action_id, "turn-1:0")

    def test_sqlite_locked_during_version_check_is_retryable(self):
        store = FakeStore()
        coordinator = ReplyDeliveryCoordinator(
            store=store,
            version_provider=lambda *_args: (_ for _ in ()).throw(
                sqlite3.OperationalError("database table is locked")
            ),
            prepare=lambda *_args: True,
            sender=lambda *_args: True,
            clock=lambda: 100.0,
        )

        result = coordinator.deliver(make_turn())

        self.assertEqual(result.status, DeliveryStatus.RETRY)
        self.assertEqual(store.actions["turn-1:0"], "pending")

    def test_sqlite_busy_during_claim_is_retryable(self):
        store = FakeStore()
        store.conditional_claim = lambda *_args, **_kwargs: (_ for _ in ()).throw(
            sqlite3.OperationalError("database is busy")
        )

        result = make_coordinator(store).deliver(make_turn())

        self.assertEqual(result.status, DeliveryStatus.RETRY)
        self.assertEqual(store.actions["turn-1:0"], "pending")

    def test_version_is_checked_again_after_window_preparation(self):
        store = FakeStore()
        versions = {"contact-1": 3}

        def prepare(_turn, _action, _action_id, _context):
            versions["contact-1"] = 4
            return True

        result = make_coordinator(store, versions=versions, prepare=prepare).deliver(make_turn())

        self.assertEqual(result.status, DeliveryStatus.STALE)
        self.assertEqual(store.actions["turn-1:0"], "stale")
        self.assertEqual(store.claim_calls, [])

    def test_expired_turn_never_prepares_claims_or_sends(self):
        store = FakeStore()
        prepared = []
        sent = []
        result = make_coordinator(
            store,
            prepare=lambda *_args: prepared.append(True),
            sender=lambda *_args: sent.append(True),
        ).deliver(make_turn(expires_at=100.0))

        self.assertEqual(result.status, DeliveryStatus.EXPIRED)
        self.assertEqual(store.actions["turn-1:0"], "expired")
        self.assertEqual(prepared, [])
        self.assertEqual(sent, [])

    def test_each_bubble_rechecks_version_and_cancels_unsent_remainder(self):
        store = FakeStore()
        versions = {"contact-1": 3}

        def send(_turn, _action, action_id, _context):
            if action_id == "turn-1:0":
                versions["contact-1"] = 4
            return True

        result = make_coordinator(store, versions=versions, sender=send).deliver(
            make_turn(ReplyAction("text", "first"), ReplyAction("text", "second"))
        )

        self.assertEqual(result.status, DeliveryStatus.STALE)
        self.assertEqual(result.completed, 1)
        self.assertEqual(store.actions, {"turn-1:0": "done", "turn-1:1": "stale"})

    def test_sender_exception_freezes_claimed_action_and_cancels_remainder(self):
        store = FakeStore()

        def fail(*_args):
            raise RuntimeError("response lost")

        result = make_coordinator(store, sender=fail).deliver(
            make_turn(ReplyAction("text", "first"), ReplyAction("text", "second"))
        )

        self.assertEqual(result.status, DeliveryStatus.UNCERTAIN)
        self.assertEqual(store.actions, {"turn-1:0": "uncertain", "turn-1:1": "cancelled"})
        self.assertEqual([item[0] for item in store.claim_calls], ["turn-1:0"])

    def test_false_sender_result_is_uncertain_not_retryable(self):
        for false_result in (False, None, {}, []):
            with self.subTest(false_result=false_result):
                store = FakeStore()
                result = make_coordinator(store, sender=lambda *_args: false_result).deliver(make_turn())

                self.assertEqual(result.status, DeliveryStatus.UNCERTAIN)
                self.assertEqual(store.actions["turn-1:0"], "uncertain")

    def test_sender_result_truth_check_exception_is_uncertain(self):
        class BrokenResult:
            def __bool__(self):
                raise RuntimeError("result unreadable")

        store = FakeStore()
        result = make_coordinator(store, sender=lambda *_args: BrokenResult()).deliver(make_turn())

        self.assertEqual(result.status, DeliveryStatus.UNCERTAIN)
        self.assertEqual(result.error, "result unreadable")
        self.assertEqual(store.actions["turn-1:0"], "uncertain")

    def test_sqlite_busy_can_resume_after_an_already_done_bubble(self):
        store = FakeStore()
        attempts = {"second": 0}
        claim = store.conditional_claim

        def conditional_claim(action_id, **conditions):
            if action_id == "turn-1:1":
                attempts["second"] += 1
                if attempts["second"] == 1:
                    raise sqlite3.OperationalError("database is locked")
            return claim(action_id, **conditions)

        store.conditional_claim = conditional_claim
        coordinator = make_coordinator(store)
        turn = make_turn(ReplyAction("text", "first"), ReplyAction("text", "second"))

        first = coordinator.deliver(turn)
        second = coordinator.deliver(turn)

        self.assertEqual(first.status, DeliveryStatus.RETRY)
        self.assertEqual(first.completed, 1)
        self.assertEqual(second.status, DeliveryStatus.DONE)
        self.assertEqual(second.completed, 2)
        self.assertEqual(store.actions, {"turn-1:0": "done", "turn-1:1": "done"})

    def test_conditional_claim_rejection_never_calls_sender(self):
        store = FakeStore()
        store.conditional_claim = lambda *_args, **_kwargs: ClaimStatus.BLOCKED
        sent = []

        result = make_coordinator(store, sender=lambda *_args: sent.append(True)).deliver(make_turn())

        self.assertEqual(result.status, DeliveryStatus.BLOCKED)
        self.assertEqual(sent, [])

    def test_explicit_cancel_is_clean_and_idempotent(self):
        store = FakeStore()
        coordinator = make_coordinator(store)
        turn = make_turn()
        store.register_reply_turn(
            turn.turn_id,
            conversation=turn.conversation,
            expected_version=turn.expected_version,
            expires_at=turn.expires_at,
            event_ids=turn.event_ids,
            action_count=len(turn.actions),
            chat_type=turn.chat_type,
        )

        coordinator.cancel(turn.turn_id)
        coordinator.cancel(turn.turn_id)
        result = coordinator.deliver(turn)

        self.assertEqual(result.status, DeliveryStatus.CANCELLED)
        self.assertEqual(store.actions["turn-1:0"], "cancelled")

    def test_stop_during_send_finishes_current_bubble_and_cancels_remainder(self):
        store = FakeStore()
        coordinator = None

        def send(_turn, _action, action_id, _context):
            if action_id == "turn-1:0":
                coordinator.stop()
            return True

        coordinator = make_coordinator(store, sender=send)
        result = coordinator.deliver(
            make_turn(ReplyAction("text", "first"), ReplyAction("text", "second"))
        )

        self.assertEqual(result.status, DeliveryStatus.CANCELLED)
        self.assertEqual(result.completed, 1)
        self.assertEqual(store.actions, {"turn-1:0": "done", "turn-1:1": "cancelled"})

    def test_echo_tracker_consumes_only_the_matching_conversation_and_content(self):
        now = {"value": 10.0}
        tracker = ReplyEchoTracker(clock=lambda: now["value"])
        action = ReplyAction("text", "hello")
        tracker.reserve("turn-1:0", "Alice", action)
        tracker.activate(("turn-1:0",))

        self.assertIsNone(tracker.match("Bob", "text", "hello"))
        self.assertIsNone(tracker.match("Alice", "text", "different"))
        matched = tracker.match("Alice", "text", "hello")

        self.assertEqual(matched.action_id, "turn-1:0")
        self.assertIsNone(tracker.match("Alice", "text", "hello"))

    def test_echo_tracker_is_inactive_while_reserved(self):
        now = {"value": 10.0}
        tracker = ReplyEchoTracker(ttl=5, clock=lambda: now["value"])
        tracker.reserve("turn-1:0", "Alice", ReplyAction("text", "same"))

        self.assertIsNone(tracker.match("Alice", "text", "same"))

    def test_echo_tracker_does_not_expire_during_a_slow_send(self):
        now = {"value": 10.0}
        tracker = ReplyEchoTracker(ttl=5, clock=lambda: now["value"])
        tracker.reserve("turn-1:0", "Alice", ReplyAction("voice", "[语音]"))
        tracker.activate(("turn-1:0",))

        now["value"] = 41.0

        self.assertIsNotNone(tracker.match("Alice", "voice", '语音31"秒'))

    def test_echo_tracker_expires_after_the_post_send_grace_period(self):
        now = {"value": 10.0}
        tracker = ReplyEchoTracker(ttl=60, clock=lambda: now["value"])
        tracker.reserve("turn-1:0", "Alice", ReplyAction("text", "same"))
        tracker.activate(("turn-1:0",))
        tracker.complete(("turn-1:0",))

        now["value"] = 71.0

        self.assertIsNone(tracker.match("Alice", "text", "same"))

    def test_material_echo_can_match_its_exact_native_type(self):
        tracker = ReplyEchoTracker()
        tracker.reserve(
            "forward-1",
            "Alice",
            ReplyAction("file", "miniapp material"),
            message_types=("miniapp",),
        )
        tracker.activate(("forward-1",))

        self.assertIsNone(tracker.match("Alice", "link", "miniapp material"))
        self.assertIsNotNone(tracker.match("Alice", "miniapp", "miniapp material"))

    def test_same_type_material_echo_prefers_matching_content(self):
        tracker = ReplyEchoTracker()
        tracker.reserve(
            "forward-1",
            "Alice",
            ReplyAction("file", "first material"),
            message_types=("miniapp",),
        )
        tracker.reserve(
            "forward-2",
            "Alice",
            ReplyAction("file", "second material"),
            message_types=("miniapp",),
        )
        tracker.activate(("forward-1", "forward-2"))

        second = tracker.match("Alice", "miniapp", "second material")
        first = tracker.match("Alice", "miniapp", "first material")

        self.assertEqual(second.action_id, "forward-2")
        self.assertEqual(first.action_id, "forward-1")

    def test_echo_tracker_never_matches_same_named_private_and_group_chats(self):
        tracker = ReplyEchoTracker()
        tracker.reserve(
            "turn-1:0",
            "同名会话",
            ReplyAction("text", "reply"),
            chat_type="group",
        )
        tracker.activate(("turn-1:0",))

        self.assertIsNone(
            tracker.match("同名会话", "text", "reply", chat_type="private")
        )
        self.assertIsNotNone(
            tracker.match("同名会话", "text", "reply", chat_type="group")
        )

    def test_stop_preserves_a_turn_cancelled_during_prepare(self):
        store = FakeStore()
        coordinator = make_coordinator(store, prepare=lambda *_args: False)

        result = coordinator.deliver(make_turn())
        coordinator.stop()

        self.assertEqual(result.status, DeliveryStatus.CANCELLED)
        self.assertEqual(store.actions["turn-1:0"], "cancelled")

    def test_stop_can_cancel_an_active_turn_from_another_thread(self):
        store = FakeStore()
        entered = threading.Event()
        release = threading.Event()

        def send(_turn, _action, _action_id, _context):
            entered.set()
            release.wait(1)
            return True

        coordinator = make_coordinator(store, sender=send)
        turn = make_turn(ReplyAction("text", "first"), ReplyAction("text", "second"))
        results = []
        thread = threading.Thread(target=lambda: results.append(coordinator.deliver(turn)))
        thread.start()
        self.assertTrue(entered.wait(1))

        coordinator.stop()
        release.set()
        thread.join(1)

        self.assertFalse(thread.is_alive())
        self.assertEqual(results[0].status, DeliveryStatus.CANCELLED)
        self.assertEqual(store.actions, {"turn-1:0": "done", "turn-1:1": "cancelled"})


if __name__ == "__main__":
    unittest.main()
