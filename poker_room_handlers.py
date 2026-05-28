"""Telegram handlers for the forum-scoped poker room."""

from __future__ import annotations

import html
import logging
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, InputMediaPhoto, Update
from telegram.constants import ParseMode
from telegram.ext import ContextTypes

import poker_room
import poker_room_llm
import poker_room_render

log = logging.getLogger(__name__)

CALLBACK_PREFIX = "pr"
TURN_JOB_NAME = "poker-room-turn"
AUTO_DEAL_JOB_NAME = "poker-room-auto-deal"
ADMIN_OPEN = "open"
ADMIN_CLOSE = "close"
ADMIN_RESET = "reset"


@dataclass(frozen=True)
class RoomConfig:
    chat_id: int
    thread_id: int | None
    admin_user_ids: set[int]
    state_path: Path = Path("data/poker_room_state.json")
    render_dir: Path = Path(tempfile.gettempdir()) / "poker_room_renders"

    @classmethod
    def from_env(cls) -> "RoomConfig | None":
        raw_chat = os.environ.get("POKER_ROOM_CHAT_ID")
        raw_thread = os.environ.get("POKER_ROOM_THREAD_ID")
        if not raw_chat:
            return None
        admins = {
            int(part.strip())
            for part in os.environ.get("POKER_ADMIN_USER_IDS", "").split(",")
            if part.strip().isdigit()
        }
        return cls(
            chat_id=int(raw_chat),
            thread_id=int(raw_thread) if raw_thread else None,
            admin_user_ids=admins,
            state_path=Path(os.environ.get("POKER_STATE_PATH", "data/poker_room_state.json")),
        )


def reset_room_for_tests() -> None:
    """Clear cached room globals from tests that reuse module state."""
    # Runtime state is held in context.bot_data. This hook exists for symmetry with
    # the older game modules and for future module-level caches.


def get_room(context) -> poker_room.PokerRoom:
    room = context.bot_data.get("poker_room")
    if isinstance(room, poker_room.PokerRoom):
        return room
    config = _config(context)
    if config is None:
        room = poker_room.PokerRoom()
    else:
        room = poker_room.JsonRoomStore(config.state_path).load()
    context.bot_data["poker_room"] = room
    return room


async def poker_room_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    config = _config(context)
    if config is None:
        return
    if not _allowed_message(update, config):
        message = update.effective_message
        chat = update.effective_chat
        log.info(
            "Ignored poker room message chat=%s thread=%s configured_chat=%s configured_thread=%s",
            getattr(chat, "id", None),
            getattr(message, "message_thread_id", None) if message is not None else None,
            config.chat_id,
            config.thread_id,
        )
        return
    message = update.effective_message
    user = update.effective_user
    if message is None or user is None or not message.text:
        return
    log.info(
        "Poker room message chat=%s thread=%s user=%s text=%r",
        update.effective_chat.id if update.effective_chat else None,
        getattr(message, "message_thread_id", None),
        user.id,
        message.text[:160],
    )

    room = get_room(context)
    _append_public_message(context, _display_name(user), message.text)
    await _apply_due_timeout(context, room)

    hand = room.current_hand
    if hand is None and _room_ready_for_auto_deal(room):
        _schedule_auto_deal_if_missing(context)
        parsed_action = poker_room_llm.deterministic_parse(message.text)
        if parsed_action.kind == "poker_action":
            await message.reply_text("Раздача сброшена после рестарта. Новая раздача через 15 секунд.")
            return

    if hand and hand.status == poker_room.STATUS_BETTING and hand.to_act_user_id == user.id:
        parsed = poker_room_llm.parse_with_fallback(
            message.text,
            hand,
            recent_public_messages=context.bot_data.get("poker_room_recent_messages", []),
        )
        if parsed.kind == "poker_action" and parsed.action is not None:
            try:
                result = hand.apply_action(user.id, parsed.action)
            except poker_room.PokerActionError as exc:
                await message.reply_text(str(exc))
                return
            _save_room(context, room)
            await _render_room(context, hand, force_new=result.new_cards)
            _schedule_turn_timeout(context, hand)
            if hand.status == poker_room.STATUS_ENDED:
                _schedule_auto_deal(context)
            return

    parsed_room = poker_room_llm.parse_room_intent_with_fallback(
        message.text,
        room,
        recent_public_messages=context.bot_data.get("poker_room_recent_messages", []),
    )
    if parsed_room.kind == "room_intent" and parsed_room.room_intent and parsed_room.confidence >= 0.55:
        log.info(
            "Parsed room intent user=%s intent=%s confidence=%.2f",
            user.id,
            parsed_room.room_intent,
            parsed_room.confidence,
        )
        markup = _confirmation_markup(parsed_room.room_intent, user.id)
        await message.reply_text("Подтверди.", reply_markup=markup)
        return

    admin_intent = _admin_intent_from_text(message.text)
    if admin_intent:
        if user.id not in config.admin_user_ids:
            await message.reply_text("Только админ стола.")
            return
        await message.reply_text("Подтверди.", reply_markup=_admin_confirmation_markup(admin_intent, user.id))
        return

    return


