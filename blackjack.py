"""Stateful one-player blackjack minigame."""

from __future__ import annotations

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

from cards import format_card_html, full_deck

TABLE_TIMEOUT_SECONDS: Final[float] = 5 * 60

STATUS_PLAYING: Final[str] = "playing"
STATUS_ENDED: Final[str] = "ended"
STATUS_EXPIRED: Final[str] = "expired"

ACTION_HIT: Final[str] = "hit"
ACTION_STAND: Final[str] = "stand"

OUTCOME_PLAYER: Final[str] = "player"
OUTCOME_DEALER: Final[str] = "dealer"
OUTCOME_PUSH: Final[str] = "push"

_ACTIVE_STATUSES: Final[set[str]] = {STATUS_PLAYING}
_TABLES: dict[int, list["BlackjackTable"]] = {}
_TABLE_IDS = itertools.count(1)


class BlackjackError(Exception):
    """Base error for blackjack table failures."""


class TableLimitError(BlackjackError):
    """Raised when a chat already has an active blackjack table."""


@dataclass
class BlackjackResult:
    kind: str
    text: str = ""


@dataclass
class BlackjackTable:
    chat_id: int
    table_id: int
    player_id: int
    player_name: str
    deck: list[str]
    created_at: float
    updated_at: float
    message_id: int | None = None
    status: str = STATUS_PLAYING
    player_hand: list[str] = field(default_factory=list)
    dealer_hand: list[str] = field(default_factory=list)
    outcome: str | None = None

    def hit(self, user_id: int, now: float | None = None) -> BlackjackResult:
        now = _coerce_now(now)
        if user_id != self.player_id:
            return BlackjackResult("not_in_game", "Это не твой стол.")
        if self.status != STATUS_PLAYING:
            return BlackjackResult("ended", "Раздача уже закрыта.")

        self.updated_at = now
        self.player_hand.append(self._draw())
        if _hand_value(self.player_hand) > 21:
            self.outcome = OUTCOME_DEALER
            self.status = STATUS_ENDED
            return BlackjackResult("ended", "Перебор.")
        if _hand_value(self.player_hand) == 21:
            self._play_dealer()
            self._settle()
            return BlackjackResult("ended", "Двадцать одно.")
        return BlackjackResult("acted", "Карта.")

    def stand(self, user_id: int, now: float | None = None) -> BlackjackResult:
        now = _coerce_now(now)
        if user_id != self.player_id:
            return BlackjackResult("not_in_game", "Это не твой стол.")
        if self.status != STATUS_PLAYING:
            return BlackjackResult("ended", "Раздача уже закрыта.")

        self.updated_at = now
        self._play_dealer()
        self._settle()
        return BlackjackResult("ended", "Стоп.")

    def expire(self, now: float | None = None) -> None:
        self.updated_at = _coerce_now(now)
        self.status = STATUS_EXPIRED

    def render_html(self) -> str:
        lines = [
            f"<b>🂡 Blackjack стол #{self.table_id}</b>",
            f"Игрок: <b>{html.escape(self.player_name)}</b>",
            "",
            f"Твои карты: {self._hand_html(self.player_hand)} — <b>{_hand_value(self.player_hand)}</b>",
            f"Дилер: {self._dealer_html()}",
        ]
        if self.status == STATUS_PLAYING:
            lines.extend(["", "Выбирай: еще карту или стоп."])
        elif self.status == STATUS_ENDED:
            lines.extend(["", f"<b>{_outcome_title(self.outcome, self.player_hand, self.dealer_hand)}</b>"])
        elif self.status == STATUS_EXPIRED:
            lines.extend(["", "Раздача закрыта по таймауту."])
        return "\n".join(lines)

    def reply_markup(self) -> InlineKeyboardMarkup | None:
        if self.status != STATUS_PLAYING:
            return None
        prefix = f"bj:{self.table_id}:"
        return InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton("Еще карту", callback_data=f"{prefix}{ACTION_HIT}"),
                    InlineKeyboardButton("Стоп", callback_data=f"{prefix}{ACTION_STAND}"),
                ]
            ]
        )

    def _dealer_html(self) -> str:
        if self.status == STATUS_PLAYING and self.dealer_hand:
            return f"{format_card_html(self.dealer_hand[0])} 🂠"
        return f"{self._hand_html(self.dealer_hand)} — <b>{_hand_value(self.dealer_hand)}</b>"

    def _hand_html(self, hand: list[str]) -> str:
        return " ".join(format_card_html(card) for card in hand)

    def _draw(self) -> str:
        return self.deck.pop(0)

    def _play_dealer(self) -> None:
        while _hand_value(self.dealer_hand) < 17:
            self.dealer_hand.append(self._draw())

    def _settle(self) -> None:
        player = _hand_value(self.player_hand)
        dealer = _hand_value(self.dealer_hand)
        if player > 21:
            self.outcome = OUTCOME_DEALER
        elif dealer > 21 or player > dealer:
            self.outcome = OUTCOME_PLAYER
        elif dealer > player:
            self.outcome = OUTCOME_DEALER
        else:
            self.outcome = OUTCOME_PUSH
        self.status = STATUS_ENDED


def create_table(
    chat_id: int,
    player_id: int,
    player_name: str,
    now: float | None = None,
    deck: list[str] | None = None,
) -> BlackjackTable:
    now = _coerce_now(now)
    cleanup_expired(chat_id, now)
    if active_table(chat_id) is not None:
        raise TableLimitError("blackjack стол уже занят")

    draw_deck = list(deck) if deck is not None else _shuffled_deck()
    table = BlackjackTable(
        chat_id=chat_id,
        table_id=next(_TABLE_IDS),
        player_id=player_id,
        player_name=player_name,
        deck=draw_deck,
        created_at=now,
        updated_at=now,
    )
    table.player_hand.append(table._draw())
    table.dealer_hand.append(table._draw())
    table.player_hand.append(table._draw())
    table.dealer_hand.append(table._draw())
    if _hand_value(table.player_hand) == 21 or _hand_value(table.dealer_hand) == 21:
        table._settle()
    _TABLES.setdefault(chat_id, []).append(table)
    return table


