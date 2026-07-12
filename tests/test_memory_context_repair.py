import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from core.memory import MemoryManager
from core.memory_context_repair import (
    build_repair_plan,
    current_message_found_near_tail,
    filter_model_repair_messages,
    normalize_wechat_snapshot,
    snapshot_messages_through_current,
)
from wxbot_core import WXBot


def msg(
    content,
    *,
    attr="friend",
    sender="张三",
    msg_type="text",
    time="2026/07/03 05:00:00",
    message_id="",
    message_hash="",
    message_hash_text="",
):
    return SimpleNamespace(
        attr=attr,
        sender=sender,
        type=msg_type,
        content=content,
        time=time,
        id=message_id,
        hash=message_hash,
        hash_text=message_hash_text,
    )


class MemoryContextRepairCoreTests(unittest.TestCase):
    def test_build_repair_plan_appends_after_anchor(self):
        local = [
            {"time": "1", "attr": "friend", "sender": "张三", "type": "text", "content": "早"},
            {"time": "2", "attr": "self", "sender": "self", "type": "text", "content": "早呀"},
        ]
        remote = local + [
            {"time": "3", "attr": "friend", "sender": "张三", "type": "text", "content": "今天不舒服"},
            {"time": "4", "attr": "self", "sender": "self", "type": "text", "content": "那多休息"},
        ]

        plan = build_repair_plan(local, remote, anchor_recent_count=5)

        self.assertTrue(plan.anchor_found)
        self.assertEqual([item["content"] for item in plan.messages_to_append], ["今天不舒服", "那多休息"])

    def test_build_repair_plan_fills_missing_messages_before_tail_anchor(self):
        local = [
            {"time": "1", "attr": "friend", "sender": "张三", "type": "text", "content": "早"},
            {"time": "4", "attr": "friend", "sender": "张三", "type": "text", "content": "后来呢"},
        ]
        remote = [
            {"time": "1", "attr": "friend", "sender": "张三", "type": "text", "content": "早"},
            {"time": "2", "attr": "self", "sender": "self", "type": "text", "content": "手机发的第一条"},
            {"time": "3", "attr": "self", "sender": "self", "type": "text", "content": "手机发的第二条"},
            {"time": "4", "attr": "friend", "sender": "张三", "type": "text", "content": "后来呢"},
        ]

        plan = build_repair_plan(local, remote, anchor_recent_count=5)

        self.assertTrue(plan.anchor_found)
        self.assertEqual(
            [item["content"] for item in plan.messages_to_append],
            ["手机发的第一条", "手机发的第二条"],
        )

    def test_repair_plan_fills_older_gap_before_current_duplicate_images(self):
        local = [
            {"time": "2026/07/12 01:00:00", "attr": "self", "sender": "self", "type": "text", "content": "旧锚点"},
            {"time": "2026/07/12 01:03:00", "attr": "friend", "sender": "张三", "type": "image", "content": "[图片]"},
            {"time": "2026/07/12 01:04:00", "attr": "friend", "sender": "张三", "type": "image", "content": "[图片]"},
        ]
        remote = [
            {"time": "2026/07/12 01:00:00", "attr": "self", "sender": "me", "type": "text", "content": "旧锚点"},
            {"time": "2026/07/12 01:01:00", "attr": "friend", "sender": "张三", "type": "text", "content": "较早漏记消息"},
            {"time": "2026/07/12 01:02:00", "attr": "self", "sender": "me", "type": "text", "content": "较早人工回复"},
            {"time": "2026/07/12 01:03:00", "attr": "friend", "sender": "张三", "type": "image", "content": "[图片]"},
            {"time": "2026/07/12 01:04:00", "attr": "friend", "sender": "张三", "type": "image", "content": "[图片]"},
        ]

        plan = build_repair_plan(local, remote, anchor_recent_count=5)

        self.assertTrue(plan.anchor_found)
        self.assertEqual(plan.anchor_index, 4)
        self.assertEqual(
            [item["content"] for item in plan.messages_to_append],
            ["较早漏记消息", "较早人工回复"],
        )

    def test_anchor_matching_tolerates_self_sender_format_mismatch(self):
        local = [
            {"time": "2026/07/04 08:00:00", "attr": "friend", "sender": "张三", "type": "text", "content": "早"},
            {"time": "2026/07/04 08:01:00", "attr": "self", "sender": "self", "type": "text", "content": "早呀"},
        ]
        remote = [
            {"time": "2026/07/04 08:00:00", "attr": "friend", "sender": "张三", "type": "text", "content": "早"},
            {"time": "2026/07/04 08:01:02", "attr": "self", "sender": "me", "type": "text", "content": "早呀"},
            {"time": "2026/07/04 08:02:00", "attr": "friend", "sender": "张三", "type": "text", "content": "那就好"},
        ]

        plan = build_repair_plan(local, remote, anchor_recent_count=5)

        self.assertTrue(plan.anchor_found)
        self.assertEqual([item["content"] for item in plan.messages_to_append], ["那就好"])

    def test_anchor_matching_skips_voice_messages_as_unstable_anchors(self):
        local = [
            {"time": "2026/07/04 08:00:00", "attr": "friend", "sender": "张三", "type": "text", "content": "第一句"},
            {"time": "2026/07/04 08:01:00", "attr": "self", "sender": "self", "type": "text", "content": "第二句"},
            {"time": "2026/07/04 08:02:00", "attr": "friend", "sender": "张三", "type": "voice", "content": "微信转写内容一"},
            {"time": "2026/07/04 08:03:00", "attr": "self", "sender": "self", "type": "voice", "content": "微信转写内容二"},
            {"time": "2026/07/04 08:04:00", "attr": "friend", "sender": "张三", "type": "voice", "content": "微信转写内容三"},
            {"time": "2026/07/04 08:05:00", "attr": "self", "sender": "self", "type": "voice", "content": "微信转写内容四"},
            {"time": "2026/07/04 08:06:00", "attr": "friend", "sender": "张三", "type": "voice", "content": "微信转写内容五"},
        ]
        remote = [
            {"time": "2026/07/04 08:00:00", "attr": "friend", "sender": "张三", "type": "text", "content": "第一句"},
            {"time": "2026/07/04 08:01:02", "attr": "self", "sender": "me", "type": "text", "content": "第二句"},
            {"time": "2026/07/04 08:02:00", "attr": "friend", "sender": "张三", "type": "voice", "content": "一条语音消息（未识别出文字）"},
            {"time": "2026/07/04 08:03:00", "attr": "self", "sender": "me", "type": "voice", "content": "一条语音消息（未识别出文字）"},
            {"time": "2026/07/04 08:04:00", "attr": "friend", "sender": "张三", "type": "voice", "content": "一条语音消息（未识别出文字）"},
            {"time": "2026/07/04 08:05:00", "attr": "self", "sender": "me", "type": "voice", "content": "一条语音消息（未识别出文字）"},
            {"time": "2026/07/04 08:06:00", "attr": "friend", "sender": "张三", "type": "voice", "content": "一条语音消息（未识别出文字）"},
            {"time": "2026/07/04 08:07:00", "attr": "friend", "sender": "张三", "type": "text", "content": "新内容"},
        ]

        plan = build_repair_plan(local, remote, anchor_recent_count=5)

        self.assertTrue(plan.anchor_found)
        self.assertEqual([item["content"] for item in plan.messages_to_append], ["新内容"])

    def test_build_repair_plan_without_anchor_uses_visible_snapshot_as_new_tail(self):
        local = [
            {
                "time": "2026/07/04 17:12:31",
                "attr": "self",
                "sender": "self",
                "type": "voice",
                "content": "梅姐，你赢的钱自己留着用，我有饭吃的。",
            },
        ]
        remote = [
            {
                "time": "2026/07/04 17:12:00",
                "attr": "self",
                "sender": "me",
                "type": "voice",
                "content": "一条语音消息（未识别出文字）",
            },
            {
                "time": "2026/07/04 17:12:36",
                "attr": "friend",
                "sender": "张三",
                "type": "voice",
                "content": "你真好。",
            },
        ]

        plan = build_repair_plan(local, remote, anchor_recent_count=5)

        self.assertFalse(plan.anchor_found)
        self.assertEqual([item["content"] for item in plan.messages_to_append], ["你真好。"])

    def test_build_repair_plan_without_anchor_initializes_empty_history(self):
        remote = [
            {"time": "2026/07/04 17:01:00", "attr": "friend", "sender": "张三", "type": "text", "content": "第一条"},
            {"time": "2026/07/04 17:02:00", "attr": "self", "sender": "self", "type": "text", "content": "第二条"},
        ]

        plan = build_repair_plan([], remote, anchor_recent_count=5)

        self.assertFalse(plan.anchor_found)
        self.assertEqual([item["content"] for item in plan.messages_to_append], ["第一条", "第二条"])

    def test_filter_repair_messages_skips_duration_only_voice_placeholder(self):
        messages = [
            {"type": "text", "attr": "friend", "sender": "张三", "content": "前一句"},
            {"type": "voice", "attr": "friend", "sender": "张三", "content": '语音8"秒'},
            {"type": "voice", "attr": "friend", "sender": "张三", "content": '语音1"秒语音未能转换'},
            {"type": "voice", "attr": "friend", "sender": "张三", "content": '语音8"秒我刚说的是这个'},
        ]

        kept = filter_model_repair_messages(messages)

        self.assertEqual([item["content"] for item in kept], ["前一句", '语音8"秒我刚说的是这个'])

    def test_same_content_with_nearby_time_is_already_present(self):
        local = [
            {
                "time": "2026/07/04 17:00:00",
                "attr": "friend",
                "sender": "张三",
                "type": "text",
                "content": "你在吗",
            },
        ]
        remote = [
            {
                "time": "2026/07/04 17:05:00",
                "attr": "friend",
                "sender": "张三",
                "type": "text",
                "content": "你在吗",
            },
        ]

        plan = build_repair_plan(local, remote, anchor_recent_count=5)

        self.assertTrue(plan.anchor_found)
        self.assertEqual(plan.messages_to_append, [])

    def test_repeated_text_without_sequence_anchor_still_repairs_missing_occurrences(self):
        local = [
            {"time": "2026/07/04 17:01:00", "attr": "friend", "sender": "张三", "type": "text", "content": "好"},
        ]
        remote = [
            {"time": "2026/07/04 17:00:00", "attr": "friend", "sender": "张三", "type": "text", "content": "好"},
            {"time": "2026/07/04 17:01:00", "attr": "friend", "sender": "张三", "type": "text", "content": "好"},
            {"time": "2026/07/04 17:02:00", "attr": "friend", "sender": "张三", "type": "text", "content": "下一条"},
        ]

        plan = build_repair_plan(local, remote, anchor_recent_count=5)

        self.assertFalse(plan.anchor_found)
        self.assertEqual([item["content"] for item in plan.messages_to_append], ["好", "下一条"])

    def test_voice_identity_is_deduped_without_using_voice_as_anchor(self):
        local = [
            {"time": "2026/07/04 17:01:00", "attr": "friend", "sender": "张三", "type": "voice", "content": "微信转写的完整语音内容。"},
        ]
        remote = [
            {"time": "2026/07/04 17:01:00", "attr": "friend", "sender": "张三", "type": "voice", "content": "微信转写的完整语音内容。"},
            {"time": "2026/07/04 17:02:00", "attr": "friend", "sender": "张三", "type": "text", "content": "下一条"},
        ]

        plan = build_repair_plan(local, remote, anchor_recent_count=5)

        self.assertFalse(plan.anchor_found)
        self.assertEqual([item["content"] for item in plan.messages_to_append], ["下一条"])

    def test_runtime_native_message_id_is_not_used_as_persistent_identity(self):
        local = [
            {
                "time": "2026/07/12 01:39:33",
                "attr": "friend",
                "sender": "张三",
                "type": "text",
                "content": "你好",
                "message_id": "native-current",
            },
        ]
        remote = [{"time": "2026/07/12 01:39:00", "attr": "friend", "sender": "张三", "type": "text", "content": "你好", "message_id": "different-runtime-id"}]

        plan = build_repair_plan(local, remote, anchor_recent_count=5)

        self.assertTrue(plan.anchor_found)
        self.assertEqual(plan.messages_to_append, [])

    def test_visible_snapshot_keeps_identical_image_occurrences_without_native_ids(self):
        remote = [
            {"time": "2026/07/12 01:39:00", "attr": "friend", "sender": "张三", "type": "image", "content": "[图片]"},
            {"time": "2026/07/12 01:39:00", "attr": "friend", "sender": "张三", "type": "image", "content": "[图片]"},
        ]

        plan = build_repair_plan([], remote, anchor_recent_count=5)

        self.assertEqual([item["content"] for item in plan.messages_to_append], ["[图片]", "[图片]"])

    def test_snapshot_messages_inherit_preceding_wechat_time_marker(self):
        snapshot = [
            msg("01:17", attr="system", sender="system", msg_type="time", time="2026-07-12 01:17:00"),
            msg("测试一下", time="", message_id="text-1"),
            msg("01:39", attr="system", sender="system", msg_type="time", time="2026-07-12 01:39:00"),
            msg("你好", time="", message_id="text-2"),
            msg("这么晚还没睡呀～", attr="self", sender="self", time="", message_id="text-3"),
        ]

        entries = normalize_wechat_snapshot(snapshot)

        self.assertEqual(
            [(item["content"], item["time"]) for item in entries],
            [
                ("测试一下", "2026-07-12 01:17:00"),
                ("你好", "2026-07-12 01:39:00"),
                ("这么晚还没睡呀～", "2026-07-12 01:39:00"),
            ],
        )

    def test_snapshot_without_time_marker_ends_at_current_trigger_time(self):
        entries = normalize_wechat_snapshot(
            [
                msg("更早人工回复", attr="self", sender="self", time=""),
                msg("更早消息", time=""),
                msg("当前触发", time=""),
            ],
            fallback_tail_time="2026/07/12 01:39:33",
        )

        self.assertEqual(
            [(item["content"], item["time"]) for item in entries],
            [
                ("更早人工回复", "2026/07/12 01:39:31"),
                ("更早消息", "2026/07/12 01:39:32"),
                ("当前触发", "2026/07/12 01:39:33"),
            ],
        )
        self.assertTrue(all(item.get("time_inferred") is True for item in entries))

    def test_inferred_leading_time_does_not_duplicate_existing_visible_message(self):
        local = [
            {
                "time": "2026/07/11 23:35:30",
                "attr": "self",
                "sender": "self",
                "type": "text",
                "content": "晚上好呀～刚看到你发的照片，是在车上吗？",
            },
        ]
        remote = normalize_wechat_snapshot([
            msg("晚上好呀～刚看到你发的照片，是在车上吗？", attr="self", sender="self", time=""),
            msg("01:17", attr="system", sender="system", msg_type="time", time="2026-07-12 01:17:00"),
            msg("测试一下", time=""),
        ])

        plan = build_repair_plan(local, remote, anchor_recent_count=5)

        self.assertEqual([item["content"] for item in plan.messages_to_append], ["测试一下"])

    def test_snapshot_stops_at_current_trigger_and_leaves_later_message_for_its_callback(self):
        current = msg("当前触发", time="", message_id="current-runtime")
        snapshot = [
            msg("更早消息", time=""),
            current,
            msg("当前触发", time="", message_id="later-runtime"),
        ]

        trimmed = snapshot_messages_through_current(snapshot, current)

        self.assertEqual([item.content for item in trimmed], ["更早消息", "当前触发"])

    def test_snapshot_uses_hash_text_before_same_content_fallback(self):
        current = msg("相同内容", time="", message_hash_text="current-row")
        snapshot = [
            current,
            msg("相同内容", time="", message_hash_text="later-row"),
        ]

        trimmed = snapshot_messages_through_current(snapshot, current)

        self.assertEqual(trimmed, [current])

    def test_group_anchor_matching_keeps_sender_identity(self):
        local = [
            {"time": "2026/07/04 08:00:00", "attr": "group", "sender": "A", "type": "text", "content": "收到"},
            {"time": "2026/07/04 08:01:00", "attr": "group", "sender": "B", "type": "text", "content": "收到"},
        ]
        remote = [
            {"time": "2026/07/04 08:00:00", "attr": "friend", "sender": "A", "type": "text", "content": "收到"},
            {"time": "2026/07/04 08:01:00", "attr": "friend", "sender": "B", "type": "text", "content": "收到"},
            {"time": "2026/07/04 08:02:00", "attr": "friend", "sender": "C", "type": "text", "content": "新内容"},
        ]

        plan = build_repair_plan(local, remote, anchor_recent_count=5, chat_type="group")

        self.assertTrue(plan.anchor_found)
        self.assertEqual([(item["sender"], item["content"]) for item in plan.messages_to_append], [("C", "新内容")])

    def test_group_repair_without_anchor_uses_visible_snapshot(self):
        local = [
            {
                "time": "2026/07/04 17:00:00",
                "attr": "group",
                "sender": "A",
                "type": "text",
                "content": "好的",
            },
        ]
        remote = [
            {
                "time": "2026/07/04 17:01:00",
                "attr": "friend",
                "sender": "B",
                "type": "text",
                "content": "好的",
            },
        ]

        plan = build_repair_plan(local, remote, anchor_recent_count=5, chat_type="group")

        self.assertFalse(plan.anchor_found)
        self.assertEqual([(item["sender"], item["content"]) for item in plan.messages_to_append], [("B", "好的")])

    def test_repeated_short_text_repairs_only_missing_occurrence(self):
        local = [
            {"time": "2026/07/12 01:00:00", "attr": "friend", "sender": "张三", "type": "text", "content": "好"},
        ]
        remote = [
            {"time": "2026/07/12 01:00:00", "attr": "friend", "sender": "张三", "type": "text", "content": "好"},
            {"time": "2026/07/12 01:01:00", "attr": "friend", "sender": "张三", "type": "text", "content": "好"},
        ]

        plan = build_repair_plan(local, remote, anchor_recent_count=5)

        self.assertFalse(plan.anchor_found)
        self.assertEqual([item["content"] for item in plan.messages_to_append], ["好"])

    def test_current_message_found_near_tail(self):
        local = [
            {"time": "1", "attr": "self", "sender": "self", "type": "text", "content": "好"},
            {"time": "2", "attr": "friend", "sender": "张三", "type": "text", "content": "来了"},
        ]

        self.assertTrue(current_message_found_near_tail(local, msg("来了", time="2")))
        self.assertFalse(current_message_found_near_tail(local, msg("新消息", time="3")))

    def test_current_message_found_near_tail_accepts_merged_source_messages(self):
        local = [
            {"time": "1", "attr": "friend", "sender": "张三", "type": "text", "content": "第一条"},
            {"time": "2", "attr": "friend", "sender": "张三", "type": "text", "content": "第二条"},
        ]
        merged = SimpleNamespace(
            type="text",
            attr="friend",
            sender="张三",
            content="第一条\n第二条",
            _merged_source_messages=[
                msg("第一条", time="1"),
                msg("第二条", time="2"),
            ],
        )

        self.assertTrue(current_message_found_near_tail(local, merged))

    def test_merged_identical_sources_cannot_reuse_one_local_match(self):
        local = [
            {"time": "1", "attr": "friend", "sender": "张三", "type": "text", "content": "好"},
        ]
        merged = SimpleNamespace(
            type="text",
            attr="friend",
            sender="张三",
            content="好\n好",
            _merged_source_messages=[msg("好", time="1"), msg("好", time="1")],
        )

        self.assertFalse(current_message_found_near_tail(local, merged))


