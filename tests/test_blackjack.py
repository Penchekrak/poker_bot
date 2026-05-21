from __future__ import annotations

import unittest

import blackjack


class BlackjackGameTests(unittest.TestCase):
    def setUp(self) -> None:
        blackjack.reset_tables_for_tests()

    def test_only_one_active_table_per_chat(self) -> None:
        table = blackjack.create_table(
            chat_id=10,
            player_id=100,
            player_name="Hero",
            now=1_000.0,
            deck=blackjack.stacked_deck(("Ah", "9d", "7c", "8s")),
        )

        self.assertIs(blackjack.active_table(10, now=1_001.0), table)
        with self.assertRaisesRegex(blackjack.TableLimitError, "стол уже занят"):
            blackjack.create_table(
                chat_id=10,
                player_id=101,
                player_name="Other",
                now=1_002.0,
                deck=blackjack.stacked_deck(("2h", "3d", "4c", "5s")),
            )

    def test_hit_can_bust_player_and_end_table(self) -> None:
        table = blackjack.create_table(
            chat_id=10,
            player_id=100,
            player_name="Hero",
            now=1_000.0,
            deck=blackjack.stacked_deck(("Th", "9d", "7c", "8s", "6h")),
        )

        result = table.hit(100, now=1_001.0)

        self.assertEqual(result.kind, "ended")
        self.assertEqual(table.status, blackjack.STATUS_ENDED)
        self.assertEqual(table.player_hand, ["Th", "7c", "6h"])
        self.assertIn("перебор", table.render_html())

    def test_stand_plays_dealer_and_player_can_win(self) -> None:
        table = blackjack.create_table(
            chat_id=10,
            player_id=100,
            player_name="Hero",
            now=1_000.0,
            deck=blackjack.stacked_deck(("Th", "9d", "7c", "6s", "9h", "Qd")),
        )

        result = table.stand(100, now=1_001.0)

        self.assertEqual(result.kind, "ended")
        self.assertEqual(table.status, blackjack.STATUS_ENDED)
        self.assertEqual(table.dealer_hand, ["9d", "6s", "9h"])
        self.assertEqual(table.outcome, blackjack.OUTCOME_PLAYER)
        html = table.render_html()
        self.assertIn("Hero", html)
        self.assertIn("Победа игрока", html)

    def test_non_player_cannot_act(self) -> None:
        table = blackjack.create_table(
            chat_id=10,
            player_id=100,
            player_name="Hero",
            now=1_000.0,
            deck=blackjack.stacked_deck(("Ah", "9d", "7c", "8s")),
        )

        result = table.hit(200, now=1_001.0)

        self.assertEqual(result.kind, "not_in_game")
        self.assertEqual(table.status, blackjack.STATUS_PLAYING)
        self.assertEqual(table.player_hand, ["Ah", "7c"])


if __name__ == "__main__":
    unittest.main()
