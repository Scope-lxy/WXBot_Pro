import threading
import time
import tempfile
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest import mock

import web_server


class WebStopTests(unittest.TestCase):
    def test_runtime_health_exposes_only_local_operational_state(self):
        old_bot = web_server.bot
        old_thread = web_server.bot_thread
        try:
            web_server.bot = SimpleNamespace(
                _ui_owner=SimpleNamespace(is_running=True),
                is_stop_requested=lambda: False,
                _runtime_instance_id='a' * 32,
                _listener_auto_recovery_active=False,
                callback_is_die=False,
            )
            web_server.bot_thread = SimpleNamespace(is_alive=lambda: True)
            web_server._set_bot_startup_state('success', '机器人已启动')

            local = web_server.app.test_client().get('/runtime_health')
            remote = web_server.app.test_client().get(
                '/runtime_health', environ_base={'REMOTE_ADDR': '10.0.0.8'}
            )

            self.assertEqual(local.status_code, 200)
            self.assertEqual(local.get_json(), {
                'status': 'ok',
                'bot_running': True,
                'runtime_id': 'a' * 32,
            })
            web_server.bot._listener_auto_recovery_active = True
            recovering = web_server.app.test_client().get('/runtime_health')
            self.assertFalse(recovering.get_json()['bot_running'])
            web_server.bot._listener_auto_recovery_active = False
            web_server.bot.callback_is_die = True
            listener_dead = web_server.app.test_client().get('/runtime_health')
            self.assertFalse(listener_dead.get_json()['bot_running'])
            self.assertEqual(remote.status_code, 404)
            self.assertEqual(remote.get_json(), {'status': 'error'})
        finally:
            web_server.bot = old_bot
            web_server.bot_thread = old_thread
            web_server._set_bot_startup_state('idle', '机器人未启动')

    def test_scheduled_start_suppression_expires_without_blocking_manual_start(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            marker = Path(temp_dir) / "suppress.txt"
            marker.write_text("1300", encoding="utf-8")

            self.assertTrue(web_server._scheduled_start_is_suppressed(str(marker), now=1000))
            self.assertTrue(marker.exists())
            self.assertFalse(web_server._scheduled_start_is_suppressed(str(marker), now=1400))
            self.assertFalse(marker.exists())

    def test_async_stop_returns_before_robot_thread_finishes(self):
        release = threading.Event()
        started = threading.Event()

        def robot_worker():
            started.set()
            release.wait(1)

        robot_thread = threading.Thread(target=robot_worker)
        robot_thread.start()
        self.assertTrue(started.wait(1))
        fake_bot = mock.Mock()
        fake_bot.stop_wxbot.return_value = True

        old_bot = web_server.bot
        old_thread = web_server.bot_thread
        old_worker = web_server.bot_stop_worker
        try:
            web_server.bot = fake_bot
            web_server.bot_thread = robot_thread
            web_server.bot_stop_worker = None
            started_at = time.monotonic()
            ok, message = web_server._request_running_bot_stop_async()
            elapsed = time.monotonic() - started_at

            self.assertTrue(ok)
            self.assertEqual(message, "机器人正在停止")
            self.assertLess(elapsed, 0.2)
            self.assertTrue(robot_thread.is_alive())
            fake_bot.stop_wxbot.assert_called_once_with()
        finally:
            release.set()
            robot_thread.join(1)
            worker = web_server.bot_stop_worker
            if worker:
                worker.join(1)
            web_server.bot = old_bot
            web_server.bot_thread = old_thread
            web_server.bot_stop_worker = old_worker
            web_server.bot_stop_requested.clear()
            web_server._set_bot_startup_state('idle', '机器人未启动')


if __name__ == "__main__":
    unittest.main()
