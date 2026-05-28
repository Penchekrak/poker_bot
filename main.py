"""Run the bot with Telegram long polling."""

from __future__ import annotations

import logging
import os
import sys
from pathlib import Path

from telegram import Update
from telegram.ext import Application, CallbackQueryHandler, CommandHandler, MessageHandler, filters

from blackjack import blackjack_callback, blackjack_command
from handlers import aces_command, on_mention
from heads_up import heads_up_callback, heads_up_command
from poker_room_handlers import poker_room_callback, poker_room_command, poker_room_message, room_callback_pattern

log = logging.getLogger(__name__)

CHAT = filters.ChatType.GROUPS
LOG_FORMAT = "%(asctime)s %(levelname)s %(name)s %(message)s"


def configure_logging() -> None:
    log_path = Path(os.environ.get("BOT_LOG_PATH", "bot.log"))
    log_path.parent.mkdir(parents=True, exist_ok=True)
    handlers: list[logging.Handler] = [
        logging.StreamHandler(),
        logging.FileHandler(log_path, encoding="utf-8"),
    ]
    logging.basicConfig(format=LOG_FORMAT, level=logging.INFO, handlers=handlers, force=True)
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)


def main() -> None:
    configure_logging()
    token = os.environ.get("BOT_TOKEN")
    if not token:
        log.error("Set BOT_TOKEN in the environment.")
        sys.exit(1)
    log.info(
        "Runtime config: poker_chat=%s poker_thread=%s poker_admins=%s llm=%s log_path=%s",
        os.environ.get("POKER_ROOM_CHAT_ID") or "unset",
        "set" if os.environ.get("POKER_ROOM_THREAD_ID") else "unset",
        "set" if os.environ.get("POKER_ADMIN_USER_IDS") else "unset",
        "set" if os.environ.get("LLM_BASE_URL") and os.environ.get("LLM_MODEL") else "unset",
        os.environ.get("BOT_LOG_PATH", "bot.log"),
    )

    app = (
        Application.builder()
        .token(token)
        .build()
    )

    app.add_handler(CommandHandler("aces_please", aces_command, filters=CHAT))
    app.add_handler(CommandHandler("heads_up", heads_up_command, filters=CHAT))
    app.add_handler(CommandHandler("blackjack", blackjack_command, filters=CHAT))
    app.add_handler(CommandHandler("poker", poker_room_command, filters=CHAT))
    app.add_handler(CallbackQueryHandler(heads_up_callback, pattern=r"^hu:"))
    app.add_handler(CallbackQueryHandler(blackjack_callback, pattern=r"^bj:"))
    app.add_handler(CallbackQueryHandler(poker_room_callback, pattern=room_callback_pattern()))
    app.add_handler(
        MessageHandler(
            CHAT & filters.TEXT & ~filters.COMMAND,
            poker_room_message,
        ),
        group=-1,
    )
    app.add_handler(
        MessageHandler(
            CHAT & filters.TEXT & ~filters.COMMAND,
            on_mention,
        )
    )

    log.info("Starting long polling…")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
