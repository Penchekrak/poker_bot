"""Multi-player Telegram poker room engine and public-state persistence."""

from __future__ import annotations

import itertools
import json
import os
import random
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Final

from treys import Card, Evaluator

from cards import canonical_pair, format_hand_plain, full_deck

STARTING_STACK: Final[int] = 10_000
SMALL_BLIND: Final[int] = 50
BIG_BLIND: Final[int] = 100
MAX_SEATS: Final[int] = 10
RESERVED_SEAT_USER_ID: Final[int] = 922_489_940
RESERVED_SEAT_USER_ID_ENV: Final[str] = "POKER_RESERVED_SEAT_USER_ID"
TURN_TIMEOUT_SECONDS: Final[float] = 120.0
AUTO_DEAL_SECONDS: Final[float] = 30.0
AUTO_SIT_OUT_TIMEOUTS: Final[int] = 2

ROOM_JOIN: Final[str] = "join"
ROOM_REBUY: Final[str] = "rebuy"
ROOM_SIT_OUT: Final[str] = "sit_out"
ROOM_LEAVE: Final[str] = "leave"

STATUS_BETTING: Final[str] = "betting"
STATUS_ENDED: Final[str] = "ended"

STREET_PREFLOP: Final[str] = "preflop"
STREET_FLOP: Final[str] = "flop"
STREET_TURN: Final[str] = "turn"
STREET_RIVER: Final[str] = "river"
STREET_SHOWDOWN: Final[str] = "showdown"

_EVALUATOR: Final[Evaluator] = Evaluator()


class PokerRoomError(Exception):
    """Base error for poker-room failures."""


class SeatLimitError(PokerRoomError):
    """Raised when a room already has ten seats."""


class PokerActionError(PokerRoomError):
    """Raised when a poker action is illegal."""


@dataclass(frozen=True)
class GameResult:
    kind: str
    text: str = ""
    new_cards: bool = False


@dataclass(frozen=True)
class PlayerAction:
    action: str
    amount: int | None = None


@dataclass
class Seat:
    user_id: int
    username: str | None
    name: str
    stack: int = STARTING_STACK
    sitting_out: bool = False
    leave_next_hand: bool = False
    auto_timeout_count: int = 0
    last_rebuy_date: str | None = None

    def public_dict(self) -> dict[str, object]:
        return {
            "user_id": self.user_id,
            "username": self.username,
            "name": self.name,
            "stack": self.stack,
            "sitting_out": self.sitting_out,
            "leave_next_hand": self.leave_next_hand,
            "auto_timeout_count": self.auto_timeout_count,
            "last_rebuy_date": self.last_rebuy_date,
        }

    @classmethod
    def from_dict(cls, data: dict[str, object]) -> "Seat":
        return cls(
            user_id=int(data["user_id"]),
            username=data.get("username") if isinstance(data.get("username"), str) else None,
            name=str(data.get("name") or data["user_id"]),
            stack=int(data.get("stack", STARTING_STACK)),
            sitting_out=bool(data.get("sitting_out", False)),
            leave_next_hand=bool(data.get("leave_next_hand", False)),
            auto_timeout_count=max(0, int(data.get("auto_timeout_count", 0))),
            last_rebuy_date=data.get("last_rebuy_date") if isinstance(data.get("last_rebuy_date"), str) else None,
        )


@dataclass
class HandPlayer:
    user_id: int
    seat_index: int
    name: str
    hand: tuple[str, str]
    stack: int
    initial_stack: int = 0
    committed: int = 0
    street_bet: int = 0
    folded: bool = False
    all_in: bool = False
    acted: bool = False


@dataclass
class SidePot:
    amount: int
    eligible_user_ids: list[int]
    winner_user_ids: list[int] = field(default_factory=list)


@dataclass(frozen=True)
class PotResolution:
    """Public outcome of one (side) pot."""

    label: str
    amount: int
    winner_user_ids: tuple[int, ...]
    winner_names: tuple[str, ...]
    hand_category: str | None


@dataclass(frozen=True)
class HandResolution:
    """Aggregate outcome of a finished hand: pot results, board, and per-player chip deltas."""

    pots: tuple[PotResolution, ...]
    board: tuple[str, ...]
    stack_deltas: tuple[tuple[int, int], ...]
    showdown: bool


