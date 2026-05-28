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

    def to_dict(self):
        return {
            "update_id": self.update_id,
            "message": {
                "message_id": 10,
                "chat": {"id": self.effective_chat.id, "type": self.effective_chat.type},
                "from": {"id": self.effective_user.id},
                "text": self.effective_message.text,
            },
        }


class PokerRoomIsolationFilterTests(unittest.TestCase):
    def _config(self, chat_id: int = -1003989263256, thread_id: int | None = 77):
        import poker_room_handlers

        return poker_room_handlers.RoomConfig(
            chat_id=chat_id,
            thread_id=thread_id,
            admin_user_ids={1},
            state_path=Path("/tmp/poker-room-state.json"),
        )

    def _message(self, chat_id: int, thread_id: int | None):
        msg = type("Msg", (), {})()
        msg.chat = type("Chat", (), {"id": chat_id})()
        msg.chat_id = chat_id
        msg.message_thread_id = thread_id
        return msg

    def test_filter_is_inert_when_config_is_none(self) -> None:
        flt = main.build_not_in_poker_room_filter(None)
        self.assertTrue(flt.filter(self._message(-12345, None)))

    def test_filter_allows_other_chats_and_topics_when_thread_is_set(self) -> None:
        flt = main.build_not_in_poker_room_filter(self._config(chat_id=-1, thread_id=10))
        self.assertTrue(flt.filter(self._message(-2, None)))
        self.assertTrue(flt.filter(self._message(-1, 11)))

    def test_filter_blocks_messages_inside_configured_chat_and_thread(self) -> None:
        flt = main.build_not_in_poker_room_filter(self._config(chat_id=-1, thread_id=10))
        self.assertFalse(flt.filter(self._message(-1, 10)))

    def test_filter_blocks_all_threads_when_thread_id_is_none(self) -> None:
        flt = main.build_not_in_poker_room_filter(self._config(chat_id=-1, thread_id=None))
        self.assertFalse(flt.filter(self._message(-1, 0)))
        self.assertFalse(flt.filter(self._message(-1, 99)))


class MainLoggingTests(unittest.TestCase):
    def test_update_summary_includes_chat_user_and_text(self) -> None:
        summary = main.update_summary(FakeUpdate())

        self.assertIn("update_id=42", summary)
        self.assertIn("chat=-1003989263256", summary)
        self.assertIn("chat_type=supergroup", summary)
        self.assertIn("user=268887491", summary)
        self.assertIn("text='сяду'", summary)
        self.assertIn("keys=message,update_id", summary)
        self.assertIn("payload_message_text='сяду'", summary)

    def test_configure_logging_writes_to_configured_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "bot.log"
            with patch.dict("os.environ", {"BOT_LOG_PATH": str(path)}, clear=False):
                main.configure_logging()

            logging.getLogger("poker-test").info("file logging probe")
            for handler in logging.getLogger().handlers:
                handler.flush()

            self.assertIn("file logging probe", path.read_text(encoding="utf-8"))

    def test_run_application_uses_polling_without_webhook_url(self) -> None:
        app = FakeApplication()

        with patch.dict("os.environ", {}, clear=True):
            main.run_application(app)

        self.assertEqual(app.polling_calls, [{"allowed_updates": main.Update.ALL_TYPES}])
        self.assertEqual(app.webhook_calls, [])

    def test_run_application_uses_webhook_when_configured(self) -> None:
        app = FakeApplication()

        with patch.dict(
            "os.environ",
            {
                "WEBHOOK_URL": "https://example.com/poker-hook",
                "WEBHOOK_LISTEN": "0.0.0.0",
                "WEBHOOK_PORT": "8443",
                "WEBHOOK_PATH": "poker-hook",
                "WEBHOOK_SECRET_TOKEN": "secret",
                "WEBHOOK_DROP_PENDING_UPDATES": "1",
            },
            clear=True,
        ):
            main.run_application(app)

        self.assertEqual(app.polling_calls, [])
        self.assertEqual(
            app.webhook_calls,
            [
                {
                    "listen": "0.0.0.0",
                    "port": 8443,
                    "url_path": "poker-hook",
                    "webhook_url": "https://example.com/poker-hook",
                    "secret_token": "secret",
                    "cert": None,
                    "key": None,
                    "ip_address": None,
                    "allowed_updates": main.Update.ALL_TYPES,
                    "drop_pending_updates": True,
                }
            ],
        )

    def test_run_application_builds_webhook_url_from_public_ip(self) -> None:
        app = FakeApplication()

        with patch.dict(
            "os.environ",
            {
                "WEBHOOK_PUBLIC_IP": "158.160.97.8",
                "WEBHOOK_LISTEN": "0.0.0.0",
                "WEBHOOK_PORT": "8443",
                "WEBHOOK_PATH": "poker-secret",
                "WEBHOOK_CERT": "/etc/poker/webhook.pem",
                "WEBHOOK_KEY": "/etc/poker/webhook.key",
            },
            clear=True,
        ):
            main.run_application(app)

        self.assertEqual(app.polling_calls, [])
        self.assertEqual(
            app.webhook_calls,
            [
                {
                    "listen": "0.0.0.0",
                    "port": 8443,
                    "url_path": "poker-secret",
                    "webhook_url": "https://158.160.97.8:8443/poker-secret",
                    "secret_token": None,
                    "cert": "/etc/poker/webhook.pem",
                    "key": "/etc/poker/webhook.key",
                    "ip_address": "158.160.97.8",
                    "allowed_updates": main.Update.ALL_TYPES,
                    "drop_pending_updates": False,
                }
            ],
        )


class FakeApplication:
    def __init__(self) -> None:
        self.polling_calls: list[dict] = []
        self.webhook_calls: list[dict] = []

    def run_polling(self, **kwargs) -> None:
        self.polling_calls.append(kwargs)

    def run_webhook(self, **kwargs) -> None:
        self.webhook_calls.append(kwargs)


if __name__ == "__main__":
    unittest.main()
