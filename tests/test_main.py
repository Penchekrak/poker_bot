from __future__ import annotations

import logging
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import main


class MainLoggingTests(unittest.TestCase):
    def test_configure_logging_writes_to_configured_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "bot.log"
            with patch.dict("os.environ", {"BOT_LOG_PATH": str(path)}, clear=False):
                main.configure_logging()

            logging.getLogger("poker-test").info("file logging probe")
            for handler in logging.getLogger().handlers:
                handler.flush()

            self.assertIn("file logging probe", path.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
