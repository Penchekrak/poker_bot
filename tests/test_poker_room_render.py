from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from PIL import Image, ImageFont

import poker_room
import poker_room_render


class PokerRoomRenderTests(unittest.TestCase):
    def make_hand(self, player_count: int) -> poker_room.PokerHand:
        room = poker_room.PokerRoom(now=1_000.0)
        user_ids = list(range(1, player_count + 1))
        if player_count == poker_room.MAX_SEATS:
            user_ids[-1] = poker_room.RESERVED_SEAT_USER_ID
        for index, user_id in enumerate(user_ids, start=1):
            room.confirm_room_intent(
                user_id,
                f"p{index}",
                f"Player {index}",
                poker_room.ROOM_JOIN,
                now=1_000.0 + index,
            )
        return room.start_hand(now=1_100.0)

    def test_layout_boxes_do_not_overlap_for_2_6_and_10_players(self) -> None:
        for count in (2, 6, 10):
            with self.subTest(count=count):
                boxes = list(poker_room_render.layout_seat_boxes(count).values())
                for i, left in enumerate(boxes):
                    for right in boxes[i + 1 :]:
                        self.assertFalse(left.overlaps(right), f"{left} overlaps {right}")

    def test_renderer_font_pair_covers_latin_and_cyrillic_so_names_are_not_tofu(self) -> None:
        latin_font = ImageFont.truetype(str(poker_room_render.LATIN_FONT_PATH), size=24)
        cyrillic_font = ImageFont.truetype(str(poker_room_render.CYRILLIC_FONT_PATH), size=16)

        latin_missing_signature = _glyph_signature(latin_font, "\u0378")
        for char in "andreyАНДРЕЙ":
            if char.isascii():
                with self.subTest(char=char):
                    self.assertNotEqual(_glyph_signature(latin_font, char), latin_missing_signature)

        cyrillic_missing_signature = _glyph_signature(cyrillic_font, "\u0378")
        for char in "АНДРЕЙ":
            with self.subTest(char=char):
                self.assertNotEqual(_glyph_signature(cyrillic_font, char), cyrillic_missing_signature)

        table_font = poker_room_render._font(24)
        self.assertIn("Minecraft", table_font.for_char("A").getname()[0])
        self.assertIn("Cyrillic", table_font.for_char("А").getname()[0])

    def test_layout_allocates_readable_seat_panels_for_private_cards(self) -> None:
        boxes = list(poker_room_render.layout_seat_boxes(10).values())

        for box in boxes:
            self.assertGreaterEqual(box.w, 390)
            self.assertGreaterEqual(box.h, 280)
            self.assertGreaterEqual(box.x, 0)
            self.assertGreaterEqual(box.y, 0)
            self.assertLessEqual(box.right, poker_room_render.WIDTH)
            self.assertLessEqual(box.bottom, poker_room_render.HEIGHT)

    def test_card_targets_are_large_enough_for_readable_glyphs(self) -> None:
        self.assertGreaterEqual(poker_room_render.WIDTH, 2400)
        self.assertGreaterEqual(poker_room_render.HEIGHT, 1500)
        self.assertGreaterEqual(poker_room_render.SEAT_CARD_H, 160)
        self.assertGreaterEqual(poker_room_render.BOARD_CARD_H, 240)

        two_card_width = poker_room_render.SEAT_CARD_W * 2 + 16
        self.assertLessEqual(two_card_width, poker_room_render.SEAT_BOX_W - 40)

    def test_render_outputs_nonblank_png_for_table_sizes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            for count in (2, 6, 10):
                with self.subTest(count=count):
                    hand = self.make_hand(count)
                    path = Path(tmp) / f"table-{count}.png"

                    result = poker_room_render.render_table_png(hand, path)

                    self.assertEqual(result.size, (2400, 1500))
                    self.assertTrue(path.exists())
                    with Image.open(path) as image:
                        self.assertEqual(image.size, (2400, 1500))
                        colors = image.convert("RGB").getcolors(maxcolors=2_000_000)
                    self.assertIsNotNone(colors)
                    self.assertGreater(len(colors or []), 20)
                    for i, left in enumerate(result.seat_boxes.values()):
                        for right in list(result.seat_boxes.values())[i + 1 :]:
                            self.assertFalse(left.overlaps(right), f"{left} overlaps {right}")


def _glyph_signature(font: ImageFont.ImageFont, char: str) -> tuple[int, tuple[int, int] | None, bytes]:
    mask = font.getmask(char, mode="L")
    return (hash(bytes(mask)), getattr(mask, "size", None), bytes(mask))


if __name__ == "__main__":
    unittest.main()
