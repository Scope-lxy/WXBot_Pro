from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Any


REPORT_ROOT = Path("backups") / "contact_worker_preemption"
COLLECTOR_SCRIPT = Path("feature") / "contact_auto_collector_worker.py"
DEFAULT_READ_CONTACT = "\u963f\u82f12"
DEFAULT_SECOND_READ_CONTACT = "\u963f\u82f13"
DEFAULT_REOPEN_CONTACT = "\u70b34"


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


def clean(value: Any) -> str:
    return str(value or "").strip()


def terminate_process_tree(process: subprocess.Popen[str]) -> dict[str, Any]:
    started = time.perf_counter()
    before = process.poll()
    if before is None:
        subprocess.run(
            ["taskkill", "/PID", str(process.pid), "/T", "/F"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )
    try:
        stdout, stderr = process.communicate(timeout=10)
    except subprocess.TimeoutExpired:
        process.kill()
        stdout, stderr = process.communicate(timeout=5)
    return {
        "pid": process.pid,
        "poll_before": before,
        "returncode": process.returncode,
        "elapsed_seconds": round(time.perf_counter() - started, 3),
        "stdout_tail": clean(stdout)[-1000:],
        "stderr_tail": clean(stderr)[-1000:],
    }


def get_verified_subwindow(wx: Any, contact: str) -> Any:
    chat = wx.GetSubWindow(nickname=contact)
    if chat is None or clean(getattr(chat, "who", "")) != contact:
        raise RuntimeError(f"subwindow verification failed: {contact}")
    return chat


def prepare_subwindow(wx: Any, contact: str) -> dict[str, Any]:
    started = time.perf_counter()
    chat = wx.GetSubWindow(nickname=contact)
    if chat is None or clean(getattr(chat, "who", "")) != contact:
        chat = wx.AddListenChat(nickname=contact, callback=lambda *_args, **_kwargs: None)
    who = clean(getattr(chat, "who", ""))
    if who != contact:
        raise RuntimeError(f"failed to prepare subwindow: expected={contact!r}, actual={who!r}")
    return {
        "contact": contact,
        "who": who,
        "elapsed_seconds": round(time.perf_counter() - started, 3),
    }


def start_collector(request_path: Path, output_path: Path, count: int) -> subprocess.Popen[str]:
    write_json(request_path, {"start_name": "", "count": max(1, int(count))})
    env = dict(os.environ)
    env["PYTHONUTF8"] = "1"
    env["PYTHONIOENCODING"] = "utf-8"
    kwargs: dict[str, Any] = {}
    if os.name == "nt" and hasattr(subprocess, "CREATE_NO_WINDOW"):
        kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW
    return subprocess.Popen(
        [
            sys.executable,
            "-X",
            "utf8",
            str(COLLECTOR_SCRIPT),
            "--request",
            str(request_path),
            "--output",
            str(output_path),
        ],
        cwd=os.getcwd(),
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
        **kwargs,
    )


def read_existing_child(contact: str, result: dict[str, Any]) -> None:
    import pythoncom
    from wxautox4 import WeChat

    pythoncom.CoInitialize()
    started = time.perf_counter()
    try:
        wx = WeChat()
        chat = get_verified_subwindow(wx, contact)
        messages = chat.GetAllMessage() or []
        result.update(
            {
                "ok": True,
                "message_count": len(messages),
                "elapsed_seconds": round(time.perf_counter() - started, 3),
            }
        )
    except Exception as exc:
        result.update(
            {
                "ok": False,
                "error": f"{type(exc).__name__}: {exc}",
                "elapsed_seconds": round(time.perf_counter() - started, 3),
            }
        )
    finally:
        pythoncom.CoUninitialize()


def run_case(args: argparse.Namespace) -> int:
    from wxautox4 import WeChat

    report_path = Path(args.report)
    report: dict[str, Any] = {
        "started_at": stamp(),
        "settings": {
            "count": args.count,
            "read_contacts": args.read_contacts,
            "reopen_contact": args.reopen_contact,
            "launch_wait_seconds": args.launch_wait,
            "read_timeout_seconds": args.read_timeout,
        },
        "steps": {},
    }
    write_json(report_path, report)

    wx = WeChat()
    report["steps"]["prepare_existing_children"] = [
        prepare_subwindow(wx, contact)
        for contact in args.read_contacts
    ]
    write_json(report_path, report)

    collector: subprocess.Popen[str] | None = None
    with tempfile.TemporaryDirectory(prefix="wxbot_preempt_probe_") as temp_dir:
        request_path = Path(temp_dir) / "request.json"
        output_path = Path(temp_dir) / "output.json"
        try:
            collector = start_collector(request_path, output_path, args.count)
            report["steps"]["collector_started"] = {
                "pid": collector.pid,
                "started_at": stamp(),
            }
            write_json(report_path, report)
            time.sleep(max(0.1, float(args.launch_wait)))
            report["steps"]["collector_alive_before_light_read"] = collector.poll() is None

            child_reads: dict[str, dict[str, Any]] = {
                contact: {}
                for contact in args.read_contacts
            }
            read_threads = [
                threading.Thread(
                    target=read_existing_child,
                    args=(contact, child_reads[contact]),
                    name=f"preemption-probe-child-read-{index}",
                    daemon=True,
                )
                for index, contact in enumerate(args.read_contacts)
            ]
            for read_thread in read_threads:
                read_thread.start()
            read_deadline = time.perf_counter() + max(1.0, float(args.read_timeout))
            for read_thread in read_threads:
                read_thread.join(max(0.0, read_deadline - time.perf_counter()))
            report["steps"]["light_reads"] = {
                contact: result or {"ok": False, "status": "still_running"}
                for contact, result in child_reads.items()
            }
            report["steps"]["collector_alive_after_light_read"] = collector.poll() is None
            write_json(report_path, report)

            report["steps"]["collector_termination"] = terminate_process_tree(collector)
            report["steps"]["collector_output_exists_after_kill"] = output_path.exists()
            if output_path.exists():
                try:
                    payload = json.loads(output_path.read_text(encoding="utf-8"))
                    report["steps"]["collector_output"] = {
                        "ok": payload.get("ok"),
                        "duration_seconds": payload.get("duration_seconds"),
                        "result_count": len(payload.get("result") or []),
                    }
                except Exception as exc:
                    report["steps"]["collector_output_error"] = f"{type(exc).__name__}: {exc}"
            write_json(report_path, report)

            alive_read_threads = [thread for thread in read_threads if thread.is_alive()]
            if alive_read_threads:
                recovery_deadline = time.perf_counter() + 20
                for read_thread in alive_read_threads:
                    read_thread.join(max(0.0, recovery_deadline - time.perf_counter()))
                if any(thread.is_alive() for thread in alive_read_threads):
                    raise RuntimeError("child GetAllMessage did not return after collector termination")
                report["steps"]["light_reads"] = child_reads

            restore_started = time.perf_counter()
            wx.SwitchToChat()
            report["steps"]["switch_to_chat_after_kill"] = {
                "ok": True,
                "elapsed_seconds": round(time.perf_counter() - restore_started, 3),
            }

            try:
                wx.RemoveListenChat(nickname=args.reopen_contact)
            except Exception:
                pass
            reopen_started = time.perf_counter()
            reopened = wx.AddListenChat(
                nickname=args.reopen_contact,
                callback=lambda *_args, **_kwargs: None,
            )
            reopened_who = clean(getattr(reopened, "who", ""))
            report["steps"]["reopen_child_after_kill"] = {
                "ok": reopened_who == args.reopen_contact,
                "who": reopened_who,
                "elapsed_seconds": round(time.perf_counter() - reopen_started, 3),
            }
            if reopened_who != args.reopen_contact:
                raise RuntimeError(
                    f"reopened wrong child: expected={args.reopen_contact!r}, actual={reopened_who!r}"
                )

            marker = f"[WXBot contact preemption probe] {run_label()} Please ignore."
            send_started = time.perf_counter()
            send_result = reopened.SendMsg(marker)
            report["steps"]["send_after_kill"] = {
                "ok": bool(send_result),
                "result": clean(send_result)[:500],
                "elapsed_seconds": round(time.perf_counter() - send_started, 3),
            }
            post_read_started = time.perf_counter()
            report["steps"]["post_kill_read"] = {
                "ok": True,
                "message_count": len(reopened.GetAllMessage() or []),
                "elapsed_seconds": round(time.perf_counter() - post_read_started, 3),
            }
        finally:
            if collector is not None and collector.poll() is None:
                report["steps"]["collector_final_cleanup"] = terminate_process_tree(collector)

    required = (
        report["steps"].get("collector_alive_before_light_read") is True
        and report["steps"].get("collector_alive_after_light_read") is True
        and all(
            item.get("ok") is True
            for item in report["steps"].get("light_reads", {}).values()
        )
        and report["steps"].get("reopen_child_after_kill", {}).get("ok") is True
        and report["steps"].get("send_after_kill", {}).get("ok") is True
        and report["steps"].get("post_kill_read", {}).get("ok") is True
    )
    report["completed_at"] = stamp()
    report["status"] = "passed" if required else "inconclusive"
    write_json(report_path, report)
    print(json.dumps(report, ensure_ascii=False, indent=2), flush=True)
    return 0 if required else 2


def run_parent(args: argparse.Namespace) -> int:
    run_dir = REPORT_ROOT / run_label()
    run_dir.mkdir(parents=True, exist_ok=True)
    report_path = run_dir / "report.json"
    command = [
        sys.executable,
        "-X",
        "utf8",
        __file__,
        "--case",
        "--report",
        str(report_path),
        "--count",
        str(args.count),
        "--read-contacts",
        *args.read_contacts,
        "--reopen-contact",
        args.reopen_contact,
        "--launch-wait",
        str(args.launch_wait),
        "--read-timeout",
        str(args.read_timeout),
    ]
    env = dict(os.environ)
    env["PYTHONUTF8"] = "1"
    env["PYTHONIOENCODING"] = "utf-8"
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
    try:
        stdout, _ = process.communicate(timeout=max(30.0, float(args.case_timeout)))
    except subprocess.TimeoutExpired:
        cleanup = terminate_process_tree(process)
        partial = {}
        if report_path.exists():
            try:
                partial = json.loads(report_path.read_text(encoding="utf-8"))
            except Exception:
                partial = {}
        partial["status"] = "timeout"
        partial["parent_cleanup"] = cleanup
        write_json(report_path, partial)
        print(json.dumps(partial, ensure_ascii=False, indent=2), flush=True)
        print(report_path.resolve(), flush=True)
        return 3
    print(stdout or "", end="")
    print(report_path.resolve(), flush=True)
    return int(process.returncode or 0)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Production-shaped contact worker preemption probe")
    parser.add_argument("--count", type=int, default=50)
    parser.add_argument(
        "--read-contacts",
        nargs="+",
        default=[DEFAULT_READ_CONTACT, DEFAULT_SECOND_READ_CONTACT],
    )
    parser.add_argument("--reopen-contact", default=DEFAULT_REOPEN_CONTACT)
    parser.add_argument("--launch-wait", type=float, default=2.0)
    parser.add_argument("--read-timeout", type=float, default=15.0)
    parser.add_argument("--case-timeout", type=float, default=90.0)
    parser.add_argument("--case", action="store_true")
    parser.add_argument("--report", default="")
    return parser


def main() -> int:
    args = build_parser().parse_args()
    if args.case:
        if not args.report:
            raise SystemExit("--report is required with --case")
        return run_case(args)
    return run_parent(args)


if __name__ == "__main__":
    raise SystemExit(main())
