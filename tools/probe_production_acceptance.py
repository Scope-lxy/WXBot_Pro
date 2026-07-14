"""Read-only observer for the final WXBot Pro production acceptance run."""

from __future__ import annotations

import argparse
import json
import os
import re
import time
import urllib.request
from urllib.parse import urljoin
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any

import psutil


EVENT_PATTERNS = {
    "private_inbound": ("运行事件：入站消息 scope=private ",),
    "group_inbound": ("运行事件：入站消息 scope=group ",),
    "image_inbound": (
        "运行事件：入站消息 scope=private type=image",
        "运行事件：入站消息 scope=group type=image",
    ),
    "voice_inbound": (
        "运行事件：入站消息 scope=private type=voice",
        "运行事件：入站消息 scope=group type=voice",
    ),
    "voice_outbound": (),
    "contact_batch": ("运行事件：通讯录批次完成",),
    "text_reply": (),
    "exclusive_queue": ("运行事件：通讯录期间独占聊天任务已排队",),
    "material_outreach": (),
    "relationship_scan": (),
    "manual_intervention": ("运行事件：人工介入已确认",),
}

FAILURE_PATTERNS = {
    "error_log": ("[ERROR]",),
    "traceback": ("Traceback (most recent call last)",),
    "ui_stuck": ("UI owner 卡死", "微信 UI 卡死", "专用卡死退出码"),
    "json_error": ("JSONDecodeError", "JSON 解析失败"),
    "wrong_window": ("错误窗口", "目标窗口不一致"),
    "duplicate_send": ("重复发送", "重复回复"),
}

DEFAULT_REQUIRED_EVENTS = tuple(EVENT_PATTERNS)
MIN_ACCEPTANCE_SECONDS = 7200.0
ACTIVITY_BUCKET_SECONDS = 900.0
MIN_ACTIVITY_BUCKETS = 6
MAX_FORMAL_SAMPLE_SECONDS = 5.0
MAX_FORMAL_SAMPLE_GAP_SECONDS = 10.0
ACTIVITY_EVENTS = {"private_inbound", "group_inbound", "image_inbound", "voice_inbound"}
RUNTIME_SCOPED_LOG_EVENTS = {
    "private_inbound", "group_inbound", "image_inbound", "voice_inbound",
    "contact_batch", "exclusive_queue", "manual_intervention",
}
RUNTIME_ID_RE = re.compile(r"\bruntime_id=([0-9a-f]{32})\b")
CRITICAL_JSON_PARTS = {
    "contact_profiles",
    "relationship_scan",
    "tasks",
    "ui_delivery",
    "unanswered_inbound",
    "friend_request",
    "chat_memory",
    "memory",
}


def _iso_now() -> str:
    return datetime.now().replace(microsecond=0).isoformat()


def _timestamp_at_or_after(value: Any, started_at: str) -> bool:
    text = str(value or "").strip()
    if not text:
        return False
    try:
        candidate = datetime.fromisoformat(text.replace("Z", "+00:00")).replace(tzinfo=None)
        started = datetime.fromisoformat(started_at.replace("Z", "+00:00")).replace(tzinfo=None)
    except ValueError:
        return False
    return candidate >= started


def state_event_markers(relative_path: str, payload: Any, started_at: str) -> set[str]:
    normalized = relative_path.replace("\\", "/").lower()
    events: set[str] = set()
    if normalized.endswith("data/config/voice_reply_state.json") and isinstance(payload, dict):
        limits = payload.get("limits") or {}
        if any(
            isinstance(item, dict) and _timestamp_at_or_after(item.get("last_sent_at"), started_at)
            for item in limits.values()
        ):
            events.add("voice_outbound")
    if "/ui_delivery/" in normalized and isinstance(payload, list):
        if any(
            isinstance(item, dict)
            and str(item.get("kind") or "") == "send_audio"
            and str(item.get("status") or "") == "done"
            and _timestamp_at_or_after(item.get("finished_at"), started_at)
            for item in payload
        ):
            events.add("voice_outbound")
    if "/relationship_scan/" in normalized and isinstance(payload, dict):
        runtime = payload.get("runtime") or {}
        if (
            str(runtime.get("last_scan_mode") or "") == "full"
            and _timestamp_at_or_after(runtime.get("last_scan_at"), started_at)
        ):
            events.add("relationship_scan")
    if "/tasks/material_outreach/history.json" in normalized and isinstance(payload, dict):
        if any(
            isinstance(item, dict)
            and bool(item.get("success"))
            and _timestamp_at_or_after(item.get("sent_at"), started_at)
            for item in (payload.get("send_records") or [])
        ):
            events.add("material_outreach")
    return events


