from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from telegram.error import BadRequest

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
        self.message_id = 100
        self.reply_texts: list[tuple[str, object | None]] = []
        self.reactions: list[tuple[object, bool | None]] = []

    async def reply_text(self, text: str, reply_markup=None, **kwargs) -> None:
        self.reply_texts.append((text, reply_markup))

    async def set_reaction(self, reaction=None, is_big=None, **kwargs) -> bool:
        self.reactions.append((reaction, is_big))
        return True


class FakeCallbackQuery:
    def __init__(self, data: str, user: FakeUser, chat_id: int = 10, thread_id: int = 77) -> None:
        self.data = data
        self.message = type("Msg", (), {"chat_id": chat_id, "message_thread_id": thread_id})()
        self.from_user = user
        self.answers: list[tuple[str, bool]] = []
        self.fail_answer = False

    async def answer(self, text: str = "", show_alert: bool = False, **kwargs) -> None:
        if self.fail_answer:
            raise BadRequest("Query is too old and response timeout expired or query id is invalid")
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
        self.fail_send_photo = False
        self.fail_edit_media = False

    async def send_photo(self, **kwargs):
        if self.fail_send_photo:
            raise RuntimeError("send_photo failed")
        self.sent_photos.append(kwargs)
        return type("Sent", (), {"message_id": 500 + len(self.sent_photos)})()

    async def edit_message_media(self, **kwargs):
        if self.fail_edit_media:
            raise RuntimeError("edit_message_media failed")
        self.edited_media.append(kwargs)

    async def send_message(self, **kwargs):
        self.sent_messages.append(kwargs)
        return type("Sent", (), {"message_id": 700 + len(self.sent_messages)})()


class FakeJob:
    def __init__(self, name: str | None, when: float, data: dict | None = None) -> None:
        self.name = name
        self.when = when
        self.data = data
        self.removed = False

    def schedule_removal(self) -> None:
        self.removed = True


