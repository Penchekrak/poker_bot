"""Telegram update handlers."""

from __future__ import annotations

from telegram import MessageEntity, Update
from telegram.ext import ContextTypes

from cards import deal_random_hand, format_hand_html
from equity import read_cached_equity
from heads_up import aces_busy_message, cleanup_expired_and_edit
from hints import hints_for_hand


def _bot_mentioned_text(
    message,
    bot_username: str | None,
    bot_id: int | None,
) -> bool:
    text = message.text
    if not text:
        return False
    entities = message.entities or []
    uname = (bot_username or "").lower()
    needle = f"@{uname}" if uname else ""

    for ent in entities:
        if ent.type == MessageEntity.MENTION and needle:
            frag = text[ent.offset : ent.offset + ent.length]
            if frag.lower() == needle:
                return True
        if ent.type == MessageEntity.TEXT_MENTION and ent.user and bot_id:
            if ent.user.id == bot_id:
                return True

    if uname and needle in text.lower():
        return True
    return False


def _opening_line(hand: tuple[str, str]) -> str:
    n_aces = sum(1 for c in hand if c[0] == "A")
    if n_aces == 2:
        return "<b>Тузы? Это пик твоей покерной карьеры</b> \U0001f0a1\U0001f0b1"
    if n_aces == 1:
        return "<b>Ладно держи одного, положи его в задний карман и будет карманная пара</b> \U0001f0cf"
    return "<b>Сегодня ты без туза</b> \U0001f0cf"


def _format_message(
    hand: tuple[str, str],
    hu_win: float,
    hu_tie: float,
    three_win: float,
    three_tie: float,
) -> str:
    lines = [
        _opening_line(hand),
        "",
        f"Тебе сдали {format_hand_html(hand)}",
        "",
        f"Хедз-ап — победа: <b>{hu_win * 100:.1f}%</b>, ничья: <b>{hu_tie * 100:.1f}%</b>",
        f"Трое — победа: <b>{three_win * 100:.1f}%</b>, ничья: <b>{three_tie * 100:.1f}%</b>",
        "",
        "<i>Надеемся на руки:</i>",
    ]
    for h in hints_for_hand(hand):
        lines.append(f"\u2022 {h}")
    return "\n".join(lines)


CACHE_MISS_MESSAGE = (
    "Не удалось загрузить таблицу эквити для этой руки "
    "(нет записи в equity_cache.json). Обновите кеш на сервере."
)


async def aces_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_message is None:
        return
    if update.effective_chat is not None:
        await cleanup_expired_and_edit(update.effective_chat.id, context)
        busy = aces_busy_message(update.effective_chat.id)
        if busy:
            await update.effective_message.reply_text(busy)
            return
    hand = deal_random_hand()
    eq = read_cached_equity(hand)
    if eq is None:
        await update.effective_message.reply_text(CACHE_MISS_MESSAGE)
        return
    hu_w, hu_t, th_w, th_t = eq
    await update.effective_message.reply_html(
        _format_message(hand, hu_w, hu_t, th_w, th_t)
    )


async def on_mention(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_message is None:
        return
    bot = context.bot
    if not _bot_mentioned_text(
        update.effective_message,
        bot.username,
        bot.id,
    ):
        return
    if update.effective_chat is not None:
        await cleanup_expired_and_edit(update.effective_chat.id, context)
        busy = aces_busy_message(update.effective_chat.id)
        if busy:
            await update.effective_message.reply_text(busy)
            return
    hand = deal_random_hand()
    eq = read_cached_equity(hand)
    if eq is None:
        await update.effective_message.reply_text(CACHE_MISS_MESSAGE)
        return
    hu_w, hu_t, th_w, th_t = eq
    await update.effective_message.reply_html(
        _format_message(hand, hu_w, hu_t, th_w, th_t)
    )
