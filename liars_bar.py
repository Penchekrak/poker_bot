"""Stateful multiplayer Liar's Bar mini-game for Telegram groups."""

from __future__ import annotations

import asyncio
import html
import itertools
import random
import time
from dataclasses import dataclass, field
from typing import Final

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ParseMode
from telegram.error import BadRequest
from telegram.ext import ContextTypes

MIN_PLAYERS: Final[int] = 2
MAX_PLAYERS: Final[int] = 4
HAND_SIZE: Final[int] = 5
TABLE_TIMEOUT_SECONDS: Final[float] = 20 * 60

STATUS_LOBBY: Final[str] = "lobby"
STATUS_PLAYING: Final[str] = "playing"
STATUS_ENDED: Final[str] = "ended"
STATUS_CANCELLED: Final[str] = "cancelled"
STATUS_EXPIRED: Final[str] = "expired"

TARGET_RANKS: Final[tuple[str, ...]] = ("A", "K", "Q")
JOKER: Final[str] = "*"
_VALID_CARDS: Final[set[str]] = {*TARGET_RANKS, JOKER}
_ACTIVE_STATUSES: Final[set[str]] = {STATUS_LOBBY, STATUS_PLAYING}

_Scope = tuple[int, int | None]
_GAMES: dict[_Scope, list["LiarsBarGame"]] = {}
_GAME_IDS = itertools.count(1)


class LiarsBarError(Exception):
    """Base error for Liar's Bar game creation."""


class GameLimitError(LiarsBarError):
    """Raised when a group topic already has an active game."""


@dataclass
class GameResult:
    kind: str
    text: str
    changed: bool = False
    penalized_user_id: int | None = None
    truthful: bool | None = None
    eliminated: bool = False


@dataclass
class PlayerState:
    user_id: int
    name: str
    hand: list[str] = field(default_factory=list)
    alive: bool = True
    fatal_trigger: int | None = None
    trigger_pulls: int = 0


@dataclass(frozen=True)
class LastPlay:
    player_id: int
    cards: tuple[str, ...]

    @property
    def count(self) -> int:
        return len(self.cards)


