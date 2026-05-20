"""Stateful heads-up Hold'em minigame."""

from __future__ import annotations

import html
import itertools
import math
import random
import re
import time
from dataclasses import dataclass, field
from typing import Final

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ParseMode
from telegram.error import BadRequest
from telegram.ext import ContextTypes
from treys import Card, Evaluator

from cards import (
    RANKS,
    canonical_pair,
    format_card_html,
    format_card_plain,
    format_hand_html,
    format_hand_plain,
    full_deck,
)

STARTING_STACK: Final[int] = 10_000
SMALL_BLIND: Final[int] = 50
BIG_BLIND: Final[int] = 100
MAX_TABLES_PER_CHAT: Final[int] = 2
TABLE_TIMEOUT_SECONDS: Final[float] = 5 * 60
FOLD_REVEAL_SECONDS: Final[float] = 60

STATUS_AWAITING_CONFIRM: Final[str] = "awaiting_confirm"
STATUS_BETTING: Final[str] = "betting"
STATUS_FOLD_REVEAL: Final[str] = "fold_reveal"
STATUS_ENDED: Final[str] = "ended"
STATUS_EXPIRED: Final[str] = "expired"

STREET_PREFLOP: Final[str] = "preflop"
STREET_FLOP: Final[str] = "flop"
STREET_TURN: Final[str] = "turn"
STREET_RIVER: Final[str] = "river"
STREET_SHOWDOWN: Final[str] = "showdown"

ACTION_CARDS: Final[str] = "cards"
ACTION_CHECK: Final[str] = "check"
ACTION_CALL: Final[str] = "call"
ACTION_FOLD: Final[str] = "fold"
ACTION_MIN_RAISE: Final[str] = "min_raise"
ACTION_POT_RAISE: Final[str] = "pot_raise"
ACTION_ALL_IN: Final[str] = "all_in"

_ACTIVE_STATUSES: Final[set[str]] = {
    STATUS_AWAITING_CONFIRM,
    STATUS_BETTING,
    STATUS_FOLD_REVEAL,
}

_TABLES: dict[int, list["GameTable"]] = {}
_TABLE_IDS = itertools.count(1)
_EVALUATOR: Final[Evaluator] = Evaluator()
_USERNAME_RE: Final[re.Pattern[str]] = re.compile(r"@?([A-Za-z0-9_]{5,32})")
_HAND_CLASS_RU: Final[dict[int, str]] = {
    1: "стрит-флеш",
    2: "каре",
    3: "фулл-хаус",
    4: "флеш",
    5: "стрит",
    6: "сет",
    7: "две пары",
    8: "пара",
    9: "старшая карта",
}


class HeadsUpError(Exception):
    """Base error for table creation failures."""


class TableLimitError(HeadsUpError):
    """Raised when a chat already has all heads-up tables occupied."""


class SeatTakenError(HeadsUpError):
    """Raised when the caller already has an active seat in the chat."""


@dataclass
class GameResult:
    kind: str
    text: str = ""


@dataclass
class PlayerState:
    role: str
    user_id: int | None
    username: str | None
    name: str
    hand: tuple[str, str]
    stack: int
    committed: int
    street_bet: int
    revealed: bool = False
    confirmed: bool = False
    acted: bool = False

    @property
    def label(self) -> str:
        return self.name


