import os
import tempfile
import unittest

from core.prompt_system import PromptBuilder, PromptSystem, SystemPromptStore


PROJECT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ACTIVE_PERSONA_PATH = os.path.join(PROJECT_DIR, "data", "prompt", "瑞东-红颜知己V3.md")
SYSTEM_PROMPT_DIR = os.path.join(PROJECT_DIR, "data", "system_prompts")


class PromptOfflineBoundaryTests(unittest.TestCase):
    def test_active_persona_excludes_physical_delivery_from_support(self):
        with open(ACTIVE_PERSONA_PATH, "r", encoding="utf-8") as f:
            prompt = f.read()

        self.assertIn("想给你寄东西", prompt)
        self.assertIn("本规则压过“不劝退主动支持”", prompt)
        self.assertIn("实物礼物、快递和任何需要现实交付的安排都不属于支持", prompt)

    def test_final_prompt_keeps_real_world_delivery_boundary(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            prompt_dir = os.path.join(temp_dir, "prompt")
            state_dir = os.path.join(temp_dir, "state")
            os.makedirs(prompt_dir)
            with open(os.path.join(prompt_dir, "测试人设.md"), "w", encoding="utf-8") as f:
                f.write("基础人设内容")

            system = PromptSystem(
                {"default_prompt": "测试人设", "chat_memory_switch": False},
                state_dir=state_dir,
                prompt_dir=prompt_dir,
                prompt_builder=PromptBuilder(SystemPromptStore(SYSTEM_PROMPT_DIR)),
            )
            final_prompt = system.build_prompt("测试联系人")

        self.assertIn("寄送或收取快递和实物礼物", final_prompt)
        self.assertIn("不得同意、承诺、确认、索取、提供", final_prompt)


if __name__ == "__main__":
    unittest.main()
