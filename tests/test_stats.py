"""Колонки Stats/Trends: `/api/campaigns/{id}/stats` поверх отчётов Keitaro.

Сервис делает два запроса `/report/build` подряд: итоги (stream_id + offer_id) и тренд по дням
(day + stream_id + offer_id). Эмулятор на оба отвечает одним и тем же `report_rows`, поэтому
там, где важно различать ответы, они подкладываются по очереди через `fail_next(status=200)`:
первый вызов получает итоги, второй — тренд, как у настоящего трекера.
"""

from __future__ import annotations

import datetime as dt
from typing import Any

import httpx
import pytest

from tests.conftest import Editor
from tests.fake_keitaro import FakeKeitaro

A_0009, A_0008, FITO, OXYS = 3749, 3717, 11111, 11112
REPORT = r"^/report/build$"


def day(days_ago: int = 0) -> str:
    today = dt.datetime.now(dt.timezone.utc).date()
    return (today - dt.timedelta(days=days_ago)).isoformat()


def feed(fake: FakeKeitaro, totals: list[Any], trend: list[Any]) -> None:
    """Первый `/report/build` (итоги) получит `totals`, второй (тренд) — `trend`."""
    fake.fail_next("POST", REPORT, status=200, body={"rows": totals})
    fake.fail_next("POST", REPORT, status=200, body={"rows": trend})


def stats(editor: Editor, expect: int = 200, **params: str) -> dict[str, Any]:
    response = editor.client.get(f"/api/campaigns/{editor.campaign_id}/stats", params=params)
    assert response.status_code == expect, response.text
    return response.json()


def report_requests(fake: FakeKeitaro) -> list[dict[str, Any]]:
    return [body for method, path, body in fake.requests if (method, path) == ("POST",
                                                                                "/report/build")]


@pytest.fixture
def flow1(editor: Editor) -> int:
    """Keitaro-ID гео-потока (без офферов) той же кампании."""
    return next(s["id"] for s in editor.fake.streams.values() if s["schema"] == "redirect")


class TestTotals:
    def test_offer_and_stream_totals(self, editor, flow1):
        flow2 = editor.kt_stream_id
        feed(editor.fake, totals=[
            {"stream_id": flow1, "offer_id": 0, "clicks": 40, "conversions": 0},
            {"stream_id": flow2, "offer_id": A_0009, "clicks": 100, "campaign_unique_clicks": 80,
             "conversions": 5, "revenue": 12.5, "cr": 5.0, "epc": 0.125},
            {"stream_id": flow2, "offer_id": A_0008, "clicks": 60, "campaign_unique_clicks": 55,
             "conversions": 1, "revenue": 3, "cr": 1.67, "epc": 0.05},
        ], trend=[])
        body = stats(editor)
        assert body["available"] is True and "reason" not in body
        assert body["offers"] == {
            f"{flow2}:{A_0009}": {"clicks": 100, "unique_clicks": 80, "conversions": 5,
                                  "revenue": 12.5, "cr": 5.0, "epc": 0.125, "trend": [0] * 7},
            f"{flow2}:{A_0008}": {"clicks": 60, "unique_clicks": 55, "conversions": 1,
                                  "revenue": 3.0, "cr": 1.67, "epc": 0.05, "trend": [0] * 7},
        }
        assert body["streams"] == {str(flow1): {"clicks": 40}, str(flow2): {"clicks": 160}}

    @pytest.mark.parametrize("offer_id", [0, None, "", "absent"])
    def test_row_without_offer_counts_for_stream_only(self, editor, flow1, offer_id):
        row = {"stream_id": flow1, "clicks": 9}
        if offer_id != "absent":
            row["offer_id"] = offer_id
        feed(editor.fake, totals=[row], trend=[])
        body = stats(editor)
        assert body["offers"] == {} and body["streams"] == {str(flow1): {"clicks": 9}}

    def test_same_offer_in_two_streams_is_kept_apart(self, editor, flow1):
        flow2 = editor.kt_stream_id
        feed(editor.fake, totals=[{"stream_id": flow1, "offer_id": A_0009, "clicks": 1},
                                  {"stream_id": flow2, "offer_id": A_0009, "clicks": 2}], trend=[])
        offers = stats(editor)["offers"]
        assert (offers[f"{flow1}:{A_0009}"]["clicks"], offers[f"{flow2}:{A_0009}"]["clicks"]) == \
            (1, 2)

    def test_numbers_come_as_strings_nulls_and_garbage(self, editor):
        flow2 = editor.kt_stream_id
        feed(editor.fake, totals=[{
            "stream_id": flow2, "offer_id": A_0009, "clicks": "12", "campaign_unique_clicks": None,
            "conversions": "abc", "revenue": "7.123456789", "cr": [], "epc": {"value": 1}}],
            trend=[])
        assert stats(editor)["offers"][f"{flow2}:{A_0009}"] == {
            "clicks": 12, "unique_clicks": 0, "conversions": 0, "revenue": 7.1235, "cr": 0.0,
            "epc": 0.0, "trend": [0] * 7}

    @pytest.mark.parametrize("rows", [None, [], ["junk", 5, None, ["nested"]]])
    def test_empty_or_junk_rows_give_empty_stats(self, editor, rows):
        editor.fake.fail_next("POST", REPORT, status=200, body={"rows": rows}, times=2)
        body = stats(editor)
        assert body["available"] is True and body["offers"] == {} and body["streams"] == {}

    def test_report_is_asked_for_this_campaign_only(self, editor):
        kt_campaign = next(iter(editor.fake.campaigns))
        editor.fake.requests.clear()
        stats(editor)
        totals, trend = report_requests(editor.fake)
        expected_filter = [{"name": "campaign_id", "operator": "EQUALS", "expression": kt_campaign}]
        assert totals["filters"] == trend["filters"] == expected_filter
        assert totals["dimensions"] == ["stream_id", "offer_id"]
        assert trend["dimensions"] == ["day", "stream_id", "offer_id"]
        assert {"clicks", "conversions", "revenue", "cr", "epc"} <= set(totals["measures"])

    def test_unknown_campaign_is_404_and_keitaro_is_not_asked(self, client, fake):
        response = client.get("/api/campaigns/999/stats")
        assert response.status_code == 404
        assert response.json()["error"]["code"] == "campaign_not_found"
        assert report_requests(fake) == []

    def test_reading_stats_leaves_no_trace_in_journal_or_draft(self, editor):
        before = editor.client.get("/api/operations").json()["total"]
        stats(editor)
        assert editor.client.get("/api/operations").json()["total"] == before
        assert editor.stream["is_dirty"] is False


