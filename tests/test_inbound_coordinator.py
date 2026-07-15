import unittest
from contextlib import contextmanager
from dataclasses import FrozenInstanceError, replace

from core.inbound_coordinator import (
    InboundCoordinator,
    InboundEvent,
    NativeDirectionClassifier,
)
class RecordingStore:
    def __init__(self):
        self.events = []
        self.version = 0

    def record_inbound(self, event):
        self.events.append(event)
        if event.direction != "bot_echo":
            self.version += 1
        return {
            "event_id": f"event-{len(self.events)}",
            "is_new": True,
            "version": self.version,
        }

    @contextmanager
    def inbound_batch(self):
        yield self.record_inbound


def inbound_event(
    *,
    content="你好",
    received_at=10.0,
    source="global",
    source_batch="batch-1",
    source_order=0,
    native_attr="friend",
    native_id="",
    native_hash="",
    native_hash_text="",
    native_time="",
    chat_type="private",
    related_delivery_id="",
):
    return InboundEvent(
        conversation="张三",
        chat_type=chat_type,
        content=content,
        original_content=content,
        message_type="text",
        sender="张三",
        native_attr=native_attr,
        native_id=native_id,
        native_hash=native_hash,
        native_hash_text=native_hash_text,
        native_time=native_time,
        related_delivery_id=related_delivery_id,
        received_at=received_at,
        source=source,
        source_batch=source_batch,
        source_order=source_order,
    )


