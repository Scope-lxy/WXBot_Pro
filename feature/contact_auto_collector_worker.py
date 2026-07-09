"""Minimal wxautox4 contact collector used by auto maintenance.

This script is intentionally tiny: it only switches to Contacts, calls
GetFriendDetails, writes raw data to a JSON file, and exits. The parent process
owns the WeChat UI lock, hard timeout, SwitchToChat recovery, and persistence.
"""

from __future__ import annotations

import argparse
import json
import time
import traceback
from pathlib import Path
from typing import Any

from wxautox4 import WeChat


def _clean_text(value: Any) -> str:
    return str(value or "").strip()


def _detail_name(raw_detail: Any) -> str:
    if not isinstance(raw_detail, dict):
        return _clean_text(raw_detail)
    for key in ("备注", "remark", "昵称", "nickname", "name", "微信号", "wechat_id", "wxid"):
        value = _clean_text(raw_detail.get(key))
        if value:
            return value
    return ""


def _contact_name_matches(name: Any, start_name: Any) -> bool:
    start_name = _clean_text(start_name)
    if not start_name:
        return True
    return _clean_text(name).startswith(start_name)


def _write_json(path: str | Path, payload: dict[str, Any]) -> None:
    Path(path).write_text(
        json.dumps(payload, ensure_ascii=False, default=str),
        encoding="utf-8",
    )


def collect(request: dict[str, Any]) -> dict[str, Any]:
    count = max(1, int(request.get("count") or 1))
    start_name = _clean_text(request.get("start_name"))
    callback_names: list[str] = []
    matched_name = ""

    def callback(detail: Any) -> bool:
        nonlocal matched_name
        name = _detail_name(detail)
        matched = _contact_name_matches(name, start_name)
        if matched:
            if not matched_name:
                matched_name = name
            if name:
                callback_names.append(name)
        return matched

    wx = WeChat()
    wx.SwitchToContact()
    kwargs: dict[str, Any] = {
        "n": count,
        "save_head_image": False,
    }
    if start_name:
        kwargs["callback"] = callback
    result = wx.GetFriendDetails(**kwargs)
    return {
        "ok": True,
        "result": result,
        "callback_names": callback_names,
        "matched_name": matched_name,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--request", required=True)
    parser.add_argument("--output", required=True)
    args = parser.parse_args()

    started_at = time.perf_counter()
    try:
        request = json.loads(Path(args.request).read_text(encoding="utf-8"))
        payload = collect(request)
    except Exception as exc:
        payload = {
            "ok": False,
            "error": str(exc),
            "traceback": traceback.format_exc(),
        }
    payload["duration_seconds"] = round(time.perf_counter() - started_at, 3)
    _write_json(args.output, payload)
    return 0 if payload.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
