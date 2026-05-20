"""Короткие подсказки по текстуре руки (для подписи в боте)."""

from __future__ import annotations

from cards import RANKS


def _rank_idx(rank: str) -> int:
    return RANKS.index(rank)


def hints_for_hand(hand: tuple[str, str]) -> list[str]:
    c1, c2 = hand
    r1, s1 = c1[0], c1[1]
    r2, s2 = c2[0], c2[1]
    i1, i2 = _rank_idx(r1), _rank_idx(r2)
    high, low = (r1, s1, i1), (r2, s2, i2)
    if i1 < i2:
        low, high = high, low
    rh, _, ih = high
    _, _, il = low
    suited = s1 == s2
    pair = r1 == r2
    out: list[str] = []

    if pair:
        if ih <= 5:
            out.append(
                "Малая пара: основное усиление — тройка; без неё рука редко достаточно сильна."
            )
        elif ih <= 8:
            out.append(
                "Средняя пара: возможна старшая пара на флопе; оценивайте общие карты и оппонентов."
            )
        else:
            out.append(
                "Высокая пара: нередко старшая пара на флопе; размер ставок согласуйте с опасностью текстуры."
            )
        if ih >= RANKS.index("T"):
            out.append(
                "При низкой и несвязанной текстуре общих карт старшая пара сохраняет силу дольше."
            )
        return out[:4]

    # Разница по индексу: 1 = коннектор, 2 = one-gapper, 3 = two-gapper…
    rank_diff = ih - il
    broadway = ih >= RANKS.index("T") and il >= RANKS.index("T")
    wheelish = il <= RANKS.index("5") and rank_diff <= 4

    if rh == "A":
        if il >= RANKS.index("T"):
            out.append(
                "Туз с высоким кикером: к числу сильнейших неспаренных стартовых рук."
            )
        elif il < RANKS.index("9"):
            out.append(
                "Туз с низким кикером: на связанных и координированных текстурах растёт риск доминирования."
            )
        else:
            out.append(
                "Туз со средним кикером: относительная сила определяется текстурой и глубиной раздачи."
            )
    elif broadway:
        out.append(
            "Обе карты старше десятки: возможны старшие пары и стриты; учитывайте доминирование по кикеру."
        )

    if suited:
        if rh == "A":
            out.append(
                "Одномастные карты с тузом: наиболее полный потенциал флеша из двух стартовых карт."
            )
        elif ih >= RANKS.index("J"):
            out.append(
                "Одномастные старшие карты: выраженный потенциал флеша и дополнительных дро на следующих улицах."
            )
        else:
            out.append(
                "Одномастность у младших рангов: потенциал флеша ниже; ценность выше в позиции и в глубокой раздаче."
            )

    if rank_diff == 1:
        out.append(
            "Смежные по достоинству карты: повышенная вероятность стрита и неполного стрит-дро."
        )
    elif rank_diff == 2:
        out.append(
            "Разрыв в один ранг: неполное стрит-дро; вероятность усиления сильно зависит от общих карт."
        )
    elif rank_diff == 3:
        out.append(
            "Разрыв в два ранга: стрит реже; исход сильнее зависит от текстуры флопа."
        )
    elif rank_diff <= 5 and not pair:
        out.append(
            "Большой разрыв по рангам: рука для избирательного входа; требуется согласованная линия игры."
        )
    elif rank_diff > 5 and not pair:
        out.append(
            "Очень большой разрыв: основная ценность в паре либо в редких сильных комбинациях."
        )

    if wheelish and not suited:
        out.append(
            "Низкие разномастные карты: потенциал для представления силы ограничен; важны позиция и последовательность решений."
        )

    if not out:
        out.append(
            "Низкая префлоп-сила: сужайте диапазон входа с учётом позиции и соотношения шансов банка к ставке."
        )

    seen: set[str] = set()
    uniq: list[str] = []
    for line in out:
        if line not in seen:
            seen.add(line)
            uniq.append(line)
    return uniq[:4]