async def poker_room_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    config = _config(context)
    if config is None or not _allowed_message(update, config):
        return
    message = update.effective_message
    user = update.effective_user
    if message is None or user is None:
        return
    if user.id not in config.admin_user_ids:
        await message.reply_text("Только админ стола.")
        return
    room = get_room(context)
    room.is_open = True
    _save_room(context, room)
    _schedule_auto_deal(context)
    log.info("Poker room opened by admin user=%s chat=%s", user.id, config.chat_id)
    await message.reply_text("Стол открыт.")


async def poker_room_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    config = _config(context)
    query = update.callback_query
    user = update.effective_user
    if config is None or query is None or user is None or query.message is None or not query.data:
        return
    if not _allowed_callback(query, config):
        return

    room = get_room(context)
    parts = query.data.split(":")
    if len(parts) < 2 or parts[0] != CALLBACK_PREFIX:
        return

    if parts[1] == "cards":
        hand = room.current_hand
        if hand is None or user.id not in hand.players:
            await query.answer("Ты не в текущей раздаче.", show_alert=True)
            return
        await query.answer(hand.private_hand_text(user.id), show_alert=True)
        return

    if parts[1] == "confirm" and len(parts) == 4:
        intent = parts[2]
        try:
            expected_user_id = int(parts[3])
        except ValueError:
            await query.answer("Кнопка сломалась.")
            return
        if expected_user_id != user.id:
            await query.answer("Это не твоя кнопка.", show_alert=True)
            return
        try:
            result = room.confirm_room_intent(user.id, user.username, _display_name(user), intent)
        except poker_room.PokerRoomError as exc:
            await query.answer(str(exc), show_alert=True)
            return
        _save_room(context, room)
        await query.answer(result.text)
        _schedule_auto_deal(context)
        return

    if parts[1] == "admin" and len(parts) == 4:
        intent = parts[2]
        try:
            expected_user_id = int(parts[3])
        except ValueError:
            await query.answer("Кнопка сломалась.")
            return
        if expected_user_id != user.id:
            await query.answer("Это не твоя кнопка.", show_alert=True)
            return
        if user.id not in config.admin_user_ids:
            await query.answer("Только админ стола.", show_alert=True)
            return
        try:
            text = _apply_admin_intent(context, room, intent)
        except poker_room.PokerRoomError as exc:
            await query.answer(str(exc), show_alert=True)
            return
        await query.answer(text)
        return

    if parts[1] == "reveal" and len(parts) == 3:
        hand = room.current_hand
        if hand is None:
            await query.answer("Раздачи нет.")
            return
        result = hand.choose_public_reveal(user.id, reveal=parts[2] == "1")
        await query.answer(result.text)
        await _render_room(context, hand, force_new=False)
        return

    await query.answer("Не понял кнопку.")


async def poker_room_timeout_job(context: ContextTypes.DEFAULT_TYPE) -> None:
    config = _config(context)
    if config is None:
        return
    room = get_room(context)
    await _apply_due_timeout(context, room, force=True)


async def poker_room_auto_deal_job(context: ContextTypes.DEFAULT_TYPE) -> None:
    room = get_room(context)
    if not room.is_open:
        return
    if room.current_hand and room.current_hand.status != poker_room.STATUS_ENDED:
        return
    try:
        hand = room.start_hand()
    except poker_room.PokerRoomError:
        return
    _save_room(context, room)
    await _render_room(context, hand, force_new=True)
    _schedule_turn_timeout(context, hand)


def room_callback_pattern() -> str:
    return rf"^{CALLBACK_PREFIX}:"


def room_text_filter_enabled(context) -> bool:
    return _config(context) is not None


def _config(context) -> RoomConfig | None:
    configured = getattr(context, "bot_data", {}).get("poker_room_config")
    if isinstance(configured, RoomConfig):
        return configured
    return RoomConfig.from_env()


