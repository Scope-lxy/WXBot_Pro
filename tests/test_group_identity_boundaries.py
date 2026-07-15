import tempfile
import time
import unittest
from types import SimpleNamespace

from core.memory import MemoryManager
from core.message_store import MessageStore
from feature import message_routing


def _routing_config(**overrides):
    values = {
        "AllListen_switch": False,
        "global_blacklist": [],
        "listen_list": [],
        "group": [],
        "group_switch": True,
        "group_image_recognition_switch": False,
        "group_voice_recognition_switch": False,
        "chat_image_recognition_switch": False,
        "chat_voice_recognition_switch": False,
        "group_keyword_switch": False,
        "group_keyword_at_only": False,
        "keyword_dict": {},
        "group_reply_at": False,
        "group_listen_only": False,
        "AtMe": "@机器人",
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def _record(store, *, chat_type, content, native_id, image_path="", conversation="同名会话"):
    payload = {
        "conversation": conversation,
        "chat_type": chat_type,
        "direction": "friend",
        "sender": "发送者",
        "content": content,
        "original_content": content,
        "message_type": "image" if image_path else "text",
        "native_attr": "friend",
        "native_id": native_id,
        "received_at": time.time(),
        "source": "test",
        "source_batch": native_id,
        "source_order": 0,
    }
    if image_path:
        payload["image_paths"] = (image_path,)
    return store.record_inbound(payload)


class GroupRoutingIdentityTests(unittest.TestCase):
    def test_private_chat_with_group_name_stays_private(self):
        config = _routing_config(
            listen_list=["同名会话"],
            group=["同名会话"],
        )
        bot = SimpleNamespace(config=config, _pause_group_reply=False)
        chat = SimpleNamespace(who="同名会话", chat_type="private")
        message = SimpleNamespace(type="text", content="你好", sender="好友")

        self.assertEqual(
            message_routing.route_process_message(bot, chat, message),
            {"action": "private_ai"},
        )

    def test_group_chat_in_private_list_is_not_routed_as_private(self):
        config = _routing_config(listen_list=["同名会话"], group=[])
        bot = SimpleNamespace(config=config, _pause_group_reply=False)
        chat = SimpleNamespace(who="同名会话", chat_type="group")
        message = SimpleNamespace(type="text", content="你好", sender="群友")

        self.assertEqual(
            message_routing.route_process_message(bot, chat, message),
            {"action": "skip"},
        )

    def test_recognition_switches_use_chat_type_before_name(self):
        config = _routing_config(
            listen_list=["同名会话"],
            group=["同名会话"],
            group_image_recognition_switch=True,
            chat_voice_recognition_switch=True,
        )
        bot = SimpleNamespace(config=config)

        self.assertEqual(
            message_routing._recognition_switches_for_chat(
                bot,
                SimpleNamespace(who="同名会话", chat_type="private"),
            ),
            (False, True),
        )
        self.assertEqual(
            message_routing._recognition_switches_for_chat(
                bot,
                SimpleNamespace(who="同名会话", chat_type="group"),
            ),
            (True, False),
        )

    def test_internal_routing_rejects_friend_chat_type(self):
        bot = SimpleNamespace(config=_routing_config(), _pause_group_reply=False)

        with self.assertRaisesRegex(ValueError, "private or group"):
            message_routing.route_process_message(
                bot,
                SimpleNamespace(who="张三", chat_type="friend"),
                SimpleNamespace(type="text", content="你好", sender="张三"),
            )


class MemoryChatIdentityTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        self.store = MessageStore(self.tempdir.name, "identity-test")
        self.manager = MemoryManager(
            "identity-test",
            self.tempdir.name,
            message_store=self.store,
        )

    def test_same_name_private_and_group_history_are_isolated(self):
        _record(self.store, chat_type="private", content="私聊消息", native_id="p1")
        _record(self.store, chat_type="group", content="群聊消息", native_id="g1")

        self.assertEqual(
            [item["content"] for item in self.manager.get_messages(
                "同名会话", 10, chat_type="private"
            )],
            ["私聊消息"],
        )
        self.assertEqual(
            [item["content"] for item in self.manager.get_messages(
                "同名会话", 10, chat_type="group"
            )],
            ["群聊消息"],
        )

    def test_group_name_does_not_use_private_contact_resolver(self):
        manager = MemoryManager(
            "identity-test",
            self.tempdir.name,
            chat_name_resolver=lambda _name: "私聊真源",
            message_store=self.store,
        )
        _record(
            self.store,
            chat_type="private",
            content="消息",
            native_id="private-message",
            conversation="私聊真源",
        )
        _record(self.store, chat_type="group", content="群消息", native_id="group-message")

        self.assertEqual(
            [item["content"] for item in manager.get_messages(
                "同名会话", 10, chat_type="private"
            )],
            ["消息"],
        )
        self.assertEqual(
            [item["content"] for item in manager.get_messages(
                "同名会话", 10, chat_type="group"
            )],
            ["群消息"],
        )
        self.assertEqual(
            self.store.history("私聊真源", 10, chat_type="group"),
            [],
        )

    def test_visual_notes_and_delete_are_scoped_by_chat_type(self):
        image_path = "C:/tmp/same.png"
        _record(
            self.store,
            chat_type="private",
            content="[图片]",
            native_id="p-image",
            image_path=image_path,
        )
        _record(
            self.store,
            chat_type="group",
            content="[图片]",
            native_id="g-image",
            image_path=image_path,
        )

        self.assertTrue(self.manager.attach_visual_notes(
            "同名会话",
            [image_path],
            ["私聊图片摘要"],
            chat_type="private",
        ))
        private = self.manager.get_messages("同名会话", 10, chat_type="private")
        group = self.manager.get_messages("同名会话", 10, chat_type="group")
        self.assertEqual(private[0]["visual_note"], "私聊图片摘要")
        self.assertNotIn("visual_note", group[0])

        self.manager.clear_messages("同名会话", chat_type="private")
        self.assertEqual(
            self.manager.get_messages("同名会话", 10, chat_type="private"),
            [],
        )
        self.assertEqual(
            [item["content"] for item in self.manager.get_messages(
                "同名会话", 10, chat_type="group"
            )],
            ["[图片]"],
        )

    def test_memory_api_rejects_friend_chat_type(self):
        with self.assertRaisesRegex(ValueError, "private or group"):
            self.manager.get_messages("同名会话", 10, chat_type="friend")


if __name__ == "__main__":
    unittest.main()
