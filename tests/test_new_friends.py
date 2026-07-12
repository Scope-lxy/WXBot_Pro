import unittest
from datetime import datetime

from feature.new_friends import (
    build_new_friend_remark,
    iter_new_friend_welcome_actions,
    normalize_new_friend_welcome_messages,
    new_friend_welcome_message_has_content,
)


class NewFriendWelcomeMessageTests(unittest.TestCase):
    def test_normalize_keeps_single_structured_message(self):
        message = normalize_new_friend_welcome_messages({
            "text": "  你好  ",
            "files": [" C:\\Docs\\a.pdf ", ""],
        })

        self.assertEqual(message, {"text": "你好", "files": ["C:\\Docs\\a.pdf"]})

    def test_normalize_rejects_non_dict_values(self):
        self.assertEqual(
            normalize_new_friend_welcome_messages([{"text": "旧数组不兼容"}]),
            {"text": "", "files": []},
        )

    def test_normalize_limits_files_per_message(self):
        files = [f"C:\\Docs\\{index}.pdf" for index in range(12)]

        message = normalize_new_friend_welcome_messages({"text": "", "files": files})

        self.assertEqual(len(message["files"]), 9)
        self.assertEqual(message["files"][-1], "C:\\Docs\\8.pdf")

    def test_iter_actions_sends_text_then_files(self):
        actions = list(iter_new_friend_welcome_actions(
            {"text": "欢迎", "files": ["C:\\Docs\\a.pdf", "C:\\Docs\\b.zip"]},
        ))

        self.assertEqual(actions, [
            {"type": "text", "content": "欢迎"},
            {"type": "file", "path": "C:\\Docs\\a.pdf"},
            {"type": "file", "path": "C:\\Docs\\b.zip"},
        ])

    def test_has_content_checks_text_or_files(self):
        self.assertFalse(new_friend_welcome_message_has_content({"text": "", "files": []}))
        self.assertTrue(new_friend_welcome_message_has_content({"text": "", "files": ["C:\\Docs\\a.pdf"]}))


class NewFriendRemarkTests(unittest.TestCase):
    def test_remark_always_keeps_a_valid_original_nickname(self):
        remark = build_new_friend_remark("阿英2", prefix="来源_", suffix="_机器人")

        self.assertIn("阿英2", remark)
        self.assertLessEqual(len(remark.encode("gbk")), 32)

    def test_pure_emoji_nickname_is_valid(self):
        remark = build_new_friend_remark("😀✨", suffix="_新好友")

        self.assertIn("😀✨", remark)

    def test_control_characters_and_replacement_mark_are_removed(self):
        remark = build_new_friend_remark("阿\x00英\ufffd2")

        self.assertEqual(remark, "阿英2")

    def test_question_mark_only_nickname_uses_timestamped_fallback(self):
        remark = build_new_friend_remark(
            "????",
            now=datetime(2026, 7, 11, 6, 30, 0),
        )

        self.assertEqual(remark, "新好友_20260711063000")

    def test_nickname_has_priority_over_oversized_decorations(self):
        remark = build_new_friend_remark("阿英2", prefix="前缀" * 20, suffix="后缀" * 20)

        self.assertIn("阿英2", remark)
        self.assertLessEqual(len(remark.encode("gbk")), 32)


if __name__ == "__main__":
    unittest.main()