HAND_CATEGORY_RU: Final[dict[str, str]] = {
    "High Card": "Старшая карта",
    "Pair": "Пара",
    "Two Pair": "Две пары",
    "Three of a Kind": "Сет",
    "Straight": "Стрит",
    "Flush": "Флеш",
    "Full House": "Фулл-хаус",
    "Four of a Kind": "Каре",
    "Straight Flush": "Стрит-флеш",
    "Royal Flush": "Роял-флеш",
}


@dataclass
class PokerRoom:
    now: float | None = None
    seats: dict[int, Seat] = field(default_factory=dict)
    seat_order: list[int] = field(default_factory=list)
    button_user_id: int | None = None
    is_open: bool = True
    current_hand: "PokerHand | None" = None

    def confirm_room_intent(
        self,
        user_id: int,
        username: str | None,
        name: str,
        intent: str,
        now: float | None = None,
    ) -> GameResult:
        if intent == ROOM_JOIN:
            return self._join(user_id, username, name)
        if intent == ROOM_SIT_OUT:
            seat = self._require_seat(user_id)
            seat.sitting_out = True
            seat.auto_timeout_count = 0
            return GameResult("room", "Сядешь вне игры со следующей раздачи.")
        if intent == ROOM_LEAVE:
            seat = self._require_seat(user_id)
            if _is_reserved_seat(user_id):
                seat.leave_next_hand = False
                seat.sitting_out = True
                seat.auto_timeout_count = 0
                return GameResult("room", "Место сохранено, ситаут включён.")
            seat.leave_next_hand = True
            seat.sitting_out = True
            seat.auto_timeout_count = 0
            if self.current_hand is None or self.current_hand.status == STATUS_ENDED:
                self._remove_left_seats()
            return GameResult("room", "Выход из-за стола принят.")
        if intent == ROOM_REBUY:
            seat = self._require_seat(user_id)
            today = _date_key(now)
            if seat.last_rebuy_date == today:
                return GameResult("rejected", "Ребай сегодня уже был.")
            if seat.stack > 0:
                return GameResult("rejected", "Ребай доступен после нуля.")
            seat.stack = STARTING_STACK
            seat.sitting_out = False
            seat.auto_timeout_count = 0
            seat.last_rebuy_date = today
            return GameResult("room", "Ребай 10 000 принят.")
        raise PokerRoomError("unknown room intent")

    def start_hand(self, now: float | None = None, deck: list[str] | None = None) -> "PokerHand":
        if not self.is_open:
            raise PokerRoomError("стол закрыт")
        if self.current_hand and self.current_hand.status != STATUS_ENDED:
            raise PokerRoomError("hand already active")
        self._remove_left_seats()
        active_user_ids = [
            user_id
            for user_id in self.seat_order
            if user_id in self.seats
            and not self.seats[user_id].sitting_out
            and self.seats[user_id].stack > 0
        ]
        if len(active_user_ids) < 2:
            raise PokerRoomError("need at least two active players")

        button = self._next_button(active_user_ids)
        self.button_user_id = button
        draw_deck = list(deck) if deck is not None else _shuffled_deck()
        players: dict[int, HandPlayer] = {}
        for index, user_id in enumerate(active_user_ids):
            seat = self.seats[user_id]
            hand = canonical_pair(draw_deck.pop(0), draw_deck.pop(0))
            players[user_id] = HandPlayer(
                user_id=user_id,
                seat_index=index,
                name=seat.name,
                hand=hand,
                stack=seat.stack,
                initial_stack=seat.stack,
            )

        created_at = _coerce_now(now)
        hand = PokerHand(
            room=self,
            players=players,
            order=active_user_ids,
            deck=draw_deck,
            button_user_id=button,
            created_at=created_at,
            updated_at=created_at,
            hand_id=_new_hand_id(created_at),
        )
        hand.post_blinds()
        self.current_hand = hand
        return hand

    def to_public_dict(self) -> dict[str, object]:
        return {
            "version": 1,
            "is_open": self.is_open,
            "button_user_id": self.button_user_id,
            "seat_order": self.seat_order,
            "seats": [self.seats[user_id].public_dict() for user_id in self.seat_order if user_id in self.seats],
        }

    @classmethod
    def from_public_dict(cls, data: dict[str, object]) -> "PokerRoom":
        room = cls()
        room.is_open = bool(data.get("is_open", True))
        raw_button = data.get("button_user_id")
        room.button_user_id = int(raw_button) if raw_button is not None else None
        for raw_seat in data.get("seats", []):
            if not isinstance(raw_seat, dict):
                continue
            seat = Seat.from_dict(raw_seat)
            room.seats[seat.user_id] = seat
            room.seat_order.append(seat.user_id)
        return room

    def _join(self, user_id: int, username: str | None, name: str) -> GameResult:
        if user_id in self.seats:
            seat = self.seats[user_id]
            seat.username = username
            seat.name = name
            seat.sitting_out = False
            seat.leave_next_hand = False
            seat.auto_timeout_count = 0
            return GameResult("room", "Ты снова в игре.")
        if not self.is_open:
            raise PokerRoomError("стол закрыт")
        if not self._has_open_seat_for(user_id):
            raise SeatLimitError("стол заполнен")
        self.seats[user_id] = Seat(user_id=user_id, username=username, name=name)
        self.seat_order.append(user_id)
        if self.button_user_id is None:
            self.button_user_id = user_id
        return GameResult("room", "Место занято.")

    def _require_seat(self, user_id: int) -> Seat:
        try:
            return self.seats[user_id]
        except KeyError as exc:
            raise PokerRoomError("not seated") from exc

    def _next_button(self, active_user_ids: list[int]) -> int:
        if self.button_user_id not in active_user_ids:
            return active_user_ids[0]
        current = active_user_ids.index(self.button_user_id)
        if self.current_hand and self.current_hand.status == STATUS_ENDED:
            return active_user_ids[(current + 1) % len(active_user_ids)]
        return active_user_ids[current]

    def _remove_left_seats(self) -> None:
        keep = []
        for user_id in self.seat_order:
            seat = self.seats.get(user_id)
            if seat is None:
                continue
            if seat.leave_next_hand and (self.current_hand is None or self.current_hand.status == STATUS_ENDED):
                if _is_reserved_seat(user_id):
                    seat.leave_next_hand = False
                    seat.sitting_out = True
                    keep.append(user_id)
                    continue
                del self.seats[user_id]
                continue
            keep.append(user_id)
        self.seat_order = keep

    def _has_open_seat_for(self, user_id: int) -> bool:
        seated_count = sum(1 for seated_user_id in self.seat_order if seated_user_id in self.seats)
        if seated_count >= MAX_SEATS:
            return False
        if _is_reserved_seat(user_id) or reserved_seat_user_id() in self.seats:
            return True
        return seated_count < MAX_SEATS - 1


