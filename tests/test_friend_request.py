import unittest
from datetime import datetime, timedelta

from PIL import Image, ImageDraw

from feature import friend_request
from feature.friend_request_senders import blue_text_fragments_from_image, choose_blue_text_fragment


class FriendRequestLogicTest(unittest.TestCase):
    def test_build_candidates_from_directory_filters_tags(self):
        directory = {
            "subjects": [
                {"subject_type": "friend", "status": "active", "remark": "瑞东（私人号）", "tags": ["删除我的人"]},
                {"subject_type": "friend", "status": "active", "remark": "追梦瑞弟", "tags": ["删除我的人", "黑名单"]},
                {"subject_type": "friend", "status": "active", "remark": "普通好友", "tags": ["普通"]},
                {"subject_type": "group", "status": "active", "remark": "群", "tags": ["删除我的人"]},
            ]
        }
        settings = friend_request.normalize_settings({
            "include_tags": ["删除我的人"],
        })

        candidates = friend_request.build_candidates_from_directory(directory, settings)

        self.assertEqual([item["display_name"] for item in candidates], ["瑞东（私人号）", "追梦瑞弟"])
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
        state["candidates"] = [friend_request.normalize_candidate({"display_name": "瑞东", "send_name": "瑞东"})]

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
