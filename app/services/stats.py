"""Колонки Stats и Trends редактора: клики/конверсии по офферам потока из отчётов Keitaro.

Статистика — украшение, а не основа: если ключу API отчёты недоступны или Keitaro
ответил ошибкой, редактор продолжает работать, а в колонках показывается прочерк.
"""

from __future__ import annotations

import datetime as dt
import logging
import math
from typing import Any

from app.keitaro.client import KeitaroClient
from app.keitaro.errors import KeitaroError

logger = logging.getLogger(__name__)

PERIODS = {"today": 1, "7d": 7, "30d": 30}  # период → сколько дней показывать, считая сегодня
_MEASURES = ["clicks", "campaign_unique_clicks", "conversions", "revenue", "cr", "epc"]


def _number(value: Any) -> float:
    try:
        number = float(value or 0)
    except (TypeError, ValueError, OverflowError):
        return 0.0
    # "NaN" и "1e999" float() принимает молча, а int() на nan/inf падает, и в JSON их не передать.
    return round(number, 4) if math.isfinite(number) else 0.0


def _filters(keitaro_campaign_id: int) -> list[dict[str, Any]]:
    return [{"name": "campaign_id", "operator": "EQUALS", "expression": keitaro_campaign_id}]


async def campaign_stats(
    client: KeitaroClient, keitaro_campaign_id: int, period: str = "7d"
) -> dict[str, Any]:
    """Итоги и тренд по дням для каждого (поток, оффер) кампании.

    Ответ: `{"available": bool, "period": ..., "days": [...],
             "offers": {"<stream_id>:<offer_id>": {clicks, conversions, ..., "trend": [...]}},
             "streams": {"<stream_id>": {"clicks": n}}}`
    """
    period = period if period in PERIODS else "7d"  # мусор в параметре не возвращаем как есть
    days_count = PERIODS[period]
    today = dt.datetime.now(dt.timezone.utc).date()
    days = [(today - dt.timedelta(days=offset)).isoformat()
            for offset in range(days_count - 1, -1, -1)]
    result: dict[str, Any] = {"available": True, "period": period, "days": days,
                              "offers": {}, "streams": {}}
    # Явные даты вместо интервалов Keitaro вида `7_days_ago`: тот захватывает лишний день,
    # и итог по офферу перестаёт сходиться с суммой тренда по дням.
    report_range = {"from": days[0], "to": days[-1], "timezone": "UTC"}
    try:
        totals = await client.build_report({
            "range": report_range, "dimensions": ["stream_id", "offer_id"],
            "measures": _MEASURES, "filters": _filters(keitaro_campaign_id)})
        trend = await client.build_report({
            "range": report_range, "dimensions": ["day", "stream_id", "offer_id"],
            "measures": ["clicks", "conversions"], "filters": _filters(keitaro_campaign_id)})
    except KeitaroError as exc:
        logger.info("статистика кампании %s недоступна: %s", keitaro_campaign_id, exc.message)
        return {**result, "available": False, "reason": exc.message}

    for row in totals.get("rows") or []:
        if not isinstance(row, dict):
            continue
        stream_id, offer_id = row.get("stream_id"), row.get("offer_id")
        clicks = int(_number(row.get("clicks")))
        stream_total = result["streams"].setdefault(str(stream_id), {"clicks": 0})
        stream_total["clicks"] += clicks
        if not offer_id:
            continue
        result["offers"][f"{stream_id}:{offer_id}"] = {
            "clicks": clicks,
            "unique_clicks": int(_number(row.get("campaign_unique_clicks"))),
            "conversions": int(_number(row.get("conversions"))),
            "revenue": _number(row.get("revenue")),
            "cr": _number(row.get("cr")),
            "epc": _number(row.get("epc")),
            "trend": [0] * len(days),
        }
    day_index = {day: index for index, day in enumerate(days)}
    for row in trend.get("rows") or []:
        if not isinstance(row, dict):
            continue
        key = f"{row.get('stream_id')}:{row.get('offer_id')}"
        index = day_index.get(str(row.get("day"))[:10])
        if key in result["offers"] and index is not None:
            result["offers"][key]["trend"][index] = int(_number(row.get("clicks")))
    return result