def classify_log_line(line: str) -> tuple[set[str], set[str]]:
    events = {
        name
        for name, patterns in EVENT_PATTERNS.items()
        if any(pattern in line for pattern in patterns)
    }
    failures = {
        name
        for name, patterns in FAILURE_PATTERNS.items()
        if any(pattern in line for pattern in patterns)
    }
    return events, failures


def log_line_is_current(line: str, started_at: str) -> bool:
    if not line.startswith("[") or "]" not in line:
        return False
    stamp = line[1:line.index("]")]
    try:
        started = datetime.fromisoformat(started_at)
        candidate = datetime.strptime(f"{started.year}-{stamp}", "%Y-%m-%d %H:%M:%S")
        if candidate < started and (started - candidate).days > 180:
            candidate = candidate.replace(year=started.year + 1)
    except ValueError:
        return False
    return candidate >= started.replace(microsecond=0)


def state_category(path: Path) -> str:
    parts = {part.lower() for part in path.parts}
    for category in (
        "contact_profiles", "relationship_scan", "ui_delivery", "unanswered_inbound",
        "friend_request", "chat_memory", "memory", "tasks", "config",
    ):
        if category in parts:
            return category
    return "other"


def state_schema_error(relative_path: str, payload: Any) -> str:
    normalized = relative_path.replace("\\", "/").lower()
    if normalized.endswith("contact_profiles/contacts.json"):
        if not isinstance(payload, dict) or not isinstance(payload.get("subjects"), list):
            return "contact_profiles_schema"
    elif "/relationship_scan/" in normalized:
        if not isinstance(payload, dict) or not isinstance(payload.get("records"), list):
            return "relationship_scan_schema"
    elif "/ui_delivery/" in normalized:
        if not isinstance(payload, list):
            return "ui_delivery_schema"
    elif "/unanswered_inbound/" in normalized:
        if not isinstance(payload, list):
            return "unanswered_inbound_schema"
    elif "/chat_memory/" in normalized:
        if not isinstance(payload, dict) or not isinstance(payload.get("memories"), list):
            return "chat_memory_schema"
    elif "/memory/" in normalized and normalized.endswith("_memory.json"):
        if not isinstance(payload, list):
            return "memory_schema"
    elif "/memory/" in normalized and normalized.endswith("/name.json"):
        if not isinstance(payload, dict) or "name" not in payload:
            return "memory_name_schema"
    elif normalized.endswith("data/config/voice_reply_state.json"):
        if not isinstance(payload, dict) or not isinstance(payload.get("limits"), dict):
            return "voice_reply_state_schema"
    elif "/tasks/" in normalized:
        if normalized.endswith(("/tasks.json", "/materials.json")):
            if not isinstance(payload, list):
                return "tasks_list_schema"
        elif normalized.endswith("/keyword_reply/rules.json"):
            if not isinstance(payload, dict):
                return "keyword_reply_rules_schema"
        elif normalized.endswith(("/history.json", "/runtime.json")) and not isinstance(payload, dict):
            return "task_runtime_schema"
    elif "/friend_request/" in normalized and normalized.endswith("state.json"):
        if not isinstance(payload, dict) or not isinstance(payload.get("candidates"), list):
            return "friend_request_schema"
    elif normalized.endswith("data/config/runtime_metrics_v1.json"):
        if not isinstance(payload, dict) or not isinstance(payload.get("hours"), dict):
            return "runtime_metrics_schema"
    return ""


