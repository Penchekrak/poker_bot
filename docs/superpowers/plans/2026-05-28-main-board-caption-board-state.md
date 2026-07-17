# Main Board Caption Board State Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a written current-board line to the Telegram poker room's main board message caption.

**Architecture:** Keep the change in `poker_room_handlers.py`, where main board captions are already composed. Add a tiny board caption formatter that uses `cards.format_card_html` for community cards and an italic empty state before the flop.

**Tech Stack:** Python 3, `unittest`, python-telegram-bot HTML captions, existing `cards.py` formatting helpers.

---

### Task 1: Main Board Caption Board Line

**Files:**
- Modify: `tests/test_poker_room_handlers.py`
- Modify: `poker_room_handlers.py`

- [x] **Step 1: Write the failing caption tests**

Add these tests inside `PokerRoomHandlerTests` in `tests/test_poker_room_handlers.py`:

```python
    async def test_main_board_caption_writes_empty_board_state_preflop(self) -> None:
        context = FakeContext(self.config)
        room = poker_room_handlers.get_room(context)
        room.confirm_room_intent(1, "alice", "Alice", poker_room.ROOM_JOIN)
        room.confirm_room_intent(2, "bob", "Bob", poker_room.ROOM_JOIN)
        hand = room.start_hand(now=1_000.0)

        caption = poker_room_handlers._caption(hand)

        self.assertIn("Доска: <i>пока пусто</i>", caption)

    async def test_main_board_caption_writes_current_board_cards(self) -> None:
        context = FakeContext(self.config)
        room = poker_room_handlers.get_room(context)
        room.confirm_room_intent(1, "alice", "Alice", poker_room.ROOM_JOIN)
        room.confirm_room_intent(2, "bob", "Bob", poker_room.ROOM_JOIN)
        hand = room.start_hand(
            now=1_000.0,
            deck=poker_room.stacked_deck(
                {
                    1: ("Ah", "Ad"),
                    2: ("Kh", "Kd"),
                    "board": ("2c", "7d", "9h", "Ts", "3c"),
                }
            ),
        )
        hand.apply_action(hand.to_act_user_id, poker_room.PlayerAction("call"), now=1_001.0)
        hand.apply_action(hand.to_act_user_id, poker_room.PlayerAction("check"), now=1_002.0)

        caption = poker_room_handlers._caption(hand)

        self.assertIn("Доска: <b>2</b>♣ <b>7</b>♦ <b>9</b>♥", caption)
```

- [x] **Step 2: Run the new tests and verify they fail for the missing caption line**

Run:

```bash
rtk .venv/bin/python -m unittest tests.test_poker_room_handlers.PokerRoomHandlerTests.test_main_board_caption_writes_empty_board_state_preflop tests.test_poker_room_handlers.PokerRoomHandlerTests.test_main_board_caption_writes_current_board_cards
```

Expected result: both tests fail with assertions that `Доска:` is not found in the caption.

- [x] **Step 3: Implement the caption board line**

In `poker_room_handlers.py`, import `format_card_html`:

```python
from cards import format_card_html
```

Add this helper near `_caption`:

```python
def _board_caption_html(hand: poker_room.PokerHand) -> str:
    if not hand.board:
        return "<i>пока пусто</i>"
    return " ".join(format_card_html(card) for card in hand.board)
```

Update `_caption` so the initial lines include the board:

```python
    lines = [
        "<b>Покерный стол</b>",
        f"Банк: <b>{hand.pot}</b>",
        f"Улица: <b>{html.escape(hand.street)}</b>",
        f"Доска: {_board_caption_html(hand)}",
    ]
```

- [x] **Step 4: Run the focused tests and verify they pass**

Run:

```bash
rtk .venv/bin/python -m unittest tests.test_poker_room_handlers.PokerRoomHandlerTests.test_main_board_caption_writes_empty_board_state_preflop tests.test_poker_room_handlers.PokerRoomHandlerTests.test_main_board_caption_writes_current_board_cards
```

Expected result: both tests pass.

- [x] **Step 5: Run the handler test file**

Run:

```bash
rtk .venv/bin/python -m unittest tests.test_poker_room_handlers
```

Expected result: all handler tests pass.

- [x] **Step 6: Run the full project tests**

Run:

```bash
rtk .venv/bin/python -m unittest discover -s tests
```

Expected result: all tests pass.
