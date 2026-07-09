"""Centralized helpers for WeChat UI action locking.

Production WXBot instances must expose ``_get_wechat_action_lock``. Small
unit-test doubles that do not touch real WeChat UI may omit it and are treated
as already safe.
"""

from __future__ import annotations

from contextlib import contextmanager
from typing import Any, Callable, Iterator


ReleaseLock = Callable[[], None]


def _noop_release() -> None:
    return None


def _lock_for(bot: Any) -> Any | None:
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
