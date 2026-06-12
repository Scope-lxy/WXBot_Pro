"""Small helpers for WeChat desktop window recovery."""

from __future__ import annotations

import os
import time
from collections.abc import Iterable


MOUSEEVENTF_LEFTDOWN = 0x0002
MOUSEEVENTF_LEFTUP = 0x0004
SW_RESTORE = 9
SW_SHOW = 5
HWND_TOPMOST = -1
HWND_NOTOPMOST = -2
SWP_NOSIZE = 0x0001
SWP_NOMOVE = 0x0002
SWP_SHOWWINDOW = 0x0040
WECHAT_AUTO_RESIZE_SIZE = (1000, 6000)


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


def bring_top_windows_to_front(title: str, *, wait: float = 0.3) -> int:
    handles = top_window_handles_by_title(title, visible_only=False)
    if not handles:
        return 0
    if os.name != "nt":
        return 0
    try:
        import ctypes
    except Exception:
        return 0

    user32 = ctypes.windll.user32
    shown = 0
    flags = SWP_NOMOVE | SWP_NOSIZE | SWP_SHOWWINDOW
    for hwnd in handles:
        try:
            hwnd = int(hwnd)
            user32.ShowWindow(hwnd, SW_RESTORE)
            user32.ShowWindow(hwnd, SW_SHOW)
            user32.SetWindowPos(hwnd, HWND_TOPMOST, 0, 0, 0, 0, flags)
            user32.SetWindowPos(hwnd, HWND_NOTOPMOST, 0, 0, 0, 0, flags)
            user32.BringWindowToTop(hwnd)
            user32.SetForegroundWindow(hwnd)
            shown += 1
        except Exception:
            continue
    if shown and wait:
        time.sleep(max(0.0, float(wait)))
    return shown


def bring_wechat_main_window_to_front(*, wait: float = 0.3) -> int:
    return bring_top_windows_to_front("微信", wait=wait) + bring_top_windows_to_front("WeChat", wait=wait)


def move_cursor_to_wechat_main_window_center(*, wait: float = 0.05) -> bool:
    if os.name != "nt":
        return False
    handles = top_window_handles_by_title("微信", visible_only=False) or top_window_handles_by_title("WeChat", visible_only=False)
    if not handles:
        return False
    try:
        import ctypes
        from ctypes import wintypes
    except Exception:
        return False

    user32 = ctypes.windll.user32
    hwnd = int(handles[0])
    rect = wintypes.RECT()
    try:
        if not user32.GetWindowRect(hwnd, ctypes.byref(rect)):
            return False
        width = max(1, int(rect.right - rect.left))
        height = max(1, int(rect.bottom - rect.top))
        x = int(rect.left + width * 0.5)
        y = int(rect.top + height * 0.5)
        user32.SetCursorPos(x, y)
        if wait:
            time.sleep(max(0.0, float(wait)))
        return True
    except Exception:
        return False


def click_wechat_main_window_chat_nav(*, wait: float = 0.1) -> bool:
    if os.name != "nt":
        return False
    handles = top_window_handles_by_title("微信", visible_only=False) or top_window_handles_by_title("WeChat", visible_only=False)
    if not handles:
        return False
    try:
        import ctypes
        from ctypes import wintypes
    except Exception:
        return False

    user32 = ctypes.windll.user32
    hwnd = int(handles[0])
    rect = wintypes.RECT()
    point = wintypes.POINT()
    try:
        if not user32.GetWindowRect(hwnd, ctypes.byref(rect)):
            return False
        user32.GetCursorPos(ctypes.byref(point))
        width = max(1, int(rect.right - rect.left))
        height = max(1, int(rect.bottom - rect.top))
        x = int(rect.left + min(32, max(12, width - 12)))
        y = int(rect.top + min(110, max(42, height - 12)))
        user32.ShowWindow(hwnd, SW_RESTORE)
        user32.ShowWindow(hwnd, SW_SHOW)
        user32.SetForegroundWindow(hwnd)
        user32.SetCursorPos(x, y)
        user32.mouse_event(MOUSEEVENTF_LEFTDOWN, 0, 0, 0, 0)
        user32.mouse_event(MOUSEEVENTF_LEFTUP, 0, 0, 0, 0)
        user32.SetCursorPos(int(point.x), int(point.y))
        if wait:
            time.sleep(max(0.0, float(wait)))
        return True
    except Exception:
        return False


def rebind_wechat_client(bot, *, versions: Iterable[str] = ("微信", "WeChat")):
    last_exc = None
    for version_name in versions:
        try:
            from wxautox4.param import WxParam
            from wxautox4 import WeChat

            WxParam.CHAT_WINDOW_SIZE = WECHAT_AUTO_RESIZE_SIZE
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