class MemoryManagerContextRepairTests(unittest.TestCase):
    def test_save_message_keeps_repeated_text_without_explicit_time(self):
        with tempfile.TemporaryDirectory() as tmp:
            manager = MemoryManager("wxid", tmp)

            manager.save_message("张三", "张三", "好", "text", "friend", 100)
            manager.save_message("张三", "张三", "好", "text", "friend", 100)

            self.assertEqual([item["content"] for item in manager.get_messages("张三", 10)], ["好", "好"])

    def test_save_message_keeps_repeated_content_with_same_message_time(self):
        with tempfile.TemporaryDirectory() as tmp:
            manager = MemoryManager("wxid", tmp)

            manager.save_message("张三", "张三", "好", "text", "friend", 100, message_time="2026/07/03 05:00:00")
            manager.save_message("张三", "张三", "好", "text", "friend", 100, message_time="2026/07/03 05:00:00")

            self.assertEqual([item["content"] for item in manager.get_messages("张三", 10)], ["好", "好"])

    def test_visible_snapshot_reconciliation_is_idempotent_and_preserves_occurrences(self):
        with tempfile.TemporaryDirectory() as tmp:
            manager = MemoryManager("wxid", tmp)

            first = manager.append_missing_messages(
                "张三",
                [
                    {"time": "2026/07/12 01:39:00", "attr": "friend", "sender": "张三", "type": "image", "content": "[图片]"},
                    {"time": "2026/07/12 01:39:00", "attr": "friend", "sender": "张三", "type": "image", "content": "[图片]"},
                ],
                100,
                reconcile_visible_snapshot=True,
            )
            second = manager.append_missing_messages(
                "张三",
                [
                    {"time": "2026/07/12 01:39:00", "attr": "friend", "sender": "张三", "type": "image", "content": "[图片]"},
                    {"time": "2026/07/12 01:39:00", "attr": "friend", "sender": "张三", "type": "image", "content": "[图片]"},
                ],
                100,
                reconcile_visible_snapshot=True,
            )

            stored = manager.get_messages("张三", 10)
            self.assertEqual(first["added"], 2)
            self.assertEqual(second["added"], 0)
            self.assertEqual([item["content"] for item in stored], ["[图片]", "[图片]"])

    def test_append_missing_messages_dedupes_by_time_and_fingerprint(self):
        with tempfile.TemporaryDirectory() as tmp:
            manager = MemoryManager("wxid", tmp)
            manager.save_message("张三", "张三", "早", "text", "friend", 100, message_time="2026/07/03 05:00:00")

            result = manager.append_missing_messages(
                "张三",
                [
                    {"time": "2026/07/03 05:00:00", "attr": "friend", "sender": "张三", "type": "text", "content": "早"},
                    {"time": "2026/07/03 05:01:00", "attr": "self", "sender": "self", "type": "text", "content": "早呀"},
                ],
                100,
            )

            self.assertEqual(result["added"], 1)
            self.assertEqual([item["content"] for item in manager.get_messages("张三", 10)], ["早", "早呀"])

    def test_append_missing_messages_sorts_entries_by_time(self):
        with tempfile.TemporaryDirectory() as tmp:
            manager = MemoryManager("wxid", tmp)

            result = manager.append_missing_messages(
                "张三",
                [
                    {"time": "2026/07/03 05:02:00", "attr": "friend", "sender": "张三", "type": "text", "content": "第三条"},
                    {"time": "2026/07/03 05:01:00", "attr": "self", "sender": "self", "type": "text", "content": "第二条"},
                    {"time": "2026/07/03 05:00:00", "attr": "friend", "sender": "张三", "type": "text", "content": "第一条"},
                ],
                100,
            )

            self.assertEqual(result["added"], 3)
            self.assertEqual(
                [item["content"] for item in manager.get_messages("张三", 10)],
                ["第一条", "第二条", "第三条"],
            )

    def test_append_missing_messages_preserves_snapshot_order_with_same_marker_time(self):
        with tempfile.TemporaryDirectory() as tmp:
            manager = MemoryManager("wxid", tmp)

            manager.append_missing_messages(
                "张三",
                [
                    {"time": "2026-07-12 01:39:00", "attr": "friend", "sender": "张三", "type": "text", "content": "你好", "message_id": "text-1"},
                    {"time": "2026-07-12 01:39:00", "attr": "self", "sender": "self", "type": "text", "content": "第一句回复", "message_id": "text-2"},
                    {"time": "2026-07-12 01:39:00", "attr": "self", "sender": "self", "type": "text", "content": "第二句回复", "message_id": "text-3"},
                ],
                100,
            )

            self.assertEqual(
                [item["content"] for item in manager.get_messages("张三", 10)],
                ["你好", "第一句回复", "第二句回复"],
            )

    def test_dash_snapshot_times_insert_stopped_messages_before_current_trigger(self):
        with tempfile.TemporaryDirectory() as tmp:
            manager = MemoryManager("wxid", tmp)
            manager.save_message(
                "张三",
                "张三",
                "停机补洞最终触发",
                "text",
                "friend",
                100,
                message_time="2026/07/12 03:53:17",
            )
            snapshot = [
                {"time": "2026-07-12 03:39:00", "attr": "friend", "sender": "张三", "type": "text", "content": "停机补洞同文测试"},
                {"time": "2026-07-12 03:39:00", "attr": "friend", "sender": "张三", "type": "text", "content": "停机补洞同文测试"},
                {"time": "2026-07-12 03:39:00", "attr": "self", "sender": "self", "type": "text", "content": "停机人工回复测试"},
                {"time": "2026-07-12 03:53:17", "attr": "friend", "sender": "张三", "type": "text", "content": "停机补洞最终触发"},
            ]

            first = manager.append_missing_messages(
                "张三",
                snapshot,
                100,
                reconcile_visible_snapshot=True,
            )
            second = manager.append_missing_messages(
                "张三",
                snapshot,
                100,
                reconcile_visible_snapshot=True,
            )

            stored = manager.get_messages("张三", 10)
            self.assertEqual(first["added"], 3)
            self.assertEqual(second["added"], 0)
            self.assertEqual(
                [item["content"] for item in stored],
                [
                    "停机补洞同文测试",
                    "停机补洞同文测试",
                    "停机人工回复测试",
                    "停机补洞最终触发",
                ],
            )
            self.assertTrue(all("-" not in item["time"][:10] for item in stored))

    def test_inferred_time_marker_is_not_persisted_in_memory(self):
        with tempfile.TemporaryDirectory() as tmp:
            manager = MemoryManager("wxid", tmp)

            manager.append_missing_messages(
                "张三",
                [
                    {
                        "time": "2026-07-12 01:16:59",
                        "time_inferred": True,
                        "attr": "friend",
                        "sender": "张三",
                        "type": "text",
                        "content": "新消息",
                    },
                ],
                100,
                reconcile_visible_snapshot=True,
            )

            stored = manager.get_messages("张三", 10)
            self.assertEqual(stored[0]["time"], "2026/07/12 01:16:59")
            self.assertNotIn("time_inferred", stored[0])


