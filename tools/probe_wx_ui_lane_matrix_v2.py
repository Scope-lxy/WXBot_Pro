from __future__ import annotations

import argparse
import json
import math
import os
import struct
import subprocess
import sys
import threading
import time
import wave
from datetime import datetime
from pathlib import Path
from typing import Any


DEFAULT_CONTACTS = [
    "\u963f\u82f12",
    "\u963f\u82f13",
    "\u963f\u82f14",
    "\u70b33",
    "\u70b34",
]
DEFAULT_FFMPEG_DIR = Path("venv") / "tools" / "ffmpeg" / "bin"
REPORT_ROOT = Path("backups") / "ui_lane_probe_v2"
ASSET_ROOT = REPORT_ROOT / "assets"


def clean(value: Any) -> str:
    return str(value or "").strip()


def timestamp() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]


def run_label() -> str:
    return datetime.now().strftime("%Y%m%d_%H%M%S")


def write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, default=str),
        encoding="utf-8",
    )


def make_assets(voice_seconds: int) -> dict[str, str]:
    ASSET_ROOT.mkdir(parents=True, exist_ok=True)
    voice_seconds = max(2, int(voice_seconds or 3))
    wav_path = ASSET_ROOT / f"lane_probe_{voice_seconds}s.wav"
    txt_path = ASSET_ROOT / "lane_probe.txt"
    txt_path.write_text(
        "WXBot UI lane probe file. Please ignore.\n" + timestamp() + "\n",
        encoding="utf-8",
    )
    sample_rate = 16000
    with wave.open(str(wav_path), "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(sample_rate)
        for index in range(sample_rate * voice_seconds):
            value = int(9000 * math.sin(2 * math.pi * 440 * index / sample_rate))
            wav.writeframesraw(struct.pack("<h", value))
    return {
        "voice": str(wav_path.resolve()),
        "file": str(txt_path.resolve()),
        "voice_seconds": str(voice_seconds),
    }


def configure_ffmpeg_path() -> str:
    ffmpeg_dir = DEFAULT_FFMPEG_DIR.resolve()
    if not (ffmpeg_dir / "ffmpeg.exe").exists():
        return ""
    current = os.environ.get("PATH", "")
    value = str(ffmpeg_dir)
    if value.lower() not in {item.lower() for item in current.split(os.pathsep) if item}:
        os.environ["PATH"] = value + os.pathsep + current
    return value


CASES: dict[str, list[tuple[str, float]]] = {
    "baseline_contact_scan": [("main.contact_scan", 0.0)],
    "baseline_child_read": [("child.read.0", 0.0)],
    "baseline_child_text": [("child.text.0", 0.0)],
    "baseline_child_audio": [("child.audio.1", 0.0)],
    "baseline_child_file": [("child.file.1", 0.0)],
    "baseline_main_history": [("main.history.0", 0.0)],
    "contact_vs_child_read": [("main.contact_scan", 0.0), ("child.read.0", 2.0)],
    "contact_vs_child_text": [("main.contact_scan", 0.0), ("child.text.0", 2.0)],
    "contact_vs_child_audio": [("main.contact_scan", 0.0), ("child.audio.1", 2.0)],
    "contact_vs_child_file": [("main.contact_scan", 0.0), ("child.file.1", 2.0)],
    "contact_vs_main_history": [("main.contact_scan", 0.0), ("main.history.0", 2.0)],
    "history_vs_child_read": [("main.history.0", 0.0), ("child.read.1", 1.0)],
    "child_audio_vs_text": [("child.audio.1", 0.0), ("child.text.0", 0.0)],
    "child_file_vs_text": [("child.file.1", 0.0), ("child.text.0", 0.0)],
    "parallel_child_reads": [("child.read.0", 0.0), ("child.read.1", 0.0)],
    "parallel_child_texts": [("child.text.0", 0.0), ("child.text.1", 0.0)],
    "parallel_three_child_reads": [
        ("child.read.0", 0.0),
        ("child.read.1", 0.0),
        ("child.read.2", 0.0),
    ],
    "parallel_three_child_texts": [
        ("child.text.0", 0.0),
        ("child.text.1", 0.0),
        ("child.text.2", 0.0),
    ],
}


def operation_family(name: str) -> str:
    parts = name.split(".")
    return ".".join(parts[:2])


def operation_contact(name: str, contacts: list[str]) -> str:
    parts = name.split(".")
    if len(parts) < 3:
        return contacts[0]
    try:
        return contacts[int(parts[2]) % len(contacts)]
    except (TypeError, ValueError):
        return contacts[0]


def get_child_chat(wx: Any, contact: str) -> Any:
    get_subwindow = getattr(wx, "GetSubWindow", None)
    chat = get_subwindow(contact) if callable(get_subwindow) else None
    if chat is None:
        chat = wx.AddListenChat(
            nickname=contact,
            callback=lambda *_args, **_kwargs: None,
        )
    if chat is None or clean(getattr(chat, "who", "")) != contact:
        raise RuntimeError(f"child window verification failed: {contact}")
    return chat


def prepare_subwindows(contacts: list[str]) -> list[dict[str, Any]]:
    from wxautox4 import WeChat

    wx = WeChat()
    results = []
    for contact in contacts[:3]:
        started = time.perf_counter()
        try:
            chat = get_child_chat(wx, contact)
            results.append(
                {
                    "contact": contact,
                    "ok": True,
                    "who": clean(getattr(chat, "who", "")),
                    "elapsed_seconds": round(time.perf_counter() - started, 3),
                }
            )
        except Exception as exc:
            results.append(
                {
                    "contact": contact,
                    "ok": False,
                    "error": f"{type(exc).__name__}: {exc}",
                    "elapsed_seconds": round(time.perf_counter() - started, 3),
                }
            )
    return results


def execute_operation(
    name: str,
    contacts: list[str],
    assets: dict[str, str],
    scan_count: int,
    history_count: int,
    marker: str,
) -> Any:
    from wxautox4 import WeChat

    wx = WeChat()
    family = operation_family(name)
    contact = operation_contact(name, contacts)
    if family == "main.contact_scan":
        wx.SwitchToContact()
        return len(
            wx.GetFriendDetails(
                n=max(1, int(scan_count)),
                timeout=60,
                save_head_image=False,
                interval=0,
                speed=5,
            )
            or []
        )
    if family == "main.history":
        wx.ChatWith(contact, exact=True)
        return len(
            wx.GetHistoryMessage(
                max(1, int(history_count)),
                interval=0.2,
                speed=3,
                goback=True,
            )
            or []
        )
    chat = get_child_chat(wx, contact)
    if family == "child.read":
        return len(chat.GetAllMessage() or [])
    if family == "child.text":
        return chat.SendMsg(f"[WXBot UI lane probe] {marker}. Please ignore.")
    if family == "child.audio":
        return chat.SendAudio(
            assets["voice"],
            duration=int(assets["voice_seconds"]),
        )
    if family == "child.file":
        return chat.SendFiles(assets["file"])
    raise KeyError(name)


def run_worker(
    name: str,
    delay: float,
    start_event: threading.Event,
    output: dict[str, dict[str, Any]],
    output_lock: threading.Lock,
    contacts: list[str],
    assets: dict[str, str],
    scan_count: int,
    history_count: int,
    marker: str,
) -> None:
    import pythoncom

    pythoncom.CoInitialize()
    try:
        start_event.wait()
        if delay > 0:
            time.sleep(delay)
        started_monotonic = time.perf_counter()
        record: dict[str, Any] = {
            "name": name,
            "family": operation_family(name),
            "delay_seconds": delay,
            "started_at": timestamp(),
            "started_monotonic": started_monotonic,
        }
        try:
            result = execute_operation(
                name,
                contacts,
                assets,
                scan_count,
                history_count,
                marker,
            )
            record["ok"] = True
            record["result"] = clean(result)[:500]
        except Exception as exc:
            record["ok"] = False
            record["error"] = f"{type(exc).__name__}: {exc}"
        ended_monotonic = time.perf_counter()
        record["ended_at"] = timestamp()
        record["ended_monotonic"] = ended_monotonic
        record["elapsed_seconds"] = round(ended_monotonic - started_monotonic, 3)
        with output_lock:
            output[name] = record
    finally:
        pythoncom.CoUninitialize()


def overlap_seconds(records: list[dict[str, Any]]) -> float:
    if len(records) < 2:
        return 0.0
    latest_start = max(float(item["started_monotonic"]) for item in records)
    earliest_end = min(float(item["ended_monotonic"]) for item in records)
    return round(max(0.0, earliest_end - latest_start), 3)


def post_case_probe(contact: str) -> dict[str, Any]:
    import pythoncom
    from wxautox4 import WeChat

    pythoncom.CoInitialize()
    started = time.perf_counter()
    try:
        wx = WeChat()
        wx.SwitchToChat()
        chat = get_child_chat(wx, contact)
        count = len(chat.GetAllMessage() or [])
        return {
            "ok": True,
            "message_count": count,
            "elapsed_seconds": round(time.perf_counter() - started, 3),
        }
    except Exception as exc:
        return {
            "ok": False,
            "error": f"{type(exc).__name__}: {exc}",
            "elapsed_seconds": round(time.perf_counter() - started, 3),
        }
    finally:
        pythoncom.CoUninitialize()


def run_child(args: argparse.Namespace) -> int:
    contacts = [clean(item) for item in args.contacts if clean(item)]
    case_ops = CASES[args.child_case]
    assets = json.loads(args.assets_json)
    prepared = prepare_subwindows(contacts)
    output: dict[str, dict[str, Any]] = {}
    output_lock = threading.Lock()
    start_event = threading.Event()
    threads = []
    marker = f"{args.child_case}-{run_label()}"
    for name, delay in case_ops:
        thread = threading.Thread(
            target=run_worker,
            args=(
                name,
                delay,
                start_event,
                output,
                output_lock,
                contacts,
                assets,
                args.scan_count,
                args.history_count,
                marker,
            ),
            daemon=True,
            name=name,
        )
        threads.append(thread)
        thread.start()
    started = time.perf_counter()
    start_event.set()
    deadline = started + args.child_timeout
    for thread in threads:
        thread.join(max(0.0, deadline - time.perf_counter()))
    alive = [thread.name for thread in threads if thread.is_alive()]
    records = list(output.values())
    report = {
        "case": args.child_case,
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "prepared": prepared,
        "operations": output,
        "overlap_seconds": overlap_seconds(records),
        "alive_threads": alive,
        "post_case_probe": None if alive else post_case_probe(contacts[0]),
        "elapsed_seconds": round(time.perf_counter() - started, 3),
    }
    write_json(Path(args.child_report), report)
    print(json.dumps(report, ensure_ascii=False, indent=2), flush=True)
    if alive:
        os._exit(2)
    return 0 if all(item.get("ok") for item in records) else 1


BASELINE_CASES = {
    "main.contact_scan": "baseline_contact_scan",
    "child.read": "baseline_child_read",
    "child.text": "baseline_child_text",
    "child.audio": "baseline_child_audio",
    "child.file": "baseline_child_file",
    "main.history": "baseline_main_history",
}


def kill_process_tree(pid: int) -> None:
    subprocess.run(
        ["taskkill", "/PID", str(pid), "/T", "/F"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=False,
    )


def baseline_durations(cases: dict[str, Any]) -> dict[str, float]:
    result = {}
    for family, case_name in BASELINE_CASES.items():
        child = (cases.get(case_name) or {}).get("child_report") or {}
        for operation in (child.get("operations") or {}).values():
            if operation.get("family") == family and operation.get("ok"):
                result[family] = float(operation.get("elapsed_seconds") or 0.0)
                break
    return result


def enrich_analysis(summary: dict[str, Any]) -> None:
    baselines = baseline_durations(summary["cases"])
    summary["baseline_seconds"] = baselines
    for case_name, case in summary["cases"].items():
        child = case.get("child_report") or {}
        ratios = {}
        for name, operation in (child.get("operations") or {}).items():
            baseline = baselines.get(operation.get("family"))
            if baseline and baseline > 0 and operation.get("elapsed_seconds") is not None:
                ratios[name] = round(float(operation["elapsed_seconds"]) / baseline, 2)
        case["slowdown_ratio"] = ratios
        if case.get("status") != "ok" or child.get("alive_threads"):
            case["classification"] = "unsafe"
        elif not (child.get("post_case_probe") or {}).get("ok"):
            case["classification"] = "unsafe_after_effect"
        elif ratios and max(ratios.values()) > 3.0:
            case["classification"] = "severely_degraded"
        elif ratios and max(ratios.values()) > 1.5:
            case["classification"] = "degraded"
        else:
            case["classification"] = "compatible"


def run_parent(args: argparse.Namespace) -> int:
    configure_ffmpeg_path()
    contacts = [clean(item) for item in args.contacts if clean(item)]
    if len(contacts) < 2:
        raise SystemExit("at least two test contacts are required")
    assets = make_assets(args.voice_seconds)
    selected = args.cases or list(CASES)
    run_dir = REPORT_ROOT / run_label()
    run_dir.mkdir(parents=True, exist_ok=True)
    summary: dict[str, Any] = {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "contacts": contacts,
        "assets": assets,
        "settings": {
            "scan_count": args.scan_count,
            "history_count": args.history_count,
            "child_timeout": args.child_timeout,
            "case_timeout": args.case_timeout,
        },
        "cases": {},
    }
    for case_name in selected:
        if case_name not in CASES:
            raise SystemExit(f"unknown case: {case_name}")
        child_report = run_dir / f"{case_name}.json"
        command = [
            sys.executable,
            "-X",
            "utf8",
            __file__,
            "--child-case",
            case_name,
            "--child-report",
            str(child_report),
            "--assets-json",
            json.dumps(assets, ensure_ascii=True),
            "--scan-count",
            str(args.scan_count),
            "--history-count",
            str(args.history_count),
            "--child-timeout",
            str(args.child_timeout),
            "--contacts",
            *contacts,
        ]
        env = dict(os.environ)
        env["PYTHONUTF8"] = "1"
        env["PYTHONIOENCODING"] = "utf-8"
        started = time.perf_counter()
        process = subprocess.Popen(
            command,
            cwd=os.getcwd(),
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        timed_out = False
        try:
            stdout, _ = process.communicate(timeout=args.case_timeout)
        except subprocess.TimeoutExpired:
            timed_out = True
            kill_process_tree(process.pid)
            stdout, _ = process.communicate(timeout=10)
        case = {
            "status": "timeout" if timed_out else ("ok" if process.returncode == 0 else "failed"),
            "returncode": process.returncode,
            "elapsed_seconds": round(time.perf_counter() - started, 3),
            "stdout_tail": (stdout or "")[-4000:],
        }
        if child_report.exists():
            case["child_report"] = json.loads(child_report.read_text(encoding="utf-8"))
        summary["cases"][case_name] = case
        enrich_analysis(summary)
        write_json(run_dir / "summary.json", summary)
        print(
            json.dumps(
                {
                    "case": case_name,
                    "status": case["status"],
                    "classification": case.get("classification"),
                    "slowdown_ratio": case.get("slowdown_ratio"),
                },
                ensure_ascii=False,
            ),
            flush=True,
        )
    print(str((run_dir / "summary.json").resolve()), flush=True)
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Focused wxautox4 UI lane probe")
    parser.add_argument("--contacts", nargs="*", default=DEFAULT_CONTACTS)
    parser.add_argument("--cases", nargs="*", default=[])
    parser.add_argument("--scan-count", type=int, default=20)
    parser.add_argument("--history-count", type=int, default=15)
    parser.add_argument("--voice-seconds", type=int, default=3)
    parser.add_argument("--child-timeout", type=float, default=75)
    parser.add_argument("--case-timeout", type=float, default=90)
    parser.add_argument("--child-case", choices=sorted(CASES), default="")
    parser.add_argument("--child-report", default="")
    parser.add_argument("--assets-json", default="{}")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.child_case:
        return run_child(args)
    return run_parent(args)


if __name__ == "__main__":
    raise SystemExit(main())