class FakeJobQueue:
    def __init__(self) -> None:
        self.jobs: list[tuple[str, float]] = []
        self.job_kwargs_by_name: dict[str, dict] = {}
        self.job_objects: list[FakeJob] = []

    def get_jobs_by_name(self, name: str):
        return [job for job in self.job_objects if job.name == name and not job.removed]

    def run_once(self, callback, when, name=None, data=None, job_kwargs=None):
        self.jobs.append((name, when))
        job = FakeJob(name, when, data=data)
        self.job_objects.append(job)
        if name is not None:
            self.job_kwargs_by_name[name] = job_kwargs or {}
        return job


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

        self.assertIn("подтверди", message.reply_texts[0][0].lower())

    async def test_join_text_requires_confirmation_then_persists_and_schedules_deal(self) -> None:
        context = FakeContext(self.config)
        user = FakeUser(1, "alice", "Alice")
        message = FakeMessage("сяду")

        await poker_room_handlers.poker_room_message(FakeUpdate(user, message=message), context)

        self.assertIn("подтверди", message.reply_texts[0][0].lower())
        button = message.reply_texts[0][1].inline_keyboard[0][0]
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
        self.assertEqual(message.reply_texts[0][0], "Подтверди.")
        button = message.reply_texts[0][1].inline_keyboard[0][0]
        self.assertEqual(button.callback_data, "pr:confirm:join:1")

    async def test_poker_command_opens_room_for_admin(self) -> None:
        context = FakeContext(self.config)
        room = poker_room_handlers.get_room(context)
        room.is_open = False
        admin = FakeUser(1, "alice", "Alice")
        message = FakeMessage("/poker")

        await poker_room_handlers.poker_room_command(FakeUpdate(admin, message=message), context)

        self.assertTrue(room.is_open)
        self.assertEqual(message.reply_texts[-1][0], "Стол открыт.")
        self.assertTrue(self.config.state_path.exists())

    async def test_auto_deal_job_allows_short_scheduler_misfires(self) -> None:
        context = FakeContext(self.config)
        room = poker_room_handlers.get_room(context)
        room.confirm_room_intent(1, "alice", "Alice", poker_room.ROOM_JOIN)
        room.confirm_room_intent(2, "bob", "Bob", poker_room.ROOM_JOIN)
        admin = FakeUser(1, "alice", "Alice")

        await poker_room_handlers.poker_room_command(FakeUpdate(admin, message=FakeMessage("/poker")), context)

        self.assertGreaterEqual(
            context.job_queue.job_kwargs_by_name["poker-room-auto-deal"]["misfire_grace_time"],
            30,
        )

    async def test_poker_command_rejects_non_admin(self) -> None:
        context = FakeContext(self.config)
        user = FakeUser(2, "bob", "Bob")
        message = FakeMessage("/poker")

        await poker_room_handlers.poker_room_command(FakeUpdate(user, message=message), context)

        self.assertEqual(message.reply_texts[-1][0], "Только админ стола.")

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

        hand = room.current_hand
        self.assertIsNotNone(hand)
        query = FakeCallbackQuery(f"pr:cards:{hand.hand_id}", FakeUser(1, "alice", "Alice"))
        await poker_room_handlers.poker_room_callback(FakeUpdate(query.from_user, query=query), context)

        self.assertEqual(query.answers[-1][0], "Твои карты: A♦ · A♥")
        self.assertTrue(query.answers[-1][1])

    async def test_private_cards_callback_rejects_stale_hand_button(self) -> None:
        context = FakeContext(self.config)
        room = poker_room_handlers.get_room(context)
        room.confirm_room_intent(1, "alice", "Alice", poker_room.ROOM_JOIN)
        room.confirm_room_intent(2, "bob", "Bob", poker_room.ROOM_JOIN)
        first_hand = room.start_hand()
        stale_hand_id = first_hand.hand_id
        first_hand.apply_action(first_hand.to_act_user_id, poker_room.PlayerAction("fold"))
        room.start_hand()

        query = FakeCallbackQuery(f"pr:cards:{stale_hand_id}", FakeUser(1, "alice", "Alice"))
        await poker_room_handlers.poker_room_callback(FakeUpdate(query.from_user, query=query), context)

        self.assertIn("устарела", query.answers[-1][0].lower())
        self.assertTrue(query.answers[-1][1])

    async def test_reveal_callback_rejects_stale_hand_button(self) -> None:
        context = FakeContext(self.config)
        room = poker_room_handlers.get_room(context)
        room.confirm_room_intent(1, "alice", "Alice", poker_room.ROOM_JOIN)
        room.confirm_room_intent(2, "bob", "Bob", poker_room.ROOM_JOIN)
        first_hand = room.start_hand()
        stale_hand_id = first_hand.hand_id
        first_hand.apply_action(first_hand.to_act_user_id, poker_room.PlayerAction("fold"))
        room.start_hand()

        query = FakeCallbackQuery(f"pr:reveal:{stale_hand_id}:1", FakeUser(2, "bob", "Bob"))
        await poker_room_handlers.poker_room_callback(FakeUpdate(query.from_user, query=query), context)

        self.assertIn("устарела", query.answers[-1][0].lower())
        self.assertTrue(query.answers[-1][1])

    async def test_turn_timeout_job_ignores_stale_actor_payload(self) -> None:
        context = FakeContext(self.config)
        room = poker_room_handlers.get_room(context)
        room.confirm_room_intent(1, "alice", "Alice", poker_room.ROOM_JOIN)
        room.confirm_room_intent(2, "bob", "Bob", poker_room.ROOM_JOIN)
        hand = room.start_hand(now=1_000.0)
        first_actor = hand.to_act_user_id
        poker_room_handlers._schedule_turn_timeout(context, hand)
        stale_job = context.job_queue.get_jobs_by_name(poker_room_handlers.TURN_JOB_NAME)[0]

        hand.apply_action(first_actor, poker_room.PlayerAction("call"), now=1_001.0)
        context.job = stale_job
        await poker_room_handlers.poker_room_timeout_job(context)

        self.assertEqual(hand.street, poker_room.STREET_PREFLOP)
        self.assertEqual(hand.to_act_user_id, 2)

    async def test_render_falls_back_to_new_message_when_edit_fails(self) -> None:
        context = FakeContext(self.config)
        room = poker_room_handlers.get_room(context)
        room.confirm_room_intent(1, "alice", "Alice", poker_room.ROOM_JOIN)
        room.confirm_room_intent(2, "bob", "Bob", poker_room.ROOM_JOIN)
        hand = room.start_hand(now=1_000.0)

        rendered = await poker_room_handlers._render_room(context, hand, force_new=True)
        self.assertTrue(rendered)
        original_message_id = context.bot_data["poker_room_render_message_id"]
        context.bot.fail_edit_media = True

        hand.apply_action(hand.to_act_user_id, poker_room.PlayerAction("call"), now=1_001.0)
        rendered = await poker_room_handlers._render_room(context, hand, force_new=False)

        self.assertTrue(rendered)
        self.assertGreaterEqual(len(context.bot.sent_photos), 2)
        self.assertNotEqual(context.bot_data["poker_room_render_message_id"], original_message_id)

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
        self.assertGreaterEqual(
            context.job_queue.job_kwargs_by_name["poker-room-turn"]["misfire_grace_time"],
            30,
        )
        self.assertEqual(message.reactions[-1][0], "👌")

    async def test_current_actor_non_action_gets_polite_mention_prompt(self) -> None:
        context = FakeContext(self.config)
        room = poker_room_handlers.get_room(context)
        room.confirm_room_intent(1, "alice", "Alice", poker_room.ROOM_JOIN)
        room.confirm_room_intent(2, "bob", "Bob", poker_room.ROOM_JOIN)
        room.start_hand()

        message = FakeMessage("думаю")
        await poker_room_handlers.poker_room_message(FakeUpdate(FakeUser(1, "alice", "Alice"), message=message), context)

        self.assertEqual(len(message.reply_texts), 1)
        self.assertIn("@alice", message.reply_texts[0][0])
        self.assertIn("твой ход", message.reply_texts[0][0].lower())

    async def test_ended_hand_sends_final_message_and_schedules_next_deal(self) -> None:
        context = FakeContext(self.config)
        room = poker_room_handlers.get_room(context)
        room.confirm_room_intent(1, "alice", "Alice", poker_room.ROOM_JOIN)
        room.confirm_room_intent(2, "bob", "Bob", poker_room.ROOM_JOIN)
        room.start_hand()

        message = FakeMessage("fold")
        await poker_room_handlers.poker_room_message(FakeUpdate(FakeUser(1, "alice", "Alice"), message=message), context)

        self.assertEqual(room.current_hand.status, poker_room.STATUS_ENDED)
        self.assertEqual(len(context.bot.sent_messages), 1)
        self.assertIn("Раздача окончена", context.bot.sent_messages[0]["text"])
        self.assertIn("Новая раздача", context.bot.sent_messages[0]["text"])
        self.assertIn(("poker-room-auto-deal", poker_room.AUTO_DEAL_SECONDS), context.job_queue.jobs)

    async def test_auto_deal_render_failure_rolls_back_invisible_hand_and_unsettled_stacks(self) -> None:
        context = FakeContext(self.config)
        room = poker_room_handlers.get_room(context)
        room.confirm_room_intent(1, "alice", "Alice", poker_room.ROOM_JOIN)
        room.confirm_room_intent(2, "bob", "Bob", poker_room.ROOM_JOIN)
        context.bot.fail_send_photo = True

        await poker_room_handlers.poker_room_auto_deal_job(context)

        self.assertIsNone(room.current_hand)
        self.assertEqual(room.seats[1].stack, poker_room.STARTING_STACK)
        self.assertEqual(room.seats[2].stack, poker_room.STARTING_STACK)
        self.assertEqual(sum(seat.stack for seat in room.seats.values()), poker_room.STARTING_STACK * 2)

    async def test_actor_action_after_restart_schedules_new_hand_instead_of_silent_ignore(self) -> None:
        context = FakeContext(self.config)
        room = poker_room_handlers.get_room(context)
        room.confirm_room_intent(1, "alice", "Alice", poker_room.ROOM_JOIN)
        room.confirm_room_intent(2, "bob", "Bob", poker_room.ROOM_JOIN)
        self.assertIsNone(room.current_hand)

        message = FakeMessage("call")
        await poker_room_handlers.poker_room_message(FakeUpdate(FakeUser(1, "alice", "Alice"), message=message), context)

        self.assertIn("Новая раздача", message.reply_texts[-1][0])
        self.assertIn(("poker-room-auto-deal", poker_room.AUTO_DEAL_SECONDS), context.job_queue.jobs)

    async def test_admin_close_requires_admin_and_confirmation(self) -> None:
        context = FakeContext(self.config)
        user = FakeUser(2, "bob", "Bob")
        message = FakeMessage("закрыть стол")

        await poker_room_handlers.poker_room_message(FakeUpdate(user, message=message), context)

        self.assertIn("админ", message.reply_texts[0][0].lower())

        admin = FakeUser(1, "alice", "Alice")
        admin_message = FakeMessage("закрыть стол")
        await poker_room_handlers.poker_room_message(FakeUpdate(admin, message=admin_message), context)

        button = admin_message.reply_texts[0][1].inline_keyboard[0][0]
        self.assertEqual(button.callback_data, "pr:admin:close:1")

        query = FakeCallbackQuery(button.callback_data, admin)
        await poker_room_handlers.poker_room_callback(FakeUpdate(admin, query=query), context)

        room = poker_room_handlers.get_room(context)
        self.assertFalse(room.is_open)
        self.assertTrue(self.config.state_path.exists())

    async def test_admin_close_cancels_turn_and_auto_deal_jobs(self) -> None:
        context = FakeContext(self.config)
        room = poker_room_handlers.get_room(context)
        room.confirm_room_intent(1, "alice", "Alice", poker_room.ROOM_JOIN)
        room.confirm_room_intent(2, "bob", "Bob", poker_room.ROOM_JOIN)
        hand = room.start_hand()
        poker_room_handlers._schedule_turn_timeout(context, hand)
        poker_room_handlers._schedule_auto_deal(context)
        admin = FakeUser(1, "alice", "Alice")
        message = FakeMessage("закрыть стол")

        await poker_room_handlers.poker_room_message(FakeUpdate(admin, message=message), context)
        button = message.reply_texts[0][1].inline_keyboard[0][0]
        query = FakeCallbackQuery(button.callback_data, admin)
        await poker_room_handlers.poker_room_callback(FakeUpdate(admin, query=query), context)

        self.assertEqual(context.job_queue.get_jobs_by_name(poker_room_handlers.TURN_JOB_NAME), [])
        self.assertEqual(context.job_queue.get_jobs_by_name(poker_room_handlers.AUTO_DEAL_JOB_NAME), [])
        data = json.loads(self.config.state_path.read_text(encoding="utf-8"))
        self.assertFalse(data["is_open"])
        self.assertEqual(sum(seat["stack"] for seat in data["seats"]), poker_room.STARTING_STACK * 2)

    async def test_stale_admin_callback_answer_does_not_raise(self) -> None:
        context = FakeContext(self.config)
        room = poker_room_handlers.get_room(context)
        admin = FakeUser(1, "alice", "Alice")
        query = FakeCallbackQuery("pr:admin:close:1", admin)
        query.fail_answer = True

        await poker_room_handlers.poker_room_callback(FakeUpdate(admin, query=query), context)

        self.assertFalse(room.is_open)

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
        button = message.reply_texts[0][1].inline_keyboard[0][0]
        query = FakeCallbackQuery(button.callback_data, admin)
        await poker_room_handlers.poker_room_callback(FakeUpdate(admin, query=query), context)

        reset_room = poker_room_handlers.get_room(context)
        self.assertEqual(reset_room.seats, {})
        self.assertIsNone(reset_room.current_hand)
        self.assertNotIn("poker_room_render_message_id", context.bot_data)


if __name__ == "__main__":
    unittest.main()
