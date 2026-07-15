import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace
from unittest.mock import patch

from core.memory import MemoryManager
from core.memory_context_repair import (
    build_tail_repair_plan,
    normalize_wechat_snapshot,
    snapshot_before_current,
)
from wxbot_core import WXBot
from feature import listening


def bubble(
    content,
    *,
    attr="friend",
    sender="张三",
    msg_type="text",
    message_id="",
    hash_text="",
    time="",
):
    return SimpleNamespace(
        attr=attr,
        sender=sender,
        type=msg_type,
        content=content,
        id=message_id,
        hash="",
        hash_text=hash_text,
        time=time,
    )


def stored_entry(
    event_id,
    content,
    *,
    received_at,
    attr="friend",
    sender="张三",
    msg_type="text",
    native_id="",
    chat_type="private",
    conversation="张三",
):
    return {
        "event_id": event_id,
        "conversation": conversation,
        "chat_type": chat_type,
        "direction": "manual_self" if attr == "self" else "friend",
        "sender": sender,
        "content": content,
        "original_content": content,
        "message_type": msg_type,
        "native_attr": attr,
        "native_id": native_id,
        "native_time": "",
        "received_at": received_at,
        "metadata": {},
    }


class SnapshotBoundaryTests(unittest.TestCase):
    def test_merged_ai_batch_excludes_every_source_bubble(self):
        first = bubble("第一条", message_id="source-1")
        second = bubble("第二条", message_id="source-2")
        current = SimpleNamespace(_merged_source_messages=[first, second])
        visible = [
            bubble("停机消息", message_id="gap"),
            first,
            second,
            bubble("后来消息", message_id="later"),
        ]

        boundary = snapshot_before_current(visible, current)

        self.assertTrue(boundary.found)
        self.assertEqual([item.content for item in boundary.messages], ["停机消息"])

    def test_native_merge_forward_is_one_current_bubble(self):
        current = bubble("外层预览", msg_type="merge", message_id="merge-1")
        visible = [
            bubble("停机消息", message_id="gap"),
            current,
            bubble("后来消息", message_id="later"),
        ]

        boundary = snapshot_before_current(visible, current)

        self.assertTrue(boundary.found)
        self.assertEqual([item.content for item in boundary.messages], ["停机消息"])

    def test_ambiguous_current_content_is_rejected(self):
        current = bubble("好的")
        boundary = snapshot_before_current(
            [bubble("好的"), bubble("好的")],
            current,
        )

        self.assertFalse(boundary.found)
        self.assertEqual(boundary.messages, [])

    def test_merged_recovery_uses_hashes_when_old_ui_ids_changed(self):
        first = bubble(
            "旧转写一",
            msg_type="voice",
            message_id="old-1",
            hash_text="hash-1",
        )
        second = bubble(
            "旧转写二",
            msg_type="voice",
            message_id="old-2",
            hash_text="hash-2",
        )
        current = SimpleNamespace(_merged_source_messages=[first, second])
        visible = [
            bubble("停机消息", message_id="gap"),
            bubble("语音2秒", msg_type="voice", message_id="new-1", hash_text="hash-1"),
            bubble("语音3秒", msg_type="voice", message_id="new-2", hash_text="hash-2"),
        ]

        boundary = snapshot_before_current(visible, current)

        self.assertTrue(boundary.found)
        self.assertEqual([item.content for item in boundary.messages], ["停机消息"])

    def test_native_merge_forward_normalizes_as_one_preview_record(self):
        entries = normalize_wechat_snapshot([
            bubble("甲: 第一条\n乙: 第二条", msg_type="merge", message_id="merge-1"),
        ])

        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0]["type"], "merge")
        self.assertEqual(entries[0]["content"], "甲: 第一条\n乙: 第二条")

    def test_skipped_voice_keeps_following_window_order_stable(self):
        entries = normalize_wechat_snapshot([
            bubble("", msg_type="voice"),
            bubble("后续文字"),
        ])

        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0]["window_order"], 1)


