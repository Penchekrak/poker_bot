"""Preflop win/tie rates vs random opponents — file cache lookup only (no runtime simulation)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Final

from cards import RANKS, canonical_pair

CACHE_FILE: Final[Path] = Path(__file__).resolve().parent / "equity_cache.json"

_FILE_CACHE: dict[str, tuple[float, float, float, float]] | None = None


def abstract_key(canon: tuple[str, str]) -> tuple:
    """
    Suits are symmetric: only rank identity and (pair | suited | offsuit) matter
    for equity vs random opponents.
    """
    a, b = canon
    ia, sa = RANKS.index(a[0]), a[1]
    ib, sb = RANKS.index(b[0]), b[1]
    if ia == ib:
        return ("p", ia)
    hi, lo = (ia, ib) if ia > ib else (ib, ia)
    return ("h", hi, lo, sa == sb)


def encode_abstract(abstract: tuple) -> str:
    kind = abstract[0]
    if kind == "p":
        return f"p:{abstract[1]}"
    if kind == "h":
        _, hi, lo, su = abstract
        return f"h:{hi}:{lo}:{1 if su else 0}"
    raise ValueError(f"bad key {abstract!r}")


def abstract_to_shorthand(abstract: tuple) -> str:
    """PokerStove-style label used in 2p.json / 3p.json (higher rank first)."""
    kind = abstract[0]
    if kind == "p":
        r = RANKS[abstract[1]]
        return f"{r}{r}"
    if kind == "h":
        _, hi, lo, su = abstract
        return f"{RANKS[hi]}{RANKS[lo]}{'s' if su else 'o'}"
    raise ValueError(f"bad key {abstract!r}")


def parse_cache_entry(raw: object) -> tuple[float, float, float, float]:
    """Normalize equity_cache.json value: [hu_win, hu_tie, 3w_win, 3w_tie] or legacy [hu_eq, 3w_eq]."""
    if not isinstance(raw, (list, tuple)):
        raise TypeError(f"cache value must be a list, not {type(raw).__name__}")
    seq = list(raw)
    if len(seq) >= 4:
        return (float(seq[0]), float(seq[1]), float(seq[2]), float(seq[3]))
    if len(seq) == 2:
        return (float(seq[0]), 0.0, float(seq[1]), 0.0)
    raise ValueError(f"cache entry must have 2 or 4 numbers, got {len(seq)}")


def all_abstract_keys() -> list[tuple]:
    """All 13 pairs + 78×2 (suited/offsuit) = 169 canonical preflop shapes."""
    keys: list[tuple] = [("p", i) for i in range(13)]
    for hi in range(1, 13):
        for lo in range(0, hi):
            for su in (False, True):
                keys.append(("h", hi, lo, su))
    return keys


def representative_hole(key: tuple) -> tuple[str, str]:
    """
    A fixed isomorphic two-card hand for a given abstract key.

    Convention (matches brute-force cache generation): pairs use spades + hearts;
    non-pair suited uses both hearts; offsuit uses heart on the higher rank and
    spades on the lower rank.
    """
    kind = key[0]
    if kind == "p":
        r = RANKS[key[1]]
        return canonical_pair(f"{r}s", f"{r}h")
    if kind == "h":
        _, hi, lo, su = key
        rh, rlo = RANKS[hi], RANKS[lo]
        if su:
            return canonical_pair(f"{rh}h", f"{rlo}h")
        return canonical_pair(f"{rh}h", f"{rlo}s")
    raise ValueError(f"bad key {key!r}")


def _load_cache_file() -> dict[str, tuple[float, float, float, float]]:
    if not CACHE_FILE.is_file():
        return {}
    with open(CACHE_FILE, encoding="utf-8") as f:
        raw = json.load(f)
    values = raw.get("values", {})
    return {k: parse_cache_entry(v) for k, v in values.items()}


def _get_file_cache() -> dict[str, tuple[float, float, float, float]]:
    global _FILE_CACHE
    if _FILE_CACHE is None:
        _FILE_CACHE = _load_cache_file()
    return _FILE_CACHE


def read_cached_equity(
    canon: tuple[str, str],
) -> tuple[float, float, float, float] | None:
    """Lookup from equity_cache.json: (hu_win, hu_tie, three_win, three_tie). None if missing."""
    key = encode_abstract(abstract_key(canon))
    return _get_file_cache().get(key)