@dataclass
class GameTable:
    chat_id: int
    table_id: int
    players: dict[str, PlayerState]
    deck: list[str]
    created_at: float
    updated_at: float
    message_id: int | None = None
    status: str = STATUS_AWAITING_CONFIRM
    street: str = STREET_PREFLOP
    board: list[str] = field(default_factory=list)
    current_bet: int = BIG_BLIND
    min_raise: int = BIG_BLIND
    to_act: str | None = None
    action_log: list[str] = field(default_factory=list)
    action_log_streets: list[str] = field(default_factory=list)
    winner_role: str | None = None
    folded_role: str | None = None
    pending_reveal_role: str | None = None
    reveal_deadline: float | None = None
    fold_hand_revealed: bool = False
    river_aggressor_role: str | None = None
    mandatory_show_role: str | None = None
    public_revealed_roles: set[str] = field(default_factory=set)
    mucked_roles: set[str] = field(default_factory=set)
    all_in_equity_comment: str | None = None

    @property
    def pot(self) -> int:
        return sum(player.committed for player in self.players.values())

    def tap_cards(
        self,
        user_id: int,
        username: str | None,
        name: str,
        now: float | None = None,
    ) -> GameResult:
        now = _coerce_now(now)
        role = self._role_for_user(user_id, username, name)
        if role is None:
            return GameResult("not_in_game", "Ты не в раздаче.")

        self.updated_at = now
        player = self.players[role]
        if self.status == STATUS_AWAITING_CONFIRM and not player.confirmed:
            player.revealed = True
            player.confirmed = True
            if all(p.confirmed for p in self.players.values()):
                self._start_betting(now)
            return GameResult("ready", f"{self._private_hand_text(role)}\n\nТы в игре.")
        return GameResult("cards", self._private_hand_text(role))

    def legal_actions(self) -> dict[str, int | None]:
        if self.status != STATUS_BETTING or self.to_act is None:
            return {}
        actor = self.players[self.to_act]
        opponent = self.players[_other_role(self.to_act)]
        call_amount = max(0, self.current_bet - actor.street_bet)
        actions: dict[str, int | None] = {ACTION_FOLD: None}

        if call_amount == 0:
            actions[ACTION_CHECK] = actor.street_bet
        else:
            actions[ACTION_CALL] = actor.street_bet + min(call_amount, actor.stack)

        if actor.stack <= call_amount or opponent.stack == 0:
            return actions

        max_target = actor.street_bet + actor.stack
        min_target = self.current_bet + self.min_raise if self.current_bet else BIG_BLIND
        if min_target <= max_target:
            actions[ACTION_MIN_RAISE] = min_target

        if call_amount == 0:
            pot_target = max(BIG_BLIND, self.pot)
        else:
            pot_target = self.current_bet + self.pot + self.min_raise
        pot_target = min(max_target, max(min_target, pot_target))
        if pot_target > self.current_bet and pot_target != actions.get(ACTION_MIN_RAISE):
            actions[ACTION_POT_RAISE] = pot_target
        elif pot_target > self.current_bet:
            actions[ACTION_POT_RAISE] = pot_target

        if max_target > self.current_bet:
            actions[ACTION_ALL_IN] = max_target
        return actions

    def optional_reveal_roles(self) -> set[str]:
        if self.status != STATUS_ENDED:
            return set()
        decided = self.public_revealed_roles | self.mucked_roles
        return set(self.players) - decided

    def apply_action(
        self,
        user_id: int,
        action: str,
        now: float | None = None,
    ) -> GameResult:
        now = _coerce_now(now)
        if self.status != STATUS_BETTING or self.to_act is None:
            return GameResult("invalid", "Сейчас не время для ставок.")
        role = self._role_for_user_id(user_id)
        if role != self.to_act:
            return GameResult("not_your_turn", "Сейчас не твой ход.")

        legal = self.legal_actions()
        if action not in legal:
            return GameResult("invalid", "Такой кнопки у тебя сейчас нет.")

        self.updated_at = now
        actor = self.players[role]
        opponent_role = _other_role(role)
        opponent = self.players[opponent_role]

        if action == ACTION_FOLD:
            self.folded_role = role
            self.winner_role = opponent_role
            opponent.stack += self.pot
            self.status = STATUS_ENDED
            self.to_act = None
            self._log(f"{actor.label}: фолд")
            return GameResult("folded", f"{actor.label} выбрасывает. {opponent.label} забирает банк.")

        if action == ACTION_CHECK:
            actor.acted = True
            self._log(f"{actor.label}: чек")
            comment = _action_comment(self.street, action, 0, self.pot)
            if comment:
                self._log(comment)
        else:
            target = legal[action]
            assert isinstance(target, int)
            pot_before = self.pot
            paid = self._commit_to(actor, target)
            actor.acted = True
            if target > self.current_bet:
                previous_bet = self.current_bet
                raise_size = target - previous_bet
                self.current_bet = target
                if raise_size >= self.min_raise:
                    self.min_raise = raise_size
                opponent.acted = False
                if self.street == STREET_RIVER:
                    self.river_aggressor_role = role
                if action == ACTION_ALL_IN:
                    self._log(f"{actor.label}: олл-ин {_chips(target)}")
                elif previous_bet == 0:
                    self._log(f"{actor.label}: бет {_chips(target)}")
                else:
                    self._log(f"{actor.label}: рейз до {_chips(target)}")
                comment = _action_comment(self.street, action, paid, pot_before)
                if comment:
                    self._log(comment)
            else:
                self._log(f"{actor.label}: колл {_chips(paid)}")

        if self._bets_equal() and self._any_player_all_in():
            if len(self.board) < 5 and self.all_in_equity_comment is None:
                self.all_in_equity_comment = _all_in_equity_comment(self)
            self._runout_to_showdown(now)
            return GameResult("showdown", "Олл-ин закрыт, крутим доску до конца.")
        if self._round_complete():
            return self._advance_street(now)

        self.to_act = opponent_role
        return GameResult("acted", "Принято.")

    def choose_public_reveal(
        self,
        user_id: int,
        reveal: bool,
        now: float | None = None,
    ) -> GameResult:
        now = _coerce_now(now)
        if self.status != STATUS_ENDED:
            return GameResult("invalid", "Раздача еще не закрыта.")
        role = self._role_for_user_id(user_id)
        if role is None:
            return GameResult("not_in_game", "Ты не в раздаче.")
        if role not in self.optional_reveal_roles():
            return GameResult("already_decided", "Твои карты уже решили свою публичную судьбу.")
        self.updated_at = now
        if reveal:
            self.public_revealed_roles.add(role)
            self._log(f"{self.players[role].label}: показал(а) руку", STREET_SHOWDOWN)
        else:
            self.mucked_roles.add(role)
            self._log(f"{self.players[role].label}: не показал(а) руку", STREET_SHOWDOWN)
        return GameResult("ended", "Показали." if reveal else "Сброшено втихую.")

    def choose_fold_reveal(
        self,
        user_id: int,
        reveal: bool,
        now: float | None = None,
    ) -> GameResult:
        return self.choose_public_reveal(user_id, reveal, now)

    def auto_muck_fold(self, now: float | None = None) -> None:
        now = _coerce_now(now)
        if self.status != STATUS_FOLD_REVEAL:
            return
        self.updated_at = now
        self.fold_hand_revealed = False
        self.status = STATUS_ENDED
        if self.pending_reveal_role:
            folder = self.players[self.pending_reveal_role]
            self._log(f"{folder.label}: не успел(а) показать, карты ушли в пас", "system")

    def expire(self, now: float | None = None) -> None:
        self.updated_at = _coerce_now(now)
        self.status = STATUS_EXPIRED
        self.to_act = None
        self._log("Игроки думали слишком долго. Раздача закрыта.", "system")

    def render_html(self) -> str:
        lines = [
            f"<b>🃏 Хедз-ап стол #{self.table_id}</b>",
            f"👥 {self._player_line('sb', 'SB 50')} vs {self._player_line('bb', 'BB 100')}",
            f"💰 Банк: <b>{_chips(self.pot)}</b>",
            f"<blockquote>🂠 Доска: {self._board_html()}</blockquote>",
            "",
            f"🎲 Улица: <b>{_street_title(self.street)}</b>",
        ]
        if self.status == STATUS_AWAITING_CONFIRM:
            lines.extend(
                [
                    "",
                    "Жмем <b>Играть</b>: тап показывает карты лично тебе и сразу сажает за стол.",
                    f"🔥 {self._safe_label('bb')} и @{self.players['sb'].username}, хватит смотреть в потолок.",
                    f"✅ Подтверждения: {self._confirm_mark('sb')} SB / {self._confirm_mark('bb')} BB",
                ]
            )
        elif self.status == STATUS_BETTING:
            actor = self.players[self.to_act] if self.to_act else None
            if actor:
                lines.append(f"👉 Ход: <b>{html.escape(actor.label)}</b>")
            lines.append(self._owed_line())
        elif self.status == STATUS_FOLD_REVEAL:
            winner = self.players[self.winner_role] if self.winner_role else None
            folder = self.players[self.pending_reveal_role] if self.pending_reveal_role else None
            if winner:
                lines.append(f"🏆 Победитель: <b>{html.escape(winner.label)}</b>")
            if folder:
                lines.append(f"👀 {html.escape(folder.label)}, показать сброшенную руку или уйти загадкой?")
        elif self.status == STATUS_ENDED:
            lines.extend(self._ended_lines())
        elif self.status == STATUS_EXPIRED:
            lines.append("Игроки думали слишком долго. Раздача закрыта.")

        if self.action_log:
            lines.extend(self._action_log_lines())
        return "\n".join(lines)

    def reply_markup(self) -> InlineKeyboardMarkup | None:
        callback = lambda suffix: f"hu:{self.table_id}:{suffix}"
        if self.status == STATUS_AWAITING_CONFIRM:
            return InlineKeyboardMarkup(
                [[InlineKeyboardButton("🃏 Играть", callback_data=callback(ACTION_CARDS))]]
            )
        if self.status == STATUS_BETTING:
            rows: list[list[InlineKeyboardButton]] = [
                [InlineKeyboardButton("🃏 Мои карты", callback_data=callback(ACTION_CARDS))]
            ]
            action_buttons = []
            legal = self.legal_actions()
            for action in (
                ACTION_CHECK,
                ACTION_CALL,
                ACTION_FOLD,
                ACTION_MIN_RAISE,
                ACTION_POT_RAISE,
                ACTION_ALL_IN,
            ):
                if action in legal:
                    action_buttons.append(
                        InlineKeyboardButton(_action_label(action, legal[action]), callback_data=callback(f"act:{action}"))
                    )
            rows.extend(_chunk_buttons(action_buttons, 3))
            return InlineKeyboardMarkup(rows)
        if self.status == STATUS_FOLD_REVEAL:
            return InlineKeyboardMarkup(
                [
                    [
                        InlineKeyboardButton("👀 Показать", callback_data=callback("reveal:1")),
                        InlineKeyboardButton("🤫 Сбросить втихую", callback_data=callback("reveal:0")),
                    ]
                ]
            )
        if self.status == STATUS_ENDED and self.optional_reveal_roles():
            return InlineKeyboardMarkup(
                [
                    [
                        InlineKeyboardButton("👀 Показать мои карты", callback_data=callback("reveal:1")),
                        InlineKeyboardButton("🤫 Не показывать", callback_data=callback("reveal:0")),
                    ]
                ]
            )
        return None

    def _role_for_user(
        self,
        user_id: int,
        username: str | None,
        name: str,
    ) -> str | None:
        existing = self._role_for_user_id(user_id)
        if existing:
            return existing
        if user_has_active_seat(self.chat_id, user_id, except_table_id=self.table_id):
            return None
        normalized = normalize_username(username)
        small = self.players["sb"]
        if small.user_id is None and normalized and normalized == small.username:
            small.user_id = user_id
            small.name = name or f"@{normalized}"
            return "sb"
        return None

    def _role_for_user_id(self, user_id: int) -> str | None:
        for role, player in self.players.items():
            if player.user_id == user_id:
                return role
        return None

    def _private_hand_text(self, role: str) -> str:
        return f"Твои карты: {format_hand_plain(self.players[role].hand)}"

    def _start_betting(self, now: float) -> None:
        self.status = STATUS_BETTING
        self.to_act = "sb"
        self.updated_at = now
        self._log("Карты подтверждены. Малый блайнд первый.", STREET_PREFLOP)

    def _commit_to(self, player: PlayerState, target: int) -> int:
        need = max(0, target - player.street_bet)
        paid = min(need, player.stack)
        player.stack -= paid
        player.street_bet += paid
        player.committed += paid
        return paid

    def _round_complete(self) -> bool:
        return self._bets_equal() and all(player.acted for player in self.players.values())

    def _bets_equal(self) -> bool:
        bets = {player.street_bet for player in self.players.values()}
        return len(bets) == 1

    def _any_player_all_in(self) -> bool:
        return any(player.stack == 0 for player in self.players.values())

    def _advance_street(self, now: float) -> GameResult:
        if self.street == STREET_PREFLOP:
            self.board.extend(self._draw(3))
            self.street = STREET_FLOP
            self._log(f"Флоп: {self._board_plain()}", STREET_FLOP)
            self._log(_street_suspense(STREET_FLOP, self.board), STREET_FLOP)
            self._reset_street(now)
            return GameResult("advanced", "Флоп.")
        if self.street == STREET_FLOP:
            self.board.extend(self._draw(1))
            self.street = STREET_TURN
            self._log(f"Терн: {format_card_plain(self.board[-1])}", STREET_TURN)
            self._log(_street_suspense(STREET_TURN, self.board), STREET_TURN)
            self._reset_street(now)
            return GameResult("advanced", "Терн.")
        if self.street == STREET_TURN:
            self.board.extend(self._draw(1))
            self.street = STREET_RIVER
            self._log(f"Ривер: {format_card_plain(self.board[-1])}", STREET_RIVER)
            self._log(_street_suspense(STREET_RIVER, self.board), STREET_RIVER)
            self._reset_street(now)
            return GameResult("advanced", "Ривер.")
        self._runout_to_showdown(now)
        return GameResult("showdown", "Вскрытие.")

    def _reset_street(self, now: float) -> None:
        self.updated_at = now
        self.current_bet = 0
        self.min_raise = BIG_BLIND
        self.to_act = "bb"
        if self.street == STREET_RIVER:
            self.river_aggressor_role = None
        for player in self.players.values():
            player.street_bet = 0
            player.acted = False

    def _runout_to_showdown(self, now: float) -> None:
        while len(self.board) < 5:
            if len(self.board) == 0:
                self.board.extend(self._draw(3))
                self._log(f"Флоп: {self._board_plain()}", STREET_FLOP)
                self._log(_street_suspense(STREET_FLOP, self.board), STREET_FLOP)
            elif len(self.board) == 3:
                self.board.extend(self._draw(1))
                self._log(f"Терн: {format_card_plain(self.board[-1])}", STREET_TURN)
                self._log(_street_suspense(STREET_TURN, self.board), STREET_TURN)
            else:
                self.board.extend(self._draw(1))
                self._log(f"Ривер: {format_card_plain(self.board[-1])}", STREET_RIVER)
                self._log(_street_suspense(STREET_RIVER, self.board), STREET_RIVER)
        self._showdown(now)

    def _showdown(self, now: float) -> None:
        scores = {
            role: _evaluate(player.hand, self.board)
            for role, player in self.players.items()
        }
        sb_score, bb_score = scores["sb"], scores["bb"]
        if sb_score < bb_score:
            self.winner_role = "sb"
            self.players["sb"].stack += self.pot
            self._log(f"Вскрытие: {self.players['sb'].label} забирает {_chips(self.pot)}", STREET_SHOWDOWN)
        elif bb_score < sb_score:
            self.winner_role = "bb"
            self.players["bb"].stack += self.pot
            self._log(f"Вскрытие: {self.players['bb'].label} забирает {_chips(self.pot)}", STREET_SHOWDOWN)
        else:
            self.winner_role = None
            half = self.pot // 2
            odd = self.pot % 2
            self.players["sb"].stack += half + odd
            self.players["bb"].stack += half
            self._log(f"Вскрытие: дележ банка {_chips(self.pot)}", STREET_SHOWDOWN)
        self.mandatory_show_role = self.river_aggressor_role
        for role in self._showdown_required_roles():
            self.public_revealed_roles.add(role)
            self._log(f"{self.players[role].label}: обязан(а) показать {_showdown_show_reason(self, role)}", STREET_SHOWDOWN)
        self.status = STATUS_ENDED
        self.street = STREET_SHOWDOWN
        self.to_act = None
        self.updated_at = now

    def _showdown_required_roles(self) -> list[str]:
        roles: set[str] = set()
        if self.winner_role is None:
            roles.update(("sb", "bb"))
        else:
            roles.add(self.winner_role)
        if self.river_aggressor_role:
            roles.add(self.river_aggressor_role)
        return [role for role in ("sb", "bb") if role in roles]

    def _draw(self, count: int) -> list[str]:
        cards = self.deck[:count]
        del self.deck[:count]
        return cards

    def _player_line(self, role: str, blind: str) -> str:
        player = self.players[role]
        return f"<b>{html.escape(player.label)}</b> ({blind}, стек {_chips(player.stack)})"

    def _safe_label(self, role: str) -> str:
        return html.escape(self.players[role].label)

    def _confirm_mark(self, role: str) -> str:
        player = self.players[role]
        if player.confirmed:
            return "готов"
        if player.revealed:
            return "видел карты"
        return "спит"

    def _board_plain(self) -> str:
        return " ".join(format_card_plain(card) for card in self.board)

    def _board_html(self) -> str:
        if not self.board:
            return "<i>пока пусто</i>"
        return " ".join(format_card_html(card) for card in self.board)

    def _owed_line(self) -> str:
        if self.to_act is None:
            return ""
        actor = self.players[self.to_act]
        owed = max(0, self.current_bet - actor.street_bet)
        if owed:
            return f"💸 Доставить: <b>{_chips(owed)}</b>"
        return "✅ Можно чекнуть или устроить финансовую ошибку."

    def _ended_lines(self) -> list[str]:
        if self.folded_role:
            winner = self.players[self.winner_role] if self.winner_role else None
            lines = []
            if winner:
                lines.append(f"🏆 Победитель: <b>{html.escape(winner.label)}</b>")
            lines.append(self._public_hand_line("sb"))
            lines.append(self._public_hand_line("bb"))
            if self.optional_reveal_roles():
                lines.append("👀 Любой участник может показать руку, если очень хочется красивого позора.")
            else:
                lines.append("Фолд зафиксирован. Возможно, даже не преступление.")
            lines.extend(self._verdict_lines())
            lines.append(self._stacks_line())
            return lines

        lines = [
            self._public_hand_line("sb"),
            self._public_hand_line("bb"),
        ]
        if self.winner_role:
            winner = self.players[self.winner_role]
            loser = self.players[_other_role(self.winner_role)]
            lines.append(f"🏆 Победитель: <b>{html.escape(winner.label)}</b>. {html.escape(loser.label)} был слишком упрям, чтобы найти кнопку фолд.")
        else:
            lines.append("🤝 Ничья. Все очень старались, банк сделал вид, что ничего не было.")
        lines.extend(self._mandatory_show_lines())
        if self.optional_reveal_roles():
            lines.append("👀 Остальные могут показать руку добровольно или сохранить драматическую тайну.")
        lines.extend(self._verdict_lines())
        lines.append(self._stacks_line())
        return lines

    def _mandatory_show_lines(self) -> list[str]:
        if self.folded_role:
            return []
        if self.winner_role is None:
            return ["⚖️ Дележ банка: обе руки открыты."]

        lines = []
        winner = self.players[self.winner_role]
        if self.winner_role == self.mandatory_show_role:
            lines.append(
                f"⚖️ {html.escape(winner.label)} показывает обязательно: победная рука и последняя агрессия на ривере."
            )
        else:
            lines.append(f"⚖️ {html.escape(winner.label)} показывает обязательно: победную руку нельзя спрятать.")
            if self.mandatory_show_role:
                aggressor = self.players[self.mandatory_show_role]
                lines.append(f"⚖️ {html.escape(aggressor.label)} показывает обязательно: последняя агрессия на ривере.")
        return lines

    def _public_hand_line(self, role: str) -> str:
        player = self.players[role]
        prefix = role.upper()
        label = html.escape(player.label)
        if role in self.public_revealed_roles:
            suffix = ""
            if len(self.board) >= 3:
                suffix = f" — {_hand_class_name(player.hand, self.board)}"
            return f"{prefix} {label}: {format_hand_html(player.hand)}{suffix}"
        if role in self.mucked_roles:
            return f"{prefix} {label}: <i>не показал(а)</i>"
        return f"{prefix} {label}: <i>скрыта, можно показать</i>"

    def _verdict_lines(self) -> list[str]:
        comments = build_outcome_commentary(self)
        if not comments:
            return []
        lines = ["", "<b>🎙 Вердикт дилера:</b>"]
        lines.extend(f"• {html.escape(comment)}" for comment in comments)
        return lines

    def _stacks_line(self) -> str:
        return (
            f"💼 Стеки: {html.escape(self.players['sb'].label)} {_chips(self.players['sb'].stack)}, "
            f"{html.escape(self.players['bb'].label)} {_chips(self.players['bb'].stack)}"
        )

    def _log(self, text: str, street: str | None = None) -> None:
        self.action_log.append(text)
        self.action_log_streets.append(street or self.street)

    def _action_log_lines(self) -> list[str]:
        if not self.action_log:
            return []
        grouped: dict[str, list[str]] = {}
        for text, street in itertools.zip_longest(
            self.action_log,
            self.action_log_streets,
            fillvalue="system",
        ):
            grouped.setdefault(str(street), []).append(str(text))

        lines = ["", "<b>📜 Ход раздачи:</b>"]
        for street in ("setup", STREET_PREFLOP, STREET_FLOP, STREET_TURN, STREET_RIVER, STREET_SHOWDOWN, "system"):
            entries = grouped.get(street)
            if not entries:
                continue
            body = "; ".join(html.escape(entry) for entry in entries)
            lines.append(f"<b>{_street_log_title(street)}:</b> {body}")
        return lines