class TailPlanTests(unittest.TestCase):
    def test_appends_only_visible_tail_after_contiguous_local_suffix(self):
        local = [
            stored_entry("a", "旧一", received_at=1),
            stored_entry("b", "旧二", received_at=2, attr="self", sender="self"),
        ]
        visible = normalize_wechat_snapshot([
            bubble("旧一"),
            bubble("旧二", attr="self", sender="self"),
            bubble("漏掉一"),
            bubble("漏掉二"),
        ])

        plan = build_tail_repair_plan(local, visible, chat_type="private")

        self.assertTrue(plan.anchor_found)
        self.assertEqual(
            [item["content"] for item in plan.messages_to_append],
            ["漏掉一", "漏掉二"],
        )

    def test_without_history_anchor_appends_the_whole_visible_tail(self):
        local = [stored_entry("old", "完全无关", received_at=1)]
        visible = normalize_wechat_snapshot([
            bubble("最新一"),
            bubble("最新二"),
        ])

        plan = build_tail_repair_plan(local, visible, chat_type="private")

        self.assertFalse(plan.anchor_found)
        self.assertEqual(
            [item["content"] for item in plan.messages_to_append],
            ["最新一", "最新二"],
        )

    def test_same_text_occurrences_are_not_content_deduplicated(self):
        visible = normalize_wechat_snapshot([bubble("好"), bubble("好")])

        plan = build_tail_repair_plan([], visible, chat_type="private")

        self.assertEqual(
            [item["content"] for item in plan.messages_to_append],
            ["好", "好"],
        )


