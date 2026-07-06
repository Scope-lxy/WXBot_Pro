"""Read-only stress test for `wechat-cli history`.

The script does not run `wechat-cli init` and does not write WXBot data. It only
executes `wechat-cli history <target>` and records whether each call succeeds.
"""

from __future__ import annotations

import argparse
import csv
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import datetime
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import time
from typing import Any


DEFAULT_TIMEOUT_SECONDS = 20
TARGET_RE = re.compile(r"target=(?P<target>(?:wxid_[a-zA-Z0-9_]+|[0-9a-zA-Z]+@chatroom))")


@dataclass(frozen=True)
class RunResult:
    step: int
    concurrency: int
    run_index: int
    target: str
    ok: bool
    classification: str
    returncode: int | None
    elapsed_ms: int
    message_count: int
    output_first_line: str
    started_at: str
    ended_at: str


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[1]


def _coerce_enabled_value(value: Any, default: bool = False) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return default
    text = str(value).strip().lower()
    if text in {"0", "false", "no", "off", "disabled", "disable"}:
        return False
    if text in {"1", "true", "yes", "on", "enabled", "enable"}:
        return True
    return default


def wechat_cli_disabled_by_project_config() -> bool:
    disable_env = os.environ.get("WXBOT_DISABLE_WECHAT_CLI")
    if disable_env is not None and _coerce_enabled_value(disable_env, False):
        return True
    config_path = _repo_root() / "data" / "config" / "config.json"
    try:
        with config_path.open("r", encoding="utf-8-sig") as file:
            data = json.load(file)
        return not (isinstance(data, dict) and _coerce_enabled_value(data.get("wechat_cli_enabled"), False))
    except Exception:
        return True


def _command_env() -> dict[str, str]:
    env = dict(os.environ)
    env["PYTHONUTF8"] = "1"
    env["PYTHONIOENCODING"] = "utf-8"
    return env


def _bundled_tool_candidates() -> list[str]:
    tool_dir = _repo_root() / "venv" / "tools" / "wechat-cli"
    return [
        str(tool_dir / "wechat-cli.exe"),
        str(tool_dir / "wechat-cli"),
        str(tool_dir / "bin" / "wechat-cli.exe"),
        str(tool_dir / "bin" / "wechat-cli"),
        str(tool_dir / "pyenv" / "Scripts" / "wechat-cli.exe"),
        str(tool_dir / "pyenv" / "Scripts" / "wechat-cli"),
    ]


def find_wechat_cli_executable(explicit: str = "") -> str:
    for value in (
        explicit,
        os.environ.get("WXBOT_WECHAT_CLI_EXE", ""),
        *_bundled_tool_candidates(),
        shutil.which("wechat-cli") or "",
        shutil.which("wechat-cli.exe") or "",
    ):
        value = str(value or "").strip()
        if value and os.path.isfile(value):
            return value
    return ""


def wechat_cli_config_ready(state_dir: str = "") -> bool:
    target = Path(state_dir).expanduser() if state_dir else Path.home() / ".wechat-cli"
    return (target / "config.json").is_file() and (target / "all_keys.json").is_file()


def latest_log_files(count: int = 2) -> list[Path]:
    logs_dir = _repo_root() / "wxbot_logs"
    if not logs_dir.is_dir():
        return []
    files = [path for path in logs_dir.iterdir() if path.is_file()]
    files.sort(key=lambda path: path.stat().st_mtime, reverse=True)
    return files[: max(1, count)]


def targets_from_latest_logs(max_targets: int) -> list[str]:
    seen: set[str] = set()
    targets: list[str] = []
    for path in latest_log_files():
        try:
            lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            continue
        for line in reversed(lines):
            match = TARGET_RE.search(line)
            if not match:
                continue
            target = match.group("target")
            if target in seen:
                continue
            seen.add(target)
            targets.append(target)
            if len(targets) >= max_targets:
                return targets
    return targets


def parse_targets(args: argparse.Namespace) -> list[str]:
    targets: list[str] = []
    for value in args.target:
        targets.extend(part.strip() for part in value.split(",") if part.strip())
    if args.targets_file:
        path = Path(args.targets_file).expanduser()
        try:
            for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
                item = line.strip()
                if item and not item.startswith("#"):
                    targets.append(item)
        except OSError as exc:
            raise SystemExit(f"无法读取 targets 文件: {exc}") from exc
    if not targets and args.from_latest_logs:
        targets = targets_from_latest_logs(args.max_targets)

    deduped: list[str] = []
    seen: set[str] = set()
    for target in targets:
        if target not in seen:
            seen.add(target)
            deduped.append(target)
    return deduped[: args.max_targets]


