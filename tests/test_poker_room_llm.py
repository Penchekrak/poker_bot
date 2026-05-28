from __future__ import annotations

import asyncio
import json
import os
import unittest
from unittest.mock import patch

import poker_room
import poker_room_llm


def _run(awaitable):
    return asyncio.get_event_loop().run_until_complete(awaitable) if False else asyncio.run(awaitable)


class PokerRoomLlmTests(unittest.TestCase):
    def make_hand(self) -> poker_room.PokerHand:
        room = poker_room.PokerRoom(now=1_000.0)
        room.confirm_room_intent(1, "alice", "Alice", poker_room.ROOM_JOIN, now=1_000.0)
        room.confirm_room_intent(2, "bob", "Bob", poker_room.ROOM_JOIN, now=1_001.0)
        return room.start_hand(
            now=1_010.0,
            deck=poker_room.stacked_deck(
                {
                    1: ("Ah", "Ad"),
                    2: ("Kh", "Kd"),
                    "board": ("2c", "7d", "9h", "Ts", "3c"),
                }
            ),
        )

    def test_parser_payload_contains_public_state_but_no_private_cards_or_deck(self) -> None:
        hand = self.make_hand()

        payload = poker_room_llm.build_parser_payload(
            hand,
            actor_message="ну давай рейз 500",
            recent_public_messages=[("Bob", "не верю тебе")],
        )
        raw = json.dumps(payload, ensure_ascii=False)

        self.assertIn("legal_actions", raw)
        self.assertIn("ну давай рейз 500", raw)
        self.assertNotIn("Ah", raw)
        self.assertNotIn("Ad", raw)
        self.assertNotIn("Kh", raw)
        self.assertNotIn("Kd", raw)
        self.assertNotIn("deck", raw.lower())

    def test_deterministic_fallback_parses_exact_actions(self) -> None:
        hand = self.make_hand()

        self.assertEqual(
            _run(poker_room_llm.parse_with_fallback("fold", hand, client=None)).action,
            poker_room.PlayerAction("fold"),
        )
        self.assertEqual(
            _run(poker_room_llm.parse_with_fallback("all in", hand, client=None)).action,
            poker_room.PlayerAction("all_in"),
        )
        self.assertEqual(
            _run(poker_room_llm.parse_with_fallback("raise to 500", hand, client=None)).action,
            poker_room.PlayerAction("raise_to", 500),
        )
        self.assertEqual(
            _run(poker_room_llm.parse_with_fallback("raise 500", hand, client=None)).action,
            poker_room.PlayerAction("raise_ambiguous", 500),
        )

    def test_valid_llm_json_is_schema_validated_into_action(self) -> None:
        hand = self.make_hand()

        class FakeClient:
            def complete_json(self, payload, tools=None, tool_choice=None):
                self.tools = tools
                self.tool_choice = tool_choice
                return {"kind": "poker_action", "action": "call", "confidence": 0.91}

        client = FakeClient()
        parsed = _run(poker_room_llm.parse_with_fallback("ладно доставлю", hand, client=client))

        self.assertEqual(parsed.kind, "poker_action")
        self.assertEqual(parsed.action, poker_room.PlayerAction("call"))
        self.assertGreaterEqual(parsed.confidence, 0.9)
        self.assertEqual(client.tools[0]["function"]["name"], "submit_poker_intent")

    def test_room_intent_llm_parses_free_form_join_without_hand_context(self) -> None:
        room = poker_room.PokerRoom(now=1_000.0)

        class FakeClient:
            def complete_json(self, payload, tools=None, tool_choice=None):
                raw = json.dumps(payload, ensure_ascii=False)
                self.raw = raw
                self.tools = tools
                return {
                    "kind": "room_intent",
                    "room_intent": poker_room.ROOM_JOIN,
                    "confidence": 0.87,
                    "reason": "player wants to sit",
                }

        client = FakeClient()
        parsed = _run(poker_room_llm.parse_room_intent_with_fallback(
            "я бы присел к вам за стол",
            room,
            client=client,
            recent_public_messages=[("Bob", "садись")],
        ))

        self.assertEqual(parsed.kind, "room_intent")
        self.assertEqual(parsed.room_intent, poker_room.ROOM_JOIN)
        self.assertGreater(parsed.confidence, 0.8)
        self.assertEqual(client.tools[0]["function"]["name"], "submit_room_intent")
        self.assertNotIn("deck", client.raw.lower())

    def test_room_intent_deterministic_fallback_keeps_exact_commands(self) -> None:
        room = poker_room.PokerRoom(now=1_000.0)

        class FailingClient:
            def complete_json(self, payload, tools=None, tool_choice=None):
                raise AssertionError("exact room commands should not call LLM")

        parsed = _run(poker_room_llm.parse_room_intent_with_fallback("сяду", room, client=FailingClient()))

        self.assertEqual(parsed.kind, "room_intent")
        self.assertEqual(parsed.room_intent, poker_room.ROOM_JOIN)

    def test_tool_call_arguments_are_extracted_from_openai_response(self) -> None:
        message = {
            "role": "assistant",
            "content": None,
            "tool_calls": [
                {
                    "type": "function",
                    "function": {
                        "name": "submit_poker_intent",
                        "arguments": "{\"kind\":\"poker_action\",\"action\":\"raise_to\",\"amount\":500,\"confidence\":0.93}",
                    },
                }
            ],
        }

        parsed = poker_room_llm.json_from_openai_message(message)

        self.assertEqual(parsed["kind"], "poker_action")
        self.assertEqual(parsed["action"], "raise_to")
        self.assertEqual(parsed["amount"], 500)

    def test_invalid_llm_json_uses_deterministic_fallback(self) -> None:
        hand = self.make_hand()

        class BrokenClient:
            def complete_json(self, payload):
                return {"kind": "poker_action", "action": "teleport_chips"}

        parsed = _run(poker_room_llm.parse_with_fallback("call", hand, client=BrokenClient()))

        self.assertEqual(parsed.kind, "poker_action")
        self.assertEqual(parsed.action, poker_room.PlayerAction("call"))

    def test_client_reads_timeout_from_environment(self) -> None:
        with patch.dict(os.environ, {"LLM_TIMEOUT_SECONDS": "42"}, clear=False):
            client = poker_room_llm.OpenAIJsonClient()

        self.assertEqual(client.timeout, 42.0)

    def test_commentary_payload_never_includes_hole_cards(self) -> None:
        hand = self.make_hand()
        hand.apply_action(1, poker_room.PlayerAction("all_in"), now=1_011.0)
        hand.apply_action(2, poker_room.PlayerAction("call"), now=1_012.0)

        payload = poker_room_llm.build_commentary_payload(hand, "showdown", [])
        raw = json.dumps(payload, ensure_ascii=False)

        self.assertIn("showdown", raw)
        self.assertIn("board", raw)
        self.assertNotIn("Ah", raw)
        self.assertNotIn("Ad", raw)
        self.assertNotIn("Kh", raw)
        self.assertNotIn("Kd", raw)

    def test_commentary_uses_llm_when_valid_and_fallback_when_invalid(self) -> None:
        hand = self.make_hand()

        class GoodClient:
            def complete_json(self, payload, tools=None, tool_choice=None):
                self.tools = tools
                return {"commentary": "Дилер посмотрел на банк и сделал вид, что так и надо."}

        class ResponseClient:
            def complete_json(self, payload):
                return {"response": "Дилер принял реплику под другим ключом, но без лишней драмы."}

        class BadClient:
            def complete_json(self, payload):
                return {"commentary": ""}

        good = GoodClient()
        self.assertIn("Дилер посмотрел", _run(poker_room_llm.generate_dealer_commentary("flop", hand, client=good)))
        self.assertEqual(good.tools[0]["function"]["name"], "submit_dealer_commentary")
        self.assertIn("другим ключом", _run(poker_room_llm.generate_dealer_commentary("flop", hand, client=ResponseClient())))
        self.assertIn("Дилер", _run(poker_room_llm.generate_dealer_commentary("flop", hand, client=BadClient())))

    def test_async_client_is_awaited_when_present(self) -> None:
        hand = self.make_hand()
        calls: dict[str, list] = {"async": [], "sync": []}

        class AsyncClient:
            async def complete_json_async(self, payload, tools=None, tool_choice=None):
                calls["async"].append((tools, tool_choice))
                return {"kind": "poker_action", "action": "fold", "confidence": 0.9}

            def complete_json(self, payload, tools=None, tool_choice=None):
                calls["sync"].append((tools, tool_choice))
                raise AssertionError("sync path should not be used when async is available")

        parsed = _run(poker_room_llm.parse_with_fallback("сброс", hand, client=AsyncClient()))

        self.assertEqual(parsed.action, poker_room.PlayerAction("fold"))
        self.assertEqual(len(calls["async"]), 1)
        self.assertEqual(calls["sync"], [])


if __name__ == "__main__":
    unittest.main()
