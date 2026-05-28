"""8-bit public table renderer for the Telegram poker room."""

from __future__ import annotations

import math
from dataclasses import dataclass
from pathlib import Path
from typing import Final

from PIL import Image, ImageDraw, ImageFont

import poker_room

WIDTH = 2400
HEIGHT = 1500
ASSET_DIR = Path(__file__).resolve().parent / "assets" / "poker_table"
LATIN_FONT_PATH: Final[Path] = ASSET_DIR / "Minecraft.ttf"
CYRILLIC_FONT_PATH: Final[Path] = ASSET_DIR / "cyrillic-minecraft-font.ttf"
FONT_PATH: Final[Path] = CYRILLIC_FONT_PATH
SEAT_BOX_W: Final[int] = 400
SEAT_BOX_H: Final[int] = 290
SEAT_CARD_W: Final[int] = 118
SEAT_CARD_H: Final[int] = 164
BOARD_CARD_W: Final[int] = 178
BOARD_CARD_H: Final[int] = 248

STREET_LABELS_RU: Final[dict[str, str]] = {
    poker_room.STREET_PREFLOP: "ПРЕФЛОП",
    poker_room.STREET_FLOP: "ФЛОП",
    poker_room.STREET_TURN: "ТЕРН",
    poker_room.STREET_RIVER: "РИВЕР",
    poker_room.STREET_SHOWDOWN: "ВСКРЫТИЕ",
}

@dataclass(frozen=True)
class Box:
    x: int
    y: int
    w: int
    h: int

    @property
    def right(self) -> int:
        return self.x + self.w

    @property
    def bottom(self) -> int:
        return self.y + self.h

    @property
    def center_x(self) -> int:
        return self.x + self.w // 2

    @property
    def center_y(self) -> int:
        return self.y + self.h // 2

    def overlaps(self, other: "Box") -> bool:
        return not (
            self.right <= other.x
            or other.right <= self.x
            or self.bottom <= other.y
            or other.bottom <= self.y
        )


@dataclass(frozen=True)
class RenderResult:
    path: Path
    size: tuple[int, int]
    seat_boxes: dict[int, Box]


@dataclass(frozen=True)
class TableFont:
    latin: ImageFont.ImageFont
    cyrillic: ImageFont.ImageFont

    def for_char(self, char: str) -> ImageFont.ImageFont:
        codepoint = ord(char)
        if 0x0400 <= codepoint <= 0x052F:
            return self.cyrillic
        return self.latin


def layout_seat_boxes(count: int, width: int = WIDTH, height: int = HEIGHT) -> dict[int, Box]:
    if count < 2 or count > poker_room.MAX_SEATS:
        raise ValueError("seat count must be between 2 and 10")
    box_w = SEAT_BOX_W
    box_h = SEAT_BOX_H
    center_x = width // 2
    center_y = height // 2
    margin = 24
    radius_x = width // 2 - box_w // 2 - 30
    radius_y = height // 2 - box_h // 2 - 20
    boxes: dict[int, Box] = {}
    for index in range(count):
        angle = -math.pi / 2 + (2 * math.pi * index / count)
        x = int(center_x + math.cos(angle) * radius_x - box_w / 2)
        y = int(center_y + math.sin(angle) * radius_y - box_h / 2)
        boxes[index] = Box(
            x=max(margin, min(width - box_w - margin, x)),
            y=max(margin, min(height - box_h - margin, y)),
            w=box_w,
            h=box_h,
        )
    return boxes


def render_table_png(hand: poker_room.PokerHand, path: str | Path) -> RenderResult:
    output = Path(path)
    image = Image.new("RGBA", (WIDTH, HEIGHT), "#1f2937ff")
    draw = ImageDraw.Draw(image)
    font_banner = _font(28)
    font = _font(30)
    font_small = _font(22)

    _draw_table(draw)
    _draw_banner(draw, _status_text(hand), font_banner)
    _draw_action_log(draw, hand, font_small)
    _draw_board(image, draw, hand)
    _draw_pot(image, draw, hand, font)

    seat_boxes = layout_seat_boxes(len(hand.order))
    for index, user_id in enumerate(hand.order):
        _draw_seat(image, draw, hand, user_id, seat_boxes[index], font, font_small)

    output.parent.mkdir(parents=True, exist_ok=True)
    image.save(output, format="PNG")
    return RenderResult(path=output, size=image.size, seat_boxes={hand.order[i]: box for i, box in seat_boxes.items()})


