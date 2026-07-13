"""Small helpers for durable local file replacement on Windows."""

from __future__ import annotations

import errno
import os
import time


TRANSIENT_REPLACE_WINERRORS = {5, 32, 33}
TRANSIENT_REPLACE_ERRNOS = {errno.EACCES, errno.EBUSY, errno.EPERM}
DEFAULT_REPLACE_RETRY_DELAYS = (0.05, 0.1, 0.2, 0.4)


def is_transient_replace_error(exc: BaseException) -> bool:
    return (
        getattr(exc, "winerror", None) in TRANSIENT_REPLACE_WINERRORS
        or getattr(exc, "errno", None) in TRANSIENT_REPLACE_ERRNOS
    )


def replace_with_retry(source, destination, *, delays=DEFAULT_REPLACE_RETRY_DELAYS) -> None:
    """Replace a file, retrying only transient Windows sharing/access errors."""
    retry_delays = tuple(max(0.0, float(delay)) for delay in delays)
    for attempt in range(len(retry_delays) + 1):
        try:
            os.replace(source, destination)
            return
        except OSError as exc:
            if attempt >= len(retry_delays) or not is_transient_replace_error(exc):
                raise
            time.sleep(retry_delays[attempt])
