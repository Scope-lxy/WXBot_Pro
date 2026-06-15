from __future__ import annotations

import json
import time
import traceback
from datetime import datetime

from wxautox4 import WeChat


SOURCE_CHAT = "文件传输助手"
SOURCE_TITLE = "亲爱的，我想每天多了解你一点，心就靠近一点！"
MESSAGE = "测试SendUrlCard"
TARGETS = ["LXYou", "阿英2", "瑞东（私人号）"]


def log_step(message: str) -> None:
    stamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{stamp}] {message}", flush=True)


def find_source_message(wx: WeChat):
    wx.ChatWith(SOURCE_CHAT)
    messages = wx.GetAllMessage()
    for message in reversed(messages):
        content = str(getattr(message, "content", "") or "")
        if getattr(message, "type", "") == "link" and SOURCE_TITLE in content:
            return message
    raise RuntimeError(f"未在 {SOURCE_CHAT} 当前可见消息中找到目标链接：{SOURCE_TITLE}")


def main() -> int:
    started_at = time.time()
    log_step("开始最小实机测试：素材链接转发")
    log_step(f"素材来源：{SOURCE_CHAT}")
    log_step(f"素材标题：{SOURCE_TITLE}")
    log_step(f"目标联系人：{json.dumps(TARGETS, ensure_ascii=False)}")
    log_step(f"附带文字：{MESSAGE}")

    try:
        wx = WeChat()
        log_step("WeChat 实例创建成功，准备读取素材消息")
        source_message = find_source_message(wx)
        log_step(f"已找到素材消息：{type(source_message).__name__}")
        result = source_message.forward(
            TARGETS,
            message=MESSAGE,
            timeout=5,
        )
        duration = round(time.time() - started_at, 3)
        log_step(f"转发调用已返回，耗时 {duration} 秒")
        print(json.dumps({"ok": True, "result": result}, ensure_ascii=False, indent=2, default=str))
        return 0
    except Exception as exc:
        duration = round(time.time() - started_at, 3)
        log_step(f"转发失败，耗时 {duration} 秒：{exc}")
        log_step(traceback.format_exc().rstrip())
        print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False, indent=2))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
