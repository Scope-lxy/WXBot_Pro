"""Small, pure-data boundary between reply generation and WeChat delivery."""

from __future__ import annotations

import sqlite3
import threading
import time
from collections import OrderedDict
from dataclasses import dataclass, replace
from enum import Enum
from typing import Any, Callable, Protocol


def is_retryable_sqlite_error(exc: BaseException) -> bool:
    if not isinstance(exc, sqlite3.OperationalError):
        return False
    error_code = getattr(exc, "sqlite_errorcode", None)
    if error_code is not None:
        return (int(error_code) & 0xFF) in {sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED}
    message = str(exc).strip().lower()
    return any(
        marker in message
        for marker in (
            "database is locked",
            "database table is locked",
            "database schema is locked",
            "database is busy",
        )
    )


class ReplyKind(str, Enum):
    TEXT = "text"
    VOICE = "voice"
    FILE = "file"
    QUOTE = "quote"


class ReplySource(str, Enum):
    AI = "ai"
    KEYWORD = "keyword"
    ERROR = "error"


@dataclass(frozen=True, slots=True)
class ReplyAction:
    kind: ReplyKind
    content: str
    source: ReplySource = ReplySource.AI
    send_value: str = ""

    def __post_init__(self) -> None:
        kind = self.kind if isinstance(self.kind, ReplyKind) else ReplyKind(self.kind)
        source = self.source if isinstance(self.source, ReplySource) else ReplySource(self.source)
        if not isinstance(self.content, str) or not self.content.strip():
            raise ValueError("reply action content must not be empty")
        object.__setattr__(self, "kind", kind)
        object.__setattr__(self, "source", source)
        object.__setattr__(self, "send_value", str(self.send_value or ""))


@dataclass(frozen=True, slots=True)
class ReplyTurn:
    turn_id: str
    conversation: str
    expected_version: int
    expires_at: float
    event_ids: tuple[str, ...]
    actions: tuple[ReplyAction, ...]
    chat_type: str = "private"

    def __post_init__(self) -> None:
        turn_id = str(self.turn_id or "").strip()
        conversation = str(self.conversation or "").strip()
        event_ids = tuple(str(item or "").strip() for item in self.event_ids)
        actions = tuple(self.actions)
        chat_type = str(self.chat_type or "private").strip().lower() or "private"
        if not turn_id:
            raise ValueError("turn_id must not be empty")
        if not conversation:
            raise ValueError("conversation must not be empty")
        if int(self.expected_version) < 0:
            raise ValueError("expected_version must not be negative")
        if float(self.expires_at) <= 0:
            raise ValueError("expires_at must be positive")
        if not event_ids or any(not item for item in event_ids):
            raise ValueError("event_ids must not be empty")
        if len(set(event_ids)) != len(event_ids):
            raise ValueError("event_ids must be unique")
        if not actions or any(not isinstance(item, ReplyAction) for item in actions):
            raise ValueError("actions must contain ReplyAction values")
        if chat_type not in {"private", "group"}:
            raise ValueError("chat_type must be private or group")
        object.__setattr__(self, "turn_id", turn_id)
        object.__setattr__(self, "conversation", conversation)
        object.__setattr__(self, "expected_version", int(self.expected_version))
        object.__setattr__(self, "expires_at", float(self.expires_at))
        object.__setattr__(self, "event_ids", event_ids)
        object.__setattr__(self, "actions", actions)
        object.__setattr__(self, "chat_type", chat_type)

    def action_id(self, index: int) -> str:
        if index < 0 or index >= len(self.actions):
            raise IndexError(index)
        return f"{self.turn_id}:{index}"


class ClaimStatus(str, Enum):
    CLAIMED = "claimed"
    DONE = "done"
    BLOCKED = "blocked"
    CANCELLED = "cancelled"
    STALE = "stale"
    EXPIRED = "expired"
    UNCERTAIN = "uncertain"


class DeliveryStatus(str, Enum):
    DONE = "done"
    RETRY = "retry"
    BLOCKED = "blocked"
    CANCELLED = "cancelled"
    STALE = "stale"
    EXPIRED = "expired"
    UNCERTAIN = "uncertain"


@dataclass(frozen=True, slots=True)
class DeliveryResult:
    status: DeliveryStatus
    completed: int = 0
    action_id: str = ""
    error: str = ""


class DeliveryNotStarted(RuntimeError):
    """A claimed action was rejected before the WeChat handler was invoked."""

    def __init__(self, status: DeliveryStatus, message: str = "") -> None:
        if status not in {
            DeliveryStatus.CANCELLED,
            DeliveryStatus.STALE,
            DeliveryStatus.EXPIRED,
        }:
            raise ValueError("not-started status must be cancelled, stale, or expired")
        self.status = status
        super().__init__(str(message or status.value))


