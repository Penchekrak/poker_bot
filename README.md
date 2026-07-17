# Poker Telegram Bot

A Telegram group bot for quick poker hand prompts, a dedicated poker room, and
lightweight group mini-games.

## Features

- `/aces_please` deals a random hand and shows cached preflop equity.
- `/heads_up @username` starts a two-player in-chat Hold'em table.
- `/blackjack` starts a one-player blackjack table. Each chat can have one active blackjack table.
- `/liars_bar` (or `/liars`) starts the Russian-language Liar's Bar card mini-game.
- Mentioning the bot in a group triggers the same random-hand response.
- A chat-scoped poker room can be enabled with `POKER_ROOM_CHAT_ID`; set
  `POKER_ROOM_THREAD_ID` only when it should be limited to one forum topic.
  Admins open it with `/poker`. Players join/rebuy/sit out via free-form chat
  messages, view private cards via callback alerts, and act in chat.

## Requirements

- Python 3.10+
- A Telegram bot token from BotFather

## Local Setup

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
export BOT_TOKEN=123456:replace-with-your-token
./start.sh
```

The bot reads `BOT_TOKEN` from the environment. By default it uses long polling.
For webhook mode, set either `WEBHOOK_URL` or `WEBHOOK_PUBLIC_IP`; the latter
builds `https://<ip>:<WEBHOOK_PORT>/<WEBHOOK_PATH>` and also passes that IP to
Telegram. Optional webhook settings are `WEBHOOK_LISTEN`, `WEBHOOK_PORT`,
`WEBHOOK_PATH`, `WEBHOOK_IP_ADDRESS`, `WEBHOOK_CERT`, `WEBHOOK_KEY`,
`WEBHOOK_SECRET_TOKEN`, and `WEBHOOK_DROP_PENDING_UPDATES=1`. For local testing,
you can also create an untracked `.env` file with these values.
The poker room additionally reads `POKER_ROOM_CHAT_ID`, optional
`POKER_ROOM_THREAD_ID`, `POKER_ADMIN_USER_IDS`, `POKER_RESERVED_SEAT_USER_ID`,
`POKER_STATE_PATH`, `BOT_LOG_PATH`, `LLM_BASE_URL`, `LLM_API_KEY`, and
`LLM_MODEL` when that feature is enabled. Logs default to `bot.log`; inspect a deployment with
`tail -f bot.log`.

All features use the same `BOT_TOKEN` and bot process. Liar's Bar is available
only inside the configured dedicated poker chat/topic. The other regular
mini-games remain available only outside it.

## Tests

```bash
python -m unittest discover -s tests
```

## Repository Scope

This public repository contains only the runtime bot code, tests, and CI. Local
deployment scripts, generated artifacts, cache-building tools, and private
environment details are intentionally kept out of Git.