class StorageContractTests(unittest.TestCase):
    def make_manager(self, tmp):
        return MemoryManager("wxid", tmp)

    def test_no_anchor_tail_is_written_immediately_before_current(self):
        with tempfile.TemporaryDirectory() as tmp:
            manager = self.make_manager(tmp)
            manager.message_store.append_history([
                stored_entry("current", "当前", received_at=100),
            ])
            visible = normalize_wechat_snapshot([
                bubble("最新一"),
                bubble("最新二"),
            ])

            result = manager.reconcile_visible_tail(
                "张三",
                visible,
                current_event_ids=("current",),
                chat_type="private",
            )

            self.assertEqual(result["added"], 2)
            self.assertFalse(result["anchor_found"])
            self.assertEqual(
                [item["content"] for item in result["history_messages"]],
                ["最新一", "最新二"],
            )
            self.assertEqual(
                [item["content"] for item in manager.get_messages("张三", 10, chat_type="private")],
                ["最新一", "最新二", "当前"],
            )

    def test_no_anchor_does_not_restore_explicitly_deleted_history(self):
        with tempfile.TemporaryDirectory() as tmp:
            manager = self.make_manager(tmp)
            manager.message_store.append_history([
                stored_entry("deleted", "已删除", received_at=50),
            ])
            manager.message_store.delete_conversation("张三", chat_type="private", now=90)
            manager.message_store.append_history([
                stored_entry("current", "当前", received_at=100),
            ])

            result = manager.reconcile_visible_tail(
                "张三",
                normalize_wechat_snapshot([bubble("已删除")]),
                current_event_ids=("current",),
                chat_type="private",
            )

            self.assertEqual(result["added"], 0)
            self.assertEqual(result["deleted_boundary_skipped"], 1)
            self.assertEqual(
                [item["content"] for item in manager.get_messages("张三", 10, chat_type="private")],
                ["当前"],
            )

    def test_retry_of_same_current_batch_is_idempotent(self):
        with tempfile.TemporaryDirectory() as tmp:
            manager = self.make_manager(tmp)
            manager.message_store.append_history([
                stored_entry("current", "当前", received_at=100),
            ])
            visible = normalize_wechat_snapshot([bubble("漏掉")])

            first = manager.reconcile_visible_tail(
                "张三",
                visible,
                current_event_ids=("current",),
                chat_type="private",
            )
            second = manager.reconcile_visible_tail(
                "张三",
                visible,
                current_event_ids=("current",),
                chat_type="private",
            )

            self.assertEqual(first["added"], 1)
            self.assertEqual(second["added"], 0)
            self.assertEqual(
                [item["content"] for item in manager.get_messages("张三", 10, chat_type="private")],
                ["漏掉", "当前"],
            )

    def test_old_deletion_marker_does_not_block_a_later_no_anchor_tail(self):
        with tempfile.TemporaryDirectory() as tmp:
            manager = self.make_manager(tmp)
            manager.message_store.append_history([
                stored_entry("deleted", "已删除", received_at=50),
            ], now=50)
            manager.message_store.delete_conversation("张三", chat_type="private", now=90)
            manager.message_store.append_history([
                stored_entry("post-delete", "删除后消息", received_at=95),
            ], now=95)
            manager.message_store.append_history([
                stored_entry("current", "当前", received_at=100),
            ], now=100)

            result = manager.reconcile_visible_tail(
                "张三",
                normalize_wechat_snapshot([bubble("新的可见尾段")]),
                current_event_ids=("current",),
                chat_type="private",
            )

            self.assertEqual(result["added"], 1)
            self.assertFalse(result["anchor_found"])
            self.assertEqual(result["deleted_boundary_skipped"], 0)

    def test_clear_waits_for_the_atomic_repair_transaction(self):
        with tempfile.TemporaryDirectory() as tmp:
            manager = self.make_manager(tmp)
            manager.message_store.append_history([
                stored_entry("current", "当前", received_at=100),
            ])
            entered = threading.Event()
            release = threading.Event()

            from core import message_store as message_store_module

            original_builder = message_store_module.build_tail_repair_plan

            def blocking_builder(*args, **kwargs):
                entered.set()
                self.assertTrue(release.wait(5))
                return original_builder(*args, **kwargs)

            with patch.object(
                message_store_module,
                "build_tail_repair_plan",
                side_effect=blocking_builder,
            ), ThreadPoolExecutor(max_workers=2) as pool:
                repair = pool.submit(
                    manager.reconcile_visible_tail,
                    "张三",
                    normalize_wechat_snapshot([bubble("漏掉")]),
                    current_event_ids=("current",),
                    chat_type="private",
                )
                self.assertTrue(entered.wait(5))
                clear = pool.submit(
                    manager.clear_messages,
                    "张三",
                    chat_type="private",
                )
                self.assertFalse(clear.done())
                release.set()
                self.assertEqual(repair.result(timeout=5)["added"], 1)
                clear.result(timeout=5)

            self.assertEqual(manager.get_messages("张三", 10, chat_type="private"), [])


class FakeChat:
    def __init__(self, who="张三", chat_type="private", visible=None):
        self.who = who
        self.chat_type = chat_type
        self.visible = list(visible or [])
        self.get_all_calls = 0

    def GetAllMessage(self):
        self.get_all_calls += 1
        return list(self.visible)


