from __future__ import annotations

import unittest
from collections import Counter

import liars_bar


DECK = ["A"] * 6 + ["K"] * 6 + ["Q"] * 6 + [liars_bar.JOKER] * 2


class NoShuffleRng:
    def shuffle(self, values) -> None:
        return None

    def choice(self, values):
        return values[0]

    def randint(self, start: int, stop: int) -> int:
        return start


class FalseyNoShuffleRng(NoShuffleRng):
    def __bool__(self) -> bool:
        return False


class FakeUser:
    def __init__(self, user_id: int, name: str) -> None:
        self.id = user_id
        self.full_name = name
        self.first_name = name
        self.username = name.lower()


class FakeChat:
    def __init__(self, chat_id: int) -> None:
        self.id = chat_id


class FakeMessage:
    def __init__(
        self,
        chat_id: int = -100,
        thread_id: int | None = 77,
        message_id: int = 10,
        text: str = "/liars_bar",
        reply_error: Exception | None = None,
    ) -> None:
        self.chat_id = chat_id
        self.message_thread_id = thread_id
        self.message_id = message_id
        self.text = text
        self.replies: list[FakeMessage] = []
        self.html = ""
        self.reply_markup = None
        self.reply_error = reply_error

    async def reply_html(self, text: str, reply_markup=None, **kwargs):
        if self.reply_error is not None:
            raise self.reply_error
        sent = FakeMessage(
            chat_id=self.chat_id,
            thread_id=self.message_thread_id,
            message_id=100 + len(self.replies),
            text="",
        )
        sent.html = text
        sent.reply_markup = reply_markup
        self.replies.append(sent)
        return sent

    async def reply_text(self, text: str, reply_markup=None, **kwargs):
        return await self.reply_html(text, reply_markup=reply_markup, **kwargs)


class FakeCallbackQuery:
    def __init__(
        self,
        data: str,
        user: FakeUser,
        message: FakeMessage,
        answer_error: Exception | None = None,
        edit_error: Exception | None = None,
    ) -> None:
        self.data = data
        self.from_user = user
        self.message = message
        self.answers: list[tuple[str, bool]] = []
        self.edits: list[dict] = []
        self.answer_error = answer_error
        self.edit_error = edit_error

    async def answer(self, text: str = "", show_alert: bool = False, **kwargs) -> None:
        self.answers.append((text, show_alert))
        if self.answer_error is not None:
            raise self.answer_error

    async def edit_message_text(self, **kwargs) -> None:
        self.edits.append(kwargs)
        if self.edit_error is not None:
            raise self.edit_error
        self.message.html = kwargs.get("text", "")
        self.message.reply_markup = kwargs.get("reply_markup")


class FakeUpdate:
    def __init__(
        self,
        user: FakeUser,
        message: FakeMessage | None = None,
        query: FakeCallbackQuery | None = None,
    ) -> None:
        effective_message = message if message is not None else query.message
        self.effective_user = user
        self.effective_message = effective_message
        self.effective_chat = FakeChat(effective_message.chat_id)
        self.callback_query = query


class FakeBot:
    def __init__(self) -> None:
        self.edits: list[dict] = []
        self.edit_error: Exception | None = None

    async def edit_message_text(self, **kwargs) -> None:
        self.edits.append(kwargs)
        if self.edit_error is not None:
            raise self.edit_error


class FakeContext:
    def __init__(self) -> None:
        self.bot = FakeBot()


def player(game, user_id: int):
    return next(item for item in game.players if item.user_id == user_id)


def callback_data(markup, command: str) -> str:
    values = [
        button.callback_data
        for row in markup.inline_keyboard
        for button in row
        if button.callback_data is not None
    ]
    return next(value for value in values if command in value.split(":"))


