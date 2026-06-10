"""Small helpers for WeChat desktop window recovery."""

from __future__ import annotations

import os
import time
from collections.abc import Iterable


WM_CLOSE = 0x0010


def top_window_handles_by_title(title: str, *, visible_only: bool = True) -> list[int]:
    if os.name != "nt":
        return []
    try:
        import ctypes
        from ctypes import wintypes
    except Exception:
        return []

    user32 = ctypes.windll.user32
    enum_proc = ctypes.WINFUNCTYPE(ctypes.c_bool, wintypes.HWND, wintypes.LPARAM)
    wanted = str(title or "").strip()
    handles: list[int] = []

    def window_title(hwnd):
        length = user32.GetWindowTextLengthW(hwnd)
        if length <= 0:
            return ""
        buffer = ctypes.create_unicode_buffer(length + 1)
        user32.GetWindowTextW(hwnd, buffer, length + 1)
        return buffer.value

    def callback(hwnd, _lparam):
        if visible_only and not bool(user32.IsWindowVisible(hwnd)):
            return True
        if window_title(hwnd) == wanted:
            handles.append(int(hwnd))
        return True

    user32.EnumWindows(enum_proc(callback), 0)
    return handles


def close_top_windows_by_title(title: str, *, visible_only: bool = True, wait: float = 0.3) -> int:
    handles = top_window_handles_by_title(title, visible_only=visible_only)
    if not handles:
        return 0
    try:
        import ctypes
    except Exception:
        return 0

    user32 = ctypes.windll.user32
    closed = 0
    for hwnd in handles:
        try:
            user32.PostMessageW(int(hwnd), WM_CLOSE, 0, 0)
            closed += 1
        except Exception:
            continue
    if closed and wait:
        time.sleep(max(0.0, float(wait)))
    return closed


def rebind_wechat_client(bot, *, versions: Iterable[str] = ("微信", "WeChat")):
    last_exc = None
    for version_name in versions:
        try:
            from wxautox4 import WeChat

            bot.wx = WeChat(version=version_name)
            return bot.wx
        except Exception as exc:
            last_exc = exc
    if last_exc:
        raise last_exc
    raise RuntimeError("未能初始化微信客户端")


def run_with_wechat_rebind_retry(
    bot,
    action,
    *,
    cleanup=None,
    attempts: int = 2,
    on_retry=None,
):
    last_exc = None
    for attempt in range(1, max(1, int(attempts or 1)) + 1):
        if callable(cleanup):
            cleanup()
        try:
            return action()
        except Exception as exc:
            last_exc = exc
            if attempt >= max(1, int(attempts or 1)):
                raise
            if callable(on_retry):
                on_retry(exc, attempt)
            if callable(cleanup):
                cleanup()
            rebind_wechat_client(bot)
    if last_exc:
        raise last_exc
    return None