def normalize_username(username: str | None) -> str | None:
    if not username:
        return None
    value = username.strip().lstrip("@").lower()
    return value or None


def parse_challenge_username(text: str | None, args: list[str] | tuple[str, ...] | None = None) -> str | None:
    candidates = list(args or [])
    if not candidates and text:
        candidates = text.split()[1:]
    for candidate in candidates:
        match = _USERNAME_RE.fullmatch(candidate.strip())
        if match:
            return normalize_username(match.group(1))
    return None


def create_table(
    chat_id: int,
    caller_id: int,
    caller_username: str | None,
    caller_name: str,
    callee_username: str,
    now: float | None = None,
    deck: list[str] | None = None,
) -> GameTable:
    now = _coerce_now(now)
    cleanup_expired(chat_id, now)
    if tables_are_full(chat_id, now=None):
        raise TableLimitError("все столы заняты")
    if user_has_active_seat(chat_id, caller_id):
        raise SeatTakenError("ты уже сидишь за столом")

    callee = normalize_username(callee_username)
    caller_uname = normalize_username(caller_username)
    if callee is None:
        raise HeadsUpError("Позови игрока через @username.")
    if caller_uname and caller_uname == callee:
        raise HeadsUpError("Сам себя вызвал. Сильно, но нет.")

    draw_deck = list(deck) if deck is not None else _shuffled_deck()
    sb_hand = canonical_pair(draw_deck.pop(0), draw_deck.pop(0))
    bb_hand = canonical_pair(draw_deck.pop(0), draw_deck.pop(0))
    table = GameTable(
        chat_id=chat_id,
        table_id=next(_TABLE_IDS),
        players={
            "sb": PlayerState(
                role="sb",
                user_id=None,
                username=callee,
                name=f"@{callee}",
                hand=sb_hand,
                stack=STARTING_STACK - SMALL_BLIND,
                committed=SMALL_BLIND,
                street_bet=SMALL_BLIND,
            ),
            "bb": PlayerState(
                role="bb",
                user_id=caller_id,
                username=caller_uname,
                name=_seat_name(caller_name, caller_uname, caller_id),
                hand=bb_hand,
                stack=STARTING_STACK - BIG_BLIND,
                committed=BIG_BLIND,
                street_bet=BIG_BLIND,
            ),
        },
        deck=draw_deck,
        created_at=now,
        updated_at=now,
        action_log=[f"Блайнды: @{callee} 50, {_seat_name(caller_name, caller_uname, caller_id)} 100"],
        action_log_streets=["setup"],
    )
    _TABLES.setdefault(chat_id, []).append(table)
    return table