class FakeLock:
    def __init__(self, acquired=True):
        self.acquired = acquired
        self.acquire_calls = []
        self.released = False

    def acquire(self, blocking=True):
        self.acquire_calls.append(blocking)
        return self.acquired

    def release(self):
        self.released = True


class FakeChat:
    who = "张三"
    chat_type = "private"

    def __init__(self, visible=None, history=None):
        self.visible = visible or []
        self.history = history or []
        self.get_all_calls = 0
        self.ChatBox = SimpleNamespace(get_msgs_from_history=self.get_msgs_from_history)

    def GetAllMessage(self):
        self.get_all_calls += 1
        return self.visible

    def get_msgs_from_history(self, limit, callback=None, interval=0.2, speed=5, goback=True):
        self.history_args = {
            "limit": limit,
            "interval": interval,
            "speed": speed,
            "goback": goback,
        }
        return self.history[:limit]


class WXBotContextRepairTests(unittest.TestCase):
    def make_bot(self, tmp, *, lock=None, memory_context_count=50):
        bot = WXBot.__new__(WXBot)
        bot.config = SimpleNamespace(
            memory_switch=True,
            memory_context_switch=True,
            memory_max_count=100,
            memory_context_count=memory_context_count,
        )
        bot.memory_manager = MemoryManager("wxid", tmp)
        bot._mark_chat_memory_dirty = lambda *args, **kwargs: None
        bot.is_stop_requested = lambda: False
        bot._wechat_action_lock = lock or FakeLock()
        bot._get_wechat_action_lock = lambda: bot._wechat_action_lock
        bot._incoming_seen_lock = None
        bot._ensure_message_runtime_state()
        return bot

    def test_visible_window_repairs_gap_after_anchor(self):
        with tempfile.TemporaryDirectory() as tmp:
            bot = self.make_bot(tmp)
            bot.memory_manager.append_missing_messages(
                "张三",
                [
                    {"time": "1", "attr": "friend", "sender": "张三", "type": "text", "content": "早"},
                    {"time": "2", "attr": "self", "sender": "self", "type": "text", "content": "早呀"},
                ],
                100,
            )
            chat = FakeChat(visible=[
                msg("早", time="1"),
                msg("早呀", attr="self", sender="self", time="2"),
                msg("新内容", time="3"),
            ])

            bot._repair_private_context_before_ai(chat, msg("新内容", time="3"))

            self.assertEqual(
                [item["content"] for item in bot.memory_manager.get_messages("张三", 10)],
                ["早", "早呀", "新内容"],
            )

    def test_private_inbound_memory_preserves_wechat_message_time(self):
        with tempfile.TemporaryDirectory() as tmp:
            bot = self.make_bot(tmp)
            incoming = msg(
                "新内容",
                time="2026/07/04 17:01:02",
                message_id="native-text-1",
                message_hash="content-hash",
                message_hash_text="row-hash",
            )
            incoming._wxbot_received_at = "2026/07/04 17:01:09"

            saved = bot._save_private_incoming_memory_message(FakeChat(), incoming)

            self.assertTrue(saved)
            self.assertEqual(
                bot.memory_manager.get_messages("张三", 10)[0]["time"],
                "2026/07/04 17:01:02",
            )

    def test_successful_repair_sets_three_hundred_second_ttl(self):
        with tempfile.TemporaryDirectory() as tmp:
            bot = self.make_bot(tmp)
            bot.memory_manager.append_missing_messages(
                "张三",
                [{"time": "1", "attr": "friend", "sender": "张三", "type": "text", "content": "旧锚点"}],
                100,
            )
            chat = FakeChat(visible=[
                msg("旧锚点", time="1"),
                msg("新内容", time="2"),
            ])

            repaired = bot._repair_private_context_before_ai(chat, msg("新内容", time="2"))

            self.assertTrue(repaired)
            self.assertIn("private:张三", bot._memory_context_repair_last_at)
            self.assertFalse(bot._context_repair_success_ttl_allows("private:张三", 300))

    def test_visible_window_repair_uses_snapshot_without_scrolling_when_anchor_missing(self):
        with tempfile.TemporaryDirectory() as tmp:
            lock = FakeLock()
            bot = self.make_bot(tmp, lock=lock)
            bot.memory_manager.append_missing_messages(
                "张三",
                [{"time": "1", "attr": "friend", "sender": "张三", "type": "text", "content": "旧锚点"}],
                100,
            )
            chat = FakeChat(
                visible=[msg("新内容", time="3")],
                history=[msg("旧锚点", time="1"), msg("中间", time="2"), msg("新内容", time="3")],
            )

            repaired = bot._repair_private_context_before_ai(chat, msg("新内容", time="3"))

            self.assertTrue(repaired)
            self.assertFalse(hasattr(chat, "history_args"))
            self.assertEqual(lock.acquire_calls, [False])
            self.assertTrue(lock.released)
            self.assertEqual(
                [item["content"] for item in bot.memory_manager.get_messages("张三", 10)],
                ["旧锚点", "新内容"],
            )

    def test_lock_busy_skips_visible_read(self):
        with tempfile.TemporaryDirectory() as tmp:
            lock = FakeLock(acquired=False)
            bot = self.make_bot(tmp, lock=lock)
            bot.memory_manager.append_missing_messages(
                "张三",
                [{"time": "1", "attr": "friend", "sender": "张三", "type": "text", "content": "旧锚点"}],
                100,
            )
            chat = FakeChat(visible=[msg("新内容", time="3")], history=[msg("新内容", time="3")])

            bot._repair_private_context_before_ai(chat, msg("新内容", time="3"))

            self.assertEqual(lock.acquire_calls, [False])
            self.assertFalse(lock.released)
            self.assertEqual(chat.get_all_calls, 0)
            self.assertEqual([item["content"] for item in bot.memory_manager.get_messages("张三", 10)], ["旧锚点"])
            self.assertNotIn("张三", bot._memory_context_repair_startup_done)
            self.assertNotIn("private:张三", bot._memory_context_repair_last_at)

    def test_without_anchor_appends_visible_snapshot_when_history_exists(self):
        with tempfile.TemporaryDirectory() as tmp:
            bot = self.make_bot(tmp)
            bot.memory_manager.append_missing_messages(
                "张三",
                [{"time": "1", "attr": "friend", "sender": "张三", "type": "text", "content": "旧锚点"}],
                100,
            )
            chat = FakeChat(visible=[msg("新内容", time="3")])

            repaired = bot._repair_private_context_before_ai(chat, msg("新内容", time="3"))

            self.assertTrue(repaired)
            self.assertEqual([item["content"] for item in bot.memory_manager.get_messages("张三", 10)], ["旧锚点", "新内容"])

    def test_without_anchor_initializes_empty_history_from_visible_snapshot(self):
        with tempfile.TemporaryDirectory() as tmp:
            bot = self.make_bot(tmp)
            chat = FakeChat(visible=[msg("第一条", time="1"), msg("第二条", time="2")])

            repaired = bot._repair_private_context_before_ai(chat, msg("第二条", time="2"))

            self.assertTrue(repaired)
            self.assertEqual(
                [item["content"] for item in bot.memory_manager.get_messages("张三", 10)],
                ["第一条", "第二条"],
            )

    def test_snapshot_time_markers_insert_stopped_period_messages_before_current_trigger(self):
        with tempfile.TemporaryDirectory() as tmp:
            bot = self.make_bot(tmp)
            bot.memory_manager.append_missing_messages(
                "张三",
                [{"time": "2026/07/12 01:39:33", "attr": "friend", "sender": "张三", "type": "text", "content": "当前消息", "message_id": "native-current"}],
                100,
            )
            chat = FakeChat(visible=[
                msg("01:20", attr="system", sender="system", msg_type="time", time="2026/07/12 01:20:00"),
                msg("更早人工回复", attr="self", sender="self", time=""),
                msg("更早图片", msg_type="image", time=""),
                msg("01:39", attr="system", sender="system", msg_type="time", time="2026/07/12 01:39:00"),
                msg("当前消息", time=""),
            ])

            repaired = bot._repair_private_context_before_ai(
                chat,
                msg("当前消息", time="2026/07/12 01:39:33"),
            )

            self.assertTrue(repaired)
            self.assertEqual(
                [item["content"] for item in bot.memory_manager.get_messages("张三", 10)],
                ["更早人工回复", "[图片]", "当前消息"],
            )

    def test_private_repair_skips_periodic_read_without_strong_reason(self):
        with tempfile.TemporaryDirectory() as tmp:
            lock = FakeLock()
            bot = self.make_bot(tmp, lock=lock)
            bot.memory_manager.append_missing_messages(
                "张三",
                [
                    {"time": "2026/07/03 05:00:00", "attr": "friend", "sender": "张三", "type": "text", "content": "早"},
                    {"time": "2026/07/03 05:03:00", "attr": "friend", "sender": "张三", "type": "text", "content": "后来呢"},
                ],
                100,
            )
            bot._memory_context_repair_startup_done.add("张三")
            bot._memory_context_repair_last_at["private:张三"] = 0
            chat = FakeChat(visible=[
                msg("早", time="2026/07/03 05:00:00"),
                msg("手机发的第一条", attr="self", sender="self", time="2026/07/03 05:01:00"),
                msg("手机发的第二条", attr="self", sender="self", time="2026/07/03 05:02:00"),
                msg("后来呢", time="2026/07/03 05:03:00"),
            ])

            repaired = bot._repair_private_context_before_ai(chat, msg("后来呢", time="2026/07/03 05:03:00"))

            self.assertFalse(repaired)
            self.assertEqual(lock.acquire_calls, [])
            self.assertEqual(chat.get_all_calls, 0)
            self.assertEqual(
                [item["content"] for item in bot.memory_manager.get_messages("张三", 10)],
                ["早", "后来呢"],
            )

    def test_merged_private_message_does_not_trigger_repair_when_sources_are_saved(self):
        with tempfile.TemporaryDirectory() as tmp:
            lock = FakeLock()
            bot = self.make_bot(tmp)
            bot.memory_manager.append_missing_messages(
                "张三",
                [
                    {"time": "1", "attr": "friend", "sender": "张三", "type": "text", "content": "第一条"},
                    {"time": "2", "attr": "friend", "sender": "张三", "type": "text", "content": "第二条"},
                ],
                100,
            )
            bot._memory_context_repair_startup_done.add("张三")
            bot._wechat_action_lock = lock
            chat = FakeChat(visible=[msg("微信界面消息", time="3")])
            merged = SimpleNamespace(
                type="text",
                attr="friend",
                sender="张三",
                content="第一条\n第二条",
                _merged_source_messages=[
                    msg("第一条", time="1"),
                    msg("第二条", time="2"),
                ],
            )

            repaired = bot._repair_private_context_before_ai(chat, merged)

            self.assertFalse(repaired)
            self.assertEqual(lock.acquire_calls, [])
            self.assertEqual(chat.get_all_calls, 0)
            self.assertEqual(
                [item["content"] for item in bot.memory_manager.get_messages("张三", 10)],
                ["第一条", "第二条"],
            )

    def test_periodic_repair_does_not_read_without_strong_reason(self):
        with tempfile.TemporaryDirectory() as tmp:
            lock = FakeLock()
            bot = self.make_bot(tmp, lock=lock)
            bot.memory_manager.append_missing_messages(
                "张三",
                [
                    {"time": "2026/07/03 05:00:00", "attr": "friend", "sender": "张三", "type": "text", "content": "本地旧消息"},
                    {"time": "2026/07/03 05:10:00", "attr": "friend", "sender": "张三", "type": "text", "content": "当前消息"},
                ],
                100,
            )
            bot._memory_context_repair_startup_done.add("张三")
            bot._memory_context_repair_last_at["private:张三"] = 0
            chat = FakeChat(
                visible=[msg("另一个可见消息", time="2026/07/03 05:11:00")],
                history=[
                    msg("本地旧消息", time="2026/07/03 05:00:00"),
                    msg("历史中间", time="2026/07/03 05:01:00"),
                    msg("当前消息", time="2026/07/03 05:10:00"),
                ],
            )

            bot._repair_private_context_before_ai(chat, msg("当前消息", time="2026/07/03 05:10:00"))

            self.assertEqual(lock.acquire_calls, [])
            self.assertFalse(hasattr(chat, "history_args"))
            self.assertEqual(
                [item["content"] for item in bot.memory_manager.get_messages("张三", 10)],
                ["本地旧消息", "当前消息"],
            )

    def test_group_configured_chat_does_not_repair(self):
        with tempfile.TemporaryDirectory() as tmp:
            bot = self.make_bot(tmp)
            bot.config.group = ["测试群"]
            bot.memory_manager.append_missing_messages(
                "测试群",
                [{"time": "1", "attr": "friend", "sender": "张三", "type": "text", "content": "旧锚点"}],
                100,
            )
            chat = FakeChat(visible=[msg("新内容", time="3")])
            chat.who = "测试群"

            repaired = bot._repair_private_context_before_ai(chat, msg("新内容", time="3"))

            self.assertFalse(repaired)
            self.assertEqual([item["content"] for item in bot.memory_manager.get_messages("测试群", 10)], ["旧锚点"])

    def test_group_chat_type_does_not_repair(self):
        with tempfile.TemporaryDirectory() as tmp:
            bot = self.make_bot(tmp)
            bot.memory_manager.append_missing_messages(
                "未配置群",
                [{"time": "1", "attr": "friend", "sender": "张三", "type": "text", "content": "旧锚点"}],
                100,
            )
            chat = FakeChat(visible=[msg("新内容", time="3")])
            chat.who = "未配置群"
            chat.chat_type = "group"

            repaired = bot._repair_private_context_before_ai(chat, msg("新内容", time="3"))

            self.assertFalse(repaired)
            self.assertEqual([item["content"] for item in bot.memory_manager.get_messages("未配置群", 10)], ["旧锚点"])

    def test_group_current_trigger_is_saved_once_before_visible_repair(self):
        with tempfile.TemporaryDirectory() as tmp:
            bot = self.make_bot(tmp)
            bot.config.group = ["测试群"]
            current = msg("当前触发", sender="张三", time="2026/07/12 01:39:33")
            chat = FakeChat(visible=[
                msg("01:39", attr="system", sender="system", msg_type="time", time="2026/07/12 01:39:00"),
                msg("当前触发", sender="张三", time=""),
            ])
            chat.who = "测试群"
            chat.chat_type = "group"

            repaired = bot._repair_group_context_before_ai(chat, current)
            bot._save_incoming_memory_message(chat, current, chat_type="group")

            self.assertTrue(repaired)
            self.assertTrue(getattr(current, "_wxbot_memory_persisted", False))
            self.assertEqual(
                [item["content"] for item in bot.memory_manager.get_messages("测试群", 10)],
                ["当前触发"],
            )
