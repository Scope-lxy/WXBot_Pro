"""Read one bounded history snapshot and report controlled-message identities."""

from __future__ import annotations

import argparse
import json
import re
import time
from collections import defaultdict

from core.memory_context_repair import normalize_wechat_snapshot


def main():
    from wxautox4 import WeChat

    parser = argparse.ArgumentParser()
    parser.add_argument("--target", default="细妹小号")
    parser.add_argument("--count", type=int, default=50)
    parser.add_argument("--prefix", default="DEEP")
    parser.add_argument("--repeat", type=int, default=1)
    args = parser.parse_args()

    wx = WeChat(version="微信")
    wx.ChatWith(who=args.target, exact=True)
    info = dict(wx.ChatInfo() or {})
    if str(info.get("chat_name") or "").strip() != args.target:
        raise RuntimeError(f"历史读取目标不匹配：{info}")

    pattern = re.compile(rf"\b{re.escape(args.prefix)}-(\d{{2}})\b", re.IGNORECASE)
    rows = []
    runs = []
    for run_number in range(1, max(1, args.repeat) + 1):
        started = time.monotonic()
        raw = list(wx.GetHistoryMessage(
            max(1, args.count),
            interval=0.2,
            speed=5,
            goback=True,
        ) or [])
        elapsed = time.monotonic() - started
        entries = normalize_wechat_snapshot(raw, source="wechat_context_repair")
        run_labels = []
        for entry in entries:
            match = pattern.search(str(entry.get("content") or ""))
            if not match:
                continue
            label = int(match.group(1))
            run_labels.append(label)
            rows.append({
                "run": run_number,
                "label": label,
                "message_id": str(entry.get("message_id") or ""),
                "hash": str(entry.get("native_hash") or ""),
                "hash_text": str(entry.get("native_hash_text") or ""),
            })
        runs.append({
            "run": run_number,
            "elapsed_seconds": round(elapsed, 3),
            "raw_count": len(raw),
            "usable_count": len(entries),
            "labels": run_labels,
        })

    hashes_by_label = defaultdict(set)
    for row in rows:
        hashes_by_label[row["label"]].add((row["hash"], row["hash_text"]))
    ids_by_label = defaultdict(set)
    for row in rows:
        ids_by_label[row["label"]].add(row["message_id"])
    labels = [row["label"] for row in rows]
    unique_hashes = {
        (row["hash"], row["hash_text"])
        for row in rows
        if row["hash"] or row["hash_text"]
    }
    result = {
        "target": args.target,
        "requested": args.count,
        "runs": runs,
        "labels": labels,
        "unique_labels": sorted(set(labels)),
        "missing_1_to_35": sorted(set(range(1, 36)).difference(labels)),
        "duplicate_occurrences": len(labels) - len(set(labels)),
        "nonempty_message_ids": sum(1 for row in rows if row["message_id"]),
        "unique_message_ids": len({row["message_id"] for row in rows if row["message_id"]}),
        "unique_hash_pairs": len(unique_hashes),
        "labels_with_unstable_hash": sorted(
            label for label, hashes in hashes_by_label.items() if len(hashes) != 1
        ),
        "labels_with_unstable_message_id": sorted(
            label for label, values in ids_by_label.items() if len(values) != 1
        ),
        "labels_with_empty_hash": sorted(
            label
            for label, hashes in hashes_by_label.items()
            if any(not any(pair) for pair in hashes)
        ),
    }
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
