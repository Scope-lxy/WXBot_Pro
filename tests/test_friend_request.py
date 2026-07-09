import unittest
import tempfile
import threading
from datetime import datetime, timedelta
from types import SimpleNamespace

from PIL import Image, ImageDraw

from feature import friend_request
from feature.friend_request_senders import blue_text_fragments_from_image, choose_blue_text_fragment


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

    def test_run_once_records_sender_exception_as_failed(self):
        class RaisingSender:
            def send(self, *args, **kwargs):
                raise RuntimeError("微信未初始化")

        class FakeBot:
            wx_id = "wxid_test"

            def __init__(self, data_dir):
                self.config = SimpleNamespace(DATA_DIR=data_dir)
                self._lock = threading.Lock()

            def _get_wechat_action_lock(self):
                return self._lock

        with tempfile.TemporaryDirectory() as data_dir:
            state = friend_request.default_state("wxid_test")
            state["candidates"] = [friend_request.normalize_candidate({
                "display_name": "瑞东",
                "send_name": "瑞东",
                "tags": ["删除我的人"],
            })]
            friend_request.save_state(data_dir, state)
            original_sender = friend_request.ConversationVerifySender
            friend_request.ConversationVerifySender = RaisingSender
            try:
                result = friend_request.run_once(FakeBot(data_dir), force=True, now=datetime(2026, 6, 11, 9, 30, 0))
            finally:
                friend_request.ConversationVerifySender = original_sender

            saved = friend_request.load_state(data_dir, "wxid_test")
            self.assertEqual(result["status"], "failed")
            self.assertEqual(saved["candidates"][0]["status"], "failed")
            self.assertIn("微信未初始化", saved["executions"][-1]["message"])

    def test_run_once_records_lock_busy_as_visible_last_result(self):
        class FakeBot:
            wx_id = "wxid_test"

            def __init__(self, data_dir):
                self.config = SimpleNamespace(DATA_DIR=data_dir)
                self._lock = threading.Lock()
                self._lock.acquire()

            def _get_wechat_action_lock(self):
                return self._lock

        with tempfile.TemporaryDirectory() as data_dir:
            state = friend_request.default_state("wxid_test")
            state["candidates"] = [friend_request.normalize_candidate({
                "display_name": "瑞东",
                "send_name": "瑞东",
                "tags": ["删除我的人"],
            })]
            friend_request.save_state(data_dir, state)

            bot = FakeBot(data_dir)
            try:
                result = friend_request.run_once(bot, force=True, now=datetime(2026, 6, 11, 9, 30, 0))
            finally:
                bot._lock.release()

            saved = friend_request.load_state(data_dir, "wxid_test")
            self.assertEqual(result["status"], "skipped")
            self.assertIn("微信操作锁占用中", result["message"])
            self.assertIn("微信操作锁占用中", saved["runtime"]["last_result"])
            self.assertIn("微信操作锁占用中", saved["candidates"][0]["last_result"])

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
