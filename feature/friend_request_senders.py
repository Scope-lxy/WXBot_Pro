"""Send friend requests through existing verification prompts."""

from __future__ import annotations

import random
import time
from dataclasses import dataclass
from typing import Any

from core.wechat_window import bring_wechat_main_window_to_front


VERIFY_LINK_TEXT = "发送朋友验证"
VERIFY_EVIDENCE_TEXT = "开启了朋友验证"


def _clean_text(value: Any) -> str:
    return str(value or "").strip()


def is_wechat_link_blue(rgb: tuple[int, int, int]) -> bool:
    r, g, b = rgb
    return b >= 100 and b > r + 25 and b > g + 6 and r < 150 and g < 175


def blue_text_fragments_from_image(image, *, origin: tuple[int, int] = (0, 0)) -> list[dict[str, Any]]:
    """Return blue text fragments grouped by visual row from a small message image."""
    img = image.convert("RGB")
    width, height = img.size
    pixels = img.load()
    mask: set[tuple[int, int]] = set()
    for y in range(height):
        for x in range(width):
            if is_wechat_link_blue(pixels[x, y]):
                mask.add((x, y))

    components: list[dict[str, Any]] = []
    while mask:
        seed = mask.pop()
        stack = [seed]
        xs = [seed[0]]
        ys = [seed[1]]
        count = 1
        while stack:
            x, y = stack.pop()
            for nx in (x - 1, x, x + 1):
                for ny in (y - 1, y, y + 1):
                    if (nx, ny) in mask:
                        mask.remove((nx, ny))
                        stack.append((nx, ny))
                        xs.append(nx)
                        ys.append(ny)
                        count += 1
        x1, y1, x2, y2 = min(xs), min(ys), max(xs), max(ys)
        if count >= 2 and (y2 - y1 + 1) >= 3:
            components.append({
                "bbox": (x1 + origin[0], y1 + origin[1], x2 + origin[0], y2 + origin[1]),
                "count": count,
            })

    components.sort(key=lambda item: (item["bbox"][1], item["bbox"][0]))
    rows: list[dict[str, Any]] = []
    for component in components:
        x1, y1, x2, y2 = component["bbox"]
        cy = (y1 + y2) / 2
        for row in rows:
            if abs(row["cy"] - cy) <= 6:
                row["items"].append(component)
                row["cy"] = (row["cy"] * row["n"] + cy) / (row["n"] + 1)
                row["n"] += 1
                break
        else:
            rows.append({"cy": cy, "n": 1, "items": [component]})

    rows.sort(key=lambda row: row["cy"])
    fragments: list[dict[str, Any]] = []
    for row_index, row in enumerate(rows):
        items = sorted(row["items"], key=lambda item: item["bbox"][0])
        groups: list[list[dict[str, Any]]] = []
        group: list[dict[str, Any]] = []
        last_right = None
        for item in items:
            x1, _y1, x2, _y2 = item["bbox"]
            if not group or x1 - int(last_right or x1) <= 14:
                group.append(item)
            else:
                groups.append(group)
                group = [item]
            last_right = x2
        if group:
            groups.append(group)
        for group_items in groups:
            x1 = min(item["bbox"][0] for item in group_items)
            y1 = min(item["bbox"][1] for item in group_items)
            x2 = max(item["bbox"][2] for item in group_items)
            y2 = max(item["bbox"][3] for item in group_items)
            count = sum(int(item["count"]) for item in group_items)
            width_value = x2 - x1 + 1
            height_value = y2 - y1 + 1
            fragments.append({
                "row": row_index,
                "bbox": (x1, y1, x2, y2),
                "width": width_value,
                "height": height_value,
                "count": count,
                "char_est": max(1, round(width_value / 12)),
            })
    return fragments


def choose_blue_text_fragment(fragments: list[dict[str, Any]]) -> dict[str, Any] | None:
    valid = [
        fragment for fragment in fragments
        if int(fragment.get("width", 0)) >= 30 and int(fragment.get("height", 0)) >= 7
    ]
    if not valid:
        return None

    def score(fragment: dict[str, Any]) -> tuple[float, float, float]:
        x1, _y1, _x2, _y2 = fragment["bbox"]
        width_score = min(int(fragment.get("width", 0)), 80)
        row_score = int(fragment.get("row", 0)) * 8
        return (width_score, row_score, x1 / 1000)

    return sorted(valid, key=score, reverse=True)[0]


def click_point_with_offset(fragment: dict[str, Any], *, rng: random.Random | None = None) -> tuple[int, int]:
    rng = rng or random.Random()
    x1, y1, x2, y2 = fragment["bbox"]
    return (
        int(x1 + (x2 - x1) * rng.uniform(0.35, 0.70)),
        int(y1 + (y2 - y1) * rng.uniform(0.40, 0.65)),
    )