@dataclass
class LiarsBarGame:
    chat_id: int
    thread_id: int | None
    game_id: int
    host_user_id: int
    players: list[PlayerState]
    created_at: float
    updated_at: float
    rng: random.Random = field(repr=False)
    message_id: int | None = None
    status: str = STATUS_LOBBY
    round_number: int = 0
    target_rank: str | None = None
    current_actor_id: int | None = None
    last_play: LastPlay | None = None
    pile: list[LastPlay] = field(default_factory=list)
    pending_selections: dict[int, set[int]] = field(default_factory=dict)
    forced_challenge: bool = False
    winner_user_id: int | None = None
    last_event: str = ""
    cleanup_pending: bool = False
    lock: asyncio.Lock = field(default_factory=asyncio.Lock, repr=False, compare=False)

    def player(self, user_id: int) -> PlayerState | None:
        return next((player for player in self.players if player.user_id == user_id), None)

    def join(self, user_id: int, name: str, now: float | None = None) -> GameResult:
        if self.status != STATUS_LOBBY:
            return GameResult("not_lobby", "Игра уже началась.")
        existing = self.player(user_id)
        if existing is not None:
            existing.name = name
            return GameResult("already_joined", "Ты уже за столом.")
        if len(self.players) >= MAX_PLAYERS:
            return GameResult("full", "Все четыре места уже заняты.")

        self.players.append(PlayerState(user_id=user_id, name=name))
        self.updated_at = _coerce_now(now)
        return GameResult("joined", "Место занято.", changed=True)

    def leave(self, user_id: int, now: float | None = None) -> GameResult:
        if self.status != STATUS_LOBBY:
            return GameResult("not_lobby", "После старта выйти из лобби нельзя.")
        player = self.player(user_id)
        if player is None:
            return GameResult("not_in_game", "Тебя нет за этим столом.")

        self.players.remove(player)
        self.updated_at = _coerce_now(now)
        if not self.players:
            self.status = STATUS_CANCELLED
            self.last_event = "Лобби закрыто: все ушли."
            return GameResult("cancelled", "Лобби закрыто.", changed=True)
        if self.host_user_id == user_id:
            self.host_user_id = self.players[0].user_id
        return GameResult("left", "Ты вышел из лобби.", changed=True)

    def start(
        self,
        user_id: int,
        now: float | None = None,
        deck: list[str] | None = None,
        target_rank: str | None = None,
        bullet_chambers: dict[int, int] | None = None,
    ) -> GameResult:
        if self.status != STATUS_LOBBY:
            return GameResult("already_started", "Игра уже началась.")
        if user_id != self.host_user_id:
            return GameResult("not_host", "Начать игру может только ведущий.")
        if len(self.players) < MIN_PLAYERS:
            return GameResult("too_few", "Нужно хотя бы два игрока.")

        _validate_round_spec(deck, target_rank, len(self.players))
        supplied = bullet_chambers or {}
        if any(chamber not in range(1, 7) for chamber in supplied.values()):
            raise ValueError("fatal trigger must be between 1 and 6")
        chambers: dict[int, int] = {}
        for player in self.players:
            chamber = supplied[player.user_id] if player.user_id in supplied else self.rng.randint(1, 6)
            chambers[player.user_id] = chamber

        for player in self.players:
            player.alive = True
            player.trigger_pulls = 0
            player.fatal_trigger = chambers[player.user_id]

        self.status = STATUS_PLAYING
        starter = self.rng.choice([player.user_id for player in self.players])
        self._start_round(starter, _coerce_now(now), deck=deck, target_rank=target_rank)
        self.last_event = "Карты розданы. Первый раунд начался."
        return GameResult("started", "Игра началась.", changed=True)

    def select(self, user_id: int, card_number: int, now: float | None = None) -> GameResult:
        del now  # Private selection does not extend the public game's timeout.
        error = self._turn_error(user_id)
        if error is not None:
            return error
        if self.forced_challenge:
            return GameResult("must_challenge", "После последней карты можно только проверить ложь.")
        player = self.player(user_id)
        assert player is not None
        if card_number < 1 or card_number > len(player.hand):
            return GameResult("bad_card", "Такой карты в руке нет.")

        selected = self.pending_selections.setdefault(user_id, set())
        if card_number in selected:
            selected.remove(card_number)
            return GameResult("selected", self._selection_text(user_id))
        if len(selected) >= 3:
            return GameResult("too_many", "Можно положить не больше трех карт.")
        selected.add(card_number)
        return GameResult("selected", self._selection_text(user_id))

    def play(self, user_id: int, now: float | None = None) -> GameResult:
        error = self._turn_error(user_id)
        if error is not None:
            return error
        if self.forced_challenge:
            return GameResult("must_challenge", "Сейчас нужно нажать «Лжец!».")
        player = self.player(user_id)
        assert player is not None
        selected = sorted(self.pending_selections.get(user_id, set()))
        if not selected:
            return GameResult("no_cards", "Сначала выбери от одной до трех карт.")
        if len(selected) > 3 or any(index < 1 or index > len(player.hand) for index in selected):
            self.pending_selections.pop(user_id, None)
            return GameResult("stale_selection", "Рука изменилась. Выбери карты заново.")

        cards = tuple(player.hand[index - 1] for index in selected)
        for index in reversed(selected):
            del player.hand[index - 1]
        play = LastPlay(player_id=user_id, cards=cards)
        self.last_play = play
        self.pile.append(play)
        self.pending_selections.clear()
        self.forced_challenge = not player.hand
        self.current_actor_id = self._next_alive_after(user_id)
        self.updated_at = _coerce_now(now)
        self.last_event = f"{player.name} объявляет {play.count} × {_rank_label(self.target_rank)}."
        return GameResult("played", f"Сыграно карт: {play.count}.", changed=True)

    def challenge(
        self,
        user_id: int,
        now: float | None = None,
        deck: list[str] | None = None,
        target_rank: str | None = None,
    ) -> GameResult:
        error = self._turn_error(user_id)
        if error is not None:
            return error
        if self.last_play is None or self.target_rank is None:
            return GameResult("nothing_to_challenge", "Проверять пока нечего.")

        now_value = _coerce_now(now)
        challenged = self.player(self.last_play.player_id)
        challenger = self.player(user_id)
        assert challenged is not None and challenger is not None
        truthful = all(card in {self.target_rank, JOKER} for card in self.last_play.cards)
        penalized = challenger if truthful else challenged
        next_trigger_pull = penalized.trigger_pulls + 1
        fired = next_trigger_pull == penalized.fatal_trigger
        survivor_count = sum(player.alive for player in self.players) - int(fired)
        if survivor_count > 1:
            _validate_round_spec(deck, target_rank, survivor_count)

        penalized.trigger_pulls = next_trigger_pull
        if fired:
            penalized.alive = False
            penalized.hand.clear()

        verdict = "правда" if truthful else "ложь"
        trigger = "выстрел — игрок выбывает" if fired else "холостой щелчок"
        event = (
            f"Вскрытие: {challenged.name} положил {format_cards(self.last_play.cards)} — "
            f"{verdict}. {penalized.name}: {trigger}."
        )
        survivors = [player for player in self.players if player.alive]
        self.pending_selections.clear()
        self.updated_at = now_value
        if len(survivors) == 1:
            self.status = STATUS_ENDED
            self.winner_user_id = survivors[0].user_id
            self.current_actor_id = None
            self.last_event = f"{event} Победитель: {survivors[0].name}."
            return GameResult(
                "ended",
                "Игра окончена.",
                changed=True,
                penalized_user_id=penalized.user_id,
                truthful=truthful,
                eliminated=fired,
            )

        starter = penalized.user_id if penalized.alive else self._next_alive_after(penalized.user_id)
        assert starter is not None
        self._start_round(starter, now_value, deck=deck, target_rank=target_rank)
        self.last_event = event
        return GameResult(
            "eliminated" if fired else "safe",
            "Новый раунд.",
            changed=True,
            penalized_user_id=penalized.user_id,
            truthful=truthful,
            eliminated=fired,
        )

    def cancel(self, user_id: int, now: float | None = None) -> GameResult:
        if self.status not in _ACTIVE_STATUSES:
            return GameResult("closed", "Этот стол уже закрыт.")
        if user_id != self.host_user_id:
            return GameResult("not_host", "Закрыть стол может только ведущий.")
        self.status = STATUS_CANCELLED
        self.current_actor_id = None
        self.updated_at = _coerce_now(now)
        self.last_event = "Ведущий закрыл стол."
        return GameResult("cancelled", "Стол закрыт.", changed=True)

    def expire(self, now: float | None = None) -> None:
        self.status = STATUS_EXPIRED
        self.current_actor_id = None
        self.updated_at = _coerce_now(now)
        self.last_event = "Стол закрыт по таймауту."
        self.cleanup_pending = True

    def private_hand_text(self, user_id: int) -> str:
        player = self.player(user_id)
        if player is None:
            return "Тебя нет за этим столом."
        if self.status != STATUS_PLAYING:
            return "Карты появятся после старта игры."
        if not player.alive:
            return "Ты уже выбыл из игры."
        selected = self.pending_selections.get(user_id, set())
        cards = [
            f"{'✓' if index in selected else '·'} {index}: {_card_label(card)}"
            for index, card in enumerate(player.hand, start=1)
        ]
        suffix = "\nВыбрано: " + (", ".join(map(str, sorted(selected))) or "ничего")
        return "Твои карты:\n" + ("\n".join(cards) or "рука пуста") + suffix

    def render_html(self) -> str:
        lines = [f"<b>🃏 Liar's Bar · стол #{self.game_id}</b>"]
        if self.status == STATUS_LOBBY:
            lines.extend(
                [
                    f"Игроки: <b>{len(self.players)}/{MAX_PLAYERS}</b>",
                    "",
                    *self._player_lines(lobby=True),
                    "",
                    "Нужно 2–4 игрока. Ведущий запускает раздачу.",
                ]
            )
            return "\n".join(lines)

        if self.status == STATUS_PLAYING:
            actor = self.player(self.current_actor_id) if self.current_actor_id is not None else None
            lines.extend(
                [
                    f"Раунд: <b>{self.round_number}</b>",
                    f"Карта стола: <b>{_rank_label(self.target_rank)}</b>",
                    f"Ход: <b>{html.escape(actor.name)}</b>" if actor else "Ход: —",
                    "",
                    *self._player_lines(lobby=False),
                ]
            )
            if self.last_play is not None:
                author = self.player(self.last_play.player_id)
                if author is not None:
                    lines.extend(
                        [
                            "",
                            f"Последняя заявка: <b>{html.escape(author.name)}</b> — "
                            f"<b>{self.last_play.count} × {_rank_label(self.target_rank)}</b>",
                        ]
                    )
            if self.forced_challenge:
                lines.append("Последние карты сыграны: следующий ход — только «Лжец!».")
            if self.last_event:
                lines.extend(["", html.escape(self.last_event)])
            return "\n".join(lines)

        if self.last_event:
            lines.extend(["", html.escape(self.last_event)])
        return "\n".join(lines)

    def reply_markup(self) -> InlineKeyboardMarkup | None:
        def callback(command: str) -> str:
            return f"lb:{self.game_id}:{command}"

        if self.status == STATUS_LOBBY:
            return InlineKeyboardMarkup(
                [
                    [
                        InlineKeyboardButton("Сесть", callback_data=callback("join")),
                        InlineKeyboardButton("Выйти", callback_data=callback("leave")),
                    ],
                    [
                        InlineKeyboardButton("Начать", callback_data=callback("start")),
                        InlineKeyboardButton("Закрыть", callback_data=callback("cancel")),
                    ],
                    [InlineKeyboardButton("Правила", callback_data=callback("rules"))],
                ]
            )
        if self.status != STATUS_PLAYING:
            return None

        rows: list[list[InlineKeyboardButton]] = [
            [InlineKeyboardButton("Мои карты", callback_data=callback("cards"))]
        ]
        actor = self.player(self.current_actor_id) if self.current_actor_id is not None else None
        if actor is not None and not self.forced_challenge and actor.hand:
            selectors = [
                InlineKeyboardButton(str(index), callback_data=callback(f"pick:{index}"))
                for index in range(1, len(actor.hand) + 1)
            ]
            rows.extend(_chunk_buttons(selectors, 5))
            rows.append([InlineKeyboardButton("Сыграть", callback_data=callback("play"))])
        if self.last_play is not None:
            rows.append([InlineKeyboardButton("Лжец!", callback_data=callback("liar"))])
        rows.append(
            [
                InlineKeyboardButton("Правила", callback_data=callback("rules")),
                InlineKeyboardButton("Закрыть", callback_data=callback("cancel")),
            ]
        )
        return InlineKeyboardMarkup(rows)

    def _start_round(
        self,
        starter_user_id: int,
        now: float,
        deck: list[str] | None = None,
        target_rank: str | None = None,
    ) -> None:
        living = [player for player in self.players if player.alive]
        _validate_round_spec(deck, target_rank, len(living))
        draw_deck = list(deck) if deck is not None else liars_deck()
        if deck is None:
            self.rng.shuffle(draw_deck)

        for player in self.players:
            player.hand.clear()
        for _ in range(HAND_SIZE):
            for player in living:
                player.hand.append(draw_deck.pop(0))

        self.round_number += 1
        self.target_rank = target_rank or self.rng.choice(TARGET_RANKS)
        self.current_actor_id = starter_user_id
        self.last_play = None
        self.pile.clear()
        self.pending_selections.clear()
        self.forced_challenge = False
        self.updated_at = now

    def _next_alive_after(self, user_id: int) -> int | None:
        if not self.players:
            return None
        start = next((index for index, player in enumerate(self.players) if player.user_id == user_id), -1)
        for offset in range(1, len(self.players) + 1):
            player = self.players[(start + offset) % len(self.players)]
            if player.alive:
                return player.user_id
        return None

    def _turn_error(self, user_id: int) -> GameResult | None:
        if self.status != STATUS_PLAYING:
            return GameResult("not_playing", "Игра сейчас не идет.")
        player = self.player(user_id)
        if player is None or not player.alive:
            return GameResult("not_in_game", "Ты не участвуешь в этой раздаче.")
        if self.current_actor_id != user_id:
            return GameResult("not_turn", "Сейчас ход другого игрока.")
        return None

    def _selection_text(self, user_id: int) -> str:
        selected = self.pending_selections.get(user_id, set())
        if not selected:
            return "Выбор снят."
        return "Выбрано: " + ", ".join(map(str, sorted(selected)))

    def _player_lines(self, lobby: bool) -> list[str]:
        lines = []
        for index, player in enumerate(self.players, start=1):
            label = html.escape(player.name)
            host = " · ведущий" if player.user_id == self.host_user_id else ""
            if lobby:
                lines.append(f"{index}. <b>{label}</b>{host}")
            elif player.alive:
                lines.append(
                    f"• <b>{label}</b> — {len(player.hand)} карт, курок {player.trigger_pulls}/6"
                )
            else:
                lines.append(f"• <s>{label}</s> — выбыл")
        return lines


