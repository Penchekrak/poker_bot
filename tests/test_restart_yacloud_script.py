from __future__ import annotations

import subprocess
import unittest
from pathlib import Path


class RestartYacloudScriptTests(unittest.TestCase):
    def setUp(self) -> None:
        self.repo_root = Path(__file__).resolve().parents[1]
        self.script = self.repo_root / "dev" / "restart_yacloud.sh"

    def run_script(self, *args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["bash", str(self.script), "--dry-run", *args],
            cwd=self.repo_root,
            text=True,
            capture_output=True,
            check=False,
        )

    def test_default_restart_preserves_remote_state_and_logs(self) -> None:
        result = self.run_script()

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("dev/deploy_yacloud.sh --run", result.stdout)
        self.assertNotIn("rm -f", result.stdout)
        self.assertNotIn(": >", result.stdout)

    def test_reset_all_clears_remote_state_backup_and_logs_before_restart(self) -> None:
        result = self.run_script("--reset-all")

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("${BOT_LOG_PATH:-bot.log}", result.stdout)
        self.assertIn("nohup.out", result.stdout)
        self.assertIn("${POKER_STATE_PATH:-data/poker_room_state.json}", result.stdout)
        self.assertIn("${POKER_STATE_PATH:-data/poker_room_state.json}.bak", result.stdout)
        self.assertIn("dev/deploy_yacloud.sh --run", result.stdout)


if __name__ == "__main__":
    unittest.main()