def stacked_deck(spec: dict[str, tuple[str, ...]]) -> list[str]:
    ordered: list[str] = []
    ordered.extend(spec.get("sb", ()))
    ordered.extend(spec.get("bb", ()))
    ordered.extend(spec.get("board", ()))
    seen = set(ordered)
    if len(seen) != len(ordered):
        raise ValueError("stacked deck contains duplicate cards")
    ordered.extend(card for card in full_deck() if card not in seen)
    return ordered


def active_tables(chat_id: int, now: float | None = None) -> list[GameTable]:
    if now is not None:
        cleanup_expired(chat_id, now)
    return [table for table in _TABLES.get(chat_id, []) if table.status in _ACTIVE_STATUSES]


def tables_are_full(chat_id: int, now: float | None = None) -> bool:
    return len(active_tables(chat_id, now=now)) >= MAX_TABLES_PER_CHAT


def heads_up_busy_message(chat_id: int, now: float | None = None) -> str | None:
    return "все столы заняты" if tables_are_full(chat_id, now=now) else None


def aces_busy_message(chat_id: int, now: float | None = None) -> str | None:
    return "все дилеры заняты" if tables_are_full(chat_id, now=now) else None


def build_outcome_commentary(table: GameTable) -> list[str]:
    if table.status != STATUS_ENDED:
        return []
    if table.folded_role:
        return _fold_commentary(table)
    return _showdown_commentary(table)