def stacked_deck(cards: tuple[str, ...]) -> list[str]:
    seen = set(cards)
    if len(seen) != len(cards):
        raise ValueError("stacked deck contains duplicate cards")
    return list(cards) + [card for card in full_deck() if card not in seen]


def active_table(chat_id: int, now: float | None = None) -> BlackjackTable | None:
    if now is not None:
        cleanup_expired(chat_id, now)
    for table in _TABLES.get(chat_id, []):
        if table.status in _ACTIVE_STATUSES:
            return table
    return None


def get_table(chat_id: int, table_id: int, now: float | None = None) -> BlackjackTable | None:
    if now is not None:
        cleanup_expired(chat_id, now)
    for table in _TABLES.get(chat_id, []):
        if table.table_id == table_id:
            return table
    return None


def cleanup_expired(chat_id: int, now: float | None = None) -> list[BlackjackTable]:
    now = _coerce_now(now)
    kept: list[BlackjackTable] = []
    expired: list[BlackjackTable] = []
    for table in _TABLES.get(chat_id, []):
        if table.status == STATUS_PLAYING and now - table.updated_at >= TABLE_TIMEOUT_SECONDS:
            table.expire(now)
            expired.append(table)
            continue
        if table.status == STATUS_PLAYING:
            kept.append(table)
            continue
        if table.status == STATUS_ENDED and now - table.updated_at < TABLE_TIMEOUT_SECONDS:
            kept.append(table)
    _TABLES[chat_id] = kept
    return expired


async def cleanup_expired_and_edit(chat_id: int, context: ContextTypes.DEFAULT_TYPE) -> None:
    for table in cleanup_expired(chat_id):
        if table.message_id is None:
            continue
        try:
            await context.bot.edit_message_text(
                chat_id=chat_id,
                message_id=table.message_id,
                text=table.render_html(),
                parse_mode=ParseMode.HTML,
                reply_markup=None,
            )
        except Exception:
            pass


async def blackjack_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.effective_message
    chat = update.effective_chat
    user = update.effective_user
    if message is None or chat is None or user is None:
        return
    await cleanup_expired_and_edit(chat.id, context)

    try:
        table = create_table(chat.id, user.id, _display_name(user))
    except BlackjackError as exc:
        await message.reply_text(str(exc))
        return

    sent = await message.reply_html(table.render_html(), reply_markup=table.reply_markup())
    table.message_id = sent.message_id


async def blackjack_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    user = update.effective_user
    if query is None or user is None or query.message is None or not query.data:
        return
    chat_id = query.message.chat_id
    await cleanup_expired_and_edit(chat_id, context)

    parsed = _parse_callback_data(query.data)
    if parsed is None:
        await query.answer("Кнопка сломалась.")
        return
    table_id, action = parsed
    table = get_table(chat_id, table_id)
    if table is None:
        await query.answer("Раздача уже закончилась.")
        return

    if action == ACTION_HIT:
        result = table.hit(user.id)
    elif action == ACTION_STAND:
        result = table.stand(user.id)
    else:
        await query.answer("Не понял кнопку.")
        return

    await query.answer(result.text, show_alert=False)
    await edit_query_message(query, table)


def reset_tables_for_tests() -> None:
    global _TABLE_IDS
    _TABLES.clear()
    _TABLE_IDS = itertools.count(1)


def _hand_value(hand: list[str]) -> int:
    total = 0
    aces = 0
    for card in hand:
        rank = card[0]
        if rank == "A":
            total += 11
            aces += 1
        elif rank in {"T", "J", "Q", "K"}:
            total += 10
        else:
            total += int(rank)
    while total > 21 and aces:
        total -= 10
        aces -= 1
    return total


def _outcome_title(outcome: str | None, player_hand: list[str], dealer_hand: list[str]) -> str:
    if _hand_value(player_hand) > 21:
        return "Победа дилера: перебор игрока"
    if _hand_value(dealer_hand) > 21:
        return "Победа игрока: перебор дилера"
    if outcome == OUTCOME_PLAYER:
        return "Победа игрока"
    if outcome == OUTCOME_DEALER:
        return "Победа дилера"
    if outcome == OUTCOME_PUSH:
        return "Пуш"
    return "Раздача закрыта"


def _parse_callback_data(data: str) -> tuple[int, str] | None:
    prefix, sep, rest = data.partition(":")
    if prefix != "bj" or not sep:
        return None
    raw_table, sep, command = rest.partition(":")
    if not sep or not raw_table.isdigit():
        return None
    return int(raw_table), command


def _shuffled_deck() -> list[str]:
    deck = full_deck()
    random.shuffle(deck)
    return deck


def _coerce_now(now: float | None) -> float:
    return time.time() if now is None else now


def _display_name(user) -> str:
    if getattr(user, "full_name", None):
        return user.full_name
    if getattr(user, "username", None):
        return f"@{user.username}"
    return str(user.id)


async def edit_query_message(query, table: BlackjackTable) -> None:
    try:
        await query.edit_message_text(
            text=table.render_html(),
            parse_mode=ParseMode.HTML,
            reply_markup=table.reply_markup(),
        )
    except BadRequest as exc:
        if "message is not modified" in str(exc).lower():
            return
        raise
