from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import threading
import time
import uuid
import wave
from datetime import datetime
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.wechat_ui_actions import UIIntent, UIIntentKind, WeChatUIOwner
from core.wechat_ui_runtime import WeChatUIRuntime


REPORT_ROOT = Path("backups") / "contact_owner_queue"
DEFAULT_LIGHT_CONTACT = "\u963f\u82f12"
DEFAULT_AUDIO_CONTACT = "\u70b34"


def stamp() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]


def run_label() -> str:
    return datetime.now().strftime("%Y%m%d_%H%M%S")


def write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, default=str),
        encoding="utf-8",
    )


def create_silent_wav(path: Path, duration_seconds: float = 1.0) -> dict[str, Any]:
    sample_rate = 16000
    frame_count = max(1, int(sample_rate * duration_seconds))
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as audio:
        audio.setnchannels(1)
        audio.setsampwidth(2)
        audio.setframerate(sample_rate)
        audio.writeframes(b"\x00\x00" * frame_count)
    content = path.read_bytes()
    return {
        "created_at": stamp(),
        "path": str(path.resolve()),
        "duration_seconds": round(frame_count / sample_rate, 3),
        "size_bytes": len(content),
        "sha256": hashlib.sha256(content).hexdigest(),
    }


def wait_for(predicate, timeout: float, label: str) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.05)
    raise TimeoutError(label)


