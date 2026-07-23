import json
import os
import tempfile
import unittest

from core.wxbot_config import WXBotConfig
from core.prompt_system import PromptSystem


class WXBotConfigLegacyPromptTests(unittest.TestCase):
    def test_reply_limit_defaults_are_consistent_for_private_and_group_chat(self):
        original_cwd = os.getcwd()
        with tempfile.TemporaryDirectory() as temp_dir:
            os.chdir(temp_dir)
            try:
                config = WXBotConfig()

                self.assertEqual(config.chat_text_reply_limit_count, 50)
                self.assertEqual(config.chat_text_reply_limit_hours, 5)
                self.assertEqual(config.group_text_reply_limit_count, 50)
                self.assertEqual(config.group_text_reply_limit_hours, 5)
                self.assertEqual(config.chat_voice_reply_limit_count, 5)
                self.assertEqual(config.chat_voice_reply_limit_hours, 5)
                self.assertEqual(config.group_voice_reply_limit_count, 5)
                self.assertEqual(config.group_voice_reply_limit_hours, 5)
            finally:
                os.chdir(original_cwd)

    def test_chat_memory_protects_the_same_count_as_recent_history(self):
        system = PromptSystem(
            {
                "memory_context_count": 37,
            },
            state_dir="unused",
        )

        self.assertEqual(system.memory_extractor.protected_recent_count, 37)

    def test_memory_context_count_allows_up_to_200(self):
        original_cwd = os.getcwd()
        with tempfile.TemporaryDirectory() as temp_dir:
            os.chdir(temp_dir)
            try:
                config_dir = os.path.join(temp_dir, "data", "config")
                os.makedirs(config_dir, exist_ok=True)
                config_path = os.path.join(config_dir, "config.json")
                with open(config_path, "w", encoding="utf-8") as f:
                    json.dump({"memory_context_count": 200}, f, ensure_ascii=False)

                config = WXBotConfig()

                self.assertEqual(config.memory_context_count, 200)
            finally:
                os.chdir(original_cwd)

    def test_legacy_prompt_field_is_not_loaded_or_persisted(self):
        original_cwd = os.getcwd()
        with tempfile.TemporaryDirectory() as temp_dir:
            os.chdir(temp_dir)
            try:
                config_dir = os.path.join(temp_dir, "data", "config")
                os.makedirs(config_dir, exist_ok=True)
                config_path = os.path.join(config_dir, "config.json")
                with open(config_path, "w", encoding="utf-8") as f:
                    json.dump(
                        {
                            "api_configs": [
                                {
                                    "id": "api_test",
                                    "sdk": "DusAPI",
                                    "key": "test-key",
                                    "url": "https://api.example.com",
                                    "model": "test-model",
                                }
                            ],
                            "api_id": "api_test",
                            "prompt": "旧版系统提示词",
                            "default_prompt": "默认",
                        },
                        f,
                        ensure_ascii=False,
                    )

                config = WXBotConfig()

                self.assertEqual(config.prompt, "")
                self.assertEqual(config.current_api_config.prompt, "")

                config.save_config()

                with open(config_path, "r", encoding="utf-8") as f:
                    saved = json.load(f)
                self.assertNotIn("prompt", saved)
            finally:
                os.chdir(original_cwd)
