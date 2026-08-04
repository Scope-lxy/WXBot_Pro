import unittest
from unittest import mock

from core.api import (
    API_ERROR_REPLY_TEXT,
    DusAPI,
    OpenAIAPI,
    _api_log_label,
    build_api_config_snapshot,
    describe_api_error,
    format_api_error_log_message,
)
from core.logger import format_panel_log_message


class FakeProviderError(Exception):
    def __init__(self, status_code, message):
        super().__init__(message)
        self.status_code = status_code
        self.body = {'message': message}


class ApiLogLabelTest(unittest.TestCase):
    def test_api_log_label_uses_model_without_runtime_index(self):
        config = build_api_config_snapshot({"sdk": "DusAPI", "model": "mimo-v2.5"})

        self.assertEqual(_api_log_label(config, "DusAPI GPT"), "接口：mimo-v2.5")

    def test_same_status_code_uses_provider_message_to_distinguish_causes(self):
        image_error = FakeProviderError(
            400,
            "messages[1]: unknown variant `image_url`, expected `text`",
        )
        parameter_error = FakeProviderError(400, "invalid request parameter")

        self.assertIn("图片", describe_api_error(image_error))
        self.assertIn("参数", describe_api_error(parameter_error))
        self.assertNotEqual(describe_api_error(image_error), describe_api_error(parameter_error))

    def test_panel_api_error_is_readable_while_file_message_keeps_details(self):
        error = FakeProviderError(402, "Insufficient Balance")

        file_message = format_api_error_log_message("接口：test-model", error)
        panel_message = format_panel_log_message(file_message, level="WARNING")

        self.assertIn("余额", panel_message)
        self.assertIn("402", panel_message)
        self.assertNotIn("Insufficient Balance", panel_message)
        self.assertIn("Insufficient Balance", file_message)

    def test_openai_probe_can_suppress_expected_failure_log(self):
        api = OpenAIAPI.__new__(OpenAIAPI)
        api.config = build_api_config_snapshot(
            {"sdk": "OpenAI SDK", "model": "text-only", "api_protocol": "chat_completions"},
        )
        api.DS_NOW_MOD = api.config.model
        api.last_protocol_status = {"status": "unknown"}
        expected_error = FakeProviderError(
            400,
            "messages[1]: unknown variant `image_url`, expected `text`",
        )
        api._call_chat_completions_api = mock.Mock(side_effect=expected_error)

        with mock.patch("core.api.log") as fake_log:
            result = api.chat("test", log_errors=False)

        self.assertEqual(result, API_ERROR_REPLY_TEXT)
        self.assertIs(api.last_error, expected_error)
        fake_log.assert_not_called()

    def test_dusapi_probe_can_suppress_expected_failure_log(self):
        api = DusAPI(build_api_config_snapshot(
            {
                "sdk": "DusAPI",
                "key": "test-key",
                "url": "https://example.test",
                "model": "gpt-test",
            },
            max_retries=0,
        ))
        expected_error = FakeProviderError(429, "Rate limit exceeded")

        with mock.patch("core.api.requests.post", side_effect=expected_error), mock.patch(
            "core.api.log"
        ) as fake_log:
            result = api.chat("test", stream=False, log_errors=False)

        self.assertEqual(result, API_ERROR_REPLY_TEXT)
        self.assertIs(api.last_error, expected_error)
        fake_log.assert_not_called()


if __name__ == "__main__":
    unittest.main()
