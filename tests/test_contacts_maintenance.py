import unittest
from unittest.mock import patch

from feature.contacts import prepare_contact_directory_window


class ContactMaintenancePrepareTests(unittest.TestCase):
    def test_prepare_switches_contact_without_show(self):
        calls = []

        class FakeWeChat:
            def Show(self):
                calls.append("Show")
                raise AssertionError("Show should not be called")

            def SwitchToContact(self):
                calls.append("SwitchToContact")

        class FakeBot:
            wx = FakeWeChat()

        with patch("feature.contacts.close_contact_directory_management_windows", return_value=0):
            prepare_contact_directory_window(FakeBot())

        self.assertEqual(calls, ["SwitchToContact"])

    def test_prepare_rebinds_and_retries_after_switch_failure(self):
        calls = []

        class BrokenWeChat:
            def SwitchToContact(self):
                calls.append("broken")
                raise RuntimeError("missing contact tab")

        class HealthyWeChat:
            def SwitchToContact(self):
                calls.append("healthy")

        class FakeBot:
            wx = BrokenWeChat()

        def fake_rebind(bot):
            calls.append("rebind")
            bot.wx = HealthyWeChat()
            return bot.wx

        with (
            patch("feature.contacts.close_contact_directory_management_windows", return_value=0),
            patch("core.wechat_window.rebind_wechat_client", side_effect=fake_rebind),
        ):
            prepare_contact_directory_window(FakeBot())

        self.assertEqual(calls, ["broken", "rebind", "healthy"])


if __name__ == "__main__":
    unittest.main()
