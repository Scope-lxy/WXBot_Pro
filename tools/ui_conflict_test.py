from __future__ import annotations

import argparse
import json
import sys
import threading
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import requests


DEFAULT_CONTACTS = [
    "阿英2",
    "阿英3",
    "阿英4",
    "炳3",
    "炳4",
    "瑞东（私人号）",
    "深情瑞弟",
    "帅弟情深",
    "追梦瑞弟",
]

DEFAULT_CONFIG = {
    "base_url": "http://127.0.0.1:10001",
    "username": "admin",
    "password": "123456",
    "contacts": list(DEFAULT_CONTACTS),
    "scheduled_targets": 3,
    "material_targets": 3,
    "scheduled_message_text": "【WXBot 冲突测试】定时消息，请忽略。",
    "contact_refresh_mode": "test",
    "contact_refresh_start_name": "",
    "task_timeout_seconds": 240,
    "poll_interval_seconds": 2,
    "task_only_delay_seconds": 6,
    "maintenance_overlap_delay_seconds": 4,
}

REPORT_DIR = Path("backups") / "ui_conflict_tests"
CONFIG_PATH = Path("tools") / "ui_conflict_test_config.json"
EXAMPLE_CONFIG_PATH = Path("tools") / "ui_conflict_test_config.example.json"


def _clean_text(value: Any) -> str:
    return str(value or "").strip()


def _iso_seconds(dt: datetime) -> str:
    return dt.replace(microsecond=0).isoformat()


def _future_time_parts(delay_seconds: int) -> tuple[datetime, str]:
    run_at = datetime.now() + timedelta(seconds=max(0, int(delay_seconds or 0)))
    return run_at.replace(microsecond=0), run_at.strftime("%Y-%m-%d")


def _recent_label() -> str:
    return datetime.now().strftime("%Y%m%d_%H%M%S")


def _unique_task_id(prefix: str) -> str:
    return f"ui_conflict_{prefix}_{_recent_label()}_{uuid.uuid4().hex[:8]}"


def _pick_targets(contacts: list[str], count: int) -> list[str]:
    count = max(1, int(count or 1))
    targets: list[str] = []
    for item in contacts:
        text = _clean_text(item)
        if text and text not in targets:
            targets.append(text)
        if len(targets) >= count:
            break
    return targets


def build_scheduled_message_task(
    *,
    contacts: list[str],
    text: str,
    delay_seconds: int,
    target_count: int,
    phase_label: str,
) -> dict[str, Any]:
    run_at, run_day = _future_time_parts(delay_seconds)
    targets = _pick_targets(contacts, target_count)
    return {
        "id": _unique_task_id("scheduled"),
        "name": f"冲突测试-定时消息-{phase_label}",
        "enabled": True,
        "status": "pending",
        "targets_mode": "direct",
        "targets": targets,
        "manual_target_names": [],
        "msgs": [f"{text} [{phase_label}]"],
        "schedule_mode": "fixed_at",
        "trigger_kind": "fixed",
        "repeat_mode": "once",
        "repeat_rule": "custom_dates",
        "repeat_values": [run_day],
        "time_value": run_at.strftime("%H:%M"),
        "start_at": run_at.strftime("%Y-%m-%dT%H:%M"),
        "fire_at": _iso_seconds(run_at),
        "next_run_at": _iso_seconds(run_at),
    }


