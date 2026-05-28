from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

import poker_room


class PokerRoomEngineTests(unittest.TestCase):
    def make_room(self) -> poker_room.PokerRoom:
        room = poker_room.PokerRoom(now=1_000.0)
        room.confirm_room_intent(1, "alice", "Alice", poker_room.ROOM_JOIN, now=1_000.0)
        room.confirm_room_intent(2, "bob", "Bob", poker_room.ROOM_JOIN, now=1_001.0)
        room.confirm_room_intent(3, "cara", "Cara", poker_room.ROOM_JOIN, now=1_002.0)
        return room

    def test_starts_multiway_hand_with_button_blinds_and_preflop_actor(self) -> None:
        room = self.make_room()

        hand = room.start_hand(
            now=1_010.0,
            deck=poker_room.stacked_deck(
                {
                    1: ("Ah", "Ad"),
                    2: ("Kh", "Kd"),
                    3: ("Qh", "Qd"),
                    "board": ("2c", "7d", "9h", "Ts", "3c"),
                }
            ),
        )

        self.assertEqual(hand.button_user_id, 1)
        self.assertEqual(hand.small_blind_user_id, 2)
        self.assertEqual(hand.big_blind_user_id, 3)
        self.assertEqual(hand.to_act_user_id, 1)
        self.assertEqual(hand.pot, 150)
        self.assertEqual(hand.players[1].stack, 10_000)
        self.assertEqual(hand.players[2].stack, 9_950)
        self.assertEqual(hand.players[3].stack, 9_900)
        self.assertEqual(hand.private_hand_text(1), "Твои карты: A♦ · A♥")

    def test_ambiguous_raise_uses_lesser_legal_amount(self) -> None:
        room = self.make_room()
        hand = room.start_hand(now=1_010.0)

        result = hand.apply_action(1, poker_room.PlayerAction("raise_ambiguous", 500), now=1_011.0)

        self.assertEqual(result.kind, "acted")
        self.assertEqual(hand.current_bet, 500)
        self.assertEqual(hand.players[1].street_bet, 500)
        self.assertIn("рейз до 500", hand.public_log[-1])

    def test_multiway_all_in_side_pots_award_main_and_side_correctly(self) -> None:
        room = poker_room.PokerRoom(now=1_000.0)
        room.confirm_room_intent(1, "alice", "Alice", poker_room.ROOM_JOIN, now=1_000.0)
        room.confirm_room_intent(2, "bob", "Bob", poker_room.ROOM_JOIN, now=1_001.0)
        room.confirm_room_intent(3, "cara", "Cara", poker_room.ROOM_JOIN, now=1_002.0)
        room.seats[1].stack = 500
        room.seats[2].stack = 300
        room.seats[3].stack = 500
        hand = room.start_hand(
            now=1_010.0,
            deck=poker_room.stacked_deck(
                {
                    1: ("Ah", "Ad"),
                    2: ("Kh", "Kd"),
                    3: ("Qh", "Qd"),
                    "board": ("Kc", "2d", "3h", "8s", "5c"),
                }
            ),
        )

        self.assertEqual(hand.apply_action(1, poker_room.PlayerAction("all_in"), now=1_011.0).kind, "acted")
        self.assertEqual(hand.apply_action(2, poker_room.PlayerAction("all_in"), now=1_012.0).kind, "acted")
        result = hand.apply_action(3, poker_room.PlayerAction("call"), now=1_013.0)

        self.assertEqual(result.kind, "showdown")
        self.assertEqual(hand.board, ["Kc", "2d", "3h", "8s", "5c"])
        self.assertEqual(room.seats[1].stack, 400)
        self.assertEqual(room.seats[2].stack, 900)
        self.assertEqual(room.seats[3].stack, 0)
        self.assertTrue(room.seats[3].sitting_out)
        self.assertIn(1, hand.public_revealed_user_ids)
        self.assertIn(2, hand.public_revealed_user_ids)
        self.assertNotIn(3, hand.public_revealed_user_ids)

    def test_covering_call_against_all_in_runs_board_to_showdown(self) -> None:
        room = poker_room.PokerRoom(now=1_000.0)
        room.confirm_room_intent(1, "alice", "Alice", poker_room.ROOM_JOIN, now=1_000.0)
        room.confirm_room_intent(2, "bob", "Bob", poker_room.ROOM_JOIN, now=1_001.0)
        room.seats[1].stack = 1_000
        room.seats[2].stack = 500
        hand = room.start_hand(
            now=1_010.0,
            deck=poker_room.stacked_deck(
                {
                    1: ("Ah", "Ad"),
                    2: ("Kh", "Kd"),
                    "board": ("2c", "7d", "9h", "Ts", "3c"),
                }
            ),
        )

        hand.apply_action(1, poker_room.PlayerAction("raise_to", 500), now=1_011.0)
        result = hand.apply_action(2, poker_room.PlayerAction("call"), now=1_012.0)

        self.assertEqual(result.kind, "showdown")
        self.assertEqual(hand.status, poker_room.STATUS_ENDED)
        self.assertEqual(hand.board, ["2c", "7d", "9h", "Ts", "3c"])

    def test_fold_awards_pot_without_auto_revealing_any_hand(self) -> None:
        room = self.make_room()
        hand = room.start_hand(now=1_010.0)

        result = hand.apply_action(1, poker_room.PlayerAction("fold"), now=1_011.0)

        self.assertEqual(result.kind, "acted")
        self.assertEqual(hand.status, poker_room.STATUS_BETTING)
        self.assertEqual(hand.public_revealed_user_ids, set())

        hand.apply_action(2, poker_room.PlayerAction("fold"), now=1_012.0)

        self.assertEqual(hand.status, poker_room.STATUS_ENDED)
        self.assertEqual(hand.public_revealed_user_ids, set())
        self.assertEqual(hand.optional_reveal_user_ids(), {3})

    def test_short_small_blind_all_in_keeps_hand_progressing(self) -> None:
        room = poker_room.PokerRoom(now=1_000.0)
        room.confirm_room_intent(1, "alice", "Alice", poker_room.ROOM_JOIN, now=1_000.0)
        room.confirm_room_intent(2, "bob", "Bob", poker_room.ROOM_JOIN, now=1_001.0)
        room.seats[1].stack = 50
        hand = room.start_hand(now=1_010.0)

        self.assertEqual(hand.to_act_user_id, 2)
        self.assertTrue(hand.players[1].all_in)

        result = hand.apply_timeout(now=1_131.0)

        self.assertEqual(result.kind, "showdown")
        self.assertEqual(hand.status, poker_room.STATUS_ENDED)

    def test_folded_player_cannot_choose_public_reveal(self) -> None:
        room = self.make_room()
        hand = room.start_hand(now=1_010.0)
        hand.apply_action(1, poker_room.PlayerAction("fold"), now=1_011.0)
        hand.apply_action(2, poker_room.PlayerAction("fold"), now=1_012.0)

        result = hand.choose_public_reveal(1, reveal=True)

        self.assertEqual(result.kind, "invalid")
        self.assertEqual(result.text, "Фолд уже не показываем.")

    def test_timeout_checks_for_free_and_folds_when_facing_bet(self) -> None:
        room = self.make_room()
        hand = room.start_hand(now=1_000.0)

        result = hand.apply_timeout(now=1_121.0)

        self.assertEqual(result.kind, "acted")
        self.assertTrue(hand.players[1].folded)
        self.assertEqual(hand.to_act_user_id, 2)

        hand.apply_action(2, poker_room.PlayerAction("call"), now=1_122.0)
        self.assertEqual(hand.apply_timeout(now=1_243.0).kind, "advanced")
        self.assertEqual(hand.street, poker_room.STREET_FLOP)

    def test_closed_room_rejects_new_seats_and_new_hands(self) -> None:
        room = poker_room.PokerRoom(now=1_000.0)
        room.confirm_room_intent(1, "alice", "Alice", poker_room.ROOM_JOIN, now=1_000.0)
        room.confirm_room_intent(2, "bob", "Bob", poker_room.ROOM_JOIN, now=1_001.0)

        room.is_open = False

        with self.assertRaises(poker_room.PokerRoomError):
            room.confirm_room_intent(3, "cara", "Cara", poker_room.ROOM_JOIN, now=1_002.0)
        with self.assertRaises(poker_room.PokerRoomError):
            room.start_hand(now=1_010.0)