def user_has_active_seat(
    chat_id: int,
    user_id: int,
    except_table_id: int | None = None,
) -> bool:
    for table in active_tables(chat_id):
        if table.table_id == except_table_id:
            continue
        for player in table.players.values():
            if player.user_id == user_id:
                return True
    return False


def get_table(chat_id: int, table_id: int, now: float | None = None) -> GameTable | None:
    if now is not None:
        cleanup_expired(chat_id, now)
    for table in _TABLES.get(chat_id, []):
        if table.table_id == table_id:
            return table
    return None


def cleanup_expired(chat_id: int, now: float | None = None) -> list[GameTable]:
    now = _coerce_now(now)
    kept: list[GameTable] = []
    removed: list[GameTable] = []
    for table in _TABLES.get(chat_id, []):
        if table.status == STATUS_FOLD_REVEAL and table.reveal_deadline is not None and now >= table.reveal_deadline:
            table.auto_muck_fold(now)
            removed.append(table)
            kept.append(table)
            continue
        if table.status in _ACTIVE_STATUSES and now - table.updated_at >= TABLE_TIMEOUT_SECONDS:
            table.expire(now)
            removed.append(table)
            continue
        if table.status in _ACTIVE_STATUSES:
            kept.append(table)
            continue
        if table.status == STATUS_ENDED and now - table.updated_at < TABLE_TIMEOUT_SECONDS:
            kept.append(table)
    _TABLES[chat_id] = kept
    return removed


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