@dataclass(frozen=True, slots=True)
class ExpectedReplyEcho:
    """One short-lived outbound echo expectation kept only in memory."""

    action_id: str
    conversation: str
    chat_type: str
    kind: ReplyKind
    content: str
    expires_at: float
    confirmable: bool = True
    matchable: bool = False
    message_types: tuple[str, ...] = ()


class ReplyEchoTracker:
    """Correlate wxautox self callbacks with a claimed reply bubble."""

    def __init__(self, *, ttl=60.0, max_items=512, clock=time.time) -> None:
        self._ttl = max(1.0, float(ttl))
        self._max_items = max(1, int(max_items))
        self._clock = clock
        self._items: OrderedDict[str, ExpectedReplyEcho] = OrderedDict()
        self._lock = threading.Lock()

    def reserve(
        self,
        action_id: str,
        conversation: str,
        action: ReplyAction,
        *,
        confirmable=True,
        chat_type="private",
        message_types=(),
    ) -> None:
        now = float(self._clock())
        expected = ExpectedReplyEcho(
            action_id=str(action_id or "").strip(),
            conversation=str(conversation or "").strip(),
            chat_type=str(chat_type or "private").strip().lower() or "private",
            kind=action.kind,
            content=str(action.content or "").strip(),
            expires_at=0.0,
            confirmable=bool(confirmable),
            message_types=tuple(
                dict.fromkeys(
                    str(item or "").strip().lower()
                    for item in message_types or ()
                    if str(item or "").strip()
                )
            ),
        )
        if not expected.action_id or not expected.conversation:
            raise ValueError("echo action_id and conversation are required")
        with self._lock:
            self._prune_locked(now)
            self._items[expected.action_id] = expected
            self._items.move_to_end(expected.action_id)
            while len(self._items) > self._max_items:
                self._items.popitem(last=False)

    def match(
        self,
        conversation: str,
        message_type: str,
        content: str,
        *,
        chat_type="private",
    ) -> ExpectedReplyEcho | None:
        now = float(self._clock())
        conversation = str(conversation or "").strip()
        chat_type = str(chat_type or "private").strip().lower() or "private"
        message_type = str(message_type or "text").strip().lower() or "text"
        content = str(content or "").strip()
        with self._lock:
            self._prune_locked(now)
            fallback = None
            for action_id, expected in tuple(self._items.items()):
                if not expected.matchable:
                    continue
                if expected.conversation != conversation or expected.chat_type != chat_type:
                    continue
                if not self._matches(expected, message_type, content):
                    continue
                if content and content == expected.content:
                    self._items.pop(action_id, None)
                    return expected
                if fallback is None:
                    fallback = (action_id, expected)
            if fallback is not None:
                action_id, expected = fallback
                self._items.pop(action_id, None)
                return expected
        return None

    def discard(self, action_id: str) -> None:
        with self._lock:
            self._items.pop(str(action_id or "").strip(), None)

    def activate(self, action_ids) -> None:
        now = float(self._clock())
        with self._lock:
            for action_id in action_ids or ():
                key = str(action_id or "").strip()
                expected = self._items.get(key)
                if expected is None:
                    continue
                self._items[key] = replace(
                    expected,
                    expires_at=float("inf"),
                    matchable=True,
                )
                self._items.move_to_end(key)

    def complete(self, action_ids) -> None:
        now = float(self._clock())
        with self._lock:
            for action_id in action_ids or ():
                key = str(action_id or "").strip()
                expected = self._items.get(key)
                if expected is None or not expected.matchable:
                    continue
                self._items[key] = replace(expected, expires_at=now + self._ttl)
                self._items.move_to_end(key)

    @staticmethod
    def _matches(expected: ExpectedReplyEcho, message_type: str, content: str) -> bool:
        if expected.message_types:
            return message_type in expected.message_types
        if expected.kind in {ReplyKind.TEXT, ReplyKind.QUOTE}:
            return message_type in {"text", "quote"} and content == expected.content
        if expected.kind == ReplyKind.VOICE:
            return message_type in {"voice", "audio"}
        return message_type in {"file", "image", "video"}

    def _prune_locked(self, now: float) -> None:
        for action_id, expected in tuple(self._items.items()):
            if expected.matchable and expected.expires_at < now:
                self._items.pop(action_id, None)


