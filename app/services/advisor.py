"""Советник долей: предлагает, как перераспределить трафик между офферами по статистике.

Это ПОДСКАЗКА, а не автопилот: советник ничего не публикует и даже не меняет черновик сам.
Пользователь видит таблицу «сейчас → предлагается → почему» и решает, применять ли.
У самого Keitaro авто-оптимизации долей нет — проценты там ставят руками.

Как считается предложение:

1. Оценка оффера — CR (конверсии/клики) или EPC (выручка/клик), сглаженная к среднему по
   потоку: `(успехи + среднее * K) / (клики + K)`. Оффер с 3 кликами и 1 конверсией не
   получит «33% CR» — при малой выборке оценка почти равна средней по потоку.
2. Целевые доли пропорциональны оценкам.
3. Предохранители: каждому офферу не меньше `floor`% (иначе он перестанет получать трафик
   и его статистика навсегда застынет), не больше `cap`%, и за один раз доля меняется
   не больше чем на `max_step` пунктов — без резких рывков.
4. Закреплённые (pin) доли не трогаются. Итог всегда даёт ровно 100.

Если данных мало (`min_clicks` на весь поток), советник честно говорит «рано» и
ничего не предлагает.
"""

from __future__ import annotations

from dataclasses import dataclass, field

TOTAL = 100
SMOOTHING_CLICKS = 50  # K: «вес» среднего по потоку в оценке каждого оффера


@dataclass(frozen=True)
class AdvisorInput:
    key: int  # id привязки
    share: int  # текущая доля
    pinned: bool
    clicks: int = 0
    conversions: int = 0
    revenue: float = 0.0


@dataclass
class Advice:
    key: int
    current: int
    proposed: int
    score: float  # сглаженная оценка (CR в % или EPC)
    reason: str


@dataclass
class AdvisorResult:
    ready: bool
    message: str
    metric: str
    items: list[Advice] = field(default_factory=list)

    @property
    def changed(self) -> bool:
        return any(item.proposed != item.current for item in self.items)


def _smoothed(successes: float, clicks: int, pooled_rate: float) -> float:
    return (successes + pooled_rate * SMOOTHING_CLICKS) / (clicks + SMOOTHING_CLICKS)


def _fit(raw: dict[int, float], bounds: dict[int, tuple[int, int]], budget: int,
         priority: list[int]) -> tuple[dict[int, int], bool]:
    """Целые доли как можно ближе к `raw`, строго в границах и с суммой ровно `budget`.

    Сначала округляем и вписываем в границы, затем добираем или снимаем по одному пункту:
    прибавляем в порядке `priority` (лучшие офферы первыми), убавляем — в обратном. Если
    границы шага несовместны с бюджетом, они ослабляются до [1, budget] — сумма важнее шага.
    """
    shares = {key: min(max(round(value), bounds[key][0]), bounds[key][1]) for key, value in raw.items()}
    diff = budget - sum(shares.values())
    relaxed = False
    while diff != 0:
        order = priority if diff > 0 else priority[::-1]
        step = 1 if diff > 0 else -1
        moved = False
        for key in order:
            low, high = bounds[key]
            # Сравниваем с той границей, К КОТОРОЙ идём: доля, стоящая за пределами коридора
            # (такое приходит из Keitaro — сумму долей он не проверяет), всё равно должна
            # уметь двигаться внутрь. Проверка «low <= доля+шаг <= high» тут зацикливалась.
            if (step > 0 and shares[key] < high) or (step < 0 and shares[key] > low):
                shares[key] += step
                diff -= step
                moved = True
                if diff == 0:
                    break
        if not moved:
            if relaxed:  # двигаться некуда даже в самых широких границах — бюджет недостижим
                raise ValueError(f"не удаётся распределить {budget}% между {len(shares)} офферами")
            bounds, relaxed = dict.fromkeys(bounds, (1, budget)), True
    return shares, relaxed


