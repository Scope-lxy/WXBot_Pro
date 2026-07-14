import threading
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import web_server
from feature import friend_request


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

    def test_friend_request_clear_executions_route_preserves_duplicate_index(self):
        wx_id = "wxid_test"
        with tempfile.TemporaryDirectory() as data_dir:
            state = friend_request.default_state(wx_id)
            candidate = friend_request.normalize_candidate({
                "candidate_id": "contact-1",
                "display_name": "测试好友",
                "send_name": "测试好友",
                "tags": ["删除我的人"],
                "status": "sent",
            })
            state["candidates"] = [candidate]
            state["executions"] = [{
                "at": "2026-06-11T09:30:00",
                "candidate_id": candidate["candidate_id"],
                "status": "sent",
            }]
            friend_request.save_state(data_dir, state)

            with mock.patch.object(web_server, "DATA_DIR", data_dir), mock.patch.object(
                web_server,
                "_friend_request_wx_id_from_request",
                return_value=wx_id,
            ):
                client = web_server.app.test_client()
                with client.session_transaction() as session:
                    session["logged_in"] = True
                response = client.delete("/friend_request/executions", json={"wx_id": wx_id})

            payload = response.get_json()
            saved = friend_request.load_state(data_dir, wx_id)
            self.assertEqual(response.status_code, 200)
            self.assertEqual(payload["status"], "success")
            self.assertEqual(payload["payload"]["wx_id"], wx_id)
            self.assertEqual(payload["payload"]["executions"], [])
            self.assertEqual(saved["executions"], [])
            self.assertEqual(
                saved["runtime"]["recent_sent_at_by_candidate"][candidate["candidate_id"]],
                "2026-06-11T09:30:00",
            )

    def test_material_clear_route_only_clears_selected_account_pool(self):
        wx_id = "wxid_material"
        other_wx_id = "wxid_other"
        material = {
            "id": "mat_1",
            "source": "文件传输助手",
            "type": "link",
            "content_preview": "测试素材",
            "stable_signature": "link|测试素材",
        }
        history = {
            "send_records": [{"task_id": "task_1", "success": True}],
            "skip_records": [],
            "progress_records": [],
        }
        fake_bot = SimpleNamespace(
            wx_id=wx_id,
            _material_runtime_messages={"mat_1": object()},
        )

        with tempfile.TemporaryDirectory() as data_dir, mock.patch.object(
            web_server,
            "DATA_DIR",
            data_dir,
        ), mock.patch.object(
            web_server,
            "_material_outreach_wx_id_from_request",
            return_value=wx_id,
        ), mock.patch.object(web_server, "bot", fake_bot):
            web_server._save_material_outreach_materials([material], wx_id)
            web_server._save_material_outreach_materials([material], other_wx_id)
            web_server._save_material_outreach_history(history, wx_id)
            client = web_server.app.test_client()
            with client.session_transaction() as session:
                session["logged_in"] = True

            response = client.delete("/material_outreach/materials", json={"wx_id": wx_id})

            payload = response.get_json()
            self.assertEqual(response.status_code, 200)
            self.assertEqual(payload["status"], "success")
            self.assertEqual(payload["removed_count"], 1)
            self.assertEqual(payload["materials"], [])
            self.assertEqual(payload["stats"]["materials_total"], 0)
            self.assertEqual(web_server._load_material_outreach_materials(wx_id), [])
            self.assertEqual(len(web_server._load_material_outreach_materials(other_wx_id)), 1)
            self.assertEqual(web_server._load_material_outreach_history(wx_id)["send_records"], history["send_records"])
            self.assertEqual(fake_bot._material_runtime_messages, {})

    def test_material_clear_button_only_appears_on_material_management_tab(self):
        root = Path(__file__).resolve().parents[1]
        dashboard = (root / "templates" / "dashboard.html").read_text(encoding="utf-8")

        self.assertEqual(dashboard.count('id="btn-clear-materials"'), 1)
        self.assertIn("$('#btn-clear-materials').prop('hidden', tabName !== 'materials');", dashboard)
        self.assertIn("url:'/material_outreach/materials'", dashboard)

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

    def test_reply_limit_defaults_match_the_panel_and_save_validation(self):
        root = Path(__file__).resolve().parents[1]
        dashboard = (root / "templates" / "dashboard.html").read_text(encoding="utf-8")
        expected_defaults = {
            "chat_text_reply_limit_count": 50,
            "chat_text_reply_limit_hours": 5,
            "group_text_reply_limit_count": 50,
            "group_text_reply_limit_hours": 5,
            "chat_voice_reply_limit_count": 5,
            "chat_voice_reply_limit_hours": 5,
            "group_voice_reply_limit_count": 5,
            "group_voice_reply_limit_hours": 5,
        }

        for field, default in expected_defaults.items():
            self.assertIn(f"config.get('{field}', {default})", dashboard)

        invalid_config = {field: "invalid" for field in expected_defaults}
        web_server._coerce_int_range_fields(invalid_config)
        for field, default in expected_defaults.items():
            self.assertEqual(invalid_config[field], default)

        voice_config = {}
        web_server.normalize_voice_reply_config(voice_config)
        for field, default in expected_defaults.items():
            if "voice" in field:
                self.assertEqual(voice_config[field], default)

    def test_contact_directory_columns_match_wechat_profile_fields(self):
        root = Path(__file__).resolve().parents[1]
        dashboard = (root / "templates" / "dashboard.html").read_text(encoding="utf-8")
        renderer = dashboard.split("function renderContactDirectoryContactRow(contact){", 1)[1].split(
            "function updateContactDirectoryContactList(){", 1
        )[0]

        self.assertIn(
            '<div class="contact-directory-column-header">\n'
            '            <span>备注</span>\n'
            '            <span>昵称</span>\n'
            '            <span>微信号</span>',
            dashboard,
        )
        self.assertLess(renderer.index("contact.remark"), renderer.index("contact.nickname"))
        self.assertLess(renderer.index("contact.nickname"), renderer.index("contact.wechat_id"))
        self.assertNotIn("contact.name", renderer)

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
