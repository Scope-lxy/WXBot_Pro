from __future__ import annotations

import argparse
import json
import math
import struct
import threading
import time
import wave
from datetime import datetime
from pathlib import Path
from typing import Any, Callable


DEFAULT_CONTACTS = ["阿英2", "阿英3", "阿英4"]
REPORT_DIR = Path("backups") / "ui_parallelism_probes"
ASSET_DIR = REPORT_DIR / "assets"


def now_label() -> str:
    return datetime.now().strftime("%Y%m%d_%H%M%S")


def stamp() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]


def clean(value: Any) -> str:
    return str(value or "").strip()


def json_default(value: Any) -> str:
    return str(value)


def make_test_wav() -> Path:
    ASSET_DIR.mkdir(parents=True, exist_ok=True)
    path = ASSET_DIR / "ui_parallelism_voice_test.wav"
    if path.exists():
        return path.resolve()
    sample_rate = 16000
    duration_seconds = 1.0
    frames = int(sample_rate * duration_seconds)
    with wave.open(str(path), "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(sample_rate)
        for index in range(frames):
            value = int(12000 * math.sin(2 * math.pi * 440 * index / sample_rate))
            wav.writeframesraw(struct.pack("<h", value))
    return path.resolve()


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
        recorder.log("error", op=name, elapsed_seconds=elapsed, error=f"{type(exc).__name__}: {exc}")
        return {"op": name, "ok": False, "elapsed_seconds": elapsed, "error": f"{type(exc).__name__}: {exc}"}


def prepare_chat(wx: Any, contact: str, recorder: Recorder) -> Any:
    chat = None
    get_sub = getattr(wx, "GetSubWindow", None)
    if callable(get_sub):
        chat = get_sub(contact)
    if chat is None:
        recorder.log("prepare_chat_add_listen", contact=contact)
        chat = wx.AddListenChat(nickname=contact, callback=lambda *_args, **_kwargs: None)
    recorder.log("prepare_chat_done", contact=contact, chat=clean(chat), who=clean(getattr(chat, "who", "")))
    return chat


def latest_forwardable_message(chat: Any, recorder: Recorder, *, label: str) -> Any:
    messages = list(chat.GetAllMessage() or [])
    recorder.log("material_messages_loaded", label=label, count=len(messages))
    for msg in reversed(messages):
        forward = getattr(msg, "forward", None)
        if callable(forward):
            recorder.log(
                "material_message_selected",
                label=label,
                type=clean(getattr(msg, "type", "")),
                content=clean(getattr(msg, "content", ""))[:120],
            )
            return msg
    raise RuntimeError(f"{label} 未找到可转发消息")


def seed_material_message(chat: Any, recorder: Recorder) -> Any:
    timed_call(
        "seed_material_source.SendMsg",
        recorder,
        lambda: chat.SendMsg(f"【WXBot UI并行测试】素材种子消息 {stamp()}，请忽略。"),
    )
    time.sleep(1.5)
    return latest_forwardable_message(chat, recorder, label="seed_material_source")


def run_contact_scan(wx: Any, recorder: Recorder, *, count: int, interval: float, speed: int) -> dict[str, Any]:
    hits: list[str] = []

    def callback(value: Any) -> bool:
        name = clean(value.get("昵称") if isinstance(value, dict) else value)
        if name:
            hits.append(name)
        if len(hits) <= 5 or len(hits) % 10 == 0:
            recorder.log("contact_scan_hit", count=len(hits), name=name)
        return False

    def action() -> Any:
        wx.SwitchToContact()
        return wx.GetFriendDetails(n=count, interval=interval, speed=speed, callback=callback)

    summary = timed_call("main.SwitchToContact+GetFriendDetails", recorder, action)
    summary["hit_count"] = len(hits)
    summary["first_hits"] = hits[:10]
    return summary


def run_delayed(name: str, delay: float, recorder: Recorder, func: Callable[[], Any], output: dict[str, Any]) -> None:
    time.sleep(max(0, float(delay)))
    output[name] = timed_call(name, recorder, func)


def test_main_scan_with_child_ops(wx: Any, chats: dict[str, Any], contacts: list[str], recorder: Recorder, args: argparse.Namespace) -> dict[str, Any]:
    recorder.log("case_begin", case="main_scan_with_child_ops")
    outputs: dict[str, Any] = {}
    scan_thread = threading.Thread(
        target=lambda: outputs.update({
            "main_scan": run_contact_scan(wx, recorder, count=args.scan_count, interval=args.scan_interval, speed=args.scan_speed)
        }),
        daemon=True,
    )
    scan_thread.start()
    workers = [
        threading.Thread(
            target=run_delayed,
            args=(
                "child.SendMsg_during_main_scan",
                args.child_delay,
                recorder,
                lambda: chats[contacts[0]].SendMsg(f"【WXBot UI并行测试】子窗口发消息 during 通讯录读取 {stamp()}，请忽略。"),
                outputs,
            ),
            daemon=True,
        ),
        threading.Thread(
            target=run_delayed,
            args=(
                "child.GetAllMessage_during_main_scan",
                args.child_delay + 2,
                recorder,
                lambda: len(chats[contacts[1]].GetAllMessage() or []),
                outputs,
            ),
            daemon=True,
        ),
    ]
    for worker in workers:
        worker.start()
    scan_thread.join(timeout=args.case_timeout)
    for worker in workers:
        worker.join(timeout=args.case_timeout)
    try:
        wx.SwitchToChat()
    except Exception as exc:
        recorder.log("switch_back_error", error=f"{type(exc).__name__}: {exc}")
    recorder.log("case_finish", case="main_scan_with_child_ops")
    return outputs


def test_main_scan_with_voice_and_material_forward(
    wx: Any,
    chats: dict[str, Any],
    contacts: list[str],
    recorder: Recorder,
    args: argparse.Namespace,
    *,
    audio_path: Path,
    material_message: Any,
) -> dict[str, Any]:
    recorder.log("case_begin", case="main_scan_with_voice_and_material_forward")
    outputs: dict[str, Any] = {}
    scan_thread = threading.Thread(
        target=lambda: outputs.update({
            "main_scan": run_contact_scan(wx, recorder, count=args.scan_count, interval=args.scan_interval, speed=args.scan_speed)
        }),
        daemon=True,
    )
    scan_thread.start()
    workers = [
        threading.Thread(
            target=run_delayed,
            args=(
                "child.SendAudio_during_main_scan",
                args.child_delay,
                recorder,
                lambda: chats[contacts[0]].SendAudio(str(audio_path), duration=1),
                outputs,
            ),
            daemon=True,
        ),
        threading.Thread(
            target=run_delayed,
            args=(
                "message.forward_during_main_scan",
                args.child_delay + 2,
                recorder,
                lambda: material_message.forward(contacts[1]),
                outputs,
            ),
            daemon=True,
        ),
    ]
    for worker in workers:
        worker.start()
    scan_thread.join(timeout=args.case_timeout)
    for worker in workers:
        worker.join(timeout=args.case_timeout)
    try:
        wx.SwitchToChat()
    except Exception as exc:
        recorder.log("switch_back_error", error=f"{type(exc).__name__}: {exc}")
    recorder.log("case_finish", case="main_scan_with_voice_and_material_forward")
    return outputs


def test_main_scan_with_material_pool_reads(
    wx: Any,
    chats: dict[str, Any],
    contacts: list[str],
    recorder: Recorder,
    args: argparse.Namespace,
) -> dict[str, Any]:
    recorder.log("case_begin", case="main_scan_with_material_pool_reads")
    outputs: dict[str, Any] = {}
    source = contacts[0]
    scan_thread = threading.Thread(
        target=lambda: outputs.update({
            "main_scan": run_contact_scan(wx, recorder, count=args.scan_count, interval=args.scan_interval, speed=args.scan_speed)
        }),
        daemon=True,
    )
    scan_thread.start()

    def main_history_read() -> Any:
        wx.ChatWith(source, exact=True)
        messages = wx.GetHistoryMessage(args.history_count, interval=0.2, speed=3, goback=True)
        return len(messages or [])

    workers = [
        threading.Thread(
            target=run_delayed,
            args=(
                "material_pool.child_visible_GetAllMessage_during_main_scan",
                args.child_delay,
                recorder,
                lambda: len(chats[source].GetAllMessage() or []),
                outputs,
            ),
            daemon=True,
        ),
        threading.Thread(
            target=run_delayed,
            args=(
                "material_pool.main_ChatWith_GetHistoryMessage_during_main_scan",
                args.child_delay + 2,
                recorder,
                main_history_read,
                outputs,
            ),
            daemon=True,
        ),
    ]
    for worker in workers:
        worker.start()
    scan_thread.join(timeout=args.case_timeout)
    for worker in workers:
        worker.join(timeout=args.case_timeout)
    try:
        wx.SwitchToChat()
    except Exception as exc:
        recorder.log("switch_back_error", error=f"{type(exc).__name__}: {exc}")
    recorder.log("case_finish", case="main_scan_with_material_pool_reads")
    return outputs


def test_two_child_sends(chats: dict[str, Any], contacts: list[str], recorder: Recorder) -> dict[str, Any]:
    recorder.log("case_begin", case="two_child_sends")
    outputs: dict[str, Any] = {}
    start = threading.Event()

    def make_runner(key: str, contact: str) -> Callable[[], None]:
        def runner() -> None:
            start.wait()
            outputs[key] = timed_call(
                key,
                recorder,
                lambda: chats[contact].SendMsg(f"【WXBot UI并行测试】两个子窗口同时发送 {contact} {stamp()}，请忽略。"),
            )
        return runner

    threads = [
        threading.Thread(target=make_runner("child.SendMsg_A", contacts[0]), daemon=True),
        threading.Thread(target=make_runner("child.SendMsg_B", contacts[1]), daemon=True),
    ]
    for thread in threads:
        thread.start()
    recorder.log("release_parallel_children")
    start.set()
    for thread in threads:
        thread.join(timeout=60)
    recorder.log("case_finish", case="two_child_sends")
    return outputs


def test_parallel_child_mixed_ops(
    chats: dict[str, Any],
    contacts: list[str],
    recorder: Recorder,
    *,
    audio_path: Path,
    material_message: Any,
) -> dict[str, Any]:
    recorder.log("case_begin", case="parallel_child_mixed_ops")
    outputs: dict[str, Any] = {}
    start = threading.Event()

    operations: list[tuple[str, Callable[[], Any]]] = [
        (
            "child.SendMsg_parallel_mixed",
            lambda: chats[contacts[0]].SendMsg(f"【WXBot UI并行测试】混合并发文字 {stamp()}，请忽略。"),
        ),
        (
            "child.SendAudio_parallel_mixed",
            lambda: chats[contacts[1]].SendAudio(str(audio_path), duration=1),
        ),
        (
            "message.forward_parallel_mixed",
            lambda: material_message.forward(contacts[2]),
        ),
    ]

    def make_runner(key: str, func: Callable[[], Any]) -> Callable[[], None]:
        def runner() -> None:
            start.wait()
            outputs[key] = timed_call(key, recorder, func)
        return runner

    threads = [threading.Thread(target=make_runner(key, func), daemon=True) for key, func in operations]
    for thread in threads:
        thread.start()
    recorder.log("release_parallel_child_mixed_ops")
    start.set()
    for thread in threads:
        thread.join(timeout=60)
    recorder.log("case_finish", case="parallel_child_mixed_ops")
    return outputs


def test_main_chatwith_with_child_ops(wx: Any, chats: dict[str, Any], contacts: list[str], recorder: Recorder) -> dict[str, Any]:
    recorder.log("case_begin", case="main_chatwith_with_child_ops")
    outputs: dict[str, Any] = {}
    start = threading.Event()

    def main_runner() -> None:
        start.wait()
        outputs["main.ChatWith"] = timed_call("main.ChatWith", recorder, lambda: wx.ChatWith(contacts[2], exact=True))

    def child_runner() -> None:
        start.wait()
        outputs["child.GetAllMessage_during_main_chatwith"] = timed_call(
            "child.GetAllMessage_during_main_chatwith",
            recorder,
            lambda: len(chats[contacts[0]].GetAllMessage() or []),
        )

    threads = [threading.Thread(target=main_runner, daemon=True), threading.Thread(target=child_runner, daemon=True)]
    for thread in threads:
        thread.start()
    recorder.log("release_main_chatwith_child_read")
    start.set()
    for thread in threads:
        thread.join(timeout=60)
    try:
        wx.SwitchToChat()
    except Exception as exc:
        recorder.log("switch_back_error", error=f"{type(exc).__name__}: {exc}")
    recorder.log("case_finish", case="main_chatwith_with_child_ops")
    return outputs


def test_main_send_with_child_read(wx: Any, chats: dict[str, Any], contacts: list[str], recorder: Recorder) -> dict[str, Any]:
    recorder.log("case_begin", case="main_send_with_child_read")
    outputs: dict[str, Any] = {}
    start = threading.Event()

    def main_runner() -> None:
        start.wait()
        outputs["main.SendMsg"] = timed_call(
            "main.SendMsg",
            recorder,
            lambda: wx.SendMsg(f"【WXBot UI并行测试】主窗口发送 {stamp()}，请忽略。", who=contacts[2], exact=True),
        )

    def child_runner() -> None:
        start.wait()
        outputs["child.GetAllMessage_during_main_send"] = timed_call(
            "child.GetAllMessage_during_main_send",
            recorder,
            lambda: len(chats[contacts[0]].GetAllMessage() or []),
        )

    threads = [threading.Thread(target=main_runner, daemon=True), threading.Thread(target=child_runner, daemon=True)]
    for thread in threads:
        thread.start()
    recorder.log("release_main_send_child_read")
    start.set()
    for thread in threads:
        thread.join(timeout=60)
    recorder.log("case_finish", case="main_send_with_child_read")
    return outputs


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="实测 wxautox4 主窗口/子窗口 UI 操作并行性")
    parser.add_argument("--contacts", nargs="*", default=DEFAULT_CONTACTS, help="测试联系人，至少 3 个")
    parser.add_argument("--scan-count", type=int, default=50)
    parser.add_argument("--scan-interval", type=float, default=0.1)
    parser.add_argument("--scan-speed", type=int, default=5)
    parser.add_argument("--history-count", type=int, default=20)
    parser.add_argument("--child-delay", type=float, default=3.0)
    parser.add_argument("--case-timeout", type=float, default=180.0)
    args = parser.parse_args(argv)

    contacts = [clean(item) for item in args.contacts if clean(item)]
    if len(contacts) < 3:
        raise SystemExit("至少需要 3 个测试联系人")

    from wxautox4 import WeChat

    recorder = Recorder()
    recorder.log("probe_begin", contacts=contacts, scan_count=args.scan_count, scan_interval=args.scan_interval, scan_speed=args.scan_speed)
    wx = WeChat()
    chats = {contact: prepare_chat(wx, contact, recorder) for contact in contacts[:3]}
    audio_path = make_test_wav()
    recorder.log("audio_asset_ready", path=str(audio_path))
    material_message = seed_material_message(chats[contacts[0]], recorder)

    report = {
        "generated_at": datetime.now().replace(microsecond=0).isoformat(),
        "contacts": contacts[:3],
        "cases": {
            "main_scan_with_child_ops": test_main_scan_with_child_ops(wx, chats, contacts, recorder, args),
            "main_scan_with_voice_and_material_forward": test_main_scan_with_voice_and_material_forward(
                wx,
                chats,
                contacts,
                recorder,
                args,
                audio_path=audio_path,
                material_message=material_message,
            ),
            "main_scan_with_material_pool_reads": test_main_scan_with_material_pool_reads(wx, chats, contacts, recorder, args),
            "two_child_sends": test_two_child_sends(chats, contacts, recorder),
            "parallel_child_mixed_ops": test_parallel_child_mixed_ops(
                chats,
                contacts,
                recorder,
                audio_path=audio_path,
                material_message=material_message,
            ),
            "main_chatwith_with_child_ops": test_main_chatwith_with_child_ops(wx, chats, contacts, recorder),
            "main_send_with_child_read": test_main_send_with_child_read(wx, chats, contacts, recorder),
        },
        "events": recorder.events,
    }
    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    path = REPORT_DIR / f"report_{now_label()}.json"
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2, default=json_default), encoding="utf-8")
    recorder.log("probe_finish", report=str(path))
    print(json.dumps(report["cases"], ensure_ascii=False, indent=2, default=json_default))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
