from __future__ import annotations

import json
import sys
import time
import traceback
from datetime import datetime

from wxautox4 import WeChat


URL = "https://page.om.qq.com/page/OoiLJQ5R0k3IvH1e2RbbkQyw0"
MESSAGE = "测试SendUrlCard"
FRIENDS = ["LXYou", "阿英2", "瑞东（私人号）"]


def log_step(message: str) -> None:
    stamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{stamp}] {message}", flush=True)


def main() -> int:
    started_at = time.time()
    log_step("开始最小实机测试：SendUrlCard")
    log_step(f"目标联系人：{json.dumps(FRIENDS, ensure_ascii=False)}")
    log_step(f"链接：{URL}")
    log_step(f"附带文字：{MESSAGE}")

    try:
        wx = WeChat()
        log_step("WeChat 实例创建成功，准备发送链接卡片")
        result = wx.SendUrlCard(
            url=URL,
            friends=FRIENDS,
            message=MESSAGE,
            timeout=5,
        )
        duration = round(time.time() - started_at, 3)
        log_step(f"发送调用已返回，耗时 {duration} 秒")
        print(json.dumps({"ok": True, "result": result}, ensure_ascii=False, indent=2, default=str))
        return 0
    except Exception as exc:
        duration = round(time.time() - started_at, 3)
        log_step(f"发送失败，耗时 {duration} 秒：{exc}")
        log_step(traceback.format_exc().rstrip())
        print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False, indent=2))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
