"""End-to-end live probe for GetNextNewMessage truncation and deep recovery."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sqlite3
import tempfile
import time
from collections import Counter, defaultdict
from pathlib import Path

from core.inbound_coordinator import InboundCoordinator, InboundEvent
from core.memory_context_repair import normalize_wechat_snapshot
from core.message_store import MessageStore


LABEL_PATTERN = re.compile(r"\bDEEP-(\d{2})\b", re.IGNORECASE)


def _labels(entries):
    labels = []
    for entry in entries:
        match = LABEL_PATTERN.search(str(entry.get("content") or ""))
        if match:
            labels.append(int(match.group(1)))
    return labels


def _native_id(entry):
    return str(entry.get("message_id") or "").strip()


def _session_snapshot(wx):
    return [
        {
            "name": str(getattr(item, "name", "") or "").strip(),
            "isnew": bool(getattr(item, "isnew", False)),
            "new_count": int(getattr(item, "new_count", 0) or 0),
            "ismute": bool(getattr(item, "ismute", False)),
        }
        for item in (wx.GetSession() or [])
    ]


def _event_from_entry(conversation, entry, *, batch, order, observed_at):
    return InboundEvent(
        conversation=conversation,
        chat_type="private",
        content=str(entry.get("content") or ""),
        original_content=str(entry.get("content") or ""),
        received_at=observed_at + order / 1000.0,
        source="global",
        source_batch=batch,
        source_order=order,
        message_type=str(entry.get("type") or "text"),
        sender=str(entry.get("sender") or conversation),
        native_attr=str(entry.get("attr") or "friend"),
        native_id=_native_id(entry),
        native_hash=str(entry.get("native_hash") or ""),
        native_hash_text=str(entry.get("native_hash_text") or ""),
        native_time=str(entry.get("time") or ""),
    )


def _history_event(conversation, entry, *, index, observed_at):
    native_id = _native_id(entry)
    identity = native_id or json.dumps(
        [
            conversation,
            entry.get("attr"),
            entry.get("sender"),
            entry.get("type"),
            entry.get("content"),
            entry.get("time"),
            index,
        ],
        ensure_ascii=False,
        separators=(",", ":"),
    )
    event_id = "evt_deep_probe_" + hashlib.sha256(identity.encode("utf-8")).hexdigest()
    attr = str(entry.get("attr") or "").strip().lower()
    return {
        "event_id": event_id,
        "conversation": conversation,
        "chat_type": "private",
        "received_at": observed_at + index / 1000.0,
        "direction": "manual_self" if attr == "self" else "friend",
        "sender": str(entry.get("sender") or ""),
        "content": str(entry.get("content") or ""),
        "original_content": str(entry.get("content") or ""),
        "message_type": str(entry.get("type") or "text"),
        "native_attr": attr,
        "native_id": native_id,
        "native_hash": str(entry.get("native_hash") or ""),
        "native_hash_text": str(entry.get("native_hash_text") or ""),
        "native_time": str(entry.get("time") or ""),
        "metadata": {"context_repair": True, "deep_probe": True},
    }


def _ordered_union(sources):
    entries_by_id = {}
    rank = {}
    edges = defaultdict(set)
    indegree = defaultdict(int)
    next_rank = 0
    source_ids = []

    for entries in sources:
        ids = []
        for entry in entries:
            native_id = _native_id(entry)
            if not native_id:
                continue
            if native_id not in entries_by_id:
                entries_by_id[native_id] = entry
                rank[native_id] = next_rank
                next_rank += 1
            ids.append(native_id)
        source_ids.append(ids)
        for left, right in zip(ids, ids[1:]):
            if left == right or right in edges[left]:
                continue
            edges[left].add(right)
            indegree[right] += 1
            indegree.setdefault(left, indegree.get(left, 0))

    ready = sorted(
        (native_id for native_id in entries_by_id if indegree[native_id] == 0),
        key=rank.get,
    )
    ordered_ids = []
    while ready:
        native_id = ready.pop(0)
        ordered_ids.append(native_id)
        for successor in sorted(edges[native_id], key=rank.get):
            indegree[successor] -= 1
            if indegree[successor] == 0:
                ready.append(successor)
                ready.sort(key=rank.get)

    cycle = len(ordered_ids) != len(entries_by_id)
    if cycle:
        ordered_ids = sorted(entries_by_id, key=rank.get)
    overlaps = []
    known = set()
    for ids in source_ids:
        overlaps.append(len(known.intersection(ids)))
        known.update(ids)
    return [entries_by_id[native_id] for native_id in ordered_ids], cycle, overlaps


def _sqlite_probe(conversation, scan_entries, ordered_entries):
    scan_ids = {_native_id(entry) for entry in scan_entries if _native_id(entry)}
    deep_missing = [entry for entry in ordered_entries if _native_id(entry) not in scan_ids]
    observed_at = time.time() - max(60, len(ordered_entries))

    with tempfile.TemporaryDirectory(prefix="wxbot-deep-unread-") as temp_dir:
        store = MessageStore(Path(temp_dir), "probe-account")
        coordinator = InboundCoordinator(store)
        accepted = []
        for index, entry in enumerate(scan_entries):
            accepted.append(coordinator.accept(_event_from_entry(
                conversation,
                entry,
                batch="controlled-get-next",
                order=index,
                observed_at=observed_at + len(deep_missing),
            )))

        deep_events = [
            _history_event(conversation, entry, index=index, observed_at=observed_at)
            for index, entry in enumerate(deep_missing)
        ]
        first_added = store.append_history(deep_events)
        second_added = store.append_history(deep_events)

        connection = sqlite3.connect(store.path)
        try:
            connection.row_factory = sqlite3.Row
            rows = [
                dict(row)
                for row in connection.execute(
                    """
                    SELECT native_id, content, source, processing_state,
                           reply_expires_at, conversation_version
                    FROM chat_events
                    WHERE conversation = ? AND chat_type = 'private'
                    ORDER BY received_at, event_seq
                    """,
                    (conversation,),
                ).fetchall()
            ]
        finally:
            connection.close()

        controlled_rows = [row for row in rows if LABEL_PATTERN.search(str(row["content"] or ""))]
        labels = [int(LABEL_PATTERN.search(row["content"]).group(1)) for row in controlled_rows]
        native_ids = [str(row["native_id"] or "") for row in controlled_rows]
        history_rows = [row for row in controlled_rows if row["source"] == "history_import"]
        return {
            "scan_new_count": sum(1 for item in accepted if item.is_new),
            "deep_missing_count": len(deep_missing),
            "deep_first_added": int(first_added),
            "deep_same_batch_second_added": int(second_added),
            "final_controlled_row_count": len(controlled_rows),
            "final_controlled_labels": labels,
            "final_duplicate_label_count": len(labels) - len(set(labels)),
            "final_duplicate_native_id_count": len(native_ids) - len(set(native_ids)),
            "deep_rows_all_handled": all(row["processing_state"] == "handled" for row in history_rows),
            "deep_rows_have_no_reply_ttl": all(row["reply_expires_at"] is None for row in history_rows),
        }


def run_probe(target, expected_count, page_size, max_pages):
    from wxautox4 import WeChat

    wx = WeChat(version="微信")
    before = _session_snapshot(wx)
    unread = [item for item in before if item["isnew"]]
    if unread != [{
        "name": target,
        "isnew": True,
        "new_count": expected_count,
        "ismute": False,
    }]:
        raise RuntimeError(f"未读前置条件不满足：{unread}")
    if any(str(getattr(chat, "who", "") or "").strip() == target for chat in (wx.GetAllSubWindow() or [])):
        raise RuntimeError(f"测试目标 {target} 已存在子窗口")

    started = time.monotonic()
    result = wx.GetNextNewMessage(filter_mute=False)
    scan_elapsed = time.monotonic() - started
    result = result if isinstance(result, dict) else {}
    actual_target = str(result.get("chat_name") or "").strip()
    if actual_target != target:
        raise RuntimeError(f"GetNextNewMessage 返回了其他会话：{actual_target}")
    scan_entries = normalize_wechat_snapshot(result.get("msg") or [], source="global")

    pages = []
    page_results = []
    found_labels = set(_labels(scan_entries))
    for page_number in range(1, max_pages + 1):
        started = time.monotonic()
        raw = list(wx.GetHistoryMessage(page_size, interval=0.2, speed=5, goback=True) or [])
        elapsed = time.monotonic() - started
        entries = normalize_wechat_snapshot(raw, source="wechat_context_repair")
        pages.append(entries)
        labels = _labels(entries)
        found_labels.update(labels)
        page_results.append({
            "page": page_number,
            "requested": page_size,
            "raw_count": len(raw),
            "usable_count": len(entries),
            "elapsed_seconds": round(elapsed, 3),
            "controlled_labels": labels,
            "native_id_count": sum(1 for entry in entries if _native_id(entry)),
        })
        if found_labels.issuperset(range(1, expected_count + 1)):
            break

    ordered, cycle, overlaps = _ordered_union([*reversed(pages), scan_entries])
    all_entries_by_id = {}
    for entry in [*scan_entries, *(entry for page in pages for entry in page)]:
        native_id = _native_id(entry)
        if native_id:
            all_entries_by_id.setdefault(native_id, entry)
    controlled_unique = [
        entry for entry in all_entries_by_id.values()
        if LABEL_PATTERN.search(str(entry.get("content") or ""))
    ]
    controlled_labels = _labels(controlled_unique)
    counts = Counter(controlled_labels)
    expected = set(range(1, expected_count + 1))
    after = _session_snapshot(wx)

    return {
        "target": target,
        "unread_before": unread,
        "scan_elapsed_seconds": round(scan_elapsed, 3),
        "scan_raw_count": len(result.get("msg") or []),
        "scan_usable_count": len(scan_entries),
        "scan_controlled_labels": _labels(scan_entries),
        "scan_native_id_count": sum(1 for entry in scan_entries if _native_id(entry)),
        "history_pages": page_results,
        "source_overlap_counts": overlaps,
        "order_graph_has_cycle": cycle,
        "ordered_controlled_labels": _labels(ordered),
        "unique_controlled_labels": sorted(counts),
        "missing_controlled_labels": sorted(expected.difference(counts)),
        "duplicate_controlled_labels": sorted(label for label, count in counts.items() if count > 1),
        "unique_controlled_count": len(controlled_unique),
        "unread_after": [item for item in after if item["isnew"]],
        "sqlite_probe": _sqlite_probe(target, scan_entries, ordered),
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--target", default="细妹小号")
    parser.add_argument("--expected-count", type=int, default=35)
    parser.add_argument("--page-size", type=int, default=30)
    parser.add_argument("--max-pages", type=int, default=4)
    parser.add_argument("--output", default="")
    args = parser.parse_args()

    result = run_probe(
        str(args.target or "").strip(),
        max(1, args.expected_count),
        max(1, args.page_size),
        max(1, args.max_pages),
    )
    payload = json.dumps(result, ensure_ascii=False, indent=2)
    print(payload)
    if args.output:
        path = Path(args.output)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(payload + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
