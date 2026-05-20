"""Deck helpers, canonical keys, and HTML display for hole cards."""

from __future__ import annotations

import random
from typing import Final

RANKS: Final = "23456789TJQKA"
SUITS: Final = "shdc"  # spades, hearts, diamonds, clubs (treys letters)

SUIT_UNICODE: Final[dict[str, str]] = {
    "s": "\N{BLACK SPADE SUIT}",
    "h": "\N{BLACK HEART SUIT}",
    "d": "\N{BLACK DIAMOND SUIT}",
    "c": "\N{BLACK CLUB SUIT}",
}


def full_deck() -> list[str]:
    return [f"{r}{s}" for r in RANKS for s in SUITS]


def deal_random_hand() -> tuple[str, str]:
    deck = full_deck()
    random.shuffle(deck)
    a, b = deck[0], deck[1]
    return canonical_pair(a, b)


def canonical_pair(c1: str, c2: str) -> tuple[str, str]:
    def sort_key(card: str) -> tuple[int, str]:
        rank, suit = card[0], card[1]
        return (RANKS.index(rank), suit)

    return tuple(sorted((c1, c2), key=sort_key))


def card_to_treys(card: str) -> str:
    """Our storage matches treys string format (e.g. 'As', 'Td')."""
    return card


def format_card_html(card: str) -> str:
    rank, suit = card[0], card[1]
    sym = SUIT_UNICODE[suit]
    return f"<b>{rank}</b>{sym}"


def format_hand_html(hand: tuple[str, str]) -> str:
    c1, c2 = hand
    sep = " \u00b7 "
    return f"{format_card_html(c1)}{sep}{format_card_html(c2)}"


def format_card_plain(card: str) -> str:
    rank, suit = card[0], card[1]
    return f"{rank}{SUIT_UNICODE[suit]}"


def format_hand_plain(hand: tuple[str, str]) -> str:
    c1, c2 = hand
    return f"{format_card_plain(c1)} \u00b7 {format_card_plain(c2)}"
