"""Read-only-ish wxauto voice message probe.

By default this script only reads visible messages. Pass --to-text to call
VoiceMessage.to_text(), which may operate the WeChat UI.
"""

from __future__ import annotations

import argparse
import json
import time
from typing import Any

from wxautox4 import WeChat


def _safe_call(fn, *args, **kwargs):
    try:
        return fn(*args, **kwargs)
    except TypeError:
        return fn(*args)


def _value(obj: Any, name: str) -> Any:
    try:
        return getattr(obj, name, None)
    except Exception as exc:
        return f"<getattr failed: {type(exc).__name__}: {exc}>"


def _raw_summary(raw: Any) -> Any:
    if raw is None:
        return None
    summary = {
        "class": raw.__class__.__name__,
        "name": _value(raw, "Name"),
        "automation_id": _value(raw, "AutomationId"),
        "control_type": str(_value(raw, "ControlTypeName") or _value(raw, "ControlType")),
        "localized_type": _value(raw, "LocalizedControlType"),
    }
    rect = _value(raw, "BoundingRectangle")
    if rect is not None:
        summary["rect"] = str(rect)
    return summary


def _message_summary(message: Any, index: int, call_to_text: bool) -> dict[str, Any]:
    data: dict[str, Any] = {
        "tail_index": index,
        "class": message.__class__.__name__,
        "type": _value(message, "type"),
        "attr": _value(message, "attr"),
        "id": _value(message, "id"),
        "hash": _value(message, "hash"),
        "sender": _value(message, "sender"),
        "content": _value(message, "content"),
        "time": _value(message, "time"),
        "raw": _raw_summary(_value(message, "raw")),
    }
    if call_to_text and callable(getattr(message, "to_text", None)):
        try:
            data["to_text"] = message.to_text()
        except Exception as exc:
            data["to_text_error"] = f"{type(exc).__name__}: {exc}"
    return data


def _open_chat(wx: WeChat, chat_name: str):
    chat = None
    get_subwindow = getattr(wx, "GetSubWindow", None)
    if callable(get_subwindow):
        try:
            chat = get_subwindow(chat_name)
        except Exception:
            chat = None
    if chat is not None and callable(getattr(chat, "GetAllMessage", None)):
        return chat

    chat_with = getattr(wx, "ChatWith", None)
    if callable(chat_with):
        try:
            _safe_call(chat_with, chat_name, exact=True)
        except TypeError:
            _safe_call(chat_with, chat_name)

    if callable(get_subwindow):
        try:
            chat = get_subwindow(chat_name)
        except Exception:
            chat = None
    if chat is not None and callable(getattr(chat, "GetAllMessage", None)):
        return chat
    return wx


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--chat", required=True, help="聊天名，例如：阿英2")
    parser.add_argument("--rounds", type=int, default=6)
    parser.add_argument("--interval", type=float, default=3.0)
    parser.add_argument("--limit", type=int, default=30)
    parser.add_argument("--to-text", action="store_true", help="调用 VoiceMessage.to_text()")
    args = parser.parse_args()

    wx = WeChat()
    chat = _open_chat(wx, args.chat)
    print(f"probe chat={args.chat!r} reader={chat.__class__.__name__} to_text={args.to_text}")
    for round_index in range(1, args.rounds + 1):
        print(f"\n=== round {round_index}/{args.rounds} at {time.strftime('%H:%M:%S')} ===")
        try:
            messages = list((chat.GetAllMessage() or [])[-args.limit :])
        except Exception as exc:
            print(f"GetAllMessage failed: {type(exc).__name__}: {exc}")
            messages = []

        voice_rows = [
            _message_summary(message, index, args.to_text)
            for index, message in enumerate(messages, start=max(0, len(messages) - args.limit))
            if _value(message, "type") == "voice" or callable(getattr(message, "to_text", None))
        ]
        if not voice_rows:
            print("no voice-like messages in visible tail")
        for row in voice_rows:
            print(json.dumps(row, ensure_ascii=False, default=str))
        if round_index < args.rounds:
            time.sleep(args.interval)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