def run_probe(args: argparse.Namespace) -> int:
    if not args.confirm_send:
        raise SystemExit("This probe sends two messages. Re-run with --confirm-send.")

    report_dir = REPORT_ROOT / run_label()
    report_path = report_dir / "report.json"
    audio_path = report_dir / "owner_queue_probe.wav"
    report: dict[str, Any] = {
        "started_at": stamp(),
        "settings": {
            "contact_count": 50,
            "light_contact": args.light_contact,
            "audio_contact": args.audio_contact,
        },
        "audio_preparation": create_silent_wav(audio_path),
        "events": [],
        "inbound_callbacks": [],
    }
    local_ffmpeg_bin = ROOT / "venv" / "tools" / "ffmpeg" / "bin"
    if (local_ffmpeg_bin / "ffmpeg.exe").exists():
        os.environ["PATH"] = f"{local_ffmpeg_bin}{os.pathsep}{os.environ.get('PATH', '')}"
    report["ffmpeg"] = {
        "local_bin": str(local_ffmpeg_bin.resolve()),
        "available": (local_ffmpeg_bin / "ffmpeg.exe").exists(),
    }
    write_json(report_path, report)

    event_lock = threading.Lock()

    def record(event: str, **details: Any) -> None:
        with event_lock:
            report["events"].append({"event": event, "at": stamp(), "monotonic": time.monotonic(), **details})
            write_json(report_path, report)

    def on_message(conversation, message) -> None:
        with event_lock:
            report["inbound_callbacks"].append({
                "at": stamp(),
                "conversation": str(getattr(conversation, "who", "") or ""),
                "chat_type": str(getattr(conversation, "chat_type", "") or ""),
                "message_type": str(getattr(message, "type", "") or ""),
                "message_attr": str(getattr(message, "attr", "") or ""),
            })
            write_json(report_path, report)

    runtime = WeChatUIRuntime(on_message)
    handlers = runtime.handlers()
    wrapped_handlers = {}
    for kind, handler in handlers.items():
        def wrapped(payload, *, _kind=kind, _handler=handler):
            record(f"handler_start:{_kind.value}")
            try:
                return _handler(payload)
            finally:
                record(f"handler_end:{_kind.value}")

        wrapped_handlers[kind] = wrapped

    owner = WeChatUIOwner(wrapped_handlers, poll_interval=0.05)
    runtime.set_heartbeat(owner.heartbeat_current_action)
    owner.start()
    bootstrapped = False
    contact_ticket = None
    try:
        bootstrap = owner.call(
            UIIntent(
                UIIntentKind.BOOTSTRAP,
                {"listeners": [args.light_contact, args.audio_contact]},
            ),
            60,
        )
        bootstrapped = True
        report["bootstrap"] = bootstrap
        record("bootstrap_complete")

        contact_ticket = owner.submit(UIIntent(UIIntentKind.CONTACT_START, {"start_name": "", "count": 50}))
        wait_for(lambda: owner.contact_active, 15, "contact collector did not become active")
        record("contact_active")

        audio_ticket = owner.submit(UIIntent(
            UIIntentKind.SEND_AUDIO,
            {
                "conversation": args.audio_contact,
                "path": str(audio_path.resolve()),
                "delivery_id": f"contact-owner-queue-audio:{uuid.uuid4()}",
            },
        ))
        marker = f"[WXBot owner queue probe] {run_label()} Please ignore."
        text_ticket = owner.submit(UIIntent(
            UIIntentKind.SEND_TEXT,
            {"conversation": args.light_contact, "text": marker},
        ))

        report["light_send_result"] = str(text_ticket.result(30) or "")
        report["exclusive_audio_done_while_contact_active"] = bool(audio_ticket.done)
        report["contact_active_after_light_send"] = bool(owner.contact_active)
        record(
            "light_send_complete",
            contact_active=owner.contact_active,
            audio_done=audio_ticket.done,
        )

        contact_result = contact_ticket.result(310)
        contact_items = list((contact_result or {}).get("result") or [])
        report["contact_result"] = {
            "ok": bool((contact_result or {}).get("ok")),
            "result_count": len(contact_items),
            "raw_result_count": int((contact_result or {}).get("raw_result_count") or 0),
            "duration_seconds": float((contact_result or {}).get("duration_seconds") or 0),
            "cursor_candidate_count": len((contact_result or {}).get("cursor_candidates") or []),
        }
        record("contact_complete")
        report["audio_send_result"] = str(audio_ticket.result(190) or "")
        record("audio_complete")
        messages = owner.call(
            UIIntent(UIIntentKind.GET_MESSAGES, {"conversation": args.audio_contact}),
            60,
        )
        tail = list(messages or [])[-12:]
        visible_self_voice = any(
            str(getattr(message, "attr", "") or "").lower() == "self"
            and str(getattr(message, "type", "") or "").lower() in {"voice", "audio"}
            for message in tail
        )
        report["delivery_verification"] = {
            "method": "owner_get_messages_tail",
            "tail_count": len(tail),
            "self_voice_in_tail": visible_self_voice,
        }
        record("audio_visibility_checked", self_voice_in_tail=visible_self_voice)

        event_names = [item["event"] for item in report["events"]]
        required_order = [
            "contact_active",
            "handler_start:send_text",
            "handler_end:send_text",
            "handler_start:contact_recover",
            "handler_end:contact_recover",
            "handler_start:send_audio",
            "handler_end:send_audio",
        ]
        positions = []
        for name in required_order:
            start = positions[-1] + 1 if positions else 0
            positions.append(event_names.index(name, start))
        report["assertions"] = {
            "light_send_finished_during_contact": report["contact_active_after_light_send"],
            "audio_waited_for_contact": not report["exclusive_audio_done_while_contact_active"],
            "recovery_preceded_audio": positions == sorted(positions),
            "contact_returned_50": report["contact_result"]["result_count"] == 50,
            "audio_visible_in_tail": visible_self_voice,
        }
        report["status"] = "passed" if all(report["assertions"].values()) else "failed"
    except Exception as exc:
        report["status"] = "failed"
        report["error"] = f"{type(exc).__name__}: {exc}"
    finally:
        if owner.contact_active:
            owner.terminate_active_contact_job()
            if contact_ticket is not None:
                try:
                    contact_ticket.result(15)
                except Exception:
                    pass
        if bootstrapped:
            try:
                owner.call_shutdown(30)
            except Exception as exc:
                report["shutdown_error"] = f"{type(exc).__name__}: {exc}"
        owner.stop(timeout=10)
        report["completed_at"] = stamp()
        write_json(report_path, report)

    print(json.dumps(report, ensure_ascii=False, indent=2), flush=True)
    print(report_path.resolve(), flush=True)
    return 0 if report.get("status") == "passed" else 2


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Verify owner scheduling around a real 50-contact batch")
    parser.add_argument("--light-contact", default=DEFAULT_LIGHT_CONTACT)
    parser.add_argument("--audio-contact", default=DEFAULT_AUDIO_CONTACT)
    parser.add_argument("--confirm-send", action="store_true")
    return parser


if __name__ == "__main__":
    raise SystemExit(run_probe(build_parser().parse_args()))