def build_material_outreach_task(
    *,
    contacts: list[str],
    delay_seconds: int,
    target_count: int,
    phase_label: str,
) -> dict[str, Any]:
    run_at, run_day = _future_time_parts(delay_seconds)
    targets = _pick_targets(contacts, target_count)
    return {
        "id": _unique_task_id("material"),
        "name": f"冲突测试-素材转发-{phase_label}",
        "enabled": True,
        "status": "pending",
        "targets": [],
        "manual_target_names": targets,
        "material_types": ["all"],
        "trigger_strategy": "fixed",
        "mode": "fixed",
        "schedule_mode": "fixed_at",
        "repeat_mode": "once",
        "repeat_rule": "custom_dates",
        "repeat_values": [run_day],
        "time_value": run_at.strftime("%H:%M"),
        "start_at": run_at.strftime("%Y-%m-%dT%H:%M"),
        "fire_at": _iso_seconds(run_at),
        "next_fire_at": _iso_seconds(run_at),
        "material_source_filter": "",
        "preface_mode": "none",
        "preface_text": "",
        "preface_random_emojis": False,
        "ai_preface_goal": "",
        "ai_preface_intensity": "",
        "ai_preface_extra_instruction": "",
        "ai_preface_failure_mode": "send_without_preface",
        "batch_size_mode": "fixed",
        "batch_size_fixed": 1,
        "batch_size_min": 1,
        "batch_size_max": 1,
        "batch_material_strategy": "per_batch",
        "fixed_material_id": "",
        "cooldown_hours": 0,
        "target_selector": {
            "mode": "include",
            "base": "manual",
            "include_tags": [],
            "exclude_tags": [],
        },
        "last_error": "",
    }


@dataclass(frozen=True)
class ConflictTestPhase:
    key: str
    label: str
    delay_seconds: int
    include_contact_refresh: bool = False


def build_default_phases(config: dict[str, Any]) -> list[ConflictTestPhase]:
    return [
        ConflictTestPhase(
            key="tasks_only",
            label="任务互测",
            delay_seconds=int(config.get("task_only_delay_seconds", 6) or 6),
            include_contact_refresh=False,
        ),
        ConflictTestPhase(
            key="with_contact_refresh",
            label="任务+通讯录维护",
            delay_seconds=int(config.get("maintenance_overlap_delay_seconds", 4) or 4),
            include_contact_refresh=True,
        ),
    ]


class PanelClient:
    def __init__(self, *, base_url: str, username: str, password: str):
        self.base_url = base_url.rstrip("/")
        self.username = username
        self.password = password
        self.session = requests.Session()
        self.session.headers.update({"User-Agent": "WXBot-Pro-UI-Conflict-Test"})

    def _url(self, path: str) -> str:
        return f"{self.base_url}{path}"

    def login(self) -> None:
        response = self.session.post(
            self._url("/"),
            data={"username": self.username, "password": self.password},
            allow_redirects=True,
            timeout=15,
        )
        response.raise_for_status()
        auth = self.session.get(self._url("/api/check_auth"), timeout=15)
        auth.raise_for_status()
        payload = auth.json()
        if not payload.get("authenticated"):
            raise RuntimeError("面板登录失败，请检查账号密码或本地服务状态")

    def get_json(self, path: str, *, params: dict[str, Any] | None = None) -> dict[str, Any]:
        response = self.session.get(self._url(path), params=params, timeout=30)
        response.raise_for_status()
        payload = response.json()
        if isinstance(payload, dict) and payload.get("status") == "error":
            raise RuntimeError(payload.get("message") or f"请求失败: {path}")
        return payload

    def post_json(self, path: str, payload: dict[str, Any]) -> dict[str, Any]:
        response = self.session.post(self._url(path), json=payload, timeout=60)
        response.raise_for_status()
        data = response.json()
        if isinstance(data, dict) and data.get("status") == "error":
            raise RuntimeError(data.get("message") or f"请求失败: {path}")
        return data

    def ensure_bot_running(self) -> dict[str, Any]:
        snapshot = self.post_json("/start_bot", {})
        if _clean_text(snapshot.get("status")) == "success" and "已在运行" in _clean_text(snapshot.get("message")):
            return snapshot
        deadline = time.time() + 120
        while time.time() < deadline:
            status = self.get_json("/get_startup_status")
            if status.get("status") in {"success", "error"}:
                return status
            time.sleep(2)
        raise RuntimeError("等待机器人启动超时")

    def get_task_payload(self, module: str, *, wx_id: str = "") -> dict[str, Any]:
        params = {"wx_id": wx_id} if wx_id else None
        return self.get_json(f"/api/task-workbench/{module}", params=params)

    def get_task_runtime(self, module: str, *, wx_id: str = "") -> dict[str, Any]:
        params = {"wx_id": wx_id} if wx_id else None
        return self.get_json(f"/api/task-workbench/{module}/runtime", params=params)

    def save_task_lists(
        self,
        *,
        wx_id: str,
        scheduled_tasks: list[dict[str, Any]],
        material_tasks: list[dict[str, Any]],
    ) -> dict[str, Any]:
        payload = {
            "task_scope_wx_id": wx_id,
            "scheduled_message_task_list": scheduled_tasks,
            "material_outreach_list": material_tasks,
        }
        return self.post_json("/save_config", payload)

    def start_contact_refresh(self, *, mode: str, start_name: str) -> dict[str, Any]:
        payload = {"mode": mode, "start_name": start_name}
        return self.post_json("/contact_profiles/refresh_batch", payload)

    def get_logs_after(self, after_id: int) -> dict[str, Any]:
        return self.get_json("/get_logs", params={"after_id": after_id})


