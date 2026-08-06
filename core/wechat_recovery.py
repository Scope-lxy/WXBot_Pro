"""WeChat UI deadline detection and staged recovery coordination."""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable

from core.logger import log


UI_STUCK_EXIT_CODE = 86
UI_STUCK_STOPPED_EXIT_CODE = 87
UI_OWNER_LOCK_RECOVERY_GRACE_SECONDS = 10.0
LISTENER_RECOVERY_PROBE_INTERVAL_SECONDS = 5.0
LISTENER_RECOVERY_OBSERVATION_SECONDS = 10 * 60.0
LISTENER_RECOVERY_HRESULTS = {
    -2147220991,  # Event cannot call any of the subscribers.
    -2147023174,  # RPC server unavailable.
    -2146233088,
}
LISTENER_RECOVERY_ERROR_PATTERNS = (
    "事件无法调用任何订户",
    "RPC 服务器不可用",
    "远程过程调用失败",
    "元素不可用",
    "对象不再连接到服务器",
    "Find Control Timeout",
)


@dataclass(frozen=True)
class RecoveryStateSnapshot:
    active: bool
    attempted: bool
    probe_after: float
    last_error: str
    source: str
    force_rebind: bool
    observation_started_at: float
    failed_conversations: frozenset[tuple[str, str]]
    after_rebind: bool
    observation_had_success: bool
    restart_requested: bool


@dataclass
class _RecoveryState:
    active: bool = False
    attempted: bool = False
    probe_after: float = 0.0
    last_error: str = ""
    source: str = ""
    force_rebind: bool = False
    observation_started_at: float = 0.0
    failed_conversations: set[tuple[str, str]] = field(default_factory=set)
    after_rebind: bool = False
    observation_had_success: bool = False
    restart_requested: bool = False


def is_listener_recovery_desktop_error(exc: BaseException | None) -> bool:
    if exc is None:
        return False
    hresult = getattr(exc, "hresult", None)
    if isinstance(hresult, int) and hresult in LISTENER_RECOVERY_HRESULTS:
        return True
    args = getattr(exc, "args", ())
    if args and isinstance(args[0], int) and args[0] in LISTENER_RECOVERY_HRESULTS:
        return True
    text = str(exc or "")
    return any(pattern in text for pattern in LISTENER_RECOVERY_ERROR_PATTERNS)


class UIWatchdog:
    """Observe owner deadlines; process termination is supplied by the caller."""

    def __init__(
        self,
        snapshot_provider,
        on_timeout,
        *,
        poll_interval: float = 0.5,
        recovery_grace_seconds: float = UI_OWNER_LOCK_RECOVERY_GRACE_SECONDS,
    ):
        self._snapshot_provider = snapshot_provider
        self._on_timeout = on_timeout
        self._poll_interval = max(0.01, float(poll_interval))
        self._recovery_grace_seconds = max(0.0, float(recovery_grace_seconds))
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
            if self._recovery_grace_seconds:
                log(
                    level="WARNING",
                    message=(
                        f"微信 UI 调用达到截止线：{snapshot.kind}，"
                        f"等待 UI owner 处理异常 {self._recovery_grace_seconds:g} 秒"
                    ),
                )
                if self._stop_event.wait(self._recovery_grace_seconds):
                    return
                current = self._snapshot_provider()
                current_deadline = float(getattr(current, "deadline_at", 0.0) or 0.0)
                if (
                    getattr(current, "kind", "") != getattr(snapshot, "kind", "")
                    or getattr(current, "started_at", 0.0) != getattr(snapshot, "started_at", 0.0)
                    or current_deadline > time.monotonic()
                ):
                    continue
            self._on_timeout(snapshot)
            return


