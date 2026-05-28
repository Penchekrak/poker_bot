# Poker Table Asset Notice

Card, card-back, and chip assets in this directory are vendored from:

- Farama Foundation PettingZoo: https://github.com/Farama-Foundation/PettingZoo
- Source paths:
  - `pettingzoo/classic/rlcard_envs/img/*.png`

PettingZoo's repository license states that Farama-owned elements are released under the MIT license. It also notes that the Secret Code font was released under the MIT license by Matthew Welch.

The PettingZoo Texas Hold'em environment attributes the pixel playing-card art to Mariia Khmelnytska:
https://www.123rf.com/photo_104453049_stock-vector-pixel-art-playing-cards-standart-deck-vector-set.html

The table UI combines two bundled Minecraft-style fonts at render time:

- `Minecraft.ttf` renders Latin names, numbers, and punctuation.
- `cyrillic-minecraft-font.ttf` renders Cyrillic names and Russian labels.

These assets are used here only for the public poker-table render.