class TestTrend:
    def test_clicks_are_laid_out_by_day(self, editor):
        flow2 = editor.kt_stream_id
        today, yesterday, two_ago, five_ago = day(0), day(1), day(2), day(5)
        feed(editor.fake,
             totals=[{"stream_id": flow2, "offer_id": A_0009, "clicks": 100},
                     {"stream_id": flow2, "offer_id": A_0008, "clicks": 7}],
             trend=[{"day": today, "stream_id": flow2, "offer_id": A_0009, "clicks": 60},
                    {"day": yesterday, "stream_id": flow2, "offer_id": A_0009, "clicks": 30},
                    {"day": five_ago, "stream_id": flow2, "offer_id": A_0009, "clicks": 10},
                    {"day": two_ago, "stream_id": flow2, "offer_id": A_0008, "clicks": 7}])
        body = stats(editor)
        days = body["days"]
        assert days == sorted(days) and len(days) == 7 and days[-1] >= today
        first = body["offers"][f"{flow2}:{A_0009}"]["trend"]
        assert (first[days.index(today)], first[days.index(yesterday)],
                first[days.index(five_ago)], sum(first)) == (60, 30, 10, 100)
        second = body["offers"][f"{flow2}:{A_0008}"]["trend"]
        assert second[days.index(two_ago)] == 7 and sum(second) == 7

    def test_day_with_time_part_is_understood(self, editor):
        flow2, yesterday = editor.kt_stream_id, day(1)
        feed(editor.fake, totals=[{"stream_id": flow2, "offer_id": A_0009, "clicks": 5}],
             trend=[{"day": f"{yesterday} 00:00:00", "stream_id": flow2, "offer_id": A_0009,
                     "clicks": 5}])
        body = stats(editor)
        assert body["offers"][f"{flow2}:{A_0009}"]["trend"][body["days"].index(yesterday)] == 5

    def test_rows_that_do_not_fit_are_ignored(self, editor):
        flow2 = editor.kt_stream_id
        feed(editor.fake, totals=[{"stream_id": flow2, "offer_id": A_0009, "clicks": 1}],
             trend=[{"day": day(40), "stream_id": flow2, "offer_id": A_0009, "clicks": 999},
                    {"day": "вчера", "stream_id": flow2, "offer_id": A_0009, "clicks": 999},
                    {"day": None, "stream_id": flow2, "offer_id": A_0009, "clicks": 999},
                    {"stream_id": flow2, "offer_id": A_0009, "clicks": 999},
                    {"day": day(0), "stream_id": flow2, "offer_id": 424242, "clicks": 999},
                    "junk"])
        body = stats(editor)
        assert list(body["offers"]) == [f"{flow2}:{A_0009}"], "оффера без итогов в ответе нет"
        assert body["offers"][f"{flow2}:{A_0009}"]["trend"] == [0] * 7

    def test_archived_offer_still_gets_its_numbers(self, editor):
        editor.remove(FITO)
        editor.push()
        flow2 = editor.kt_stream_id
        feed(editor.fake, totals=[{"stream_id": flow2, "offer_id": FITO, "clicks": 15}], trend=[])
        assert stats(editor)["offers"][f"{flow2}:{FITO}"]["clicks"] == 15


