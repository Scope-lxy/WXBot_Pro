"""Optional local WeChat data reader backed by wechat-cli.

The reader is intentionally best-effort: callers can inspect the returned error
and fall back to wxautox4 without marking the tool permanently unavailable.
"""

from __future__ import annotations

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
from types import SimpleNamespace
from typing import Any


DEFAULT_TIMEOUT_SECONDS = 15
STATUS_CHECK_TIMEOUT_SECONDS = 10
UPDATE_CHECK_TIMEOUT_SECONDS = 45
MAX_CONTACT_FETCH_LIMIT = 30000
HISTORY_TARGET_DISAMBIGUATION_SESSION_LIMIT = 100
HISTORY_TARGET_DISAMBIGUATION_CONTACT_LIMIT = 100
HISTORY_TARGET_DISAMBIGUATION_HISTORY_LIMIT = 50
HISTORY_TARGET_DISAMBIGUATION_ANCHOR_COUNT = 4
HISTORY_TARGET_DISAMBIGUATION_MAX_CANDIDATES = 5
LIVE_CHECK_CHAT_NAME = "文件传输助手"
LIVE_CHECK_MESSAGE_PREFIX = "校验时间"
ACCOUNT_BINDINGS_FILENAME = "wechat_cli_account_bindings.json"
SYSTEM_CONTACT_USERNAMES = {
    "notifymessage",
    "weixin",
    "fmessage",
    "medianote",
    "floatbottle",
    "mphelper",
    "brandsessionholder",
    "filehelper",
    "weixinguanhaozhushou",
}

HISTORY_LINE_RE = re.compile(r"^\[(?P<time>\d{4}-\d{2}-\d{2} \d{2}:\d{2})\]\s+(?P<sender>.*?):\s*(?P<body>.*)$", re.S)
XML_ATTR_RE_TEMPLATE = r'{name}\s*=\s*"([^"]*)"'
FORWARDED_CHAT_RE = re.compile(r"^(?:.+?与.+?的)?聊天记录$")


@dataclass(frozen=True)
class LocalWechatCommandResult:
    ok: bool
    data: Any = None
    error: str = ""


@dataclass(frozen=True)
class LocalWechatReadResult:
    ok: bool
    items: list[Any]
    error: str = ""
    diagnostic: dict[str, Any] | None = None


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[1]


def bundled_wechat_cli_candidates() -> list[str]:
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
        *bundled_wechat_cli_candidates(),
        shutil.which("wechat-cli") or "",
        shutil.which("wechat-cli.exe") or "",
    ):
        value = str(value or "").strip()
        if value and os.path.isfile(value):
            return value
    return ""


def _state_dir(explicit: str = "") -> Path:
    return Path(explicit).expanduser() if explicit else Path.home() / ".wechat-cli"


def _account_bindings_file(explicit: str = "") -> Path:
    if explicit:
        return Path(explicit).expanduser()
    return _repo_root() / "data" / "config" / ACCOUNT_BINDINGS_FILENAME


def _normalize_path(value: str) -> str:
    text = _clean_text(value)
    if not text:
        return ""
    return os.path.normcase(os.path.normpath(text))


def _db_account_hint(db_dir: str) -> str:
    return Path(db_dir).parent.name if db_dir else ""


