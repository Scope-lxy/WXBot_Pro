import unittest

from feature.ai_material_outreach import normalize_ai_auto_outreach_runtime_config


class AiMaterialOutreachConfigTests(unittest.TestCase):
    def test_runtime_config_preserves_saved_user_options(self):
        config = normalize_ai_auto_outreach_runtime_config({
            "ai_material_outreach_switch": True,
            "ai_material_outreach_sensitivity": "aggressive",
            "ai_material_outreach_preface_enabled": False,
            "ai_material_outreach_preface_goal": "升温关系",
            "ai_material_outreach_preface_intensity": "积极主动",
            "ai_material_outreach_allowed_sources": ["素材源A"],
            "ai_material_outreach_detection_interval_minutes": 12,
            "ai_material_outreach_detection_message_threshold": 8,
            "ai_material_outreach_daily_limit_per_friend": 5,
        })

        self.assertTrue(config["ai_material_outreach_switch"])
        self.assertEqual(config["ai_material_outreach_sensitivity"], "aggressive")
        self.assertFalse(config["ai_material_outreach_preface_enabled"])
        self.assertEqual(config["ai_material_outreach_preface_goal"], "升温关系")
        self.assertEqual(config["ai_material_outreach_preface_intensity"], "积极主动")
        self.assertEqual(config["ai_material_outreach_allowed_sources"], ["素材源A"])
        self.assertEqual(config["ai_material_outreach_detection_interval_minutes"], 12)
        self.assertEqual(config["ai_material_outreach_detection_message_threshold"], 8)
        self.assertEqual(config["ai_material_outreach_daily_limit_per_friend"], 5)


if __name__ == "__main__":
    unittest.main()
