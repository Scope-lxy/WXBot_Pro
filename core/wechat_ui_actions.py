"""Single-owner scheduling for every in-process WeChat UI action.

The lock helpers at the bottom are temporary migration shims. New production
code must submit :class:`UIIntent` values to :class:`WeChatUIOwner` instead of
holding the legacy global lock.
"""

from __future__ import annotations

import threading
import time
from collections import deque
from contextlib import contextmanager
from dataclasses import dataclass, field
from enum import Enum
from types import MappingProxyType
from typing import Any, Callable, Iterator, Mapping

from core.logger import log


ReleaseLock = Callable[[], None]
IntentHandler = Callable[[Mapping[str, Any]], Any]
IntentVersionProvider = Callable[[str], int]
IntentPayloadPreparer = Callable[["UIIntent"], Mapping[str, Any]]


class UIIntentKind(str, Enum):
    BOOTSTRAP = "bootstrap"
    REBIND = "rebind"
    SHUTDOWN = "shutdown"
    POLL_MESSAGES = "poll_messages"
    GET_MESSAGES = "get_messages"
    SEND_TEXT = "send_text"
    SEND_TEXT_BATCH = "send_text_batch"
    SEND_ACTIONS = "send_actions"
    ADD_LISTEN = "add_listen"
    REMOVE_LISTEN = "remove_listen"
    SEND_FILE = "send_file"
    SEND_AUDIO = "send_audio"
    DOWNLOAD_MEDIA = "download_media"
    FORWARD = "forward"
    QUOTE = "quote"
    MATERIAL_READ = "material_read"
    CONTACT_START = "contact_start"
    CONTACT_RECOVER = "contact_recover"
    MAIN_WINDOW = "main_window"
    MOMENTS = "moments"
    CONTACT_EDIT = "contact_edit"
    RELATIONSHIP_SCAN = "relationship_scan"
    FRIEND_REQUEST = "friend_request"
    NEW_FRIEND = "new_friend"


class ActionBatchInterrupted(RuntimeError):
    def __init__(self, completed_results, failed_index, cause):
        self.completed_results = list(completed_results or [])
        self.failed_index = int(failed_index)
        self.cause = cause
        super().__init__(f"第 {self.failed_index + 1} 个发送动作结果未知：{cause}")


LIGHTWEIGHT_INTENTS = frozenset({UIIntentKind.GET_MESSAGES, UIIntentKind.SEND_TEXT, UIIntentKind.SEND_TEXT_BATCH})
JOURNALED_DELIVERY_INTENTS = frozenset({
    UIIntentKind.SEND_FILE,
    UIIntentKind.SEND_AUDIO,
    UIIntentKind.FORWARD,
    UIIntentKind.QUOTE,
    UIIntentKind.SEND_ACTIONS,
})
UI_STUCK_EXIT_CODE = 86
# Queueing is intentionally unbounded; active UI actions have their own watchdog deadlines.
UI_CALL_WAIT_TIMEOUT = None


def _log_runtime_event(message: str, runtime_id: str) -> None:
    runtime_id = str(runtime_id or "").strip().lower()
    if len(runtime_id) != 32 or any(char not in "0123456789abcdef" for char in runtime_id):
        return
    try:
        log(level="DEBUG", message=f"{message} runtime_id={runtime_id}")
    except Exception:
        pass


def _is_pure_data(value: Any) -> bool:
    if value is None or isinstance(value, (bool, int, float, str, bytes)):
        return True
    if isinstance(value, (list, tuple)):
        return all(_is_pure_data(item) for item in value)
    if isinstance(value, Mapping):
        return all(isinstance(key, str) and _is_pure_data(item) for key, item in value.items())
    return False


def _freeze(value: Any) -> Any:
    if isinstance(value, Mapping):
        return MappingProxyType({str(key): _freeze(item) for key, item in value.items()})
    if isinstance(value, list):
        return tuple(_freeze(item) for item in value)
    if isinstance(value, tuple):
        return tuple(_freeze(item) for item in value)
    return value


