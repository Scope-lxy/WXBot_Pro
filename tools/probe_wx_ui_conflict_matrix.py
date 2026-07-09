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
from typing import Any, Callable


DEFAULT_CONTACTS = ["阿英2", "阿英3", "阿英4", "炳3", "炳4"]
DEFAULT_FFMPEG_DIR = r"C:\Users\Admin\Desktop\WXBot_Pro\venv\tools\ffmpeg"
REPORT_DIR = Path("backups") / "ui_conflict_matrix"
ASSET_DIR = REPORT_DIR / "assets"


def clean(value: Any) -> str:
    return str(value or "").strip()


def stamp() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]


def label() -> str:
    return datetime.now().strftime("%Y%m%d_%H%M%S")


def json_default(value: Any) -> str:
    return str(value)


def write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, default=json_default), encoding="utf-8")


def configure_ffmpeg_path(ffmpeg_dir: str) -> str:
    root = Path(clean(ffmpeg_dir) or DEFAULT_FFMPEG_DIR)
    bin_dir = root / "bin"
    chosen = bin_dir if (bin_dir / "ffmpeg.exe").exists() else root
    if chosen.exists():
        current = os.environ.get("PATH", "")
        chosen_text = str(chosen.resolve())
        parts = [item for item in current.split(os.pathsep) if item]
        if not any(item.lower() == chosen_text.lower() for item in parts):
            os.environ["PATH"] = chosen_text + os.pathsep + current
        return chosen_text
    return ""


