from __future__ import annotations

import argparse
import json
import statistics
import sys
import time
from datetime import datetime
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.wechat_ui_actions import UI_CALL_WAIT_TIMEOUT, UIIntent, UIIntentKind, WeChatUIOwner
from core.wechat_ui_runtime import WeChatUIRuntime


def percentile(values: list[float], ratio: float) -> float:
    ordered = sorted(values)
    if not ordered:
        return 0.0
    index = min(len(ordered) - 1, max(0, int(round((len(ordered) - 1) * ratio))))
    return ordered[index]


def main() -> int:
    parser = argparse.ArgumentParser(description="Measure current-session relationship scan latency.")
    parser.add_argument("--runs", type=int, default=10)
    args = parser.parse_args()
    runs = max(1, min(100, int(args.runs or 10)))

    runtime = WeChatUIRuntime(lambda _conversation, _message: None)
    owner = WeChatUIOwner(runtime.handlers(), thread_name="relationship-scan-timing-owner")
    runtime.set_owner(owner)
    runtime.set_heartbeat(owner.heartbeat_current_action)
    owner.start()

    started_at = datetime.now()
    report_dir = ROOT / "backups" / "relationship_scan_timing" / started_at.strftime("%Y%m%d_%H%M%S")
    report_dir.mkdir(parents=True, exist_ok=True)
    results: list[dict] = []
    identity = {}
    bootstrap_seconds = 0.0
    shutdown_seconds = 0.0
    error = ""
    try:
        start = time.perf_counter()
        identity = dict(owner.call(UIIntent(UIIntentKind.BOOTSTRAP, {"listeners": []}), UI_CALL_WAIT_TIMEOUT) or {})
        bootstrap_seconds = time.perf_counter() - start

        for index in range(runs):
            start = time.perf_counter()
            sessions = owner.call(
                UIIntent(UIIntentKind.RELATIONSHIP_SCAN, {"mode": "current"}),
                UI_CALL_WAIT_TIMEOUT,
            )
            elapsed = time.perf_counter() - start
            results.append({
                "run": index + 1,
                "seconds": round(elapsed, 6),
                "session_count": len(list(sessions or [])),
            })
    except Exception as exc:
        error = f"{type(exc).__name__}: {exc}"
    finally:
        start = time.perf_counter()
        try:
            owner.call_shutdown(UI_CALL_WAIT_TIMEOUT)
        except Exception as exc:
            if not error:
                error = f"shutdown {type(exc).__name__}: {exc}"
        shutdown_seconds = time.perf_counter() - start
        owner.stop(cancel_pending=True)

    durations = [float(item["seconds"]) for item in results]
    counts = [int(item["session_count"]) for item in results]
    summary = {
        "started_at": started_at.isoformat(timespec="seconds"),
        "finished_at": datetime.now().isoformat(timespec="seconds"),
        "requested_runs": runs,
        "completed_runs": len(results),
        "success": not error and len(results) == runs and len(set(counts)) <= 1,
        "error": error,
        "identity": identity,
        "bootstrap_seconds": round(bootstrap_seconds, 6),
        "shutdown_seconds": round(shutdown_seconds, 6),
        "scan_total_seconds": round(sum(durations), 6),
        "scan_mean_seconds": round(statistics.mean(durations), 6) if durations else 0.0,
        "scan_median_seconds": round(statistics.median(durations), 6) if durations else 0.0,
        "scan_p95_seconds": round(percentile(durations, 0.95), 6),
        "scan_max_seconds": round(max(durations), 6) if durations else 0.0,
        "session_counts": sorted(set(counts)),
        "runs": results,
    }
    report_path = report_dir / "summary.json"
    report_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({**summary, "report_path": str(report_path)}, ensure_ascii=False, indent=2))
    return 0 if summary["success"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
