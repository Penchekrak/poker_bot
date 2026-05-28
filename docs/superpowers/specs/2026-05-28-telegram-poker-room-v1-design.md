# Telegram Poker Room V1 Design

## Summary

Build a single no-limit Hold'em cash-table room for one configured Telegram group, optionally narrowed to one forum topic. The room supports 2-10 seated players, chat-based betting parsed by an OpenAI-compatible JSON-only LLM, private hole-card callback alerts, timed turn auto-actions, persistent public stacks/seats, and an 8-bit round-table public render.

The existing `/heads_up`, `/blackjack`, and `/aces_please` features stay unchanged.

## Configuration

- `POKER_ROOM_CHAT_ID`: required chat id for the room.
- `POKER_ROOM_THREAD_ID`: optional forum topic id for the room; omit for chat-wide gating.
- `POKER_ADMIN_USER_IDS`: comma-separated user ids allowed to reset/open/close the room.
- `POKER_STATE_PATH`: optional, defaults to `data/poker_room_state.json`.
- `LLM_BASE_URL`, `LLM_API_KEY`, `LLM_MODEL`: optional OpenAI-compatible JSON parser/commentary endpoint settings.

## State And Privacy

Persist only room/accounting state: seats, stacks, active/sit-out flags, button position, last rebuy date, open/closed flag. Do not persist deck, board, hole cards, current betting state, or private messages.

If the bot restarts mid-hand, the active hand is lost by design; the next hand starts from persisted stacks and seats.

## Game Behavior

- Defaults: stack 10,000, blinds 50/100, turn timer 120 seconds, auto-deal delay 15 seconds.
- Players join/rebuy/leave/sit out by natural text intents with confirmation callbacks.
- Join is rejected after 10 seats. No waitlist.
- Bust players auto sit out. Rebuy restores 10,000 once per local calendar day.
- New hands auto-start when at least two active players with chips remain.
- Betting is full no-limit Hold'em with multiway side pots.
- Only the current actor's message can mutate betting state. Non-actor table talk may get rare non-mutating dealer asides.
- Ambiguous `raise 500` is interpreted as the lesser legal meaning.
- Turn timeout auto-checks when no chips are owed, otherwise auto-folds.
- Showdown reveals pot winners and required river aggressors; other hands can voluntarily show or muck. Folded hands never auto-reveal.

## Rendering

Use Pillow to render a fixed-size 8-bit public table PNG. Vendor selected PettingZoo/RLCard assets under `assets/poker_table/`: cards, card back, chips, and `Minecraft.ttf`. Include attribution/NOTICE.

The public image shows only public state: board, pot, street, current actor, seats, stacks, street commitments, button/blinds, all-in/folded/sit-out state, and card backs for private hands.

Routine actions edit the current photo/caption. New community cards create a new photo message.

## LLM Safety

The LLM receives only public state, legal actions, the current actor's text, and capped recent public snippets. It never receives hole cards or deck order. Player text is quoted as untrusted content. LLM output is schema-validated and then validated by the deterministic engine before any mutation.

If the endpoint fails or returns invalid JSON, deterministic commands still work: `check`, `call`, `fold`, `all in`, `bet N`, `raise to N`, `raise by N`.