def make_assets(*, voice_seconds: int) -> dict[str, str]:
    ASSET_DIR.mkdir(parents=True, exist_ok=True)
    voice_seconds = max(3, int(voice_seconds or 12))
    wav_path = ASSET_DIR / f"ui_conflict_voice_{voice_seconds}s.wav"
    txt_path = ASSET_DIR / "ui_conflict_file.txt"
    txt_path.write_text(f"WXBot UI 冲突测试文件，请忽略。\n{stamp()}\n", encoding="utf-8")
    sample_rate = 16000
    with wave.open(str(wav_path), "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(sample_rate)
        for index in range(sample_rate * voice_seconds):
            value = int(12000 * math.sin(2 * math.pi * 440 * index / sample_rate))
            wav.writeframesraw(struct.pack("<h", value))
    return {"wav": str(wav_path.resolve()), "txt": str(txt_path.resolve()), "voice_seconds": voice_seconds}


class Recorder:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.events: list[dict[str, Any]] = []

    def log(self, event: str, **data: Any) -> None:
        item = {"at": stamp(), "event": event, **data}
        with self._lock:
            self.events.append(item)
        print(json.dumps(item, ensure_ascii=False, default=json_default), flush=True)


def timed_call(name: str, recorder: Recorder, func: Callable[[], Any]) -> dict[str, Any]:
    started = time.perf_counter()
    recorder.log("start", op=name)
    try:
        result = func()
        elapsed = round(time.perf_counter() - started, 3)
        recorder.log("finish", op=name, elapsed_seconds=elapsed, result=clean(result)[:500])
        return {"op": name, "ok": True, "elapsed_seconds": elapsed, "result": clean(result)}
    except Exception as exc:
        elapsed = round(time.perf_counter() - started, 3)
        error = f"{type(exc).__name__}: {exc}"
        recorder.log("error", op=name, elapsed_seconds=elapsed, error=error)
        return {"op": name, "ok": False, "elapsed_seconds": elapsed, "error": error}


def prepare_chat(wx: Any, contact: str, recorder: Recorder) -> Any:
    chat = None
    get_sub = getattr(wx, "GetSubWindow", None)
    if callable(get_sub):
        chat = get_sub(contact)
    if chat is None:
        recorder.log("prepare_add_listen", contact=contact)
        chat = wx.AddListenChat(nickname=contact, callback=lambda *_args, **_kwargs: None)
    recorder.log("prepare_chat", contact=contact, chat=clean(chat), who=clean(getattr(chat, "who", "")))
    return chat


def latest_forwardable_message(chat: Any, recorder: Recorder) -> Any:
    messages = list(chat.GetAllMessage() or [])
    recorder.log("load_forwardable_messages", count=len(messages), chat=clean(getattr(chat, "who", "")))
    for msg in reversed(messages):
        if callable(getattr(msg, "forward", None)):
            recorder.log(
                "select_forwardable_message",
                msg_type=clean(getattr(msg, "type", "")),
                content=clean(getattr(msg, "content", ""))[:120],
            )
            return msg
    raise RuntimeError("未找到可转发素材消息")


def seed_forwardable_message(chat: Any, recorder: Recorder) -> Any:
    timed_call(
        "seed.child.SendMsg",
        recorder,
        lambda: chat.SendMsg(f"【WXBot UI冲突测试】素材种子 {stamp()}，请忽略。"),
    )
    time.sleep(1.5)
    return latest_forwardable_message(chat, recorder)


class CaseContext:
    def __init__(self, contacts: list[str], recorder: Recorder, assets: dict[str, str], *, scan_count: int, history_count: int):
        from wxautox4 import WeChat

        self.contacts = contacts
        self.recorder = recorder
        self.assets = assets
        self.scan_count = scan_count
        self.history_count = history_count
        self.wx = WeChat()
        self.chats: dict[str, Any] = {}
        self._material_message = None

    def chat(self, contact: str) -> Any:
        if contact not in self.chats:
            self.chats[contact] = prepare_chat(self.wx, contact, self.recorder)
        return self.chats[contact]

    @property
    def material_message(self) -> Any:
        if self._material_message is None:
            self._material_message = seed_forwardable_message(self.chat(self.contacts[0]), self.recorder)
        return self._material_message

    def op(self, name: str) -> Callable[[], Any]:
        c = self.contacts
        if name == "main.contact_scan":
            return self.main_contact_scan
        if name == "main.material_history_read":
            return lambda: self.main_material_history_read(c[0])
        if name == "main.ChatWith":
            return lambda: self.wx.ChatWith(c[2], exact=True)
        if name == "main.GetAllMessage":
            return lambda: len(self.wx.GetAllMessage() or [])
        if name == "main.AddListenChat":
            return lambda: self.wx.AddListenChat(nickname=c[3], callback=lambda *_args, **_kwargs: None)
        if name == "main.SendMsg":
            return lambda: self.wx.SendMsg(f"【WXBot UI冲突测试】主窗口发送 {stamp()}，请忽略。", who=c[2], exact=True)
        if name == "main.SendFiles":
            return lambda: self.wx.SendFiles(self.assets["txt"], who=c[2], exact=True)
        if name == "main.SendAudio":
            return lambda: self.wx.SendAudio(
                self.assets["wav"],
                duration=int(self.assets.get("voice_seconds") or 12),
                who=c[2],
                exact=True,
            )
        if name == "main.ChatInfo":
            return self.main_chat_info
        if name == "child.GetAllMessage":
            return lambda: len(self.chat(c[0]).GetAllMessage() or [])
        if name == "child.SendMsg":
            return lambda: self.chat(c[0]).SendMsg(f"【WXBot UI冲突测试】子窗口文字 {stamp()}，请忽略。")
        if name == "child.SendFiles":
            return lambda: self.chat(c[1]).SendFiles(self.assets["txt"])
        if name == "child.SendAudio":
            return lambda: self.chat(c[1]).SendAudio(
                self.assets["wav"],
                duration=int(self.assets.get("voice_seconds") or 12),
            )
        if name == "message.forward":
            return lambda: self.material_message.forward(c[2])
        raise KeyError(name)

    def main_contact_scan(self) -> Any:
        hits: list[str] = []

        def callback(value: Any) -> bool:
            name = clean(value.get("昵称") if isinstance(value, dict) else value)
            if name:
                hits.append(name)
            if len(hits) <= 5 or len(hits) % 10 == 0:
                self.recorder.log("contact_scan_hit", count=len(hits), name=name)
            return len(hits) >= self.scan_count

        self.wx.SwitchToContact()
        result = self.wx.GetFriendDetails(n=self.scan_count, interval=0.1, speed=5, callback=callback)
        return {"items": len(result or []), "hits": len(hits), "first_hits": hits[:10]}

    def main_material_history_read(self, source: str) -> Any:
        self.wx.ChatWith(source, exact=True)
        messages = self.wx.GetHistoryMessage(self.history_count, interval=0.2, speed=3, goback=True)
        return len(messages or [])

    def main_chat_info(self) -> Any:
        self.wx.ChatWith(self.contacts[2], exact=True)
        info = self.wx.ChatInfo()
        if isinstance(info, dict):
            return {key: info.get(key) for key in ("chat_type", "chat_name", "备注", "标签") if key in info}
        return info

    def switch_back(self) -> None:
        try:
            self.wx.SwitchToChat()
        except Exception as exc:
            self.recorder.log("switch_back_error", error=f"{type(exc).__name__}: {exc}")


CASES: dict[str, dict[str, Any]] = {
    "baseline_contact_scan": {
        "primary": ["main.contact_scan"],
        "delayed": [],
    },
    "baseline_main_history_read": {
        "primary": ["main.material_history_read"],
        "delayed": [],
    },
    "baseline_child_voice": {
        "primary": ["child.SendAudio"],
        "delayed": [],
    },
    "baseline_main_voice": {
        "primary": ["main.SendAudio"],
        "delayed": [],
    },
    "main_scan_vs_child_text_read": {
        "primary": ["main.contact_scan"],
        "delayed": [("child.SendMsg", 3), ("child.GetAllMessage", 5)],
    },
    "main_scan_vs_child_file_audio_forward": {
        "primary": ["main.contact_scan"],
        "delayed": [("child.SendFiles", 3), ("child.SendAudio", 5), ("message.forward", 7)],
    },
    "main_scan_vs_main_sends": {
        "primary": ["main.contact_scan"],
        "delayed": [("main.SendMsg", 3), ("main.SendFiles", 5), ("main.SendAudio", 7)],
    },
    "main_scan_vs_material_pool_reads": {
        "primary": ["main.contact_scan"],
        "delayed": [("child.GetAllMessage", 3), ("main.material_history_read", 5)],
    },
    "main_history_vs_child_sends": {
        "primary": ["main.material_history_read"],
        "delayed": [("child.SendMsg", 1), ("child.SendAudio", 2), ("child.SendFiles", 3)],
    },
    "main_history_vs_forward_and_main_sends": {
        "primary": ["main.material_history_read"],
        "delayed": [("message.forward", 1), ("main.SendMsg", 2), ("main.SendAudio", 3)],
    },
    "main_chatwith_vs_child_read_send": {
        "primary": ["main.ChatWith"],
        "delayed": [("child.GetAllMessage", 0), ("child.SendMsg", 0)],
    },
    "main_chatinfo_vs_child_send_read": {
        "primary": ["main.ChatInfo"],
        "delayed": [("child.SendMsg", 0), ("child.GetAllMessage", 0)],
    },
    "main_send_vs_child_read": {
        "primary": ["main.SendMsg"],
        "delayed": [("child.GetAllMessage", 0)],
    },
    "main_file_vs_child_send_read": {
        "primary": ["main.SendFiles"],
        "delayed": [("child.SendMsg", 0), ("child.GetAllMessage", 0)],
    },
    "main_audio_vs_child_send": {
        "primary": ["main.SendAudio"],
        "delayed": [("child.SendMsg", 0)],
    },
    "add_listen_vs_child_send_read": {
        "primary": ["main.AddListenChat"],
        "delayed": [("child.SendMsg", 0), ("child.GetAllMessage", 0)],
    },
    "parallel_child_text_file_audio": {
        "primary": ["child.SendMsg", "child.SendFiles", "child.SendAudio"],
        "delayed": [],
    },
    "parallel_child_forward_send_file_audio_read": {
        "primary": ["message.forward", "child.SendMsg", "child.SendFiles", "child.SendAudio", "child.GetAllMessage"],
        "delayed": [],
    },
    "main_getall_vs_child_send": {
        "primary": ["main.GetAllMessage"],
        "delayed": [("child.SendMsg", 0)],
    },
}


def run_case_child(args: argparse.Namespace) -> int:
    contacts = [clean(item) for item in args.contacts if clean(item)]
    if len(contacts) < 5:
        raise SystemExit("至少需要 5 个测试联系人")
    recorder = Recorder()
    ffmpeg_path = configure_ffmpeg_path(args.ffmpeg_dir)
    assets = make_assets(voice_seconds=args.voice_seconds)
    case = CASES[args.child_case]
    recorder.log("case_begin", case=args.child_case, contacts=contacts, assets=assets, ffmpeg_path=ffmpeg_path)
    context = CaseContext(contacts, recorder, assets, scan_count=args.scan_count, history_count=args.history_count)
    outputs: dict[str, Any] = {}

    def run_named(op_name: str, delay: float = 0) -> None:
        if delay > 0:
            time.sleep(delay)
        key = op_name
        suffix = 2
        while key in outputs:
            key = f"{op_name}#{suffix}"
            suffix += 1
        outputs[key] = timed_call(op_name, recorder, context.op(op_name))

    threads: list[threading.Thread] = []
    for op_name in case.get("primary") or []:
        threads.append(threading.Thread(target=run_named, args=(op_name, 0), daemon=True))
    for op_name, delay in case.get("delayed") or []:
        threads.append(threading.Thread(target=run_named, args=(op_name, float(delay)), daemon=True))
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=args.child_join_timeout)
    alive = [thread.name for thread in threads if thread.is_alive()]
    context.switch_back()
    report = {
        "case": args.child_case,
        "generated_at": datetime.now().replace(microsecond=0).isoformat(),
        "outputs": outputs,
        "alive_threads": alive,
        "events": recorder.events,
    }
    if args.child_report:
        write_json(Path(args.child_report), report)
    recorder.log("case_finish", case=args.child_case, alive_threads=alive)
    print(json.dumps(report, ensure_ascii=False, indent=2, default=json_default), flush=True)
    return 2 if alive else 0