@dataclass
class PokerHand:
    room: PokerRoom
    players: dict[int, HandPlayer]
    order: list[int]
    deck: list[str]
    button_user_id: int
    created_at: float
    updated_at: float
    hand_id: str
    status: str = STATUS_BETTING
    street: str = STREET_PREFLOP
    board: list[str] = field(default_factory=list)
    current_bet: int = BIG_BLIND
    min_raise: int = BIG_BLIND
    to_act_user_id: int | None = None
    small_blind_user_id: int | None = None
    big_blind_user_id: int | None = None
    river_aggressor_user_id: int | None = None
    public_revealed_user_ids: set[int] = field(default_factory=set)
    mucked_user_ids: set[int] = field(default_factory=set)
    side_pots: list[SidePot] = field(default_factory=list)
    public_log: list[str] = field(default_factory=list)
    final_announced: bool = False

    @property
    def pot(self) -> int:
        return sum(player.committed for player in self.players.values())

    def post_blinds(self) -> None:
        if len(self.order) == 2:
            self.small_blind_user_id = self.button_user_id
            self.big_blind_user_id = self._next_active_after(self.small_blind_user_id)
            self.to_act_user_id = self.small_blind_user_id
        else:
            self.small_blind_user_id = self._next_active_after(self.button_user_id)
            self.big_blind_user_id = self._next_active_after(self.small_blind_user_id)
            self.to_act_user_id = self._next_active_after(self.big_blind_user_id)

        assert self.small_blind_user_id is not None
        assert self.big_blind_user_id is not None
        self._commit_to(self.small_blind_user_id, SMALL_BLIND)
        self._commit_to(self.big_blind_user_id, BIG_BLIND)
        self.current_bet = max(
            self.players[self.small_blind_user_id].street_bet,
            self.players[self.big_blind_user_id].street_bet,
        )
        self.public_log.append(
            f"Блайнды: {self.players[self.small_blind_user_id].name} 50, "
            f"{self.players[self.big_blind_user_id].name} 100"
        )
        self._normalize_to_act_after_forced_commits(self.created_at)

    def private_hand_text(self, user_id: int) -> str:
        player = self.players[user_id]
        return f"Твои карты: {format_hand_plain(player.hand)}"

    def legal_summary(self, user_id: int | None = None) -> dict[str, int | bool | None]:
        actor_id = self.to_act_user_id if user_id is None else user_id
        if actor_id is None or actor_id not in self.players:
            return {}
        actor = self.players[actor_id]
        call_amount = max(0, self.current_bet - actor.street_bet)
        max_total = actor.street_bet + actor.stack
        min_total = self._minimum_raise_total()
        return {
            "can_check": call_amount == 0,
            "call_amount": call_amount,
            "current_bet": self.current_bet,
            "min_raise_to": min_total if actor.stack > call_amount else None,
            "max_raise_to": max_total if max_total > self.current_bet else None,
            "all_in_to": max_total,
        }

    def apply_action(
        self,
        user_id: int,
        action: PlayerAction,
        now: float | None = None,
        *,
        automatic: bool = False,
    ) -> GameResult:
        self._assert_can_act(user_id)
        actor = self.players[user_id]
        call_amount = max(0, self.current_bet - actor.street_bet)
        self.updated_at = _coerce_now(now)

        if action.action == "fold":
            actor.folded = True
            actor.acted = True
            self.public_log.append(f"{actor.name}: фолд")
            if not automatic:
                self._reset_auto_timeout_count(user_id)
            return self._after_action(now)

        if action.action == "check":
            if call_amount:
                raise PokerActionError("check is not legal facing a bet")
            actor.acted = True
            self.public_log.append(f"{actor.name}: чек")
            if not automatic:
                self._reset_auto_timeout_count(user_id)
            return self._after_action(now)

        if action.action == "call":
            if call_amount == 0:
                actor.acted = True
                self.public_log.append(f"{actor.name}: чек")
            else:
                paid = self._commit_to(user_id, actor.street_bet + call_amount)
                actor.acted = True
                self.public_log.append(f"{actor.name}: колл {paid}")
            if not automatic:
                self._reset_auto_timeout_count(user_id)
            return self._after_action(now)

        target = self._target_for_action(actor, action)
        paid = self._commit_to(user_id, target)
        actor.acted = True
        if target > self.current_bet:
            previous_bet = self.current_bet
            raise_size = target - previous_bet
            self.current_bet = target
            if raise_size >= self.min_raise:
                self.min_raise = raise_size
            for player in self.players.values():
                if player.user_id != user_id and not player.folded and not player.all_in:
                    player.acted = False
            if self.street == STREET_RIVER:
                self.river_aggressor_user_id = user_id
            if action.action == "all_in":
                self.public_log.append(f"{actor.name}: олл-ин {target}")
            elif previous_bet == 0:
                self.public_log.append(f"{actor.name}: бет {target}")
            else:
                self.public_log.append(f"{actor.name}: рейз до {target}")
        else:
            self.public_log.append(f"{actor.name}: колл {paid}")
        if not automatic:
            self._reset_auto_timeout_count(user_id)
        return self._after_action(now)

    def apply_timeout(self, now: float | None = None) -> GameResult:
        if self.to_act_user_id is None:
            return GameResult("invalid", "Нет активного хода.")
        actor = self.players[self.to_act_user_id]
        if _coerce_now(now) - self.updated_at < TURN_TIMEOUT_SECONDS:
            return GameResult("waiting", "Время еще не вышло.")
        if self.current_bet <= actor.street_bet:
            result = self.apply_action(actor.user_id, PlayerAction("check"), now=now, automatic=True)
        else:
            result = self.apply_action(actor.user_id, PlayerAction("fold"), now=now, automatic=True)
        self._record_auto_timeout(actor.user_id)
        return result

    def choose_public_reveal(self, user_id: int, reveal: bool) -> GameResult:
        if self.status != STATUS_ENDED:
            return GameResult("invalid", "Раздача еще идет.")
        player = self.players.get(user_id)
        if player is None:
            return GameResult("invalid", "Игрок не в этой раздаче.")
        if player.folded:
            return GameResult("invalid", "Фолд уже не показываем.")
        if user_id not in self.optional_reveal_user_ids():
            return GameResult("already_decided", "Эта рука уже решена.")
        if reveal:
            self.public_revealed_user_ids.add(user_id)
        else:
            self.mucked_user_ids.add(user_id)
        return GameResult("room", "Показали." if reveal else "Сброшено.")

    def optional_reveal_user_ids(self) -> set[int]:
        if self.status != STATUS_ENDED:
            return set()
        eligible = {user_id for user_id, player in self.players.items() if not player.folded}
        decided = self.public_revealed_user_ids | self.mucked_user_ids
        return eligible - decided

    def resolution_summary(self) -> HandResolution:
        """Return the public resolution of a finished hand for chat-side rendering.

        For showdowns this enumerates each side pot, names its winners and the rank class
        of the winning hand. For folded endings the pot has no hand category. Always
        includes per-player chip deltas relative to the stacks at hand start.
        """

        if self.status != STATUS_ENDED:
            raise PokerRoomError("hand not finished")
        stack_deltas: list[tuple[int, int]] = [
            (player.user_id, player.stack - player.initial_stack)
            for player in (self.players[user_id] for user_id in self.order)
        ]
        if self.side_pots:
            pots: list[PotResolution] = []
            multi = len(self.side_pots) > 1
            for index, pot in enumerate(self.side_pots):
                winners = pot.winner_user_ids or self._winners_for(pot.eligible_user_ids)
                names = tuple(self.players[user_id].name for user_id in winners)
                category = self._hand_category_for(winners[0]) if winners else None
                label = self._pot_label(index, multi)
                pots.append(
                    PotResolution(
                        label=label,
                        amount=pot.amount,
                        winner_user_ids=tuple(winners),
                        winner_names=names,
                        hand_category=category,
                    )
                )
            return HandResolution(
                pots=tuple(pots),
                board=tuple(self.board),
                stack_deltas=tuple(stack_deltas),
                showdown=True,
            )
        live = [player for player in self.players.values() if not player.folded]
        winners = [live[0].user_id] if live else []
        names = tuple(self.players[user_id].name for user_id in winners)
        return HandResolution(
            pots=(
                PotResolution(
                    label="Банк",
                    amount=sum(player.committed for player in self.players.values()),
                    winner_user_ids=tuple(winners),
                    winner_names=names,
                    hand_category=None,
                ),
            ),
            board=tuple(self.board),
            stack_deltas=tuple(stack_deltas),
            showdown=False,
        )

    def _pot_label(self, index: int, multi: bool) -> str:
        if not multi:
            return "Банк"
        if index == 0:
            return "Основной банк"
        return f"Сайд-пот {index}"

    def _hand_category_for(self, user_id: int) -> str | None:
        player = self.players.get(user_id)
        if player is None or len(self.board) < 5:
            return None
        try:
            score = _EVALUATOR.evaluate([Card.new(c) for c in player.hand], [Card.new(c) for c in self.board])
            rank_class = _EVALUATOR.get_rank_class(score)
            label = _EVALUATOR.class_to_string(rank_class)
        except Exception:
            return None
        return HAND_CATEGORY_RU.get(label, label)

    def public_snapshot(self) -> dict[str, object]:
        return {
            "status": self.status,
            "street": self.street,
            "board": list(self.board),
            "pot": self.pot,
            "to_act_user_id": self.to_act_user_id,
            "button_user_id": self.button_user_id,
            "small_blind_user_id": self.small_blind_user_id,
            "big_blind_user_id": self.big_blind_user_id,
            "players": [
                {
                    "user_id": user_id,
                    "name": self.players[user_id].name,
                    "stack": self.players[user_id].stack,
                    "committed": self.players[user_id].committed,
                    "street_bet": self.players[user_id].street_bet,
                    "folded": self.players[user_id].folded,
                    "all_in": self.players[user_id].all_in,
                    "public_revealed": user_id in self.public_revealed_user_ids,
                    "mucked": user_id in self.mucked_user_ids,
                    "hand": list(self.players[user_id].hand) if user_id in self.public_revealed_user_ids else None,
                }
                for user_id in self.order
            ],
        }

    def _target_for_action(self, actor: HandPlayer, action: PlayerAction) -> int:
        if action.action == "all_in":
            return actor.street_bet + actor.stack
        if action.amount is None:
            raise PokerActionError("amount required")
        amount = int(action.amount)
        if amount <= 0:
            raise PokerActionError("amount must be positive")
        if action.action == "bet":
            if self.current_bet != 0:
                raise PokerActionError("bet is not legal facing a bet")
            target = amount
        elif action.action == "raise_to":
            target = amount
        elif action.action == "raise_by":
            target = self.current_bet + amount
        elif action.action == "raise_ambiguous":
            candidates = [amount, self.current_bet + amount]
            legal = [candidate for candidate in candidates if self._target_is_legal_raise(actor, candidate)]
            if not legal:
                raise PokerActionError("raise amount is not legal")
            target = min(legal)
        else:
            raise PokerActionError("unknown action")
        if not self._target_is_legal_raise(actor, target):
            raise PokerActionError("target is not legal")
        return target

    def _target_is_legal_raise(self, actor: HandPlayer, target: int) -> bool:
        max_total = actor.street_bet + actor.stack
        if target <= self.current_bet:
            return False
        if target > max_total:
            return False
        min_total = self._minimum_raise_total()
        if target >= min_total:
            return True
        return target == max_total

    def _minimum_raise_total(self) -> int:
        if self.current_bet == 0:
            return BIG_BLIND
        return self.current_bet + self.min_raise

    def _after_action(self, now: float | None) -> GameResult:
        if self._live_user_ids_count() == 1:
            self._award_folded_pot(now)
            return GameResult("ended", "Банк забран без вскрытия.")
        if self._betting_closed_by_all_in():
            self._runout_to_showdown(now)
            return GameResult("showdown", "Олл-ин закрыт.", new_cards=True)
        if self._round_complete():
            return self._advance_street(now)
        self.to_act_user_id = self._next_to_act_after(self.to_act_user_id)
        return GameResult("acted", "Принято.")

    def _round_complete(self) -> bool:
        for player in self.players.values():
            if player.folded or player.all_in:
                continue
            if player.street_bet != self.current_bet:
                return False
            if not player.acted:
                return False
        return True

    def _advance_street(self, now: float | None) -> GameResult:
        if self.street == STREET_PREFLOP:
            self.board.extend(self._draw(3))
            self.street = STREET_FLOP
            self.public_log.append(f"Флоп: {' '.join(self.board)}")
            self._reset_street(now)
            return GameResult("advanced", "Флоп.", new_cards=True)
        if self.street == STREET_FLOP:
            self.board.extend(self._draw(1))
            self.street = STREET_TURN
            self.public_log.append(f"Терн: {self.board[-1]}")
            self._reset_street(now)
            return GameResult("advanced", "Терн.", new_cards=True)
        if self.street == STREET_TURN:
            self.board.extend(self._draw(1))
            self.street = STREET_RIVER
            self.river_aggressor_user_id = None
            self.public_log.append(f"Ривер: {self.board[-1]}")
            self._reset_street(now)
            return GameResult("advanced", "Ривер.", new_cards=True)
        self._showdown(now)
        return GameResult("showdown", "Вскрытие.")

    def _reset_street(self, now: float | None) -> None:
        self.updated_at = _coerce_now(now)
        self.current_bet = 0
        self.min_raise = BIG_BLIND
        for player in self.players.values():
            player.street_bet = 0
            player.acted = False
        self.to_act_user_id = self._first_postflop_actor()
        if self.to_act_user_id is None:
            self._runout_to_showdown(now)

    def _runout_to_showdown(self, now: float | None) -> None:
        while len(self.board) < 5:
            if len(self.board) == 0:
                self.board.extend(self._draw(3))
                self.public_log.append(f"Флоп: {' '.join(self.board)}")
            elif len(self.board) == 3:
                self.board.extend(self._draw(1))
                self.public_log.append(f"Терн: {self.board[-1]}")
            else:
                self.board.extend(self._draw(1))
                self.public_log.append(f"Ривер: {self.board[-1]}")
        self._showdown(now)

    def _showdown(self, now: float | None) -> None:
        self.side_pots = self._build_side_pots()
        for pot in self.side_pots:
            winners = self._winners_for(pot.eligible_user_ids)
            pot.winner_user_ids = self._order_winners_by_seat(winners)
            share, odd = divmod(pot.amount, len(pot.winner_user_ids))
            for index, user_id in enumerate(pot.winner_user_ids):
                payout = share + (1 if index < odd else 0)
                self._pay_to_seat(user_id, payout)
                self.public_revealed_user_ids.add(user_id)
        if self.river_aggressor_user_id in self.players and not self.players[self.river_aggressor_user_id].folded:
            self.public_revealed_user_ids.add(self.river_aggressor_user_id)
        self.status = STATUS_ENDED
        self.street = STREET_SHOWDOWN
        self.to_act_user_id = None
        self.updated_at = _coerce_now(now)
        self._mark_busted_players_sitting_out()

    def _award_folded_pot(self, now: float | None) -> None:
        live = [player for player in self.players.values() if not player.folded]
        if not live:
            return
        self._pay_to_seat(live[0].user_id, self.pot)
        self.status = STATUS_ENDED
        self.to_act_user_id = None
        self.updated_at = _coerce_now(now)
        self._mark_busted_players_sitting_out()

    def _build_side_pots(self) -> list[SidePot]:
        levels = sorted({player.committed for player in self.players.values() if player.committed > 0})
        previous = 0
        pots: list[SidePot] = []
        for level in levels:
            contributors = [player for player in self.players.values() if player.committed >= level]
            amount = (level - previous) * len(contributors)
            eligible = [player.user_id for player in contributors if not player.folded]
            if amount > 0 and eligible:
                pots.append(SidePot(amount=amount, eligible_user_ids=eligible))
            previous = level
        return pots

    def _winners_for(self, user_ids: list[int]) -> list[int]:
        ranked = [(user_id, _evaluate(self.players[user_id].hand, self.board)) for user_id in user_ids]
        best = min(score for _, score in ranked)
        return [user_id for user_id, score in ranked if score == best]

    def _order_winners_by_seat(self, winners: list[int]) -> list[int]:
        """Sort winners by position starting from the seat after the button.

        This gives the leftover odd chip a stable, traditional resolution
        (first-after-the-button rule) instead of relying on dict iteration order.
        """

        if not winners or self.button_user_id is None or self.button_user_id not in self.order:
            return sorted(winners, key=self.order.index)
        button_index = self.order.index(self.button_user_id)
        rotated = [self.order[(button_index + 1 + offset) % len(self.order)] for offset in range(len(self.order))]
        winner_set = set(winners)
        return [user_id for user_id in rotated if user_id in winner_set]

    def _pay_to_seat(self, user_id: int, amount: int) -> None:
        self.players[user_id].stack += amount
        self.room.seats[user_id].stack += amount

    def _commit_to(self, user_id: int, target: int) -> int:
        player = self.players[user_id]
        need = max(0, target - player.street_bet)
        paid = min(need, player.stack)
        player.stack -= paid
        player.street_bet += paid
        player.committed += paid
        if player.stack == 0:
            player.all_in = True
        self.room.seats[user_id].stack -= paid
        return paid

    def _assert_can_act(self, user_id: int) -> None:
        if self.status != STATUS_BETTING:
            raise PokerActionError("hand is not betting")
        if user_id != self.to_act_user_id:
            raise PokerActionError("not your turn")
        player = self.players[user_id]
        if player.folded or player.all_in:
            raise PokerActionError("player cannot act")

    def _draw(self, count: int) -> list[str]:
        cards = self.deck[:count]
        del self.deck[:count]
        return cards

    def _live_user_ids_count(self) -> int:
        return sum(1 for player in self.players.values() if not player.folded)

    def _betting_closed_by_all_in(self) -> bool:
        live = [player for player in self.players.values() if not player.folded]
        if len(live) <= 1:
            return False
        live_with_chips = [player for player in live if not player.all_in]
        if len(live_with_chips) > 1:
            return False
        return all(player.all_in or player.street_bet == self.current_bet for player in live)

    def _first_postflop_actor(self) -> int | None:
        return self._next_to_act_after(self.button_user_id, include_start=False)

    def _next_to_act_after(self, user_id: int | None, include_start: bool = False) -> int | None:
        if user_id is None:
            return None
        if user_id not in self.order:
            start = 0
        else:
            start = self.order.index(user_id) + (0 if include_start else 1)
        for offset in range(len(self.order)):
            candidate = self.order[(start + offset) % len(self.order)]
            player = self.players[candidate]
            if not player.folded and not player.all_in:
                return candidate
        return None

    def _next_active_after(self, user_id: int | None) -> int:
        if user_id not in self.order:
            return self.order[0]
        index = self.order.index(user_id)
        return self.order[(index + 1) % len(self.order)]

    def _mark_busted_players_sitting_out(self) -> None:
        for user_id in self.order:
            seat = self.room.seats[user_id]
            if seat.stack <= 0:
                seat.stack = 0
                seat.sitting_out = True

    def _record_auto_timeout(self, user_id: int) -> None:
        seat = self.room.seats.get(user_id)
        if seat is None:
            return
        seat.auto_timeout_count += 1
        if seat.auto_timeout_count >= AUTO_SIT_OUT_TIMEOUTS:
            seat.sitting_out = True
            self.public_log.append(f"{seat.name}: ситаут после автоходов")

    def _reset_auto_timeout_count(self, user_id: int) -> None:
        seat = self.room.seats.get(user_id)
        if seat is not None:
            seat.auto_timeout_count = 0

    def _normalize_to_act_after_forced_commits(self, now: float | None = None) -> None:
        actor_id = self.to_act_user_id
        if actor_id is None:
            return
        actor = self.players[actor_id]
        if not actor.folded and not actor.all_in:
            return
        self.to_act_user_id = self._next_to_act_after(actor_id)
        if self.to_act_user_id is not None:
            return
        if self._live_user_ids_count() <= 1:
            self._award_folded_pot(now)
            return
        self._runout_to_showdown(now)