def liars_deck() -> list[str]:
    return ["A"] * 6 + ["K"] * 6 + ["Q"] * 6 + [JOKER] * 2


def _validate_round_spec(
    deck: list[str] | None,
    target_rank: str | None,
    player_count: int,
) -> None:
    if target_rank is not None and target_rank not in TARGET_RANKS:
        raise ValueError("target rank must be A, K, or Q")
    if deck is None:
        return
    if any(card not in _VALID_CARDS for card in deck):
        raise ValueError("deck contains an invalid Liar's Bar card")
    if len(deck) < HAND_SIZE * player_count:
        raise ValueError("deck does not contain enough cards")


def create_game(
    chat_id: int,
    thread_id: int | None,
    host_id: int,
    host_name: str,
    now: float | None = None,
    rng: random.Random | None = None,
) -> LiarsBarGame:
    now_value = _coerce_now(now)
    cleanup_expired(chat_id, thread_id, now_value)
    if active_game(chat_id, thread_id) is not None:
        raise GameLimitError("В этой группе или теме уже есть стол Liar's Bar.")
    game = LiarsBarGame(
        chat_id=chat_id,
        thread_id=thread_id,
        game_id=next(_GAME_IDS),
        host_user_id=host_id,
        players=[PlayerState(user_id=host_id, name=host_name)],
        created_at=now_value,
        updated_at=now_value,
        rng=rng if rng is not None else random.Random(),
    )
    _GAMES.setdefault((chat_id, thread_id), []).append(game)
    return game