class InboundCoordinatorTests(unittest.TestCase):
    def test_batch_preserves_order_and_identical_occurrences(self):
        store = RecordingStore()
        coordinator = InboundCoordinator(store)

        accepted = coordinator.accept_batch([
            inbound_event(content="相同", source_order=0),
            inbound_event(content="相同", source_order=1),
            inbound_event(content="第三条", source_order=2),
        ])

        self.assertEqual([item.event.content for item in accepted], ["相同", "相同", "第三条"])
        self.assertEqual([item.version for item in accepted], [1, 2, 3])
        self.assertEqual(len({item.event_id for item in accepted}), 3)

    def test_failed_batch_does_not_advance_seen_or_handoff_state(self):
        class FailingStore(RecordingStore):
            def __init__(self):
                super().__init__()
                self.fail = True

            @contextmanager
            def inbound_batch(self):
                if self.fail:
                    raise RuntimeError("batch failed")
                yield self.record_inbound

        store = FailingStore()
        coordinator = InboundCoordinator(store)
        event = inbound_event(native_id="batch-1")

        with self.assertRaisesRegex(RuntimeError, "batch failed"):
            coordinator.accept_batch([event])

        store.fail = False
        accepted = coordinator.accept_batch([event])
        callback = coordinator.accept(inbound_event(
            source="subwindow",
            source_batch="callback-1",
            received_at=20,
        ))
        self.assertTrue(accepted[0].is_new)
        self.assertEqual(len(store.events), 1)
        self.assertEqual(callback.event_id, accepted[0].event_id)
        self.assertTrue(callback.handoff)

    def test_batch_requires_one_source_identity(self):
        coordinator = InboundCoordinator(RecordingStore())

        with self.assertRaisesRegex(ValueError, "share source and source_batch"):
            coordinator.accept_batch([
                inbound_event(source_batch="batch-1", source_order=0),
                inbound_event(source_batch="batch-2", source_order=1),
            ])

    def test_batch_does_not_hide_conflicting_reuse_of_one_native_id(self):
        coordinator = InboundCoordinator(RecordingStore())

        with self.assertRaisesRegex(ValueError, "native_id was reused"):
            coordinator.accept_batch([
                inbound_event(native_id="same-id", content="one", source_order=0),
                inbound_event(native_id="same-id", content="different", source_order=1),
            ])

    def test_identical_content_occurrences_are_both_persisted(self):
        store = RecordingStore()
        coordinator = InboundCoordinator(store)

        first = coordinator.accept(inbound_event(source_order=0))
        second = coordinator.accept(inbound_event(source_order=1))

        self.assertTrue(first.is_new)
        self.assertTrue(second.is_new)
        self.assertNotEqual(first.event_id, second.event_id)
        self.assertEqual([event.content for event in store.events], ["你好", "你好"])

    def test_two_global_occurrences_handoff_one_to_one_to_subwindow(self):
        store = RecordingStore()
        coordinator = InboundCoordinator(store)
        global_first = coordinator.accept(
            inbound_event(received_at=10.0, source_order=0, native_time="10:00")
        )
        global_second = coordinator.accept(
            inbound_event(received_at=11.0, source_order=1, native_time="10:00")
        )

        subwindow_first = coordinator.accept(
            inbound_event(
                received_at=20.0,
                source="subwindow",
                source_batch="callback-1",
                source_order=0,
                native_time="10:00",
            )
        )
        subwindow_second = coordinator.accept(
            inbound_event(
                received_at=21.0,
                source="subwindow",
                source_batch="callback-1",
                source_order=1,
                native_time="10:00",
            )
        )

        self.assertEqual(len(store.events), 2)
        self.assertEqual(subwindow_first.event_id, global_first.event_id)
        self.assertEqual(subwindow_second.event_id, global_second.event_id)
        self.assertEqual(subwindow_first.event.received_at, 10.0)
        self.assertEqual(subwindow_second.event.received_at, 11.0)
        self.assertTrue(subwindow_first.handoff)
        self.assertTrue(subwindow_second.handoff)
        self.assertFalse(subwindow_first.is_new)
        self.assertFalse(subwindow_second.is_new)

    def test_subwindow_observation_can_handoff_to_later_global_poll(self):
        store = RecordingStore()
        coordinator = InboundCoordinator(store)
        subwindow = coordinator.accept(inbound_event(
            source="subwindow",
            source_batch="callback-1",
            received_at=10.0,
            native_time="10:00",
        ))

        global_poll = coordinator.accept(inbound_event(
            source="global",
            source_batch="poll-1",
            received_at=11.0,
            native_time="10:00",
        ))

        self.assertEqual(len(store.events), 1)
        self.assertEqual(global_poll.event_id, subwindow.event_id)
        self.assertTrue(global_poll.handoff)
        self.assertFalse(global_poll.is_new)

    def test_same_source_occurrences_never_handoff_each_other(self):
        store = RecordingStore()
        coordinator = InboundCoordinator(store)

        first = coordinator.accept(inbound_event(
            source="subwindow",
            source_batch="callback-1",
            received_at=10.0,
        ))
        second = coordinator.accept(inbound_event(
            source="subwindow",
            source_batch="callback-2",
            received_at=11.0,
        ))

        self.assertEqual(len(store.events), 2)
        self.assertNotEqual(first.event_id, second.event_id)

    def test_native_observation_is_recorded_once_per_runtime(self):
        store = RecordingStore()
        coordinator = InboundCoordinator(store)
        first = coordinator.accept(inbound_event(native_id=42))
        repeated = coordinator.accept(
            inbound_event(
                native_id=42,
                source="subwindow",
                source_batch="callback-2",
                received_at=99.0,
            )
        )

        self.assertEqual(len(store.events), 1)
        self.assertEqual(repeated.event_id, first.event_id)
        self.assertEqual(repeated.event.received_at, 10.0)
        self.assertTrue(repeated.duplicate)
        self.assertFalse(repeated.is_new)

    def test_repeated_global_poll_does_not_consume_subwindow_handoff(self):
        store = RecordingStore()
        coordinator = InboundCoordinator(store)
        global_event = inbound_event(native_id=42)

        first = coordinator.accept(global_event)
        repeated_global = coordinator.accept(
            replace(global_event, source_batch="poll-2", received_at=20.0)
        )
        subwindow = coordinator.accept(
            inbound_event(
                source="subwindow",
                source_batch="callback-1",
                received_at=30.0,
            )
        )

        self.assertEqual(len(store.events), 1)
        self.assertEqual(repeated_global.event_id, first.event_id)
        self.assertEqual(subwindow.event_id, first.event_id)
        self.assertTrue(subwindow.handoff)
        self.assertEqual(subwindow.event.received_at, 10.0)

    def test_distinct_native_ids_preserve_identical_content_occurrences(self):
        store = RecordingStore()
        coordinator = InboundCoordinator(store)
        common = inbound_event()

        first = coordinator.accept(
            replace(common, native_id=41, native_hash="same", native_hash_text="same")
        )
        second = coordinator.accept(
            replace(
                common,
                source_order=1,
                native_id=42,
                native_hash="same",
                native_hash_text="same",
            )
        )

        self.assertEqual(len(store.events), 2)
        self.assertNotEqual(first.event_id, second.event_id)

    def test_native_hash_is_not_treated_as_a_unique_observation_id(self):
        store = RecordingStore()
        coordinator = InboundCoordinator(store)

        first = coordinator.accept(inbound_event(native_hash="same", source_order=0))
        second = coordinator.accept(inbound_event(native_hash="same", source_order=1))

        self.assertTrue(first.is_new)
        self.assertTrue(second.is_new)
        self.assertNotEqual(first.event_id, second.event_id)

    def test_stale_handoff_does_not_consume_later_identical_message(self):
        store = RecordingStore()
        coordinator = InboundCoordinator(store, handoff_ttl=10)
        coordinator.accept(inbound_event(source="global", received_at=1))

        later = coordinator.accept(inbound_event(
            source="subwindow",
            source_batch="callback-1",
            received_at=20,
        ))

        self.assertTrue(later.is_new)
        self.assertFalse(later.handoff)

    def test_bot_echo_direction_does_not_advance_store_version(self):
        store = RecordingStore()
        classifier = NativeDirectionClassifier(
            is_bot_echo=lambda event: event.content == "机器人回复"
        )
        coordinator = InboundCoordinator(store, classifier)

        friend = coordinator.accept(inbound_event(content="朋友消息", native_attr="friend"))
        bot_echo = coordinator.accept(
            inbound_event(content="机器人回复", native_attr="self", source_order=1)
        )
        manual_self = coordinator.accept(
            inbound_event(content="人工回复", native_attr="self", source_order=2)
        )

        self.assertEqual(friend.direction, "friend")
        self.assertEqual(bot_echo.direction, "bot_echo")
        self.assertEqual(manual_self.direction, "manual_self")
        self.assertEqual((friend.version, bot_echo.version, manual_self.version), (1, 1, 2))
        self.assertEqual(
            [event.direction for event in store.events],
            ["friend", "bot_echo", "manual_self"],
        )

    def test_classifier_exposes_system_and_unknown_directions(self):
        classifier = NativeDirectionClassifier()

        self.assertEqual(classifier.classify(inbound_event(native_attr="system")), "system")
        self.assertEqual(classifier.classify(inbound_event(native_attr="other")), "unknown")

    def test_related_delivery_id_classifies_self_callback_as_bot_echo(self):
        classifier = NativeDirectionClassifier()

        direction = classifier.classify(inbound_event(
            native_attr="self",
            related_delivery_id="turn-1:0",
        ))

        self.assertEqual(direction, "bot_echo")

    def test_group_member_observation_is_replyable(self):
        classifier = NativeDirectionClassifier()

        direction = classifier.classify(inbound_event(
            chat_type="group",
            native_attr="other",
        ))

        self.assertEqual(direction, "friend")

    def test_received_at_and_source_position_are_immutable_and_preserved(self):
        store = RecordingStore()
        coordinator = InboundCoordinator(store)
        event = inbound_event(
            received_at=123.456,
            source_batch="poll-7",
            source_order=8,
        )

        result = coordinator.accept(event)

        self.assertEqual(result.event.received_at, 123.456)
        self.assertEqual(result.event.source_batch, "poll-7")
        self.assertEqual(result.event.source_order, 8)
        self.assertEqual(store.events[0].received_at, 123.456)
        with self.assertRaises(FrozenInstanceError):
            event.received_at = 999.0


if __name__ == "__main__":
    unittest.main()
