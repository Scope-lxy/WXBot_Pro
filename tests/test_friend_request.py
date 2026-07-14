import unittest
import tempfile
import threading
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import patch

from PIL import Image, ImageDraw

from feature import friend_request
from feature.friend_request_senders import ConversationVerifySender, blue_text_fragments_from_image, choose_blue_text_fragment


class FriendRequestLogicTest(unittest.TestCase):
    def test_build_candidates_from_directory_filters_tags(self):
        directory = {
            "subjects": [
                {"status": "active", "remark": "瑞东（私人号）", "tags": ["删除我的人"]},
                {"status": "active", "remark": "追梦瑞弟", "tags": ["删除我的人", "黑名单"]},
                {"status": "active", "remark": "普通好友", "tags": ["普通"]},
                {"status": "missing", "remark": "旧联系人", "tags": ["删除我的人"]},
            ]
        }
        settings = friend_request.normalize_settings({
            "include_tags": ["删除我的人"],
        })

        candidates = friend_request.build_candidates_from_directory(directory, settings)

        self.assertEqual([item["name"] for item in candidates], ["瑞东（私人号）", "追梦瑞弟"])
        self.assertEqual(candidates[0]["sender_kind"], "conversation_verify")
        self.assertEqual(candidates[0]["add_object"], "deleted_me")

    def test_build_candidates_excludes_duplicate_send_names(self):
        directory = {
            "subjects": [
                {"status": "active", "contact_key": "one", "remark": "同名", "tags": ["删除我的人"]},
                {"status": "active", "contact_key": "two", "remark": "同名", "tags": ["删除我的人"]},
                {"status": "active", "contact_key": "three", "remark": "唯一好友", "tags": ["删除我的人"]},
            ]
        }
        settings = friend_request.normalize_settings({"include_tags": ["删除我的人"]})

        candidates = friend_request.build_candidates_from_directory(directory, settings)

        self.assertEqual([item["name"] for item in candidates], ["唯一好友"])

    def test_build_candidates_excludes_unsearchable_send_name(self):
        directory = {
            "subjects": [
                {
                    "status": "active",
                    "contact_key": "unsafe",
                    "nickname": "😀😀",
                    "tags": ["删除我的人"],
                },
                {"status": "active", "contact_key": "safe", "remark": "可搜索好友", "tags": ["删除我的人"]},
            ]
        }
        settings = friend_request.normalize_settings({"include_tags": ["删除我的人"]})

        candidates = friend_request.build_candidates_from_directory(directory, settings)

        self.assertEqual([item["name"] for item in candidates], ["可搜索好友"])

    def test_next_pending_candidate_respects_limits_and_force(self):
        now = datetime(2026, 6, 11, 8, 0, 0)
        state = friend_request.default_state("wxid_test")
        state["settings"] = friend_request.normalize_settings({
            "daily_limit": 1,
            "allowed_time_ranges": ["09:00-10:00"],
            "base_interval_minutes": 30,
        })
        state["candidates"] = [friend_request.normalize_candidate({"display_name": "瑞东", "send_name": "瑞东", "tags": ["删除我的人"]})]

        candidate, reason = friend_request.next_pending_candidate(state, now=now)
        self.assertIsNone(candidate)
        self.assertEqual(reason, "当前不在可发送时间段")

        candidate, reason = friend_request.next_pending_candidate(state, now=now, ignore_schedule=True)
        self.assertIsNotNone(candidate)
        self.assertEqual(reason, "")

        state["runtime"]["today_sent"] = 1
        candidate, reason = friend_request.next_pending_candidate(state, now=now, ignore_schedule=True)
        self.assertIsNone(candidate)
        self.assertEqual(reason, "今日申请数已达到上限")

    def test_record_execution_sets_randomized_next_run(self):
        now = datetime(2026, 6, 11, 9, 30, 0)
        state = friend_request.default_state("wxid_test")
        state["settings"] = friend_request.normalize_settings({"base_interval_minutes": 30})
        candidate = friend_request.normalize_candidate({"display_name": "瑞东", "send_name": "瑞东"})
        result = {"status": "sent", "message": "好友验证申请已提交"}

        friend_request.record_execution(state, candidate, result, addmsg="你好", now=now)

        next_run = datetime.fromisoformat(state["runtime"]["next_run_at"])
        self.assertGreaterEqual(next_run, now + timedelta(minutes=27))
        self.assertLessEqual(next_run, now + timedelta(minutes=33))

    def test_failed_candidate_waits_for_retry_time(self):
        now = datetime(2026, 6, 11, 9, 30, 0)
        state = friend_request.default_state("wxid_test")
        state["settings"] = friend_request.normalize_settings({"base_interval_minutes": 30})
        candidate = friend_request.normalize_candidate({"display_name": "瑞东", "send_name": "瑞东", "tags": ["删除我的人"]})
        state["candidates"] = [candidate]

        friend_request.record_execution(state, candidate, {"status": "failed", "message": "未找到验证按钮"}, addmsg="", now=now)

        retry_at = datetime.fromisoformat(candidate["next_retry_at"])
        next_candidate, reason = friend_request.next_pending_candidate(state, now=now, ignore_schedule=True)
        self.assertIsNone(next_candidate)
        self.assertEqual(reason, "没有待申请候选人")

        next_candidate, reason = friend_request.next_pending_candidate(state, now=retry_at + timedelta(seconds=1), ignore_schedule=True)
        self.assertIs(next_candidate, candidate)
        self.assertEqual(reason, "")

    def test_uncertain_candidate_is_frozen_and_never_auto_retried(self):
        now = datetime(2026, 6, 11, 9, 30, 0)
        state = friend_request.default_state("wxid_test")
        candidate = friend_request.normalize_candidate({"display_name": "瑞东", "send_name": "瑞东", "tags": ["删除我的人"]})
        state["candidates"] = [candidate]

        friend_request.record_execution(
            state,
            candidate,
            {"status": "uncertain", "message": "已进入提交阶段但结果未知"},
            addmsg="你好",
            now=now,
        )

        self.assertEqual(candidate["status"], "uncertain")
        self.assertEqual(candidate["next_retry_at"], "")
        next_candidate, reason = friend_request.next_pending_candidate(
            state,
            now=now + timedelta(days=30),
            ignore_schedule=True,
        )
        self.assertIsNone(next_candidate)
        self.assertEqual(reason, "没有待申请候选人")

    def test_submit_exception_is_uncertain_and_does_not_repeat_navigation(self):
        calls = []

        class FakeWx:
            def ChatWith(self, target, exact=True):
                calls.append(("ChatWith", target, exact))

            def ChatInfo(self):
                return {"chat_name": "瑞东"}

        class FailingWindow:
            def send(self, **_kwargs):
                calls.append(("submit",))
                raise RuntimeError("窗口返回异常")

        sender = ConversationVerifySender(wait_after_front=0, assert_owner_thread=lambda: None)
        sender._front = lambda: None
        sender._find_verify_message = lambda _wx: object()
        sender._click_verify_link = lambda _msg: {"click_point": (1, 2)}
        bot = SimpleNamespace(wx=FakeWx())

        with patch("wxautox4.ui.component.AddFriendsWnd", return_value=FailingWindow()), patch(
            "feature.friend_request_senders.time.sleep", return_value=None
        ):
            result = sender.send(bot, "瑞东", addmsg="你好", max_attempts=2)

        self.assertEqual(result["status"], "uncertain")
        self.assertEqual([call[0] for call in calls], ["ChatWith", "submit"])

    def test_sender_rejects_missing_owner_thread_assertion_before_ui(self):
        calls = []
        bot = SimpleNamespace(wx=SimpleNamespace(
            ChatWith=lambda *_args, **_kwargs: calls.append("ChatWith"),
        ))

        with self.assertRaisesRegex(RuntimeError, "只能由微信 UI owner"):
            ConversationVerifySender().send(bot, "瑞东")

        self.assertEqual(calls, [])

    def test_sent_candidate_can_run_after_duplicate_window(self):
        now = datetime(2026, 6, 11, 9, 30, 0)
        state = friend_request.default_state("wxid_test")
        state["settings"] = friend_request.normalize_settings({"recent_duplicate_days": 3})
        candidate = friend_request.normalize_candidate({
            "display_name": "瑞东",
            "send_name": "瑞东",
            "tags": ["删除我的人"],
            "status": "sent",
        })
        state["candidates"] = [candidate]
        state["executions"] = [{
            "at": (now - timedelta(days=2)).isoformat(),
            "candidate_id": candidate["candidate_id"],
            "status": "sent",
        }]

        next_candidate, _reason = friend_request.next_pending_candidate(state, now=now, ignore_schedule=True)
        self.assertIsNone(next_candidate)

        state["executions"][0]["at"] = (now - timedelta(days=4)).isoformat()
        next_candidate, reason = friend_request.next_pending_candidate(state, now=now, ignore_schedule=True)
        self.assertIs(next_candidate, candidate)
        self.assertEqual(reason, "")

    def test_next_pending_candidate_skips_stale_tag_candidates(self):
        state = friend_request.default_state("wxid_test")
        state["settings"] = friend_request.normalize_settings({"include_tags": ["删除我的人"]})
        stale = friend_request.normalize_candidate({"display_name": "旧目标", "send_name": "旧目标", "tags": ["旧标签"]})
        matched = friend_request.normalize_candidate({"display_name": "瑞东", "send_name": "瑞东", "tags": ["删除我的人"]})
        state["candidates"] = [stale, matched]

        candidate, reason = friend_request.next_pending_candidate(state, now=datetime(2026, 6, 11, 9, 30, 0), ignore_schedule=True)

        self.assertIs(candidate, matched)
        self.assertEqual(reason, "")

    def test_run_once_records_sender_exception_as_uncertain(self):
        class RaisingOwner:
            def call(self, *_args, **_kwargs):
                raise RuntimeError("微信未初始化")

        class FakeBot:
            wx_id = "wxid_test"

            def __init__(self, data_dir):
                self.config = SimpleNamespace(DATA_DIR=data_dir)
                self._ui_owner = RaisingOwner()

        with tempfile.TemporaryDirectory() as data_dir:
            state = friend_request.default_state("wxid_test")
            state["candidates"] = [friend_request.normalize_candidate({
                "display_name": "瑞东",
                "send_name": "瑞东",
                "tags": ["删除我的人"],
            })]
            friend_request.save_state(data_dir, state)
            result = friend_request.run_once(FakeBot(data_dir), force=True, now=datetime(2026, 6, 11, 9, 30, 0))

            saved = friend_request.load_state(data_dir, "wxid_test")
            self.assertEqual(result["status"], "uncertain")
            self.assertEqual(saved["candidates"][0]["status"], "uncertain")
            self.assertIn("微信未初始化", saved["executions"][-1]["message"])

    def test_run_once_records_missing_owner_as_uncertain(self):
        class FakeBot:
            wx_id = "wxid_test"

            def __init__(self, data_dir):
                self.config = SimpleNamespace(DATA_DIR=data_dir)
                self._ui_owner = None

        with tempfile.TemporaryDirectory() as data_dir:
            state = friend_request.default_state("wxid_test")
            state["candidates"] = [friend_request.normalize_candidate({
                "display_name": "瑞东",
                "send_name": "瑞东",
                "tags": ["删除我的人"],
            })]
            friend_request.save_state(data_dir, state)

            result = friend_request.run_once(FakeBot(data_dir), force=True, now=datetime(2026, 6, 11, 9, 30, 0))

            saved = friend_request.load_state(data_dir, "wxid_test")
            self.assertEqual(result["status"], "uncertain")
            self.assertIn("微信 UI owner 未运行", result["message"])
            self.assertIn("微信 UI owner 未运行", saved["runtime"]["last_result"])
            self.assertIn("微信 UI owner 未运行", saved["candidates"][0]["last_result"])

    def test_concurrent_run_once_claims_candidate_only_once(self):
        started = threading.Event()
        release = threading.Event()

        class BlockingOwner:
            def __init__(self):
                self.calls = 0

            def call(self, _intent, _timeout):
                self.calls += 1
                started.set()
                release.wait(2)
                return {"status": "sent", "message": "好友验证申请已提交"}

        class FakeBot:
            wx_id = "wxid_test"

            def __init__(self, data_dir):
                self.config = SimpleNamespace(DATA_DIR=data_dir)
                self._ui_owner = BlockingOwner()

            def _metric_increment(self, _key):
                pass

        with tempfile.TemporaryDirectory() as data_dir:
            state = friend_request.default_state("wxid_test")
            state["candidates"] = [friend_request.normalize_candidate({
                "display_name": "瑞东",
                "send_name": "瑞东",
                "tags": ["删除我的人"],
            })]
            friend_request.save_state(data_dir, state)
            bot = FakeBot(data_dir)
            first_result = {}

            worker = threading.Thread(
                target=lambda: first_result.update(friend_request.run_once(bot, force=True)),
            )
            worker.start()
            self.assertTrue(started.wait(1))

            second = friend_request.run_once(bot, force=True)
            release.set()
            worker.join(2)

            self.assertFalse(worker.is_alive())
            self.assertEqual(bot._ui_owner.calls, 1)
            self.assertEqual(first_result["status"], "sent")
            self.assertEqual(second["status"], "skipped")
            self.assertEqual(second["message"], "没有待申请候选人")

    def test_owner_version_cancellation_releases_pre_submit_claim(self):
        class CancellingOwner:
            def call(self, _intent, _timeout):
                raise friend_request.wechat_ui_actions.IntentCancelled("规则已更新")

        class FakeBot:
            wx_id = "wxid_test"

            def __init__(self, data_dir):
                self.config = SimpleNamespace(DATA_DIR=data_dir)
                self._ui_owner = CancellingOwner()

            def _friend_request_ui_guard(self, _state, candidate):
                return f"friend_request:{candidate['candidate_id']}", 1

        with tempfile.TemporaryDirectory() as data_dir:
            state = friend_request.default_state("wxid_test")
            state["candidates"] = [friend_request.normalize_candidate({
                "display_name": "瑞东",
                "send_name": "瑞东",
                "tags": ["删除我的人"],
            })]
            friend_request.save_state(data_dir, state)

            result = friend_request.run_once(FakeBot(data_dir), force=True)
            saved = friend_request.load_state(data_dir, "wxid_test")

            self.assertEqual(result["status"], "skipped")
            self.assertEqual(saved["candidates"][0]["status"], "pending")
            self.assertEqual(saved["candidates"][0]["claim_token"], "")
            self.assertIn("尚未提交微信", saved["candidates"][0]["last_result"])

    def test_refresh_candidates_preserves_active_claim_token(self):
        with tempfile.TemporaryDirectory() as data_dir:
            claimed = friend_request.normalize_candidate({
                "candidate_id": "contact-1",
                "display_name": "瑞东",
                "send_name": "瑞东",
                "status": "uncertain",
                "claim_token": "claim-1",
            })
            state = friend_request.default_state("wxid_test")
            state["candidates"] = [claimed]
            friend_request.save_state(data_dir, state)
            refreshed = friend_request.normalize_candidate({
                "candidate_id": "contact-1",
                "display_name": "瑞东",
                "send_name": "瑞东",
            })

            with patch("feature.friend_request.load_directory", return_value={"subjects": []}), patch(
                "feature.friend_request.build_candidates_from_directory", return_value=[refreshed]
            ):
                result = friend_request.refresh_candidates(data_dir, "wxid_test")

            self.assertEqual(result["candidates"][0]["status"], "uncertain")
            self.assertEqual(result["candidates"][0]["claim_token"], "claim-1")

    def test_select_message_by_add_object_random_pool(self):
        state = friend_request.default_state("wxid_test")
        state["message_rules"] = [
            friend_request.normalize_message_rule({
                "object_kind": "deleted_me",
                "messages": ["第一条", "第二条"],
            }),
            friend_request.normalize_message_rule({
                "object_kind": "group_member",
                "messages": ["群成员文案"],
            })
        ]
        candidate = friend_request.normalize_candidate({"display_name": "瑞东", "send_name": "瑞东", "add_object": "deleted_me"})

        selected = {friend_request.select_message_for_candidate(state, candidate) for _ in range(20)}
        self.assertTrue(selected <= {"第一条", "第二条"})
        self.assertTrue(selected)

    def test_no_default_message_when_rules_missing(self):
        state = friend_request.default_state("wxid_test")
        candidate = friend_request.normalize_candidate({"display_name": "瑞东", "send_name": "瑞东", "add_object": "deleted_me"})

        self.assertEqual(friend_request.select_message_for_candidate(state, candidate), "")

    def test_old_message_fields_are_dropped(self):
        state = friend_request.normalize_state({
            "wx_id": "wxid_test",
            "default_messages": ["旧默认文案"],
            "message_rules": [
                {"match_tags": ["删除我的人"], "messages": ["旧标签文案"]},
                {"object_kind": "deleted_me", "messages": ["新文案"]},
            ],
        }, wx_id="wxid_test")

        self.assertNotIn("default_messages", state)
        self.assertEqual(state["message_rules"], [{
            "rule_id": state["message_rules"][0]["rule_id"],
            "enabled": True,
            "object_kind": "deleted_me",
            "messages": ["新文案"],
        }])

    def test_payload_exposes_available_add_objects(self):
        payload = friend_request.friend_request_payload(friend_request.default_state("wxid_test"))

        self.assertEqual(payload["add_object_options"], [{"value": "deleted_me", "label": "删除我的人", "enabled": True}])
        self.assertNotIn("default_messages", payload)

    def test_blue_fragment_detection_handles_wrapped_link(self):
        image = Image.new("RGB", (140, 70), "white")
        draw = ImageDraw.Draw(image)
        blue = (50, 120, 210)
        draw.rectangle((12, 12, 62, 22), fill=blue)
        draw.rectangle((12, 38, 78, 48), fill=blue)

        fragments = blue_text_fragments_from_image(image)
        chosen = choose_blue_text_fragment(fragments)

        self.assertGreaterEqual(len(fragments), 2)
        self.assertIsNotNone(chosen)
        self.assertEqual(chosen["row"], 1)


if __name__ == "__main__":
    unittest.main()
