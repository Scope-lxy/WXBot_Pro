"""Lightweight observability helpers for real WeChat UI operations."""

from __future__ import annotations

from contextlib import contextmanager
import time

from core.logger import log

SLOW_WECHAT_UI_ACTION_SECONDS = 10.0


@contextmanager
def warn_slow_wechat_ui_action(label: str, *, threshold: float = SLOW_WECHAT_UI_ACTION_SECONDS):
    started_at = time.perf_counter()
    try:
        yield
    finally:
        elapsed = time.perf_counter() - started_at
        if elapsed >= float(threshold or SLOW_WECHAT_UI_ACTION_SECONDS):
            log(
                level="WARNING",
                message=(
                    f"[微信UI慢操作] {label} 耗时 {elapsed:.1f}s，"
                    "可能正在等待微信 UI/语音发送/弹窗操作完成"
                ),
            )
