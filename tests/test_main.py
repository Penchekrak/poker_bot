from __future__ import annotations

import logging
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

import main
from telegram import Chat, Message, Update, User


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


class RecordingApplication:
    def __init__(self) -> None:
        self.handlers: list[tuple[object, int]] = []

    def add_handler(self, handler, group: int = 0) -> None:
        self.handlers.append((handler, group))


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

    def test_dedicated_filter_is_disabled_without_room_config(self) -> None:
        flt = main.build_in_poker_room_filter(None)
        self.assertFalse(flt.filter(self._message(-1, 10)))

    def test_dedicated_filter_allows_only_configured_chat_and_thread(self) -> None:
        flt = main.build_in_poker_room_filter(self._config(chat_id=-1, thread_id=10))
        self.assertTrue(flt.filter(self._message(-1, 10)))
        self.assertFalse(flt.filter(self._message(-1, 11)))
        self.assertFalse(flt.filter(self._message(-2, 10)))

    def test_dedicated_filter_allows_configured_chat_when_thread_is_unset(self) -> None:
        flt = main.build_in_poker_room_filter(self._config(chat_id=-1, thread_id=None))
        self.assertTrue(flt.filter(self._message(-1, 0)))
        self.assertTrue(flt.filter(self._message(-1, 99)))


class HandlerRegistrationTests(unittest.TestCase):
    def test_liars_bar_is_registered_without_displacing_poker_room_handlers(self) -> None:
        app = RecordingApplication()
        config = main.poker_room_handlers.RoomConfig(
            chat_id=-1,
            thread_id=10,
            admin_user_ids={1},
            state_path=Path("/tmp/poker-room-state.json"),
        )
        outside_wrapped_callbacks = []
        dedicated_wrapped_callbacks = []

        def record_outside_callback_wrapper(callback, callback_config):
            outside_wrapped_callbacks.append((callback, callback_config))
            return callback

        def record_dedicated_callback_wrapper(callback, callback_config):
            dedicated_wrapped_callbacks.append((callback, callback_config))
            return callback

        with (
            patch.object(main.poker_room_handlers.RoomConfig, "from_env", return_value=config),
            patch.object(
                main,
                "_wrap_callback_outside_poker_room",
                side_effect=record_outside_callback_wrapper,
            ),
            patch.object(
                main,
                "_wrap_callback_in_poker_room",
                side_effect=record_dedicated_callback_wrapper,
            ),
        ):
            main.register_handlers(app)

        command_handlers = [
            handler for handler, _group in app.handlers if isinstance(handler, main.CommandHandler)
        ]
        callback_handlers = [
            handler for handler, _group in app.handlers if isinstance(handler, main.CallbackQueryHandler)
        ]

        liars_command = next(
            handler for handler in command_handlers if "liars_bar" in handler.commands
        )
        self.assertEqual(liars_command.commands, frozenset({"liars_bar", "liars"}))
        self.assertIs(liars_command.callback, main.liars_bar_command)

        def liars_filter_matches(chat_id: int, thread_id: int | None) -> bool:
            message = Message(
                message_id=1,
                date=datetime.now(timezone.utc),
                chat=Chat(chat_id, "supergroup"),
                from_user=User(1, "Player", False),
                text="/liars",
                message_thread_id=thread_id,
            )
            return bool(liars_command.filters.check_update(Update(1, message=message)))

        self.assertTrue(liars_filter_matches(-1, 10))
        self.assertFalse(liars_filter_matches(-1, 11))
        self.assertFalse(liars_filter_matches(-2, 10))

        callbacks_by_pattern = {
            handler.pattern.pattern: handler
            for handler in callback_handlers
            if handler.pattern is not None
        }
        self.assertIs(callbacks_by_pattern[r"^lb:"].callback, main.liars_bar_callback)
        self.assertIn((main.liars_bar_callback, config), dedicated_wrapped_callbacks)
        self.assertNotIn((main.liars_bar_callback, config), outside_wrapped_callbacks)
        self.assertIn((main.heads_up_callback, config), outside_wrapped_callbacks)
        self.assertIn((main.blackjack_callback, config), outside_wrapped_callbacks)

        poker_command = next(handler for handler in command_handlers if "poker" in handler.commands)
        self.assertIs(poker_command.callback, main.poker_room_command)
        self.assertIs(callbacks_by_pattern[main.room_callback_pattern()].callback, main.poker_room_callback)
        self.assertTrue(
            any(
                isinstance(handler, main.MessageHandler)
                and handler.callback is main.poker_room_message
                and group == -1
                for handler, group in app.handlers
            )
        )


class PokerRoomCallbackIsolationTests(unittest.IsolatedAsyncioTestCase):
    def _update(self, chat_id: int, thread_id: int | None):
        message = type(
            "Message",
            (),
            {"chat_id": chat_id, "message_thread_id": thread_id},
        )()

        class Query:
            def __init__(self) -> None:
                self.message = message
                self.answers = []

            async def answer(self, text: str = "", **kwargs) -> None:
                self.answers.append((text, kwargs))

        query = Query()
        return type("Update", (), {"callback_query": query})()

    async def test_regular_wrapper_suppresses_callback_inside_dedicated_poker_topic(self) -> None:
        config = PokerRoomIsolationFilterTests()._config(chat_id=-1, thread_id=10)
        calls = []

        async def callback(update, context) -> None:
            calls.append((update, context))

        wrapped = main._wrap_callback_outside_poker_room(callback, config)
        update = self._update(-1, 10)
        await wrapped(update, object())

        self.assertEqual(calls, [])
        self.assertTrue(update.callback_query.answers)

    async def test_regular_wrapper_forwards_callback_outside_dedicated_poker_topic(self) -> None:
        config = PokerRoomIsolationFilterTests()._config(chat_id=-1, thread_id=10)
        calls = []

        async def callback(update, context) -> None:
            calls.append((update, context))

        wrapped = main._wrap_callback_outside_poker_room(callback, config)
        update = self._update(-1, 11)
        context = object()
        await wrapped(update, context)

        self.assertEqual(calls, [(update, context)])
        self.assertEqual(update.callback_query.answers, [])

    async def test_dedicated_wrapper_forwards_liars_callback_inside_topic(self) -> None:
        config = PokerRoomIsolationFilterTests()._config(chat_id=-1, thread_id=10)
        calls = []

        async def callback(update, context) -> None:
            calls.append((update, context))

        wrapped = main._wrap_callback_in_poker_room(callback, config)
        update = self._update(-1, 10)
        context = object()
        await wrapped(update, context)

        self.assertEqual(calls, [(update, context)])
        self.assertEqual(update.callback_query.answers, [])

    async def test_dedicated_wrapper_suppresses_liars_callback_outside_topic(self) -> None:
        config = PokerRoomIsolationFilterTests()._config(chat_id=-1, thread_id=10)
        calls = []

        async def callback(update, context) -> None:
            calls.append((update, context))

        wrapped = main._wrap_callback_in_poker_room(callback, config)
        update = self._update(-1, 11)
        await wrapped(update, object())

        self.assertEqual(calls, [])
        self.assertTrue(update.callback_query.answers)
        self.assertIn("только", update.callback_query.answers[-1][0])


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
