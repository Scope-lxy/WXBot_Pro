import threading
import time
import unittest
from pathlib import Path

import web_server


class WebPanelJobTests(unittest.TestCase):
    def tearDown(self):
        for worker in list(web_server.panel_wechat_jobs.values()):
            worker.join(1)
        web_server.panel_wechat_jobs.clear()

    def test_dashboard_uses_bundled_jquery_for_offline_controls(self):
        root = Path(__file__).resolve().parents[1]
        dashboard = (root / "templates" / "dashboard.html").read_text(encoding="utf-8")
        jquery = root / "templates" / "static" / "jquery-3.6.0.min.js"

        self.assertTrue(jquery.is_file())
        self.assertIn("url_for('static', filename='jquery-3.6.0.min.js')", dashboard)
        self.assertNotIn("npm/jquery@3.6.0", dashboard)

    def test_split_reply_delay_controls_live_only_in_private_and_group_cards(self):
        root = Path(__file__).resolve().parents[1]
        dashboard = (root / "templates" / "dashboard.html").read_text(encoding="utf-8")

        self.assertEqual(dashboard.count('id="chat_split_reply_delay_switch"'), 1)
        self.assertEqual(dashboard.count('id="group_split_reply_delay_switch"'), 1)
        self.assertNotIn("reply_delay_split_speed_mode", dashboard)
        self.assertNotIn("reply_delay_first_min", dashboard)
        self.assertNotIn("reply_delay_first_max", dashboard)
        self.assertEqual(dashboard.count("开启后，第二条及后续气泡会自然停顿"), 2)
        self.assertNotIn("AI 已经显式换行拆好的内容会原样发送", dashboard)

    def test_split_reply_delay_dropdown_values_are_coerced_to_booleans(self):
        config = {
            "chat_split_reply_delay_switch": "false",
            "group_split_reply_delay_switch": "true",
        }

        web_server._coerce_bool_fields(config)

        self.assertIs(config["chat_split_reply_delay_switch"], False)
        self.assertIs(config["group_split_reply_delay_switch"], True)

    def test_text_reply_limits_live_in_private_and_group_reply_cards(self):
        root = Path(__file__).resolve().parents[1]
        dashboard = (root / "templates" / "dashboard.html").read_text(encoding="utf-8")

        self.assertEqual(dashboard.count('id="chat_text_reply_limit_switch"'), 1)
        self.assertEqual(dashboard.count('id="group_text_reply_limit_switch"'), 1)
        self.assertNotIn('id="text_reply_limit_switch"', dashboard)
        self.assertIn('id="chat_text_reply_limit_settings" class="chat-ability-settings"', dashboard)
        self.assertIn('id="group_text_reply_limit_settings" class="chat-ability-settings"', dashboard)
        self.assertNotIn("$(prefix + 'settings').toggle", dashboard)
        private_panel = dashboard.index('id="tab-listen"')
        group_panel = dashboard.index('id="tab-group"')
        other_panel = dashboard.index('id="tab-other"')
        private_limit = dashboard.index('id="chat_text_reply_limit_switch"', private_panel)
        private_voice = dashboard.index('id="chat_voice_reply_switch"', private_panel)
        private_split = dashboard.index('id="chat_split_reply_switch"', private_panel)
        private_merge = dashboard.index('id="chat_message_merge_settings"', private_panel)
        group_limit = dashboard.index('id="group_text_reply_limit_switch"', group_panel)
        group_voice = dashboard.index('id="group_voice_reply_switch"', group_panel)
        group_split = dashboard.index('id="group_split_reply_switch"', group_panel)
        self.assertLess(private_panel, private_limit)
        self.assertLess(private_limit, private_voice)
        self.assertLess(private_voice, private_split)
        self.assertLess(private_split, private_merge)
        self.assertLess(group_panel, group_limit)
        self.assertLess(group_limit, group_voice)
        self.assertLess(group_voice, group_split)
        self.assertNotIn("回复次数限制", dashboard[other_panel:dashboard.index('id="tab-account"')])

    def test_panel_job_returns_immediately_while_work_continues(self):
        release = threading.Event()
        started = threading.Event()

        def work():
            started.set()
            release.wait(1)

        started_at = time.monotonic()
        queued = web_server._start_panel_wechat_job('slow-test', work)
        elapsed = time.monotonic() - started_at

        self.assertTrue(queued)
        self.assertTrue(started.wait(0.2))
        self.assertLess(elapsed, 0.2)
        self.assertTrue(web_server.panel_wechat_jobs['slow-test'].is_alive())
        release.set()

    def test_same_panel_job_cannot_start_twice(self):
        release = threading.Event()
        started = threading.Event()

        def work():
            started.set()
            release.wait(1)

        self.assertTrue(web_server._start_panel_wechat_job('single-test', work))
        self.assertTrue(started.wait(0.2))
        self.assertFalse(web_server._start_panel_wechat_job('single-test', work))
        release.set()


if __name__ == '__main__':
    unittest.main()