def load_wechat_cli_account_bindings(path: str = "") -> dict[str, Any]:
    target = _account_bindings_file(path)
    try:
        with target.open("r", encoding="utf-8") as file:
            data = json.load(file)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def save_wechat_cli_account_bindings(data: dict[str, Any], path: str = "") -> None:
    target = _account_bindings_file(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    with target.open("w", encoding="utf-8") as file:
        json.dump(data if isinstance(data, dict) else {}, file, ensure_ascii=False, indent=2)


def get_wechat_cli_account_binding(account_id: str, path: str = "") -> dict[str, Any]:
    account_id = _clean_text(account_id)
    data = load_wechat_cli_account_bindings(path)
    bindings = data.get("bindings") if isinstance(data, dict) else {}
    binding = bindings.get(account_id) if isinstance(bindings, dict) else None
    return binding if isinstance(binding, dict) else {}


def save_wechat_cli_account_binding(
    account_id: str,
    db_dir: str,
    *,
    path: str = "",
    method: str = "filehelper_live_check",
) -> dict[str, Any]:
    account_id = _clean_text(account_id)
    db_dir = _clean_text(db_dir)
    if not account_id or not db_dir:
        raise ValueError("account_id and db_dir are required")
    data = load_wechat_cli_account_bindings(path)
    bindings = data.get("bindings") if isinstance(data, dict) else {}
    if not isinstance(bindings, dict):
        bindings = {}
    binding = {
        "account_id": account_id,
        "db_dir": db_dir,
        "db_account_hint": _db_account_hint(db_dir),
        "verified_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "method": method,
    }
    bindings[account_id] = binding
    data["bindings"] = bindings
    save_wechat_cli_account_bindings(data, path)
    return binding


def wechat_cli_config_ready(state_dir: str = "") -> bool:
    target = _state_dir(state_dir)
    return (target / "config.json").is_file() and (target / "all_keys.json").is_file()


def wechat_cli_config_db_dir(state_dir: str = "") -> str:
    config_path = _state_dir(state_dir) / "config.json"
    try:
        with config_path.open("r", encoding="utf-8") as file:
            data = json.load(file)
        return _clean_text(data.get("db_dir")) if isinstance(data, dict) else ""
    except Exception:
        return ""


def wechat_cli_account_matches(
    expected_wx_id: str,
    state_dir: str = "",
    bindings_file: str = "",
) -> tuple[bool, str]:
    expected = _clean_text(expected_wx_id)
    if not expected:
        return True, ""
    db_dir = wechat_cli_config_db_dir(state_dir)
    if not db_dir:
        return False, "wechat-cli config missing db_dir"
    if not expected.lower().startswith("wxid_"):
        binding = get_wechat_cli_account_binding(expected, bindings_file)
        if not binding:
            return False, "wechat-cli account binding missing for current WeChat account"
        if _normalize_path(binding.get("db_dir")) == _normalize_path(db_dir):
            return True, ""
        return False, "wechat-cli account binding points to another database directory"
    normalized = db_dir.replace("\\", "/").lower()
    if expected.lower() in normalized:
        return True, ""
    return False, "wechat-cli config belongs to another WeChat account"


def build_live_check_message() -> str:
    stamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    return f"{LIVE_CHECK_MESSAGE_PREFIX}：{stamp}"


def verify_wechat_cli_live_binding(
    expected_wx_id: str,
    send_message,
    *,
    bindings_file: str = "",
    timeout: int = STATUS_CHECK_TIMEOUT_SECONDS,
) -> dict[str, Any]:
    expected = _clean_text(expected_wx_id)
    db_dir = wechat_cli_config_db_dir()
    if not expected:
        return {"ok": False, "error": "current WeChat account is unknown"}
    if not db_dir:
        return {"ok": False, "error": "wechat-cli config missing db_dir"}
    if not callable(send_message):
        return {"ok": False, "error": "live check sender is unavailable"}

    marker = build_live_check_message()
    try:
        send_result = send_message(marker)
    except Exception as exc:
        return {"ok": False, "error": f"failed to send live check message: {exc}"}
    if send_result is False:
        return {"ok": False, "error": "failed to send live check message"}

    deadline = time.time() + max(3, int(timeout or STATUS_CHECK_TIMEOUT_SECONDS))
    last_error = ""
    while time.time() <= deadline:
        result = run_wechat_cli_json(
            ["search", marker, "--limit", "10", "--format", "json"],
            timeout=max(3, min(10, int(timeout or STATUS_CHECK_TIMEOUT_SECONDS))),
        )
        if result.ok and isinstance(result.data, dict):
            results = result.data.get("results")
            if isinstance(results, list) and any(marker in str(item) for item in results):
                binding = save_wechat_cli_account_binding(
                    expected,
                    db_dir,
                    path=bindings_file,
                    method="filehelper_live_check",
                )
                return {"ok": True, "binding": binding}
        elif not result.ok:
            last_error = sanitize_error(result.error)
        time.sleep(1)
    return {"ok": False, "error": last_error or "live check message was not found in wechat-cli search"}


def _command_env() -> dict[str, str]:
    env = dict(os.environ)
    env["PYTHONUTF8"] = "1"
    env["PYTHONIOENCODING"] = "utf-8"
    return env


def run_wechat_cli_json(
    args: list[str],
    *,
    executable: str = "",
    timeout: int = DEFAULT_TIMEOUT_SECONDS,
) -> LocalWechatCommandResult:
    exe = find_wechat_cli_executable(executable)
    if not exe:
        return LocalWechatCommandResult(False, error="wechat-cli executable not found")
    if not wechat_cli_config_ready():
        return LocalWechatCommandResult(False, error="wechat-cli config not initialized")
    try:
        completed = subprocess.run(
            [exe, *args],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=max(1, int(timeout or DEFAULT_TIMEOUT_SECONDS)),
            check=False,
            env=_command_env(),
        )
    except Exception as exc:
        return LocalWechatCommandResult(False, error=str(exc))
    if completed.returncode != 0:
        return LocalWechatCommandResult(
            False,
            error=(completed.stderr or completed.stdout or "").strip(),
        )
    try:
        return LocalWechatCommandResult(True, data=json.loads((completed.stdout or "").strip() or "null"))
    except Exception as exc:
        return LocalWechatCommandResult(False, error=f"invalid json: {exc}")


def switch_wechat_cli_to_bound_account(
    expected_wx_id: str,
    *,
    executable: str = "",
    timeout: int = 60,
    bindings_file: str = "",
) -> LocalWechatCommandResult:
    expected = _clean_text(expected_wx_id)
    if not expected or expected.lower().startswith("wxid_"):
        return LocalWechatCommandResult(False, error="bound account switch only supports account namespace bindings")
    binding = get_wechat_cli_account_binding(expected, bindings_file)
    db_dir = _clean_text(binding.get("db_dir"))
    if not db_dir:
        return LocalWechatCommandResult(False, error="wechat-cli account binding missing for current WeChat account")
    if not Path(db_dir).is_dir():
        return LocalWechatCommandResult(False, error="bound wechat-cli database directory does not exist")
    exe = find_wechat_cli_executable(executable)
    if not exe:
        return LocalWechatCommandResult(False, error="wechat-cli executable not found")
    try:
        completed = subprocess.run(
            [exe, "init", "--db-dir", db_dir, "--force"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=max(1, int(timeout or 60)),
            check=False,
            env=_command_env(),
        )
    except Exception as exc:
        return LocalWechatCommandResult(False, error=str(exc))
    if completed.returncode != 0:
        return LocalWechatCommandResult(False, error=sanitize_error(completed.stderr or completed.stdout))
    account_ok, account_error = wechat_cli_account_matches(expected, bindings_file=bindings_file)
    if not account_ok:
        return LocalWechatCommandResult(False, error=account_error)
    return LocalWechatCommandResult(True, data={"db_dir": db_dir})


def ensure_wechat_cli_account_ready(
    expected_wx_id: str,
    *,
    executable: str = "",
    bindings_file: str = "",
) -> tuple[bool, str]:
    account_ok, account_error = wechat_cli_account_matches(expected_wx_id, bindings_file=bindings_file)
    if account_ok:
        return True, ""
    expected = _clean_text(expected_wx_id)
    if expected and not expected.lower().startswith("wxid_"):
        switch_result = switch_wechat_cli_to_bound_account(
            expected,
            executable=executable,
            bindings_file=bindings_file,
        )
        if switch_result.ok:
            return wechat_cli_account_matches(expected, bindings_file=bindings_file)
        if switch_result.error:
            return False, switch_result.error
    return False, account_error


def _clean_text(value: Any) -> str:
    return str(value or "").replace("\r\n", "\n").replace("\r", "\n").strip()


def sanitize_error(value: Any) -> str:
    text = _clean_text(value)
    if not text:
        return ""
    home = str(Path.home())
    if home:
        text = text.replace(home, "%USERPROFILE%")
    return text[:300]


def _wechat_cli_version(executable: str, *, timeout: int = STATUS_CHECK_TIMEOUT_SECONDS) -> str:
    if not executable:
        return ""
    try:
        completed = subprocess.run(
            [executable, "--version"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=max(1, int(timeout or STATUS_CHECK_TIMEOUT_SECONDS)),
            check=False,
            env=_command_env(),
        )
    except Exception:
        return ""
    output = (completed.stdout or completed.stderr or "").strip()
    if completed.returncode != 0 or not output:
        return ""
    return output.splitlines()[0].strip()[:80]


def _wechat_cli_python_executable(executable: str) -> str:
    path = Path(str(executable or "")).resolve()
    scripts_dir = path.parent
    env_dir = scripts_dir.parent
    candidates = [
        env_dir / "python.exe",
        env_dir / "python",
        scripts_dir / "python.exe",
        scripts_dir / "python",
        Path(sys.executable),
    ]
    for candidate in candidates:
        try:
            if candidate.is_file():
                return str(candidate)
        except Exception:
            continue
    return sys.executable


def _installed_direct_url_metadata(executable: str, *, timeout: int = STATUS_CHECK_TIMEOUT_SECONDS) -> dict[str, Any]:
    python_exe = _wechat_cli_python_executable(executable)
    script = (
        "import importlib.metadata, json\n"
        "try:\n"
        "    dist = importlib.metadata.distribution('wechat-cli')\n"
        "    text = dist.read_text('direct_url.json') or '{}'\n"
        "    print(text)\n"
        "except Exception:\n"
        "    print('{}')\n"
    )
    try:
        completed = subprocess.run(
            [python_exe, "-c", script],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=max(1, int(timeout or STATUS_CHECK_TIMEOUT_SECONDS)),
            check=False,
            env=_command_env(),
        )
        if completed.returncode != 0:
            return {}
        data = json.loads((completed.stdout or "").strip() or "{}")
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _git_remote_head(url: str, *, timeout: int = UPDATE_CHECK_TIMEOUT_SECONDS) -> str:
    url = _clean_text(url)
    if not url:
        return ""
    try:
        completed = subprocess.run(
            ["git", "ls-remote", url, "HEAD"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=max(1, int(timeout or UPDATE_CHECK_TIMEOUT_SECONDS)),
            check=False,
            env=_command_env(),
        )
    except Exception:
        return ""
    if completed.returncode != 0:
        return ""
    first = (completed.stdout or "").strip().splitlines()
    if not first:
        return ""
    commit = first[0].split()[0].strip()
    return commit if re.fullmatch(r"[0-9a-fA-F]{40}", commit) else ""


def check_wechat_cli_update(*, timeout: int = UPDATE_CHECK_TIMEOUT_SECONDS) -> dict[str, Any]:
    """Check whether the installed wechat-cli git source has a newer HEAD."""
    checked_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    executable = find_wechat_cli_executable()
    if not executable:
        return {
            "ok": False,
            "update_available": False,
            "state": "missing_tool",
            "title": "未找到 wechat-cli 工具",
            "message": "无法检查更新，请先安装 wechat-cli。",
            "checked_at": checked_at,
            "local_version": "",
            "local_commit": "",
            "remote_commit": "",
            "source_url": "",
        }

    metadata = _installed_direct_url_metadata(executable, timeout=timeout)
    vcs_info = metadata.get("vcs_info") if isinstance(metadata.get("vcs_info"), dict) else {}
    source_url = _clean_text(metadata.get("url"))
    local_commit = _clean_text(vcs_info.get("commit_id"))
    local_version = _wechat_cli_version(executable, timeout=min(timeout, STATUS_CHECK_TIMEOUT_SECONDS))
    if not source_url or not local_commit:
        return {
            "ok": False,
            "update_available": False,
            "state": "unsupported_install",
            "title": "无法确认安装来源",
            "message": "当前 wechat-cli 未记录 Git 安装来源，只能显示版本，不能比较远端更新。",
            "checked_at": checked_at,
            "local_version": local_version,
            "local_commit": local_commit,
            "remote_commit": "",
            "source_url": source_url,
        }

    remote_commit = _git_remote_head(source_url, timeout=timeout)
    if not remote_commit:
        return {
            "ok": False,
            "update_available": False,
            "state": "remote_check_failed",
            "title": "远端版本检查失败",
            "message": "无法连接 wechat-cli 源仓库，稍后可重新检查。",
            "checked_at": checked_at,
            "local_version": local_version,
            "local_commit": local_commit,
            "remote_commit": "",
            "source_url": source_url,
        }

    update_available = remote_commit.lower() != local_commit.lower()
    return {
        "ok": True,
        "update_available": update_available,
        "state": "update_available" if update_available else "up_to_date",
        "title": "发现 wechat-cli 可用更新" if update_available else "wechat-cli 已是最新",
        "message": "检测到源仓库有新提交；当前只提示，不会自动更新。" if update_available else "当前安装版本与源仓库 HEAD 一致。",
        "checked_at": checked_at,
        "local_version": local_version,
        "local_commit": local_commit[:12],
        "remote_commit": remote_commit[:12],
        "source_url": source_url,
    }


def check_wechat_cli_status(
    *,
    timeout: int = STATUS_CHECK_TIMEOUT_SECONDS,
    expected_wx_id: str = "",
    live_check_sender=None,
    bindings_file: str = "",
) -> dict[str, Any]:
    """Return a safe dashboard status for the optional local reader."""
    checked_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    executable = find_wechat_cli_executable()
    if not executable:
        return {
            "available": False,
            "state": "missing_tool",
            "title": "未找到 wechat-cli 工具",
            "message": "本地高速读取暂不可用，机器人会自动回退微信界面读取。",
            "detail": "请确认工具已安装到 venv/tools/wechat-cli/，或设置 WXBOT_WECHAT_CLI_EXE。",
            "checked_at": checked_at,
            "version": "",
        }

    version = _wechat_cli_version(executable, timeout=timeout)
    if not wechat_cli_config_ready():
        return {
            "available": False,
            "state": "need_init",
            "title": "工具已安装，但尚未完成初始化",
            "message": "本地高速读取暂不可用，机器人会自动回退微信界面读取。",
            "detail": "请先确认当前微信账号对应的本地数据库已完成 wechat-cli 初始化。",
            "checked_at": checked_at,
            "version": version,
        }

    db_dir = wechat_cli_config_db_dir()
    account_ok, account_error = wechat_cli_account_matches(expected_wx_id, bindings_file=bindings_file)
    if not account_ok and expected_wx_id:
        switch_result = switch_wechat_cli_to_bound_account(
            expected_wx_id,
            executable=executable,
            timeout=max(timeout, 30),
            bindings_file=bindings_file,
        )
        if switch_result.ok:
            db_dir = wechat_cli_config_db_dir()
            account_ok, account_error = wechat_cli_account_matches(expected_wx_id, bindings_file=bindings_file)
        elif callable(live_check_sender):
            live_result = verify_wechat_cli_live_binding(
                expected_wx_id,
                live_check_sender,
                bindings_file=bindings_file,
                timeout=timeout,
            )
            if live_result.get("ok"):
                db_dir = wechat_cli_config_db_dir()
                account_ok, account_error = wechat_cli_account_matches(expected_wx_id, bindings_file=bindings_file)
            else:
                account_error = live_result.get("error") or switch_result.error or account_error
        elif switch_result.error:
            account_error = switch_result.error
    if not account_ok:
        return {
            "available": False,
            "state": "account_unverified",
            "title": "当前微信账号尚未完成绑定",
            "message": "本地高速读取暂不可用，机器人会自动回退微信界面读取。",
            "detail": sanitize_error(account_error),
            "checked_at": checked_at,
            "version": version,
            "db_account_hint": _db_account_hint(db_dir),
        }

    result = run_wechat_cli_json(["contacts", "--limit", "1"], executable=executable, timeout=timeout)
    if not result.ok:
        return {
            "available": False,
            "state": "read_failed",
            "title": "本地数据库读取失败",
            "message": "本地高速读取暂不可用，机器人会自动回退微信界面读取。",
            "detail": sanitize_error(result.error) or "contacts 轻量读取命令执行失败。",
            "checked_at": checked_at,
            "version": version,
        }
    if not isinstance(result.data, list):
        return {
            "available": False,
            "state": "invalid_output",
            "title": "工具输出格式异常",
            "message": "本地高速读取暂不可用，机器人会自动回退微信界面读取。",
            "detail": "contacts 轻量读取没有返回预期 JSON 列表。",
            "checked_at": checked_at,
            "version": version,
        }
    return {
        "available": True,
        "state": "available",
        "title": "wechat-cli 本地读取可用" if expected_wx_id else "wechat-cli 工具可读",
        "message": (
            "通讯录维护和聊天记录自动补全等功能将会优先使用该插件。"
            if expected_wx_id
            else "机器人启动后会自动校验当前微信账号绑定，通过后优先使用该插件。"
        ),
        "detail": "",
        "checked_at": checked_at,
        "version": version,
        "db_account_hint": _db_account_hint(db_dir),
        "account_verified": bool(expected_wx_id),
    }


def _is_system_or_public_contact(item: dict[str, Any]) -> bool:
    username = _clean_text(item.get("username"))
    if not username:
        return True
    if username in SYSTEM_CONTACT_USERNAMES:
        return True
    if username.startswith("gh_"):
        return True
    if username.endswith("@chatroom"):
        return True
    if "@openim" in username:
        return True
    if not _clean_text(item.get("nick_name") or item.get("nickname")):
        return True
    return False


def normalize_wechat_cli_contact(item: dict[str, Any]) -> dict[str, Any]:
    username = _clean_text(item.get("username"))
    nickname = _clean_text(item.get("nick_name") or item.get("nickname"))
    remark = _clean_text(item.get("remark"))
    alias = _clean_text(item.get("alias"))
    return {
        "微信号": alias or username,
        "wxid": username,
        "昵称": nickname,
        "备注": remark,
        "wechat_id": alias or username,
        "nickname": nickname,
        "remark": remark,
    }


def read_local_contacts_with_status(
    *,
    limit: int = 50,
    executable: str = "",
    include_system: bool = False,
    expected_wx_id: str = "",
) -> LocalWechatReadResult:
    account_ok, account_error = ensure_wechat_cli_account_ready(expected_wx_id, executable=executable)
    if not account_ok:
        return LocalWechatReadResult(False, [], account_error)
    fetch_limit = max(1, min(MAX_CONTACT_FETCH_LIMIT, int(limit or 50) * (3 if not include_system else 1)))
    result = run_wechat_cli_json(["contacts", "--limit", str(fetch_limit)], executable=executable)
    if not result.ok or not isinstance(result.data, list):
        return LocalWechatReadResult(False, [], sanitize_error(result.error) or "wechat-cli contacts returned invalid data")
    contacts = []
    for item in result.data:
        if not isinstance(item, dict):
            continue
        if not include_system and _is_system_or_public_contact(item):
            continue
        contacts.append(normalize_wechat_cli_contact(item))
        if len(contacts) >= max(1, int(limit or 50)):
            break
    return LocalWechatReadResult(True, contacts)


def read_local_contacts(
    *,
    limit: int = 50,
    executable: str = "",
    include_system: bool = False,
    expected_wx_id: str = "",
) -> list[dict[str, Any]]:
    return read_local_contacts_with_status(
        limit=limit,
        executable=executable,
        include_system=include_system,
        expected_wx_id=expected_wx_id,
    ).items


def _xml_attr(text: str, name: str) -> str:
    match = re.search(XML_ATTR_RE_TEMPLATE.format(name=re.escape(name)), text)
    return _clean_text(match.group(1)) if match else ""


def _strip_prefix(text: str, prefix: str) -> str:
    return _clean_text(text[len(prefix):]) if text.startswith(prefix) else _clean_text(text)


def _is_forwarded_chat_tail(text: str) -> bool:
    tail = _clean_text(text)
    if not tail:
        return False
    return bool(FORWARDED_CHAT_RE.fullmatch(tail)) or tail.startswith("聊天记录：") or tail.startswith("聊天记录:")


def _classify_link_or_file_history_body(body: str) -> tuple[str, str]:
    tail = _strip_prefix(body, "[链接/文件]")
    if _is_forwarded_chat_tail(tail):
        return "merge", f"[聊天记录] {tail}".strip()
    if tail.startswith("位置：") or tail.startswith("位置:"):
        tail = re.sub(r"^位置\s*[:：]?\s*", "", tail).strip()
        return "location", f"[位置] {tail}".strip()
    if tail.startswith(("笔记：", "笔记:", "收藏：", "收藏:")):
        tail = re.sub(r"^(?:笔记|收藏)\s*[:：]?\s*", "", tail).strip()
        return "note", f"[笔记] {tail}".strip()
    return "link", body


def _classify_history_body(body: str) -> tuple[str, str]:
    body = _clean_text(body)
    if body.startswith("[图片]"):
        return "image", "[图片]"
    if body.startswith("[语音]"):
        tail = _strip_prefix(body, "[语音]")
        if tail.startswith("<") or "voicemsg" in tail.lower():
            return "voice", "一条语音消息（未识别出文字）"
        return "voice", tail or "一条语音消息（未识别出文字）"
    if body.startswith("[表情]"):
        return "emotion", body
    if body.startswith("[视频]"):
        return "video", body
    if body.startswith("[名片]"):
        tail = _strip_prefix(body, "[名片]")
        display_name = _xml_attr(tail, "nickname") or _xml_attr(tail, "alias")
        if not display_name and tail.startswith("<"):
            return "personal_card", "[名片]"
        return "personal_card", f"[名片] {display_name or tail}".strip()
    if body.startswith("[位置]"):
        return "location", body
    if body.startswith("[小程序]"):
        return "miniapp", body
    if body.startswith("[链接]"):
        return "link", body
    if body.startswith("[文件]"):
        return "file", body
    if body.startswith("[笔记]") or body.startswith("[收藏]"):
        return "note", body
    if body.startswith("[聊天记录]"):
        return "merge", body
    if body.startswith("[合并转发]"):
        return "merge", body.replace("[合并转发]", "[聊天记录]", 1)
    if body.startswith("[链接/文件]"):
        return _classify_link_or_file_history_body(body)
    return "text", body


def parse_wechat_cli_history_line(line: str, *, chat_name: str = "") -> SimpleNamespace | None:
    match = HISTORY_LINE_RE.match(_clean_text(line))
    if not match:
        return None
    msg_type, content = _classify_history_body(match.group("body"))
    if not content and msg_type == "text":
        return None
    sender = _clean_text(match.group("sender"))
    attr = "self" if sender.lower() == "me" else "friend"
    time_text = match.group("time").replace("-", "/") + ":00"
    return SimpleNamespace(
        type=msg_type,
        attr=attr,
        sender=sender if attr != "self" else "me",
        content=content,
        time=time_text,
        who=chat_name,
    )


def _history_read_diagnostic(
    started_at: float,
    *,
    chat_name: str,
    chat_type: str,
    expected_wx_id: str,
    limit: int,
    history_target: str = "",
    resolution_source: str = "",
    error_stage: str = "",
) -> dict[str, Any]:
    return {
        "chat_name": _clean_text(chat_name),
        "chat_type": _clean_text(chat_type) or "private",
        "expected_wx_id": _clean_text(expected_wx_id),
        "limit": max(1, int(limit or 30)),
        "history_target": _clean_text(history_target),
        "resolution_source": _clean_text(resolution_source),
        "error_stage": _clean_text(error_stage),
        "elapsed_ms": int((time.perf_counter() - started_at) * 1000),
    }


def read_local_history_messages_with_status(
    chat_name: str,
    *,
    limit: int = 30,
    executable: str = "",
    expected_wx_id: str = "",
    anchor_messages: list[Any] | None = None,
    chat_type: str = "private",
) -> LocalWechatReadResult:
    started_at = time.perf_counter()
    chat_name = _clean_text(chat_name)
    normalized_chat_type = _clean_text(chat_type).lower() or "private"
    normalized_limit = max(1, int(limit or 30))

    def finish(
        ok: bool,
        items: list[Any] | None = None,
        error: str = "",
        *,
        history_target: str = "",
        resolution_source: str = "",
        error_stage: str = "",
    ) -> LocalWechatReadResult:
        return LocalWechatReadResult(
            ok,
            list(items or []),
            error,
            _history_read_diagnostic(
                started_at,
                chat_name=chat_name,
                chat_type=normalized_chat_type,
                expected_wx_id=expected_wx_id,
                limit=normalized_limit,
                history_target=history_target,
                resolution_source=resolution_source,
                error_stage=error_stage,
            ),
        )

    if not chat_name:
        return finish(False, error="empty chat name", error_stage="input")
    account_ok, account_error = ensure_wechat_cli_account_ready(expected_wx_id, executable=executable)
    if not account_ok:
        return finish(False, error=account_error, error_stage="account_check")
    history_target = chat_name
    resolution_source = "display_name_direct"
    if normalized_chat_type == "group":
        history_target, target_error, resolution_source = _resolve_group_history_target_from_sessions(
            chat_name,
            executable=executable,
            anchor_messages=anchor_messages,
        )
        if not history_target:
            return finish(
                False,
                error=target_error,
                history_target=history_target,
                resolution_source=resolution_source,
                error_stage="target_resolution",
            )
    elif _clean_text(expected_wx_id):
        history_target, target_error, resolution_source = _resolve_history_target_from_contacts(
            chat_name,
            executable=executable,
            anchor_messages=anchor_messages,
        )
        if not history_target:
            return finish(
                False,
                error=target_error,
                history_target=history_target,
                resolution_source=resolution_source,
                error_stage="target_resolution",
            )
    result = run_wechat_cli_json(
        ["history", history_target, "--limit", str(normalized_limit)],
        executable=executable,
    )
    if not result.ok or not isinstance(result.data, dict):
        return finish(
            False,
            error=sanitize_error(result.error) or "wechat-cli history returned invalid data",
            history_target=history_target,
            resolution_source=resolution_source,
            error_stage="history_command",
        )
    messages = result.data.get("messages")
    if not isinstance(messages, list):
        return finish(
            False,
            error="wechat-cli history missing messages",
            history_target=history_target,
            resolution_source=resolution_source,
            error_stage="history_payload",
        )
    parsed = []
    for line in messages:
        if not isinstance(line, str):
            continue
        item = parse_wechat_cli_history_line(line, chat_name=chat_name)
        if item is not None:
            parsed.append(item)
    if len(parsed) >= 2 and str(parsed[0].time) > str(parsed[-1].time):
        parsed.reverse()
    return finish(
        True,
        parsed,
        history_target=history_target,
        resolution_source=resolution_source,
        error_stage="",
    )


def read_local_history_messages(
    chat_name: str,
    *,
    limit: int = 30,
    executable: str = "",
    expected_wx_id: str = "",
    anchor_messages: list[Any] | None = None,
    chat_type: str = "private",
) -> list[SimpleNamespace]:
    return read_local_history_messages_with_status(
        chat_name,
        limit=limit,
        executable=executable,
        expected_wx_id=expected_wx_id,
        anchor_messages=anchor_messages,
        chat_type=chat_type,
    ).items


def _normalize_wechat_cli_session(item: dict[str, Any]) -> dict[str, str]:
    return {
        "name": _clean_text(item.get("chat")),
        "content": _clean_text(item.get("last_message")),
        "time": _clean_text(item.get("time")),
        "info": _clean_text(item.get("msg_type")),
        "username": _clean_text(item.get("username")),
        "is_group": bool(item.get("is_group")),
    }


def _contact_exact_name_match(item: dict[str, Any], chat_name: str) -> bool:
    expected = _clean_text(chat_name)
    if not expected:
        return False
    values = {
        _clean_text(item.get("username")),
        _clean_text(item.get("nick_name") or item.get("nickname")),
        _clean_text(item.get("remark")),
        _clean_text(item.get("alias")),
    }
    return expected in values


def _history_anchor_direction(attr: str, sender: str) -> str:
    attr = _clean_text(attr).lower()
    sender = _clean_text(sender).lower()
    return "self" if attr == "self" or sender in {"self", "me"} else "other"


def _relaxed_history_fingerprint(item: Any, *, chat_type: str = "private") -> str:
    if isinstance(item, dict):
        msg_type = _clean_text(item.get("type")).lower() or "text"
        attr = _clean_text(item.get("attr")).lower()
        sender = _clean_text(item.get("sender"))
        content = _clean_text(item.get("content"))
    else:
        msg_type = _clean_text(getattr(item, "type", "")).lower() or "text"
        attr = _clean_text(getattr(item, "attr", "")).lower()
        sender = _clean_text(getattr(item, "sender", ""))
        content = _clean_text(getattr(item, "content", ""))
    if msg_type == "other":
        msg_type = "text"
    if msg_type == "voice":
        return ""
    if msg_type == "image":
        content = "[图片]"
    if not content and msg_type not in {"image", "emotion", "voice", "video", "file"}:
        return ""
    direction = _history_anchor_direction(attr, sender)
    parts = [direction]
    if _clean_text(chat_type).lower() == "group" and direction != "self":
        if not sender:
            return ""
        parts.append(sender)
    parts.extend([msg_type, content])
    return "|".join(parts)


def _recent_anchor_fingerprints(
    anchor_messages: list[Any] | None,
    *,
    limit: int = 5,
    chat_type: str = "private",
) -> list[str]:
    fingerprints = []
    for item in anchor_messages or []:
        fp = _relaxed_history_fingerprint(item, chat_type=chat_type)
        if fp:
            fingerprints.append(fp)
    return fingerprints[-max(1, int(limit or 5)):]


def _longest_ordered_anchor_score(candidate_messages: list[Any], anchor_fps: list[str], *, chat_type: str = "private") -> int:
    candidate_fps = [_relaxed_history_fingerprint(item, chat_type=chat_type) for item in candidate_messages or []]
    candidate_fps = [fp for fp in candidate_fps if fp]
    if not candidate_fps or not anchor_fps:
        return 0
    max_size = min(len(anchor_fps), len(candidate_fps))
    for size in range(max_size, 0, -1):
        sequence = anchor_fps[-size:]
        for start in range(0, len(candidate_fps) - size + 1):
            if candidate_fps[start:start + size] == sequence:
                return size
    return 0


def _parse_history_messages_payload(data: Any, *, chat_name: str) -> list[SimpleNamespace]:
    if not isinstance(data, dict):
        return []
    messages = data.get("messages")
    if not isinstance(messages, list):
        return []
    parsed = []
    for line in messages:
        if not isinstance(line, str):
            continue
        item = parse_wechat_cli_history_line(line, chat_name=chat_name)
        if item is not None:
            parsed.append(item)
    if len(parsed) >= 2 and str(parsed[0].time) > str(parsed[-1].time):
        parsed.reverse()
    return parsed


def _resolve_ambiguous_history_target(
    chat_name: str,
    candidate_usernames: list[str],
    *,
    executable: str = "",
    anchor_messages: list[Any] | None = None,
    chat_type: str = "private",
) -> tuple[str, str, str]:
    candidates = sorted({_clean_text(username) for username in candidate_usernames if _clean_text(username)})
    if len(candidates) <= 1:
        if candidates:
            return candidates[0], "", "single_candidate"
        return "", "wechat-cli history target could not be resolved uniquely", "candidate_empty"

    candidate_pool = list(candidates)
    sessions_result = run_wechat_cli_json(
        ["sessions", "--limit", str(HISTORY_TARGET_DISAMBIGUATION_SESSION_LIMIT), "--format", "json"],
        executable=executable,
    )
    if sessions_result.ok and isinstance(sessions_result.data, list):
        session_matches = []
        for item in sessions_result.data:
            if not isinstance(item, dict):
                continue
            session = _normalize_wechat_cli_session(item)
            if session["name"] == chat_name and session["username"] in candidates:
                session_matches.append(session["username"])
        unique_session_matches = sorted(set(session_matches))
        if unique_session_matches:
            candidate_pool = unique_session_matches
    if len(candidate_pool) > HISTORY_TARGET_DISAMBIGUATION_MAX_CANDIDATES:
        return "", "wechat-cli history target has too many ambiguous candidates", "too_many_candidates"

    anchor_fps = _recent_anchor_fingerprints(
        anchor_messages,
        limit=HISTORY_TARGET_DISAMBIGUATION_ANCHOR_COUNT,
        chat_type=chat_type,
    )
    if len(anchor_fps) < HISTORY_TARGET_DISAMBIGUATION_ANCHOR_COUNT:
        return "", "wechat-cli history target is ambiguous for this chat name", "ambiguous_insufficient_anchors"

    scores: dict[str, int] = {}
    for username in candidate_pool:
        history_result = run_wechat_cli_json(
            [
                "history",
                username,
                "--limit",
                str(HISTORY_TARGET_DISAMBIGUATION_HISTORY_LIMIT),
                "--format",
                "json",
            ],
            executable=executable,
        )
        if not history_result.ok:
            continue
        candidate_messages = _parse_history_messages_payload(history_result.data, chat_name=chat_name)
        scores[username] = _longest_ordered_anchor_score(candidate_messages, anchor_fps, chat_type=chat_type)
    best_score = max(scores.values(), default=0)
    winners = [
        username
        for username, score in scores.items()
        if score == best_score and score >= HISTORY_TARGET_DISAMBIGUATION_ANCHOR_COUNT
    ]
    if len(winners) == 1:
        return winners[0], "", "ambiguous_anchor_match"
    return "", "wechat-cli history target is ambiguous for this chat name", "ambiguous_anchor_no_winner"


def _resolve_history_target_from_contacts(
    chat_name: str,
    *,
    executable: str = "",
    anchor_messages: list[Any] | None = None,
) -> tuple[str, str, str]:
    """Resolve a display name to one unique wechat-cli history target.

    wechat-cli history accepts usernames such as wxid/filehelper in practice,
    which is safer than display names because remarks/nicknames can collide.
    """
    chat_name = _clean_text(chat_name)
    if not chat_name:
        return "", "empty chat name", "input"
    if chat_name.lower().startswith("wxid_") or chat_name == "filehelper":
        return chat_name, "", "direct_id"
    result = run_wechat_cli_json(
        [
            "contacts",
            "--query",
            chat_name,
            "--limit",
            str(HISTORY_TARGET_DISAMBIGUATION_CONTACT_LIMIT),
            "--format",
            "json",
        ],
        executable=executable,
    )
    if not result.ok:
        return "", sanitize_error(result.error) or "wechat-cli contacts query failed", "contacts_query_failed"
    if not isinstance(result.data, list):
        return "", "wechat-cli contacts query returned invalid data", "contacts_invalid_payload"
    matched_usernames = []
    for item in result.data:
        if not isinstance(item, dict) or _is_system_or_public_contact(item):
            continue
        username = _clean_text(item.get("username"))
        if username and _contact_exact_name_match(item, chat_name):
            matched_usernames.append(username)
    unique_usernames = sorted(set(matched_usernames))
    if len(unique_usernames) == 1:
        return unique_usernames[0], "", "contacts_exact"
    if len(unique_usernames) > 1:
        return _resolve_ambiguous_history_target(
            chat_name,
            unique_usernames,
            executable=executable,
            anchor_messages=anchor_messages,
        )
    return "", "wechat-cli history target could not be resolved uniquely", "contacts_no_exact_match"


def _resolve_group_history_target_from_sessions(
    chat_name: str,
    *,
    executable: str = "",
    anchor_messages: list[Any] | None = None,
) -> tuple[str, str, str]:
    chat_name = _clean_text(chat_name)
    if not chat_name:
        return "", "empty chat name", "input"
    if chat_name.endswith("@chatroom"):
        return chat_name, "", "direct_chatroom_id"
    result = run_wechat_cli_json(
        [
            "sessions",
            "--limit",
            str(HISTORY_TARGET_DISAMBIGUATION_SESSION_LIMIT),
            "--format",
            "json",
        ],
        executable=executable,
    )
    if not result.ok:
        return "", sanitize_error(result.error) or "wechat-cli sessions query failed", "sessions_query_failed"
    if not isinstance(result.data, list):
        return "", "wechat-cli sessions query returned invalid data", "sessions_invalid_payload"
    usernames = []
    for item in result.data:
        if not isinstance(item, dict):
            continue
        session = _normalize_wechat_cli_session(item)
        if session["is_group"] and session["name"] == chat_name and session["username"]:
            usernames.append(session["username"])
    unique_usernames = sorted(set(usernames))
    if len(unique_usernames) == 1:
        return unique_usernames[0], "", "sessions_exact"
    if len(unique_usernames) > 1:
        return _resolve_ambiguous_history_target(
            chat_name,
            unique_usernames,
            executable=executable,
            anchor_messages=anchor_messages,
            chat_type="group",
        )
    return "", "wechat-cli group history target could not be resolved uniquely", "sessions_no_exact_match"


def read_local_sessions_with_status(
    *,
    limit: int = 200,
    executable: str = "",
    expected_wx_id: str = "",
    include_groups: bool = False,
) -> LocalWechatReadResult:
    account_ok, account_error = ensure_wechat_cli_account_ready(expected_wx_id, executable=executable)
    if not account_ok:
        return LocalWechatReadResult(False, [], account_error)
    result = run_wechat_cli_json(
        ["sessions", "--limit", str(max(1, int(limit or 200))), "--format", "json"],
        executable=executable,
    )
    if not result.ok or not isinstance(result.data, list):
        return LocalWechatReadResult(False, [], sanitize_error(result.error) or "wechat-cli sessions returned invalid data")
    sessions = []
    for item in result.data:
        if not isinstance(item, dict):
            continue
        session = _normalize_wechat_cli_session(item)
        if not session["name"]:
            continue
        if session["is_group"] and not include_groups:
            continue
        sessions.append(session)
    return LocalWechatReadResult(True, sessions)
