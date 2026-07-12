import unittest
from unittest.mock import patch

from feature import contact_auto_collector_worker as worker


class ContactAutoCollectorWorkerTests(unittest.TestCase):
    def test_head_batch_always_uses_callback_and_fast_bounded_options(self):
        calls = []

        class FakeWeChat:
            def SwitchToContact(self):
                calls.append("switch")

            def GetFriendDetails(self, **kwargs):
                calls.append(kwargs)
                self.callback_results = [
                    kwargs["callback"]({"备注": "阿英2"}),
                    kwargs["callback"]({"备注": "阿英3"}),
                ]
                return [{"备注": "阿英2"}, {"备注": "阿英3"}]

        with (
            patch.object(worker, "WeChat", FakeWeChat),
            patch.object(worker, "_reset_contact_list_to_top") as reset_top,
        ):
            result = worker.collect({"start_name": "", "count": 2})

        kwargs = calls[1]
        self.assertEqual(kwargs["n"], 2)
        self.assertEqual(kwargs["interval"], 0)
        self.assertEqual(kwargs["speed"], 5)
        self.assertFalse(kwargs["save_head_image"])
        self.assertEqual(result["callback_names"], [])
        self.assertEqual(result["matched_name"], "")
        reset_top.assert_called_once()

    def test_cursor_callback_rejects_until_matching_contact(self):
        callback_results = []

        class FakeWeChat:
            def SwitchToContact(self):
                pass

            def GetFriendDetails(self, **kwargs):
                callback_results.extend([
                    kwargs["callback"]({"备注": "阿英2"}),
                    kwargs["callback"]({"备注": "白云3"}),
                    kwargs["callback"]({"备注": "白云4"}),
                ])
                return [{"备注": "白云3"}, {"备注": "白云4"}]

        with (
            patch.object(worker, "WeChat", FakeWeChat),
            patch.object(worker, "_reset_contact_list_to_top"),
        ):
            result = worker.collect({"start_name": "白云3", "count": 2})

        self.assertEqual(callback_results, [False, True, False])
        self.assertEqual(result["matched_name"], "白云3")
        self.assertEqual(result["callback_names"], ["白云3"])

    def test_cursor_callback_does_not_accept_name_prefix_collision(self):
        callback_results = []

        class FakeWeChat:
            def SwitchToContact(self):
                pass

            def GetFriendDetails(self, **kwargs):
                callback_results.extend([
                    kwargs["callback"]({"备注": "白云3号"}),
                    kwargs["callback"]({"备注": "白云3"}),
                ])
                return [{"备注": "白云3"}]

        with (
            patch.object(worker, "WeChat", FakeWeChat),
            patch.object(worker, "_reset_contact_list_to_top"),
        ):
            worker.collect({"start_name": "白云3", "count": 1})

        self.assertEqual(callback_results, [False, True])

    def test_ambiguous_same_name_rows_use_later_unique_cursor(self):
        callback_results = []

        class FakeWeChat:
            def SwitchToContact(self):
                pass

            def GetFriendDetails(self, **kwargs):
                callback_results.extend([
                    kwargs["callback"]({"备注": "同名"}),
                    kwargs["callback"]({"备注": "同名"}),
                ])
                return [
                    {"备注": "同名", "微信号": "id-2"},
                    {"备注": "同名", "微信号": "id-3"},
                    {"备注": "下一位", "微信号": "id-4"},
                ]

        with (
            patch.object(worker, "WeChat", FakeWeChat),
            patch.object(worker, "_reset_contact_list_to_top"),
        ):
            result = worker.collect({
                "start_name": "同名",
                "start_identity": "wechat_id:id-2",
                "count": 3,
            })

        self.assertEqual(callback_results, [True, True])
        self.assertEqual(result["cursor_candidates"][0], {
            "name": "下一位",
            "identity": "wechat_id:id-4",
        })

    def test_same_name_cursor_rejects_changed_anchor_identity(self):
        class FakeWeChat:
            def SwitchToContact(self):
                pass

            def GetFriendDetails(self, **kwargs):
                kwargs["callback"]({"备注": "同名"})
                return [{"备注": "同名", "微信号": "different-id"}]

        with (
            patch.object(worker, "WeChat", FakeWeChat),
            patch.object(worker, "_reset_contact_list_to_top"),
            self.assertRaisesRegex(RuntimeError, "游标身份已变化"),
        ):
            worker.collect({
                "start_name": "同名",
                "start_identity": "wechat_id:expected-id",
                "count": 1,
            })

    def test_all_same_name_contacts_fail_instead_of_guessing_cursor(self):
        contacts = [
            {"备注": "同名", "微信号": f"id-{index}"}
            for index in range(1, 121)
        ]

        class FakeWeChat:
            def SwitchToContact(self):
                pass

            def GetFriendDetails(self, **kwargs):
                start_index = 0
                for index, detail in enumerate(contacts):
                    if kwargs["callback"](detail):
                        start_index = index
                        break
                return contacts[start_index:start_index + kwargs["n"]]

        with (
            patch.object(worker, "WeChat", FakeWeChat),
            patch.object(worker, "_reset_contact_list_to_top"),
            self.assertRaisesRegex(RuntimeError, "没有可安全定位"),
        ):
            worker.collect({"start_name": "", "count": 50})

    def test_repeated_virtual_rows_are_deduplicated_by_identity(self):
        repeated = {"备注": "末尾", "微信号": "tail-id"}

        class FakeWeChat:
            def SwitchToContact(self):
                pass

            def GetFriendDetails(self, **kwargs):
                kwargs["callback"]("末尾")
                return [repeated, repeated, repeated]

        with (
            patch.object(worker, "WeChat", FakeWeChat),
            patch.object(worker, "_reset_contact_list_to_top"),
        ):
            result = worker.collect({"start_name": "末尾", "start_identity": "wechat_id:tail-id", "count": 50})

        self.assertEqual(result["raw_result_count"], 3)
        self.assertEqual(result["raw_result_identities"], ["wechat_id:tail-id"] * 3)
        self.assertEqual(len(result["result"]), 1)
        self.assertEqual(result["cursor_candidates"], [{"name": "末尾", "identity": "wechat_id:tail-id"}])

    def test_reset_contact_list_to_top_focuses_list_and_sends_home(self):
        calls = []

        class FakeList:
            def Exists(self, timeout):
                calls.append(("exists", timeout))
                return True

            def SetFocus(self):
                calls.append("focus")

            def SendKeys(self, keys):
                calls.append(("keys", keys))

            def Refind(self):
                calls.append("refind")

            def GetChildren(self):
                return [type("Item", (), {"Name": "新的朋友"})()]

        contact_list = FakeList()

        class FakeRoot:
            def ListControl(self, **kwargs):
                calls.append(("find", kwargs))
                return contact_list

        fake_wx = type("FakeWeChat", (), {
            "NavigationBox": type("Nav", (), {
                "root": type("Root", (), {"control": FakeRoot()})(),
            })(),
        })()

        with patch.object(worker.time, "sleep"):
            worker._reset_contact_list_to_top(fake_wx)

        self.assertEqual(calls, [
            ("find", {"Name": "通讯录", "searchDepth": 20}),
            ("exists", 2),
            "focus",
            ("keys", "{HOME}"),
            "refind",
        ])

    def test_reset_contact_list_to_top_rejects_unverified_position(self):
        class FakeList:
            def Exists(self, _timeout):
                return True

            def SetFocus(self):
                pass

            def SendKeys(self, _keys):
                pass

            def Refind(self):
                pass

            def GetChildren(self):
                return [type("Item", (), {"Name": "普通联系人"})()]

        contact_list = FakeList()
        fake_wx = type("FakeWeChat", (), {
            "NavigationBox": type("Nav", (), {
                "root": type("Root", (), {
                    "control": type("RootControl", (), {
                        "ListControl": lambda self, **_kwargs: contact_list,
                    })(),
                })(),
            })(),
        })()

        with (
            patch.object(worker.time, "sleep"),
            self.assertRaisesRegex(RuntimeError, "未能稳定回到顶部"),
        ):
            worker._reset_contact_list_to_top(fake_wx)


if __name__ == "__main__":
    unittest.main()
