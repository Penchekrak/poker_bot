# Main Board Caption Board State Design

## Goal

Add a written board-state line to the Telegram poker room's main board photo caption so players can read the current community cards without relying only on the rendered table image.

## Scope

The change applies to the main poker room render caption built by `poker_room_handlers._caption`. It does not change the game engine, persisted room state, table PNG rendering, dealer commentary, or final hand summary messages.

## User-Facing Behavior

Every main board message caption shows a `Доска:` line immediately after `Улица:`.

Before the flop, the line reads `Доска: пока пусто`.

After community cards are dealt, the line shows the current board using the existing card display style, such as `Доска: 2♣ 7♦ 9♥`. The line updates on the same edit/send cycle already used by `_render_room`, so it appears on new street messages and routine message edits.

## Architecture

`poker_room_handlers._caption` remains the only caption composer for the main board message. A small helper formats `hand.board` for caption use, importing the existing `cards.format_card_html` function so card symbols match the rest of the bot's HTML output.

The helper returns italic HTML text for the empty preflop state and joins formatted card HTML for non-empty boards. `_caption` includes that helper output without additional escaping because `format_card_html` already emits trusted markup from engine-owned card strings.

## Testing

Add focused handler tests for `_caption`:

- A preflop hand caption contains `Доска: <i>пока пусто</i>`.
- A postflop caption contains a written board line with formatted community cards.

These tests verify the public text produced for Telegram without exercising the full photo-render path.