@dataclass(frozen=True)
class UIIntent:
    kind: UIIntentKind
    payload: Mapping[str, Any] = field(default_factory=dict)
    conversation_version: int = 0
    task_version: int = 0

    def __post_init__(self) -> None:
        kind = self.kind if isinstance(self.kind, UIIntentKind) else UIIntentKind(self.kind)
        payload = dict(self.payload or {})
        if not _is_pure_data(payload):
            raise TypeError("微信 UI 意图只允许携带纯数据")
        object.__setattr__(self, "kind", kind)
        object.__setattr__(self, "payload", _freeze(payload))
        object.__setattr__(self, "conversation_version", int(self.conversation_version or 0))
        object.__setattr__(self, "task_version", int(self.task_version or 0))


class IntentCancelled(RuntimeError):
    pass


class IntentNeedsExclusive(RuntimeError):
    """A light intent must keep its place and retry after contact recovery."""


class IntentTicket:
    def __init__(self, intent: UIIntent):
        self.intent = intent
        self._event = threading.Event()
        self._result: Any = None
        self._error: BaseException | None = None
        self.force_exclusive = False

    @property
    def done(self) -> bool:
        return self._event.is_set()

    def set_result(self, result: Any) -> None:
        if self._event.is_set():
            return
        self._result = result
        self._event.set()

    def set_error(self, error: BaseException) -> None:
        if self._event.is_set():
            return
        self._error = error
        self._event.set()

    def result(self, timeout: float | None = None) -> Any:
        if not self._event.wait(timeout):
            raise TimeoutError(f"微信 UI 意图等待超时：{self.intent.kind.value}")
        if self._error is not None:
            raise self._error
        return self._result


class CallbackActionTicket(IntentTicket):
    """Reserve the owner FIFO while a wxautox callback uses its thread-bound object."""

    def __init__(self, intent: UIIntent):
        super().__init__(intent)
        self._granted = threading.Event()
        self._completed = threading.Event()
        self.callback_result: Any = None
        self.callback_error: BaseException | None = None

    def set_error(self, error: BaseException) -> None:
        super().set_error(error)
        self._granted.set()

    def run(self, action: Callable[[], Any]) -> Any:
        self._granted.wait()
        if self.done:
            return self.result(0)
        try:
            self.callback_result = action()
            return self.callback_result
        except BaseException as exc:
            self.callback_error = exc
            raise
        finally:
            self._completed.set()


@dataclass
class ContactBatchHandle:
    """Internal handle returned after the owner starts the collector process."""

    poll: Callable[[], tuple[bool, Any]]
    terminate: Callable[[], None] = lambda: None


@dataclass(frozen=True)
class CurrentActionSnapshot:
    kind: str = ""
    payload: Mapping[str, Any] = field(default_factory=dict)
    started_at: float = 0.0
    deadline_at: float = 0.0


