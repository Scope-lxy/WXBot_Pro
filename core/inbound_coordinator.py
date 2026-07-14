"""Pure-data primitives for accepting inbound WeChat observations once."""

from __future__ import annotations

from collections import OrderedDict
from dataclasses import dataclass, replace
from threading import RLock
from typing import Callable, Literal, Mapping, Protocol, TypeAlias


InboundDirection: TypeAlias = Literal[
    "friend",
    "manual_self",
    "bot_echo",
    "system",
    "unknown",
]


@dataclass(frozen=True, slots=True)
class InboundEvent:
    """One immutable, pure-data observation copied at the wxauto boundary."""

    conversation: str
    content: str
    received_at: float
    source: str
    source_batch: str
    source_order: int
    chat_type: str = "private"
    original_content: str = ""
    message_type: str = ""
    sender: str = ""
    native_attr: str = ""
    native_id: str | int = ""
    native_hash: str = ""
    native_hash_text: str = ""
    native_time: str = ""
    related_delivery_id: str = ""
    image_paths: tuple[str, ...] = ()
    visual_notes: tuple[str, ...] = ()
    direction: InboundDirection = "unknown"


@dataclass(frozen=True, slots=True)
class InboundAcceptResult:
    """The persisted identity and routing decision for one observation."""

    event: InboundEvent
    event_id: str
    is_new: bool
    version: int
    duplicate: bool = False
    handoff: bool = False

    @property
    def direction(self) -> InboundDirection:
        return self.event.direction


class InboundStore(Protocol):
    def record_inbound(self, event: InboundEvent) -> Mapping[str, object]:
        """Persist ``event`` once and return event_id, is_new, and version."""


class DirectionClassifier(Protocol):
    def classify(self, event: InboundEvent) -> InboundDirection:
        """Classify an observation before it reaches the store."""


BotEchoMatcher: TypeAlias = Callable[[InboundEvent], bool]


class NativeDirectionClassifier:
    """Classify wxauto's scalar ``attr`` plus an injected bot-echo matcher."""

    def __init__(self, is_bot_echo: BotEchoMatcher | None = None):
        self._is_bot_echo = is_bot_echo

    def classify(self, event: InboundEvent) -> InboundDirection:
        native_attr = str(event.native_attr or "").strip().lower()
        chat_type = str(event.chat_type or "private").strip().lower()
        if native_attr == "friend" or (
            chat_type == "group" and native_attr not in {"self", "system"}
        ):
            return "friend"
        if native_attr == "system":
            return "system"
        if native_attr == "self":
            if event.related_delivery_id:
                return "bot_echo"
            if self._is_bot_echo is not None and self._is_bot_echo(event):
                return "bot_echo"
            return "manual_self"
        return "unknown"


@dataclass(frozen=True, slots=True)
class _PendingHandoff:
    signature: tuple[str, ...]
    result: InboundAcceptResult