class LiarsBarGameTests(unittest.TestCase):
    def setUp(self) -> None:
        liars_bar.reset_games_for_tests()

    def create_started_game(
        self,
        *,
        deck: list[str] | None = None,
        target_rank: str = "A",
        bullet_chambers: dict[int, int] | None = None,
    ):
        game = liars_bar.create_game(-100, 77, 1, "Alice", now=1_000.0, rng=NoShuffleRng())
        self.assertEqual(game.join(2, "Bob", now=1_001.0).kind, "joined")
        result = game.start(
            1,
            now=1_002.0,
            deck=list(DECK if deck is None else deck),
            target_rank=target_rank,
            bullet_chambers=bullet_chambers or {1: 6, 2: 6},
        )
        self.assertEqual(result.kind, "started")
        return game

    def test_lobbies_are_scoped_by_chat_and_topic_and_capacity_is_four(self) -> None:
        first = liars_bar.create_game(-100, 10, 1, "Alice", now=1.0)
        second = liars_bar.create_game(-100, 11, 2, "Bob", now=2.0)
        third = liars_bar.create_game(-200, 10, 3, "Cara", now=3.0)

        self.assertIs(liars_bar.active_game(-100, 10, now=4.0), first)
        self.assertIs(liars_bar.active_game(-100, 11, now=4.0), second)
        self.assertIs(liars_bar.active_game(-200, 10, now=4.0), third)
        self.assertIs(liars_bar.get_game(-100, 10, first.game_id), first)
        with self.assertRaises(liars_bar.GameLimitError):
            liars_bar.create_game(-100, 10, 99, "Other", now=4.0)
        self.assertEqual(first.join(2, "Bob", now=5.0).kind, "joined")
        self.assertEqual(first.join(3, "Cara", now=6.0).kind, "joined")
        self.assertEqual(first.join(4, "Dan", now=7.0).kind, "joined")
        self.assertEqual(first.join(5, "Eve", now=8.0).kind, "full")
        self.assertEqual(len(first.players), 4)

    def test_standard_deck_has_six_target_cards_and_two_jokers(self) -> None:
        self.assertEqual(
            Counter(liars_bar.liars_deck()),
            Counter({"A": 6, "K": 6, "Q": 6, liars_bar.JOKER: 2}),
        )

    def test_only_host_can_start_and_two_players_are_required(self) -> None:
        game = liars_bar.create_game(-100, 77, 1, "Alice", now=1_000.0)

        self.assertEqual(game.start(2, now=1_001.0).kind, "not_host")
        self.assertEqual(game.start(1, now=1_002.0).kind, "too_few")
        self.assertEqual(game.status, liars_bar.STATUS_LOBBY)

    def test_invalid_start_input_does_not_partially_start_game(self) -> None:
        game = liars_bar.create_game(-100, 77, 1, "Alice", now=1_000.0, rng=NoShuffleRng())
        game.join(2, "Bob", now=1_001.0)

        with self.assertRaisesRegex(ValueError, "enough cards"):
            game.start(1, now=1_002.0, deck=["A"] * 5, target_rank="A")

        self.assertEqual(game.status, liars_bar.STATUS_LOBBY)
        self.assertEqual(game.round_number, 0)
        self.assertIsNone(game.current_actor_id)
        self.assertTrue(all(item.fatal_trigger is None for item in game.players))
        with self.assertRaisesRegex(ValueError, "fatal trigger"):
            game.start(
                1,
                now=1_003.0,
                deck=list(DECK),
                target_rank="A",
                bullet_chambers={1: 6, 2: 0},
            )
        self.assertEqual(game.status, liars_bar.STATUS_LOBBY)
        self.assertTrue(all(item.fatal_trigger is None for item in game.players))
        self.assertEqual(
            game.start(1, now=1_004.0, deck=list(DECK), target_rank="A").kind,
            "started",
        )

    def test_falsey_injected_rng_is_preserved(self) -> None:
        rng = FalseyNoShuffleRng()

        game = liars_bar.create_game(-100, 77, 1, "Alice", now=1_000.0, rng=rng)

        self.assertIs(game.rng, rng)

    def test_injected_deck_is_dealt_deterministically(self) -> None:
        deck = ["A", "K", "Q", liars_bar.JOKER] * 5
        game = self.create_started_game(deck=deck)

        self.assertEqual(player(game, 1).hand, ["A", "Q", "A", "Q", "A"])
        self.assertEqual(player(game, 2).hand, ["K", liars_bar.JOKER, "K", liars_bar.JOKER, "K"])
        self.assertEqual(game.target_rank, "A")
        self.assertEqual(game.current_actor_id, 1)

    def test_selection_and_play_enforce_turn_and_three_card_limit(self) -> None:
        game = self.create_started_game(deck=["A", "K", "Q", liars_bar.JOKER] * 5)
        alice_before = list(player(game, 1).hand)

        self.assertEqual(game.select(2, 1, now=1_003.0).kind, "not_turn")
        self.assertEqual(game.select(1, 1, now=1_004.0).kind, "selected")
        self.assertEqual(game.select(1, 2, now=1_005.0).kind, "selected")
        self.assertEqual(game.select(1, 3, now=1_006.0).kind, "selected")
        self.assertEqual(game.select(1, 4, now=1_007.0).kind, "too_many")

        result = game.play(1, now=1_008.0)

        self.assertEqual(result.kind, "played")
        self.assertEqual(len(player(game, 1).hand), len(alice_before) - 3)
        self.assertEqual(game.current_actor_id, 2)
        self.assertEqual(game.play(1, now=1_009.0).kind, "not_turn")

    def test_truthful_play_penalizes_challenger_with_a_safe_pull(self) -> None:
        game = self.create_started_game(
            deck=[liars_bar.JOKER, "K", "A", "K", "A", "K", "A", "K", "A", "K"]
            + ["Q"] * 10,
            bullet_chambers={1: 6, 2: 2},
        )
        game.select(1, 1, now=1_003.0)
        game.select(1, 2, now=1_003.5)
        game.play(1, now=1_004.0)
        self.assertEqual(game.last_play.cards, (liars_bar.JOKER, "A"))

        result = game.challenge(2, now=1_005.0)

        self.assertEqual(game.round_number, 2)
        self.assertEqual(result.kind, "safe")
        self.assertIs(result.truthful, True)
        self.assertEqual(result.penalized_user_id, 2)
        self.assertFalse(result.eliminated)
        self.assertTrue(player(game, 2).alive)
        self.assertEqual(player(game, 2).trigger_pulls, 1)
        self.assertEqual(game.status, liars_bar.STATUS_PLAYING)

    def test_invalid_next_round_input_does_not_repeat_challenge_penalty(self) -> None:
        game = liars_bar.create_game(-100, 77, 1, "Alice", now=1_000.0, rng=NoShuffleRng())
        game.join(2, "Bob", now=1_001.0)
        game.join(3, "Cara", now=1_002.0)
        game.start(
            1,
            now=1_003.0,
            deck=["K"] * 15 + ["A"] * 5,
            target_rank="A",
            bullet_chambers={1: 6, 2: 6, 3: 6},
        )
        game.select(1, 1, now=1_004.0)
        game.play(1, now=1_005.0)
        last_play = game.last_play

        with self.assertRaisesRegex(ValueError, "enough cards"):
            game.challenge(2, now=1_006.0, deck=["A"] * 5, target_rank="A")

        self.assertEqual(player(game, 1).trigger_pulls, 0)
        self.assertTrue(player(game, 1).alive)
        self.assertIs(game.last_play, last_play)
        self.assertEqual(game.current_actor_id, 2)
        result = game.challenge(2, now=1_007.0)
        self.assertEqual(result.penalized_user_id, 1)
        self.assertEqual(player(game, 1).trigger_pulls, 1)

    def test_fatal_pull_eliminates_player_and_next_round_skips_them(self) -> None:
        game = liars_bar.create_game(-100, 77, 1, "Alice", now=1_000.0, rng=NoShuffleRng())
        game.join(2, "Bob", now=1_001.0)
        game.join(3, "Cara", now=1_002.0)
        game.start(
            1,
            now=1_003.0,
            deck=["K"] * 15 + ["A"] * 5,
            target_rank="A",
            bullet_chambers={1: 1, 2: 6, 3: 6},
        )
        game.select(1, 1, now=1_004.0)
        game.play(1, now=1_005.0)

        result = game.challenge(2, now=1_006.0)

        self.assertEqual(result.kind, "eliminated")
        self.assertFalse(player(game, 1).alive)
        self.assertEqual(player(game, 1).hand, [])
        self.assertEqual(game.round_number, 2)
        self.assertEqual(game.current_actor_id, 2)
        self.assertEqual(game.status, liars_bar.STATUS_PLAYING)

    def test_lie_penalizes_liar_and_fatal_pull_declares_winner(self) -> None:
        game = self.create_started_game(
            deck=["K"] * 10 + ["A"] * 10,
            target_rank="A",
            bullet_chambers={1: 1, 2: 6},
        )
        game.select(1, 1, now=1_003.0)
        game.play(1, now=1_004.0)

        result = game.challenge(2, now=1_005.0)

        self.assertEqual(result.kind, "ended")
        self.assertIs(result.truthful, False)
        self.assertEqual(result.penalized_user_id, 1)
        self.assertTrue(result.eliminated)
        self.assertFalse(player(game, 1).alive)
        self.assertEqual(game.winner_user_id, 2)
        self.assertEqual(game.status, liars_bar.STATUS_ENDED)

    def test_empty_hand_forces_the_next_player_to_challenge(self) -> None:
        game = self.create_started_game(deck=["A"] * 20)
        for index in (1, 2, 3):
            game.select(1, index, now=1_003.0 + index)
        game.play(1, now=1_006.0)
        game.select(2, 1, now=1_007.0)
        game.play(2, now=1_008.0)
        for index in (1, 2):
            game.select(1, index, now=1_009.0 + index)
        game.play(1, now=1_011.0)

        self.assertEqual(player(game, 1).hand, [])
        self.assertTrue(game.forced_challenge)
        self.assertEqual(game.select(2, 1, now=1_012.0).kind, "must_challenge")
        self.assertEqual(game.play(2, now=1_013.0).kind, "must_challenge")
        self.assertNotEqual(game.challenge(2, now=1_014.0).kind, "no_play")

    def test_public_render_hides_hands_and_private_text_is_per_player(self) -> None:
        game = self.create_started_game(deck=["A", "K", "Q", liars_bar.JOKER] * 5)

        public = game.render_html()
        alice_private = game.private_hand_text(1)
        bob_private = game.private_hand_text(2)

        self.assertNotEqual(alice_private, bob_private)
        self.assertNotIn("Q", public)
        self.assertNotIn("K", public)
        self.assertIn("A", alice_private)
        self.assertIn("K", bob_private)
        self.assertNotIn("K", alice_private)
        self.assertNotIn("Q", bob_private)

    def test_all_callback_data_is_namespaced_and_within_telegram_limit(self) -> None:
        game = liars_bar.create_game(-100, 77, 1, "Alice", now=1_000.0)
        markups = [game.reply_markup()]
        game.join(2, "Bob", now=1_001.0)
        game.start(1, now=1_002.0, deck=list(DECK), target_rank="A", bullet_chambers={1: 6, 2: 6})
        markups.append(game.reply_markup())

        data = [
            button.callback_data
            for markup in markups
            for row in markup.inline_keyboard
            for button in row
            if button.callback_data is not None
        ]
        self.assertTrue(data)
        self.assertTrue(all(value.startswith("lb:") for value in data))
        self.assertTrue(all(len(value.encode("utf-8")) <= 64 for value in data))

    def test_cleanup_expires_inactive_game_in_its_scope_only(self) -> None:
        expired = liars_bar.create_game(-100, 77, 1, "Alice", now=1_000.0)
        active = liars_bar.create_game(-100, 78, 2, "Bob", now=1_000.0)
        active.updated_at = 1_200.0

        removed = liars_bar.cleanup_expired(-100, 77, now=2_200.0)

        self.assertIn(expired, removed)
        self.assertIsNone(liars_bar.active_game(-100, 77, now=2_200.0))
        self.assertIs(liars_bar.active_game(-100, 78, now=1_201.0), active)


class LiarsBarHandlerTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        liars_bar.reset_games_for_tests()
        self.alice = FakeUser(1, "Alice")
        self.bob = FakeUser(2, "Bob")
        self.context = FakeContext()

    async def command(self) -> tuple[FakeMessage, object]:
        message = FakeMessage()
        await liars_bar.liars_bar_command(FakeUpdate(self.alice, message=message), self.context)
        game = liars_bar.active_game(message.chat_id, message.message_thread_id)
        self.assertIsNotNone(game)
        return message, game

    async def press(
        self,
        game,
        user: FakeUser,
        data: str,
        *,
        message_id: int | None = None,
        answer_error: Exception | None = None,
        edit_error: Exception | None = None,
    ):
        message = FakeMessage(
            chat_id=game.chat_id,
            thread_id=game.thread_id,
            message_id=game.message_id if message_id is None else message_id,
        )
        query = FakeCallbackQuery(
            data,
            user,
            message,
            answer_error=answer_error,
            edit_error=edit_error,
        )
        await liars_bar.liars_bar_callback(FakeUpdate(user, query=query), self.context)
        return query

    async def test_command_creates_topic_lobby_and_records_public_message(self) -> None:
        message, game = await self.command()

        self.assertEqual(game.host_user_id, self.alice.id)
        self.assertEqual(game.thread_id, message.message_thread_id)
        self.assertEqual(len(message.replies), 1)
        self.assertEqual(game.message_id, message.replies[0].message_id)
        self.assertIsNotNone(message.replies[0].reply_markup)

    async def test_command_send_failure_removes_invisible_lobby(self) -> None:
        message = FakeMessage(reply_error=RuntimeError("send failed"))

        with self.assertRaisesRegex(RuntimeError, "send failed"):
            await liars_bar.liars_bar_command(FakeUpdate(self.alice, message=message), self.context)

        self.assertIsNone(liars_bar.active_game(message.chat_id, message.message_thread_id))

    async def test_callback_flow_join_start_cards_select_play_and_challenge(self) -> None:
        _, game = await self.command()
        join = callback_data(game.reply_markup(), "join")
        joined = await self.press(game, self.bob, join)
        self.assertEqual(len(game.players), 2)
        self.assertTrue(joined.edits)

        start = callback_data(game.reply_markup(), "start")
        started = await self.press(game, self.alice, start)
        self.assertEqual(game.status, liars_bar.STATUS_PLAYING)
        self.assertTrue(started.edits)

        cards = callback_data(game.reply_markup(), "cards")
        private = await self.press(game, self.alice, cards)
        self.assertTrue(private.answers[-1][1])
        self.assertEqual(private.answers[-1][0], game.private_hand_text(self.alice.id))
        self.assertFalse(private.edits)

        users = {self.alice.id: self.alice, self.bob.id: self.bob}
        actor = users[game.current_actor_id]
        pick = callback_data(game.reply_markup(), "pick")
        selected = await self.press(game, actor, pick)
        self.assertTrue(selected.answers)
        self.assertFalse(selected.edits)
        play = callback_data(game.reply_markup(), "play")
        played = await self.press(game, actor, play)
        self.assertTrue(played.edits)
        challenger = users[game.current_actor_id]
        liar = callback_data(game.reply_markup(), "liar")
        challenged = await self.press(game, challenger, liar)
        self.assertTrue(challenged.answers)
        self.assertTrue(challenged.edits)

    async def test_malformed_stale_and_wrong_message_callbacks_are_harmless(self) -> None:
        _, game = await self.command()
        join = callback_data(game.reply_markup(), "join")

        malformed = await self.press(game, self.bob, "lb:not-a-valid-callback")
        self.assertTrue(malformed.answers)
        self.assertEqual(len(game.players), 1)

        wrong_message = await self.press(game, self.bob, join, message_id=game.message_id + 1)
        self.assertTrue(wrong_message.answers)
        self.assertEqual(len(game.players), 1)

        stale = await self.press(game, self.bob, "lb:999:join")
        self.assertTrue(stale.answers)
        self.assertEqual(len(game.players), 1)

    async def test_failed_start_edit_can_be_retried_from_stale_lobby_controls(self) -> None:
        _, game = await self.command()
        await self.press(game, self.bob, callback_data(game.reply_markup(), "join"))
        start = callback_data(game.reply_markup(), "start")

        with self.assertRaisesRegex(RuntimeError, "edit failed"):
            await self.press(game, self.alice, start, edit_error=RuntimeError("edit failed"))

        self.assertEqual(game.status, liars_bar.STATUS_PLAYING)
        retried = await self.press(game, self.alice, start)
        self.assertTrue(retried.edits)
        self.assertIn("Карта стола", retried.edits[-1]["text"])

    async def test_answer_failure_still_updates_authoritative_public_message(self) -> None:
        _, game = await self.command()
        join = callback_data(game.reply_markup(), "join")
        message = FakeMessage(
            chat_id=game.chat_id,
            thread_id=game.thread_id,
            message_id=game.message_id,
        )
        query = FakeCallbackQuery(
            join,
            self.bob,
            message,
            answer_error=RuntimeError("answer failed"),
        )

        with self.assertRaisesRegex(RuntimeError, "answer failed"):
            await liars_bar.liars_bar_callback(FakeUpdate(self.bob, query=query), self.context)

        self.assertEqual(len(game.players), 2)
        self.assertTrue(query.edits)

    async def test_inaccessible_callback_message_is_acknowledged(self) -> None:
        _, game = await self.command()
        inaccessible = type(
            "Inaccessible",
            (),
            {
                "is_accessible": False,
                "message_id": game.message_id,
                "chat": FakeChat(game.chat_id),
            },
        )()
        query = FakeCallbackQuery(f"lb:{game.game_id}:join", self.bob, inaccessible)
        update = type(
            "Update",
            (),
            {"callback_query": query, "effective_user": self.bob},
        )()

        await liars_bar.liars_bar_callback(update, self.context)

        self.assertTrue(query.answers)
        self.assertEqual(game.status, liars_bar.STATUS_CANCELLED)
        self.assertIsNone(liars_bar.active_game(game.chat_id, game.thread_id))

    async def test_expired_callback_closes_public_message_without_mutating_game(self) -> None:
        _, game = await self.command()
        join = callback_data(game.reply_markup(), "join")
        game.updated_at = 0.0

        query = await self.press(game, self.bob, join)

        self.assertEqual(game.status, liars_bar.STATUS_EXPIRED)
        self.assertEqual(len(game.players), 1)
        self.assertTrue(self.context.bot.edits)
        self.assertTrue(query.answers)

    async def test_failed_expiry_edit_is_retried(self) -> None:
        _, game = await self.command()
        game.updated_at = 0.0
        self.context.bot.edit_error = RuntimeError("temporary edit failure")

        await liars_bar.cleanup_expired_and_edit(game.chat_id, game.thread_id, self.context)
        self.assertTrue(game.cleanup_pending)

        self.context.bot.edit_error = None
        await liars_bar.cleanup_expired_and_edit(game.chat_id, game.thread_id, self.context)

        self.assertFalse(game.cleanup_pending)
        self.assertGreaterEqual(len(self.context.bot.edits), 2)


if __name__ == "__main__":
    unittest.main()
