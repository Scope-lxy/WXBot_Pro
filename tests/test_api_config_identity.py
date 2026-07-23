import unittest

from core.config import (
    api_config_by_id,
    api_supports_capability,
    validate_api_config_references,
)


class ApiConfigIdentityTests(unittest.TestCase):
    def test_legacy_indexes_are_rejected_instead_of_being_translated(self):
        config = {
            "api_configs": [
                {"id": "api_a", "model": "A"},
                {"id": "api_b", "model": "B"},
            ],
            "api_index": 1,
            "backup_chat_api_index": 0,
            "chat_image_recognition_api": 0,
            "group_image_recognition_api": 1,
        }

        self.assertIsNone(api_config_by_id(config["api_configs"], 1))
        self.assertEqual(validate_api_config_references(config), "当前聊天接口不存在，请重新选择")

    def test_removing_an_earlier_api_does_not_shift_remaining_references(self):
        config = {
            "api_configs": [
                {"id": "api_a", "model": "A"},
                {"id": "api_b", "model": "B"},
                {"id": "api_c", "model": "C"},
            ],
            "api_id": "api_c",
            "backup_chat_api_id": "api_b",
            "chat_api_map": {"张三": "api_b"},
            "group_api_map": {"测试群": "api_c"},
            "chat_image_recognition_api_id": "api_b",
            "group_image_recognition_api_id": "api_c",
            "api_capability_map": {"api_b": {"vision": True}},
        }

        config["api_configs"].pop(0)
        self.assertEqual(validate_api_config_references(config), "")
        self.assertEqual(api_config_by_id(config["api_configs"], config["backup_chat_api_id"]), config["api_configs"][0])
        self.assertEqual(api_config_by_id(config["api_configs"], config["api_id"]), config["api_configs"][1])
        self.assertEqual(config["chat_image_recognition_api_id"], "api_b")
        self.assertEqual(config["group_image_recognition_api_id"], "api_c")
        self.assertTrue(api_supports_capability(config["api_capability_map"], "api_b", "vision"))

    def test_deleted_reference_is_rejected_instead_of_falling_back_to_another_api(self):
        config = {
            "api_configs": [
                {"id": "api_a", "model": "A"},
                {"id": "api_c", "model": "C"},
            ],
            "api_id": "api_a",
            "backup_chat_api_id": "api_c",
            "chat_api_map": {},
            "group_api_map": {},
            "chat_image_recognition_api_id": "api_deleted",
            "group_image_recognition_api_id": "api_c",
        }

        self.assertEqual(validate_api_config_references(config), "私聊辅助识图接口不存在，请重新选择")
