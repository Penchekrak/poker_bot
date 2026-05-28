from __future__ import annotations

import logging
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import main


class FakeMessage:
    text = "сяду"
    caption = None
    message_thread_id = None


class FakeChat:
    id = -1003989263256
    type = "supergroup"


class FakeUser:
    id = 268887491


class FakeUpdate:
    update_id = 42
    effective_chat = FakeChat()
    effective_user = FakeUser()
    effective_message = FakeMessage()
    callback_query = None


class MainLoggingTests(unittest.TestCase):
    def test_update_summary_includes_chat_user_and_text(self) -> None:
        summary = main.update_summary(FakeUpdate())

        self.assertIn("update_id=42", summary)
        self.assertIn("chat=-1003989263256", summary)
        self.assertIn("chat_type=supergroup", summary)
        self.assertIn("user=268887491", summary)
        self.assertIn("text='сяду'", summary)

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