class WeChatUIOwner:
    """One FIFO consumer that owns all in-process WeChat/COM objects."""

    def __init__(
        self,
        handlers: Mapping[UIIntentKind, IntentHandler],
        *,
        light_timeout: float = 30.0,
        exclusive_timeout: float = 180.0,
        poll_interval: float = 0.05,
        thread_name: str = "wechat-ui-owner",
        conversation_version_provider: IntentVersionProvider | None = None,
        task_version_provider: IntentVersionProvider | None = None,
        payload_preparer: IntentPayloadPreparer | None = None,
        runtime_id: str = "",
    ):
        self._handlers = {
            kind if isinstance(kind, UIIntentKind) else UIIntentKind(kind): handler
            for kind, handler in handlers.items()
        }
        self._light_timeout = float(light_timeout)
        self._exclusive_timeout = float(exclusive_timeout)
        self._poll_interval = max(0.01, float(poll_interval))
        self._conversation_version_provider = conversation_version_provider
        self._task_version_provider = task_version_provider
        self._payload_preparer = payload_preparer
        self._runtime_id = str(runtime_id or "").strip().lower()
        self._condition = threading.Condition()
        self._queue: deque[IntentTicket] = deque()
        self._thread = threading.Thread(target=self._run, name=thread_name, daemon=True)
        self._accepting = True
        self._stop_requested = False
        self._started = False
        self._owner_thread_id: int | None = None
        self._current_action = CurrentActionSnapshot()
        self._contact_job: ContactBatchHandle | None = None
        self._contact_ticket: IntentTicket | None = None
        self._contact_barrier_active = False
        self._poll_due_ticket: IntentTicket | None = None
        self._delivery_journal = None

    @property
    def owner_thread_id(self) -> int | None:
        return self._owner_thread_id

    @property
    def contact_active(self) -> bool:
        with self._condition:
            return self._contact_barrier_active

    def wait_for_contact_idle(self) -> bool:
        with self._condition:
            while self._contact_barrier_active and not self._stop_requested:
                self._condition.wait(self._poll_interval)
            return not self._contact_barrier_active and not self._stop_requested

    @property
    def is_running(self) -> bool:
        with self._condition:
            return bool(
                self._started
                and self._thread.is_alive()
                and self._accepting
                and not self._stop_requested
            )

    def current_action_snapshot(self) -> CurrentActionSnapshot:
        with self._condition:
            return self._current_action

    def set_delivery_journal(self, journal) -> None:
        with self._condition:
            self._delivery_journal = journal

    def heartbeat_current_action(self) -> None:
        self.assert_owner_thread()
        now = time.monotonic()
        with self._condition:
            snapshot = self._current_action
            if not snapshot.kind:
                return
            kind = UIIntentKind(snapshot.kind)
            timeout = self._light_timeout if kind in LIGHTWEIGHT_INTENTS else self._exclusive_timeout
            self._current_action = CurrentActionSnapshot(
                kind=snapshot.kind,
                payload=snapshot.payload,
                started_at=snapshot.started_at,
                deadline_at=now + timeout,
            )

    def start(self) -> None:
        with self._condition:
            if self._started:
                return
            self._started = True
            self._thread.start()

    def assert_owner_thread(self) -> None:
        if threading.get_ident() != self._owner_thread_id:
            raise RuntimeError("微信 UI 对象只能由 UI owner 线程访问")

    def submit(self, intent: UIIntent) -> IntentTicket:
        if not isinstance(intent, UIIntent):
            raise TypeError("必须提交 UIIntent")
        queued_behind_contact = False
        with self._condition:
            if not self._accepting:
                raise RuntimeError("微信 UI owner 正在停止，不再接受新意图")
            if intent.kind == UIIntentKind.POLL_MESSAGES and self._contact_barrier_active:
                if self._poll_due_ticket is None or self._poll_due_ticket.done:
                    self._poll_due_ticket = IntentTicket(intent)
                    self._queue.append(self._poll_due_ticket)
                return self._poll_due_ticket
            ticket = IntentTicket(intent)
            self._queue.append(ticket)
            queued_behind_contact = self._contact_barrier_active
            self._condition.notify_all()
        if queued_behind_contact:
            _log_runtime_event("运行事件：通讯录期间微信 UI 任务已排队", self._runtime_id)
        return ticket

    def call(self, intent: UIIntent, timeout: float | None = None) -> Any:
        return self.submit(intent).result(timeout)

    def run_callback_action(self, intent: UIIntent, action: Callable[[], Any]) -> Any:
        """Run a callback-thread wxautox action in the same FIFO as owner intents."""
        if threading.get_ident() == self._owner_thread_id:
            return action()
        ticket = CallbackActionTicket(intent)
        queued_behind_contact = False
        with self._condition:
            if not self._accepting:
                raise RuntimeError("微信 UI owner 正在停止，不再接受新意图")
            self._queue.append(ticket)
            queued_behind_contact = self._contact_barrier_active
            self._condition.notify_all()
        if queued_behind_contact:
            _log_runtime_event("运行事件：通讯录期间微信 UI 任务已排队", self._runtime_id)
        return ticket.run(action)

    def cancel_pending(self) -> None:
        """Stop accepting work and cancel intents that have not started."""
        with self._condition:
            self._accepting = False
            while self._queue:
                self._queue.popleft().set_error(IntentCancelled("微信 UI owner 正在停止"))
            if self._poll_due_ticket and not self._poll_due_ticket.done:
                self._poll_due_ticket.set_error(IntentCancelled("微信 UI owner 正在停止"))
            self._poll_due_ticket = None
            self._condition.notify_all()
        self.terminate_active_contact_job()

    def call_shutdown(self, timeout: float | None = None) -> Any:
        """Queue the sole shutdown action after normal work has been cancelled."""
        ticket = IntentTicket(UIIntent(UIIntentKind.SHUTDOWN))
        with self._condition:
            if self._stop_requested:
                raise RuntimeError("微信 UI owner 已停止")
            self._accepting = False
            self._queue.appendleft(ticket)
            self._condition.notify_all()
        return ticket.result(timeout)

    def stop(self, *, cancel_pending: bool = True, timeout: float | None = 5.0) -> None:
        with self._condition:
            self._accepting = False
            self._stop_requested = True
            if cancel_pending:
                while self._queue:
                    self._queue.popleft().set_error(IntentCancelled("微信 UI owner 已停止"))
                if self._poll_due_ticket and not self._poll_due_ticket.done:
                    self._poll_due_ticket.set_error(IntentCancelled("微信 UI owner 已停止"))
            contact_job = self._contact_job
            self._condition.notify_all()
        if contact_job is not None:
            contact_job.terminate()
        if self._started and threading.get_ident() != self._owner_thread_id:
            self._thread.join(timeout)

    def terminate_active_contact_job(self) -> bool:
        with self._condition:
            contact_job = self._contact_job
        if contact_job is None:
            return False
        contact_job.terminate()
        with self._condition:
            if self._contact_job is contact_job:
                ticket = self._contact_ticket
                self._contact_job = None
                self._contact_ticket = None
                self._contact_barrier_active = False
                self._condition.notify_all()
            else:
                ticket = None
        if ticket is not None:
            ticket.set_error(IntentCancelled("通讯录采集器已被终止"))
        return True

    def _next_ticket_locked(self) -> IntentTicket | None:
        if not self._queue:
            return None
        if self._contact_barrier_active:
            return None
        return self._queue.popleft()

    def _poll_contact_job(self) -> None:
        with self._condition:
            job = self._contact_job
        if job is None:
            return
        try:
            done, result = job.poll()
        except BaseException as exc:
            done, result = True, exc
        if not done:
            return

        recover_error: BaseException | None = None
        recover = self._handlers.get(UIIntentKind.CONTACT_RECOVER)
        if recover is not None:
            try:
                self._execute_direct(UIIntent(UIIntentKind.CONTACT_RECOVER))
            except BaseException as exc:
                recover_error = exc

        with self._condition:
            ticket = self._contact_ticket
            self._contact_job = None
            self._contact_ticket = None
            self._contact_barrier_active = False
            self._condition.notify_all()
        if ticket is not None:
            if recover_error is not None:
                ticket.set_error(recover_error)
            elif isinstance(result, BaseException):
                ticket.set_error(result)
            else:
                ticket.set_result(result)

    def _execute_direct(self, intent: UIIntent) -> Any:
        handler = self._handlers.get(intent.kind)
        if handler is None:
            raise RuntimeError(f"未注册微信 UI 意图处理器：{intent.kind.value}")
        timeout = self._light_timeout if intent.kind in LIGHTWEIGHT_INTENTS else self._exclusive_timeout
        started_at = time.monotonic()
        with self._condition:
            self._current_action = CurrentActionSnapshot(
                kind=intent.kind.value,
                payload=intent.payload,
                started_at=started_at,
                deadline_at=started_at + timeout,
            )
        try:
            return handler(intent.payload)
        finally:
            with self._condition:
                self._current_action = CurrentActionSnapshot()

    def _execute_ticket(self, ticket: IntentTicket) -> None:
        with self._condition:
            if ticket is self._poll_due_ticket:
                self._poll_due_ticket = None
        starting_contact = ticket.intent.kind == UIIntentKind.CONTACT_START
        if starting_contact:
            with self._condition:
                self._contact_barrier_active = True
                self._condition.notify_all()
        try:
            intent = ticket.intent
            conversation = str(intent.payload.get("conversation") or "").strip()
            if intent.conversation_version and conversation and callable(self._conversation_version_provider):
                current = int(self._conversation_version_provider(conversation) or 0)
                if current != intent.conversation_version:
                    raise IntentCancelled("会话已有新消息，已取消过期微信操作")
            task_key = str(intent.payload.get("task_key") or "").strip()
            if intent.task_version and task_key and callable(self._task_version_provider):
                current = int(self._task_version_provider(task_key) or 0)
                if current != intent.task_version:
                    raise IntentCancelled("任务已取消或更新，已取消过期微信操作")
            if callable(self._payload_preparer):
                payload = dict(self._payload_preparer(intent) or {})
                intent = UIIntent(
                    intent.kind,
                    payload,
                    conversation_version=intent.conversation_version,
                    task_version=intent.task_version,
                )
            delivery_id = str(intent.payload.get("delivery_id") or "").strip()
            journal = self._delivery_journal if intent.kind in JOURNALED_DELIVERY_INTENTS and delivery_id else None
            if journal is not None and not journal.begin(delivery_id, intent.kind.value, intent.payload):
                raise IntentCancelled("该微信投递已提交过，禁止重复发送")
            if ticket.force_exclusive:
                payload = dict(intent.payload)
                payload["_exclusive_retry"] = True
                intent = UIIntent(
                    intent.kind,
                    payload,
                    conversation_version=intent.conversation_version,
                    task_version=intent.task_version,
                )
            if isinstance(ticket, CallbackActionTicket):
                timeout = self._light_timeout if intent.kind in LIGHTWEIGHT_INTENTS else self._exclusive_timeout
                started_at = time.monotonic()
                with self._condition:
                    self._current_action = CurrentActionSnapshot(
                        kind=intent.kind.value,
                        payload=intent.payload,
                        started_at=started_at,
                        deadline_at=started_at + timeout,
                    )
                ticket._granted.set()
                ticket._completed.wait()
                with self._condition:
                    self._current_action = CurrentActionSnapshot()
                if ticket.callback_error is not None:
                    raise ticket.callback_error
                ticket.set_result(ticket.callback_result)
                return
            try:
                result = self._execute_direct(intent)
            except BaseException as exc:
                if journal is not None:
                    details = None
                    if isinstance(exc, ActionBatchInterrupted):
                        action_count = len(intent.payload.get("actions") or ())
                        details = {
                            "failed_index": exc.failed_index,
                            "actions": [
                                {
                                    "index": index,
                                    "status": (
                                        "done" if index < exc.failed_index
                                        else "uncertain" if index == exc.failed_index
                                        else "pending"
                                    ),
                                }
                                for index in range(action_count)
                            ],
                        }
                    if details is None:
                        journal.finish(delivery_id, "uncertain", str(exc))
                    else:
                        journal.finish(delivery_id, "uncertain", str(exc), details=details)
                raise
            if journal is not None:
                journal.finish(delivery_id, "done")
            if ticket.intent.kind == UIIntentKind.CONTACT_START:
                if not isinstance(result, ContactBatchHandle):
                    raise TypeError("通讯录启动处理器必须返回 ContactBatchHandle")
                with self._condition:
                    cancelled = not self._accepting
                    if not cancelled:
                        self._contact_job = result
                        self._contact_ticket = ticket
                if cancelled:
                    result.terminate()
                    with self._condition:
                        self._contact_barrier_active = False
                        self._condition.notify_all()
                    ticket.set_error(IntentCancelled("微信 UI owner 正在停止"))
                return
            ticket.set_result(result)
        except IntentNeedsExclusive:
            ticket.force_exclusive = True
            with self._condition:
                queued_behind_contact = self._contact_barrier_active
                self._queue.appendleft(ticket)
                self._condition.notify_all()
            if queued_behind_contact:
                _log_runtime_event("运行事件：通讯录期间微信 UI 任务已排队", self._runtime_id)
        except BaseException as exc:
            if starting_contact:
                with self._condition:
                    self._contact_barrier_active = False
                    self._condition.notify_all()
            ticket.set_error(exc)

    def _run(self) -> None:
        pythoncom = None
        try:
            import pythoncom as _pythoncom
            pythoncom = _pythoncom
            pythoncom.CoInitialize()
        except Exception:
            pythoncom = None
        self._owner_thread_id = threading.get_ident()
        try:
            while True:
                self._poll_contact_job()
                with self._condition:
                    ticket = self._next_ticket_locked()
                    if ticket is None:
                        if self._stop_requested and self._contact_job is None:
                            break
                        self._condition.wait(self._poll_interval)
                        continue
                self._execute_ticket(ticket)
        finally:
            if pythoncom is not None:
                pythoncom.CoUninitialize()