def _draw_table(draw: ImageDraw.ImageDraw) -> None:
    outer = (178, 225, WIDTH - 178, HEIGHT - 169)
    inner = (290, 338, WIDTH - 290, HEIGHT - 290)
    draw.ellipse(outer, fill="#ee6c4d", outline="#172333", width=15)
    draw.ellipse(inner, fill="#83bfd1", outline="#213243", width=11)
    draw.ellipse((460, 488, WIDTH - 460, HEIGHT - 440), outline="#5d9fb5", width=8)


def _draw_banner(draw: ImageDraw.ImageDraw, text: str, font: TableFont) -> None:
    box = Box(675, 395, 1050, 88)
    _pixel_rect(draw, box, "#2f3f54", "#f7f0d6", shadow=True)
    _center_text(draw, box, text, font, "#f7f0d6")


def _draw_action_log(draw: ImageDraw.ImageDraw, hand: poker_room.PokerHand, font: TableFont) -> None:
    log_lines = [line for line in hand.public_log if not _is_board_log_line(line)]
    if not log_lines:
        return
    y = 505
    for line in log_lines[-3:]:
        safe = _font_safe(line)
        truncated = _truncate_to_width(safe, font, 950)
        _draw_text(draw, (725, y), truncated, font, "#dfe7f5")
        y += 38


def _is_board_log_line(line: str) -> bool:
    """The board image already shows the community cards, so skip log lines that announce them."""

    head = line.split(":", 1)[0].strip().lower() if ":" in line else line.strip().lower()
    return head in {"флоп", "терн", "ривер"}


def _draw_board(image: Image.Image, draw: ImageDraw.ImageDraw, hand: poker_room.PokerHand) -> None:
    card_w, card_h = BOARD_CARD_W, BOARD_CARD_H
    gap = 24
    total = card_w * 5 + gap * 4
    start_x = (WIDTH - total) // 2
    y = 625
    cards = list(hand.board)
    while len(cards) < 5:
        cards.append("__back__")
    for offset, card in enumerate(cards[:5]):
        asset = _card_asset(card)
        image.alpha_composite(
            asset.resize((card_w, card_h), Image.Resampling.NEAREST),
            (start_x + offset * (card_w + gap), y),
        )


def _draw_pot(image: Image.Image, draw: ImageDraw.ImageDraw, hand: poker_room.PokerHand, font: TableFont) -> None:
    pot_box = Box(850, 910, 700, 90)
    _pixel_rect(draw, pot_box, "#98c7da", "#172333", shadow=True)
    chip = _asset("ChipRed.png").resize((52, 52), Image.Resampling.NEAREST)
    image.alpha_composite(chip, (pot_box.x + 30, pot_box.y + 19))
    image.alpha_composite(chip, (pot_box.right - 82, pot_box.y + 19))
    if hand.status == poker_room.STATUS_ENDED and hand.pot > 0:
        text = f"БАНК 0   ВЫПЛАЧЕНО {hand.pot}"
    else:
        text = f"БАНК {hand.pot}"
    _center_text(draw, pot_box, text, font, "#172333")
    _draw_side_pots(image, draw, hand, font)


def _draw_side_pots(image: Image.Image, draw: ImageDraw.ImageDraw, hand: poker_room.PokerHand, font: TableFont) -> None:
    if len(hand.side_pots) <= 1:
        return
    y = 990
    chip = _asset("ChipBlue.png").resize((35, 35), Image.Resampling.NEAREST)
    for index, pot in enumerate(hand.side_pots, start=1):
        pot_box = Box(925, y, 550, 48)
        _pixel_rect(draw, pot_box, "#dfe7f5", "#172333")
        image.alpha_composite(chip, (pot_box.x + 10, pot_box.y + 6))
        label = "ГЛАВНЫЙ БАНК" if index == 1 else f"САЙД ПОТ {index - 1}"
        _center_text(draw, pot_box, f"{label}  {pot.amount}", font, "#172333")
        y += 52


