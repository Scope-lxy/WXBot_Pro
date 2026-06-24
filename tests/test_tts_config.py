import json
import unittest

from core.tts import create_tts_client, list_tts_model_options


class TTSConfigTests(unittest.TestCase):
    def test_volcengine_lists_doubao_1_and_2_resources(self):
        options = list_tts_model_options("volcengine_openspeech")
        labels_by_key = {item["key"]: item["label"] for item in options}

        self.assertEqual(
            [item["key"] for item in options],
            [
                "doubao_tts_2_0_standard",
                "doubao_tts_2_0_expressive",
                "doubao_tts_1_0",
                "doubao_icl_2_0",
            ],
        )
        self.assertEqual(labels_by_key["doubao_tts_2_0_standard"], "豆包语音合成 2.0 标准版（seed-tts-2.0-standard）")
        self.assertEqual(labels_by_key["doubao_tts_2_0_expressive"], "豆包语音合成 2.0 高表现力版（seed-tts-2.0-expressive）")
        self.assertEqual(labels_by_key["doubao_tts_1_0"], "豆包语音合成 1.0（seed-tts-1.0）")
        self.assertEqual(labels_by_key["doubao_icl_2_0"], "豆包复刻/设计音色 2.0（seed-icl-2.0）")

    def test_doubao_1_0_uses_seed_tts_1_resource_header(self):
        client = create_tts_client(
            {
                "sdk": "volcengine_openspeech",
                "model": "doubao_tts_1_0",
                "credentials": {"api_key": "test-key"},
                "voice_id": "zh_male_junlangnanyou_emo_v2_mars_bigtts",
            }
        )

        self.assertEqual(client._headers()["X-Api-Resource-Id"], "seed-tts-1.0")

    def test_doubao_icl_2_0_uses_seed_icl_resource_header(self):
        client = create_tts_client(
            {
                "sdk": "volcengine_openspeech",
                "model": "doubao_icl_2_0",
                "credentials": {"api_key": "test-key"},
                "voice_id": "S_custom_voice",
                "context_text": "用户刚说想听语音",
                "section_id": "section-1",
            }
        )

        payload = client._payload("你好")
        self.assertEqual(client._headers()["X-Api-Resource-Id"], "seed-icl-2.0")
        self.assertNotIn("model", payload["req_params"])
        self.assertNotIn("additions", payload["req_params"])

    def test_doubao_2_0_expressive_keeps_context_additions(self):
        client = create_tts_client(
            {
                "sdk": "volcengine_openspeech",
                "model": "doubao_tts_2_0_expressive",
                "credentials": {"api_key": "test-key"},
                "voice_id": "zh_female_test",
                "context_text": "用户刚说想听语音",
                "section_id": "section-1",
            }
        )

        payload = client._payload("你好")
        additions = json.loads(payload["req_params"]["additions"])
        self.assertEqual(payload["req_params"]["model"], "seed-tts-2.0-expressive")
        self.assertEqual(additions["context_texts"], ["用户刚说想听语音"])
        self.assertEqual(additions["section_id"], "section-1")

    def test_doubao_2_0_standard_omits_context_additions(self):
        client = create_tts_client(
            {
                "sdk": "volcengine_openspeech",
                "model": "doubao_tts_2_0_standard",
                "credentials": {"api_key": "test-key"},
                "voice_id": "zh_female_test",
                "context_text": "用户刚说想听语音",
                "section_id": "section-1",
            }
        )

        payload = client._payload("你好")
        self.assertEqual(payload["req_params"]["model"], "seed-tts-2.0-standard")
        self.assertNotIn("additions", payload["req_params"])

    def test_doubao_1_0_omits_unsupported_context_additions(self):
        client = create_tts_client(
            {
                "sdk": "volcengine_openspeech",
                "model": "doubao_tts_1_0",
                "credentials": {"api_key": "test-key"},
                "voice_id": "zh_male_junlangnanyou_emo_v2_mars_bigtts",
                "context_text": "用户刚说想听语音",
                "section_id": "section-1",
            }
        )

        self.assertNotIn("additions", client._payload("你好")["req_params"])


if __name__ == "__main__":
    unittest.main()
