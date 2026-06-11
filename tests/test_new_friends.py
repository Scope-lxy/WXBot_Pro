import unittest

from feature.new_friends import (
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


if __name__ == "__main__":
    unittest.main()