def _allowed_message(update: Update, config: RoomConfig) -> bool:
    chat = update.effective_chat
    message = update.effective_message
    return bool(
        chat is not None
        and message is not None
        and chat.id == config.chat_id
        and (config.thread_id is None or getattr(message, "message_thread_id", None) == config.thread_id)
    )


def _allowed_callback(query, config: RoomConfig) -> bool:
    return bool(
        query.message.chat_id == config.chat_id
        and (config.thread_id is None or getattr(query.message, "message_thread_id", None) == config.thread_id)
    )


def _room_intent_from_text(text: str) -> str | None:
    value = " ".join(text.lower().strip().split())
    if value in {"сяду", "садусь", "join", "в игру", "играю"}:
        return poker_room.ROOM_JOIN
    if value in {"ребай", "rebuy", "докуп", "докуплюсь"}:
        return poker_room.ROOM_REBUY
    if value in {"ситаут", "sit out", "sitout", "посижу", "следующую пропущу"}:
        return poker_room.ROOM_SIT_OUT
    if value in {"уйду", "leave", "выхожу", "покинуть стол"}:
        return poker_room.ROOM_LEAVE
    return None


def _admin_intent_from_text(text: str) -> str | None:
    value = " ".join(text.lower().strip().split())
    if value in {"открыть стол", "open table", "poker open"}:
        return ADMIN_OPEN
    if value in {"закрыть стол", "close table", "poker close"}:
        return ADMIN_CLOSE
    if value in {"сбросить стол", "reset table", "poker reset"}:
        return ADMIN_RESET
    return None


def _confirmation_markup(intent: str, user_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [[InlineKeyboardButton("Подтвердить", callback_data=f"{CALLBACK_PREFIX}:confirm:{intent}:{user_id}")]]
    )


def _admin_confirmation_markup(intent: str, user_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [[InlineKeyboardButton("Подтвердить", callback_data=f"{CALLBACK_PREFIX}:admin:{intent}:{user_id}")]]
    )


def _apply_admin_intent(context, room: poker_room.PokerRoom, intent: str) -> str:
    if intent == ADMIN_OPEN:
        room.is_open = True
        _save_room(context, room)
        _schedule_auto_deal(context)
        return "Стол открыт."
    if intent == ADMIN_CLOSE:
        room.is_open = False
        _cancel_jobs(context, names={AUTO_DEAL_JOB_NAME})
        _save_room(context, room)
        return "Стол закрыт."
    if intent == ADMIN_RESET:
        reset_room = poker_room.PokerRoom()
        context.bot_data["poker_room"] = reset_room
        context.bot_data.pop("poker_room_render_message_id", None)
        context.bot_data.pop("poker_room_recent_messages", None)
        _cancel_jobs(context)
        _save_room(context, reset_room)
        return "Стол сброшен."
    raise poker_room.PokerRoomError("unknown admin intent")


def _table_markup(hand: poker_room.PokerHand) -> InlineKeyboardMarkup:
    rows = [[InlineKeyboardButton("Мои карты", callback_data=f"{CALLBACK_PREFIX}:cards")]]
    if hand.status == poker_room.STATUS_ENDED and hand.optional_reveal_user_ids():
        rows.append(
            [
                InlineKeyboardButton("Показать", callback_data=f"{CALLBACK_PREFIX}:reveal:1"),
                InlineKeyboardButton("Не показывать", callback_data=f"{CALLBACK_PREFIX}:reveal:0"),
            ]
        )
    return InlineKeyboardMarkup(rows)


async def _apply_due_timeout(context, room: poker_room.PokerRoom, force: bool = False) -> None:
    hand = room.current_hand
    if hand is None or hand.status != poker_room.STATUS_BETTING:
        return
    result = hand.apply_timeout(now=hand.updated_at + poker_room.TURN_TIMEOUT_SECONDS if force else None)
    if result.kind in {"waiting", "invalid"}:
        return
    _save_room(context, room)
    await _render_room(context, hand, force_new=result.new_cards)
    if hand.status == poker_room.STATUS_ENDED:
        _schedule_auto_deal(context)
    else:
        _schedule_turn_timeout(context, hand)


