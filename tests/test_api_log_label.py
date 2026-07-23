import unittest

from core.api import _api_log_label, build_api_config_snapshot


class ApiLogLabelTest(unittest.TestCase):
    def test_api_log_label_uses_model_without_runtime_index(self):
        config = build_api_config_snapshot({"sdk": "DusAPI", "model": "mimo-v2.5"})

        self.assertEqual(_api_log_label(config, "DusAPI GPT"), "接口：mimo-v2.5")


if __name__ == "__main__":
    unittest.main()