class TestSameRowsForBothRequests:
    """Эмулятор без подсказок: `report_rows` уходит и на итоги, и на тренд."""

    def test_rows_without_day_fill_totals_and_leave_trend_flat(self, editor, flow1):
        flow2 = editor.kt_stream_id
        editor.fake.report_rows = [
            {"stream_id": flow1, "offer_id": 0, "clicks": 40},
            {"stream_id": flow2, "offer_id": A_0009, "clicks": 100, "campaign_unique_clicks": 80,
             "conversions": 5, "revenue": 12.5, "cr": 5.0, "epc": 0.125},
        ]
        body = stats(editor)
        assert body["offers"][f"{flow2}:{A_0009}"] == {
            "clicks": 100, "unique_clicks": 80, "conversions": 5, "revenue": 12.5, "cr": 5.0,
            "epc": 0.125, "trend": [0] * 7}
        assert body["streams"] == {str(flow1): {"clicks": 40}, str(flow2): {"clicks": 100}}

    def test_rows_with_day_are_laid_out_into_trend_without_crash(self, editor):
        flow2, today, yesterday = editor.kt_stream_id, day(0), day(1)
        editor.fake.report_rows = [
            {"day": yesterday, "stream_id": flow2, "offer_id": A_0009, "clicks": 40,
             "conversions": 1},
            {"day": today, "stream_id": flow2, "offer_id": A_0009, "clicks": 60, "conversions": 2},
        ]
        body = stats(editor)
        trend = body["offers"][f"{flow2}:{A_0009}"]["trend"]
        assert (trend[body["days"].index(yesterday)], trend[body["days"].index(today)]) == (40, 60)
        assert sum(trend) == 100


class TestPeriods:
    @pytest.mark.parametrize(("period", "days_count"), [("today", 1), ("7d", 7), ("30d", 30)])
    def test_period_maps_to_explicit_dates(self, editor, period, days_count):
        """Итоги и тренд запрашиваются за ОДНИ И ТЕ ЖЕ даты (интервал Keitaro вида 7_days_ago
        захватывал бы лишний день, и сумма тренда не сходилась бы с итогом)."""
        editor.fake.requests.clear()
        today = day(0)
        body = stats(editor, period=period)
        assert body["period"] == period and len(body["days"]) == days_count
        assert body["days"][-1] >= today and body["days"] == sorted(set(body["days"]))
        assert [r["range"] for r in report_requests(editor.fake)] == \
            [{"from": body["days"][0], "to": body["days"][-1], "timezone": "UTC"}] * 2

    def test_week_is_the_default(self, editor):
        editor.fake.requests.clear()
        body = stats(editor)
        assert body["period"] == "7d" and len(body["days"]) == 7
        assert report_requests(editor.fake)[0]["range"]["from"] == body["days"][0]

    @pytest.mark.parametrize("period", ["мусор", "", "7D", "365d", "<script>", "1_year_ago"])
    def test_unknown_period_falls_back_to_week(self, editor, period):
        editor.fake.requests.clear()
        body = stats(editor, period=period)
        assert body["available"] is True and len(body["days"]) == 7
        assert {r["range"]["from"] for r in report_requests(editor.fake)} == {body["days"][0]}

    def test_trend_length_follows_the_period(self, editor):
        flow2, long_ago = editor.kt_stream_id, day(28)
        feed(editor.fake, totals=[{"stream_id": flow2, "offer_id": A_0009, "clicks": 3}],
             trend=[{"day": long_ago, "stream_id": flow2, "offer_id": A_0009, "clicks": 3}])
        body = stats(editor, period="30d")
        trend = body["offers"][f"{flow2}:{A_0009}"]["trend"]
        assert len(trend) == 30 and trend[body["days"].index(long_ago)] == 3 and sum(trend) == 3


