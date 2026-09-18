"""Тесты калькулятора долей: цифры из видео + перебор всех небольших комбинаций."""

from __future__ import annotations

import itertools

import pytest

from app.services.weights import (
    WeightItem,
    WeightsError,
    check_distribution,
    equalize,
    rebalance,
    set_share,
)


def items(*spec: tuple[int, bool]) -> list[WeightItem]:
    """spec = (share, pinned); ключ и порядок = позиция в списке."""
    return [WeightItem(key=i, share=s, pinned=p, order=i) for i, (s, p) in enumerate(spec)]


def shares(result: dict[int, int]) -> list[int]:
    return [result[k] for k in sorted(result)]


class TestNumbersFromVideo:
    def test_three_offers_33_33_34(self):
        assert shares(rebalance(items((0, False), (0, False), (0, False)))) == [33, 33, 34]

    def test_four_offers_25_each(self):
        assert shares(rebalance(items(*[(33, False)] * 3, (0, False)))) == [25, 25, 25, 25]

    def test_after_remove_last_added_gets_34(self):
        # было 4 по 25, удалили второй: остались 0009, 11111, 11112 -> 33/33/34
        assert shares(rebalance(items((25, False), (25, False), (25, False)))) == [33, 33, 34]

    def test_pinned_25_and_two_free_37_38(self):
        # Oxys (раньше), 0008 закреплён 25, 0009 возвращён последним -> 37 / 25 / 38
        result = rebalance(
            [
                WeightItem(key=11112, share=25, pinned=False, order=1),
                WeightItem(key=3717, share=25, pinned=True, order=0),
                WeightItem(key=3749, share=0, pinned=False, order=2),
            ]
        )
        assert result == {11112: 37, 3717: 25, 3749: 38}

    def test_single_offer_gets_100(self):
        assert shares(rebalance(items((17, False)))) == [100]

    def test_two_offers_50_50(self):
        assert shares(rebalance(items((100, False), (0, False)))) == [50, 50]


class TestInvariants:
    @pytest.mark.parametrize("n", range(1, 41))
    def test_sum_is_100_and_spread_at_most_one(self, n):
        result = shares(rebalance(items(*[(0, False)] * n)))
        assert sum(result) == 100
        assert max(result) - min(result) <= 1
        assert result == sorted(result), "добавка +1 уходит последним по порядку"

    def test_exhaustive_small_pinned_combinations(self):
        checked = 0
        for n in range(1, 6):
            for mask in itertools.product([False, True], repeat=n):
                n_free = mask.count(False)
                for pinned_share in (1, 10, 25, 33, 50):
                    pinned_sum = pinned_share * mask.count(True)
                    spec = [(pinned_share if p else 7, p) for p in mask]
                    if n_free and 100 - pinned_sum < n_free:
                        with pytest.raises(WeightsError):
                            rebalance(items(*spec))
                        continue
                    if pinned_sum > 100:
                        continue
                    result = rebalance(items(*spec))
                    checked += 1
                    for i, p in enumerate(mask):
                        if p:
                            assert result[i] == pinned_share, "закреплённая доля не меняется"
                        else:
                            assert result[i] >= 1
                    if n_free:
                        assert sum(result.values()) == 100
                        free = [result[i] for i, p in enumerate(mask) if not p]
                        assert max(free) - min(free) <= 1
        assert checked > 100

    def test_order_decides_who_gets_remainder_not_key(self):
        result = rebalance(
            [WeightItem(key=1, share=0, pinned=False, order=9), WeightItem(2, 0, False, 1),
             WeightItem(3, 0, False, 5)]
        )
        assert result == {2: 33, 3: 33, 1: 34}

    def test_does_not_mutate_input(self):
        src = items((10, False), (20, True))
        rebalance(src)
        assert [i.share for i in src] == [10, 20]


class TestPinnedEdgeCases:
    def test_all_pinned_returns_unchanged(self):
        assert shares(rebalance(items((25, True), (25, True)))) == [25, 25]

    def test_pinned_100_with_free_offer_is_error(self):
        with pytest.raises(WeightsError) as err:
            rebalance(items((100, True), (0, False)))
        assert err.value.code == "not_enough_for_free"

    def test_pinned_99_with_two_free_is_error(self):
        with pytest.raises(WeightsError):
            rebalance(items((99, True), (0, False), (0, False)))

    def test_pinned_98_with_two_free_gives_1_1(self):
        assert shares(rebalance(items((98, True), (0, False), (0, False)))) == [98, 1, 1]

    def test_pinned_sum_over_100_is_error(self):
        with pytest.raises(WeightsError):
            rebalance(items((60, True), (60, True), (0, False)))

    def test_pinned_out_of_range(self):
        with pytest.raises(WeightsError) as err:
            rebalance(items((120, True)))
        assert err.value.code == "pinned_out_of_range"

    def test_empty_is_empty(self):
        assert rebalance([]) == {}


class TestSetShare:
    def test_manual_share_pins_and_rebalances_others(self):
        assert shares(set_share(items((34, False), (33, False), (33, False)), 0, 50)) == [50, 25, 25]

    def test_manual_share_keeps_other_pins(self):
        result = set_share(items((25, True), (37, False), (38, False)), 1, 40)
        assert shares(result) == [25, 40, 35]

    @pytest.mark.parametrize("value", [0, -5, 101, 1000])
    def test_out_of_range(self, value):
        with pytest.raises(WeightsError) as err:
            set_share(items((50, False), (50, False)), 0, value)
        assert err.value.code == "share_out_of_range"

    def test_single_offer_only_100_allowed(self):
        assert shares(set_share(items((100, False)), 0, 100)) == [100]
        with pytest.raises(WeightsError) as err:
            set_share(items((100, False)), 0, 60)
        assert err.value.code == "pinned_sum_mismatch"

    def test_no_room_for_others(self):
        with pytest.raises(WeightsError) as err:
            set_share(items((50, False), (50, False)), 0, 100)
        assert err.value.code == "not_enough_for_free"

    def test_unknown_key(self):
        with pytest.raises(WeightsError) as err:
            set_share(items((100, False)), 42, 10)
        assert err.value.code == "unknown_item"


class TestEqualizeAndCheck:
    def test_equalize_drops_pins(self):
        assert shares(equalize(items((70, True), (20, True), (10, False)))) == [33, 33, 34]

    def test_check_ok(self):
        assert check_distribution(items((33, False), (33, False), (34, False))) == []

    def test_check_sum_mismatch_mentions_pins(self):
        problems = check_distribution(items((25, True), (25, True)))
        assert len(problems) == 1 and "50%" in problems[0] and "закреплены" in problems[0]

    def test_check_zero_share_and_empty(self):
        assert any("0%" in p for p in check_distribution(items((100, False), (0, False))))
        assert check_distribution([]) == ["В потоке нет активных офферов."]
