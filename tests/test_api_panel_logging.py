import unittest
from unittest import mock

import web_server
from core.api import API_ERROR_REPLY_TEXT


class FakeImageCapabilityError(Exception):
    status_code = 400
    body = {
        'message': "messages[1]: unknown variant `image_url`, expected `text`",
    }


class FakeApiClient:
    def __init__(self):
        self.calls = 0
        self.last_error = None
        self.last_protocol_status = {'status': 'chat_completions_ok'}
        self.image_log_errors = None

    def chat(self, *_args, **kwargs):
        self.calls += 1
        if self.calls == 1:
            return 'OK'
        self.image_log_errors = kwargs.get('log_errors')
        self.last_error = FakeImageCapabilityError()
        return API_ERROR_REPLY_TEXT


class ApiPanelLoggingTest(unittest.TestCase):
    def test_text_only_model_probe_logs_capability_result_without_raw_json(self):
        api = FakeApiClient()
        payload = {
            'api_id': 'api_test',
            'api_config': {
                'id': 'api_test',
                'sdk': 'OpenAI SDK',
                'key': 'test-key',
                'url': 'https://example.test/v1',
                'model': 'text-only-model',
                'api_protocol': 'chat_completions',
            },
        }

        with mock.patch.object(web_server, '_build_test_api_client', return_value=api), mock.patch.object(
            web_server, '_persist_api_capability', return_value=True
        ), mock.patch.object(web_server, 'log') as fake_log:
            client = web_server.app.test_client()
            with client.session_transaction() as session:
                session['logged_in'] = True
            response = client.post('/test_api_config', json=payload)

        result = response.get_json()
        self.assertEqual(response.status_code, 200)
        self.assertEqual(result['status'], 'success')
        self.assertEqual(result['data']['image_test']['status'], 'error')
        self.assertFalse(api.image_log_errors)

        messages = [call.args[1] for call in fake_log.call_args_list]
        self.assertTrue(any('文本可用' in message and '图片不可用' in message for message in messages))
        self.assertFalse(any('unknown variant' in message or "'error':" in message for message in messages))


if __name__ == '__main__':
    unittest.main()
