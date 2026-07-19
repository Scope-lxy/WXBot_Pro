"""Lightweight observability helpers for real WeChat UI operations."""

from __future__ import annotations

from contextlib import contextmanager
import time

from core.logger import log

SLOW_WECHAT_UI_ACTION_INFO_SECONDS = 30.0
SLOW_WECHAT_UI_ACTION_WARNING_SECONDS = 60.0


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