class TestReportsUnavailable:
    def test_forbidden_reports_mean_dashes_not_errors(self, editor):
        editor.fake.fail_next("POST", REPORT, status=403, body={"error": "Reports are not allowed"})
        body = stats(editor)
        assert body["available"] is False
        assert "Reports are not allowed" in body["reason"]
        assert body["offers"] == {} and body["streams"] == {} and len(body["days"]) == 7

    def test_editor_keeps_working_when_reports_are_forbidden(self, editor):
        editor.fake.fail_next("POST", REPORT, status=403, body={"error": "no access"}, times=50)
        assert stats(editor)["available"] is False
        editor.add(OXYS)
        editor.push()
        assert editor.in_keitaro() == {A_0009: 25, A_0008: 25, FITO: 25, OXYS: 25}

    def test_trend_failure_does_not_leave_half_an_answer(self, editor):
        flow2 = editor.kt_stream_id
        editor.fake.fail_next("POST", REPORT, status=200,
                              body={"rows": [{"stream_id": flow2, "offer_id": A_0009,
                                              "clicks": 5}]})
        editor.fake.fail_next("POST", REPORT, status=500, body="boom")
        body = stats(editor)
        assert body["available"] is False and body["offers"] == {}

    def test_network_failure_is_retried_then_reported_as_unavailable(self, editor):
        editor.fake.requests.clear()
        editor.fake.fail_next("POST", REPORT, times=50,
                              exception=lambda r: httpx.ConnectError("down", request=r))
        body = stats(editor)
        assert body["available"] is False and "Нет связи" in body["reason"]
        assert len(report_requests(editor.fake)) == 3, "отчёт — чтение: 1 попытка + 2 повтора"

    def test_short_glitch_is_healed_by_retry(self, editor):
        flow2 = editor.kt_stream_id
        editor.fake.report_rows = [{"stream_id": flow2, "offer_id": A_0009, "clicks": 5}]
        editor.fake.fail_next("POST", REPORT, status=502, body="bad gateway")
        body = stats(editor)
        assert body["available"] is True and body["offers"][f"{flow2}:{A_0009}"]["clicks"] == 5

    @pytest.mark.parametrize("body", ["<html>login</html>", [1, 2], "null", "42"])
    def test_unexpected_report_body_is_unavailable_not_500(self, editor, body):
        editor.fake.fail_next("POST", REPORT, status=200, body=body, times=2)
        assert stats(editor)["available"] is False

    def test_cloudflare_block_is_explained_in_reason(self, editor):
        editor.fake.fail_next("POST", REPORT, status=403, body=editor.fake.cloudflare_body())
        assert "Cloudflare" in stats(editor)["reason"]

    @pytest.mark.parametrize(("field", "value"), [
        ("clicks", "NaN"), ("clicks", "1e999"), ("clicks", "Infinity"), ("conversions", "-inf"),
        ("revenue", "nan"), ("cr", "inf"), ("epc", "-Infinity"), ("clicks", 10**400),
    ])
    def test_not_finite_numbers_do_not_crash_stats(self, editor, field, value):
        # Регрессия: float("NaN") и float("1e999") не бросают исключений, а int(nan)/int(inf)
        # падал → 500; nan в revenue/cr/epc уходил во фронтенд как null.
        flow2 = editor.kt_stream_id
        feed(editor.fake, totals=[{"stream_id": flow2, "offer_id": A_0009, "clicks": 1,
                                   field: value}], trend=[])
        body = stats(editor)
        assert body["offers"][f"{flow2}:{A_0009}"][field] == 0

    def test_not_finite_clicks_in_trend_are_zero(self, editor):
        flow2, today = editor.kt_stream_id, day(0)
        feed(editor.fake, totals=[{"stream_id": flow2, "offer_id": A_0009, "clicks": 1}],
             trend=[{"day": today, "stream_id": flow2, "offer_id": A_0009, "clicks": "NaN"}])
        assert stats(editor)["offers"][f"{flow2}:{A_0009}"]["trend"] == [0] * 7
