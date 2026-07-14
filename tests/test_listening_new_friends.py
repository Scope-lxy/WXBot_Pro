import unittest
from types import SimpleNamespace
from unittest.mock import patch

from feature import listening


class PassNewFriendsTests(unittest.TestCase):
    def test_pass_new_friends_respects_reply_switch(self):
        intents = []

        class Owner:
            def call(self, intent, _timeout):
                intents.append(intent)
                return [{"name": "阿英2", "send_name": "阿英2"}]

        bot = SimpleNamespace(
            _ui_owner=Owner(),
            config=SimpleNamespace(
                new_friend_archive_switch=False,
                new_friend_tags=[],
                new_friend_reply_switch=False,
                new_friend_msg={"text": "欢迎"},
            ),
            _config_ui_task_guard=lambda _category: ("new_friend", 3),
            _metric_increment=lambda _key: None,
        )

        with patch("feature.listening.time.sleep", return_value=None):
            self.assertTrue(listening.pass_new_friends(bot))

        self.assertEqual(
            [intent.kind for intent in intents],
            [listening.wechat_ui_actions.UIIntentKind.NEW_FRIEND],
        )

    def test_owner_new_friend_welcome_submits_each_action_separately(self):
        intents = []
        delays = []

        class Owner:
            def call(self, intent, _timeout):
                intents.append(intent)
                if intent.kind == listening.wechat_ui_actions.UIIntentKind.NEW_FRIEND:
                    return [{"name": "阿英2", "send_name": "阿英2_新好友"}]
                return [True]

        bot = SimpleNamespace(
            _ui_owner=Owner(),
            config=SimpleNamespace(
                new_friend_archive_switch=True,
                new_friend_remark_prefix="",
                new_friend_remark_suffix="_新好友",
                new_friend_remark_prefix_timestamp=False,
                new_friend_remark_suffix_timestamp=False,
                new_friend_tags=["新好友"],
                new_friend_reply_switch=True,
                new_friend_msg={"text": "欢迎", "files": [r"C:\Docs\a.pdf"]},
            ),
            _config_ui_task_guard=lambda _category: ("new_friend", 3),
            _inter_message_delay_or_stop=lambda: delays.append("delay"),
            _metric_increment=lambda _key: None,
        )

        with patch("feature.listening.time.sleep", return_value=None):
            self.assertTrue(listening.pass_new_friends(bot))

        send_intents = [
            intent for intent in intents
            if intent.kind == listening.wechat_ui_actions.UIIntentKind.SEND_ACTIONS
        ]
        self.assertEqual(len(send_intents), 2)
        self.assertTrue(
            all(intent.payload["chat_type"] == "private" for intent in send_intents)
        )
        self.assertEqual([len(intent.payload["actions"]) for intent in send_intents], [1, 1])
        self.assertEqual(send_intents[0].payload["actions"][0]["type"], "text")
        self.assertEqual(send_intents[1].payload["actions"][0]["type"], "file")
        self.assertEqual(delays, ["delay"])


class GroupWelcomeTests(unittest.TestCase):
    def test_group_welcome_submits_one_journaled_action(self):
        sent = []
        tracked = []

        def send_tracked(target, action, sender, **kwargs):
            tracked.append((target, action, kwargs))
            return sender(kwargs["delivery_id"])

        bot = SimpleNamespace(
            config=SimpleNamespace(group_welcome_msg="欢迎"),
            _config_ui_task_guard=lambda _category: ("group_welcome", 3),
            _send_tracked_outbound=send_tracked,
        )
        chat = SimpleNamespace(
            who="测试群",
            chat_type="group",
            SendActions=lambda actions, **kwargs: sent.append((actions, kwargs)) or True,
        )
        messages = (
            ('"张三"加入群聊', "张三"),
            ('"李四"通过扫描二维码加入了群聊', "李四"),
        )

        with patch("feature.listening.time.sleep", return_value=None):
            for content, expected_name in messages:
                with self.subTest(content=content):
                    result = listening.send_group_welcome_msg(
                        bot,
                        chat,
                        SimpleNamespace(content=content),
                    )
                    self.assertTrue(result)
                    actions, kwargs = sent[-1]
                    self.assertEqual(actions, [{"type": "text", "text": "欢迎", "at": expected_name}])
                    self.assertEqual(kwargs["task_version"], 3)
                    self.assertTrue(kwargs["delivery_id"].startswith("group-welcome:"))
                    self.assertEqual(kwargs["echo_delivery_ids"], (kwargs["delivery_id"],))

        self.assertEqual(len(sent), 2)
        self.assertEqual([item[0] for item in tracked], ["测试群", "测试群"])
        self.assertTrue(all(item[2]["chat_type"] == "group" for item in tracked))

    def test_empty_group_welcome_is_silent(self):
        bot = SimpleNamespace(
            config=SimpleNamespace(group_welcome_msg=""),
            _config_ui_task_guard=lambda _category: ("group_welcome", 3),
            _send_tracked_outbound=lambda *_args, **_kwargs: self.fail("空欢迎语不应发送"),
        )
        chat = SimpleNamespace(who="测试群", chat_type="group")

        self.assertTrue(listening.send_group_welcome_msg(
            bot,
            chat,
            SimpleNamespace(content='"张三"加入群聊'),
        ))


if __name__ == "__main__":
    unittest.main()
