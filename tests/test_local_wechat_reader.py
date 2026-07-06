import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from core.local_wechat_reader import (
    LocalWechatCommandResult,
    check_wechat_cli_status,
    check_wechat_cli_update,
    ensure_wechat_cli_account_ready,
    normalize_wechat_cli_contact,
    parse_wechat_cli_history_line,
    read_local_contacts_with_status,
    read_local_history_messages_with_status,
    read_local_sessions_with_status,
    save_wechat_cli_account_binding,
    verify_wechat_cli_live_binding,
    wechat_cli_account_matches,
    wechat_cli_integration_enabled,
)


class LocalWechatReaderTests(unittest.TestCase):
    def setUp(self):
        self._enabled_patch = patch("core.local_wechat_reader.wechat_cli_integration_enabled", return_value=True)
        self._enabled_patch.start()

    def tearDown(self):
        self._enabled_patch.stop()

    def test_normalize_contact_uses_existing_contact_fields_only(self):
        contact = normalize_wechat_cli_contact({
            "username": "wxid_abc",
            "nick_name": "阿英2",
            "remark": "A0-阿英2",
            "alias": "aying2",
        })

        self.assertEqual(contact["微信号"], "aying2")
        self.assertEqual(contact["wxid"], "wxid_abc")
        self.assertEqual(contact["昵称"], "阿英2")
        self.assertEqual(contact["备注"], "A0-阿英2")
        self.assertNotIn("source", contact)

    def test_parse_text_history_line(self):
        message = parse_wechat_cli_history_line("[2026-07-04 04:34] LXYou: 测试测试", chat_name="LXYou")

        self.assertEqual(message.type, "text")
        self.assertEqual(message.attr, "friend")
        self.assertEqual(message.sender, "LXYou")
        self.assertEqual(message.content, "测试测试")
        self.assertEqual(message.time, "2026/07/04 04:34:00")

    def test_parse_voice_history_line_hides_raw_xml(self):
        message = parse_wechat_cli_history_line(
            '[2026-07-04 04:34] LXYou: [语音] <msg><voicemsg voicelength="123" /></msg>',
            chat_name="LXYou",
        )

        self.assertEqual(message.type, "voice")
        self.assertEqual(message.content, "一条语音消息（未识别出文字）")
        self.assertNotIn("voicemsg", message.content)

    def test_parse_card_and_forwarded_history_lines(self):
        card = parse_wechat_cli_history_line(
            '[2026-07-04 04:36] LXYou: [名片] <?xml version="1.0"?><msg nickname="未白镇" />',
            chat_name="LXYou",
        )
        forwarded = parse_wechat_cli_history_line(
            "[2026-07-04 04:36] LXYou: [链接/文件] LXYou与赖书炳的聊天记录",
            chat_name="LXYou",
        )

        self.assertEqual(card.type, "personal_card")
        self.assertEqual(card.content, "[名片] 未白镇")
        self.assertEqual(forwarded.type, "merge")
        self.assertEqual(forwarded.content, "[聊天记录] LXYou与赖书炳的聊天记录")

    def test_parse_miniapp_history_line(self):
        message = parse_wechat_cli_history_line(
            "[2026-07-04 04:36] LXYou: [小程序] 写一封浪漫情书",
            chat_name="LXYou",
        )

        self.assertEqual(message.type, "miniapp")
        self.assertEqual(message.content, "[小程序] 写一封浪漫情书")

    def test_parse_wechat_cli_history_type_matrix(self):
        cases = [
            ("[表情] [微笑]", "emotion", "[表情] [微笑]"),
            ("[视频] 周末活动.mp4", "video", "[视频] 周末活动.mp4"),
            ("[文件] 报价单.pdf", "file", "[文件] 报价单.pdf"),
            ("[位置] 福建省福州市鼓楼区", "location", "[位置] 福建省福州市鼓楼区"),
            ("[笔记] 复盘重点", "note", "[笔记] 复盘重点"),
            ("[收藏] 重要资料", "note", "[收藏] 重要资料"),
            ("[聊天记录] A与B的聊天记录", "merge", "[聊天记录] A与B的聊天记录"),
            ("[合并转发] A与B的聊天记录", "merge", "[聊天记录] A与B的聊天记录"),
            ("[链接/文件] A与B的聊天记录", "merge", "[聊天记录] A与B的聊天记录"),
            ("[链接/文件] 位置：福建省福州市鼓楼区", "location", "[位置] 福建省福州市鼓楼区"),
            ("[链接/文件] 笔记：复盘重点", "note", "[笔记] 复盘重点"),
            ("[链接/文件] 腾讯新闻", "link", "[链接/文件] 腾讯新闻"),
        ]

        for body, expected_type, expected_content in cases:
            with self.subTest(body=body):
                message = parse_wechat_cli_history_line(
                    f"[2026-07-04 04:36] LXYou: {body}",
                    chat_name="LXYou",
                )

                self.assertEqual(message.type, expected_type)
                self.assertEqual(message.content, expected_content)

    def test_parse_link_or_file_keeps_ambiguous_titles_as_link(self):
        cases = [
            "[链接/文件] 如何恢复聊天记录",
            "[链接/文件] 位置营销活动复盘",
            "[链接/文件] 收藏夹整理技巧",
        ]

        for body in cases:
            with self.subTest(body=body):
                message = parse_wechat_cli_history_line(
                    f"[2026-07-04 04:36] LXYou: {body}",
                    chat_name="LXYou",
                )

                self.assertEqual(message.type, "link")
                self.assertEqual(message.content, body)

    def test_parse_card_without_display_name_hides_raw_xml(self):
        message = parse_wechat_cli_history_line(
            '[2026-07-04 04:36] LXYou: [名片] <msg username="wxid_abc" />',
            chat_name="LXYou",
        )

        self.assertEqual(message.type, "personal_card")
        self.assertEqual(message.content, "[名片]")
        self.assertNotIn("<msg", message.content)

    def test_contacts_status_reports_command_failure(self):
        with patch("core.local_wechat_reader.run_wechat_cli_json") as run:
            run.return_value.ok = False
            run.return_value.data = None
            run.return_value.error = "boom"

            result = read_local_contacts_with_status(limit=10)

        self.assertFalse(result.ok)
        self.assertEqual(result.items, [])
        self.assertIn("boom", result.error)

    def test_contacts_reader_fetches_extra_raw_rows_for_10000_friend_limit(self):
        with patch("core.local_wechat_reader.run_wechat_cli_json") as run:
            run.return_value = LocalWechatCommandResult(True, data=[])

            result = read_local_contacts_with_status(limit=10000)

        self.assertTrue(result.ok)
        self.assertEqual(run.call_args.args[0], ["contacts", "--limit", "30000"])

    def test_contacts_reader_filters_non_friend_directory_entries(self):
        with patch("core.local_wechat_reader.run_wechat_cli_json") as run:
            run.return_value = LocalWechatCommandResult(True, data=[
                {"username": "wxid_friend", "nick_name": "阿英2", "remark": "A0-阿英2"},
                {"username": "12345@chatroom", "nick_name": "测试群", "remark": ""},
                {"username": "25984984907045214@openim", "nick_name": "企业微信联系人", "remark": ""},
                {"username": "filehelper", "nick_name": "文件传输助手", "remark": ""},
                {"username": "gh_158599a58f81", "nick_name": "公众号", "remark": ""},
                {"username": "weixinguanhaozhushou", "nick_name": "微信公众平台", "remark": ""},
                {"username": "wxid_empty_nick", "nick_name": "", "remark": ""},
            ])

            result = read_local_contacts_with_status(limit=10)

        self.assertTrue(result.ok)
        self.assertEqual([item["wxid"] for item in result.items], ["wxid_friend"])

    def test_read_local_sessions_maps_last_message_and_filters_groups(self):
        with patch("core.local_wechat_reader.run_wechat_cli_json") as run:
            run.return_value = LocalWechatCommandResult(True, data=[
                {
                    "chat": "阿英2",
                    "username": "wxid_abc",
                    "is_group": False,
                    "last_message": "消息已发出，但被对方拒收了。",
                    "msg_type": "系统",
                    "time": "07-04 07:52",
                },
                {
                    "chat": "测试群",
                    "username": "123@chatroom",
                    "is_group": True,
                    "last_message": "普通消息",
                    "msg_type": "文本",
                    "time": "07-04 07:53",
                },
            ])

            result = read_local_sessions_with_status(limit=10)

        self.assertTrue(result.ok)
        self.assertEqual(len(result.items), 1)
        self.assertEqual(result.items[0]["name"], "阿英2")
        self.assertEqual(result.items[0]["content"], "消息已发出，但被对方拒收了。")
        self.assertEqual(result.items[0]["info"], "系统")

    def test_group_history_resolves_target_from_sessions(self):
        def fake_run(args, **_kwargs):
            if args and args[0] == "sessions":
                return LocalWechatCommandResult(True, data=[
                    {"chat": "测试群", "username": "123@chatroom", "is_group": True},
                ])
            if args and args[0] == "history":
                self.assertEqual(args[1], "123@chatroom")
                return LocalWechatCommandResult(True, data={
                    "messages": ["[2026-07-04 04:34] A: 群消息"]
                })
            return LocalWechatCommandResult(False, error="unexpected")

        with (
            patch("core.local_wechat_reader.ensure_wechat_cli_account_ready", return_value=(True, "")),
            patch("core.local_wechat_reader.run_wechat_cli_json", side_effect=fake_run),
        ):
            result = read_local_history_messages_with_status(
                "测试群",
                limit=10,
                expected_wx_id="wxid_current",
                chat_type="group",
            )

        self.assertTrue(result.ok)
        self.assertEqual(result.items[0].sender, "A")
        self.assertEqual(result.items[0].content, "群消息")

    def test_group_history_disambiguates_duplicate_group_names_with_sender_anchors(self):
        anchor_messages = [
            {"type": "text", "attr": "group", "sender": "A", "content": f"锚点{index}"}
            for index in range(1, 5)
        ]

        def fake_run(args, **_kwargs):
            if args and args[0] == "sessions":
                return LocalWechatCommandResult(True, data=[
                    {"chat": "测试群", "username": "123@chatroom", "is_group": True},
                    {"chat": "测试群", "username": "456@chatroom", "is_group": True},
                ])
            if args[:2] == ["history", "123@chatroom"]:
                return LocalWechatCommandResult(True, data={
                    "messages": [
                        f"[2026-07-04 04:3{index}] A: 锚点{index + 1}"
                        for index in range(4)
                    ]
                })
            if args[:2] == ["history", "456@chatroom"]:
                return LocalWechatCommandResult(True, data={
                    "messages": [
                        f"[2026-07-04 04:3{index}] B: 锚点{index + 1}"
                        for index in range(4)
                    ]
                })
            return LocalWechatCommandResult(False, error="unexpected")

        with (
            patch("core.local_wechat_reader.ensure_wechat_cli_account_ready", return_value=(True, "")),
            patch("core.local_wechat_reader.run_wechat_cli_json", side_effect=fake_run),
        ):
            result = read_local_history_messages_with_status(
                "测试群",
                limit=10,
                expected_wx_id="wxid_current",
                anchor_messages=anchor_messages,
                chat_type="group",
            )

        self.assertTrue(result.ok)
        self.assertEqual([item.sender for item in result.items], ["A", "A", "A", "A"])

    def test_account_match_rejects_non_wxid_namespace(self):
        with tempfile.TemporaryDirectory() as tmp:
            ok, error = wechat_cli_account_matches("scope_rui", bindings_file=f"{tmp}/bindings.json")

        self.assertFalse(ok)
        self.assertIn("binding missing", error)

    def test_account_match_accepts_bound_scope_namespace(self):
        with tempfile.TemporaryDirectory() as tmp:
            bindings_file = f"{tmp}/bindings.json"
            db_dir = "C:/Users/Admin/Documents/xwechat_files/wxid_abc_b125/db_storage"
            save_wechat_cli_account_binding("scope_rui", db_dir, path=bindings_file)
            with patch("core.local_wechat_reader.wechat_cli_config_db_dir", return_value=db_dir):
                ok, error = wechat_cli_account_matches("scope_rui", bindings_file=bindings_file)

        self.assertTrue(ok)
        self.assertEqual(error, "")

    def test_account_match_requires_configured_wxid(self):
        with patch(
            "core.local_wechat_reader.wechat_cli_config_db_dir",
            return_value="C:/Users/Admin/Documents/xwechat_files/wxid_abc_b125/db_storage",
        ):
            self.assertEqual(wechat_cli_account_matches("wxid_abc"), (True, ""))
            ok, error = wechat_cli_account_matches("wxid_def")

        self.assertFalse(ok)
        self.assertIn("another WeChat account", error)

    def test_ensure_account_ready_switches_bound_scope_account(self):
        with tempfile.TemporaryDirectory() as tmp:
            bindings_file = f"{tmp}/bindings.json"
            db_dir = "C:/Users/Admin/Documents/xwechat_files/wxid_abc_b125/db_storage"
            save_wechat_cli_account_binding("scope_rui", db_dir, path=bindings_file)

            with (
                patch(
                    "core.local_wechat_reader.wechat_cli_config_db_dir",
                    side_effect=[
                        "C:/Users/Admin/Documents/xwechat_files/wxid_old/db_storage",
                        db_dir,
                    ],
                ),
                patch(
                    "core.local_wechat_reader.switch_wechat_cli_to_bound_account",
                    return_value=LocalWechatCommandResult(True, data={"db_dir": db_dir}),
                ) as switch,
            ):
                ok, error = ensure_wechat_cli_account_ready("scope_rui", bindings_file=bindings_file)

        self.assertTrue(ok)
        self.assertEqual(error, "")
        switch.assert_called_once()

    def test_contacts_reader_switches_bound_account_before_reading(self):
        with tempfile.TemporaryDirectory() as tmp:
            bindings_file = f"{tmp}/bindings.json"
            db_dir = "C:/Users/Admin/Documents/xwechat_files/wxid_abc_b125/db_storage"
            save_wechat_cli_account_binding("scope_rui", db_dir, path=bindings_file)

            with (
                patch(
                    "core.local_wechat_reader.wechat_cli_account_matches",
                    side_effect=[(False, "wrong account"), (True, "")],
                ),
                patch(
                    "core.local_wechat_reader.switch_wechat_cli_to_bound_account",
                    return_value=LocalWechatCommandResult(True, data={"db_dir": db_dir}),
                ) as switch,
                patch(
                    "core.local_wechat_reader.run_wechat_cli_json",
                    return_value=LocalWechatCommandResult(True, data=[
                        {"username": "wxid_abc", "nick_name": "阿英2", "remark": "", "alias": ""}
                    ]),
                ),
            ):
                result = read_local_contacts_with_status(
                    limit=10,
                    expected_wx_id="scope_rui",
                )

        self.assertTrue(result.ok)
        self.assertEqual(result.items[0]["wxid"], "wxid_abc")
        switch.assert_called_once()

    def test_live_check_saves_scope_binding_after_marker_found(self):
        with tempfile.TemporaryDirectory() as tmp:
            bindings_file = f"{tmp}/bindings.json"
            db_dir = "C:/Users/Admin/Documents/xwechat_files/wxid_abc_b125/db_storage"
            sent = []

            def sender(message):
                sent.append(message)
                return True

            def fake_run(args, **_kwargs):
                if args and args[0] == "search":
                    return LocalWechatCommandResult(True, data={"results": [f"me: {sent[-1]}"]})
                return LocalWechatCommandResult(False, error="unexpected")

            with (
                patch("core.local_wechat_reader.wechat_cli_config_db_dir", return_value=db_dir),
                patch("core.local_wechat_reader.run_wechat_cli_json", side_effect=fake_run),
            ):
                result = verify_wechat_cli_live_binding(
                    "scope_rui",
                    sender,
                    bindings_file=bindings_file,
                    timeout=3,
                )
                ok, error = wechat_cli_account_matches("scope_rui", bindings_file=bindings_file)

        self.assertTrue(result["ok"])
        self.assertTrue(ok)
        self.assertEqual(error, "")
        self.assertEqual(len(sent), 1)

    def test_history_status_parses_messages_in_oldest_first_order(self):
        with patch("core.local_wechat_reader.run_wechat_cli_json") as run:
            run.return_value.ok = True
            run.return_value.data = {
                "messages": [
                    "[2026-07-04 04:35] me: 第二条",
                    "[2026-07-04 04:34] LXYou: 第一条",
                ]
            }
            run.return_value.error = ""

            result = read_local_history_messages_with_status("LXYou", limit=10)

        self.assertTrue(result.ok)
        self.assertEqual([item.content for item in result.items], ["第一条", "第二条"])

    def test_history_status_keeps_oldest_first_order(self):
        with patch("core.local_wechat_reader.run_wechat_cli_json") as run:
            run.return_value.ok = True
            run.return_value.data = {
                "messages": [
                    "[2026-07-04 04:34] LXYou: 第一条",
                    "[2026-07-04 04:35] me: 第二条",
                ]
            }
            run.return_value.error = ""

            result = read_local_history_messages_with_status("LXYou", limit=10)

        self.assertTrue(result.ok)
        self.assertEqual([item.content for item in result.items], ["第一条", "第二条"])

    def test_history_with_expected_account_uses_unique_contact_username(self):
        def fake_run(args, **_kwargs):
            if args[:2] == ["contacts", "--query"]:
                return LocalWechatCommandResult(True, data=[
                    {"username": "wxid_abc", "nick_name": "张三", "remark": "客户张三", "alias": ""}
                ])
            if args and args[0] == "history":
                self.assertEqual(args[1], "wxid_abc")
                return LocalWechatCommandResult(True, data={
                    "messages": ["[2026-07-04 04:34] 张三: 测试"]
                })
            return LocalWechatCommandResult(False, error="unexpected")

        with (
            patch("core.local_wechat_reader.ensure_wechat_cli_account_ready", return_value=(True, "")),
            patch("core.local_wechat_reader.run_wechat_cli_json", side_effect=fake_run),
        ):
            result = read_local_history_messages_with_status(
                "客户张三",
                limit=10,
                expected_wx_id="wxid_current",
            )

        self.assertTrue(result.ok)
        self.assertEqual(result.items[0].content, "测试")

    def test_history_with_expected_account_rejects_ambiguous_contact_name(self):
        with (
            patch("core.local_wechat_reader.ensure_wechat_cli_account_ready", return_value=(True, "")),
            patch("core.local_wechat_reader.run_wechat_cli_json") as run,
        ):
            run.return_value = LocalWechatCommandResult(True, data=[
                {"username": "wxid_a", "nick_name": "张三", "remark": "", "alias": ""},
                {"username": "wxid_b", "nick_name": "张三", "remark": "", "alias": ""},
            ])

            result = read_local_history_messages_with_status(
                "张三",
                limit=10,
                expected_wx_id="wxid_current",
            )

        self.assertFalse(result.ok)
        self.assertIn("ambiguous", result.error)
        self.assertEqual(run.call_count, 2)

    def test_history_contact_resolution_queries_reasonable_window_to_avoid_false_unique(self):
        with (
            patch("core.local_wechat_reader.ensure_wechat_cli_account_ready", return_value=(True, "")),
            patch("core.local_wechat_reader.run_wechat_cli_json") as run,
        ):
            run.side_effect = [
                LocalWechatCommandResult(True, data=[
                    {"username": f"wxid_noise_{index}", "nick_name": "张三客户", "remark": "", "alias": ""}
                    for index in range(1, 21)
                ] + [
                    {"username": "wxid_a", "nick_name": "张三", "remark": "", "alias": ""},
                    {"username": "wxid_b", "nick_name": "张三", "remark": "", "alias": ""},
                ]),
                LocalWechatCommandResult(True, data=[]),
            ]

            result = read_local_history_messages_with_status(
                "张三",
                limit=10,
                expected_wx_id="wxid_current",
            )

        self.assertFalse(result.ok)
        self.assertEqual(run.call_args_list[0].args[0], [
            "contacts",
            "--query",
            "张三",
            "--limit",
            "100",
            "--format",
            "json",
        ])
        self.assertIn("ambiguous", result.error)

    def test_history_with_expected_account_disambiguates_with_four_anchors(self):
        anchor_messages = [
            {"type": "text", "attr": "friend", "content": f"锚点{index}"}
            for index in range(1, 5)
        ]

        def fake_run(args, **_kwargs):
            if args[:2] == ["contacts", "--query"]:
                return LocalWechatCommandResult(True, data=[
                    {"username": "wxid_a", "nick_name": "张三", "remark": "", "alias": ""},
                    {"username": "wxid_b", "nick_name": "张三", "remark": "", "alias": ""},
                ])
            if args and args[0] == "sessions":
                return LocalWechatCommandResult(True, data=[
                    {"chat": "张三", "username": "wxid_a", "last_message": "普通消息"},
                    {"chat": "张三", "username": "wxid_b", "last_message": "普通消息"},
                ])
            if args[:2] == ["history", "wxid_a"]:
                return LocalWechatCommandResult(True, data={
                    "messages": [
                        f"[2026-07-04 04:3{index}] 张三: 锚点{index + 1}"
                        for index in range(4)
                    ]
                })
            if args[:2] == ["history", "wxid_b"]:
                return LocalWechatCommandResult(True, data={
                    "messages": ["[2026-07-04 04:34] 张三: 另一个人"]
                })
            return LocalWechatCommandResult(False, error="unexpected")

        with (
            patch("core.local_wechat_reader.ensure_wechat_cli_account_ready", return_value=(True, "")),
            patch("core.local_wechat_reader.run_wechat_cli_json", side_effect=fake_run),
        ):
            result = read_local_history_messages_with_status(
                "张三",
                limit=10,
                expected_wx_id="wxid_current",
                anchor_messages=anchor_messages,
            )

        self.assertTrue(result.ok)
        self.assertEqual([item.content for item in result.items], [f"锚点{index}" for index in range(1, 5)])

    def test_history_with_expected_account_requires_four_anchors_to_disambiguate(self):
        anchor_messages = [
            {"type": "text", "attr": "friend", "content": f"锚点{index}"}
            for index in range(1, 4)
        ]

        with (
            patch("core.local_wechat_reader.ensure_wechat_cli_account_ready", return_value=(True, "")),
            patch("core.local_wechat_reader.run_wechat_cli_json") as run,
        ):
            run.side_effect = [
                LocalWechatCommandResult(True, data=[
                    {"username": "wxid_a", "nick_name": "张三", "remark": "", "alias": ""},
                    {"username": "wxid_b", "nick_name": "张三", "remark": "", "alias": ""},
                ]),
                LocalWechatCommandResult(True, data=[]),
            ]

            result = read_local_history_messages_with_status(
                "张三",
                limit=10,
                expected_wx_id="wxid_current",
                anchor_messages=anchor_messages,
            )

        self.assertFalse(result.ok)
        self.assertIn("ambiguous", result.error)
        self.assertEqual(run.call_count, 2)

    def test_history_disambiguation_skips_voice_anchors(self):
        anchor_messages = [
            {"type": "text", "attr": "friend", "content": f"锚点{index}"}
            for index in range(1, 5)
        ] + [
            {"type": "voice", "attr": "friend", "content": "微信侧语音转写"},
            {"type": "voice", "attr": "self", "content": "另一条语音转写"},
        ]

        def fake_run(args, **_kwargs):
            if args[:2] == ["contacts", "--query"]:
                return LocalWechatCommandResult(True, data=[
                    {"username": "wxid_a", "nick_name": "张三", "remark": "", "alias": ""},
                    {"username": "wxid_b", "nick_name": "张三", "remark": "", "alias": ""},
                ])
            if args and args[0] == "sessions":
                return LocalWechatCommandResult(True, data=[
                    {"chat": "张三", "username": "wxid_a", "last_message": "普通消息"},
                    {"chat": "张三", "username": "wxid_b", "last_message": "普通消息"},
                ])
            if args[:2] == ["history", "wxid_a"]:
                return LocalWechatCommandResult(True, data={
                    "messages": [
                        f"[2026-07-04 04:3{index}] 张三: 锚点{index + 1}"
                        for index in range(4)
                    ] + [
                        "[2026-07-04 04:35] 张三: [语音] <voicemsg />",
                    ]
                })
            if args[:2] == ["history", "wxid_b"]:
                return LocalWechatCommandResult(True, data={
                    "messages": ["[2026-07-04 04:34] 张三: 另一个人"]
                })
            return LocalWechatCommandResult(False, error="unexpected")

        with (
            patch("core.local_wechat_reader.ensure_wechat_cli_account_ready", return_value=(True, "")),
            patch("core.local_wechat_reader.run_wechat_cli_json", side_effect=fake_run),
        ):
            result = read_local_history_messages_with_status(
                "张三",
                limit=10,
                expected_wx_id="wxid_current",
                anchor_messages=anchor_messages,
            )

        self.assertTrue(result.ok)
        self.assertEqual([item.content for item in result.items], [
            "锚点1",
            "锚点2",
            "锚点3",
            "锚点4",
            "一条语音消息（未识别出文字）",
        ])

    def test_history_disambiguation_does_not_trust_single_session_without_four_anchors(self):
        anchor_messages = [
            {"type": "text", "attr": "friend", "content": f"锚点{index}"}
            for index in range(1, 4)
        ]

        with (
            patch("core.local_wechat_reader.ensure_wechat_cli_account_ready", return_value=(True, "")),
            patch("core.local_wechat_reader.run_wechat_cli_json") as run,
        ):
            run.side_effect = [
                LocalWechatCommandResult(True, data=[
                    {"username": "wxid_a", "nick_name": "张三", "remark": "", "alias": ""},
                    {"username": "wxid_b", "nick_name": "张三", "remark": "", "alias": ""},
                ]),
                LocalWechatCommandResult(True, data=[
                    {"chat": "张三", "username": "wxid_a", "last_message": "锚点3"},
                ]),
            ]

            result = read_local_history_messages_with_status(
                "张三",
                limit=10,
                expected_wx_id="wxid_current",
                anchor_messages=anchor_messages,
            )

        self.assertFalse(result.ok)
        self.assertIn("ambiguous", result.error)
        self.assertEqual(run.call_count, 2)

    def test_history_disambiguation_skips_too_many_candidates_before_history_reads(self):
        anchor_messages = [
            {"type": "text", "attr": "friend", "content": f"锚点{index}"}
            for index in range(1, 5)
        ]

        with (
            patch("core.local_wechat_reader.ensure_wechat_cli_account_ready", return_value=(True, "")),
            patch("core.local_wechat_reader.run_wechat_cli_json") as run,
        ):
            run.side_effect = [
                LocalWechatCommandResult(True, data=[
                    {"username": f"wxid_{index}", "nick_name": "张三", "remark": "", "alias": ""}
                    for index in range(1, 7)
                ]),
                LocalWechatCommandResult(True, data=[]),
            ]

            result = read_local_history_messages_with_status(
                "张三",
                limit=10,
                expected_wx_id="wxid_current",
                anchor_messages=anchor_messages,
            )

        self.assertFalse(result.ok)
        self.assertIn("too many", result.error)
        self.assertEqual(run.call_count, 2)

    def test_check_status_reports_missing_tool(self):
        with patch("core.local_wechat_reader.find_wechat_cli_executable", return_value=""):
            status = check_wechat_cli_status()

        self.assertFalse(status["available"])
        self.assertEqual(status["state"], "missing_tool")

    def test_wechat_cli_integration_disabled_by_config(self):
        with tempfile.TemporaryDirectory() as tmp:
            config_path = Path(tmp) / "config.json"
            config_path.write_text('{"wechat_cli_enabled": false}', encoding="utf-8")

            self.assertFalse(wechat_cli_integration_enabled(str(config_path)))

    def test_wechat_cli_integration_disabled_by_default_config(self):
        with tempfile.TemporaryDirectory() as tmp:
            config_path = Path(tmp) / "config.json"
            config_path.write_text("{}", encoding="utf-8")

            self.assertFalse(wechat_cli_integration_enabled(str(config_path)))

    def test_wechat_cli_integration_enabled_by_config(self):
        with tempfile.TemporaryDirectory() as tmp:
            config_path = Path(tmp) / "config.json"
            config_path.write_text('{"wechat_cli_enabled": true}', encoding="utf-8")

            self.assertTrue(wechat_cli_integration_enabled(str(config_path)))

    def test_wechat_cli_integration_disabled_by_env(self):
        with patch.dict("os.environ", {"WXBOT_DISABLE_WECHAT_CLI": "1"}):
            self.assertFalse(wechat_cli_integration_enabled())

    def test_check_status_reports_disabled_without_probe(self):
        with (
            patch("core.local_wechat_reader.wechat_cli_integration_enabled", return_value=False),
            patch("core.local_wechat_reader.find_wechat_cli_executable") as find_exe,
        ):
            status = check_wechat_cli_status()

        self.assertFalse(status["available"])
        self.assertEqual(status["state"], "disabled")
        find_exe.assert_not_called()

    def test_check_update_reports_disabled_without_probe(self):
        with (
            patch("core.local_wechat_reader.wechat_cli_integration_enabled", return_value=False),
            patch("core.local_wechat_reader.find_wechat_cli_executable") as find_exe,
        ):
            status = check_wechat_cli_update()

        self.assertFalse(status["ok"])
        self.assertEqual(status["state"], "disabled")
        find_exe.assert_not_called()

    def test_disabled_reader_does_not_run_cli_command(self):
        with (
            patch("core.local_wechat_reader.wechat_cli_integration_enabled", return_value=False),
            patch("core.local_wechat_reader.run_wechat_cli_json") as run_cli,
        ):
            result = read_local_sessions_with_status(limit=10)

        self.assertFalse(result.ok)
        self.assertIn("disabled", result.error)
        run_cli.assert_not_called()

    def test_disabled_live_binding_does_not_send_probe_message(self):
        sender = Mock()
        with patch("core.local_wechat_reader.wechat_cli_integration_enabled", return_value=False):
            result = verify_wechat_cli_live_binding("scope_rui", sender)

        self.assertFalse(result["ok"])
        self.assertIn("disabled", result["error"])
        sender.assert_not_called()

    def test_disabled_account_switch_does_not_run_init(self):
        with (
            patch("core.local_wechat_reader.wechat_cli_integration_enabled", return_value=False),
            patch("core.local_wechat_reader.subprocess.run") as run_process,
        ):
            result = __import__("core.local_wechat_reader", fromlist=["switch_wechat_cli_to_bound_account"]).switch_wechat_cli_to_bound_account("scope_rui")

        self.assertFalse(result.ok)
        self.assertIn("disabled", result.error)
        run_process.assert_not_called()

    def test_check_status_reports_need_init(self):
        with (
            patch("core.local_wechat_reader.find_wechat_cli_executable", return_value="C:/tool/wechat-cli.exe"),
            patch("core.local_wechat_reader._wechat_cli_version", return_value="0.2.4"),
            patch("core.local_wechat_reader.wechat_cli_config_ready", return_value=False),
        ):
            status = check_wechat_cli_status()

        self.assertFalse(status["available"])
        self.assertEqual(status["state"], "need_init")
        self.assertEqual(status["version"], "0.2.4")

    def test_check_status_available_after_contacts_probe(self):
        with (
            patch("core.local_wechat_reader.find_wechat_cli_executable", return_value="C:/tool/wechat-cli.exe"),
            patch("core.local_wechat_reader._wechat_cli_version", return_value="0.2.4"),
            patch("core.local_wechat_reader.wechat_cli_config_ready", return_value=True),
            patch("core.local_wechat_reader.run_wechat_cli_json", return_value=LocalWechatCommandResult(True, data=[])),
        ):
            status = check_wechat_cli_status()

        self.assertTrue(status["available"])
        self.assertEqual(status["state"], "available")

    def test_check_status_reuses_existing_binding_without_live_message(self):
        with tempfile.TemporaryDirectory() as tmp:
            bindings_file = f"{tmp}/bindings.json"
            db_dir = "C:/Users/Admin/Documents/xwechat_files/wxid_abc_b125/db_storage"
            save_wechat_cli_account_binding("scope_rui", db_dir, path=bindings_file)

            def sender(_message):
                raise AssertionError("live check should not run when binding still matches")

            with (
                patch("core.local_wechat_reader.find_wechat_cli_executable", return_value="C:/tool/wechat-cli.exe"),
                patch("core.local_wechat_reader._wechat_cli_version", return_value="0.2.4"),
                patch("core.local_wechat_reader.wechat_cli_config_ready", return_value=True),
                patch("core.local_wechat_reader.wechat_cli_config_db_dir", return_value=db_dir),
                patch("core.local_wechat_reader.run_wechat_cli_json", return_value=LocalWechatCommandResult(True, data=[])),
            ):
                status = check_wechat_cli_status(
                    expected_wx_id="scope_rui",
                    live_check_sender=sender,
                    bindings_file=bindings_file,
                )

        self.assertTrue(status["available"])
        self.assertTrue(status["account_verified"])

    def test_check_status_does_not_bind_without_live_sender(self):
        with tempfile.TemporaryDirectory() as tmp:
            bindings_file = f"{tmp}/bindings.json"
            with (
                patch("core.local_wechat_reader.find_wechat_cli_executable", return_value="C:/tool/wechat-cli.exe"),
                patch("core.local_wechat_reader._wechat_cli_version", return_value="0.2.4"),
                patch("core.local_wechat_reader.wechat_cli_config_ready", return_value=True),
                patch(
                    "core.local_wechat_reader.wechat_cli_config_db_dir",
                    return_value="C:/Users/Admin/Documents/xwechat_files/wxid_abc_b125/db_storage",
                ),
                patch("core.local_wechat_reader.run_wechat_cli_json", return_value=LocalWechatCommandResult(True, data=[])),
            ):
                status = check_wechat_cli_status(
                    expected_wx_id="scope_rui",
                    bindings_file=bindings_file,
                )

        self.assertFalse(status["available"])
        self.assertEqual(status["state"], "account_unverified")

    def test_check_status_switches_to_existing_bound_directory(self):
        with tempfile.TemporaryDirectory() as tmp:
            bindings_file = f"{tmp}/bindings.json"
            db_dir = f"{tmp}/db_storage"
            Path(db_dir).mkdir()
            save_wechat_cli_account_binding("scope_rui", db_dir, path=bindings_file)
            current = {"db_dir": f"{tmp}/other/db_storage"}

            def fake_db_dir(*_args, **_kwargs):
                return current["db_dir"]

            def fake_run(command, **_kwargs):
                current["db_dir"] = db_dir
                return __import__("types").SimpleNamespace(returncode=0, stdout="", stderr="")

            with (
                patch("core.local_wechat_reader.find_wechat_cli_executable", return_value="C:/tool/wechat-cli.exe"),
                patch("core.local_wechat_reader._wechat_cli_version", return_value="0.2.4"),
                patch("core.local_wechat_reader.wechat_cli_config_ready", return_value=True),
                patch("core.local_wechat_reader.wechat_cli_config_db_dir", side_effect=fake_db_dir),
                patch("core.local_wechat_reader.subprocess.run", side_effect=fake_run),
                patch("core.local_wechat_reader.run_wechat_cli_json", return_value=LocalWechatCommandResult(True, data=[])),
            ):
                status = check_wechat_cli_status(expected_wx_id="scope_rui", bindings_file=bindings_file)

        self.assertTrue(status["available"])
        self.assertEqual(status["state"], "available")

    def test_check_update_reports_up_to_date_without_updating(self):
        commit = "a" * 40
        with (
            patch("core.local_wechat_reader.find_wechat_cli_executable", return_value="C:/tool/wechat-cli.exe"),
            patch("core.local_wechat_reader._installed_direct_url_metadata", return_value={
                "url": "https://github.com/huohuoer/wechat-cli.git",
                "vcs_info": {"commit_id": commit},
            }),
            patch("core.local_wechat_reader._wechat_cli_version", return_value="wechat-cli, version 0.2.4"),
            patch("core.local_wechat_reader._git_remote_head", return_value=commit) as remote_head,
        ):
            status = check_wechat_cli_update()

        self.assertTrue(status["ok"])
        self.assertFalse(status["update_available"])
        self.assertEqual(status["state"], "up_to_date")
        remote_head.assert_called_once()

    def test_check_update_reports_available_when_remote_differs(self):
        with (
            patch("core.local_wechat_reader.find_wechat_cli_executable", return_value="C:/tool/wechat-cli.exe"),
            patch("core.local_wechat_reader._installed_direct_url_metadata", return_value={
                "url": "https://github.com/huohuoer/wechat-cli.git",
                "vcs_info": {"commit_id": "a" * 40},
            }),
            patch("core.local_wechat_reader._wechat_cli_version", return_value="wechat-cli, version 0.2.4"),
            patch("core.local_wechat_reader._git_remote_head", return_value="b" * 40),
        ):
            status = check_wechat_cli_update()

        self.assertTrue(status["ok"])
        self.assertTrue(status["update_available"])
        self.assertEqual(status["state"], "update_available")


if __name__ == "__main__":
    unittest.main()
