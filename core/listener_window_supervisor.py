"""Small in-memory retry state for dynamic listener windows."""

from __future__ import annotations

import threading
import time
from dataclasses import asdict, dataclass


@dataclass
class WindowRetry:
    conversation: str
    first_failed_at: float
    next_retry_at: float
    attempts: int = 0
    last_error: str = ""
    allow_rebuild: bool = False
    degraded: bool = False
    inflight: bool = False


class ListenerWindowSupervisor:
    """Retry missing subwindows without owning any message data."""

    def __init__(
        self,
        *,
        retry_delays=(30.0, 60.0),
        retry_interval=60.0,
        degraded_after=600.0,
        degraded_interval=300.0,
        clock=time.time,
    ):
        self._retry_delays = tuple(max(0.0, float(value)) for value in retry_delays)
        self._retry_interval = max(1.0, float(retry_interval))
        self._degraded_after = max(1.0, float(degraded_after))
        self._degraded_interval = max(self._retry_interval, float(degraded_interval))
        self._clock = clock
        self._items: dict[str, WindowRetry] = {}
        self._lock = threading.RLock()

    def request(self, conversation, *, error="", allow_rebuild=False, now=None):
        name = str(conversation or "").strip()
        if not name:
            return False
        now = self._clock() if now is None else float(now)
        with self._lock:
            item = self._items.get(name)
            if item is None:
                item = WindowRetry(name, now, now)
                self._items[name] = item
            item.allow_rebuild = item.allow_rebuild or bool(allow_rebuild)
            if error:
                item.last_error = str(error)
            return True

    def contains(self, conversation):
        name = str(conversation or "").strip()
        with self._lock:
            return name in self._items

    def claim_due(self, *, limit=1, now=None):
        now = self._clock() if now is None else float(now)
        claimed = []
        with self._lock:
            due = sorted(
                (
                    item for item in self._items.values()
                    if not item.inflight and item.next_retry_at <= now
                ),
                key=lambda item: (item.next_retry_at, item.first_failed_at, item.conversation),
            )
            for item in due[: max(1, int(limit or 1))]:
                item.inflight = True
                claimed.append(asdict(item))
        return claimed

    def succeeded(self, conversation):
        name = str(conversation or "").strip()
        with self._lock:
            return self._items.pop(name, None) is not None

    def release(self, conversation, *, retry_after=0.0, now=None):
        """Release a claim when no window attempt was made."""
        name = str(conversation or "").strip()
        now = self._clock() if now is None else float(now)
        with self._lock:
            item = self._items.get(name)
            if item is None:
                return False
            item.inflight = False
            item.next_retry_at = max(item.next_retry_at, now + max(0.0, float(retry_after)))
            return True

    def consume_rebuild(self, conversation):
        """Allow one controlled close/rebuild for a stale registration."""
        name = str(conversation or "").strip()
        with self._lock:
            item = self._items.get(name)
            if item is None or not item.allow_rebuild:
                return False
            item.allow_rebuild = False
            return True

    def failed(self, conversation, error="", *, allow_rebuild=False, now=None):
        name = str(conversation or "").strip()
        if not name:
            return None
        now = self._clock() if now is None else float(now)
        with self._lock:
            item = self._items.get(name)
            if item is None:
                item = WindowRetry(name, now, now)
                self._items[name] = item
            item.inflight = False
            item.attempts += 1
            item.allow_rebuild = item.allow_rebuild or bool(allow_rebuild)
            if error:
                item.last_error = str(error)
            item.degraded = now - item.first_failed_at >= self._degraded_after
            if item.degraded:
                delay = self._degraded_interval
            elif item.attempts <= len(self._retry_delays):
                delay = self._retry_delays[item.attempts - 1]
            else:
                delay = self._retry_interval
            item.next_retry_at = now + delay
            return asdict(item)

    def snapshot(self):
        with self._lock:
            return [
                asdict(item)
                for item in sorted(self._items.values(), key=lambda item: item.conversation)
            ]

    def clear(self):
        with self._lock:
            self._items.clear()