def classify_failure(returncode: int | None, text: str) -> str:
    if returncode is None:
        return "timeout"
    if "找不到聊天对象" in text:
        return "target_not_found"
    if "找不到" in text and "消息记录" in text:
        return "missing_history"
    if "初始化失败" in text:
        return "init_failed"
    if "密钥文件不存在" in text:
        return "missing_keys"
    if "invalid json" in text.lower():
        return "invalid_json"
    if returncode != 0:
        return f"returncode_{returncode}"
    return "unknown"


def run_history_once(
    executable: str,
    target: str,
    *,
    limit: int,
    timeout: int,
    step: int,
    concurrency: int,
    run_index: int,
) -> RunResult:
    started = datetime.now()
    start_time = time.perf_counter()
    command = [executable, "history", target, "--limit", str(limit), "--format", "json"]
    try:
        completed = subprocess.run(
            command,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=max(1, timeout),
            check=False,
            env=_command_env(),
        )
        elapsed_ms = int((time.perf_counter() - start_time) * 1000)
        raw = (completed.stdout or completed.stderr or "").strip()
        first_line = raw.splitlines()[0] if raw else ""
        if completed.returncode != 0:
            return RunResult(
                step,
                concurrency,
                run_index,
                target,
                False,
                classify_failure(completed.returncode, raw),
                completed.returncode,
                elapsed_ms,
                0,
                first_line[:300],
                started.strftime("%Y-%m-%d %H:%M:%S.%f")[:-3],
                datetime.now().strftime("%Y-%m-%d %H:%M:%S.%f")[:-3],
            )
        try:
            payload = json.loads(raw or "null")
        except json.JSONDecodeError as exc:
            return RunResult(
                step,
                concurrency,
                run_index,
                target,
                False,
                "invalid_json",
                completed.returncode,
                elapsed_ms,
                0,
                str(exc)[:300],
                started.strftime("%Y-%m-%d %H:%M:%S.%f")[:-3],
                datetime.now().strftime("%Y-%m-%d %H:%M:%S.%f")[:-3],
            )
        messages = payload.get("messages") if isinstance(payload, dict) else None
        if not isinstance(messages, list):
            return RunResult(
                step,
                concurrency,
                run_index,
                target,
                False,
                "missing_messages_payload",
                completed.returncode,
                elapsed_ms,
                0,
                first_line[:300],
                started.strftime("%Y-%m-%d %H:%M:%S.%f")[:-3],
                datetime.now().strftime("%Y-%m-%d %H:%M:%S.%f")[:-3],
            )
        return RunResult(
            step,
            concurrency,
            run_index,
            target,
            True,
            "ok",
            completed.returncode,
            elapsed_ms,
            len(messages),
            "",
            started.strftime("%Y-%m-%d %H:%M:%S.%f")[:-3],
            datetime.now().strftime("%Y-%m-%d %H:%M:%S.%f")[:-3],
        )
    except subprocess.TimeoutExpired:
        elapsed_ms = int((time.perf_counter() - start_time) * 1000)
        return RunResult(
            step,
            concurrency,
            run_index,
            target,
            False,
            "timeout",
            None,
            elapsed_ms,
            0,
            "command timed out",
            started.strftime("%Y-%m-%d %H:%M:%S.%f")[:-3],
            datetime.now().strftime("%Y-%m-%d %H:%M:%S.%f")[:-3],
        )
    except Exception as exc:
        elapsed_ms = int((time.perf_counter() - start_time) * 1000)
        return RunResult(
            step,
            concurrency,
            run_index,
            target,
            False,
            "exception",
            None,
            elapsed_ms,
            0,
            str(exc)[:300],
            started.strftime("%Y-%m-%d %H:%M:%S.%f")[:-3],
            datetime.now().strftime("%Y-%m-%d %H:%M:%S.%f")[:-3],
        )


