import unittest
import re
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
PRODUCTION_SUFFIXES = {".py", ".html", ".css", ".bat"}
EXCLUDED_PARTS = {"tests", "tools", "venv", ".git"}


def production_files():
    for path in ROOT.rglob("*"):
        if not path.is_file() or path.suffix.lower() not in PRODUCTION_SUFFIXES:
            continue
        if any(part in EXCLUDED_PARTS for part in path.relative_to(ROOT).parts):
            continue
        yield path


class ProductionBoundaryTests(unittest.TestCase):
    def test_wechat_client_creation_is_limited_to_owner_and_contact_worker(self):
        allowed = {
            Path("core/wechat_ui_runtime.py"),
            Path("feature/contact_auto_collector_worker.py"),
        }
        import_pattern = re.compile(r"(?:from\s+wxautox4\s+import\s+[^\n]*\bWeChat\b|\bWeChat\s*\()")
        hits = []
        for path in production_files():
            if path.suffix.lower() != ".py":
                continue
            relative = path.relative_to(ROOT)
            if relative in allowed:
                continue
            if import_pattern.search(path.read_text(encoding="utf-8", errors="ignore")):
                hits.append(str(relative))
        self.assertEqual(hits, [])

    def test_direct_wxautox_uia_is_limited_to_owner_executed_modules(self):
        allowed = {
            Path("core/wechat_ui_runtime.py"),
            Path("feature/contact_auto_collector_worker.py"),
            Path("feature/friend_request_senders.py"),
        }
        patterns = (
            re.compile(r"from\s+wxautox4\s+import\s+uia\b"),
            re.compile(r"from\s+wxautox4\.ui\.component\s+import\b"),
            re.compile(r"\bListControl\s*\("),
        )
        hits = []
        for path in production_files():
            if path.suffix.lower() != ".py":
                continue
            relative = path.relative_to(ROOT)
            if relative in allowed:
                continue
            text = path.read_text(encoding="utf-8", errors="ignore")
            if any(pattern.search(text) for pattern in patterns):
                hits.append(str(relative))
        self.assertEqual(hits, [])

    def test_production_has_no_to_text_calls(self):
        hits = []
        for path in production_files():
            if path.suffix.lower() != ".py":
                continue
            if ".to_text(" in path.read_text(encoding="utf-8", errors="ignore"):
                hits.append(str(path.relative_to(ROOT)))
        self.assertEqual(hits, [])

    def test_message_download_calls_are_limited_to_ui_runtime(self):
        allowed = {Path("core/wechat_ui_runtime.py")}
        pattern = re.compile(r"\.(?:download|download_quote_image)\s*\(")
        hits = []
        for path in production_files():
            if path.suffix.lower() != ".py" or path.relative_to(ROOT) in allowed:
                continue
            if pattern.search(path.read_text(encoding="utf-8", errors="ignore")):
                hits.append(str(path.relative_to(ROOT)))
        self.assertEqual(hits, [])

    def test_business_layer_does_not_import_raw_wxautox_messages(self):
        text = (ROOT / "wxbot_core.py").read_text(encoding="utf-8")
        self.assertNotIn("from wxautox4.msgs import", text)

    def test_production_has_no_wechat_cli_integration(self):
        hits = []
        patterns = (
            re.compile(r"\bwechat_cli\b"),
            re.compile(r"wechat-cli-card"),
            re.compile(r"core\.local_wechat_reader"),
            re.compile(r"WXBOT_WECHAT_CLI"),
        )
        for path in production_files():
            text = path.read_text(encoding="utf-8", errors="ignore")
            if any(pattern.search(text) for pattern in patterns):
                hits.append(str(path.relative_to(ROOT)))
        self.assertEqual(hits, [])

    def test_removed_legacy_send_locks_do_not_return(self):
        text = (ROOT / "wxbot_core.py").read_text(encoding="utf-8")
        self.assertNotIn("class _WechatActionLockedChat", text)
        self.assertNotIn("_chat_send_locks", text)

    def test_moments_and_contact_edit_have_no_business_thread_ui_fallback(self):
        core_text = (ROOT / "wxbot_core.py").read_text(encoding="utf-8")
        contacts_text = (ROOT / "feature" / "contacts.py").read_text(encoding="utf-8")
        self.assertNotIn("self.wx.Moments(", core_text)
        self.assertNotIn("bot.wx.EditFriendInfo(", contacts_text)
        self.assertNotIn('getattr(bot.wx, "ChatWith"', contacts_text)

    def test_relationship_scan_has_no_business_thread_ui_fallback(self):
        text = (ROOT / "feature" / "relationship_scan.py").read_text(encoding="utf-8")
        self.assertNotIn("bot.wx.SessionBox", text)
        self.assertNotIn(".GetSession(", text)

    def test_removed_group_welcome_probability_does_not_return(self):
        checked_paths = (
            ROOT / "core" / "wxbot_config.py",
            ROOT / "feature" / "listening.py",
            ROOT / "templates" / "dashboard.html",
            ROOT / "templates" / "static" / "dashboard.css",
            ROOT / "web_server.py",
        )
        hits = [
            str(path.relative_to(ROOT))
            for path in checked_paths
            if "group_welcome_random" in path.read_text(encoding="utf-8")
        ]
        self.assertEqual(hits, [])

    def test_removed_first_reply_delay_and_speed_modes_do_not_return(self):
        checked_paths = (
            ROOT / "core" / "wxbot_config.py",
            ROOT / "wxbot_core.py",
            ROOT / "templates" / "dashboard.html",
            ROOT / "web_server.py",
        )
        removed_names = (
            "reply_delay_switch",
            "reply_delay_first_min",
            "reply_delay_first_max",
            "reply_delay_split_speed_mode",
            "reply_delay_split_min",
            "reply_delay_split_max",
        )
        hits = [
            f"{path.relative_to(ROOT)}:{name}"
            for path in checked_paths
            for name in removed_names
            if re.search(rf"\b{re.escape(name)}\b", path.read_text(encoding="utf-8"))
        ]
        self.assertEqual(hits, [])

    def test_quote_followups_cannot_return_as_one_owner_action(self):
        core_text = (ROOT / "wxbot_core.py").read_text(encoding="utf-8")
        runtime_text = (ROOT / "core" / "wechat_ui_runtime.py").read_text(encoding="utf-8")
        self.assertNotIn("_ui_quote_message_batch", core_text)
        self.assertNotIn("followup_texts", runtime_text)


if __name__ == "__main__":
    unittest.main()