class WeChatRecoveryCoordinator:
    """Own recovery state and choose rebuild, rebind, or process restart."""

    def __init__(
        self,
        *,
        probe_client: Callable[..., Any],
        rebuild_listener: Callable[[], bool],
        set_client: Callable[[Any], None],
        is_client_binding_failure: Callable[[BaseException], bool],
        log_event: Callable[..., None],
        mark_listener_alive: Callable[[], None] | None = None,
        wall_clock: Callable[[], float] = time.time,
        monotonic_clock: Callable[[], float] = time.monotonic,
    ):
        self._probe_client = probe_client
        self._rebuild_listener = rebuild_listener
        self._set_client = set_client
        self._is_client_binding_failure = is_client_binding_failure
        self._log_event = log_event
        self._mark_listener_alive = mark_listener_alive or (lambda: None)
        self._wall_clock = wall_clock
        self._monotonic_clock = monotonic_clock
        self._state = _RecoveryState()
        self._state_lock = threading.RLock()
        self._process_lock = threading.Lock()

    def state_snapshot(self) -> RecoveryStateSnapshot:
        with self._state_lock:
            state = self._state
            return RecoveryStateSnapshot(
                active=state.active,
                attempted=state.attempted,
                probe_after=state.probe_after,
                last_error=state.last_error,
                source=state.source,
                force_rebind=state.force_rebind,
                observation_started_at=state.observation_started_at,
                failed_conversations=frozenset(state.failed_conversations),
                after_rebind=state.after_rebind,
                observation_had_success=state.observation_had_success,
                restart_requested=state.restart_requested,
            )

    def _log(self, level: str, message: str) -> None:
        self._log_event(level=level, message=message)

    @staticmethod
    def _conversation_key(conversation) -> tuple[str, str]:
        return (
            str(getattr(conversation, "chat_type", "private") or "private"),
            str(getattr(conversation, "who", "") or ""),
        )

    def _clear_observation_locked(self) -> None:
        self._state.observation_started_at = 0.0
        self._state.failed_conversations = set()
        self._state.after_rebind = False
        self._state.observation_had_success = False

    def _clear_active_locked(self, *, clear_error: bool = False) -> None:
        self._state.active = False
        self._state.probe_after = 0.0
        self._state.source = ""
        if clear_error:
            self._state.last_error = ""

    def begin_observation(self, *, after_rebind: bool, now: float | None = None) -> None:
        with self._state_lock:
            self._state.observation_started_at = (
                self._monotonic_clock() if now is None else float(now)
            )
            self._state.failed_conversations = set()
            self._state.after_rebind = bool(after_rebind)
            self._state.observation_had_success = True

    def note_listener_operation(self, _conversation, *, now: float | None = None) -> None:
        with self._state_lock:
            started_at = self._state.observation_started_at
            if not started_at:
                return
            now_ts = self._monotonic_clock() if now is None else float(now)
            had_success = self._state.observation_had_success
            if not had_success:
                self._state.observation_started_at = now_ts
            self._state.observation_had_success = True
            self._state.failed_conversations = set()
            if had_success and now_ts - started_at >= LISTENER_RECOVERY_OBSERVATION_SECONDS:
                self._clear_observation_locked()

    def record_local_recovery_failure(self, conversation, *, now: float | None = None) -> str:
        with self._state_lock:
            now_ts = self._monotonic_clock() if now is None else float(now)
            state = self._state
            started_at = state.observation_started_at
            if not started_at:
                return "rebuild"
            key = self._conversation_key(conversation)
            if not state.failed_conversations:
                if (
                    state.observation_had_success
                    and now_ts - started_at >= LISTENER_RECOVERY_OBSERVATION_SECONDS
                ):
                    self._clear_observation_locked()
                    return "rebuild"
                state.observation_started_at = now_ts
                state.observation_had_success = False
                state.failed_conversations = {key}
                return "observe"
            if now_ts - started_at > LISTENER_RECOVERY_OBSERVATION_SECONDS:
                state.observation_started_at = now_ts
                state.failed_conversations = {key}
                return "observe"
            state.failed_conversations.add(key)
            if len(state.failed_conversations) < 2:
                return "observe"
            if state.after_rebind:
                state.restart_requested = True
                return "restart"
            state.force_rebind = True
            return "rebind"

    def arm(self, exc: BaseException, *, source: str = "", now: float | None = None) -> bool:
        if not (
            is_listener_recovery_desktop_error(exc)
            or self._is_client_binding_failure(exc)
        ):
            return False
        now_ts = self._wall_clock() if now is None else float(now)
        with self._state_lock:
            state = self._state
            already_active = state.active
            state.active = True
            state.attempted = False
            state.probe_after = now_ts + LISTENER_RECOVERY_PROBE_INTERVAL_SECONDS
            state.last_error = str(exc or "")
            state.source = str(source or "").strip()
            state.force_rebind = state.force_rebind or self._is_client_binding_failure(exc)
            force_rebind = state.force_rebind
            recovery_source = state.source
        self._mark_listener_alive()
        if not already_active:
            source_text = f"{recovery_source}触发" if recovery_source else "运行时触发"
            recovery_level = "微信客户端重绑" if force_rebind else "监听窗口重建"
            self._log(
                "WARNING",
                (
                    f"自恢复【{recovery_level}】等待：检测到微信 UI 暂时不可操作"
                    f"（{source_text}），将在桌面恢复后执行。原因：{exc}"
                ),
            )
        return True

    def _arm_runtime_recovery(self, conversation) -> bool:
        with self._state_lock:
            state = self._state
            if state.active:
                return False
            state.active = True
            state.attempted = False
            state.probe_after = 0.0
            state.last_error = ""
            force_rebind = state.force_rebind
            who = str(getattr(conversation, "who", "") or "")
            if force_rebind:
                state.source = "监听重建后连续出现跨会话子窗口失败"
            else:
                state.source = f"监听窗口 {who} 局部恢复耗尽"
        if force_rebind:
            message = (
                "自恢复【微信客户端重绑】开始：监听重建后 10 分钟内连续 2 个不同会话的 "
                f"MoveWindow 1400 局部恢复仍失败（最近会话：“{who}”）"
            )
        else:
            message = (
                f"自恢复【监听窗口重建】开始：会话“{who}”的 "
                "MoveWindow 1400 局部恢复仍失败"
            )
        self._log("WARNING", message)
        return True

    def record_listener_recovery_exhausted(
        self,
        conversation,
        *,
        now: float | None = None,
    ) -> str:
        escalation = self.record_local_recovery_failure(conversation, now=now)
        if escalation in {"rebuild", "rebind"}:
            self._arm_runtime_recovery(conversation)
        elif escalation == "restart":
            self._log(
                "ERROR",
                (
                    "自恢复【机器人重启】开始：微信客户端重绑后 10 分钟内连续 2 个不同会话的 "
                    "MoveWindow 1400 局部恢复仍失败"
                ),
            )
        return escalation

    def _schedule_probe_retry(self, exc: BaseException, now_ts: float) -> None:
        with self._state_lock:
            self._state.active = True
            self._state.probe_after = now_ts + LISTENER_RECOVERY_PROBE_INTERVAL_SECONDS
            self._state.last_error = str(exc or "")
        self._mark_listener_alive()

    def _finish_failure(self, exc: BaseException, recovery_level: str, message: str) -> str:
        with self._state_lock:
            self._state.attempted = True
            self._state.last_error = str(exc or "")
            self._clear_active_locked()
        self._log("ERROR", f"自恢复【{recovery_level}】失败：{message}")
        return "failed"

    def process(self, *, now: float | None = None) -> str:
        now_ts = self._wall_clock() if now is None else float(now)
        with self._state_lock:
            if self._state.restart_requested:
                return "restart"
            if not self._state.active:
                return "idle"
            if self._state.probe_after and now_ts < self._state.probe_after:
                return "waiting"
        if not self._process_lock.acquire(blocking=False):
            return "waiting"
        try:
            with self._state_lock:
                force_rebind = self._state.force_rebind
            did_rebind = force_rebind
            try:
                client = (
                    self._probe_client(force_rebind=True)
                    if force_rebind
                    else self._probe_client()
                )
            except Exception as initial_exc:
                recovery_exc = initial_exc
                if self._is_client_binding_failure(initial_exc):
                    with self._state_lock:
                        self._state.force_rebind = True
                    if not did_rebind:
                        did_rebind = True
                        self._log(
                            "WARNING",
                            f"自恢复【微信客户端重绑】开始：客户端探活发现绑定失效。原因：{initial_exc}",
                        )
                    try:
                        client = self._probe_client(force_rebind=True)
                    except Exception as rebind_exc:
                        recovery_exc = rebind_exc
                    else:
                        recovery_exc = None
                if recovery_exc is not None:
                    if is_listener_recovery_desktop_error(recovery_exc):
                        self._schedule_probe_retry(recovery_exc, now_ts)
                        return "waiting"
                    recovery_level = "微信客户端重绑" if did_rebind else "监听窗口重建"
                    return self._finish_failure(
                        recovery_exc,
                        recovery_level,
                        f"客户端探活未通过。原因：{recovery_exc}",
                    )

            self._set_client(client)
            with self._state_lock:
                self._state.attempted = True
                self._state.probe_after = 0.0
            try:
                recovered = self._rebuild_listener()
            except Exception as exc:
                recovery_level = "微信客户端重绑" if did_rebind else "监听窗口重建"
                if is_listener_recovery_desktop_error(exc):
                    self._schedule_probe_retry(exc, self._wall_clock())
                    self._log(
                        "WARNING",
                        f"自恢复【{recovery_level}】等待：桌面暂时不可操作，稍后继续。原因：{exc}",
                    )
                    return "waiting"
                return self._finish_failure(exc, recovery_level, str(exc))

            if recovered:
                with self._state_lock:
                    self._clear_active_locked(clear_error=True)
                    self._state.force_rebind = False
                    begin_observation = did_rebind or not self._state.observation_started_at
                if begin_observation:
                    self.begin_observation(after_rebind=did_rebind)
                self._mark_listener_alive()
                recovery_level = "微信客户端重绑" if did_rebind else "监听窗口重建"
                success = (
                    "客户端已重新初始化，固定监听窗口已恢复"
                    if did_rebind
                    else "固定监听窗口已恢复"
                )
                self._log("SUCCESS", f"自恢复【{recovery_level}】成功：{success}")
                return "recovered"

            recovery_level = "微信客户端重绑" if did_rebind else "监听窗口重建"
            error = RuntimeError(f"{recovery_level}后固定监听窗口仍不可用")
            return self._finish_failure(error, recovery_level, "固定监听窗口未恢复")
        finally:
            self._process_lock.release()

    def watchdog_exit_code(self, snapshot, *, stopped_by_user: bool) -> int:
        kind = str(getattr(snapshot, "kind", "") or "未知动作")
        with self._state_lock:
            self._state.restart_requested = True
            self._state.source = f"微信 UI 动作 {kind} 超过截止线"
            self._state.last_error = self._state.source
        suffix = "；用户已请求停止，因此只恢复面板" if stopped_by_user else ""
        self._log(
            "ERROR",
            f"自恢复【机器人重启】开始：微信 UI 动作 {kind} 已被看门狗判定为卡死{suffix}",
        )
        return UI_STUCK_STOPPED_EXIT_CODE if stopped_by_user else UI_STUCK_EXIT_CODE

    def controlled_restart_exit_code(self, *, owner_idle: bool) -> int | None:
        if not owner_idle:
            return None
        with self._state_lock:
            self._state.restart_requested = True
        self._log(
            "ERROR",
            "自恢复【机器人重启】执行：当前微信操作已结束，正在退出并交给面板重新启动",
        )
        return UI_STUCK_EXIT_CODE

    def status_snapshot(self) -> dict[str, str | bool]:
        state = self.state_snapshot()
        if state.restart_requested:
            status = "restart"
            message = "恢复升级已完成，正在等待当前微信操作结束后重启机器人"
            active = True
        elif state.active:
            status = "waiting"
            active = True
            message = (
                "桌面暂时不可操作，正在等待恢复后自动重绑微信客户端"
                if state.force_rebind
                else "桌面暂时不可操作，正在等待恢复后自动重建监听"
            )
        elif state.attempted and state.last_error:
            status = "failed"
            active = False
            message = "微信 UI 自恢复失败"
        else:
            status = "idle"
            active = False
            message = ""
        return {
            "listener_recovery_active": active,
            "listener_recovery_status": status,
            "listener_recovery_message": message,
            "listener_recovery_source": state.source,
            "listener_recovery_error": state.last_error,
        }