@dataclass
class ConversationVerifyResult:
    status: str
    message: str
    data: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return {"status": self.status, "message": self.message, "data": self.data}


class ConversationVerifySender:
    """Click the existing chat verification link and submit an add-friend request."""

    def __init__(self, *, wait_after_front: float = 0.35, assert_owner_thread=None):
        self.wait_after_front = wait_after_front
        self._assert_owner_thread = assert_owner_thread

    def _front(self) -> None:
        bring_wechat_main_window_to_front(wait=self.wait_after_front)

    def _find_verify_message(self, wx):
        for msg in reversed(wx.GetAllMessage()):
            content = _clean_text(getattr(msg, "content", ""))
            if VERIFY_LINK_TEXT in content and VERIFY_EVIDENCE_TEXT in content:
                return msg
        return None

    def _click_verify_link(self, msg) -> dict[str, Any]:
        from PIL import ImageGrab
        from wxautox4 import uia

        rect = msg.control.BoundingRectangle
        bbox = (
            max(0, int(rect.left)),
            max(0, int(rect.top) - 3),
            max(0, int(rect.right)),
            max(0, int(rect.bottom) + 3),
        )
        image = ImageGrab.grab(bbox=bbox)
        fragments = blue_text_fragments_from_image(image, origin=(bbox[0], bbox[1]))
        fragment = choose_blue_text_fragment(fragments)
        if not fragment:
            raise RuntimeError("未能定位发送朋友验证蓝色文字")
        click_point = click_point_with_offset(fragment)
        uia.Click(*click_point)
        return {
            "message_rect": str(rect),
            "capture_bbox": bbox,
            "fragments": fragments,
            "chosen_fragment": fragment,
            "click_point": click_point,
        }

    def send(
        self,
        bot,
        target_name: str,
        *,
        addmsg: str = "",
        remark: str = "",
        tags: list[str] | None = None,
        max_attempts: int = 2,
    ) -> dict[str, Any]:
        if not callable(self._assert_owner_thread):
            raise RuntimeError("好友申请 UI 只能由微信 UI owner 执行")
        self._assert_owner_thread()
        if not getattr(bot, "wx", None):
            raise RuntimeError("微信客户端未初始化")
        target_name = _clean_text(target_name)
        if not target_name:
            raise RuntimeError("缺少目标联系人")
        tags = list(tags or [])
        last_error = ""
        for attempt in range(1, max(1, int(max_attempts or 1)) + 1):
            try:
                self._front()
                bot.wx.ChatWith(target_name, exact=True)
                time.sleep(0.8)
                self._front()
                chat_info = bot.wx.ChatInfo() or {}
                chat_name = _clean_text(chat_info.get("chat_name"))
                if chat_name != target_name:
                    raise RuntimeError(f"当前会话不是目标联系人：{chat_info}")
                msg = self._find_verify_message(bot.wx)
                if not msg:
                    return ConversationVerifyResult(
                        "skipped",
                        "未找到发送朋友验证入口",
                        {"target": target_name, "chat_info": chat_info},
                    ).to_dict()
                click_meta = self._click_verify_link(msg)
                time.sleep(1.0)
                from wxautox4.ui.component import AddFriendsWnd

                wnd = AddFriendsWnd()
                send_kwargs = {
                    "addmsg": _clean_text(addmsg),
                    "remark": _clean_text(remark) or target_name,
                    "tags": tags,
                }
                try:
                    wnd.send(**send_kwargs)
                except Exception as exc:
                    return ConversationVerifyResult(
                        "uncertain",
                        f"好友申请已进入提交阶段，但未能确认结果：{exc}",
                        {
                            "target": target_name,
                            "addmsg": _clean_text(addmsg),
                            "remark": _clean_text(remark) or target_name,
                            "attempt": attempt,
                            "phase": "submit",
                            "click": click_meta,
                        },
                    ).to_dict()
                return ConversationVerifyResult(
                    "sent",
                    "好友验证申请已提交",
                    {
                        "target": target_name,
                        "addmsg": _clean_text(addmsg),
                        "remark": _clean_text(remark) or target_name,
                        "attempt": attempt,
                        "click": click_meta,
                    },
                ).to_dict()
            except Exception as exc:
                last_error = str(exc)
                self._front()
                if attempt >= max(1, int(max_attempts or 1)):
                    break
        return ConversationVerifyResult(
            "failed",
            last_error or "发送好友验证失败",
            {"target": target_name},
        ).to_dict()