class JsonRoomStore:
    """Atomic JSON persistence for public room state only."""

    def __init__(self, path: str | os.PathLike[str]) -> None:
        self.path = Path(path)

    def save(self, room: PokerRoom) -> None:
        self.save_public_dict(room.to_public_dict())

    def save_public_dict(self, data: dict[str, object]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True)
        if self.path.exists():
            backup_path = self.path.with_suffix(self.path.suffix + ".bak")
            try:
                backup_path.write_bytes(self.path.read_bytes())
            except OSError:
                pass
        with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=self.path.parent, delete=False) as tmp:
            tmp.write(payload)
            tmp.write("\n")
            tmp_path = Path(tmp.name)
        tmp_path.replace(self.path)

    def load(self) -> PokerRoom:
        if not self.path.exists():
            return PokerRoom()
        data = json.loads(self.path.read_text(encoding="utf-8"))
        return PokerRoom.from_public_dict(data)


def stacked_deck(spec: dict[int | str, tuple[str, ...]]) -> list[str]:
    ordered: list[str] = []
    for key in spec:
        if isinstance(key, int):
            ordered.extend(spec[key])
    ordered.extend(spec.get("board", ()))
    seen = set(ordered)
    if len(seen) != len(ordered):
        raise ValueError("stacked deck contains duplicate cards")
    ordered.extend(card for card in full_deck() if card not in seen)
    return ordered


def _evaluate(hand: tuple[str, str], board: list[str]) -> int:
    return _EVALUATOR.evaluate([Card.new(card) for card in hand], [Card.new(card) for card in board])


def _shuffled_deck() -> list[str]:
    deck = full_deck()
    random.shuffle(deck)
    return deck


def _new_hand_id(now: float | None = None) -> str:
    millis = int(_coerce_now(now) * 1000)
    return f"h{millis:x}{random.getrandbits(16):04x}"


def _coerce_now(now: float | None) -> float:
    return time.time() if now is None else now


def _date_key(now: float | None = None) -> str:
    return time.strftime("%Y-%m-%d", time.localtime(_coerce_now(now)))


def reserved_seat_user_id() -> int:
    raw = os.environ.get(RESERVED_SEAT_USER_ID_ENV)
    if not raw:
        return RESERVED_SEAT_USER_ID
    try:
        return int(raw)
    except ValueError:
        return RESERVED_SEAT_USER_ID


def _is_reserved_seat(user_id: int) -> bool:
    return user_id == reserved_seat_user_id()
