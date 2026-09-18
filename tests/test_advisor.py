"""Советник долей: предохранители, сумма 100, честное «рано», неприкосновенность закреплённых."""

from __future__ import annotations

import itertools

import pytest

from app.services.advisor import AdvisorInput, advise


def offer(key, share, clicks, conversions, pinned=False, revenue=0.0):
    return AdvisorInput(key=key, share=share, pinned=pinned, clicks=clicks,
                        conversions=conversions, revenue=revenue)


def proposed(result):
    return {item.key: item.proposed for item in result.items}


class TestReadiness:
    def test_too_few_clicks_means_wait(self):
        result = advise([offer(1, 50, 20, 5), offer(2, 50, 30, 1)])
        assert not result.ready and "рано" in result.message.lower()
        assert result.items == []

    def test_no_conversions_at_all(self):
        result = advise([offer(1, 50, 500, 0), offer(2, 50, 500, 0)])
        assert not result.ready and "нет ни конверсий" in result.message

    def test_single_free_offer_has_nothing_to_split(self):
        result = advise([offer(1, 50, 500, 50, pinned=True), offer(2, 50, 500, 5)])
        assert not result.ready


class TestProposal:
    def test_better_offer_gets_more_but_step_is_limited(self):
        result = advise([offer(1, 50, 1000, 100), offer(2, 50, 1000, 10)])
        assert result.ready and result.changed
        assert proposed(result) == {1: 65, 2: 35}, "шаг ±15 п. за один раз"

    def test_floor_keeps_weak_offer_alive(self):
        result = advise([offer(1, 90, 5000, 900), offer(2, 10, 5000, 1)], max_step=100)
        assert proposed(result)[2] >= 5, "без трафика статистика оффера застыла бы навсегда"
        assert proposed(result)[1] <= 80, "потолок 80%"
        assert sum(proposed(result).values()) == 100

    def test_pinned_share_is_untouched_and_rest_sums_to_100(self):
        result = advise([offer(1, 25, 800, 10, pinned=True), offer(2, 37, 800, 80),
                         offer(3, 38, 800, 8)])
        shares = proposed(result)
        assert shares[1] == 25
        assert sum(shares.values()) == 100
        assert shares[2] > 37 > shares[3]

    def test_thin_sample_is_not_trusted_and_not_moved(self):
        # «25% CR» на 20 кликах — шум: такой оффер не получает прибавку и не теряет долю
        result = advise([offer(1, 40, 1000, 100), offer(2, 40, 1000, 50), offer(3, 20, 20, 5)])
        assert result.ready
        assert proposed(result)[3] == 20
        assert "выборка мала" in next(i.reason for i in result.items if i.key == 3)
        assert proposed(result)[1] > 40 > proposed(result)[2]
        assert sum(proposed(result).values()) == 100

    def test_only_one_offer_has_enough_data(self):
        result = advise([offer(1, 50, 2000, 100), offer(2, 50, 3, 3)])
        assert not result.ready and "меньше чем по двум" in result.message

    def test_moderate_sample_is_pulled_to_average(self):
        # 40 кликов: участвует, но оценка стянута к средней по потоку и помечена
        result = advise([offer(1, 50, 2000, 100), offer(2, 50, 40, 20)])
        assert "мало данных" in next(i.reason for i in result.items if i.key == 2)
        assert proposed(result)[2] <= 65

    def test_epc_metric_uses_revenue(self):
        result = advise([offer(1, 50, 1000, 10, revenue=500.0), offer(2, 50, 1000, 50, revenue=50.0)],
                        metric="epc")
        assert result.metric == "epc" and proposed(result)[1] > proposed(result)[2]

    def test_equal_offers_stay_equal(self):
        result = advise([offer(1, 50, 1000, 50), offer(2, 50, 1000, 50)])
        assert proposed(result) == {1: 50, 2: 50} and not result.changed

    def test_unknown_metric_falls_back_to_cr(self):
        assert advise([offer(1, 50, 500, 50), offer(2, 50, 500, 5)], metric="bogus").metric == "cr"


class TestInvariants:
    @pytest.mark.parametrize("n", [2, 3, 4, 7, 12])
    def test_sum_is_always_100_and_bounds_hold(self, n):
        base, extra = divmod(100, n)
        shares = [base + (1 if i >= n - extra else 0) for i in range(n)]
        for pattern in itertools.islice(itertools.product([1, 20, 90], repeat=n), 60):
            items = [offer(i, shares[i], 1000, conv) for i, conv in enumerate(pattern)]
            result = advise(items)
            assert result.ready
            values = proposed(result)
            assert sum(values.values()) == 100
            assert all(value >= 1 for value in values.values())

    def test_does_not_mutate_input_and_is_deterministic(self):
        items = [offer(1, 60, 900, 90), offer(2, 40, 900, 30)]
        first, second = advise(items), advise(items)
        assert proposed(first) == proposed(second)
        assert [i.share for i in items] == [60, 40]

    def test_many_pins_leave_small_budget(self):
        result = advise([offer(1, 48, 900, 9, pinned=True), offer(2, 48, 900, 90, pinned=True),
                         offer(3, 2, 900, 90), offer(4, 2, 900, 9)])
        values = proposed(result)
        assert values[1] == 48 and values[2] == 48
        assert values[3] + values[4] == 4 and min(values[3], values[4]) >= 1


class TestNeverHangs:
    """Регрессия: при долях-«весах» из Keitaro (сумма ≠ 100) подгонка зацикливалась и вешала сервер."""

    def test_unbalanced_shares_are_refused_not_looped(self):
        result = advise([offer(1, 40, 500, 50, pinned=True), offer(2, 40, 500, 5, pinned=True),
                         offer(3, 40, 500, 50), offer(4, 40, 500, 5)])
        assert not result.ready and "160%" in result.message and "Пересчитать" in result.message

    def test_fit_terminates_when_shares_start_outside_bounds(self):
        from app.services.advisor import _fit
        shares, relaxed = _fit({1: 10.0, 2: 10.0}, {1: (25, 55), 2: (25, 55)}, 20, [1, 2])
        assert sum(shares.values()) == 20 and relaxed and min(shares.values()) >= 1

    def test_fit_reports_impossible_budget_instead_of_spinning(self):
        from app.services.advisor import _fit
        with pytest.raises(ValueError):
            _fit({1: 1.0, 2: 1.0}, {1: (1, 1), 2: (1, 1)}, 1, [1, 2])

    @pytest.mark.parametrize("seed", range(40))
    def test_fuzz_always_returns(self, seed):
        import random
        rng = random.Random(seed)  # noqa: S311 - воспроизводимый фазз, не криптография
        n = rng.randint(2, 8)
        cuts = sorted(rng.sample(range(1, 100), n - 1))
        shares = [b - a for a, b in zip([0, *cuts], [*cuts, 100], strict=True)]
        items = [offer(i, shares[i], rng.choice([0, 5, 40, 3000]), rng.randint(0, 40),
                       pinned=rng.random() < 0.3, revenue=rng.uniform(-50, 500)) for i in range(n)]
        for metric in ("cr", "epc"):
            result = advise(items, metric=metric)
            if result.ready:
                assert sum(i.proposed for i in result.items) == 100
                assert all(i.proposed >= 1 for i in result.items)