def _draw_seat(
    image: Image.Image,
    draw: ImageDraw.ImageDraw,
    hand: poker_room.PokerHand,
    user_id: int,
    box: Box,
    font: TableFont,
    font_small: TableFont,
) -> None:
    player = hand.players[user_id]
    border = "#b7f59a" if user_id == hand.to_act_user_id else "#f7f0d6"
    fill = "#326f55" if user_id == hand.to_act_user_id else "#566171"
    if player.folded:
        fill = "#474d58"
        border = "#9aa0a8"
    if player.all_in and not player.folded:
        border = "#f0b35a"
    _pixel_rect(draw, box, fill, border, shadow=True)

    name_width = box.w - 30 - (140 if player.street_bet else 0)
    name = _fit_text(player.name, font, name_width)
    _center_text(draw, Box(box.x + 15, box.y + 10, box.w - 30, 45), name, font, "#f7f0d6")

    card_y = box.y + 62
    card_gap = 16
    card_1 = _seat_card_asset(hand, player, 0).resize((SEAT_CARD_W, SEAT_CARD_H), Image.Resampling.NEAREST)
    card_2 = _seat_card_asset(hand, player, 1).resize((SEAT_CARD_W, SEAT_CARD_H), Image.Resampling.NEAREST)
    first_card_x = box.center_x - (SEAT_CARD_W * 2 + card_gap) // 2
    second_card_x = first_card_x + SEAT_CARD_W + card_gap
    image.alpha_composite(card_1, (first_card_x, card_y))
    image.alpha_composite(card_2, (second_card_x, card_y))
    if player.folded:
        _draw_fold_overlay(draw, first_card_x, card_y, SEAT_CARD_W, SEAT_CARD_H)
        _draw_fold_overlay(draw, second_card_x, card_y, SEAT_CARD_W, SEAT_CARD_H)

    bottom_label_box = Box(box.x + 20, box.y + box.h - 52, box.w - 40, 40)
    _pixel_rect(draw, bottom_label_box, "#263342", "#f7f0d6")
    bottom_text = _seat_bottom_text(player, hand.status == poker_room.STATUS_ENDED)
    _center_text(draw, bottom_label_box, bottom_text, font_small, "#f7f0d6")

    if player.street_bet:
        bet_box = Box(box.x + box.w - 130, box.y - 22, 145, 52)
        _pixel_rect(draw, bet_box, "#f0b35a", "#172333")
        _center_text(draw, bet_box, str(player.street_bet), font_small, "#172333")

    _draw_seat_role_badge(draw, hand, user_id, box, font_small)


def _seat_bottom_text(player: poker_room.HandPlayer, hand_ended: bool) -> str:
    if player.folded:
        return "ФОЛД"
    if player.all_in and not hand_ended:
        return f"АЛЛИН {player.committed}"
    return str(player.stack)


def _draw_seat_role_badge(
    draw: ImageDraw.ImageDraw,
    hand: poker_room.PokerHand,
    user_id: int,
    box: Box,
    font: TableFont,
) -> None:
    badges: list[tuple[str, str, str]] = []
    if user_id == hand.button_user_id:
        badges.append(("Д", "#f7c948", "#172333"))
    if user_id == hand.small_blind_user_id:
        badges.append(("МБ", "#dfe7f5", "#172333"))
    if user_id == hand.big_blind_user_id:
        badges.append(("ББ", "#dfe7f5", "#172333"))
    if not badges:
        return
    size = 46
    gap = 6
    total = len(badges) * size + (len(badges) - 1) * gap
    if box.center_x > WIDTH * 0.68:
        start_x = box.x - total - 6
        badge_y = box.center_y - size // 2
    elif box.center_x < WIDTH * 0.32:
        start_x = box.right + 6
        badge_y = box.center_y - size // 2
    else:
        start_x = box.center_x - total // 2
        if box.center_y > HEIGHT * 0.72:
            badge_y = box.y - size + 6
        else:
            badge_y = box.bottom - 6
    start_x = max(8, min(WIDTH - total - 8, start_x))
    badge_y = max(8, min(HEIGHT - size - 8, badge_y))
    for index, (label, fill, ink) in enumerate(badges):
        bx = start_x + index * (size + gap)
        draw.rectangle((bx + 4, badge_y + 4, bx + size + 4, badge_y + size + 4), fill="#00000055")
        draw.rectangle((bx, badge_y, bx + size, badge_y + size), fill=fill, outline=ink, width=3)
        _center_text(draw, Box(bx, badge_y, size, size), label, font, ink)


def _draw_fold_overlay(draw: ImageDraw.ImageDraw, x: int, y: int, w: int, h: int) -> None:
    draw.line((x + 6, y + 6, x + w - 6, y + h - 6), fill="#d33a2c", width=6)
    draw.line((x + w - 6, y + 6, x + 6, y + h - 6), fill="#d33a2c", width=6)


def _seat_card_asset(hand: poker_room.PokerHand, player: poker_room.HandPlayer, index: int) -> Image.Image:
    if player.user_id in hand.public_revealed_user_ids:
        return _card_asset(player.hand[index])
    return _card_asset("__back__")


