"""Live probe for callback replay during non-scrolling tail snapshots."""

from __future__ import annotations

import hashlib
import json
import threading
import time

from core.memory_context_repair import normalize_wechat_snapshot


TARGET = "阿英2"


def _semantic(entry):
    payload = [
        str(entry.get("attr") or ""),
        str(entry.get("sender") or ""),
        str(entry.get("type") or ""),
        str(entry.get("content") or ""),
        str(entry.get("time") or ""),
    ]
    return hashlib.sha256(
        json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def main():
    from wxautox4 import WeChat

    callbacks = []
    callback_lock = threading.Lock()

    def callback(message, chat):
        with callback_lock:
            callbacks.append({
                "conversation": str(getattr(chat, "who", "") or ""),
                "thread_id": threading.get_ident(),
                "type": str(getattr(message, "type", "") or ""),
            })
        return True

    wx = WeChat(version="微信")
    existing = [
        str(getattr(chat, "who", "") or "").strip()
        for chat in (wx.GetAllSubWindow() or [])
    ]
    if TARGET in existing:
        raise RuntimeError(f"{TARGET} 已存在子窗口")

    added = False
    try:
        wx.StartListening()
        chat = wx.AddListenChat(nickname=TARGET, callback=callback)
        if not chat or isinstance(chat, dict):
            raise RuntimeError(f"建窗失败：{chat}")
        if str(getattr(chat, "who", "") or "").strip() != TARGET:
            raise RuntimeError(f"建窗目标不匹配：{getattr(chat, 'who', '')}")
        added = True
        time.sleep(1.0)
        callbacks_during_add = len(callbacks)

        reader_thread = threading.get_ident()
        started = time.monotonic()
        first_raw = list(chat.GetAllMessage() or [])
        first_elapsed = time.monotonic() - started
        time.sleep(1.0)
        callbacks_after_first = len(callbacks) - callbacks_during_add

        started = time.monotonic()
        second_raw = list(chat.GetAllMessage() or [])
        second_elapsed = time.monotonic() - started
        time.sleep(1.0)
        callbacks_after_second = len(callbacks) - callbacks_during_add - callbacks_after_first

        first = normalize_wechat_snapshot(first_raw, source="wechat_context_repair")
        second = normalize_wechat_snapshot(second_raw, source="wechat_context_repair")
        first_semantic = [_semantic(entry) for entry in first]
        second_semantic = [_semantic(entry) for entry in second]
        first_ids = [str(entry.get("message_id") or "") for entry in first]
        second_ids = [str(entry.get("message_id") or "") for entry in second]
        first_hashes = [
            (str(entry.get("native_hash") or ""), str(entry.get("native_hash_text") or ""))
            for entry in first
        ]
        second_hashes = [
            (str(entry.get("native_hash") or ""), str(entry.get("native_hash_text") or ""))
            for entry in second
        ]
        callback_threads = [int(item["thread_id"]) for item in callbacks]
        result = {
            "target": TARGET,
            "preexisting_subwindows": existing,
            "callbacks_during_add": callbacks_during_add,
            "callbacks_during_first_get_all": callbacks_after_first,
            "callbacks_during_second_get_all": callbacks_after_second,
            "callback_thread_ids": sorted(set(callback_threads)),
            "callbacks_off_reader_thread": sum(1 for value in callback_threads if value != reader_thread),
            "first_raw_count": len(first_raw),
            "first_usable_count": len(first),
            "first_elapsed_seconds": round(first_elapsed, 3),
            "second_raw_count": len(second_raw),
            "second_usable_count": len(second),
            "second_elapsed_seconds": round(second_elapsed, 3),
            "semantic_snapshots_equal": first_semantic == second_semantic,
            "message_id_snapshots_equal": first_ids == second_ids,
            "hash_snapshots_equal": first_hashes == second_hashes,
        }
        print(json.dumps(result, ensure_ascii=False, indent=2))
    finally:
        if added:
            try:
                wx.RemoveListenChat(nickname=TARGET)
            except Exception:
                pass
            try:
                residual = wx.GetSubWindow(nickname=TARGET)
                close = getattr(residual, "Close", None)
                if callable(close):
                    close()
            except Exception:
                pass


if __name__ == "__main__":
    main()
