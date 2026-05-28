"""Run the bot with Telegram long polling."""

from __future__ import annotations

import logging
import os
import sys

from telegram import Update
from telegram.ext import Application, CallbackQueryHandler, CommandHandler, MessageHandler, filters

from blackjack import blackjack_callback, blackjack_command
from handlers import aces_command, on_mention
from heads_up import heads_up_callback, heads_up_command
from poker_room_handlers import poker_room_callback, poker_room_message, room_callback_pattern

logging.basicConfig(
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
    level=logging.INFO,
)
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)
log = logging.getLogger(__name__)

CHAT = filters.ChatType.GROUPS


def main() -> None:
    token = os.environ.get("BOT_TOKEN")
    if not token:
        log.error("Set BOT_TOKEN in the environment.")
        sys.exit(1)

    app = (
        Application.builder()
        .token(token)
        .build()
    )

    app.add_handler(CommandHandler("aces_please", aces_command, filters=CHAT))
    app.add_handler(CommandHandler("heads_up", heads_up_command, filters=CHAT))
    app.add_handler(CommandHandler("blackjack", blackjack_command, filters=CHAT))
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