class UIWatchdog:
    """Observe owner deadlines; process termination is supplied by the caller."""

    def __init__(self, snapshot_provider, on_timeout, *, poll_interval: float = 0.5):
        self._snapshot_provider = snapshot_provider
        self._on_timeout = on_timeout
        self._poll_interval = max(0.01, float(poll_interval))
        self._stop_event = threading.Event()
        self._thread = threading.Thread(target=self._run, name="wechat-ui-watchdog", daemon=True)
        self._started = False

    def start(self):
        if self._started:
            return
        self._started = True
        self._thread.start()

    def stop(self, timeout: float | None = 1.0):
        self._stop_event.set()
        if self._started and threading.get_ident() != self._thread.ident:
            self._thread.join(timeout)

    def _run(self):
        while not self._stop_event.wait(self._poll_interval):
            snapshot = self._snapshot_provider()
            deadline = float(getattr(snapshot, "deadline_at", 0.0) or 0.0)
            if not deadline or time.monotonic() < deadline:
                continue
            self._on_timeout(snapshot)
            return


def _noop_release() -> None:
    return None


def _lock_for(bot: Any) -> Any | None:
    if getattr(bot, "_ui_owner", None) is not None:
        return None
    getter = getattr(bot, "_get_wechat_action_lock", None)
    if not callable(getter):
        return None
    return getter()


def acquire(bot: Any, *, blocking: bool = True) -> ReleaseLock | None:
    lock = _lock_for(bot)
    if lock is None:
        return _noop_release
    acquire_fn = getattr(lock, "acquire", None)
    release_fn = getattr(lock, "release", None)
    if callable(acquire_fn):
        acquired = acquire_fn(blocking=bool(blocking))
        if not acquired:
            return None
        if not callable(release_fn):
            raise RuntimeError("微信 UI 操作锁不可用")
        return release_fn
    enter_fn = getattr(lock, "__enter__", None)
    exit_fn = getattr(lock, "__exit__", None)
    if callable(enter_fn) and callable(exit_fn):
        enter_fn()
        return lambda: exit_fn(None, None, None)
    raise RuntimeError("微信 UI 操作锁不可用")


def try_acquire(bot: Any) -> ReleaseLock | None:
    return acquire(bot, blocking=False)


def is_busy(bot: Any) -> bool:
    release = try_acquire(bot)
    if not release:
        return True
    release()
    return False


@contextmanager
def hold(bot: Any) -> Iterator[None]:
    release = acquire(bot, blocking=True)
    if not release:
        raise RuntimeError("微信 UI 操作锁不可用")
    try:
        yield
    finally:
        release()