def _remove_game(game: LiarsBarGame) -> None:
    scope = (game.chat_id, game.thread_id)
    games = _GAMES.get(scope, [])
    if game in games:
        games.remove(game)
    if not games:
        _GAMES.pop(scope, None)


def active_game(
    chat_id: int,
    thread_id: int | None = None,
    now: float | None = None,
) -> LiarsBarGame | None:
    if now is not None:
        cleanup_expired(chat_id, thread_id, now)
    return next(
        (game for game in _GAMES.get((chat_id, thread_id), []) if game.status in _ACTIVE_STATUSES),
        None,
    )


def get_game(chat_id: int, thread_id: int | None, game_id: int) -> LiarsBarGame | None:
    return next(
        (game for game in _GAMES.get((chat_id, thread_id), []) if game.game_id == game_id),
        None,
    )


def _find_game_in_chat(chat_id: int, game_id: int) -> LiarsBarGame | None:
    for (scope_chat_id, _thread_id), games in _GAMES.items():
        if scope_chat_id != chat_id:
            continue
        for game in games:
            if game.game_id == game_id:
                return game
    return None


def cleanup_expired(
    chat_id: int,
    thread_id: int | None = None,
    now: float | None = None,
) -> list[LiarsBarGame]:
    now_value = _coerce_now(now)
    scope = (chat_id, thread_id)
    kept: list[LiarsBarGame] = []
    expired: list[LiarsBarGame] = []
    for game in _GAMES.get(scope, []):
        if game.status in _ACTIVE_STATUSES and now_value - game.updated_at >= TABLE_TIMEOUT_SECONDS:
            game.expire(now_value)
            expired.append(game)
            kept.append(game)
            continue
        if game.status in _ACTIVE_STATUSES:
            kept.append(game)
            continue
        if game.cleanup_pending:
            expired.append(game)
        if now_value - game.updated_at < TABLE_TIMEOUT_SECONDS:
            kept.append(game)
    _GAMES[scope] = kept
    return expired


