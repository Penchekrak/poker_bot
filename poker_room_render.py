"""8-bit public table renderer for the Telegram poker room."""

from __future__ import annotations

import math
from dataclasses import dataclass
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

import poker_room

WIDTH = 1920
HEIGHT = 1200
ASSET_DIR = Path(__file__).resolve().parent / "assets" / "poker_table"


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


def layout_seat_boxes(count: int, width: int = WIDTH, height: int = HEIGHT) -> dict[int, Box]:
    if count < 2 or count > poker_room.MAX_SEATS:
        raise ValueError("seat count must be between 2 and 10")
    box_w = 246
    box_h = 136
    center_x = width // 2
    center_y = height // 2
    radius_x = width // 2 - box_w // 2 - 48
    radius_y = height // 2 - box_h // 2 - 45
    boxes: dict[int, Box] = {}
    for index in range(count):
        angle = -math.pi / 2 + (2 * math.pi * index / count)
        x = int(center_x + math.cos(angle) * radius_x - box_w / 2)
        y = int(center_y + math.sin(angle) * radius_y - box_h / 2)
        boxes[index] = Box(
            x=max(10, min(width - box_w - 10, x)),
            y=max(10, min(height - box_h - 10, y)),
            w=box_w,
            h=box_h,
        )
    return boxes


def render_table_png(hand: poker_room.PokerHand, path: str | Path) -> RenderResult:
    output = Path(path)
    image = Image.new("RGBA", (WIDTH, HEIGHT), "#1f2937ff")
    draw = ImageDraw.Draw(image)
    font_big = _font(42)
    font = _font(27)
    font_small = _font(20)

    _draw_table(draw)
    _draw_banner(draw, _status_text(hand), font)
    _draw_board(image, draw, hand, font_big)

    seat_boxes = layout_seat_boxes(len(hand.order))
    for index, user_id in enumerate(hand.order):
        _draw_seat(image, draw, hand, user_id, seat_boxes[index], font, font_small)

    output.parent.mkdir(parents=True, exist_ok=True)
    image.save(output, format="PNG")
    return RenderResult(path=output, size=image.size, seat_boxes={hand.order[i]: box for i, box in seat_boxes.items()})


def _draw_table(draw: ImageDraw.ImageDraw) -> None:
    # Thick pixel-ish border around an oval table; no copied Clubs assets.
    outer = (142, 180, WIDTH - 142, HEIGHT - 135)
    inner = (232, 270, WIDTH - 232, HEIGHT - 232)
    draw.ellipse(outer, fill="#ee6c4d", outline="#172333", width=12)
    draw.ellipse(inner, fill="#83bfd1", outline="#213243", width=9)
    draw.ellipse((368, 390, WIDTH - 368, HEIGHT - 352), outline="#5d9fb5", width=6)


def _draw_banner(draw: ImageDraw.ImageDraw, text: str, font: ImageFont.ImageFont) -> None:
    box = Box(585, 309, 750, 69)
    _pixel_rect(draw, box, "#2f3f54", "#f7f0d6", shadow=True)
    _center_text(draw, box, text, font, "#f7f0d6")


def _draw_board(image: Image.Image, draw: ImageDraw.ImageDraw, hand: poker_room.PokerHand, font: ImageFont.ImageFont) -> None:
    card_w, card_h = 114, 168
    gap = 15
    total = card_w * 5 + gap * 4
    start_x = (WIDTH - total) // 2
    y = 501
    cards = list(hand.board)
    while len(cards) < 5:
        cards.append("__back__")
    for offset, card in enumerate(cards[:5]):
        asset = _card_asset(card)
        image.alpha_composite(asset.resize((card_w, card_h), Image.Resampling.NEAREST), (start_x + offset * (card_w + gap), y))

    pot_box = Box(758, 711, 405, 75)
    _pixel_rect(draw, pot_box, "#98c7da", "#172333", shadow=True)
    chip = _asset("ChipRed.png").resize((42, 42), Image.Resampling.NEAREST)
    image.alpha_composite(chip, (pot_box.x + 27, pot_box.y + 16))
    image.alpha_composite(chip, (pot_box.right - 69, pot_box.y + 16))
    _center_text(draw, pot_box, f"POT {hand.pot}", font, "#172333")


def _draw_seat(
    image: Image.Image,
    draw: ImageDraw.ImageDraw,
    hand: poker_room.PokerHand,
    user_id: int,
    box: Box,
    font: ImageFont.ImageFont,
    font_small: ImageFont.ImageFont,
) -> None:
    player = hand.players[user_id]
    border = "#b7f59a" if user_id == hand.to_act_user_id else "#f7f0d6"
    fill = "#326f55" if user_id == hand.to_act_user_id else "#566171"
    if player.folded:
        fill = "#474d58"
        border = "#9aa0a8"
    _pixel_rect(draw, box, fill, border, shadow=True)

    name = _truncate(player.name.upper(), 18)
    _center_text(draw, Box(box.x + 12, box.y + 10, box.w - 24, 28), name, font_small, "#f7f0d6")

    card_y = box.y + 42
    card_1 = _seat_card_asset(hand, player, 0).resize((38, 59), Image.Resampling.NEAREST)
    card_2 = _seat_card_asset(hand, player, 1).resize((38, 59), Image.Resampling.NEAREST)
    image.alpha_composite(card_1, (box.x + box.w // 2 - 42, card_y))
    image.alpha_composite(card_2, (box.x + box.w // 2 + 5, card_y))

    stack_text = "FOLD" if player.folded else str(player.stack)
    _pixel_rect(draw, Box(box.x + 32, box.y + 100, box.w - 64, 28), "#263342", "#f7f0d6")
    _center_text(draw, Box(box.x + 32, box.y + 99, box.w - 64, 30), stack_text, font_small, "#f7f0d6")

    if player.street_bet:
        _pixel_rect(draw, Box(box.x + box.w - 82, box.y - 15, 96, 36), "#f0b35a", "#172333")
        _center_text(draw, Box(box.x + box.w - 82, box.y - 16, 96, 38), str(player.street_bet), font_small, "#172333")


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


def _font(size: int) -> ImageFont.ImageFont:
    font_path = ASSET_DIR / "Minecraft.ttf"
    try:
        return ImageFont.truetype(str(font_path), size=size)
    except OSError:
        return ImageFont.load_default()


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
    font: ImageFont.ImageFont,
    fill: str,
) -> None:
    bbox = draw.textbbox((0, 0), text, font=font)
    x = box.x + (box.w - (bbox[2] - bbox[0])) // 2
    y = box.y + (box.h - (bbox[3] - bbox[1])) // 2 - 1
    draw.text((x, y), text, font=font, fill=fill)


def _status_text(hand: poker_room.PokerHand) -> str:
    if hand.status == poker_room.STATUS_ENDED:
        return "HAND ENDED"
    actor = hand.players.get(hand.to_act_user_id) if hand.to_act_user_id else None
    actor_text = actor.name.upper() if actor else "СТОЛ"
    owed = 0
    if actor:
        owed = max(0, hand.current_bet - actor.street_bet)
    street = {
        poker_room.STREET_PREFLOP: "PREFLOP",
        poker_room.STREET_FLOP: "FLOP",
        poker_room.STREET_TURN: "TURN",
        poker_room.STREET_RIVER: "RIVER",
        poker_room.STREET_SHOWDOWN: "SHOWDOWN",
    }.get(hand.street, hand.street.upper())
    return f"{street} | ACT {actor_text} | CALL {owed}"


def _truncate(text: str, max_len: int) -> str:
    return text if len(text) <= max_len else text[: max_len - 1] + "…"