class PokerRoomPersistenceTests(unittest.TestCase):
    def test_json_state_persists_stacks_but_not_active_hand_private_information(self) -> None:
        room = poker_room.PokerRoom(now=1_000.0)
        room.confirm_room_intent(1, "alice", "Alice", poker_room.ROOM_JOIN, now=1_000.0)
        room.confirm_room_intent(2, "bob", "Bob", poker_room.ROOM_JOIN, now=1_001.0)
        room.start_hand(
            now=1_010.0,
            deck=poker_room.stacked_deck(
                {
                    1: ("Ah", "Ad"),
                    2: ("Kh", "Kd"),
                    "board": ("2c", "7d", "9h", "Ts", "3c"),
                }
            ),
        )

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "room.json"
            poker_room.JsonRoomStore(path).save(room)
            raw = path.read_text()
            data = json.loads(raw)

            self.assertNotIn("Ah", raw)
            self.assertNotIn("Ad", raw)
            self.assertNotIn("Kh", raw)
            self.assertNotIn("deck", raw)
            self.assertNotIn("board", raw)
            self.assertEqual(data["seats"][0]["stack"], 9_950)

            loaded = poker_room.JsonRoomStore(path).load()
            self.assertIsNone(loaded.current_hand)
            self.assertEqual(loaded.seats[1].stack, 9_950)


if __name__ == "__main__":
    unittest.main()
