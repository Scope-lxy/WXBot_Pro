"""Safe capability probe for optional wechat-cli integration.

This script intentionally does not run `wechat-cli init` or any command that
reads WeChat databases/messages. It only checks whether the CLI can be found,
whether help/version commands run, and whether the local config files exist.
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
from typing import Any


DEFAULT_TIMEOUT_SECONDS = 10
STATE_DIR_NAME = ".wechat-cli"
CONFIG_FILE_NAME = "config.json"
KEYS_FILE_NAME = "all_keys.json"


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


def _candidate_executables(explicit: str = "") -> list[str]:
    candidates: list[str] = []
    for value in (
        explicit,
        os.environ.get("WXBOT_WECHAT_CLI_EXE", ""),
        *_bundled_tool_candidates(),
        shutil.which("wechat-cli") or "",
        shutil.which("wechat-cli.exe") or "",
    ):
        value = str(value or "").strip()
        if value and value not in candidates:
            candidates.append(value)
    return candidates


def _run_command(command: list[str], *, timeout: int) -> dict[str, Any]:
    try:
        completed = subprocess.run(
            command,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=max(1, int(timeout or DEFAULT_TIMEOUT_SECONDS)),
            check=False,
        )
        output = (completed.stdout or completed.stderr or "").strip()
        return {
            "ok": completed.returncode == 0,
            "returncode": completed.returncode,
            "output_first_line": output.splitlines()[0] if output else "",
        }
    except FileNotFoundError as exc:
        return {"ok": False, "returncode": None, "output_first_line": str(exc)}
    except subprocess.TimeoutExpired:
        return {"ok": False, "returncode": None, "output_first_line": "command timed out"}
    except Exception as exc:
        return {"ok": False, "returncode": None, "output_first_line": str(exc)}


def _first_working_executable(candidates: list[str], *, timeout: int) -> tuple[str, dict[str, Any]]:
    last_result: dict[str, Any] = {}
    for executable in candidates:
        result = _run_command([executable, "--help"], timeout=timeout)
        last_result = result
        if result.get("ok"):
            return executable, result
    return "", last_result


def _read_json_object(path: Path) -> dict[str, Any]:
    try:
        with path.open("r", encoding="utf-8") as file:
            data = json.load(file)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _config_summary(state_dir: Path) -> dict[str, Any]:
    config_path = state_dir / CONFIG_FILE_NAME
    keys_path = state_dir / KEYS_FILE_NAME
    config = _read_json_object(config_path) if config_path.is_file() else {}
    keys = _read_json_object(keys_path) if keys_path.is_file() else {}
    return {
        "state_dir": str(state_dir),
        "config_exists": config_path.is_file(),
        "keys_exists": keys_path.is_file(),
        "config_keys": sorted(config.keys()),
        "key_entry_count": len(keys) if isinstance(keys, dict) else 0,
    }


def build_probe_report(args: argparse.Namespace) -> dict[str, Any]:
    candidates = _candidate_executables(args.exe)
    executable, help_result = _first_working_executable(candidates, timeout=args.timeout)
    version_result = (
        _run_command([executable, "--version"], timeout=args.timeout)
        if executable
        else {"ok": False, "returncode": None, "output_first_line": ""}
    )
    state_dir = Path(args.state_dir).expanduser() if args.state_dir else Path.home() / STATE_DIR_NAME
    config = _config_summary(state_dir)
    return {
        "safe_probe": True,
        "commands_executed": ["--help"] + (["--version"] if executable else []),
        "commands_not_executed": ["init", "history", "contacts", "sessions", "unread", "new-messages"],
        "candidate_count": len(candidates),
        "executable": executable,
        "help": help_result,
        "version": version_result,
        "config": config,
        "ready_for_read_commands": bool(executable and help_result.get("ok") and config["config_exists"] and config["keys_exists"]),
    }


def print_human_report(report: dict[str, Any]) -> None:
    config = report.get("config") or {}
    help_result = report.get("help") or {}
    version_result = report.get("version") or {}
    print("wechat-cli 安全探测结果")
    print("=" * 28)
    print("安全边界: 只执行 --help / --version；未执行 init；未读取聊天记录或通讯录。")
    print(f"可执行文件: {report.get('executable') or '未找到/不可用'}")
    print(f"候选数量: {report.get('candidate_count', 0)}")
    print(f"help 可用: {'是' if help_result.get('ok') else '否'}")
    if help_result.get("output_first_line"):
        print(f"help 输出: {help_result.get('output_first_line')}")
    print(f"version 可用: {'是' if version_result.get('ok') else '否'}")
    if version_result.get("output_first_line"):
        print(f"version 输出: {version_result.get('output_first_line')}")
    print(f"配置目录: {config.get('state_dir')}")
    print(f"config.json: {'存在' if config.get('config_exists') else '不存在'}")
    print(f"all_keys.json: {'存在' if config.get('keys_exists') else '不存在'}")
    print(f"读库命令就绪: {'是' if report.get('ready_for_read_commands') else '否'}")
    if not report.get("ready_for_read_commands"):
        print("下一步: 如需真实数据探测，请先人工安装并确认是否执行 wechat-cli init。")


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Safe probe for optional wechat-cli integration. Does not run init or read WeChat data.",
    )
    parser.add_argument("--exe", default="", help="wechat-cli executable path. Defaults to PATH/WXBOT_WECHAT_CLI_EXE.")
    parser.add_argument("--state-dir", default="", help="wechat-cli state directory. Defaults to ~/.wechat-cli.")
    parser.add_argument("--timeout", type=int, default=DEFAULT_TIMEOUT_SECONDS, help="Command timeout in seconds.")
    parser.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(list(argv or sys.argv[1:]))
    if wechat_cli_disabled_by_project_config():
        message = "wechat-cli 已被项目配置禁用，未执行探测命令。"
        if args.json:
            print(json.dumps({"disabled": True, "message": message}, ensure_ascii=False, indent=2))
        else:
            print(message)
        return 2
    report = build_probe_report(args)
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        print_human_report(report)
    return 0 if report.get("executable") else 1


if __name__ == "__main__":
    raise SystemExit(main())