class ReplyDeliveryStore(Protocol):
    def register_reply_turn(
        self,
        turn_id: str,
        *,
        conversation: str,
        expected_version: int,
        expires_at: float,
        event_ids: tuple[str, ...],
        action_count: int,
        chat_type: str = "private",
    ) -> bool: ...

    def conditional_claim(
        self,
        action_id: str,
        *,
        conversation: str,
        expected_version: int,
        expires_at: float,
        now: float,
    ) -> ClaimStatus | str | bool: ...

    def confirm_outbound(
        self,
        action_id: str,
        conversation: str,
        *,
        content: str,
        sent_at: float,
        chat_type: str = "private",
        message_type: str = "text",
    ) -> object: ...

    def finish(self, action_id: str, status: str = "uncertain", error: str = "") -> None: ...

    def delivery_action_status(self, action_id: str) -> str: ...

    def cancel_pending(
        self,
        turn_id: str,
        status: str = "cancelled",
        error: str = "",
    ) -> None: ...


PrepareReply = Callable[[ReplyTurn, ReplyAction, str, Any], Any]
SendReply = Callable[[ReplyTurn, ReplyAction, str, Any], Any]
VersionProvider = Callable[[str, str], int]


class ReplyDeliveryCoordinator:
    """Deliver one immutable turn without owning a worker or retry loop."""

    def __init__(
        self,
        *,
        store: ReplyDeliveryStore,
        version_provider: VersionProvider,
        prepare: PrepareReply,
        sender: SendReply,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._store = store
        self._version_provider = version_provider
        self._prepare = prepare
        self._sender = sender
        self._clock = clock
        self._stopping = threading.Event()
        self._lock = threading.Lock()
        self._cancelled: set[str] = set()
        self._known: set[str] = set()

    def deliver(self, turn: ReplyTurn, context: Any = None) -> DeliveryResult:
        try:
            self._store.register_reply_turn(
                turn.turn_id,
                conversation=turn.conversation,
                expected_version=turn.expected_version,
                expires_at=turn.expires_at,
                event_ids=turn.event_ids,
                action_count=len(turn.actions),
                chat_type=turn.chat_type,
            )
        except Exception as exc:
            if is_retryable_sqlite_error(exc):
                return DeliveryResult(
                    DeliveryStatus.RETRY,
                    action_id=turn.action_id(0),
                    error=str(exc),
                )
            raise
        with self._lock:
            self._known.add(turn.turn_id)
        try:
            result = self._deliver_registered(turn, context)
        except Exception:
            with self._lock:
                self._known.discard(turn.turn_id)
            raise
        if result.status not in {DeliveryStatus.RETRY, DeliveryStatus.BLOCKED}:
            with self._lock:
                self._known.discard(turn.turn_id)
        return result

    def cancel(self, turn_id: str, reason: str = "reply turn cancelled") -> None:
        turn_id = str(turn_id or "").strip()
        if not turn_id:
            return
        with self._lock:
            self._cancelled.add(turn_id)
        self._cancel_pending(turn_id, DeliveryStatus.CANCELLED, reason)

    def stop(self) -> None:
        self._stopping.set()
        with self._lock:
            known = tuple(self._known)
        for turn_id in known:
            self._cancel_pending(turn_id, DeliveryStatus.CANCELLED, "reply coordinator stopped")

    def _deliver_registered(self, turn: ReplyTurn, context: Any) -> DeliveryResult:
        completed = 0
        for index, action in enumerate(turn.actions):
            action_id = turn.action_id(index)
            preflight = self._preflight(turn, action, action_id, completed)
            if preflight is not None:
                return preflight

            prepared = self._prepare(turn, action, action_id, context)
            if not prepared:
                reason = "reply preparation stopped before delivery claim"
                self._cancel_pending(turn.turn_id, DeliveryStatus.CANCELLED, reason)
                return DeliveryResult(
                    DeliveryStatus.CANCELLED,
                    completed,
                    action_id,
                    reason,
                )

            preflight = self._preflight(turn, action, action_id, completed)
            if preflight is not None:
                return preflight

            try:
                claim = self._claim(turn, action_id)
            except Exception as exc:
                if is_retryable_sqlite_error(exc):
                    return DeliveryResult(DeliveryStatus.RETRY, completed, action_id, str(exc))
                raise
            if claim == ClaimStatus.DONE:
                completed += 1
                continue
            if claim != ClaimStatus.CLAIMED:
                status = DeliveryStatus(claim.value)
                if status not in {DeliveryStatus.BLOCKED}:
                    self._cancel_pending(turn.turn_id, status, f"claim rejected: {claim.value}")
                return DeliveryResult(status, completed, action_id, f"claim rejected: {claim.value}")

            try:
                sent = self._sender(turn, action, action_id, context)
                if not sent:
                    if self._action_done(action_id):
                        completed += 1
                        continue
                    frozen = self._freeze_uncertain(
                        turn,
                        action_id,
                        completed,
                        "sender returned a false result after delivery was claimed",
                    )
                    if frozen is None:
                        completed += 1
                        continue
                    return frozen
            except DeliveryNotStarted as exc:
                try:
                    final_status = self._store.finish(
                        action_id,
                        exc.status.value,
                        str(exc),
                    )
                except Exception as finish_exc:
                    if self._action_done(action_id):
                        completed += 1
                        continue
                    frozen = self._freeze_uncertain(
                        turn,
                        action_id,
                        completed,
                        str(finish_exc),
                    )
                    if frozen is None:
                        completed += 1
                        continue
                    return frozen
                if str(final_status) == DeliveryStatus.DONE.value:
                    completed += 1
                    continue
                return DeliveryResult(exc.status, completed, action_id, str(exc))
            except Exception as exc:
                if self._action_done(action_id):
                    completed += 1
                    continue
                frozen = self._freeze_uncertain(turn, action_id, completed, str(exc))
                if frozen is None:
                    completed += 1
                    continue
                return frozen
            try:
                self._store.confirm_outbound(
                    action_id,
                    turn.conversation,
                    content=action.content,
                    sent_at=float(self._clock()),
                    chat_type=turn.chat_type,
                    message_type=self._history_message_type(action.kind),
                )
            except Exception as exc:
                if self._action_done(action_id):
                    completed += 1
                    continue
                frozen = self._freeze_uncertain(turn, action_id, completed, str(exc))
                if frozen is None:
                    completed += 1
                    continue
                return frozen
            completed += 1

        return DeliveryResult(DeliveryStatus.DONE, completed)

    @staticmethod
    def _history_message_type(kind: ReplyKind) -> str:
        if kind == ReplyKind.VOICE:
            return "voice"
        if kind == ReplyKind.FILE:
            return "file"
        return "text"

    def _action_done(self, action_id: str) -> bool:
        try:
            return str(self._store.delivery_action_status(action_id)) == DeliveryStatus.DONE.value
        except Exception:
            return False

    def _preflight(
        self,
        turn: ReplyTurn,
        _action: ReplyAction,
        action_id: str,
        completed: int,
    ) -> DeliveryResult | None:
        with self._lock:
            cancelled = turn.turn_id in self._cancelled
        if self._stopping.is_set() or cancelled:
            reason = "reply coordinator stopped" if self._stopping.is_set() else "reply turn cancelled"
            self._cancel_pending(turn.turn_id, DeliveryStatus.CANCELLED, reason)
            return DeliveryResult(DeliveryStatus.CANCELLED, completed, action_id, reason)

        if float(self._clock()) >= turn.expires_at:
            reason = "reply turn expired before delivery claim"
            self._cancel_pending(turn.turn_id, DeliveryStatus.EXPIRED, reason)
            return DeliveryResult(DeliveryStatus.EXPIRED, completed, action_id, reason)

        try:
            current_version = int(self._version_provider(turn.conversation, turn.chat_type))
        except Exception as exc:
            if is_retryable_sqlite_error(exc):
                return DeliveryResult(DeliveryStatus.RETRY, completed, action_id, str(exc))
            raise
        if current_version != turn.expected_version:
            reason = "conversation version changed before delivery claim"
            self._cancel_pending(turn.turn_id, DeliveryStatus.STALE, reason)
            return DeliveryResult(DeliveryStatus.STALE, completed, action_id, reason)
        return None

    def _claim(self, turn: ReplyTurn, action_id: str) -> ClaimStatus:
        value = self._store.conditional_claim(
            action_id,
            conversation=turn.conversation,
            expected_version=turn.expected_version,
            expires_at=turn.expires_at,
            now=float(self._clock()),
        )
        if value is True:
            return ClaimStatus.CLAIMED
        if value is False:
            return ClaimStatus.BLOCKED
        return value if isinstance(value, ClaimStatus) else ClaimStatus(value)

    def _freeze_uncertain(
        self,
        turn: ReplyTurn,
        action_id: str,
        completed: int,
        error: str,
    ) -> DeliveryResult | None:
        try:
            final_status = self._store.finish(
                action_id,
                DeliveryStatus.UNCERTAIN.value,
                error,
            )
        except Exception:
            final_status = ""
        if str(final_status) == DeliveryStatus.DONE.value or self._action_done(action_id):
            return None
        self._cancel_pending(
            turn.turn_id,
            DeliveryStatus.CANCELLED,
            "a previous reply action has an uncertain delivery result",
        )
        return DeliveryResult(DeliveryStatus.UNCERTAIN, completed, action_id, error)

    def _cancel_pending(self, turn_id: str, status: DeliveryStatus, reason: str) -> None:
        try:
            self._store.cancel_pending(turn_id, status=status.value, error=reason)
        except Exception:
            pass