def metric_totals(path: Path) -> dict[str, int]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return {}
    totals: Counter[str] = Counter()
    for bucket in (payload.get("hours") or {}).values():
        if not isinstance(bucket, dict):
            continue
        for key, value in bucket.items():
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                totals[key] += int(value)
    return dict(totals)


def metric_delta(before: dict[str, int], after: dict[str, int]) -> dict[str, int]:
    return {
        key: int(after.get(key, 0)) - int(before.get(key, 0))
        for key in sorted(set(before) | set(after))
        if int(after.get(key, 0)) - int(before.get(key, 0))
    }


def automatic_acceptance_status(
    *,
    duration_seconds: float,
    required_events: tuple[str, ...],
    event_counts: dict[str, int],
    failure_counts: dict[str, int],
    panel_failures: int,
    bot_unhealthy_samples: int,
    json_errors: list[str],
    process_failures: int,
    max_panel_processes: int,
    max_collectors: int,
    final_collectors: int,
    max_test_processes: int,
    runtime_metric_delta: dict[str, int],
    interrupted: bool,
    smoke_run: bool,
    panel_zero_samples: int,
    panel_pid_count: int,
    json_suspicious_changes: list[str],
    max_panel_sample_gap: float,
    activity_buckets: list[int],
    max_web_server_families: int,
    runtime_id_count: int,
) -> dict[str, Any]:
    missing_events = [name for name in required_events if int(event_counts.get(name, 0)) <= 0]
    reasons = []
    if duration_seconds < MIN_ACCEPTANCE_SECONDS:
        reasons.append("duration_too_short")
    if interrupted:
        reasons.append("interrupted")
    if smoke_run:
        reasons.append("smoke_run")
    if missing_events:
        reasons.append("required_events_missing")
    if any(failure_counts.values()):
        reasons.append("failure_log_detected")
    if panel_failures:
        reasons.append("panel_unhealthy")
    if bot_unhealthy_samples:
        reasons.append("bot_not_continuously_running")
    if panel_zero_samples:
        reasons.append("panel_process_missing")
    if json_errors:
        reasons.append("json_validation_failed")
    if json_suspicious_changes:
        reasons.append("json_state_suspicious_change")
    if process_failures:
        reasons.append("process_snapshot_failed")
    if max_panel_processes > 1:
        reasons.append("multiple_panel_processes")
    if max_web_server_families > 1:
        reasons.append("extra_web_server_family")
    if panel_pid_count > 1:
        reasons.append("panel_pid_changed")
    if max_collectors > 1:
        reasons.append("multiple_collectors")
    if final_collectors:
        reasons.append("collector_left_running")
    if max_test_processes:
        reasons.append("test_process_detected")
    buckets = sorted(set(int(item) for item in activity_buckets))
    expected_last_bucket = max(0, int(max(0.0, duration_seconds - 0.001) // ACTIVITY_BUCKET_SECONDS))
    activity_is_sustained = (
        len(buckets) >= MIN_ACTIVITY_BUCKETS
        and bool(buckets)
        and buckets[0] == 0
        and buckets[-1] >= expected_last_bucket
        and all((right - left) <= 2 for left, right in zip(buckets, buckets[1:]))
    )
    if not activity_is_sustained:
        reasons.append("activity_not_sustained")
    if max_panel_sample_gap > MAX_FORMAL_SAMPLE_GAP_SECONDS:
        reasons.append("panel_sample_gap_too_large")
    if runtime_id_count != 1:
        reasons.append("runtime_instance_not_stable")
    for metric in ("received_messages", "reply_count"):
        if int(runtime_metric_delta.get(metric, 0)) <= 0:
            reasons.append(f"metric_{metric}_missing")
    return {
        "automatic_checks_passed": not reasons,
        "status": "manual_review_required" if not reasons else "incomplete",
        "reasons": reasons,
        "missing_events": missing_events,
        "manual_review_required": [
            "duplicate_or_missing_real_reply",
            "wrong_conversation_window",
            "contact_batch_real_inbound_sequence",
        ],
    }


class AcceptanceObserver:
    def __init__(self, args: argparse.Namespace):
        self.args = args
        self.root = Path(args.root).resolve()
        self.started_wall = time.time()
        self.started_monotonic = time.monotonic()
        self.started_at = _iso_now()
        self.log_offsets: dict[Path, int] = {}
        self.event_counts: Counter[str] = Counter()
        self.failure_counts: Counter[str] = Counter()
        self.event_evidence: dict[str, list[dict[str, str]]] = {}
        self.activity_buckets: set[int] = set()
        self.panel_samples = 0
        self.panel_failures = 0
        self.bot_unhealthy_samples = 0
        self.runtime_ids: set[str] = set()
        self.last_panel_sample_at: float | None = None
        self.max_panel_sample_gap = 0.0
        self.process_samples = 0
        self.process_failures = 0
        self.max_panel_processes = 0
        self.panel_zero_samples = 0
        self.panel_listener_pids: set[int] = set()
        self.max_web_server_processes = 0
        self.max_web_server_families = 0
        self.max_collectors = 0
        self.final_collectors = 0
        self.max_test_processes = 0
        self.json_errors: set[str] = set()
        self.json_suspicious_changes: set[str] = set()
        self.state_evidence_seen: set[tuple[str, str]] = set()
        self.metrics_path = self.root / "data" / "config" / "runtime_metrics_v1.json"
        self.metrics_before = metric_totals(self.metrics_path)
        self.report_path = self._report_path()
        self._initialize_log_offsets()
        self.baseline_json_sizes = self._capture_json_baseline()
        self.started_wall = time.time()
        self.started_monotonic = time.monotonic()
        self.started_at = _iso_now()
        self.metrics_before = metric_totals(self.metrics_path)
        self.log_offsets.clear()
        self._initialize_log_offsets()

    def _report_path(self) -> Path:
        if self.args.output:
            return Path(self.args.output).resolve()
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        return self.root / "backups" / "production_acceptance" / stamp / "report.json"

    def _initialize_log_offsets(self) -> None:
        for path in (self.root / "wxbot_logs").glob("log_*.txt"):
            try:
                self.log_offsets[path] = path.stat().st_size
            except OSError:
                continue

    def _read_new_logs(self) -> None:
        log_dir = self.root / "wxbot_logs"
        for path in log_dir.glob("log_*.txt"):
            if path not in self.log_offsets:
                try:
                    self.log_offsets[path] = path.stat().st_size if path.stat().st_mtime < self.started_wall else 0
                except OSError:
                    continue
            offset = self.log_offsets[path]
            try:
                size = path.stat().st_size
                if size < offset:
                    self.failure_counts["log_truncated"] += 1
                    self.log_offsets[path] = size
                    continue
                with path.open("r", encoding="utf-8", errors="replace") as handle:
                    handle.seek(offset)
                    lines = []
                    while True:
                        line_offset = handle.tell()
                        line = handle.readline()
                        if not line:
                            self.log_offsets[path] = handle.tell()
                            break
                        if not line.endswith("\n"):
                            self.log_offsets[path] = line_offset
                            break
                        lines.append((line_offset, line))
            except OSError:
                continue
            for line_offset, line in lines:
                if not log_line_is_current(line, self.started_at):
                    continue
                events, failures = classify_log_line(line)
                runtime_match = RUNTIME_ID_RE.search(line)
                line_runtime_id = runtime_match.group(1) if runtime_match else ""
                timestamp = line.split("]", 1)[0].lstrip("[") if "]" in line else ""
                for event in events:
                    if event in RUNTIME_SCOPED_LOG_EVENTS and line_runtime_id not in self.runtime_ids:
                        continue
                    self._record_event(event, {
                        "timestamp": timestamp,
                        "log_file": path.name,
                        "byte_offset": str(line_offset),
                    })
                for failure in failures:
                    self.failure_counts[failure] += 1

    def _record_event(self, event: str, evidence_item: dict[str, str]) -> None:
        self.event_counts[event] += 1
        evidence = self.event_evidence.setdefault(event, [])
        if len(evidence) < 10:
            evidence.append(evidence_item)
        if event in ACTIVITY_EVENTS:
            elapsed = max(0.0, time.monotonic() - self.started_monotonic)
            self.activity_buckets.add(int(elapsed // ACTIVITY_BUCKET_SECONDS))

    def _sample_panel(self) -> None:
        sampled_at = time.monotonic()
        if self.last_panel_sample_at is not None:
            self.max_panel_sample_gap = max(
                self.max_panel_sample_gap,
                sampled_at - self.last_panel_sample_at,
            )
        self.last_panel_sample_at = sampled_at
        self.panel_samples += 1
        try:
            with urllib.request.urlopen(self.args.panel_url, timeout=3) as response:
                if int(response.status) != 200:
                    self.panel_failures += 1
            health_url = urljoin(self.args.panel_url.rstrip('/') + '/', 'runtime_health')
            with urllib.request.urlopen(health_url, timeout=3) as response:
                payload = json.loads(response.read().decode('utf-8'))
                runtime_id = str(payload.get('runtime_id') or '').strip().lower()
                runtime_id_valid = bool(RUNTIME_ID_RE.fullmatch(f"runtime_id={runtime_id}"))
                if int(response.status) != 200 or not bool(payload.get('bot_running')) or not runtime_id_valid:
                    self.bot_unhealthy_samples += 1
                else:
                    self.runtime_ids.add(runtime_id)
        except Exception:
            self.panel_failures += 1
            self.bot_unhealthy_samples += 1

    def _sample_processes(self) -> None:
        try:
            workspace = str(self.root).casefold()
            process_rows = []
            for process in psutil.process_iter(["pid", "name", "cmdline", "exe", "cwd"]):
                try:
                    info = process.info
                    command = " ".join(info.get("cmdline") or [])
                    location = " ".join((str(info.get("exe") or ""), str(info.get("cwd") or ""), command)).casefold()
                    if workspace in location:
                        process_rows.append((
                            int(info["pid"]),
                            int(process.ppid()),
                            str(info.get("name") or ""),
                            command,
                        ))
                except (psutil.Error, OSError):
                    continue
            listening_pids = {
                int(connection.pid)
                for connection in psutil.net_connections(kind="tcp")
                if connection.pid
                and connection.status == psutil.CONN_LISTEN
                and connection.laddr
                and 10001 <= int(connection.laddr.port) <= 10999
            }
            panels = sum(
                pid in listening_pids and "web_server.py" in command
                for pid, _ppid, _name, command in process_rows
            )
            panel_pids = {
                pid for pid, _ppid, _name, command in process_rows
                if pid in listening_pids and "web_server.py" in command
            }
            web_server_rows = [row for row in process_rows if "web_server.py" in row[3]]
            web_servers = len(web_server_rows)
            web_server_pids = {pid for pid, _ppid, _name, _command in web_server_rows}
            web_server_families = sum(
                ppid not in web_server_pids
                for _pid, ppid, _name, _command in web_server_rows
            )
            collectors = sum(
                name.casefold() in {"python.exe", "pythonw.exe"}
                and "contact_auto_collector_worker.py" in command
                for _pid, _ppid, name, command in process_rows
            )
            tests = sum(
                name.casefold() in {"python.exe", "pythonw.exe"}
                and ("unittest" in command or "pytest" in command)
                for _pid, _ppid, name, command in process_rows
            )
        except (psutil.Error, OSError, ValueError):
            self.process_failures += 1
            panels = collectors = tests = web_servers = web_server_families = 0
            panel_pids = set()
        self.process_samples += 1
        self.max_panel_processes = max(self.max_panel_processes, panels)
        if panels == 0:
            self.panel_zero_samples += 1
        self.panel_listener_pids.update(panel_pids)
        self.max_web_server_processes = max(self.max_web_server_processes, web_servers)
        self.max_web_server_families = max(self.max_web_server_families, web_server_families)
        self.max_collectors = max(self.max_collectors, collectors)
        self.final_collectors = collectors
        self.max_test_processes = max(self.max_test_processes, tests)

    def _is_monitored_json(self, path: Path) -> bool:
        if path.suffix.lower() != ".json":
            return False
        if path.parent == self.root / "data" / "config":
            return True
        return bool(set(path.parts) & CRITICAL_JSON_PARTS)

    def _iter_monitored_json(self):
        data_dir = self.root / "data"
        for dirpath, _dirnames, filenames in os.walk(data_dir):
            for filename in filenames:
                path = Path(dirpath) / filename
                if self._is_monitored_json(path):
                    yield path

    def _read_and_validate_json(self, path: Path, *, collect_events: bool) -> None:
        category = state_category(path)
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            relative = str(path.relative_to(self.root))
            schema_error = state_schema_error(relative, payload)
            if schema_error:
                self.json_errors.add(schema_error)
            if collect_events:
                for event in state_event_markers(relative, payload, self.started_at):
                    evidence_key = (event, category)
                    if evidence_key in self.state_evidence_seen:
                        continue
                    self.state_evidence_seen.add(evidence_key)
                    self._record_event(event, {
                        "timestamp": _iso_now(),
                        "state_source": f"{event}_state",
                    })
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            self.json_errors.add(f"{category}:{exc.__class__.__name__}")

    def _capture_json_baseline(self) -> dict[Path, int]:
        baseline = {}
        for path in self._iter_monitored_json():
            try:
                baseline[path] = path.stat().st_size
            except OSError:
                continue
            self._read_and_validate_json(path, collect_events=False)
        return baseline

    def _validate_changed_json(self) -> None:
        current_paths = set(self._iter_monitored_json())
        for missing in set(self.baseline_json_sizes) - current_paths:
            self.json_errors.add(f"{state_category(missing)}:deleted")
        for path in current_paths:
            try:
                stat = path.stat()
            except OSError as exc:
                self.json_errors.add(f"{state_category(path)}:{exc.__class__.__name__}")
                continue
            old_size = self.baseline_json_sizes.get(path)
            if old_size and old_size >= 1024 and stat.st_size <= max(16, old_size // 10):
                self.json_suspicious_changes.add(f"{state_category(path)}:size_collapse")
            if path in self.baseline_json_sizes and stat.st_mtime < self.started_wall:
                continue
            self._read_and_validate_json(path, collect_events=True)

    def _build_report(self, *, interrupted: bool = False) -> dict[str, Any]:
        duration = max(0.0, time.monotonic() - self.started_monotonic)
        metrics_after = metric_totals(self.metrics_path)
        metrics_delta = metric_delta(self.metrics_before, metrics_after)
        if int(metrics_delta.get("reply_count", 0)) > 0:
            self.event_counts["text_reply"] = max(1, self.event_counts.get("text_reply", 0))
            self.event_evidence.setdefault("text_reply", [{
                "timestamp": _iso_now(),
                "state_source": "runtime_metrics",
            }])
        required = DEFAULT_REQUIRED_EVENTS
        automatic = automatic_acceptance_status(
            duration_seconds=duration,
            required_events=required,
            event_counts=dict(self.event_counts),
            failure_counts=dict(self.failure_counts),
            panel_failures=self.panel_failures,
            bot_unhealthy_samples=self.bot_unhealthy_samples,
            json_errors=sorted(self.json_errors),
            process_failures=self.process_failures,
            max_panel_processes=self.max_panel_processes,
            max_collectors=self.max_collectors,
            final_collectors=self.final_collectors,
            max_test_processes=self.max_test_processes,
            runtime_metric_delta=metrics_delta,
            interrupted=interrupted,
            smoke_run=bool(self.args.smoke),
            panel_zero_samples=self.panel_zero_samples,
            panel_pid_count=len(self.panel_listener_pids),
            json_suspicious_changes=sorted(self.json_suspicious_changes),
            max_panel_sample_gap=self.max_panel_sample_gap,
            activity_buckets=sorted(self.activity_buckets),
            max_web_server_families=self.max_web_server_families,
            runtime_id_count=len(self.runtime_ids),
        )
        return {
            "schema_version": 1,
            "started_at": self.started_at,
            "finished_at": _iso_now(),
            "duration_seconds": round(duration, 3),
            "requested_duration_seconds": self.args.duration_seconds,
            "interrupted": interrupted,
            "panel": {
                "url": self.args.panel_url,
                "samples": self.panel_samples,
                "failures": self.panel_failures,
                "bot_unhealthy_samples": self.bot_unhealthy_samples,
                "max_sample_gap_seconds": round(self.max_panel_sample_gap, 3),
                "runtime_ids": sorted(self.runtime_ids),
            },
            "processes": {
                "samples": self.process_samples,
                "failures": self.process_failures,
                "max_panel_processes": self.max_panel_processes,
                "panel_zero_samples": self.panel_zero_samples,
                "panel_listener_pids": sorted(self.panel_listener_pids),
                "max_web_server_processes": self.max_web_server_processes,
                "max_web_server_families": self.max_web_server_families,
                "max_collectors": self.max_collectors,
                "final_collectors": self.final_collectors,
                "max_test_processes": self.max_test_processes,
            },
            "event_counts": dict(self.event_counts),
            "event_evidence": self.event_evidence,
            "activity_bucket_count": len(self.activity_buckets),
            "activity_buckets": sorted(self.activity_buckets),
            "failure_counts": dict(self.failure_counts),
            "json_errors": sorted(self.json_errors),
            "json_suspicious_changes": sorted(self.json_suspicious_changes),
            "runtime_metric_delta": metrics_delta,
            "assessment": automatic,
        }

    def _write_report(self, report: dict[str, Any]) -> None:
        self.report_path.parent.mkdir(parents=True, exist_ok=True)
        temp = self.report_path.with_suffix(self.report_path.suffix + ".tmp")
        temp.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        temp.replace(self.report_path)

    def run(self) -> int:
        interrupted = False
        next_json_check = 0.0
        next_process_check = 0.0
        try:
            while time.monotonic() - self.started_monotonic < self.args.duration_seconds:
                self._sample_panel()
                self._read_new_logs()
                now = time.monotonic()
                if now >= next_process_check:
                    self._sample_processes()
                    next_process_check = now + self.args.process_sample_seconds
                if now >= next_json_check:
                    self._validate_changed_json()
                    next_json_check = now + 60.0
                self._write_report(self._build_report())
                remaining = self.args.duration_seconds - (time.monotonic() - self.started_monotonic)
                if remaining > 0:
                    time.sleep(min(self.args.sample_seconds, remaining))
        except KeyboardInterrupt:
            interrupted = True
        self._sample_panel()
        self._read_new_logs()
        self._sample_processes()
        self._validate_changed_json()
        report = self._build_report(interrupted=interrupted)
        self._write_report(report)
        print(str(self.report_path))
        if interrupted:
            return 130
        return 3 if report["assessment"]["automatic_checks_passed"] else 2


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", default=str(Path(__file__).resolve().parents[1]))
    parser.add_argument("--duration-seconds", type=float, default=7200.0)
    parser.add_argument("--sample-seconds", type=float, default=2.0)
    parser.add_argument("--process-sample-seconds", type=float, default=2.0)
    parser.add_argument("--panel-url", default="http://127.0.0.1:10001/")
    parser.add_argument("--output", default="")
    parser.add_argument("--smoke", action="store_true", help="Allow a short observer self-test that can never pass acceptance.")
    args = parser.parse_args(argv)
    args.duration_seconds = max(1.0, float(args.duration_seconds))
    if args.duration_seconds < MIN_ACCEPTANCE_SECONDS and not args.smoke:
        parser.error(f"formal acceptance requires at least {int(MIN_ACCEPTANCE_SECONDS)} seconds; use --smoke for a short self-test")
    args.sample_seconds = max(0.2, float(args.sample_seconds))
    args.process_sample_seconds = max(args.sample_seconds, float(args.process_sample_seconds))
    if not args.smoke and (
        args.sample_seconds > MAX_FORMAL_SAMPLE_SECONDS
        or args.process_sample_seconds > MAX_FORMAL_SAMPLE_SECONDS
    ):
        parser.error(f"formal acceptance sample intervals must be at most {int(MAX_FORMAL_SAMPLE_SECONDS)} seconds")
    return args


if __name__ == "__main__":
    raise SystemExit(AcceptanceObserver(parse_args()).run())