def _task_scope_wx_id(client: PanelClient) -> str:
    payload = client.get_task_payload("scheduled_message")
    return _clean_text(((payload.get("meta") or {}) if isinstance(payload.get("meta"), dict) else {}).get("wx_id"))


def _load_task_backups(client: PanelClient, wx_id: str) -> dict[str, list[dict[str, Any]]]:
    backups: dict[str, list[dict[str, Any]]] = {}
    for module in ("scheduled_message", "material_outreach"):
        payload = client.get_task_payload(module, wx_id=wx_id)
        tasks = payload.get("tasks") if isinstance(payload.get("tasks"), list) else []
        backups[module] = tasks
    return backups


def _save_backup_report(backup: dict[str, Any]) -> Path:
    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    path = REPORT_DIR / f"backup_{_recent_label()}.json"
    path.write_text(json.dumps(backup, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def _restore_backups(client: PanelClient, wx_id: str, backup: dict[str, Any]) -> dict[str, Any]:
    return client.save_task_lists(
        wx_id=wx_id,
        scheduled_tasks=list(backup.get("scheduled_message") or []),
        material_tasks=list(backup.get("material_outreach") or []),
    )


def _latest_log_id(client: PanelClient) -> int:
    payload = client.get_logs_after(0)
    logs = payload.get("logs") if isinstance(payload.get("logs"), list) else []
    return max((int(item.get("id") or 0) for item in logs if isinstance(item, dict)), default=0)


def _fetch_new_logs(client: PanelClient, after_id: int) -> tuple[list[dict[str, Any]], int]:
    payload = client.get_logs_after(after_id)
    logs = [item for item in (payload.get("logs") or []) if isinstance(item, dict)]
    new_after_id = after_id
    for item in logs:
        try:
            new_after_id = max(new_after_id, int(item.get("id") or 0))
        except (TypeError, ValueError):
            continue
    return logs, new_after_id


def _material_runtime_ready(client: PanelClient, wx_id: str) -> bool:
    payload = client.get_task_payload("material_outreach", wx_id=wx_id)
    materials = payload.get("materials") if isinstance(payload.get("materials"), list) else []
    return any(bool(item.get("runtime_available")) for item in materials if isinstance(item, dict))


def _execution_by_task_id(runtime_payload: dict[str, Any], task_id: str) -> dict[str, Any] | None:
    executions = runtime_payload.get("executions")
    if not isinstance(executions, list):
        executions = ((runtime_payload.get("runtime") or {}) if isinstance(runtime_payload.get("runtime"), dict) else {}).get("executions")
    if not isinstance(executions, list):
        return None
    for item in executions:
        if not isinstance(item, dict):
            continue
        if _clean_text(item.get("task_id")) == task_id:
            return item
    return None


def _phase_done(client: PanelClient, wx_id: str, task_ids: dict[str, str]) -> tuple[bool, dict[str, Any]]:
    module_runtime = {
        "scheduled_message": client.get_task_runtime("scheduled_message", wx_id=wx_id),
        "material_outreach": client.get_task_runtime("material_outreach", wx_id=wx_id),
    }
    executions: dict[str, Any] = {}
    for module, task_id in task_ids.items():
        found = _execution_by_task_id(module_runtime[module], task_id)
        if not found:
            return False, {"module_runtime": module_runtime}
        executions[module] = found
    return True, {"module_runtime": module_runtime, "executions": executions}


def _start_contact_refresh_thread(
    client: PanelClient,
    *,
    mode: str,
    start_name: str,
) -> tuple[threading.Thread, dict[str, Any]]:
    result: dict[str, Any] = {}

    def runner() -> None:
        try:
            result["response"] = client.start_contact_refresh(mode=mode, start_name=start_name)
        except Exception as exc:  # pragma: no cover - best effort reporting
            result["error"] = str(exc)

    thread = threading.Thread(target=runner, daemon=True)
    thread.start()
    return thread, result


def _interesting_logs(logs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    keywords = (
        "通讯录维护",
        "定时消息",
        "素材转发",
        "Find Control Timeout",
        "失败",
        "WARNING",
        "ERROR",
    )
    picked = []
    for item in logs:
        message = _clean_text(item.get("message"))
        if any(keyword in message for keyword in keywords):
            picked.append(item)
    return picked


def run_phase(
    client: PanelClient,
    *,
    wx_id: str,
    config: dict[str, Any],
    phase: ConflictTestPhase,
) -> dict[str, Any]:
    scheduled_task = build_scheduled_message_task(
        contacts=list(config.get("contacts") or []),
        text=_clean_text(config.get("scheduled_message_text")) or DEFAULT_CONFIG["scheduled_message_text"],
        delay_seconds=phase.delay_seconds,
        target_count=int(config.get("scheduled_targets", 3) or 3),
        phase_label=phase.label,
    )
    material_task = build_material_outreach_task(
        contacts=list(config.get("contacts") or []),
        delay_seconds=phase.delay_seconds,
        target_count=int(config.get("material_targets", 3) or 3),
        phase_label=phase.label,
    )
    client.save_task_lists(
        wx_id=wx_id,
        scheduled_tasks=[scheduled_task],
        material_tasks=[material_task],
    )

    log_after_id = _latest_log_id(client)
    task_ids = {
        "scheduled_message": _clean_text(scheduled_task.get("id")),
        "material_outreach": _clean_text(material_task.get("id")),
    }
    refresh_thread = None
    refresh_result: dict[str, Any] = {}
    if phase.include_contact_refresh:
        refresh_thread, refresh_result = _start_contact_refresh_thread(
            client,
            mode=_clean_text(config.get("contact_refresh_mode")) or "test",
            start_name=_clean_text(config.get("contact_refresh_start_name")),
        )

    logs: list[dict[str, Any]] = []
    poll_interval = max(1, int(config.get("poll_interval_seconds", 2) or 2))
    deadline = time.time() + max(60, int(config.get("task_timeout_seconds", 240) or 240))
    completion_info: dict[str, Any] = {}
    while time.time() < deadline:
        new_logs, log_after_id = _fetch_new_logs(client, log_after_id)
        logs.extend(new_logs)
        done, completion_info = _phase_done(client, wx_id, task_ids)
        if done:
            if refresh_thread is not None:
                refresh_thread.join(timeout=5)
            break
        time.sleep(poll_interval)
    else:
        if refresh_thread is not None:
            refresh_thread.join(timeout=5)
        raise RuntimeError(f"{phase.label} 超时，未能等到所有任务完成")

    return {
        "phase": phase.label,
        "task_ids": task_ids,
        "executions": completion_info.get("executions", {}),
        "refresh": refresh_result,
        "logs": _interesting_logs(logs),
    }


def load_runtime_config(path: str | None) -> dict[str, Any]:
    config = dict(DEFAULT_CONFIG)
    chosen = Path(path) if path else CONFIG_PATH
    if chosen.exists():
        payload = json.loads(chosen.read_text(encoding="utf-8"))
        if isinstance(payload, dict):
            config.update(payload)
    return config


def build_report(
    *,
    wx_id: str,
    phases: list[dict[str, Any]],
    backup_path: Path,
    restore_result: dict[str, Any] | None,
) -> dict[str, Any]:
    return {
        "generated_at": datetime.now().replace(microsecond=0).isoformat(),
        "wx_id": wx_id,
        "backup_path": str(backup_path),
        "restore_result": restore_result or {},
        "phases": phases,
    }


def print_report(report: dict[str, Any]) -> None:
    print(f"测试微信号：{_clean_text(report.get('wx_id')) or '未知'}")
    print(f"备份文件：{_clean_text(report.get('backup_path'))}")
    for phase in report.get("phases") or []:
        print(f"\n== {phase.get('phase')} ==")
        executions = phase.get("executions") if isinstance(phase.get("executions"), dict) else {}
        for module, execution in executions.items():
            if not isinstance(execution, dict):
                continue
            result = _clean_text(execution.get("result")) or "unknown"
            summary = _clean_text(execution.get("result_summary")) or _clean_text(execution.get("result_message"))
            print(f"{module}: {result} | {summary}")
        refresh = phase.get("refresh") if isinstance(phase.get("refresh"), dict) else {}
        if refresh:
            if refresh.get("error"):
                print(f"contact_refresh: error | {_clean_text(refresh.get('error'))}")
            elif isinstance(refresh.get("response"), dict):
                response = refresh["response"]
                print(f"contact_refresh: {_clean_text(response.get('status'))} | {_clean_text(response.get('message'))}")
        logs = phase.get("logs") if isinstance(phase.get("logs"), list) else []
        for item in logs[:12]:
            level = _clean_text(item.get("level")) or "INFO"
            message = _clean_text(item.get("message"))
            print(f"[{level}] {message}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="WXBot Pro 真实微信 UI 冲突测试脚本")
    parser.add_argument("--config", help="可选配置文件路径，默认读取 tools/ui_conflict_test_config.json")
    args = parser.parse_args(argv)

    config = load_runtime_config(args.config)
    client = PanelClient(
        base_url=_clean_text(config.get("base_url")) or DEFAULT_CONFIG["base_url"],
        username=_clean_text(config.get("username")) or DEFAULT_CONFIG["username"],
        password=str(config.get("password") or DEFAULT_CONFIG["password"]),
    )

    restore_result = None
    backup_path = REPORT_DIR / "backup_missing.json"
    report_path: Path | None = None
    try:
        client.login()
        startup = client.ensure_bot_running()
        if _clean_text(startup.get("status")) == "error":
            raise RuntimeError(_clean_text(startup.get("message")) or "机器人启动失败")
        wx_id = _task_scope_wx_id(client)
        if not _material_runtime_ready(client, wx_id):
            raise RuntimeError("当前素材池没有可发送素材，请先让素材源投喂至少 1 条素材后再运行测试")
        backups = _load_task_backups(client, wx_id)
        backup_path = _save_backup_report(backups)

        phases = []
        for phase in build_default_phases(config):
            print(f"\n开始执行：{phase.label}")
            phases.append(run_phase(client, wx_id=wx_id, config=config, phase=phase))

        restore_result = _restore_backups(client, wx_id, backups)
        report = build_report(
            wx_id=wx_id,
            phases=phases,
            backup_path=backup_path,
            restore_result=restore_result,
        )
        REPORT_DIR.mkdir(parents=True, exist_ok=True)
        report_path = REPORT_DIR / f"report_{_recent_label()}.json"
        report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        print("\n测试完成，已恢复原任务配置。")
        print_report(report)
        print(f"\n报告文件：{report_path}")
        return 0
    except Exception as exc:
        print(f"\n测试失败：{exc}", file=sys.stderr)
        if report_path:
            print(f"已有部分报告：{report_path}", file=sys.stderr)
        return 1
    finally:
        if restore_result is None:
            try:
                client.login()
                wx_id = _task_scope_wx_id(client)
                if backup_path.exists():
                    backups = json.loads(backup_path.read_text(encoding="utf-8"))
                    if isinstance(backups, dict):
                        _restore_backups(client, wx_id, backups)
            except Exception:
                pass


if __name__ == "__main__":
    raise SystemExit(main())
