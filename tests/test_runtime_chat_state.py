import unittest
from types import SimpleNamespace

from core import runtime_chat_state


class RuntimeChatStateSendTests(unittest.TestCase):
    def test_send_text_delegates_to_ui_owner_sender(self):
        sends = []

        class CachedChat:
            def SendMsg(self, _msg):
                raise AssertionError("不应绕过 UI owner 使用缓存 Chat 发送")

        bot = SimpleNamespace(
            _listen_chats={"阿英2": CachedChat()},
            _send_text_to_target_without_child=lambda target, msg, **kwargs: sends.append(
                (target, msg, kwargs)
            ) or True,
        )

        result = runtime_chat_state.send_text_to_target(
            bot,
            "阿英2",
            "你好",
            contact_key="wechat_id:wxid_aying2",
            task_key="scheduled_message:task-1",
            task_version=3,
            require_contact_key=True,
        )

        self.assertTrue(result)
        self.assertEqual(sends, [(
            "阿英2",
            "你好",
            {
                "contact_key": "wechat_id:wxid_aying2",
                "task_key": "scheduled_message:task-1",
                "task_version": 3,
                "require_contact_key": True,
            },
        )])

    def test_send_text_without_ui_owner_sender_fails_closed(self):
        result = runtime_chat_state.send_text_to_target(SimpleNamespace(), "阿英2", "你好")

        self.assertFalse(result)

    def test_send_file_delegates_to_ui_owner_sender(self):
        sends = []

        class CachedChat:
            def SendFiles(self, filepath=None):
                raise AssertionError("不应绕过 UI owner 使用缓存 Chat 发送")

        bot = SimpleNamespace(
            _listen_chats={"阿英2": CachedChat()},
            _send_file_to_target_without_child=lambda target, path, **kwargs: sends.append(
                (target, path, kwargs)
            ) or True,
        )

        result = runtime_chat_state.send_file_to_target(
            bot,
            "阿英2",
            r"C:\tmp\a.pdf",
            task_key="material_outreach:task-1",
            task_version=4,
        )

        self.assertTrue(result)
        self.assertEqual(sends, [(
            "阿英2",
            r"C:\tmp\a.pdf",
            {
                "contact_key": "",
                "task_key": "material_outreach:task-1",
                "task_version": 4,
                "require_contact_key": False,
            },
        )])


if __name__ == "__main__":
    unittest.main()
