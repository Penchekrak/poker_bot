# Poker Telegram Bot

A Telegram group bot for quick poker hand prompts and a lightweight heads-up
Texas Hold'em mini-game.

## Features

- `/aces_please` deals a random hand and shows cached preflop equity.
- `/heads_up @username` starts a two-player in-chat Hold'em table.
- Mentioning the bot in a group triggers the same random-hand response.

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

## Tests

```bash
python -m unittest discover -s tests
```

## Repository Scope

This public repository contains only the runtime bot code, tests, and CI. Local
deployment scripts, generated artifacts, cache-building tools, and private
environment details are intentionally kept out of Git.
