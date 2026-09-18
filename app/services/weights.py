"""Пересчёт долей (share) офферов в потоке — «чтобы цифры были верные».

Правила сняты с оригинального AdRobot (видео из ТЗ) и проверены на его цифрах:

1. Доли активных офферов в сумме дают ровно 100.
2. Закреплённая (pinned) доля при пересчёте не меняется никогда.
3. Остаток `100 - сумма закреплённых` делится между незакреплёнными поровну, нацело.
4. То, что не поделилось нацело, получают по +1 последние по порядку активации офферы
   (новый и возвращённый через Bring back оффер встаёт в конец):
   3 оффера → 33/33/34; закреплено 25 и два свободных → 37/38.
5. Удалённый оффер получает 0 и в расчёте не участвует.

Модуль чистый: ни базы, ни сети — только арифметика, поэтому покрыт тестами перебором.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass

TOTAL = 100


class WeightsError(ValueError):
    """Распределить доли по правилам невозможно; `code` — машинный код причины."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass(frozen=True)
class WeightItem:
    """Активный оффер потока глазами калькулятора."""

    key: int  # любой уникальный идентификатор (id привязки или оффера)
    share: int  # текущая доля
    pinned: bool  # закреплена ли доля
    order: int  # порядок активации: больше = позже встал в поток


def rebalance(items: Sequence[WeightItem]) -> dict[int, int]:
    """Возвращает новые доли `{key: share}` для всех переданных активных офферов.

    Закреплённые доли возвращаются как есть. Если свободных офферов нет, доли не меняются
    (сумму в этом случае проверяет `check_distribution`).
    """
    pinned = [item for item in items if item.pinned]
    free = sorted((item for item in items if not item.pinned), key=lambda i: (i.order, i.key))
    result = {item.key: item.share for item in pinned}

    for item in pinned:
        if not 0 <= item.share <= TOTAL:
            raise WeightsError("pinned_out_of_range", f"Закреплённая доля {item.share}% вне 0–100%.")

    if not free:
        return result

    remaining = TOTAL - sum(item.share for item in pinned)
    if remaining < len(free):
        raise WeightsError(
            "not_enough_for_free",
            f"Закреплено {TOTAL - remaining}% — на {len(free)} незакреплённых оффер(ов) "
            f"остаётся {max(remaining, 0)}%, каждому нужно хотя бы по 1%. "
            "Уменьшите закреплённую долю или снимите закрепление.",
        )

    base, extra = divmod(remaining, len(free))
    for index, item in enumerate(free):
        gets_extra = index >= len(free) - extra
        result[item.key] = base + (1 if gets_extra else 0)
    return result


def set_share(items: Sequence[WeightItem], key: int, value: int) -> dict[int, int]:
    """Ручной ввод доли: оффер `key` получает `value` и закрепляется, остальные пересчитываются.

    Закрепление обязательно: иначе следующий же пересчёт затёр бы введённое руками число.
    """
    if not 1 <= value <= TOTAL:
        raise WeightsError("share_out_of_range", "Доля должна быть целым числом от 1 до 100.")
    if key not in {item.key for item in items}:
        raise WeightsError("unknown_item", "Оффер не найден среди активных офферов потока.")
    updated = [
        WeightItem(i.key, value, True, i.order) if i.key == key else i for i in items
    ]
    others_free = [i for i in updated if not i.pinned]
    if not others_free:
        total = sum(i.share for i in updated)
        if total != TOTAL:
            raise WeightsError(
                "pinned_sum_mismatch",
                f"Все офферы закреплены, сумма получится {total}%, а нужно {TOTAL}%. "
                "Снимите закрепление хотя бы с одного оффера.",
            )
    return rebalance(updated)


def equalize(items: Iterable[WeightItem]) -> dict[int, int]:
    """«Выровнять»: снять все закрепления и поделить 100% поровну."""
    return rebalance([WeightItem(i.key, i.share, False, i.order) for i in items])


def check_distribution(items: Sequence[WeightItem]) -> list[str]:
    """Проблемы текущего распределения (пустой список = всё хорошо). Нужен перед Push."""
    if not items:
        return ["В потоке нет активных офферов."]
    problems: list[str] = []
    total = sum(item.share for item in items)
    if total != TOTAL:
        hint = ""
        if all(item.pinned for item in items):
            hint = " Все офферы закреплены — снимите закрепление или нажмите «Выровнять»."
        problems.append(f"Сумма долей {total}%, а должна быть {TOTAL}%.{hint}")
    zero = [item for item in items if item.share <= 0]
    if zero:
        problems.append(f"У {len(zero)} активных оффер(ов) доля 0% — трафик на них не пойдёт.")
    return problems
