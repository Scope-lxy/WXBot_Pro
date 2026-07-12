import unittest
from datetime import datetime
from types import SimpleNamespace

from core import runtime_chat_state
from core.wechat_ui_actions import (
    ActionBatchInterrupted,
    IntentCancelled,
    UI_CALL_WAIT_TIMEOUT,
    UIIntent,
    UIIntentKind,
    WeChatUIOwner,
)
from feature.scheduled_message_tasks import (
    mark_scheduled_message_running,
    recover_interrupted_scheduled_message_task,
)
from feature.scheduled_messages import execute_scheduled_message_task
from wxbot_core import WXBot


class ScheduledMessageRecoveryTests(unittest.TestCase):
    def test_interrupted_inflight_delivery_becomes_uncertain_and_never_auto_runs(self):
        task = mark_scheduled_message_running(
            {
                "id": "task-1",
                "enabled": True,
                "status": "pending",
                "targets": ["张三"],
                "msgs": ["你好"],
            },
            run_id="run-1",
            started_at=datetime(2026, 7, 11, 10, 0).isoformat(),
        )
        task["pending_snapshot"] = {
            "run_id": "run-1",
            "delivery_records": [{
                "key": "0:0",
                "target": "张三",
                "message_index": 0,
                "status": "inflight",
                "error": "",
            }],
        }

        recovered = recover_interrupted_scheduled_message_task(task)

        self.assertEqual(recovered["status"], "pending_confirm")
        records = recovered["last_result"]["delivery_records"]
        self.assertEqual(records[0]["status"], "uncertain")
        self.assertEqual(recovered["next_run_at"], "")

    def test_send_exception_is_uncertain_and_callback_persists_both_states(self):
        records = [{
            "key": "0:0",
            "target": "张三",
            "message_index": 0,
            "status": "pending",
            "error": "",
        }]
        states = []

        result = execute_scheduled_message_task(
            task={"targets": ["张三"], "msgs": ["你好"]},
            send_text=lambda *_args: (_ for _ in ()).throw(RuntimeError("连接中断")),
            send_file=lambda *_args: True,
            is_image_path=lambda _value: False,
            human_delay=lambda: None,
            should_stop=lambda: False,
            notify_error=lambda *_args: None,
            nickname="测试账号",
            scheduled_tasks=[],
            config_data={},
            save_config=None,
            delivery_records=records,
            on_delivery_state=lambda record: states.append(record["status"]),
        )

        self.assertEqual(result["result_type"], "uncertain")
        self.assertEqual(states, ["inflight", "uncertain"])
        self.assertEqual(records[0]["status"], "uncertain")

    def test_contact_target_record_reaches_send_callback_unchanged(self):
        target = {
            "contact_key": "wechat_id:wxid_zhangsan",
            "send_name": "张三备注",
            "display_name": "张三",
            "require_contact_key": False,
        }
        sends = []

        result = execute_scheduled_message_task(
            task={"targets": [target], "msgs": ["你好"]},
            send_text=lambda actual_target, msg: sends.append((actual_target, msg)) or True,
            send_file=lambda *_args: True,
            is_image_path=lambda _value: False,
            human_delay=lambda: None,
            should_stop=lambda: False,
            notify_error=lambda *_args: None,
            nickname="测试账号",
            scheduled_tasks=[],
            config_data={},
            save_config=None,
        )

        self.assertEqual(result["result_type"], "success")
        self.assertEqual(sends, [(target, "你好")])

    def test_scheduled_task_definition_change_cancels_queued_owner_send(self):
        task = {
            "id": "task-1",
            "name": "提醒",
            "enabled": True,
            "targets": ["张三"],
            "msgs": ["旧内容"],
        }
        bot = WXBot.__new__(WXBot)
        bot.config = SimpleNamespace(scheduled_message_task_list=[task])
        task_key, task_version = bot._scheduled_message_ui_guard(task)
        task["msgs"] = ["新内容"]
        owner = WeChatUIOwner(
            {UIIntentKind.SEND_TEXT: lambda payload: payload["text"]},
            task_version_provider=bot._current_ui_task_version,
        )
        owner.start()
        try:
            ticket = owner.submit(UIIntent(
                UIIntentKind.SEND_TEXT,
                {"conversation": "张三", "text": "旧内容", "task_key": task_key},
                task_version=task_version,
            ))
            with self.assertRaises(IntentCancelled):
                ticket.result(1)
        finally:
            owner.stop()

    def test_runtime_sender_forwards_task_and_contact_identity(self):
        calls = []
        bot = SimpleNamespace(
            _ui_owner=object(),
            _send_text_to_target_without_child=lambda target, msg, **kwargs: calls.append((target, msg, kwargs)) or True,
        )

        result = runtime_chat_state.send_text_to_target(
            bot,
            "旧备注",
            "你好",
            contact_key="wechat_id:wxid_zhangsan",
            task_key="scheduled_message:task-1",
            task_version=7,
        )

        self.assertTrue(result)
        self.assertEqual(calls, [(
            "旧备注",
            "你好",
            {
                "contact_key": "wechat_id:wxid_zhangsan",
                "task_key": "scheduled_message:task-1",
                "task_version": 7,
                "require_contact_key": False,
            },
        )])

    def test_owner_text_sender_forwards_required_contact_identity(self):
        calls = []

        class Owner:
            def call(self, intent, timeout):
                calls.append((intent, timeout))
                return True

        bot = WXBot.__new__(WXBot)
        bot._ui_owner = Owner()
        bot._remember_private_outbound_echo_for_send_result = lambda *_args, **_kwargs: None

        result = bot._send_text_to_target_without_child(
            "旧备注",
            "你好",
            contact_key="wechat_id:wxid_zhangsan",
            task_key="scheduled_message:task-1",
            task_version=7,
            require_contact_key=True,
        )

        self.assertTrue(result)
        intent, timeout = calls[0]
        self.assertIs(timeout, UI_CALL_WAIT_TIMEOUT)
        self.assertEqual(intent.payload["contact_key"], "wechat_id:wxid_zhangsan")
        self.assertTrue(intent.payload["require_contact_key"])
        self.assertEqual(intent.task_version, 7)

    def test_saved_contact_key_resolves_current_remark_for_scheduled_task(self):
        bot = WXBot.__new__(WXBot)
        directory = {
            "subjects": [{
                "contact_key": "wechat_id:wxid_zhangsan",
                "wechat_id": "wxid_zhangsan",
                "remark": "新备注",
                "nickname": "张三",
            }],
        }
        bot._load_contact_profiles_directory = lambda: (directory, "contacts.json", "wxid_test")

        records = bot._resolve_scheduled_message_task_target_records({
            "targets_mode": "manual",
            "targets": ["wechat_id:wxid_zhangsan"],
            "manual_target_names": [],
        })

        self.assertEqual(records[0]["contact_key"], "wechat_id:wxid_zhangsan")
        self.assertEqual(records[0]["send_name"], "新备注")
        self.assertTrue(records[0]["require_contact_key"])

    def test_owner_payload_preparer_rejects_duplicate_current_send_name(self):
        bot = WXBot.__new__(WXBot)
        directory = {
            "subjects": [
                {"contact_key": "wechat_id:a", "wechat_id": "a", "remark": "同名"},
                {"contact_key": "wechat_id:b", "wechat_id": "b", "remark": "同名"},
            ],
        }
        bot._load_contact_profiles_directory = lambda: (directory, "contacts.json", "wxid_test")

        with self.assertRaises(IntentCancelled):
            bot._prepare_ui_intent_payload(UIIntent(
                UIIntentKind.SEND_TEXT,
                {"conversation": "旧备注", "contact_key": "wechat_id:a", "text": "你好"},
            ))

    def test_manual_exact_name_in_combined_mode_does_not_require_contact_key(self):
        bot = WXBot.__new__(WXBot)
        bot._resolve_scheduled_message_task_targets = lambda _task: ["通讯录外精确名称"]
        bot._load_contact_profiles_directory = lambda: ({"subjects": []}, "contacts.json", "wxid_test")

        records = bot._resolve_scheduled_message_task_target_records({
            "targets_mode": "include",
            "targets": [],
            "manual_target_names": ["通讯录外精确名称"],
        })

        self.assertEqual(records[0]["contact_key"], "")
        self.assertFalse(records[0]["require_contact_key"])

    def test_action_batch_interruption_preserves_done_uncertain_and_not_started(self):
        records = []

        result = execute_scheduled_message_task(
            task={"targets": ["张三"], "msgs": ["第一条", "第二条", "第三条"]},
            send_text=lambda *_args: True,
            send_file=lambda *_args: True,
            send_actions=lambda *_args: (_ for _ in ()).throw(
                ActionBatchInterrupted([True], 1, RuntimeError("结果丢失"))
            ),
            is_image_path=lambda _value: False,
            human_delay=lambda: None,
            should_stop=lambda: False,
            notify_error=lambda *_args: None,
            nickname="测试账号",
            scheduled_tasks=[],
            config_data={},
            save_config=None,
            delivery_records=records,
        )

        self.assertEqual([item["status"] for item in records], ["done", "uncertain", "pending"])
        self.assertEqual(result["success_count"], 1)
        self.assertEqual(result["uncertain_count"], 1)

    def test_material_manual_exact_target_is_not_forced_to_have_contact_key(self):
        captured = []

        class Owner:
            def call(self, intent, _timeout):
                captured.append(intent)
                return True

        bot = WXBot.__new__(WXBot)
        bot._ui_owner = Owner()
        bot._material_ui_context = SimpleNamespace(value={
            "task_key": "material_outreach:task-1",
            "task_version": 3,
            "contacts_by_name": {
                "通讯录外精确名称": {
                    "contact_key": "",
                    "send_name": "通讯录外精确名称",
                    "require_contact_key": False,
                },
            },
        })

        bot._ui_forward_message(
            SimpleNamespace(who="素材源"),
            SimpleNamespace(type="text", attr="friend", sender="", content="素材", original_content="素材"),
            "通讯录外精确名称",
        )

        target = captured[0].payload["target_contacts"][0]
        self.assertEqual(target["contact_key"], "")
        self.assertFalse(target["require_contact_key"])


if __name__ == "__main__":
    unittest.main()
