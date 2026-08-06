import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

from core.wechat_recovery import (
    UI_OWNER_LOCK_RECOVERY_GRACE_SECONDS,
    UI_STUCK_EXIT_CODE,
    UI_STUCK_STOPPED_EXIT_CODE,
)
from core.wechat_ui_actions import (
    WXAUTOX_UI_LOCK_TIMEOUT_SECONDS,
    WeChatUIOwner,
)
from feature.contacts import AUTO_MAINTENANCE_COLLECT_HARD_TIMEOUT_SECONDS


REPO_ROOT = Path(__file__).resolve().parents[1]


def _run_fault_script(script, cwd):
    env = dict(os.environ)
    env["PYTHONUTF8"] = "1"
    env["PYTHONPATH"] = str(REPO_ROOT)
    return subprocess.run(
        [sys.executable, "-X", "utf8", "-c", script],
        cwd=cwd,
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=10,
    )


class UIStuckRecoveryTests(unittest.TestCase):
    def test_production_deadlines_leave_time_for_kernel_lock_timeout_handling(self):
        owner = WeChatUIOwner({})
        self.assertEqual(WXAUTOX_UI_LOCK_TIMEOUT_SECONDS, 30.0)
        self.assertEqual(UI_OWNER_LOCK_RECOVERY_GRACE_SECONDS, 10.0)
        self.assertEqual(owner._light_timeout, 30.0)
        self.assertEqual(owner._exclusive_timeout, 180.0)
        self.assertEqual(AUTO_MAINTENANCE_COLLECT_HARD_TIMEOUT_SECONDS, 300)

    def test_light_and_exclusive_watchdogs_exit_only_with_stuck_code(self):
        for kind in ("SEND_TEXT", "SEND_FILE"):
            with self.subTest(kind=kind), tempfile.TemporaryDirectory() as temp_dir:
                script = f"""
import threading
import time
from core import wechat_recovery, wechat_ui_actions
from wxbot_core import WXBot

kind = wechat_ui_actions.UIIntentKind.{kind}
owner = wechat_ui_actions.WeChatUIOwner(
    {{kind: lambda _payload: threading.Event().wait(5)}},
    light_timeout=0.15,
    exclusive_timeout=0.15,
)
bot = WXBot.__new__(WXBot)
bot._ui_owner = owner
bot.is_stop_requested = lambda: False
owner.start()
watchdog = wechat_recovery.UIWatchdog(
    owner.current_action_snapshot,
    bot._handle_ui_owner_timeout,
    poll_interval=0.01,
    recovery_grace_seconds=0.05,
)
watchdog.start()
owner.submit(wechat_ui_actions.UIIntent(kind, {{"conversation": "fault-test"}}))
time.sleep(3)
raise SystemExit(99)
"""
                result = _run_fault_script(script, temp_dir)
                self.assertEqual(result.returncode, UI_STUCK_EXIT_CODE, result.stdout + result.stderr)

    def test_watchdog_does_not_exit_when_owner_returns_during_recovery_grace(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            script = """
import threading
import time
from core import wechat_recovery, wechat_ui_actions
from wxbot_core import WXBot

kind = wechat_ui_actions.UIIntentKind.SEND_TEXT
owner = wechat_ui_actions.WeChatUIOwner(
    {kind: lambda _payload: threading.Event().wait(0.2)},
    light_timeout=0.15,
    exclusive_timeout=0.15,
)
bot = WXBot.__new__(WXBot)
bot._ui_owner = owner
bot.is_stop_requested = lambda: False
owner.start()
watchdog = wechat_recovery.UIWatchdog(
    owner.current_action_snapshot,
    bot._handle_ui_owner_timeout,
    poll_interval=0.01,
    recovery_grace_seconds=0.15,
)
watchdog.start()
owner.call(wechat_ui_actions.UIIntent(kind, {"conversation": "grace-test"}))
time.sleep(0.2)
raise SystemExit(0)
"""
            result = _run_fault_script(script, temp_dir)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_stop_time_stall_writes_scheduled_start_suppression_marker(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            script = """
from core.wechat_ui_actions import CurrentActionSnapshot
from wxbot_core import WXBot

bot = WXBot.__new__(WXBot)
bot._ui_owner = None
bot.is_stop_requested = lambda: True
bot._handle_ui_owner_timeout(CurrentActionSnapshot(kind="shutdown", started_at=1, deadline_at=2))
"""
            started_at = time.time()
            result = _run_fault_script(script, temp_dir)
            marker = Path(temp_dir) / "runtime" / "suppress_scheduled_bot_start_until.txt"

            self.assertEqual(result.returncode, UI_STUCK_STOPPED_EXIT_CODE, result.stdout + result.stderr)
            self.assertTrue(marker.exists())
            self.assertGreater(float(marker.read_text(encoding="utf-8")), started_at + 290)


if __name__ == "__main__":
    unittest.main()