def write_csv(path: Path, rows: list[RunResult]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fieldnames = [
        "step",
        "concurrency",
        "run_index",
        "target",
        "ok",
        "classification",
        "returncode",
        "elapsed_ms",
        "message_count",
        "output_first_line",
        "started_at",
        "ended_at",
    ]
    with path.open("w", encoding="utf-8-sig", newline="") as file:
        writer = csv.DictWriter(file, fieldnames=fieldnames)
        writer.writeheader()
        for row in rows:
            writer.writerow(row.__dict__)


def print_summary(rows: list[RunResult], output_path: Path) -> None:
    print("wechat-cli history 压测结果")
    print("=" * 32)
    print(f"记录文件: {output_path}")
    print(f"总次数: {len(rows)}")
    if not rows:
        return
    by_step: dict[int, list[RunResult]] = {}
    for row in rows:
        by_step.setdefault(row.step, []).append(row)
    for step, step_rows in sorted(by_step.items()):
        concurrency = step_rows[0].concurrency
        ok_count = sum(1 for row in step_rows if row.ok)
        fail_count = len(step_rows) - ok_count
        elapsed_values = sorted(row.elapsed_ms for row in step_rows)
        p95 = elapsed_values[int(len(elapsed_values) * 0.95) - 1] if elapsed_values else 0
        avg = int(sum(elapsed_values) / len(elapsed_values)) if elapsed_values else 0
        print(f"\n并发 {concurrency}: 成功 {ok_count}/{len(step_rows)}，失败 {fail_count}，平均 {avg}ms，P95 {p95}ms")
        classes: dict[str, int] = {}
        for row in step_rows:
            classes[row.classification] = classes.get(row.classification, 0) + 1
        for name, count in sorted(classes.items(), key=lambda item: (-item[1], item[0])):
            print(f"  {name}: {count}")


def parse_concurrency_steps(value: str) -> list[int]:
    steps: list[int] = []
    for part in value.split(","):
        part = part.strip()
        if not part:
            continue
        steps.append(max(1, int(part)))
    return steps or [1]


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Read-only stress test for wechat-cli history.")
    parser.add_argument("--exe", default="", help="wechat-cli executable. Defaults to WXBOT_WECHAT_CLI_EXE/bundled/PATH.")
    parser.add_argument("--state-dir", default="", help="wechat-cli state directory. Defaults to ~/.wechat-cli.")
    parser.add_argument("--target", action="append", default=[], help="Target wxid/chatroom. Can be repeated or comma-separated.")
    parser.add_argument("--targets-file", default="", help="UTF-8 text file, one target per line.")
    parser.add_argument("--from-latest-logs", action=argparse.BooleanOptionalAction, default=True, help="Use target=... values from latest wxbot logs when no target is provided.")
    parser.add_argument("--max-targets", type=int, default=8, help="Maximum target count when reading from logs.")
    parser.add_argument("--rounds", type=int, default=10, help="Rounds per target per concurrency step.")
    parser.add_argument("--concurrency-steps", default="1,2,4", help="Comma-separated concurrency levels, e.g. 1,2,4,8.")
    parser.add_argument("--limit", type=int, default=60, help="wechat-cli history --limit value.")
    parser.add_argument("--timeout", type=int, default=DEFAULT_TIMEOUT_SECONDS, help="Timeout per history command in seconds.")
    parser.add_argument("--delay-ms", type=int, default=0, help="Delay between scheduling jobs within each step.")
    parser.add_argument("--output", default="", help="CSV output path. Defaults to runtime/wechat_cli_history_stress_*.csv.")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(list(argv or sys.argv[1:]))
    if wechat_cli_disabled_by_project_config():
        print("wechat-cli 已被项目配置禁用，未执行 history 压测。", file=sys.stderr)
        return 2
    executable = find_wechat_cli_executable(args.exe)
    if not executable:
        print("未找到 wechat-cli 可执行文件。", file=sys.stderr)
        return 2
    if not wechat_cli_config_ready(args.state_dir):
        print("wechat-cli config.json/all_keys.json 未就绪，未执行 history。", file=sys.stderr)
        return 2

    targets = parse_targets(args)
    if not targets:
        print("没有可测试的 target。请传 --target wxid_xxx，或确认最新日志里有 target=... 诊断字段。", file=sys.stderr)
        return 2

    output_path = Path(args.output).expanduser() if args.output else (
        _repo_root() / "runtime" / f"wechat_cli_history_stress_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv"
    )
    all_rows: list[RunResult] = []
    steps = parse_concurrency_steps(args.concurrency_steps)
    rounds = max(1, int(args.rounds))

    print(f"可执行文件: {executable}")
    print(f"目标数量: {len(targets)}")
    print("目标: " + ", ".join(targets))

    for step_index, concurrency in enumerate(steps, start=1):
        jobs = [(run_index, target) for run_index in range(1, rounds + 1) for target in targets]
        print(f"\n开始并发 {concurrency}: {len(jobs)} 次")
        with ThreadPoolExecutor(max_workers=concurrency) as pool:
            futures = []
            for run_index, target in jobs:
                futures.append(
                    pool.submit(
                        run_history_once,
                        executable,
                        target,
                        limit=max(1, int(args.limit)),
                        timeout=max(1, int(args.timeout)),
                        step=step_index,
                        concurrency=concurrency,
                        run_index=run_index,
                    )
                )
                if args.delay_ms > 0:
                    time.sleep(args.delay_ms / 1000)
            completed_count = 0
            for future in as_completed(futures):
                row = future.result()
                all_rows.append(row)
                completed_count += 1
                if completed_count % max(1, min(10, len(jobs))) == 0 or not row.ok:
                    status = "OK" if row.ok else row.classification
                    print(f"  {completed_count}/{len(jobs)} {status} {row.target} {row.elapsed_ms}ms")
        write_csv(output_path, all_rows)

    print_summary(all_rows, output_path)
    return 0 if all(row.ok for row in all_rows) else 1


if __name__ == "__main__":
    raise SystemExit(main())
