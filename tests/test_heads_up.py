from __future__ import annotations

import unittest

from telegram.error import BadRequest

import heads_up


class HeadsUpGameTests(unittest.TestCase):
    def setUp(self) -> None:
        heads_up.reset_tables_for_tests()

    def start_confirmed_table(self) -> heads_up.GameTable:
        table = heads_up.create_table(
            chat_id=10,
            caller_id=100,
            caller_username="hero",
            caller_name="Hero",
            callee_username="villain",
            now=1_000.0,
            deck=heads_up.stacked_deck(
                {
                    "sb": ("Ah", "Ad"),
                    "bb": ("Ks", "Kd"),
                    "board": ("2c", "7d", "9h", "Ts", "3c"),
                }
            ),
        )
        self.assertEqual(table.tap_cards(200, "villain", "Villain", now=1_001.0).kind, "ready")
        self.assertEqual(table.tap_cards(100, "hero", "Hero", now=1_002.0).kind, "ready")
        return table

    def test_table_capacity_and_busy_messages(self) -> None:
        first = heads_up.create_table(10, 100, "hero", "Hero", "villain", now=1.0)
        second = heads_up.create_table(10, 101, "p1", "P1", "p2", now=2.0)

        self.assertEqual([first.table_id, second.table_id], [1, 2])
        self.assertTrue(heads_up.tables_are_full(10, now=3.0))
        self.assertEqual(heads_up.heads_up_busy_message(10, now=3.0), "все столы заняты")
        self.assertEqual(heads_up.aces_busy_message(10, now=3.0), "все дилеры заняты")
        with self.assertRaisesRegex(heads_up.TableLimitError, "все столы заняты"):
            heads_up.create_table(10, 102, "p3", "P3", "p4", now=4.0)

    def test_same_user_cannot_sit_at_two_tables_in_chat(self) -> None:
        heads_up.create_table(10, 100, "hero", "Hero", "villain", now=1.0)

        with self.assertRaisesRegex(heads_up.SeatTakenError, "уже сидишь"):
            heads_up.create_table(10, 100, "hero", "Hero", "other", now=2.0)

        table = heads_up.create_table(10, 101, "other", "Other", "hero", now=3.0)
        result = table.tap_cards(100, "hero", "Hero", now=4.0)
        self.assertEqual(result.kind, "not_in_game")

    def test_username_binding_and_one_tap_ready(self) -> None:
        table = heads_up.create_table(10, 100, "hero", "Hero", "villain", now=1.0)

        stranger = table.tap_cards(300, "railbird", "Railbird", now=2.0)
        self.assertEqual(stranger.kind, "not_in_game")

        first = table.tap_cards(200, "villain", "Villain", now=3.0)
        ready = table.tap_cards(100, "hero", "Hero", now=4.0)

        self.assertEqual(first.kind, "ready")
        self.assertIn("Твои карты:", first.text)
        self.assertTrue(table.players["sb"].confirmed)
        self.assertEqual(table.players["sb"].user_id, 200)
        self.assertEqual(ready.kind, "ready")
        self.assertEqual(table.status, heads_up.STATUS_BETTING)
        self.assertEqual(table.to_act, "sb")

    def test_blinds_stacks_and_big_blind_check_moves_to_flop(self) -> None:
        table = self.start_confirmed_table()

        self.assertEqual(table.pot, 150)
        self.assertEqual(table.players["sb"].stack, 9_950)
        self.assertEqual(table.players["bb"].stack, 9_900)
        self.assertEqual(table.players["sb"].street_bet, 50)
        self.assertEqual(table.players["bb"].street_bet, 100)

        result = table.apply_action(200, heads_up.ACTION_CALL, now=1_005.0)

        self.assertEqual(result.kind, "acted")
        self.assertEqual(table.street, heads_up.STREET_PREFLOP)
        self.assertEqual(table.to_act, "bb")
        self.assertEqual(table.pot, 200)
        self.assertEqual(table.current_bet, 100)
        self.assertIn("колл 50", table.action_log[-1])

        result = table.apply_action(100, heads_up.ACTION_CHECK, now=1_006.0)

        self.assertEqual(result.kind, "advanced")
        self.assertEqual(table.street, heads_up.STREET_FLOP)
        self.assertEqual(table.board, ["2c", "7d", "9h"])
        self.assertEqual(table.pot, 200)
        self.assertEqual(table.to_act, "bb")
        self.assertEqual(table.current_bet, 0)
        self.assertTrue(any("чек" in entry for entry in table.action_log))

    def test_min_pot_and_all_in_targets_are_capped_by_stack(self) -> None:
        table = self.start_confirmed_table()

        actions = table.legal_actions()
        self.assertEqual(actions[heads_up.ACTION_MIN_RAISE], 200)
        self.assertEqual(actions[heads_up.ACTION_POT_RAISE], 350)
        self.assertEqual(actions[heads_up.ACTION_ALL_IN], 10_000)

        table.apply_action(200, heads_up.ACTION_POT_RAISE, now=1_005.0)
        self.assertEqual(table.players["sb"].street_bet, 350)
        self.assertEqual(table.current_bet, 350)
        self.assertEqual(table.min_raise, 250)
        self.assertEqual(table.to_act, "bb")

        actions = table.legal_actions()
        self.assertEqual(actions[heads_up.ACTION_CALL], 350)
        self.assertEqual(actions[heads_up.ACTION_MIN_RAISE], 600)
        self.assertEqual(actions[heads_up.ACTION_POT_RAISE], 1_050)
        self.assertEqual(actions[heads_up.ACTION_ALL_IN], 10_000)

    def test_check_check_advances_postflop_street(self) -> None:
        table = self.start_confirmed_table()
        table.apply_action(200, heads_up.ACTION_CALL, now=1_005.0)
        table.apply_action(100, heads_up.ACTION_CHECK, now=1_006.0)

        table.apply_action(100, heads_up.ACTION_CHECK, now=1_007.0)
        result = table.apply_action(200, heads_up.ACTION_CHECK, now=1_008.0)

        self.assertEqual(result.kind, "advanced")
        self.assertEqual(table.street, heads_up.STREET_TURN)
        self.assertEqual(table.board, ["2c", "7d", "9h", "Ts"])
        self.assertEqual(table.to_act, "bb")

    def test_fold_waits_for_folder_reveal_choice(self) -> None:
        table = self.start_confirmed_table()

        result = table.apply_action(200, heads_up.ACTION_FOLD, now=1_005.0)

        self.assertEqual(result.kind, "folded")
        self.assertEqual(table.status, heads_up.STATUS_ENDED)
        self.assertEqual(table.winner_role, "bb")
        self.assertEqual(table.optional_reveal_roles(), {"sb", "bb"})

        winner_reveal = table.choose_public_reveal(100, reveal=True, now=1_006.0)
        self.assertEqual(winner_reveal.kind, "ended")
        self.assertIn("bb", table.public_revealed_roles)
        self.assertEqual(table.optional_reveal_roles(), {"sb"})

        reveal = table.choose_public_reveal(200, reveal=True, now=1_007.0)
        self.assertEqual(reveal.kind, "ended")
        self.assertEqual(table.status, heads_up.STATUS_ENDED)
        self.assertIn("sb", table.public_revealed_roles)

    def test_hidden_fold_commentary_does_not_leak_cards(self) -> None:
        table = self.start_confirmed_table()
        table.apply_action(200, heads_up.ACTION_FOLD, now=1_005.0)

        html = table.render_html()

        self.assertIn("🎙 Вердикт дилера:", html)
        self.assertIn("руки скрыты", html)
        self.assertNotIn("Хороший фолд", html)
        self.assertNotIn("эвакуация", html)

    def test_fold_commentary_mentions_revealed_winner_hand_without_leaking_folder(self) -> None:
        table = heads_up.create_table(
            chat_id=15,
            caller_id=100,
            caller_username="hero",
            caller_name="Hero",
            callee_username="villain",
            now=1_000.0,
            deck=heads_up.stacked_deck(
                {
                    "sb": ("Ah", "Kd"),
                    "bb": ("5d", "6h"),
                    "board": ("8c", "Qh", "7s", "4h", "2c"),
                }
            ),
        )
        table.tap_cards(200, "villain", "Villain", now=1_001.0)
        table.tap_cards(100, "hero", "Hero", now=1_002.0)
        table.apply_action(200, heads_up.ACTION_CALL, now=1_003.0)
        table.apply_action(100, heads_up.ACTION_CHECK, now=1_004.0)
        table.apply_action(100, heads_up.ACTION_CHECK, now=1_005.0)
        table.apply_action(200, heads_up.ACTION_CHECK, now=1_006.0)
        table.apply_action(100, heads_up.ACTION_MIN_RAISE, now=1_007.0)
        table.apply_action(200, heads_up.ACTION_FOLD, now=1_008.0)
        table.choose_public_reveal(100, reveal=True, now=1_009.0)

        html = table.render_html()

        self.assertIn("Hero показал(а): стрит", html)
        self.assertIn("фолд выглядит нормально", html)
        self.assertIn("SB Villain: <i>скрыта", html)
        self.assertNotIn("A", html)
        self.assertNotIn("K", html)
        self.assertNotIn("Фолд принят, но руки скрыты", html)

    def test_revealed_good_fold_gets_credit(self) -> None:
        table = heads_up.create_table(
            chat_id=12,
            caller_id=100,
            caller_username="hero",
            caller_name="Hero",
            callee_username="villain",
            now=1_000.0,
            deck=heads_up.stacked_deck(
                {
                    "sb": ("Ks", "Kd"),
                    "bb": ("Ah", "Ad"),
                    "board": ("2c", "7d", "9h", "Ts", "3c"),
                }
            ),
        )
        table.tap_cards(200, "villain", "Villain", now=1_001.0)
        table.tap_cards(100, "hero", "Hero", now=1_002.0)
        table.apply_action(200, heads_up.ACTION_FOLD, now=1_003.0)
        table.choose_public_reveal(200, reveal=True, now=1_004.0)
        table.choose_public_reveal(100, reveal=True, now=1_005.0)

        html = table.render_html()

        self.assertIn("Хороший фолд", html)
        self.assertIn("санитарная норма", html)

    def test_revealed_bad_fold_gets_called_out(self) -> None:
        table = self.start_confirmed_table()
        table.apply_action(200, heads_up.ACTION_FOLD, now=1_005.0)
        table.choose_public_reveal(200, reveal=True, now=1_006.0)
        table.choose_public_reveal(100, reveal=True, now=1_007.0)

        html = table.render_html()

        self.assertIn("Это был не фолд", html)
        self.assertIn("эвакуация", html)

    def test_all_in_call_auto_runs_remaining_board_to_showdown(self) -> None:
        table = self.start_confirmed_table()

        table.apply_action(200, heads_up.ACTION_ALL_IN, now=1_005.0)
        result = table.apply_action(100, heads_up.ACTION_CALL, now=1_006.0)

        self.assertEqual(result.kind, "showdown")
        self.assertEqual(table.status, heads_up.STATUS_ENDED)
        self.assertEqual(table.street, heads_up.STREET_SHOWDOWN)
        self.assertEqual(table.board, ["2c", "7d", "9h", "Ts", "3c"])
        self.assertEqual(table.winner_role, "sb")
        self.assertEqual(table.players["sb"].stack, 20_000)
        self.assertEqual(table.players["bb"].stack, 0)
        self.assertIsNone(table.mandatory_show_role)
        self.assertEqual(table.public_revealed_roles, {"sb"})
        self.assertEqual(table.optional_reveal_roles(), {"bb"})

    def test_revealed_river_swing_gets_called_out(self) -> None:
        table = heads_up.create_table(
            chat_id=13,
            caller_id=100,
            caller_username="hero",
            caller_name="Hero",
            callee_username="villain",
            now=1_000.0,
            deck=heads_up.stacked_deck(
                {
                    "sb": ("Ah", "Ad"),
                    "bb": ("Ks", "Qs"),
                    "board": ("Kc", "Qd", "2h", "3c", "Ac"),
                }
            ),
        )
        table.tap_cards(200, "villain", "Villain", now=1_001.0)
        table.tap_cards(100, "hero", "Hero", now=1_002.0)
        table.apply_action(200, heads_up.ACTION_ALL_IN, now=1_003.0)
        table.apply_action(100, heads_up.ACTION_CALL, now=1_004.0)
        table.choose_public_reveal(200, reveal=True, now=1_005.0)
        table.choose_public_reveal(100, reveal=True, now=1_006.0)

        html = table.render_html()

        self.assertIn("Ривер переписал результат", html)
        self.assertIn("медицина бессильна", html)

    def test_showdown_revealed_hands_include_hand_names(self) -> None:
        table = heads_up.create_table(
            chat_id=14,
            caller_id=100,
            caller_username="hero",
            caller_name="Hero",
            callee_username="villain",
            now=1_000.0,
            deck=heads_up.stacked_deck(
                {
                    "sb": ("Ah", "Ad"),
                    "bb": ("Ks", "Kd"),
                    "board": ("Ac", "7d", "9h", "Ts", "3c"),
                }
            ),
        )
        table.tap_cards(200, "villain", "Villain", now=1_001.0)
        table.tap_cards(100, "hero", "Hero", now=1_002.0)
        table.apply_action(200, heads_up.ACTION_ALL_IN, now=1_005.0)
        table.apply_action(100, heads_up.ACTION_CALL, now=1_006.0)
        table.choose_public_reveal(200, reveal=True, now=1_007.0)
        table.choose_public_reveal(100, reveal=True, now=1_008.0)

        html = table.render_html()

        self.assertIn("SB Villain:", html)
        self.assertIn("— сет", html)
        self.assertIn("BB Hero:", html)
        self.assertIn("— пара", html)

    def test_all_in_before_river_adds_favorite_line(self) -> None:
        table = self.start_confirmed_table()

        table.apply_action(200, heads_up.ACTION_ALL_IN, now=1_005.0)
        table.apply_action(100, heads_up.ACTION_CALL, now=1_006.0)
        table.choose_public_reveal(200, reveal=True, now=1_007.0)
        table.choose_public_reveal(100, reveal=True, now=1_008.0)

        html = table.render_html()

        self.assertIn("На олл-ине фаворит:", html)
        self.assertIn("Villain", html)
        self.assertIn("%", html)

    def test_completed_table_is_retained_for_final_render_recovery(self) -> None:
        table = self.start_confirmed_table()
        table.apply_action(200, heads_up.ACTION_ALL_IN, now=1_005.0)
        table.apply_action(100, heads_up.ACTION_CALL, now=1_006.0)

        heads_up.cleanup_expired(10, now=1_007.0)

        self.assertIs(heads_up.get_table(10, table.table_id), table)
        self.assertFalse(heads_up.tables_are_full(10, now=1_007.0))
        final_html = table.render_html()
        self.assertIn("Вскрытие", final_html)
        self.assertIn("SB", final_html)
        self.assertIn("BB", final_html)

    def test_showdown_can_tie_and_split_odd_chip_to_small_blind(self) -> None:
        table = heads_up.create_table(
            chat_id=11,
            caller_id=100,
            caller_username="hero",
            caller_name="Hero",
            callee_username="villain",
            now=1_000.0,
            deck=heads_up.stacked_deck(
                {
                    "sb": ("Ah", "2d"),
                    "bb": ("As", "3c"),
                    "board": ("Kc", "Kd", "Kh", "Ks", "Qc"),
                }
            ),
        )
        table.tap_cards(200, "villain", "Villain", now=1_001.0)
        table.tap_cards(100, "hero", "Hero", now=1_002.0)

        table.apply_action(200, heads_up.ACTION_CALL, now=1_005.0)
        table.apply_action(100, heads_up.ACTION_CHECK, now=1_006.0)
        table.apply_action(100, heads_up.ACTION_CHECK, now=1_007.0)
        table.apply_action(200, heads_up.ACTION_CHECK, now=1_008.0)
        table.apply_action(100, heads_up.ACTION_CHECK, now=1_009.0)
        table.apply_action(200, heads_up.ACTION_CHECK, now=1_010.0)
        table.apply_action(100, heads_up.ACTION_CHECK, now=1_011.0)
        result = table.apply_action(200, heads_up.ACTION_CHECK, now=1_012.0)

        self.assertEqual(result.kind, "showdown")
        self.assertIsNone(table.winner_role)
        self.assertEqual(table.players["sb"].stack, 10_000)
        self.assertEqual(table.players["bb"].stack, 10_000)
        self.assertIsNone(table.mandatory_show_role)
        self.assertEqual(table.public_revealed_roles, {"sb", "bb"})
        self.assertEqual(table.optional_reveal_roles(), set())
        self.assertIsNone(table.reply_markup())

    def test_river_aggressor_and_showdown_winner_are_forced_to_show(self) -> None:
        table = self.start_confirmed_table()
        table.apply_action(200, heads_up.ACTION_CALL, now=1_005.0)
        table.apply_action(100, heads_up.ACTION_CHECK, now=1_006.0)
        table.apply_action(100, heads_up.ACTION_CHECK, now=1_007.0)
        table.apply_action(200, heads_up.ACTION_CHECK, now=1_008.0)
        table.apply_action(100, heads_up.ACTION_CHECK, now=1_009.0)
        table.apply_action(200, heads_up.ACTION_CHECK, now=1_010.0)

        table.apply_action(100, heads_up.ACTION_MIN_RAISE, now=1_011.0)
        result = table.apply_action(200, heads_up.ACTION_CALL, now=1_012.0)

        self.assertEqual(result.kind, "showdown")
        self.assertEqual(table.mandatory_show_role, "bb")
        self.assertEqual(table.public_revealed_roles, {"sb", "bb"})
        self.assertEqual(table.optional_reveal_roles(), set())
        html = table.render_html()
        self.assertIn("BB Hero:", html)
        self.assertIn("SB Villain:", html)
        self.assertNotIn("скрыта", html)
        self.assertIsNone(table.reply_markup())

    def test_showdown_winner_must_show_even_when_river_aggressor_loses(self) -> None:
        table = heads_up.create_table(
            chat_id=16,
            caller_id=100,
            caller_username="hero",
            caller_name="Hero",
            callee_username="villain",
            now=1_000.0,
            deck=heads_up.stacked_deck(
                {
                    "sb": ("Kd", "2s"),
                    "bb": ("Ac", "2c"),
                    "board": ("Qh", "8d", "8s", "5c", "Kc"),
                }
            ),
        )
        table.tap_cards(200, "villain", "Villain", now=1_001.0)
        table.tap_cards(100, "hero", "Hero", now=1_002.0)
        table.apply_action(200, heads_up.ACTION_CALL, now=1_003.0)
        table.apply_action(100, heads_up.ACTION_CHECK, now=1_004.0)
        table.apply_action(100, heads_up.ACTION_CHECK, now=1_005.0)
        table.apply_action(200, heads_up.ACTION_CHECK, now=1_006.0)
        table.apply_action(100, heads_up.ACTION_CHECK, now=1_007.0)
        table.apply_action(200, heads_up.ACTION_CHECK, now=1_008.0)
        table.apply_action(100, heads_up.ACTION_MIN_RAISE, now=1_009.0)
        result = table.apply_action(200, heads_up.ACTION_CALL, now=1_010.0)

        self.assertEqual(result.kind, "showdown")
        self.assertEqual(table.winner_role, "sb")
        self.assertEqual(table.public_revealed_roles, {"sb", "bb"})
        self.assertEqual(table.optional_reveal_roles(), set())
        self.assertIsNone(table.reply_markup())

        html = table.render_html()
        self.assertIn("SB Villain:", html)
        self.assertIn("BB Hero:", html)
        self.assertNotIn("не показал", html)
        self.assertNotIn("Руки скрыты", html)

    def test_checked_river_forces_winner_and_allows_loser_to_muck(self) -> None:
        table = self.start_confirmed_table()
        table.apply_action(200, heads_up.ACTION_CALL, now=1_005.0)
        table.apply_action(100, heads_up.ACTION_CHECK, now=1_006.0)
        table.apply_action(100, heads_up.ACTION_CHECK, now=1_007.0)
        table.apply_action(200, heads_up.ACTION_CHECK, now=1_008.0)
        table.apply_action(100, heads_up.ACTION_CHECK, now=1_009.0)
        table.apply_action(200, heads_up.ACTION_CHECK, now=1_010.0)
        table.apply_action(100, heads_up.ACTION_CHECK, now=1_011.0)
        table.apply_action(200, heads_up.ACTION_CHECK, now=1_012.0)

        self.assertIsNone(table.mandatory_show_role)
        self.assertEqual(table.public_revealed_roles, {"sb"})
        self.assertEqual(table.optional_reveal_roles(), {"bb"})
        labels = [
            button.text
            for row in table.reply_markup().inline_keyboard
            for button in row
        ]
        self.assertEqual(labels, ["👀 Показать мои карты", "🤫 Не показывать"])

        winner_muck = table.choose_public_reveal(200, reveal=False, now=1_013.0)
        self.assertEqual(winner_muck.kind, "already_decided")
        loser_muck = table.choose_public_reveal(100, reveal=False, now=1_014.0)
        self.assertEqual(loser_muck.kind, "ended")
        self.assertIn("bb", table.mucked_roles)

    def test_lazy_timeout_removes_tables_after_five_minutes_and_auto_mucks_fold(self) -> None:
        active = heads_up.create_table(10, 100, "hero", "Hero", "villain", now=1_000.0)
        expired = heads_up.create_table(10, 101, "p1", "P1", "p2", now=1_001.0)
        active.updated_at = 1_310.0
        expired.updated_at = 1_302.0

        removed = heads_up.cleanup_expired(10, now=1_603.0)

        self.assertEqual([t.table_id for t in removed], [expired.table_id])
        self.assertEqual([t.table_id for t in heads_up.active_tables(10, now=1_603.0)], [active.table_id])

        heads_up.reset_tables_for_tests()
        table = self.start_confirmed_table()
        table.apply_action(200, heads_up.ACTION_FOLD, now=2_000.0)
        heads_up.cleanup_expired(10, now=2_061.0)
        self.assertEqual(table.status, heads_up.STATUS_ENDED)
        self.assertEqual(table.mucked_roles, set())

    def test_rendering_uses_pretty_russian_streets_board_and_buttons(self) -> None:
        table = self.start_confirmed_table()
        table.apply_action(200, heads_up.ACTION_CALL, now=1_005.0)
        table.apply_action(100, heads_up.ACTION_CHECK, now=1_006.0)

        html = table.render_html()
        markup = table.reply_markup()
        labels = [
            button.text
            for row in markup.inline_keyboard
            for button in row
        ]

        self.assertIn("<blockquote>🂠 Доска:", html)
        self.assertIn("🎲 Улица: <b>Флоп</b>", html)
        self.assertIn("👉 Ход:", html)
        self.assertNotIn("flop", html)
        self.assertIn("🃏 Мои карты", labels)
        self.assertIn("✅ Чек", labels)
        self.assertIn("🚀 Олл-ин 9 900", labels)

    def test_action_log_is_grouped_by_street_with_comments(self) -> None:
        table = self.start_confirmed_table()
        table.apply_action(200, heads_up.ACTION_CALL, now=1_005.0)
        table.apply_action(100, heads_up.ACTION_CHECK, now=1_006.0)
        table.apply_action(100, heads_up.ACTION_CHECK, now=1_007.0)

        html = table.render_html()

        self.assertIn("<b>📜 Ход раздачи:</b>", html)
        self.assertIn("<b>Префлоп:</b>", html)
        self.assertIn("Villain: колл 50", html)
        self.assertIn("<b>Флоп:</b>", html)
        self.assertIn("Флоп сухой. Пока без паники.", html)
        self.assertNotIn("<b>📜 Лог:</b>", html)

    def test_expiry_text_is_restrained(self) -> None:
        table = heads_up.create_table(10, 100, "hero", "Hero", "villain", now=1_000.0)

        table.expire(now=1_700.0)
        html = table.render_html()

        self.assertIn("Игроки думали слишком долго. Раздача закрыта.", html)
        self.assertNotIn("стыда", html)


class EditMessageTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        heads_up.reset_tables_for_tests()

    async def test_message_not_modified_bad_request_is_ignored(self) -> None:
        class FakeQuery:
            async def edit_message_text(self, **kwargs) -> None:
                raise BadRequest(
                    "Message is not modified: specified new message content and reply markup "
                    "are exactly the same as a current content and reply markup of the message"
                )

        table = heads_up.create_table(10, 100, "hero", "Hero", "villain", now=1_000.0)

        await heads_up.edit_query_message(FakeQuery(), table)


if __name__ == "__main__":
    unittest.main()
