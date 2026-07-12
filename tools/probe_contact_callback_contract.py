from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
import time
from datetime import datetime
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def clean_text(value: Any) -> str:
    return str(value or "").strip()


def detail_name(detail: Any) -> str:
    if not isinstance(detail, dict):
        return clean_text(detail)
    for key in ("备注", "昵称", "微信号", "name", "remark", "nickname", "wechat_id"):
        value = clean_text(detail.get(key))
        if value:
            return value
    return ""


def write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2, default=str)
        os.replace(temp_name, path)
    finally:
        try:
            os.unlink(temp_name)
        except FileNotFoundError:
            pass


def child_run(mode: str, target: str, count: int, progress_path: Path) -> int:
    from feature.contact_auto_collector_worker import _reset_contact_list_to_top
    from wxautox4 import WeChat

    callbacks: list[dict[str, Any]] = []
    matched = False

    def callback(detail: Any) -> bool:
        nonlocal matched
        name = detail_name(detail)
        if mode == "always_true":
            decision = True
        elif mode == "always_false":
            decision = False
        elif mode == "match_pulse":
            decision = name == target
        elif mode == "match_latch":
            matched = matched or name == target
            decision = matched
        else:
            raise ValueError(f"unknown callback mode: {mode}")
        callbacks.append({"name": name, "decision": decision})
        write_json(progress_path, {"status": "running", "mode": mode, "callbacks": callbacks[-20:]})
        return decision

    started = time.perf_counter()
    wx = WeChat()
    wx.SwitchToContact()
    _reset_contact_list_to_top(wx)
    kwargs: dict[str, Any] = {
        "n": count,
        "interval": 0,
        "speed": 5,
        "save_head_image": False,
    }
    if mode != "no_callback":
        kwargs["callback"] = callback
    try:
        result = list(wx.GetFriendDetails(**kwargs) or [])
        payload = {
            "status": "completed",
            "mode": mode,
            "target": target,
            "duration_seconds": round(time.perf_counter() - started, 3),
            "callback_count": len(callbacks),
            "callbacks": callbacks[-20:],
            "result_count": len(result),
            "result_names": [detail_name(item) for item in result],
        }
        write_json(progress_path, payload)
        return 0
    except Exception as exc:
        write_json(progress_path, {
            "status": "failed",
            "mode": mode,
            "target": target,
            "duration_seconds": round(time.perf_counter() - started, 3),
            "callback_count": len(callbacks),
            "callbacks": callbacks[-20:],
            "error": str(exc),
        })
        return 1


def terminate_tree(proc: subprocess.Popen[Any]) -> None:
    if proc.poll() is not None:
        return
    subprocess.run(
        ["taskkill", "/PID", str(proc.pid), "/T", "/F"],
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        proc.kill()


def run_case(mode: str, target: str, count: int, timeout_seconds: int, report_dir: Path) -> dict[str, Any]:
    progress_path = report_dir / f"{mode}.json"
    cmd = [
        sys.executable,
        "-X",
        "utf8",
        str(Path(__file__).resolve()),
        "--child",
        "--mode",
        mode,
        "--target",
        target,
        "--count",
        str(count),
        "--progress",
        str(progress_path),
    ]
    env = dict(os.environ)
    env["PYTHONUTF8"] = "1"
    env["PYTHONIOENCODING"] = "utf-8"
    proc = subprocess.Popen(cmd, cwd=ROOT, env=env)
    try:
        proc.wait(timeout=timeout_seconds)
    except subprocess.TimeoutExpired:
        terminate_tree(proc)
        payload = json.loads(progress_path.read_text(encoding="utf-8")) if progress_path.exists() else {}
        payload.update({"status": "timeout", "mode": mode, "target": target, "timeout_seconds": timeout_seconds})
        write_json(progress_path, payload)
    payload = json.loads(progress_path.read_text(encoding="utf-8")) if progress_path.exists() else {}
    payload["exit_code"] = proc.poll()
    return payload


def parent_run(count: int, timeout_seconds: int) -> int:
    from wxautox4 import WeChat

    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    report_dir = ROOT / "backups" / "contact_callback_contract" / stamp
    report_dir.mkdir(parents=True, exist_ok=True)
    cases: list[dict[str, Any]] = []
    target = ""
    try:
        baseline = run_case("no_callback", "", count, timeout_seconds, report_dir)
        cases.append(baseline)
        names = [clean_text(name) for name in baseline.get("result_names") or [] if clean_text(name)]
        if len(names) < 2:
            raise RuntimeError("baseline did not return enough contacts to choose a target")
        target = names[1]
        for mode in ("always_true", "always_false", "match_pulse", "match_latch"):
            WeChat().SwitchToChat()
            cases.append(run_case(mode, target, count, timeout_seconds, report_dir))
    finally:
        WeChat().SwitchToChat()
    summary = {"count": count, "timeout_seconds": timeout_seconds, "target": target, "cases": cases}
    write_json(report_dir / "summary.json", summary)
    print(json.dumps({
        "report": str(report_dir / "summary.json"),
        "cases": [
            {
                "mode": item.get("mode"),
                "status": item.get("status"),
                "duration_seconds": item.get("duration_seconds"),
                "callback_count": item.get("callback_count"),
                "result_count": item.get("result_count"),
            }
            for item in cases
        ],
    }, ensure_ascii=False, indent=2))
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    parser.add_argument("--child", action="store_true")
    parser.add_argument("--mode", default="no_callback")
    parser.add_argument("--target", default="")
    parser.add_argument("--count", type=int, default=3)
    parser.add_argument("--timeout-seconds", type=int, default=45)
    parser.add_argument("--progress")
    return parser


def main() -> int:
    args = build_parser().parse_args()
    if args.child:
        if not args.progress:
            raise SystemExit("--progress is required in child mode")
        return child_run(args.mode, args.target, max(1, args.count), Path(args.progress))
    return parent_run(max(2, args.count), max(5, args.timeout_seconds))


if __name__ == "__main__":
    raise SystemExit(main())
