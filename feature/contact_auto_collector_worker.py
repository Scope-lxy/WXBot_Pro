"""Minimal wxautox4 contact collector used by auto maintenance.

This script is intentionally tiny: it only switches to Contacts, calls
GetFriendDetails, writes raw data to a JSON file, and exits. The parent process
owns the WeChat UI lock, hard timeout, SwitchToChat recovery, and persistence.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import time
import traceback
from pathlib import Path
from typing import Any

from wxautox4 import WeChat


def _clean_text(value: Any) -> str:
    return str(value or "").strip()


def _detail_name(raw_detail: Any) -> str:
    if not isinstance(raw_detail, dict):
        return _clean_text(raw_detail)
    for key in ("备注", "remark", "昵称", "nickname", "name", "微信号", "wechat_id", "wxid"):
        value = _clean_text(raw_detail.get(key))
        if value:
            return value
    return ""


def _contact_name_matches(name: Any, start_name: Any) -> bool:
    start_name = _clean_text(start_name)
    if not start_name:
        return True
    return _clean_text(name) == start_name


def _detail_identity(raw_detail: Any) -> str:
    if not isinstance(raw_detail, dict):
        return ""
    for key in ("微信号", "wechat_id", "wxid"):
        value = _clean_text(raw_detail.get(key))
        if value:
            return f"wechat_id:{value}"
    facts = {
        _clean_text(key): raw_detail.get(key)
        for key in ("备注", "昵称", "地区", "来源", "添加时间", "个性签名", "标签")
        if _clean_text(raw_detail.get(key))
    }
    if not facts:
        return ""
    encoded = json.dumps(facts, ensure_ascii=False, sort_keys=True, default=str).encode("utf-8")
    return f"fingerprint:{hashlib.sha256(encoded).hexdigest()}"


def _write_json(path: str | Path, payload: dict[str, Any]) -> None:
    Path(path).write_text(
        json.dumps(payload, ensure_ascii=False, default=str),
        encoding="utf-8",
    )


def _reset_contact_list_to_top(wx: Any) -> None:
    root_control = wx.NavigationBox.root.control
    contact_list = root_control.ListControl(Name="通讯录", searchDepth=20)
    if not contact_list.Exists(2):
        raise RuntimeError("未找到微信通讯录列表，无法从确定位置开始采集")
    contact_list.SetFocus()
    contact_list.SendKeys("{HOME}")
    for _attempt in range(20):
        time.sleep(0.1)
        contact_list.Refind()
        visible_names = [_clean_text(item.Name) for item in contact_list.GetChildren()[:8]]
        if "新的朋友" in visible_names:
            return
    raise RuntimeError("微信通讯录未能稳定回到顶部，拒绝从未知位置采集")


def collect(request: dict[str, Any]) -> dict[str, Any]:
    count = max(1, int(request.get("count") or 1))
    start_name = _clean_text(request.get("start_name"))
    start_identity = _clean_text(request.get("start_identity"))
    callback_names: list[str] = []
    matched_name = ""
    scanned_name_counts: dict[str, int] = {}

    def callback(detail: Any) -> bool:
        nonlocal matched_name
        name = _detail_name(detail)
        if not start_name:
            return True
        if name:
            scanned_name_counts[name] = scanned_name_counts.get(name, 0) + 1
        matched = _contact_name_matches(name, start_name)
        if matched:
            if not matched_name:
                matched_name = name
            if name:
                callback_names.append(name)
        return matched

    wx = WeChat()
    wx.SwitchToContact()
    _reset_contact_list_to_top(wx)
    kwargs: dict[str, Any] = {
        "n": count,
        "interval": 0,
        "speed": 5,
        "save_head_image": False,
        "callback": callback,
    }
    raw_result = list(wx.GetFriendDetails(**kwargs) or [])
    result: list[Any] = []
    raw_result_identities: list[str] = []
    result_identities: list[str] = []
    seen_result_identities: set[str] = set()
    for item in raw_result:
        identity = _detail_identity(item)
        if not identity:
            raise RuntimeError("通讯录返回了无法识别身份的联系人，拒绝合并本批数据")
        raw_result_identities.append(identity)
        if identity in seen_result_identities:
            continue
        seen_result_identities.add(identity)
        result.append(item)
        result_identities.append(identity)
    result_names = [_detail_name(item) for item in result]
    if start_name:
        if not result_names:
            raise RuntimeError("通讯录游标已命中，但没有返回锚点联系人")
        if result_names[0] != start_name:
            raise RuntimeError("通讯录游标返回位置与请求不一致，拒绝合并本批数据")
        if start_identity and (not result_identities or result_identities[0] != start_identity):
            raise RuntimeError("通讯录游标身份已变化，拒绝从旧位置继续")
    names_before_result = set(scanned_name_counts)
    if matched_name and scanned_name_counts.get(matched_name) == 1:
        names_before_result.discard(matched_name)
    identities_by_name: dict[str, set[str]] = {}
    for name, identity in zip(result_names, result_identities):
        identities_by_name.setdefault(name, set()).add(identity)
    cursor_candidates: list[dict[str, str]] = []
    for name, identity in reversed(list(zip(result_names, result_identities))):
        if not name or name in names_before_result:
            continue
        if len(identities_by_name.get(name) or ()) != 1:
            continue
        cursor = {"name": name, "identity": identity}
        if cursor in cursor_candidates:
            continue
        cursor_candidates.append(cursor)
        if len(cursor_candidates) >= 2:
            break
    if result and not cursor_candidates:
        raise RuntimeError("本批没有可安全定位的唯一联系人游标，拒绝猜测下一批位置")
    return {
        "ok": True,
        "result": result,
        "callback_names": callback_names,
        "matched_name": matched_name,
        "raw_result_identities": raw_result_identities,
        "result_identities": result_identities,
        "cursor_candidates": cursor_candidates,
        "raw_result_count": len(raw_result),
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--request", required=True)
    parser.add_argument("--output", required=True)
    args = parser.parse_args()

    started_at = time.perf_counter()
    try:
        request = json.loads(Path(args.request).read_text(encoding="utf-8"))
        payload = collect(request)
    except Exception as exc:
        payload = {
            "ok": False,
            "error": str(exc),
            "traceback": traceback.format_exc(),
        }
    payload["duration_seconds"] = round(time.perf_counter() - started_at, 3)
    _write_json(args.output, payload)
    return 0 if payload.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
