import unittest

from tools.repro_get_friend_details import build_get_friend_details_kwargs, run_probe


class ReproGetFriendDetailsTests(unittest.TestCase):
    def test_build_kwargs_without_match_name_omits_callback(self):
        kwargs, callback_hits = build_get_friend_details_kwargs(
            count=5,
            interval=1.5,
            match_name="",
        )
        self.assertEqual(kwargs, {"n": 5, "interval": 1.5})
        self.assertEqual(callback_hits, [])

    def test_build_kwargs_with_match_name_adds_callback(self):
        kwargs, callback_hits = build_get_friend_details_kwargs(
            count=3,
            interval=2.0,
            match_name=" 阿英2 ",
        )
        self.assertEqual(kwargs["n"], 3)
        self.assertEqual(kwargs["interval"], 2.0)
        self.assertIn("callback", kwargs)
        callback = kwargs["callback"]
        self.assertFalse(callback("阿英1"))
        self.assertTrue(callback("阿英2"))
        self.assertEqual(callback_hits, ["阿英1", "阿英2"])

    def test_run_probe_executes_expected_sequence(self):
        sequence = []

        class FakeWeChat:
            def Show(self):
                sequence.append("Show")

            def SwitchToContact(self):
                sequence.append("SwitchToContact")

            def GetFriendDetails(self, **kwargs):
                sequence.append(("GetFriendDetails", kwargs))
                return [{"昵称": "阿英2"}]

            def SwitchToChat(self):
                sequence.append("SwitchToChat")

        result = run_probe(
            client_factory=FakeWeChat,
            count=2,
            interval=1.2,
            match_name="",
            switch_back=True,
            printer=lambda _msg: None,
        )

        self.assertEqual(sequence[0:2], ["Show", "SwitchToContact"])
        self.assertEqual(sequence[2][0], "GetFriendDetails")
        self.assertEqual(sequence[2][1]["n"], 2)
        self.assertEqual(sequence[2][1]["interval"], 1.2)
        self.assertEqual(sequence[3], "SwitchToChat")
        self.assertEqual(result["result"], [{"昵称": "阿英2"}])


if __name__ == "__main__":
    unittest.main()