class InboundCoordinator:
    """Serialize, classify, de-duplicate, and persist inbound observations.

    Global-list observations are retained as ordered occurrences. A later
    subwindow observation consumes exactly one matching occurrence, so repeated
    messages with identical content remain distinct.
    """

    def __init__(
        self,
        store: InboundStore,
        classifier: DirectionClassifier | None = None,
        *,
        runtime_observation_limit: int = 4096,
        pending_handoff_limit: int = 2048,
        handoff_ttl: float = 120.0,
    ):
        self._store = store
        self._classifier = classifier or NativeDirectionClassifier()
        self._runtime_observation_limit = max(1, int(runtime_observation_limit))
        self._pending_handoff_limit = max(1, int(pending_handoff_limit))
        self._handoff_ttl = max(1.0, float(handoff_ttl))
        self._seen: OrderedDict[tuple[str, ...], InboundAcceptResult] = OrderedDict()
        self._handoffs: OrderedDict[int, _PendingHandoff] = OrderedDict()
        self._next_handoff_id = 0
        self._lock = RLock()

    def accept(self, event: InboundEvent) -> InboundAcceptResult:
        """Accept one observation, returning ``is_new=False`` for duplicates."""
        if not isinstance(event, InboundEvent):
            raise TypeError("event must be an InboundEvent")

        with self._lock:
            observation_keys = self._observation_keys(event)
            seen = self._find_seen(observation_keys)
            if seen is not None:
                if event.source == "subwindow":
                    self._consume_handoff(seen)
                return self._duplicate(seen)

            if event.source == "subwindow":
                handed_off = self._take_handoff(event)
                if handed_off is not None:
                    self._remember(observation_keys, handed_off)
                    return self._duplicate(handed_off, handoff=True)

            direction = self._classifier.classify(event)
            if direction not in {
                "friend",
                "manual_self",
                "bot_echo",
                "system",
                "unknown",
            }:
                raise ValueError(f"unsupported inbound direction: {direction!r}")

            classified_event = replace(event, direction=direction)
            stored = self._store.record_inbound(classified_event)
            result = InboundAcceptResult(
                event=classified_event,
                event_id=str(stored["event_id"]),
                is_new=bool(stored["is_new"]),
                version=int(stored["version"]),
            )
            self._remember(observation_keys, result)
            if event.source == "global":
                self._remember_handoff(result)
            return result

    @staticmethod
    def _observation_keys(event: InboundEvent) -> tuple[tuple[str, ...], ...]:
        prefix = (event.chat_type, event.conversation)
        if event.native_id not in {None, ""}:
            return ((*prefix, "id", str(event.native_id)),)
        return ((
            *prefix,
            "source",
            str(event.source),
            str(event.source_batch),
            str(event.source_order),
        ),)

    @staticmethod
    def _occurrence_signature(event: InboundEvent) -> tuple[str, ...]:
        original = event.original_content if event.original_content != "" else event.content
        return (
            str(event.chat_type),
            str(event.conversation),
            str(event.sender),
            str(event.message_type),
            str(event.native_attr),
            str(original),
        )

    def _find_seen(
        self,
        keys: tuple[tuple[str, ...], ...],
    ) -> InboundAcceptResult | None:
        for key in keys:
            result = self._seen.get(key)
            if result is not None:
                self._seen.move_to_end(key)
                return result
        return None

    def _remember(
        self,
        keys: tuple[tuple[str, ...], ...],
        result: InboundAcceptResult,
    ) -> None:
        for key in keys:
            self._seen[key] = result
            self._seen.move_to_end(key)
        while len(self._seen) > self._runtime_observation_limit:
            self._seen.popitem(last=False)

    def _remember_handoff(self, result: InboundAcceptResult) -> None:
        self._next_handoff_id += 1
        self._handoffs[self._next_handoff_id] = _PendingHandoff(
            signature=self._occurrence_signature(result.event),
            result=result,
        )
        while len(self._handoffs) > self._pending_handoff_limit:
            self._handoffs.popitem(last=False)

    def _take_handoff(self, event: InboundEvent) -> InboundAcceptResult | None:
        signature = self._occurrence_signature(event)
        stale_ids = []
        for handoff_id, pending in self._handoffs.items():
            age = float(event.received_at) - float(pending.result.event.received_at)
            if age > self._handoff_ttl:
                stale_ids.append(handoff_id)
                continue
            if pending.signature != signature:
                continue
            global_time = str(pending.result.event.native_time or "")
            subwindow_time = str(event.native_time or "")
            if global_time and subwindow_time and global_time != subwindow_time:
                continue
            self._handoffs.pop(handoff_id)
            return pending.result
        for handoff_id in stale_ids:
            self._handoffs.pop(handoff_id, None)
        return None

    def _consume_handoff(self, result: InboundAcceptResult) -> None:
        for handoff_id, pending in self._handoffs.items():
            if pending.result is result:
                self._handoffs.pop(handoff_id)
                return

    @staticmethod
    def _duplicate(
        original: InboundAcceptResult,
        *,
        handoff: bool = False,
    ) -> InboundAcceptResult:
        return InboundAcceptResult(
            event=original.event,
            event_id=original.event_id,
            is_new=False,
            version=original.version,
            duplicate=True,
            handoff=handoff,
        )
