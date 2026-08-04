"""Lightweight observability helpers for real WeChat UI operations."""

from __future__ import annotations

from contextlib import contextmanager
import time

from core.logger import log

SLOW_WECHAT_UI_ACTION_INFO_SECONDS = 30.0
SLOW_WECHAT_UI_ACTION_WARNING_SECONDS = 60.0


def log_wechat_ui_action_timing(
    label: str,
    *,
    total_seconds: float,
    owner_queue_seconds: float,
    ui_lock_seconds: float,
    handler_seconds: float,
    handler_invoked: bool,
    error: BaseException | None = None,
):
    total_seconds = max(0.0, float(total_seconds or 0.0))
    owner_queue_seconds = max(0.0, float(owner_queue_seconds or 0.0))
    ui_lock_seconds = max(0.0, float(ui_lock_seconds or 0.0))
    handler_seconds = max(0.0, float(handler_seconds or 0.0))
    if error is not None:
        level = "WARNING"
        result = "失败" if handler_invoked else "未开始"
    elif total_seconds > SLOW_WECHAT_UI_ACTION_WARNING_SECONDS:
        level = "WARNING"
        result = "完成"
    elif total_seconds >= SLOW_WECHAT_UI_ACTION_INFO_SECONDS:
        level = "INFO"
        result = "完成"
    else:
        level = "DEBUG"
        result = "完成"

    message = (
        f"微信窗口耗时：{label}{result}，共 {total_seconds:.1f}s"
        f"（排队 {owner_queue_seconds:.1f}s，等待微信 {ui_lock_seconds:.1f}s，"
        f"实际操作 {handler_seconds:.1f}s）"
    )
    if error is not None:
        error_text = " ".join(str(error or "").split())
        if error_text:
            message += f"；原因：{error_text}"
    log(level=level, message=message)


@contextmanager
def warn_slow_wechat_ui_action(
    label: str,
    *,
    info_threshold: float = SLOW_WECHAT_UI_ACTION_INFO_SECONDS,
    warning_threshold: float = SLOW_WECHAT_UI_ACTION_WARNING_SECONDS,
):
    started_at = time.perf_counter()
    try:
        yield
    finally:
        elapsed = time.perf_counter() - started_at
        if elapsed >= float(info_threshold or SLOW_WECHAT_UI_ACTION_INFO_SECONDS):
            level = (
                "WARNING"
                if elapsed > float(warning_threshold or SLOW_WECHAT_UI_ACTION_WARNING_SECONDS)
                else "INFO"
            )
            log(
                level=level,
                message=(
                    f"[微信UI慢操作] {label} 耗时 {elapsed:.1f}s，"
                    "可能正在等待微信 UI/语音发送/弹窗操作完成"
                ),
            )
