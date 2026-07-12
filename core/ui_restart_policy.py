"""Rolling restart limit used by the Windows launcher after WeChat UI stalls."""

from __future__ import annotations

import argparse
import os
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path


def _as_utc(value):
    if value is None:
        return datetime.now(timezone.utc)
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _parse_utc(value):
    try:
        return _as_utc(datetime.fromisoformat(str(value or "").strip()))
    except (TypeError, ValueError):
        return None


def _save_history(path, values):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    content = "".join(f"{value.isoformat()}\n" for value in values)
    fd, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_name, path)
    finally:
        if os.path.exists(temp_name):
            os.unlink(temp_name)


def allow_restart(path, *, now=None, window_minutes=30, max_restarts=3):
    now = _as_utc(now)
    cutoff = now - timedelta(minutes=max(1, int(window_minutes)))
    history_path = Path(path)
    valid = []
    if history_path.exists():
        try:
            lines = history_path.read_text(encoding="utf-8-sig").splitlines()
        except OSError:
            lines = []
        for line in lines:
            parsed = _parse_utc(line)
            if parsed is not None and parsed >= cutoff:
                valid.append(parsed)
    allowed = len(valid) < max(1, int(max_restarts))
    if allowed:
        valid.append(now)
    _save_history(history_path, valid)
    return allowed


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--path", required=True)
    parser.add_argument("--window-minutes", type=int, default=30)
    parser.add_argument("--max-restarts", type=int, default=3)
    args = parser.parse_args(argv)
    return 0 if allow_restart(
        args.path,
        window_minutes=args.window_minutes,
        max_restarts=args.max_restarts,
    ) else 1


if __name__ == "__main__":
    raise SystemExit(main())
