# Poker Telegram Bot

A Telegram group bot for quick poker hand prompts and a lightweight heads-up
Texas Hold'em mini-game.

## Features

- `/aces_please` deals a random hand and shows cached preflop equity.
- `/heads_up @username` starts a two-player in-chat Hold'em table.
- `/blackjack` starts a one-player blackjack table. Each chat can have one active blackjack table.
- Mentioning the bot in a group triggers the same random-hand response.
- A forum-scoped chat poker room can be enabled with `POKER_ROOM_CHAT_ID` and
  `POKER_ROOM_THREAD_ID`. Players join/rebuy/sit out via topic messages, view
  private cards via callback alerts, and act in chat. Configured poker admins
  can use `открыть стол`, `закрыть стол`, and `сбросить стол` inside that topic.

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

The bot uses long polling and reads `BOT_TOKEN` from the environment. For local
testing, you can also create an untracked `.env` file with `BOT_TOKEN=...`.
The poker room additionally reads `POKER_ROOM_CHAT_ID`, `POKER_ROOM_THREAD_ID`,
`POKER_ADMIN_USER_IDS`, `POKER_STATE_PATH`, `LLM_BASE_URL`, `LLM_API_KEY`, and
`LLM_MODEL` when that feature is enabled.

## Tests

```bash
python -m unittest discover -s tests
```

## Repository Scope

This public repository contains only the runtime bot code, tests, and CI. Local
deployment scripts, generated artifacts, cache-building tools, and private
environment details are intentionally kept out of Git.