def kill_process_tree(pid: int) -> None:
    subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def run_parent(args: argparse.Namespace) -> int:
    ffmpeg_path = configure_ffmpeg_path(args.ffmpeg_dir)
    contacts = [clean(item) for item in args.contacts if clean(item)]
    run_id = label()
    report_dir = REPORT_DIR / run_id
    report_dir.mkdir(parents=True, exist_ok=True)
    selected_cases = args.cases or list(CASES)
    summary: dict[str, Any] = {
        "run_id": run_id,
        "generated_at": datetime.now().replace(microsecond=0).isoformat(),
        "contacts": contacts,
        "ffmpeg_path": ffmpeg_path,
        "case_timeout": args.case_timeout,
        "cases": {},
    }
    for case_name in selected_cases:
        if case_name not in CASES:
            raise SystemExit(f"未知场景：{case_name}")
        print(f"\n=== RUN {case_name} ===", flush=True)
        child_report = report_dir / f"{case_name}.json"
        cmd = [
            sys.executable,
            "-X",
            "utf8",
            __file__,
            "--child-case",
            case_name,
            "--child-report",
            str(child_report),
            "--scan-count",
            str(args.scan_count),
            "--history-count",
            str(args.history_count),
            "--child-join-timeout",
            str(args.child_join_timeout),
            "--voice-seconds",
            str(args.voice_seconds),
            "--ffmpeg-dir",
            args.ffmpeg_dir,
            "--contacts",
            *contacts,
        ]
        child_env = dict(os.environ)
        started = time.perf_counter()
        proc = subprocess.Popen(
            cmd,
            cwd=os.getcwd(),
            env=child_env,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        timed_out = False
        try:
            stdout, _ = proc.communicate(timeout=args.case_timeout)
        except subprocess.TimeoutExpired:
            timed_out = True
            kill_process_tree(proc.pid)
            stdout, _ = proc.communicate(timeout=10)
        elapsed = round(time.perf_counter() - started, 3)
        print(stdout[-6000:] if stdout else "", flush=True)
        case_payload: dict[str, Any] = {
            "status": "timeout" if timed_out else ("ok" if proc.returncode == 0 else "failed"),
            "returncode": proc.returncode,
            "elapsed_seconds": elapsed,
            "report": str(child_report),
            "stdout_tail": stdout[-12000:] if stdout else "",
        }
        if child_report.exists():
            try:
                case_payload["child_report"] = json.loads(child_report.read_text(encoding="utf-8"))
            except Exception as exc:
                case_payload["child_report_error"] = str(exc)
        summary["cases"][case_name] = case_payload
        write_json(report_dir / "summary.json", summary)
    print(f"\nSUMMARY: {report_dir / 'summary.json'}", flush=True)
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="wxautox4 主窗口/子窗口 UI 冲突矩阵实机测试")
    parser.add_argument("--contacts", nargs="*", default=DEFAULT_CONTACTS)
    parser.add_argument("--cases", nargs="*", default=[])
    parser.add_argument("--scan-count", type=int, default=30)
    parser.add_argument("--history-count", type=int, default=20)
    parser.add_argument("--voice-seconds", type=int, default=12)
    parser.add_argument("--case-timeout", type=float, default=150)
    parser.add_argument("--child-join-timeout", type=float, default=90)
    parser.add_argument("--ffmpeg-dir", default=DEFAULT_FFMPEG_DIR)
    parser.add_argument("--child-case", choices=sorted(CASES), default="")
    parser.add_argument("--child-report", default="")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.child_case:
        return run_case_child(args)
    return run_parent(args)


if __name__ == "__main__":
    raise SystemExit(main())