async def _render_room(context, hand: poker_room.PokerHand, force_new: bool) -> None:
    config = _config(context)
    if config is None:
        return
    config.render_dir.mkdir(parents=True, exist_ok=True)
    path = config.render_dir / f"poker-room-{int(hand.updated_at)}-{len(hand.board)}.png"
    poker_room_render.render_table_png(hand, path)
    commentary = ""
    if force_new or hand.status == poker_room.STATUS_ENDED:
        commentary = poker_room_llm.generate_dealer_commentary(
            hand.street,
            hand,
            recent_public_messages=context.bot_data.get("poker_room_recent_messages", []),
        )
    caption = _caption(hand, commentary)
    markup = _table_markup(hand)
    message_id = context.bot_data.get("poker_room_render_message_id")
    if message_id and not force_new:
        with path.open("rb") as photo:
            await context.bot.edit_message_media(
                chat_id=config.chat_id,
                message_id=message_id,
                media=InputMediaPhoto(photo, caption=caption, parse_mode=ParseMode.HTML),
                reply_markup=markup,
            )
        return
    send_kwargs = {
        "chat_id": config.chat_id,
        "caption": caption,
        "parse_mode": ParseMode.HTML,
        "reply_markup": markup,
    }
    if config.thread_id is not None:
        send_kwargs["message_thread_id"] = config.thread_id
    with path.open("rb") as photo:
        send_kwargs["photo"] = photo
        sent = await context.bot.send_photo(
            **send_kwargs,
        )
    context.bot_data["poker_room_render_message_id"] = sent.message_id


def _caption(hand: poker_room.PokerHand, commentary: str = "") -> str:
    lines = [
        "<b>Покерный стол</b>",
        f"Банк: <b>{hand.pot}</b>",
        f"Улица: <b>{html.escape(hand.street)}</b>",
    ]
    if hand.to_act_user_id:
        lines.append(f"Ход: <b>{html.escape(hand.players[hand.to_act_user_id].name)}</b>")
    if hand.public_log:
        lines.append(html.escape(hand.public_log[-1]))
    if commentary:
        lines.extend(["", f"🎙 {html.escape(commentary)}"])
    return "\n".join(lines)


def _schedule_turn_timeout(context, hand: poker_room.PokerHand) -> None:
    job_queue = getattr(context, "job_queue", None)
    if job_queue is None or hand.status != poker_room.STATUS_BETTING:
        return
    for job in job_queue.get_jobs_by_name(TURN_JOB_NAME):
        job.schedule_removal()
    job_queue.run_once(
        poker_room_timeout_job,
        poker_room.TURN_TIMEOUT_SECONDS,
        name=TURN_JOB_NAME,
        data={"to_act_user_id": hand.to_act_user_id},
    )


def _schedule_auto_deal(context) -> None:
    job_queue = getattr(context, "job_queue", None)
    if job_queue is None:
        return
    room = context.bot_data.get("poker_room")
    if isinstance(room, poker_room.PokerRoom) and not room.is_open:
        return
    for job in job_queue.get_jobs_by_name(AUTO_DEAL_JOB_NAME):
        job.schedule_removal()
    job_queue.run_once(poker_room_auto_deal_job, poker_room.AUTO_DEAL_SECONDS, name=AUTO_DEAL_JOB_NAME)


def _schedule_auto_deal_if_missing(context) -> None:
    job_queue = getattr(context, "job_queue", None)
    if job_queue is None or job_queue.get_jobs_by_name(AUTO_DEAL_JOB_NAME):
        return
    _schedule_auto_deal(context)


def _room_ready_for_auto_deal(room: poker_room.PokerRoom) -> bool:
    if not room.is_open:
        return False
    if room.current_hand and room.current_hand.status != poker_room.STATUS_ENDED:
        return False
    active = [
        seat
        for user_id in room.seat_order
        if (seat := room.seats.get(user_id)) is not None
        and not seat.sitting_out
        and seat.stack > 0
    ]
    return len(active) >= 2


def _cancel_jobs(context, names: set[str] | None = None) -> None:
    job_queue = getattr(context, "job_queue", None)
    if job_queue is None:
        return
    for name in (names or {TURN_JOB_NAME, AUTO_DEAL_JOB_NAME}):
        for job in job_queue.get_jobs_by_name(name):
            job.schedule_removal()


def _save_room(context, room: poker_room.PokerRoom) -> None:
    config = _config(context)
    if config is not None:
        poker_room.JsonRoomStore(config.state_path).save(room)


def _append_public_message(context, user: str, text: str) -> None:
    messages = list(context.bot_data.get("poker_room_recent_messages", []))
    messages.append((user, text))
    context.bot_data["poker_room_recent_messages"] = messages[-10:]


def _display_name(user) -> str:
    if getattr(user, "full_name", None):
        return user.full_name
    if getattr(user, "username", None):
        return f"@{user.username}"
    return str(user.id)
