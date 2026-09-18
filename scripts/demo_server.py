#!/usr/bin/env python3
"""Демо-режим: AdRobot на встроенном эмуляторе Keitaro — без трекера, ключа и интернета.

    python scripts/demo_server.py            # http://127.0.0.1:8001
    python scripts/demo_server.py --port 9000

Эмулятор — тот же, на котором идут автотесты (`tests/fake_keitaro.py`): он повторяет поведение
настоящего Admin API, включая его «причуды». В нём уже лежит кампания из видео ТЗ
(Flow 1: AU → Google, Flow 2: три оффера 33/33/34), так что весь сценарий редактора можно
прощёлкать сразу. Данные живут в памяти и в файле `data/demo.sqlite3`; перезапуск начинает с нуля.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import uvicorn  # noqa: E402
from pydantic import SecretStr  # noqa: E402

from app import db  # noqa: E402
from app.config import Settings  # noqa: E402
from app.main import create_app  # noqa: E402
from tests.fake_keitaro import API_KEY, FakeKeitaro  # noqa: E402

DEMO_OFFERS = [
    (101, "Aurora Serum [BEAUTY-RO] — spin"),
    (102, "Aurora Serum [BEAUTY-RO] — prize"),
    (103, "FitoTea [WEIGHT-CO]"),
    (104, "Oxy Cream [BEAUTY-CL]"),
    (105, "KetoPlan [DIET-MX]"),
    (106, "SleepWell [HEALTH-AU]"),
]


def demo_report(fake: FakeKeitaro, body: dict) -> list[dict]:
    """Правдоподобная статистика для колонок Stats/Trends: одна и та же при каждом запуске."""
    import datetime as dt

    campaign_id = next((f.get("expression") for f in body.get("filters", [])
                        if f.get("name") == "campaign_id"), None)
    by_day = "day" in body.get("dimensions", [])
    today = dt.datetime.now(dt.timezone.utc).date()
    start = dt.date.fromisoformat(body.get("range", {}).get("from") or today.isoformat())
    days = max(1, (today - start).days + 1)
    rows = []
    for stream in fake.streams.values():
        if stream["campaign_id"] != campaign_id:
            continue
        for offer in stream["offers"]:
            seed = offer["offer_id"] * 7 + stream["id"]
            daily = [(seed * (n + 3)) % 23 + offer["share"] // 4 for n in range(days)]
            if by_day:
                rows += [{"day": (today - dt.timedelta(days=days - 1 - n)).isoformat(),
                          "stream_id": stream["id"], "offer_id": offer["offer_id"],
                          "clicks": clicks, "conversions": clicks // 9} for n, clicks in enumerate(daily)]
            else:
                clicks, conversions = sum(daily), sum(c // 9 for c in daily)
                rows.append({"stream_id": stream["id"], "offer_id": offer["offer_id"], "clicks": clicks,
                             "campaign_unique_clicks": int(clicks * 0.8), "conversions": conversions,
                             "revenue": conversions * 12.5, "epc": round(conversions * 12.5 / clicks, 2),
                             "cr": round(conversions * 100 / clicks, 1) if clicks else 0})
    return rows


def build_fake() -> FakeKeitaro:
    fake = FakeKeitaro()
    fake.report_handler = lambda body: demo_report(fake, body)
    fake.offers.clear()
    for offer_id, name in DEMO_OFFERS:
        fake.add_offer(offer_id, name)
    fake.add_offer(199, "Старый оффер (в архиве)", state="deleted")
    fake.seed_campaign("campaign 2", offers=[(101, 33), (102, 33), (103, 34)])
    fake.seed_campaign("Spring sale [MX]", offers=[(105, 100)])
    return fake


async def reset_database(database_url: str) -> None:
    engine = db.create_engine(database_url)
    async with engine.begin() as connection:
        await connection.run_sync(db.Base.metadata.drop_all)
        await connection.run_sync(db.Base.metadata.create_all)
    await engine.dispose()


def main() -> None:
    parser = argparse.ArgumentParser(description="AdRobot в демо-режиме (эмулятор Keitaro)")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8001)
    args = parser.parse_args()

    database_url = f"sqlite+aiosqlite:///{ROOT / 'data' / 'demo.sqlite3'}"
    settings = Settings(
        _env_file=None,
        keitaro_base_url="https://tracker.example.com",
        keitaro_api_key=SecretStr(API_KEY),
        database_url=database_url,
        tracking_domain_url="https://go.example.com",
        dictionary_ttl_seconds=5,
    )
    asyncio.run(reset_database(database_url))
    db.override_engine(db.create_engine(database_url))
    app = create_app(settings, transport=build_fake().transport())
    print(f"Демо-режим: http://{args.host}:{args.port}")
    print("Keitaro эмулируется, данные сбрасываются при каждом старте.")
    uvicorn.run(app, host=args.host, port=args.port, log_level="warning")


if __name__ == "__main__":
    main()