def advise(items: list[AdvisorInput], *, metric: str = "cr", floor: int = 5, cap: int = 80,
           max_step: int = 15, min_clicks: int = 100, min_offer_clicks: int = 30) -> AdvisorResult:
    """Предложение по долям активных офферов потока. `metric`: "cr" или "epc"."""
    metric = metric if metric in ("cr", "epc") else "cr"
    current_total = sum(item.share for item in items)
    if items and current_total != TOTAL:
        # Такое зеркалится из Keitaro (сумму долей он не проверяет). Советовать поверх кривой
        # базы нельзя: сначала её надо привести к 100% обычной кнопкой.
        return AdvisorResult(False, f"Сейчас сумма долей {current_total}%, а не 100%. Сначала нажмите "
                                    "«Пересчитать» или «Выровнять поровну», потом зовите советника.", metric)
    total_clicks = sum(item.clicks for item in items)
    if total_clicks < min_clicks and sum(1 for item in items if not item.pinned) >= 2:
        return AdvisorResult(
            False, f"Пока рано: за период {total_clicks} кликов на поток, для совета нужно от "
                   f"{min_clicks}. Оставьте доли поровну и дайте трафику накопиться.", metric)
    # Оффер с малой выборкой не двигаем вовсе: «5 конверсий на 20 кликах» — это шум, а не CR 25%.
    # Для расчёта он ведёт себя как закреплённый; доля вернётся в игру, когда накопятся клики.
    thin = {item.key for item in items if not item.pinned and item.clicks < min_offer_clicks}
    free = [item for item in items if not item.pinned and item.key not in thin]
    if len(free) < 2:
        reason = (f"Данных хватает меньше чем по двум офферам (нужно от {min_offer_clicks} кликов на оффер) "
                  "— сравнивать пока нечего." if thin else
                  "Нужно хотя бы два незакреплённых оффера — иначе делить нечего.")
        return AdvisorResult(False, reason, metric)
    # Отрицательная выручка (возвраты) не должна переворачивать сравнение — считаем её нулём.
    successes = {i.key: max(0.0, float(i.conversions) if metric == "cr" else float(i.revenue))
                 for i in items}
    pooled = sum(successes.values()) / total_clicks if total_clicks else 0.0
    if pooled <= 0:
        return AdvisorResult(False, "За период нет ни конверсий, ни выручки — сравнивать офферы не по чему.",
                             metric)

    scores = {i.key: _smoothed(successes[i.key], i.clicks, pooled) for i in free}
    budget = TOTAL - sum(i.share for i in items if i.pinned or i.key in thin)
    if budget < len(free):
        return AdvisorResult(False, "Закреплено слишком много: незакреплённым офферам не хватает "
                                    "даже по 1%. Снимите часть закреплений.", metric)
    floor = min(floor, budget // len(free))
    raw = {key: score / sum(scores.values()) * budget for key, score in scores.items()}
    bounds = {i.key: (max(floor, i.share - max_step, 1), max(min(cap, i.share + max_step), floor, 1))
              for i in free}
    priority = sorted(scores, key=lambda key: (-scores[key], key))
    shares, relaxed = _fit(raw, bounds, budget, priority)

    result = []
    for item in items:
        if item.pinned:
            result.append(Advice(item.key, item.share, item.share, 0.0, "доля закреплена — не трогаем"))
            continue
        if item.key in thin:
            result.append(Advice(item.key, item.share, item.share, 0.0,
                                 f"всего {item.clicks} кликов — выборка мала, долю не трогаем"))
            continue
        score = scores[item.key] * (100 if metric == "cr" else 1)
        label = f"CR {score:.2f}%" if metric == "cr" else f"EPC {score:.3f}"
        sample = ("мало данных, оценка близка к средней" if item.clicks < SMOOTHING_CLICKS
                  else "выборка достаточная")
        delta = shares[item.key] - item.share
        move = "без изменений" if delta == 0 else (f"+{delta} п." if delta > 0 else f"{delta} п.")
        result.append(Advice(item.key, item.share, shares[item.key], round(score, 4),
                             f"{label} при {item.clicks} кликах ({sample}); {move}"))
    message = (f"Предложение готово. Каждому офферу оставлено не меньше {floor}%. "
               + ("Ограничение шага пришлось ослабить: иначе доли не сходились к 100%."
                  if relaxed else f"Шаг ограничен ±{max_step} п."))
    return AdvisorResult(True, message, metric, result)
