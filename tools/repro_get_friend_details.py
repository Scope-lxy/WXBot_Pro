from __future__ import annotations

import argparse
import json
import time
import traceback
from datetime import datetime
from typing import Callable


def default_printer(message: str) -> None:
    print(message, flush=True)


def default_client_factory():
    from wxautox4 import WeChat

    return WeChat()


def normalize_name(value: str) -> str:
    return str(value or "").strip()


def build_get_friend_details_kwargs(
    *,
    count: int,
    interval: float,
    match_name: str,
):
    callback_hits: list[str] = []
    kwargs = {
        "n": max(1, int(count)),
        "interval": max(0.1, float(interval)),
    }
    expected_name = normalize_name(match_name)
    if expected_name:

        def callback(name):
            text = str(name or "")
            callback_hits.append(text)
            return normalize_name(text) == expected_name

        kwargs["callback"] = callback
    return kwargs, callback_hits


def log_step(printer: Callable[[str], None], message: str) -> None:
    stamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    printer(f"[{stamp}] {message}")


def run_probe(
    *,
    client_factory=default_client_factory,
    count: int,
    interval: float,
    match_name: str,
    switch_back: bool,
    show_window: bool = False,
    printer: Callable[[str], None] = default_printer,
):
    client = client_factory()
    kwargs, callback_hits = build_get_friend_details_kwargs(
        count=count,
        interval=interval,
        match_name=match_name,
    )
    started_at = time.time()
    result = None
    error = None

    log_step(printer, "开始最小复现：SwitchToContact -> GetFriendDetails")
    if show_window:
        log_step(printer, "步骤 1/4：Show()")
        client.Show()

    log_step(printer, "步骤 1/3：SwitchToContact()")
    client.SwitchToContact()

    try:
        log_step(printer, f"步骤 2/3：GetFriendDetails({json.dumps({k: v for k, v in kwargs.items() if k != 'callback'}, ensure_ascii=False)})")
        if "callback" in kwargs:
            log_step(printer, f"已启用最简 callback，目标联系人：{normalize_name(match_name)}")
        result = client.GetFriendDetails(**kwargs)
        log_step(printer, "GetFriendDetails 调用已返回")
    except Exception as exc:
        error = exc
        log_step(printer, f"GetFriendDetails 调用异常：{exc}")
        log_step(printer, traceback.format_exc().rstrip())
    finally:
        if switch_back:
            switch_to_chat = getattr(client, "SwitchToChat", None)
            if callable(switch_to_chat):
                try:
                    log_step(printer, "步骤 3/3：SwitchToChat()")
                    switch_to_chat()
                except Exception as exc:
                    log_step(printer, f"SwitchToChat 调用异常：{exc}")

    duration = round(time.time() - started_at, 3)
    summary = {
        "count": max(1, int(count)),
        "interval": max(0.1, float(interval)),
        "match_name": normalize_name(match_name),
        "switch_back": bool(switch_back),
        "show_window": bool(show_window),
        "duration_seconds": duration,
        "callback_hits": callback_hits,
        "result": result,
        "error": str(error) if error else "",
    }
    log_step(printer, f"执行完成，耗时 {duration} 秒")
    log_step(printer, "结果摘要：")
    printer(json.dumps(summary, ensure_ascii=False, indent=2, default=str))
    return summary


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="最小复现 wxautox4 GetFriendDetails 链路，不依赖 WXBot 业务代码。",
    )
    parser.add_argument("--count", type=int, default=5, help="读取联系人数量，默认 5")
    parser.add_argument("--interval", type=float, default=1.5, help="GetFriendDetails 间隔秒数，默认 1.5")
    parser.add_argument("--match-name", default="", help="可选：启用最简 callback，只匹配这个联系人名")
    parser.add_argument(
        "--show-window",
        action="store_true",
        help="先调用 wx.Show() 再切通讯录；默认不调用，避免微信 4.x 主窗口灰屏/重绘异常",
    )
    parser.add_argument(
        "--no-switch-back",
        action="store_true",
        help="默认结束后会尝试 SwitchToChat；传这个参数则不切回聊天页",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    summary = run_probe(
        count=args.count,
        interval=args.interval,
        match_name=args.match_name,
        switch_back=not args.no_switch_back,
        show_window=args.show_window,
    )
    return 1 if summary["error"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