async def heads_up_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.effective_message
    chat = update.effective_chat
    user = update.effective_user
    if message is None or chat is None or user is None:
        return
    await cleanup_expired_and_edit(chat.id, context)
    busy = heads_up_busy_message(chat.id)
    if busy:
        await message.reply_text(busy)
        return

    callee = parse_challenge_username(message.text, context.args)
    if callee is None:
        await message.reply_text("Используй: /heads_up @username")
        return

    try:
        table = create_table(
            chat_id=chat.id,
            caller_id=user.id,
            caller_username=user.username,
            caller_name=_display_name(user),
            callee_username=callee,
        )
    except HeadsUpError as exc:
        await message.reply_text(str(exc))
        return

    sent = await message.reply_html(table.render_html(), reply_markup=table.reply_markup())
    table.message_id = sent.message_id


async def heads_up_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
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
    table_id, command = parsed
    table = get_table(chat_id, table_id)
    if table is None:
        await query.answer("Раздача уже закончилась.")
        return

    if command == ACTION_CARDS:
        result = table.tap_cards(user.id, user.username, _display_name(user))
        await query.answer(result.text, show_alert=result.kind == "cards")
        if result.kind == "ready":
            await edit_query_message(query, table)
        return

    if command.startswith("act:"):
        result = table.apply_action(user.id, command.split(":", 1)[1])
        await query.answer(result.text, show_alert=False)
        await edit_query_message(query, table)
        return

    if command.startswith("reveal:"):
        result = table.choose_public_reveal(user.id, reveal=command.endswith(":1"))
        await query.answer(result.text, show_alert=False)
        await edit_query_message(query, table)
        return

    await query.answer("Не понял кнопку.")


def reset_tables_for_tests() -> None:
    global _TABLE_IDS
    _TABLES.clear()
    _TABLE_IDS = itertools.count(1)


def _evaluate(hand: tuple[str, str], board: list[str]) -> int:
    hole = [Card.new(card) for card in hand]
    community = [Card.new(card) for card in board]
    return _EVALUATOR.evaluate(hole, community)


def _hand_class_name(hand: tuple[str, str], board: list[str]) -> str:
    rank_class = _EVALUATOR.get_rank_class(_evaluate(hand, board))
    return _HAND_CLASS_RU.get(rank_class, "рука")


def _all_in_equity_comment(table: GameTable) -> str | None:
    equities = _estimate_all_in_equities(table)
    if equities is None:
        return None
    sb_pct = round(equities["sb"] * 100)
    bb_pct = round(equities["bb"] * 100)
    if abs(sb_pct - bb_pct) <= 6:
        return f"На олл-ине почти монетка: {table.players['sb'].label} {sb_pct}%, {table.players['bb'].label} {bb_pct}%."
    fav = "sb" if sb_pct > bb_pct else "bb"
    return f"На олл-ине фаворит: {table.players[fav].label}, примерно {max(sb_pct, bb_pct)}%."