class WXBotContractTests(unittest.TestCase):
    def make_bot(self, tmp):
        bot = WXBot.__new__(WXBot)
        bot.config = SimpleNamespace(
            memory_switch=True,
            memory_context_switch=True,
            chat_context_repair_switch=True,
            group_context_repair_switch=True,
            memory_context_count=50,
            memory_max_count=100,
        )
        bot.memory_manager = MemoryManager("wxid", tmp)
        bot._message_store = bot.memory_manager.message_store
        bot._mark_chat_memory_dirty = lambda *args, **kwargs: None
        bot.is_stop_requested = lambda: False
        bot._ensure_message_runtime_state()
        return bot

    def current_message(self, event_id="current", *, content="当前", message_id="current-id"):
        message = bubble(content, message_id=message_id)
        message._wxbot_event_id = event_id
        message._wxbot_event_ids = (event_id,)
        message._wxbot_received_at = 100
        return message

    def test_private_and_group_switches_remain_independent(self):
        with tempfile.TemporaryDirectory() as tmp:
            bot = self.make_bot(tmp)
            bot.config.chat_context_repair_switch = False
            private = FakeChat(visible=[])
            group = FakeChat("测试群", "group", visible=[])

            self.assertEqual(
                bot._repair_context_before_ai(private, self.current_message(), chat_type="private"),
                [],
            )
            self.assertEqual(private.get_all_calls, 0)
            self.assertTrue(bot.config.group_context_repair_switch)
            self.assertEqual(
                bot._repair_context_before_ai(group, self.current_message(), chat_type="group"),
                [],
            )
            self.assertEqual(group.get_all_calls, 1)

            bot.config.chat_context_repair_switch = True
            bot.config.group_context_repair_switch = False
            private_enabled = FakeChat("李四", "private", visible=[])
            group_disabled = FakeChat("另一个群", "group", visible=[])
            self.assertEqual(
                bot._repair_context_before_ai(
                    private_enabled,
                    self.current_message(),
                    chat_type="private",
                ),
                [],
            )
            self.assertEqual(private_enabled.get_all_calls, 1)
            self.assertEqual(
                bot._repair_context_before_ai(
                    group_disabled,
                    self.current_message(),
                    chat_type="group",
                ),
                [],
            )
            self.assertEqual(group_disabled.get_all_calls, 0)

    def test_repaired_messages_are_available_to_the_current_ai_turn(self):
        with tempfile.TemporaryDirectory() as tmp:
            bot = self.make_bot(tmp)
            bot.memory_manager.message_store.append_history([
                stored_entry("current", "当前", received_at=100),
            ])
            current = self.current_message()
            chat = FakeChat(visible=[bubble("漏掉"), current])

            repaired = bot._repair_context_before_ai(chat, current, chat_type="private")
            history = bot._get_model_context_history(
                "张三",
                event_ids=("current",),
                chat_type="private",
                extra_messages=repaired,
            )

            self.assertEqual([item["content"] for item in repaired], ["漏掉"])
            self.assertEqual([item["content"] for item in history], ["张三: 漏掉"])

    def test_successful_scan_runs_once_until_window_restore(self):
        with tempfile.TemporaryDirectory() as tmp:
            bot = self.make_bot(tmp)
            bot.memory_manager.message_store.append_history([
                stored_entry("current", "当前", received_at=100),
            ])
            current = self.current_message()
            chat = FakeChat(visible=[current])

            bot._repair_context_before_ai(chat, current, chat_type="private")
            bot._repair_context_before_ai(chat, current, chat_type="private")
            self.assertEqual(chat.get_all_calls, 1)
            history = bot.memory_manager.get_messages("张三", 10, chat_type="private")
            self.assertEqual([item["content"] for item in history], ["当前"])

            bot._mark_context_repair_needed_after_restore("张三", chat_type="private")
            bot._repair_context_before_ai(chat, current, chat_type="private")
            self.assertEqual(chat.get_all_calls, 2)

    def test_listener_window_restore_marks_the_same_chat_type_dirty(self):
        marked = []
        bot = SimpleNamespace(
            is_stop_requested=lambda: False,
            _mark_context_repair_needed_after_restore=lambda name, *, chat_type: marked.append(
                (chat_type, name)
            ),
        )
        listening.ensure_listener_window_recovery_state(bot).request(
            "测试群",
            chat_type="group",
            now=0,
        )
        sub_chat = SimpleNamespace(who="测试群", chat_type="group")

        with patch.object(
            listening,
            "get_runtime_cached_subwindow",
            return_value=sub_chat,
        ), patch.object(listening, "touch_dynamic_listener_entry"), patch.object(
            listening,
            "_bot_log",
        ):
            handled = listening.flush_listener_window_recovery_tasks(bot)

        self.assertTrue(handled)
        self.assertEqual(marked, [("group", "测试群")])


if __name__ == "__main__":
    unittest.main()
