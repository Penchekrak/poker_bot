from __future__ import annotations

import unittest
import time

import heads_up
import handlers


class FakeMessage:
    def __init__(self, text: str = "") -> None:
        self.text = text
        self.entities = []
        self.reply_texts: list[str] = []
        self.reply_htmls: list[str] = []

    async def reply_text(self, text: str) -> None:
        self.reply_texts.append(text)

    async def reply_html(self, text: str) -> None:
        self.reply_htmls.append(text)


class FakeUpdate:
    def __init__(self, chat_id: int, message: FakeMessage) -> None:
        self.effective_chat = type("Chat", (), {"id": chat_id})()
        self.effective_message = message


class FakeContext:
    def __init__(self) -> None:
        self.bot = type("Bot", (), {"username": "pokerbot", "id": 999})()


class ExistingHandlersBusyTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        heads_up.reset_tables_for_tests()
        now = time.time()
        heads_up.create_table(10, 100, "hero", "Hero", "villain", now=now)
        heads_up.create_table(10, 101, "p1", "P1", "p2", now=now + 1.0)

    async def test_aces_command_replies_dealers_busy_when_tables_are_full(self) -> None:
        message = FakeMessage()
        update = FakeUpdate(10, message)

        await handlers.aces_command(update, FakeContext())

        self.assertEqual(message.reply_texts, ["все дилеры заняты"])
        self.assertEqual(message.reply_htmls, [])

    async def test_mention_replies_dealers_busy_when_tables_are_full(self) -> None:
        message = FakeMessage("@pokerbot")
        update = FakeUpdate(10, message)

        await handlers.on_mention(update, FakeContext())

        self.assertEqual(message.reply_texts, ["все дилеры заняты"])
        self.assertEqual(message.reply_htmls, [])


if __name__ == "__main__":
    unittest.main()