def _estimate_all_in_equities(table: GameTable) -> dict[str, float] | None:
    needed = 5 - len(table.board)
    if needed <= 0:
        return None
    remaining = list(table.deck)
    if len(remaining) < needed:
        return None

    total_combos = math.comb(len(remaining), needed)
    if total_combos <= 5_000:
        runouts = itertools.combinations(remaining, needed)
        total = total_combos
    else:
        rng = random.Random(1337)
        total = 2_500
        runouts = (tuple(rng.sample(remaining, needed)) for _ in range(total))

    wins = {"sb": 0.0, "bb": 0.0}
    for runout in runouts:
        board = table.board + list(runout)
        sb_score = _evaluate(table.players["sb"].hand, board)
        bb_score = _evaluate(table.players["bb"].hand, board)
        if sb_score < bb_score:
            wins["sb"] += 1.0
        elif bb_score < sb_score:
            wins["bb"] += 1.0
        else:
            wins["sb"] += 0.5
            wins["bb"] += 0.5
    return {role: value / total for role, value in wins.items()}


def _action_comment(
    street: str,
    action: str,
    paid: int,
    pot_before: int,
) -> str | None:
    if action == ACTION_ALL_IN:
        return "Олл-ин. Теперь решает доска."
    if action == ACTION_CHECK and street == STREET_RIVER:
        return "Чек на ривере. Осторожно, но понятно."
    if action in {ACTION_MIN_RAISE, ACTION_POT_RAISE}:
        if paid >= max(BIG_BLIND * 3, pot_before):
            return "Крупная ставка. Вопрос поставлен прямо."
        if paid <= max(BIG_BLIND, pot_before // 4):
            return "Маленькая ставка. Проверка, не давление."
    return None


def _street_suspense(street: str, board: list[str]) -> str:
    if street == STREET_FLOP:
        if _board_is_paired(board):
            return "Флоп спарился. Простых решений меньше."
        if _has_flush_draw(board) or _has_straight_texture([card[0] for card in board]):
            return "Флоп с дро. Аккуратнее с героизмом."
        return "Флоп сухой. Пока без паники."
    if street == STREET_TURN:
        if _board_is_paired(board) or _has_flush_draw(board) or _has_straight_texture([card[0] for card in board]):
            return "Терн усилил доску."
        return "Терн спокойный."
    if street == STREET_RIVER:
        if _has_flush_draw(board) or _has_straight_texture([card[0] for card in board]):
            return "Ривер закрыл очевидные варианты."
        return "Ривер без фейерверков."
    return ""


def _board_is_paired(board: list[str]) -> bool:
    ranks = [card[0] for card in board]
    return len(ranks) != len(set(ranks))


def _has_flush_draw(board: list[str]) -> bool:
    suits = [card[1] for card in board]
    if not suits:
        return False
    threshold = 2 if len(board) == 3 else 3
    return max(suits.count(suit) for suit in set(suits)) >= threshold


def _street_log_title(street: str) -> str:
    if street == "setup":
        return "Раздача"
    if street == "system":
        return "Система"
    return _street_title(street)


def _fold_commentary(table: GameTable) -> list[str]:
    if not _both_hands_public(table):
        partial = _partial_fold_commentary(table)
        if partial:
            return partial
        return ["Фолд принят, но руки скрыты: дилер не вскрывает тайну без разрешения."]

    folder = table.folded_role
    winner = table.winner_role
    if folder is None or winner is None:
        return []

    comparison = _compare_roles(table, folder, winner, table.board)
    if comparison is None:
        comparison = _compare_preflop_strength(table, folder, winner)

    if comparison > 0:
        return ["Хороший фолд. Не геройство, а санитарная норма."]
    if comparison < 0:
        return ["Это был не фолд, это эвакуация с лучшей рукой."]
    return ["Фолд спорный. Судьи спорят, банк уже уехал."]


def _partial_fold_commentary(table: GameTable) -> list[str]:
    shown = [
        role
        for role in ("sb", "bb")
        if role in table.public_revealed_roles and len(table.board) >= 3
    ]
    if not shown:
        return []

    comments = [
        f"{table.players[role].label} показал(а): {_hand_class_name(table.players[role].hand, table.board)}."
        for role in shown
    ]
    winner_role = table.winner_role
    if winner_role in shown and _shown_hand_is_strong(table, winner_role):
        comments.append("Против такой руки фолд выглядит нормально.")
    elif table.folded_role in shown:
        comments.append("Вторая рука скрыта, так что точный диагноз оставим без приговора.")
    else:
        comments.append("Сброшенная рука скрыта: оценить фолд до конца нельзя.")
    return comments[:3]


def _showdown_show_reason(table: GameTable, role: str) -> str:
    reasons = []
    if table.winner_role is None:
        reasons.append("руку для дележа")
    elif role == table.winner_role:
        reasons.append("победную руку")
    if role == table.river_aggressor_role:
        reasons.append("риверную агрессию")
    return " и ".join(reasons) if reasons else "руку"


def _showdown_commentary(table: GameTable) -> list[str]:
    comments: list[str] = []
    texture = _board_texture_comment(table.board)
    if texture:
        comments.append(texture)

    if not _both_hands_public(table):
        comments.extend(_partial_showdown_commentary(table))
        return comments[:3]

    if table.all_in_equity_comment:
        comments.append(table.all_in_equity_comment)
    river = _river_swing_comment(table)
    if river:
        comments.insert(0, river)
    elif table.winner_role and _preflop_underdog_won(table):
        comments.append("До раздачи это выглядело смелее для проигравшего. Карты спокойно объяснили обратное.")
    elif table.winner_role:
        comments.append("Вскрытие без мистики: сильная рука дошла до кассы.")
    else:
        comments.append("Дележ банка. Самый дипломатичный способ никого не обрадовать.")
    return comments[:3]


def _partial_showdown_commentary(table: GameTable) -> list[str]:
    shown = [
        role
        for role in ("sb", "bb")
        if role in table.public_revealed_roles and len(table.board) >= 3
    ]
    if not shown:
        return ["Руки скрыты: без вскрытия дилер комментирует только доску и банк."]

    comments = []
    if table.winner_role in shown:
        winner = table.players[table.winner_role]
        comments.append(f"Победная рука открыта: {winner.label} — {_hand_class_name(winner.hand, table.board)}.")
    else:
        comments.extend(
            f"{table.players[role].label} показал(а): {_hand_class_name(table.players[role].hand, table.board)}."
            for role in shown
        )
    comments.append("Остальное скрыто: лишнего дилер не досочиняет.")
    return comments


def _shown_hand_is_strong(table: GameTable, role: str) -> bool:
    rank_class = _EVALUATOR.get_rank_class(_evaluate(table.players[role].hand, table.board))
    return rank_class <= 5


def _both_hands_public(table: GameTable) -> bool:
    return all(role in table.public_revealed_roles for role in ("sb", "bb"))


def _compare_roles(
    table: GameTable,
    left_role: str,
    right_role: str,
    board: list[str],
) -> int | None:
    if len(board) < 3:
        return None
    left = _evaluate(table.players[left_role].hand, board)
    right = _evaluate(table.players[right_role].hand, board)
    if left > right:
        return 1
    if left < right:
        return -1
    return 0


def _compare_preflop_strength(table: GameTable, left_role: str, right_role: str) -> int:
    left = _preflop_strength(table.players[left_role].hand)
    right = _preflop_strength(table.players[right_role].hand)
    if left < right:
        return 1
    if left > right:
        return -1
    return 0


def _preflop_strength(hand: tuple[str, str]) -> int:
    ranks = sorted((RANKS.index(card[0]) for card in hand), reverse=True)
    suited = hand[0][1] == hand[1][1]
    if ranks[0] == ranks[1]:
        return 200 + ranks[0]
    connected_bonus = max(0, 4 - abs(ranks[0] - ranks[1]))
    return ranks[0] * 12 + ranks[1] + (3 if suited else 0) + connected_bonus


def _river_swing_comment(table: GameTable) -> str | None:
    if len(table.board) != 5 or table.winner_role is None:
        return None
    turn_winner = _winner_for_board(table, table.board[:4])
    if turn_winner is not None and turn_winner != table.winner_role:
        return "Ривер переписал результат, медицина бессильна."
    return None


def _winner_for_board(table: GameTable, board: list[str]) -> str | None:
    if len(board) < 3:
        return None
    sb = _evaluate(table.players["sb"].hand, board)
    bb = _evaluate(table.players["bb"].hand, board)
    if sb < bb:
        return "sb"
    if bb < sb:
        return "bb"
    return None


def _preflop_underdog_won(table: GameTable) -> bool:
    if table.winner_role is None:
        return False
    loser_role = _other_role(table.winner_role)
    return _preflop_strength(table.players[table.winner_role].hand) < _preflop_strength(table.players[loser_role].hand)


def _board_texture_comment(board: list[str]) -> str | None:
    if len(board) < 5:
        return None
    ranks = [card[0] for card in board]
    suits = [card[1] for card in board]
    if max(ranks.count(rank) for rank in set(ranks)) >= 2:
        return "Доска спарилась и сразу начала портить людям планы."
    if max(suits.count(suit) for suit in set(suits)) >= 4:
        return "Флешовая доска приехала без приглашения, но с документами."
    if _has_straight_texture(ranks):
        return "Стрит лежал на столе так явно, что почти просил чаевые."
    return "Доска сухая, как объяснение после плохого олл-ина."


def _has_straight_texture(ranks: list[str]) -> bool:
    values = {RANKS.index(rank) for rank in ranks}
    if RANKS.index("A") in values:
        values.add(-1)
    for start in range(-1, 9):
        if len(values & set(range(start, start + 5))) >= 4:
            return True
    return False


def _shuffled_deck() -> list[str]:
    deck = full_deck()
    random.shuffle(deck)
    return deck


def _coerce_now(now: float | None) -> float:
    return time.time() if now is None else now


def _other_role(role: str) -> str:
    return "bb" if role == "sb" else "sb"


def _street_title(street: str) -> str:
    return {
        STREET_PREFLOP: "Префлоп",
        STREET_FLOP: "Флоп",
        STREET_TURN: "Терн",
        STREET_RIVER: "Ривер",
        STREET_SHOWDOWN: "Вскрытие",
    }.get(street, street)


def _action_label(action: str, target: int | None) -> str:
    if action == ACTION_CHECK:
        return "✅ Чек"
    if action == ACTION_CALL:
        return f"📞 Колл {_chips(target)}"
    if action == ACTION_FOLD:
        return "🗑 Фолд"
    if action == ACTION_MIN_RAISE:
        return f"⬆️ Мин {_chips(target)}"
    if action == ACTION_POT_RAISE:
        return f"💰 Пот {_chips(target)}"
    if action == ACTION_ALL_IN:
        return f"🚀 Олл-ин {_chips(target)}"
    return action


def _chunk_buttons(
    buttons: list[InlineKeyboardButton],
    size: int,
) -> list[list[InlineKeyboardButton]]:
    return [buttons[i : i + size] for i in range(0, len(buttons), size)]


def _parse_callback_data(data: str) -> tuple[int, str] | None:
    prefix, sep, rest = data.partition(":")
    if prefix != "hu" or not sep:
        return None
    raw_table, sep, command = rest.partition(":")
    if not sep or not raw_table.isdigit():
        return None
    return int(raw_table), command


def _display_name(user) -> str:
    if getattr(user, "full_name", None):
        return user.full_name
    if getattr(user, "username", None):
        return f"@{user.username}"
    return str(user.id)


def _chips(value: int | None) -> str:
    if value is None:
        return ""
    return f"{value:,}".replace(",", " ")


def _seat_name(name: str | None, username: str | None, user_id: int) -> str:
    if name:
        return name
    if username:
        return f"@{username}"
    return str(user_id)


async def edit_query_message(query, table: GameTable) -> None:
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
