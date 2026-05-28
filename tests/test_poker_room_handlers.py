from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import poker_room
import poker_room_handlers
import poker_room_llm


class FakeUser:
    def __init__(self, user_id: int, username: str, full_name: str) -> None:
        self.id = user_id
        self.username = username
        self.full_name = full_name


class FakeChat:
    def __init__(self, chat_id: int) -> None:
        self.id = chat_id


class FakeMessage:
    def __init__(self, text: str, chat_id: int = 10, thread_id: int = 77) -> None:
        self.text = text
        self.chat_id = chat_id
        self.message_thread_id = thread_id
        self.reply_texts: list[tuple[str, object | None]] = []

    async def reply_text(self, text: str, reply_markup=None, **kwargs) -> None:
        self.reply_texts.append((text, reply_markup))


class FakeCallbackQuery:
    def __init__(self, data: str, user: FakeUser, chat_id: int = 10, thread_id: int = 77) -> None:
        self.data = data
        self.message = type("Msg", (), {"chat_id": chat_id, "message_thread_id": thread_id})()
        self.from_user = user
        self.answers: list[tuple[str, bool]] = []

    async def answer(self, text: str = "", show_alert: bool = False, **kwargs) -> None:
        self.answers.append((text, show_alert))


class FakeUpdate:
    def __init__(self, user: FakeUser, message: FakeMessage | None = None, query: FakeCallbackQuery | None = None) -> None:
        self.effective_user = user
        self.effective_message = message
        self.effective_chat = FakeChat(message.chat_id if message else query.message.chat_id)
        self.callback_query = query


class FakeBot:
    def __init__(self) -> None:
        self.sent_photos: list[dict] = []
        self.edited_media: list[dict] = []
        self.sent_messages: list[dict] = []

    async def send_photo(self, **kwargs):
        self.sent_photos.append(kwargs)
        return type("Sent", (), {"message_id": 500 + len(self.sent_photos)})()

    async def edit_message_media(self, **kwargs):
        self.edited_media.append(kwargs)

    async def send_message(self, **kwargs):
        self.sent_messages.append(kwargs)


class FakeJobQueue:
    def __init__(self) -> None:
        self.jobs: list[tuple[str, float]] = []

    def get_jobs_by_name(self, name: str):
        return []

    def run_once(self, callback, when, name=None, data=None):
        self.jobs.append((name, when))
        return object()


class FakeContext:
    def __init__(self, config: poker_room_handlers.RoomConfig) -> None:
        self.bot = FakeBot()
        self.job_queue = FakeJobQueue()
        self.bot_data = {"poker_room_config": config}


class PokerRoomHandlerTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.config = poker_room_handlers.RoomConfig(
            chat_id=10,
            thread_id=77,
            admin_user_ids={1},
            state_path=Path(self.tmp.name) / "room.json",
            render_dir=Path(self.tmp.name) / "renders",
        )
        poker_room_handlers.reset_room_for_tests()

    def tearDown(self) -> None:
        self.tmp.cleanup()

    async def test_wrong_forum_topic_is_silently_ignored(self) -> None:
        context = FakeContext(self.config)
        message = FakeMessage("сяду", chat_id=10, thread_id=78)
        update = FakeUpdate(FakeUser(1, "alice", "Alice"), message=message)

        await poker_room_handlers.poker_room_message(update, context)

        self.assertEqual(message.reply_texts, [])
        self.assertEqual(context.bot.sent_photos, [])

    async def test_chat_only_room_accepts_any_topic_in_configured_chat(self) -> None:
        chat_only_config = poker_room_handlers.RoomConfig(
            chat_id=10,
            thread_id=None,
            admin_user_ids={1},
            state_path=self.config.state_path,
            render_dir=self.config.render_dir,
        )
        context = FakeContext(chat_only_config)
        message = FakeMessage("сяду", chat_id=10, thread_id=999)
        update = FakeUpdate(FakeUser(1, "alice", "Alice"), message=message)

        await poker_room_handlers.poker_room_message(update, context)

        self.assertIn("подтверди", context.bot.sent_messages[0]["text"].lower())

    async def test_join_text_requires_confirmation_then_persists_and_schedules_deal(self) -> None:
        context = FakeContext(self.config)
        user = FakeUser(1, "alice", "Alice")
        message = FakeMessage("сяду")

        await poker_room_handlers.poker_room_message(FakeUpdate(user, message=message), context)

        self.assertEqual(context.bot.sent_messages[0]["chat_id"], self.config.chat_id)
        self.assertNotIn("reply_to_message_id", context.bot.sent_messages[0])
        self.assertIn("подтверди", context.bot.sent_messages[0]["text"].lower())
        button = context.bot.sent_messages[0]["reply_markup"].inline_keyboard[0][0]
        query = FakeCallbackQuery(button.callback_data, user)
        await poker_room_handlers.poker_room_callback(FakeUpdate(user, query=query), context)

        room = poker_room_handlers.get_room(context)
        self.assertIn(1, room.seats)
        self.assertTrue(self.config.state_path.exists())

    async def test_free_form_room_intent_uses_llm_parser_then_confirmation(self) -> None:
        context = FakeContext(self.config)
        user = FakeUser(1, "alice", "Alice")
        message = FakeMessage("можно я присяду за стол?")

        with patch.object(
            poker_room_handlers.poker_room_llm,
            "parse_room_intent_with_fallback",
            return_value=poker_room_llm.ParsedIntent(
                kind="room_intent",
                room_intent=poker_room.ROOM_JOIN,
                confidence=0.88,
            ),
        ) as parser:
            await poker_room_handlers.poker_room_message(FakeUpdate(user, message=message), context)

        self.assertEqual(parser.call_args.args[0], "можно я присяду за стол?")
        self.assertEqual(context.bot.sent_messages[0]["text"], "Подтверди.")
        button = context.bot.sent_messages[0]["reply_markup"].inline_keyboard[0][0]
        self.assertEqual(button.callback_data, "pr:confirm:join:1")

    async def test_poker_command_opens_room_for_admin(self) -> None:
        context = FakeContext(self.config)
        room = poker_room_handlers.get_room(context)
        room.is_open = False
        admin = FakeUser(1, "alice", "Alice")
        message = FakeMessage("/poker")

        await poker_room_handlers.poker_room_command(FakeUpdate(admin, message=message), context)

        self.assertTrue(room.is_open)
        self.assertEqual(context.bot.sent_messages[-1]["text"], "Стол открыт.")
        self.assertTrue(self.config.state_path.exists())

    async def test_poker_command_rejects_non_admin(self) -> None:
        context = FakeContext(self.config)
        user = FakeUser(2, "bob", "Bob")
        message = FakeMessage("/poker")

        await poker_room_handlers.poker_room_command(FakeUpdate(user, message=message), context)

        self.assertEqual(context.bot.sent_messages[-1]["text"], "Только админ стола.")

    async def test_private_cards_callback_returns_only_clicking_players_hand(self) -> None:
        context = FakeContext(self.config)
        room = poker_room_handlers.get_room(context)
        room.confirm_room_intent(1, "alice", "Alice", poker_room.ROOM_JOIN)
        room.confirm_room_intent(2, "bob", "Bob", poker_room.ROOM_JOIN)
        room.start_hand(
            deck=poker_room.stacked_deck(
                {
                    1: ("Ah", "Ad"),
                    2: ("Kh", "Kd"),
                    "board": ("2c", "7d", "9h", "Ts", "3c"),
                }
            )
        )

        query = FakeCallbackQuery("pr:cards", FakeUser(1, "alice", "Alice"))
        await poker_room_handlers.poker_room_callback(FakeUpdate(query.from_user, query=query), context)

        self.assertEqual(query.answers[-1][0], "Твои карты: A♦ · A♥")
        self.assertTrue(query.answers[-1][1])

    async def test_current_actor_chat_applies_action_and_schedules_turn_timer(self) -> None:
        context = FakeContext(self.config)
        room = poker_room_handlers.get_room(context)
        room.confirm_room_intent(1, "alice", "Alice", poker_room.ROOM_JOIN)
        room.confirm_room_intent(2, "bob", "Bob", poker_room.ROOM_JOIN)
        room.start_hand()

        message = FakeMessage("raise to 500")
        await poker_room_handlers.poker_room_message(FakeUpdate(FakeUser(1, "alice", "Alice"), message=message), context)

        hand = room.current_hand
        self.assertIsNotNone(hand)
        self.assertEqual(hand.current_bet, 500)
        self.assertIn(("poker-room-turn", poker_room.TURN_TIMEOUT_SECONDS), context.job_queue.jobs)

    async def test_admin_close_requires_admin_and_confirmation(self) -> None:
        context = FakeContext(self.config)
        user = FakeUser(2, "bob", "Bob")
        message = FakeMessage("закрыть стол")

        await poker_room_handlers.poker_room_message(FakeUpdate(user, message=message), context)

        self.assertIn("админ", context.bot.sent_messages[0]["text"].lower())

        admin = FakeUser(1, "alice", "Alice")
        admin_message = FakeMessage("закрыть стол")
        await poker_room_handlers.poker_room_message(FakeUpdate(admin, message=admin_message), context)

        button = context.bot.sent_messages[1]["reply_markup"].inline_keyboard[0][0]
        self.assertEqual(button.callback_data, "pr:admin:close:1")

        query = FakeCallbackQuery(button.callback_data, admin)
        await poker_room_handlers.poker_room_callback(FakeUpdate(admin, query=query), context)

        room = poker_room_handlers.get_room(context)
        self.assertFalse(room.is_open)
        self.assertTrue(self.config.state_path.exists())

    async def test_admin_reset_clears_room_state(self) -> None:
        context = FakeContext(self.config)
        room = poker_room_handlers.get_room(context)
        room.confirm_room_intent(1, "alice", "Alice", poker_room.ROOM_JOIN)
        room.confirm_room_intent(2, "bob", "Bob", poker_room.ROOM_JOIN)
        room.start_hand()
        context.bot_data["poker_room_render_message_id"] = 123
        admin = FakeUser(1, "alice", "Alice")
        message = FakeMessage("сбросить стол")

        await poker_room_handlers.poker_room_message(FakeUpdate(admin, message=message), context)
        button = context.bot.sent_messages[0]["reply_markup"].inline_keyboard[0][0]
        query = FakeCallbackQuery(button.callback_data, admin)
        await poker_room_handlers.poker_room_callback(FakeUpdate(admin, query=query), context)

        reset_room = poker_room_handlers.get_room(context)
        self.assertEqual(reset_room.seats, {})
        self.assertIsNone(reset_room.current_hand)
        self.assertNotIn("poker_room_render_message_id", context.bot_data)


if __name__ == "__main__":
    unittest.main()