def _card_asset(card: str) -> Image.Image:
    if card == "__back__":
        return _asset("Card.png")
    rank, suit = card[0], card[1]
    suit_prefix = {"s": "S", "h": "H", "d": "D", "c": "C"}[suit]
    return _asset(f"{suit_prefix}{rank}.png")


def _asset(name: str) -> Image.Image:
    return Image.open(ASSET_DIR / name).convert("RGBA")


def _font(size: int) -> TableFont:
    try:
        latin = ImageFont.truetype(str(LATIN_FONT_PATH), size=size)
        cyrillic = ImageFont.truetype(str(CYRILLIC_FONT_PATH), size=max(8, round(size * 2 / 3)))
        return TableFont(latin=latin, cyrillic=cyrillic)
    except OSError:
        fallback = ImageFont.load_default()
        return TableFont(latin=fallback, cyrillic=fallback)


def _pixel_rect(
    draw: ImageDraw.ImageDraw,
    box: Box,
    fill: str,
    outline: str,
    shadow: bool = False,
) -> None:
    if shadow:
        draw.rectangle((box.x + 9, box.y + 9, box.right + 9, box.bottom + 9), fill="#00000055")
    draw.rectangle((box.x, box.y, box.right, box.bottom), fill=fill, outline=outline, width=6)


def _center_text(
    draw: ImageDraw.ImageDraw,
    box: Box,
    text: str,
    font: TableFont,
    fill: str,
) -> None:
    bbox = _text_bbox(text, font)
    x = box.x + (box.w - (bbox[2] - bbox[0])) // 2 - bbox[0]
    y = box.y + (box.h - (bbox[3] - bbox[1])) // 2 - bbox[1]
    _draw_text(draw, (x, y), text, font, fill)


def _status_text(hand: poker_room.PokerHand) -> str:
    if hand.status == poker_room.STATUS_ENDED:
        return "РАЗДАЧА ОКОНЧЕНА"
    actor = hand.players.get(hand.to_act_user_id) if hand.to_act_user_id else None
    actor_text = _font_safe(actor.name) if actor else "СТОЛ"
    owed = 0
    if actor:
        owed = max(0, hand.current_bet - actor.street_bet)
    street = STREET_LABELS_RU.get(hand.street, hand.street.upper())
    return f"{street}    ХОД {actor_text}    КОЛЛ {owed}"


def _fit_text(text: str, font: TableFont, max_width: int) -> str:
    return _truncate_to_width(_font_safe(text), font, max_width)


def _truncate_to_width(text: str, font: TableFont, max_width: int) -> str:
    if _text_width(text, font) <= max_width:
        return text
    ellipsis = "."
    body = text
    while body and _text_width(body + ellipsis, font) > max_width:
        body = body[:-1]
    return (body + ellipsis) if body else ellipsis


def _text_width(text: str, font: TableFont) -> int:
    width = 0.0
    for char in text:
        selected = font.for_char(char)
        try:
            width += selected.getlength(char)
        except AttributeError:
            bbox = selected.getbbox(char)
            width += bbox[2] - bbox[0]
    return int(round(width))


def _text_bbox(text: str, font: TableFont) -> tuple[int, int, int, int]:
    width = _text_width(text, font)
    if not text:
        return (0, 0, 0, 0)
    top = 0
    bottom = 0
    for char in text:
        if char.isspace():
            continue
        bbox = font.for_char(char).getbbox(char)
        top = min(top, bbox[1])
        bottom = max(bottom, bbox[3])
    if bottom == 0:
        bbox = font.latin.getbbox("M")
        top = bbox[1]
        bottom = bbox[3]
    return (0, top, width, bottom)


def _draw_text(
    draw: ImageDraw.ImageDraw,
    xy: tuple[int, int],
    text: str,
    font: TableFont,
    fill: str,
) -> None:
    x, y = xy
    for char in text:
        selected = font.for_char(char)
        draw.text((x, y), char, font=selected, fill=fill)
        try:
            x += int(round(selected.getlength(char)))
        except AttributeError:
            bbox = selected.getbbox(char)
            x += bbox[2] - bbox[0]


def _font_safe(text: str) -> str:
    """Normalize user/log text before measuring and drawing it."""

    if not text:
        return ""
    collapsed = " ".join(text.split())
    while "  " in collapsed:
        collapsed = collapsed.replace("  ", " ")
    return collapsed.strip()