async def cleanup_expired_and_edit(
    chat_id: int,
    thread_id: int | None,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    await _edit_expired_games(cleanup_expired(chat_id, thread_id), context)


async def _edit_expired_games(
    games: list[LiarsBarGame],
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    for game in games:
        if game.message_id is None:
            game.cleanup_pending = False
            continue
        async with game.lock:
            try:
                await context.bot.edit_message_text(
                    chat_id=game.chat_id,
                    message_id=game.message_id,
                    text=game.render_html(),
                    parse_mode=ParseMode.HTML,
                    reply_markup=None,
                )
            except Exception:
                continue
            game.cleanup_pending = False


async def liars_bar_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.effective_message
    chat = update.effective_chat
    user = update.effective_user
    if message is None or chat is None or user is None:
        return
    thread_id = getattr(message, "message_thread_id", None)
    await cleanup_expired_and_edit(chat.id, thread_id, context)
    try:
        game = create_game(chat.id, thread_id, user.id, _display_name(user))
    except LiarsBarError as exc:
        await message.reply_text(str(exc))
        return
    try:
        sent = await message.reply_html(game.render_html(), reply_markup=game.reply_markup())
    except BaseException:
        _remove_game(game)
        raise
    game.message_id = sent.message_id


async def liars_bar_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    user = update.effective_user
    if query is None or user is None or query.message is None or not query.data:
        return
    message = query.message
    if not getattr(message, "is_accessible", True):
        chat_id = getattr(getattr(message, "chat", None), "id", None)
        parsed = _parse_callback_data(query.data)
        if chat_id is not None and parsed is not None:
            game = _find_game_in_chat(chat_id, parsed[0])
            if game is not None and game.message_id == message.message_id:
                async with game.lock:
                    if game.status in _ACTIVE_STATUSES:
                        game.cancel(game.host_user_id)
                    _remove_game(game)
        await _answer_query(query, "Сообщение удалено, поэтому стол закрыт. Можно создать новый.")
        return
    chat_id = getattr(message, "chat_id", None)
    if chat_id is None:
        chat_id = getattr(getattr(message, "chat", None), "id", None)
    if chat_id is None:
        await _answer_query(query, "Этот стол уже недоступен.")
        return
    thread_id = getattr(message, "message_thread_id", None)
    expired_games = cleanup_expired(chat_id, thread_id)

    try:
        parsed = _parse_callback_data(query.data)
        if parsed is None:
            await _answer_query(query, "Кнопка сломалась.")
            return
        game_id, command = parsed
        game = get_game(chat_id, thread_id, game_id)
        if game is None or game.status == STATUS_EXPIRED:
            await _answer_query(query, "Этот стол уже закрыт.")
            return

        async with game.lock:
            if game.message_id is not None and message.message_id != game.message_id:
                await _answer_query(query, "Это старая кнопка.")
                return

            if command == "rules":
                await _answer_query(query, _rules_text(), show_alert=True)
                return
            if command == "cards":
                await _answer_query(query, game.private_hand_text(user.id), show_alert=True)
                return
            if command.startswith("pick:"):
                raw_number = command.partition(":")[2]
                if not raw_number.isdigit():
                    await _answer_query(query, "Кнопка сломалась.")
                    return
                result = game.select(user.id, int(raw_number))
                await _answer_query(query, result.text)
                return

            if command == "join":
                result = game.join(user.id, _display_name(user))
            elif command == "leave":
                result = game.leave(user.id)
            elif command == "start":
                result = game.start(user.id)
            elif command == "play":
                result = game.play(user.id)
            elif command == "liar":
                result = game.challenge(user.id)
            elif command == "cancel":
                result = game.cancel(user.id)
            else:
                await _answer_query(query, "Не понял кнопку.")
                return

            try:
                await _answer_query(query, result.text)
            finally:
                # Any recognized public action can repair a previously failed edit.
                await edit_query_message(query, game)
    finally:
        # Callback acknowledgements happen before potentially slow expiry edits.
        await _edit_expired_games(expired_games, context)


async def edit_query_message(query, game: LiarsBarGame) -> None:
    try:
        await query.edit_message_text(
            text=game.render_html(),
            parse_mode=ParseMode.HTML,
            reply_markup=game.reply_markup(),
        )
    except BadRequest as exc:
        if "message is not modified" in str(exc).lower():
            return
        raise


def reset_games_for_tests() -> None:
    global _GAME_IDS
    _GAMES.clear()
    _GAME_IDS = itertools.count(1)


def format_cards(cards: tuple[str, ...] | list[str]) -> str:
    return " ".join(_card_label(card) for card in cards)


def _parse_callback_data(data: str) -> tuple[int, str] | None:
    prefix, sep, rest = data.partition(":")
    if prefix != "lb" or not sep:
        return None
    raw_game_id, sep, command = rest.partition(":")
    if not sep or not raw_game_id.isdigit() or not command:
        return None
    return int(raw_game_id), command


async def _answer_query(query, text: str, show_alert: bool = False) -> None:
    try:
        await query.answer(text, show_alert=show_alert)
    except BadRequest as exc:
        message = str(exc).lower()
        if "query is too old" in message or "query id is invalid" in message or "response timeout" in message:
            return
        raise


def _display_name(user) -> str:
    if getattr(user, "full_name", None):
        return user.full_name
    if getattr(user, "username", None):
        return f"@{user.username}"
    return str(user.id)


def _rules_text() -> str:
    return (
        "2–4 игрока. Клади 1–3 карты, объявляя ранг стола; джокер честный. "
        "Следующий игрок кладет карты или жмет «Лжец!». Ошибившийся нажимает курок. "
        "Последний оставшийся побеждает."
    )


def _card_label(card: str) -> str:
    return "🃏" if card == JOKER else card


def _rank_label(rank: str | None) -> str:
    return {"A": "A", "K": "K", "Q": "Q"}.get(rank, "—")


def _chunk_buttons(
    buttons: list[InlineKeyboardButton],
    size: int,
) -> list[list[InlineKeyboardButton]]:
    return [buttons[index : index + size] for index in range(0, len(buttons), size)]


def _coerce_now(now: float | None) -> float:
    return time.time() if now is None else now
