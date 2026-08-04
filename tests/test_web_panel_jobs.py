import threading
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import web_server
from feature import friend_request, relationship_scan


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

    def test_dashboard_hides_coverage_diagnostics_but_shows_scan_failure(self):
        root = Path(__file__).resolve().parents[1]
        dashboard = (root / "templates" / "dashboard.html").read_text(encoding="utf-8")
        status_renderer = dashboard.split("function refreshStatus(){", 1)[1].split(
            "var STATUS_POLL_VISIBLE_MS", 1
        )[0]

        self.assertNotIn("scan_coverage_degraded", status_renderer)
        self.assertNotIn("运行中（覆盖降级）", status_renderer)
        self.assertIn("d.scan_coverage_status === 'failed'", status_renderer)
        self.assertIn("运行异常（消息扫描停止）", status_renderer)

    def test_dashboard_shows_contact_recovery_as_pending(self):
        root = Path(__file__).resolve().parents[1]
        dashboard = (root / "templates" / "dashboard.html").read_text(encoding="utf-8")
        status_renderer = dashboard.split("function refreshStatus(){", 1)[1].split(
            "var STATUS_POLL_VISIBLE_MS", 1
        )[0]

        self.assertIn("d.contact_recovery_active", status_renderer)
        self.assertIn("通讯录恢复中", status_renderer)

    def test_relationship_scan_actions_use_running_account_when_request_is_stale(self):
        running_wx_id = "wxid_running"
        stale_wx_id = "wxid_stale"
        fake_bot = SimpleNamespace(
            wx_id=running_wx_id,
            scan_relationship_sessions=mock.Mock(return_value={}),
            full_scan_relationship_sessions=mock.Mock(return_value={}),
        )
        fake_bot_thread = SimpleNamespace(is_alive=lambda: True)
        fake_full_scan_thread = SimpleNamespace(start=mock.Mock(), is_alive=lambda: True)

        with tempfile.TemporaryDirectory() as data_dir, mock.patch.object(
            web_server, "DATA_DIR", data_dir
        ), mock.patch.object(web_server, "bot", fake_bot), mock.patch.object(
            web_server, "bot_thread", fake_bot_thread
        ), mock.patch.object(
            web_server, "relationship_full_scan_thread", None
        ), mock.patch.object(
            web_server,
            "_start_panel_wechat_job",
            side_effect=lambda _name, target: target() is None,
        ), mock.patch.object(
            web_server.threading, "Thread", return_value=fake_full_scan_thread
        ) as thread_factory:
            client = web_server.app.test_client()
            with client.session_transaction() as session:
                session["logged_in"] = True

            current_response = client.post("/relationship_scan/scan", json={"wx_id": stale_wx_id})
            full_response = client.post("/relationship_scan/full_scan", json={"wx_id": stale_wx_id})
            saved = relationship_scan.load_state(data_dir, running_wx_id)

        self.assertEqual(current_response.status_code, 202)
        self.assertEqual(current_response.get_json()["payload"]["wx_id"], running_wx_id)
        fake_bot.scan_relationship_sessions.assert_called_once_with()
        self.assertEqual(full_response.status_code, 200)
        self.assertEqual(full_response.get_json()["payload"]["wx_id"], running_wx_id)
        thread_factory.assert_called_once_with(
            target=web_server._relationship_full_scan_worker,
            args=(fake_bot, running_wx_id),
            daemon=True,
        )
        fake_full_scan_thread.start.assert_called_once_with()
        self.assertTrue(saved["runtime"]["full_scan_running"])

    def test_relationship_scan_action_renders_server_error_and_response_account(self):
        root = Path(__file__).resolve().parents[1]
        dashboard = (root / "templates" / "dashboard.html").read_text(encoding="utf-8")
        action = dashboard.split("function runRelationshipScanAction(", 1)[1].split(
            "function friendRequestShortTime", 1
        )[0]

        self.assertIn("(res.payload || {}).wx_id", action)
        self.assertIn("_contactDirectoryBrowserState.wxId = responseWxId", action)
        self.assertIn("xhr.responseJSON && xhr.responseJSON.message", action)
        self.assertNotIn("xhr.responseText", action)

    def test_contact_profile_actions_use_running_account_and_drop_stale_cursor(self):
        running_wx_id = "wxid_running"
        stale_wx_id = "wxid_stale"
        fake_bot = SimpleNamespace(
            wx_id=running_wx_id,
            refresh_contact_profiles_batch=mock.Mock(return_value={"wx_id": running_wx_id}),
            set_contact_profiles_paused=mock.Mock(return_value={"wx_id": running_wx_id}),
            preview_contact_profile_remark_repairs=mock.Mock(return_value={"candidate_count": 0}),
            repair_contact_profile_remarks=mock.Mock(),
        )
        fake_bot_thread = SimpleNamespace(is_alive=lambda: True)

        with mock.patch.object(web_server, "bot", fake_bot), mock.patch.object(
            web_server, "bot_thread", fake_bot_thread
        ), mock.patch.object(
            web_server,
            "_contact_profiles_browser_payload",
            side_effect=lambda wx_id: {"wx_id": wx_id},
        ), mock.patch.object(
            web_server,
            "_start_panel_wechat_job",
            side_effect=lambda _name, target: target() is None,
        ):
            client = web_server.app.test_client()
            with client.session_transaction() as session:
                session["logged_in"] = True

            refresh_response = client.post("/contact_profiles/refresh_batch", json={
                "wx_id": stale_wx_id,
                "mode": "standard",
                "start_name": "旧账号游标",
            })
            pause_response = client.post("/contact_profiles/pause", json={
                "wx_id": stale_wx_id,
                "paused": True,
            })
            preview_response = client.get(
                "/contact_profiles/repair_preview",
                query_string={"wx_id": stale_wx_id},
            )
            repair_response = client.post(
                "/contact_profiles/repair_remarks",
                json={"wx_id": stale_wx_id},
            )

        self.assertEqual(refresh_response.status_code, 200)
        self.assertEqual(refresh_response.get_json()["browser"]["wx_id"], running_wx_id)
        fake_bot.refresh_contact_profiles_batch.assert_called_once_with(
            mode="standard",
            start_name="",
            interval=None,
            run_to_completion=True,
        )
        self.assertEqual(pause_response.get_json()["browser"]["wx_id"], running_wx_id)
        fake_bot.set_contact_profiles_paused.assert_called_once_with(True)
        self.assertEqual(preview_response.get_json()["wx_id"], running_wx_id)
        fake_bot.preview_contact_profile_remark_repairs.assert_called_once_with()
        self.assertEqual(repair_response.status_code, 202)
        self.assertEqual(repair_response.get_json()["wx_id"], running_wx_id)
        fake_bot.repair_contact_profile_remarks.assert_called_once_with()

    def test_active_action_waits_until_running_account_is_bound(self):
        fake_bot = SimpleNamespace(
            wx_id="",
            refresh_contact_profiles_batch=mock.Mock(),
        )
        fake_bot_thread = SimpleNamespace(is_alive=lambda: True)

        with mock.patch.object(web_server, "bot", fake_bot), mock.patch.object(
            web_server, "bot_thread", fake_bot_thread
        ):
            client = web_server.app.test_client()
            with client.session_transaction() as session:
                session["logged_in"] = True
            response = client.post(
                "/contact_profiles/refresh_batch",
                json={"wx_id": "wxid_history", "start_name": "历史游标"},
            )

        self.assertEqual(response.status_code, 400)
        self.assertIn("仍在连接微信", response.get_json()["message"])
        fake_bot.refresh_contact_profiles_batch.assert_not_called()

    def test_friend_request_run_once_saves_settings_to_running_account_before_action(self):
        running_wx_id = "wxid_running"
        stale_wx_id = "wxid_stale"
        events = []
        fake_bot = SimpleNamespace(
            wx_id=running_wx_id,
            run_friend_request_once=mock.Mock(
                side_effect=lambda **_kwargs: events.append(("run", running_wx_id))
            ),
        )
        fake_bot_thread = SimpleNamespace(is_alive=lambda: True)
        request_payload = {
            "wx_id": stale_wx_id,
            "settings": {"enabled": True, "daily_limit": 5},
            "message_rules": [],
        }

        def save_settings(wx_id, raw_data):
            events.append(("save", wx_id))
            self.assertEqual(raw_data, request_payload)
            return {"wx_id": wx_id}

        with mock.patch.object(web_server, "bot", fake_bot), mock.patch.object(
            web_server, "bot_thread", fake_bot_thread
        ), mock.patch.object(
            web_server, "_save_friend_request_settings", side_effect=save_settings
        ) as save_mock, mock.patch.object(
            web_server, "_friend_request_payload", side_effect=lambda wx_id: {"wx_id": wx_id}
        ), mock.patch.object(
            web_server,
            "_start_panel_wechat_job",
            side_effect=lambda _name, target: target() is None,
        ):
            client = web_server.app.test_client()
            with client.session_transaction() as session:
                session["logged_in"] = True
            response = client.post("/friend_request/run_once", json=request_payload)

        self.assertEqual(response.status_code, 202)
        self.assertEqual(response.get_json()["payload"]["wx_id"], running_wx_id)
        save_mock.assert_called_once_with(running_wx_id, request_payload)
        fake_bot.run_friend_request_once.assert_called_once_with(force=True)
        self.assertEqual(events, [("save", running_wx_id), ("run", running_wx_id)])

    def test_friend_request_run_once_does_not_execute_when_settings_save_fails(self):
        fake_bot = SimpleNamespace(
            wx_id="wxid_running",
            run_friend_request_once=mock.Mock(),
        )
        fake_bot_thread = SimpleNamespace(is_alive=lambda: True)

        with mock.patch.object(web_server, "bot", fake_bot), mock.patch.object(
            web_server, "bot_thread", fake_bot_thread
        ), mock.patch.object(
            web_server,
            "_save_friend_request_settings",
            side_effect=ValueError("设置保存失败"),
        ), mock.patch.object(web_server, "_start_panel_wechat_job") as start_job:
            client = web_server.app.test_client()
            with client.session_transaction() as session:
                session["logged_in"] = True
            response = client.post(
                "/friend_request/run_once",
                json={"wx_id": "wxid_stale", "settings": {"enabled": True}},
            )

        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.get_json()["message"], "设置保存失败")
        start_job.assert_not_called()
        fake_bot.run_friend_request_once.assert_not_called()

    def test_get_status_exposes_running_account(self):
        running_wx_id = "wxid_running"
        fake_bot = SimpleNamespace(
            wx_id=running_wx_id,
            get_status=mock.Mock(return_value={}),
            is_stop_requested=lambda: False,
            _material_runtime_messages={},
        )
        fake_bot_thread = SimpleNamespace(is_alive=lambda: True)

        with mock.patch.object(web_server, "bot", fake_bot), mock.patch.object(
            web_server, "bot_thread", fake_bot_thread
        ), mock.patch.object(
            web_server, "read_config", return_value={}
        ), mock.patch.object(
            web_server, "_inject_account_scoped_task_config", return_value={}
        ), mock.patch.object(
            web_server, "_dashboard_runtime_metrics_payload", return_value=None
        ), mock.patch.object(
            web_server,
            "_enrich_dashboard_status_snapshot",
            side_effect=lambda status, **_kwargs: dict(status),
        ):
            client = web_server.app.test_client()
            with client.session_transaction() as session:
                session["logged_in"] = True
            response = client.get("/get_status")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()["data"]["running_wx_id"], running_wx_id)

    def test_successful_startup_persists_running_account(self):
        with mock.patch.object(
            web_server,
            "_set_bot_startup_state",
            return_value={"status": "success", "message": "ready"},
        ), mock.patch.object(
            web_server, "_running_wx_id", return_value="wxid_running"
        ), mock.patch.object(web_server, "_write_last_wx_id") as write_last:
            result = web_server._report_bot_startup_state(True, "ready")

        self.assertEqual(result["status"], "success")
        write_last.assert_called_once_with("wxid_running")

    def test_dashboard_syncs_account_once_and_submits_friend_settings_with_run(self):
        root = Path(__file__).resolve().parents[1]
        dashboard = (root / "templates" / "dashboard.html").read_text(encoding="utf-8")
        status_renderer = dashboard.split("function refreshStatus(){", 1)[1].split(
            "var STATUS_POLL_VISIBLE_MS", 1
        )[0]
        friend_action = dashboard.split("function runFriendRequestAction(", 1)[1].split(
            "function updateMaterialOutreachRecordStats", 1
        )[0]
        friend_click = dashboard.split("$('#btn-friend-request-run-once').click", 1)[1].split(
            "$(document).on('change', '#friend_request_enabled", 1
        )[0]

        self.assertIn("runningWxId !== _lastRunningWxId", status_renderer)
        self.assertIn("syncContactViewsToRunningAccount(runningWxId)", status_renderer)
        self.assertIn("JSON.stringify(friendRequestSettingsPayload())", friend_action)
        self.assertIn("xhr.responseJSON && xhr.responseJSON.message", friend_action)
        self.assertNotIn("saveFriendRequestSettings", friend_click)
        self.assertNotIn(".always(function", friend_click)

    def test_group_rows_support_per_group_tts_selection(self):
        root = Path(__file__).resolve().parents[1]
        dashboard = (root / "templates" / "dashboard.html").read_text(encoding="utf-8")

        self.assertIn('class="form-select group-tts-select"', dashboard)
        self.assertIn("config.get('group_tts_map', {}).get(g)", dashboard)
        self.assertIn("group_tts_map:{}", dashboard)
        self.assertIn("configData.group_tts_map[name]=ttsIdx", dashboard)
        self.assertIn("$('.chat-tts-select, .group-tts-select')", dashboard)

    def test_chat_rule_dropdowns_are_capped_while_name_column_stays_flexible(self):
        root = Path(__file__).resolve().parents[1]
        css = (root / "templates" / "static" / "dashboard.css").read_text(encoding="utf-8")

        self.assertEqual(css.count("minmax(100px, 1fr) repeat(3, minmax(100px, 132px)) 36px"), 2)
        for selector in (
            ".chat-prompt-select",
            ".chat-api-select",
            ".chat-tts-select",
            ".group-prompt-select",
            ".group-api-select",
            ".group-tts-select",
        ):
            self.assertIn(f"{selector} {{ grid-area:", css)
            self.assertIn("max-width: 132px;", css.split(f"{selector} {{", 1)[1].split("}", 1)[0])

    def test_chat_list_and_settings_headers_have_alignment_descriptions(self):
        root = Path(__file__).resolve().parents[1]
        dashboard = (root / "templates" / "dashboard.html").read_text(encoding="utf-8")

        for description in (
            "设置私聊回复状态与免打扰会话过滤",
            "添加监听群聊，并为每个群配置专属人设与接口",
            "设置群聊回复范围、发送方式与入群欢迎",
        ):
            self.assertEqual(dashboard.count(f'<span class="desc">{description}</span>'), 1)

    def test_group_tts_map_validation_keeps_only_valid_bindings(self):
        config = {
            "group_tts_map": {
                " 测试群 ": "1",
                "": 2,
                "无效接口": "x",
                "负数接口": -1,
            }
        }

        web_server._coerce_dict_fields(config)

        self.assertEqual(config["group_tts_map"], {"测试群": 1})

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

    def test_group_quote_reply_is_a_visible_independent_switch(self):
        root = Path(__file__).resolve().parents[1]
        dashboard = (root / "templates" / "dashboard.html").read_text(encoding="utf-8")

        self.assertEqual(dashboard.count('id="group_reply_quote"'), 1)
        quote_control = dashboard.split('id="group_reply_quote"', 1)[0].rsplit("<div", 1)[-1]
        self.assertNotIn("hidden", quote_control)
        self.assertIn('<span class="switch-title">回复时引用</span>', dashboard)
        self.assertIn('<span class="switch-title">入群欢迎消息</span>', dashboard)
        self.assertNotIn("启用入群欢迎消息", dashboard)
        control_order = [
            dashboard.index('id="group_reply_at"'),
            dashboard.index('id="group_reply_at_msg"'),
            dashboard.index('id="group_welcome"'),
            dashboard.index('id="group_reply_quote"'),
        ]
        self.assertEqual(control_order, sorted(control_order))
        self.assertIn("group_reply_quote:$('#group_reply_quote').is(':checked')", dashboard)

    def test_keyword_reply_quote_and_group_mention_controls_are_saved(self):
        root = Path(__file__).resolve().parents[1]
        dashboard = (root / "templates" / "dashboard.html").read_text(encoding="utf-8")

        self.assertEqual(dashboard.count('id="chat_keyword_reply_quote"'), 1)
        self.assertEqual(dashboard.count('id="group_keyword_reply_quote"'), 1)
        self.assertEqual(dashboard.count('id="group_keyword_reply_at_msg"'), 1)
        self.assertIn(
            '<label for="chat_keyword_reply_quote"><span class="switch-title">回复时引用</span></label>',
            dashboard,
        )
        private_quote_row = dashboard.split(
            '<div class="switch-row private-keyword-trigger-dependent', 1
        )[1].split('</div>', 1)[0]
        self.assertIn('{% if not config.chat_keyword_switch %}is-muted{% endif %}', private_quote_row)
        self.assertIn('{% if not config.chat_keyword_switch %}disabled{% endif %}', private_quote_row)
        self.assertIn(
            '<label for="group_keyword_reply_quote"><span class="switch-title">回复时引用</span></label>',
            dashboard,
        )
        self.assertIn("<span class=\"switch-title\">仅被@时触发</span>", dashboard)
        self.assertIn("<span class=\"switch-title\">回复时@对方</span>", dashboard)
        self.assertEqual(dashboard.count('class="keyword-trigger-grid"'), 1)
        self.assertIn("chat_keyword_reply_quote:$('#chat_keyword_reply_quote').is(':checked')", dashboard)
        self.assertIn("group_keyword_reply_quote:$('#group_keyword_reply_quote').is(':checked')", dashboard)
        self.assertIn("group_keyword_reply_at_msg:$('#group_keyword_reply_at_msg').is(':checked')", dashboard)
        self.assertIn("var privateEnabled = $('#chat_keyword_switch').is(':checked');", dashboard)
        self.assertIn("$('.private-keyword-trigger-dependent').toggleClass('is-muted', !privateEnabled);", dashboard)
        self.assertIn("$('#chat_keyword_reply_quote').prop('disabled', !privateEnabled);", dashboard)
        self.assertIn("$('#chat_keyword_switch, #group_keyword_switch').on('change', syncKeywordTriggerControls);", dashboard)
        grid_position = dashboard.index('<div class="keyword-trigger-grid">')
        private_quote_position = dashboard.index('id="chat_keyword_reply_quote"')
        divider_position = dashboard.index(
            '<div class="detail-config-separator" aria-hidden="true"></div>',
            private_quote_position,
        )
        group_switch_position = dashboard.index('id="group_keyword_switch"')
        self.assertLess(private_quote_position, divider_position)
        self.assertLess(divider_position, group_switch_position)
        trigger_control_order = [
            dashboard.index('id="chat_keyword_switch"'),
            dashboard.index('id="chat_keyword_reply_quote"'),
            dashboard.index('id="group_keyword_switch"'),
            dashboard.index('id="group_keyword_at_only"'),
            dashboard.index('id="group_keyword_reply_quote"'),
            dashboard.index('id="group_keyword_reply_at_msg"'),
        ]
        self.assertTrue(all(position > grid_position for position in trigger_control_order))
        self.assertEqual(trigger_control_order, sorted(trigger_control_order))
        styles = (root / "templates" / "static" / "dashboard.css").read_text(encoding="utf-8")
        self.assertIn(
            ".keyword-trigger-grid .detail-config-separator { grid-column: 1 / -1; margin: 0; }",
            styles,
        )
        self.assertIn(
            ".private-keyword-trigger-dependent.is-muted { opacity: .58; }",
            styles,
        )
        self.assertEqual(
            [
                dashboard.index('id="group_keyword_switch"'),
                dashboard.index('id="group_keyword_at_only"'),
                dashboard.index('id="group_keyword_reply_quote"'),
                dashboard.index('id="group_keyword_reply_at_msg"'),
            ],
            sorted([
                dashboard.index('id="group_keyword_switch"'),
                dashboard.index('id="group_keyword_at_only"'),
                dashboard.index('id="group_keyword_reply_at_msg"'),
                dashboard.index('id="group_keyword_reply_quote"'),
            ]),
        )

        config = {
            "chat_keyword_reply_quote": "true",
            "group_keyword_reply_quote": "true",
            "group_keyword_reply_at_msg": "false",
        }
        web_server._coerce_bool_fields(config)
        self.assertIs(config["chat_keyword_reply_quote"], True)
        self.assertIs(config["group_keyword_reply_quote"], True)
        self.assertIs(config["group_keyword_reply_at_msg"], False)

    def test_update_check_runs_when_dashboard_opens(self):
        root = Path(__file__).resolve().parents[1]
        dashboard = (root / "templates" / "dashboard.html").read_text(encoding="utf-8")

        initial_check = dashboard.index("  checkUpdate(false);\n  setInterval(function(){ checkUpdate(false); }")
        self.assertGreater(initial_check, dashboard.index("function checkUpdate(manual)"))

    def test_wxautox4_compatibility_only_warns_below_verified_version(self):
        self.assertTrue(web_server._wxautox_compatibility_status("41.1.1")["needs_upgrade"])
        self.assertFalse(web_server._wxautox_compatibility_status("41.1.1.post1")["needs_upgrade"])
        self.assertFalse(web_server._wxautox_compatibility_status("41.1.2")["needs_upgrade"])

    def test_ui_stall_recovery_only_schedules_one_automatic_bot_start(self):
        class ImmediateTimer:
            def __init__(self, _delay, target):
                self._target = target
                self.daemon = False

            def start(self):
                self._target()

        previous = web_server.ui_stall_recovery_start_scheduled
        web_server.ui_stall_recovery_start_scheduled = False
        try:
            self.assertTrue(web_server._ui_stall_recovery_requested([
                "web_server.py", web_server.UI_STALL_RECOVERY_ARGUMENT,
            ]))
            self.assertFalse(web_server._ui_stall_recovery_requested(["web_server.py"]))
            with mock.patch.object(web_server, "_ui_stall_recovery_requested", return_value=True), mock.patch.object(
                web_server.threading, "Timer", ImmediateTimer
            ), mock.patch.object(web_server, "_auto_start_bot_after_ui_stall") as auto_start:
                self.assertTrue(web_server._schedule_bot_start_after_ui_stall())
                self.assertFalse(web_server._schedule_bot_start_after_ui_stall())
            auto_start.assert_called_once_with()
        finally:
            web_server.ui_stall_recovery_start_scheduled = previous

    def test_ui_stall_recovery_auto_start_uses_normal_bot_start_path(self):
        with mock.patch.object(web_server, "bot_thread", None), mock.patch.object(
            web_server, "_start_bot_runtime", return_value={"status": "success"}
        ) as start_runtime:
            web_server._auto_start_bot_after_ui_stall()

        start_runtime.assert_called_once_with(wait_timeout=web_server.BOT_START_WAIT_TIMEOUT_SECONDS)

    def test_split_reply_delay_dropdown_values_are_coerced_to_booleans(self):
        config = {
            "chat_split_reply_delay_switch": "false",
            "group_split_reply_delay_switch": "true",
        }

        web_server._coerce_bool_fields(config)

        self.assertIs(config["chat_split_reply_delay_switch"], False)
        self.assertIs(config["group_split_reply_delay_switch"], True)

    def test_context_repair_has_private_and_group_switches_in_recognition_cards(self):
        root = Path(__file__).resolve().parents[1]
        dashboard = (root / "templates" / "dashboard.html").read_text(encoding="utf-8")

        self.assertNotIn('id="memory_context_repair_switch"', dashboard)
        self.assertEqual(dashboard.count('id="chat_context_repair_switch"'), 1)
        self.assertEqual(dashboard.count('id="group_context_repair_switch"'), 1)
        private_panel = dashboard.index('id="tab-listen"')
        group_panel = dashboard.index('id="tab-group"')
        memory_panel = dashboard.index('id="tab-memory"')
        self.assertLess(private_panel, dashboard.index('id="chat_context_repair_switch"'))
        self.assertLess(dashboard.index('id="chat_context_repair_switch"'), group_panel)
        self.assertLess(group_panel, dashboard.index('id="group_context_repair_switch"'))
        self.assertLess(dashboard.index('id="group_context_repair_switch"'), memory_panel)
        self.assertIn("chat_context_repair_switch:$('#chat_context_repair_switch').is(':checked')", dashboard)
        self.assertIn("group_context_repair_switch:$('#group_context_repair_switch').is(':checked')", dashboard)

        config = {
            "chat_context_repair_switch": "false",
            "group_context_repair_switch": "true",
        }
        web_server._coerce_bool_fields(config)
        self.assertIs(config["chat_context_repair_switch"], False)
        self.assertIs(config["group_context_repair_switch"], True)

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
