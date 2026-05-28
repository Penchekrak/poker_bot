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
        for user_id in range(1, player_count + 1):
            room.confirm_room_intent(
                user_id,
                f"p{user_id}",
                f"Player {user_id}",
                poker_room.ROOM_JOIN,
                now=1_000.0 + user_id,
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
            self.assertGreaterEqual(box.w, 320)
            self.assertGreaterEqual(box.h, 210)
            self.assertGreaterEqual(box.x, 0)
            self.assertGreaterEqual(box.y, 0)
            self.assertLessEqual(box.right, poker_room_render.WIDTH)
            self.assertLessEqual(box.bottom, poker_room_render.HEIGHT)

    def test_render_outputs_nonblank_png_for_table_sizes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            for count in (2, 6, 10):
                with self.subTest(count=count):
                    hand = self.make_hand(count)
                    path = Path(tmp) / f"table-{count}.png"

                    result = poker_room_render.render_table_png(hand, path)

                    self.assertEqual(result.size, (1920, 1200))
                    self.assertTrue(path.exists())
                    with Image.open(path) as image:
                        self.assertEqual(image.size, (1920, 1200))
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
