import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
LAUNCHER = ROOT / "\u6253\u5f00\u8f6f\u4ef6.bat"


@unittest.skipUnless(os.name == "nt", "Windows launcher integration test")
class WindowsLauncherRecoveryTests(unittest.TestCase):
    def _run_launcher(self, server_exit_code):
        with tempfile.TemporaryDirectory(prefix="wxbot_launcher_test_") as temp_dir:
            workspace = Path(temp_dir)
            shutil.copy2(LAUNCHER, workspace / LAUNCHER.name)
            (workspace / "web_server.py").write_text(
                """from pathlib import Path
import os

path = Path('server_runs.txt')
count = int(path.read_text(encoding='utf-8') or '0') if path.exists() else 0
path.write_text(str(count + 1), encoding='utf-8')
raise SystemExit(int(os.environ['WXBOT_TEST_SERVER_EXIT']))
""",
                encoding="utf-8",
            )

            junction = workspace / "venv"
            junction_result = subprocess.run(
                [os.environ.get("COMSPEC", "cmd.exe"), "/d", "/c", "mklink", "/J", str(junction), str(ROOT / "venv")],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=10,
            )
            self.assertEqual(junction_result.returncode, 0, junction_result.stdout + junction_result.stderr)

            fake_bin = workspace / "fake-bin"
            fake_bin.mkdir()
            for name in ("ffmpeg.exe", "ffprobe.exe"):
                shutil.copy2(os.environ.get("COMSPEC", r"C:\Windows\System32\cmd.exe"), fake_bin / name)

            env = dict(os.environ)
            env.update({
                "PATH": f"{fake_bin}{os.pathsep}{env.get('PATH', '')}",
                "PYTHONPATH": str(ROOT),
                "PYTHONUTF8": "1",
                "PYTHONIOENCODING": "utf-8",
                "WXBOT_TEST_SERVER_EXIT": str(server_exit_code),
            })
            result = subprocess.run(
                [os.environ.get("COMSPEC", "cmd.exe"), "/d", "/c", str(workspace / LAUNCHER.name)],
                cwd=workspace,
                env=env,
                stdin=subprocess.DEVNULL,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=60,
            )
            run_count = int((workspace / "server_runs.txt").read_text(encoding="utf-8"))
            history_path = workspace / "runtime" / "ui_restart_history.txt"
            history = history_path.read_text(encoding="utf-8").splitlines() if history_path.exists() else []
            return result, run_count, history

    def test_stuck_exit_restarts_three_times_then_stops(self):
        result, run_count, history = self._run_launcher(86)

        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(run_count, 4)
        self.assertEqual(len(history), 3)
        self.assertIn("more than 3 times in 30 minutes", result.stdout)

    def test_non_stuck_exit_does_not_restart(self):
        result, run_count, history = self._run_launcher(7)

        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(run_count, 1)
        self.assertEqual(history, [])


if __name__ == "__main__":
    unittest.main()
