import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from core.ui_restart_policy import allow_restart


class UIRestartPolicyTests(unittest.TestCase):
    def test_fourth_stall_inside_rolling_window_is_rejected(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "history.txt"
            now = datetime(2026, 7, 11, 9, 0, tzinfo=timezone.utc)

            self.assertTrue(allow_restart(path, now=now))
            self.assertTrue(allow_restart(path, now=now + timedelta(minutes=1)))
            self.assertTrue(allow_restart(path, now=now + timedelta(minutes=2)))
            self.assertFalse(allow_restart(path, now=now + timedelta(minutes=3)))

            self.assertEqual(len(path.read_text(encoding="utf-8").splitlines()), 3)

    def test_expired_and_malformed_entries_do_not_consume_limit(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "history.txt"
            now = datetime(2026, 7, 11, 9, 0, tzinfo=timezone.utc)
            path.write_text(
                "bad-value\n" + (now - timedelta(minutes=31)).isoformat() + "\n",
                encoding="utf-8",
            )

            self.assertTrue(allow_restart(path, now=now))
            self.assertEqual(path.read_text(encoding="utf-8").splitlines(), [now.isoformat()])


if __name__ == "__main__":
    unittest.main()
