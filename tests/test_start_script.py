from __future__ import annotations

import os
import shutil
import stat
import subprocess
import tempfile
import unittest
from pathlib import Path


class StartScriptTests(unittest.TestCase):
    def test_env_file_is_loaded_even_when_bot_token_is_already_exported(self) -> None:
        repo_root = Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory() as tmp:
            workdir = Path(tmp)
            shutil.copy(repo_root / "start.sh", workdir / "start.sh")
            (workdir / ".env").write_text(
                "BOT_TOKEN=from-file\nLLM_API_KEY=secret-from-env\n",
                encoding="utf-8",
            )
            fake_python = workdir / ".venv" / "bin" / "python"
            fake_python.parent.mkdir(parents=True)
            fake_python.write_text(
                "#!/usr/bin/env bash\n"
                "printf 'BOT_TOKEN=%s\\nLLM_API_KEY=%s\\n' \"$BOT_TOKEN\" \"$LLM_API_KEY\"\n",
                encoding="utf-8",
            )
            fake_python.chmod(fake_python.stat().st_mode | stat.S_IXUSR)
            env = {**os.environ, "BOT_TOKEN": "from-parent"}

            result = subprocess.run(
                ["bash", str(workdir / "start.sh")],
                cwd=workdir,
                env=env,
                text=True,
                capture_output=True,
                check=False,
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("BOT_TOKEN=from-file", result.stdout)
            self.assertIn("LLM_API_KEY=secret-from-env", result.stdout)


if __name__ == "__main__":
    unittest.main()
