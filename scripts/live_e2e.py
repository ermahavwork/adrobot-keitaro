#!/usr/bin/env python3
"""Живая сквозная проверка: сценарий из видео ТЗ на НАСТОЯЩЕМ Keitaro.

Скрипт нажимает «кнопки» AdRobot через его HTTP API и после каждого шага сверяет результат
прямым запросом к Admin API Keitaro — то есть проверяет ровно то, что вы увидели бы в админке.

Запуск (AdRobot должен быть запущен, в .env — адрес трекера и ключ):

    python scripts/live_e2e.py                      # AdRobot на http://127.0.0.1:8000
    python scripts/live_e2e.py --app http://127.0.0.1:8137 --keep

Создаётся одна тестовая кампания `adrobot-e2e-<время>`; в конце она уходит в архив Keitaro
(флаг --keep оставляет её, чтобы посмотреть глазами). Нужны минимум 3 активных оффера.
"""

from __future__ import annotations

import argparse
import os
import sys
import time
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parent.parent
PASSED: list[str] = []
FAILED: list[str] = []


def load_env() -> None:
    """Подхватывает .env из корня проекта (переменные окружения важнее файла)."""
    env_file = Path(os.environ.get("ADROBOT_ENV_FILE", ROOT / ".env"))
    if not env_file.exists():
        return
    for line in env_file.read_text(encoding="utf-8").splitlines():
        if "=" in line and not line.lstrip().startswith("#"):
            key, value = line.split("=", 1)
            os.environ.setdefault(key.strip(), value.strip().strip("'\""))


def check(name: str, condition: bool, details: object = "") -> None:
    (PASSED if condition else FAILED).append(name)
    print(f"  {'OK  ' if condition else 'FAIL'} {name}" + (f"  -> {details}" if not condition else ""))


class Keitaro:
    """Прямой доступ к трекеру — только для сверки и для шага «правка руками в Keitaro»."""

    def __init__(self) -> None:
        base = os.environ["KEITARO_BASE_URL"].rstrip("/")
        self.http = httpx.Client(base_url=f"{base}/admin_api/v1", timeout=30, headers={
            "Api-Key": os.environ["KEITARO_API_KEY"], "User-Agent": "AdRobot-e2e/1.0"})

    def campaign(self, campaign_id: int) -> dict:
        return self.http.get(f"/campaigns/{campaign_id}").raise_for_status().json()

    def streams(self, campaign_id: int) -> list[dict]:
        return self.http.get(f"/campaigns/{campaign_id}/streams").raise_for_status().json()

    def offers(self, stream_id: int) -> dict[int, int]:
        stream = self.http.get(f"/streams/{stream_id}").raise_for_status().json()
        return {o["offer_id"]: o["share"] for o in stream["offers"]}

    def set_offers(self, stream_id: int, offers: dict[int, int]) -> None:
        body = {"offers": [{"offer_id": o, "share": s, "state": "active"} for o, s in offers.items()]}
        self.http.put(f"/streams/{stream_id}", json=body).raise_for_status()


class App:
    def __init__(self, base: str) -> None:
        headers = {"X-AdRobot-User": "live-e2e"}
        if os.environ.get("ADROBOT_AUTH_TOKEN"):
            headers["Authorization"] = f"Bearer {os.environ['ADROBOT_AUTH_TOKEN']}"
        self.http = httpx.Client(base_url=base, timeout=90, headers=headers)

    def call(self, method: str, path: str, body: dict | None = None, expect: int = 200,
             headers: dict | None = None) -> dict:
        response = self.http.request(method, path, json=body, headers=headers)
        if response.status_code != expect:
            raise SystemExit(f"{method} {path}: ожидался {expect}, пришёл {response.status_code}: "
                             f"{response.text[:400]}")
        return response.json()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--app", default="http://127.0.0.1:8000", help="адрес запущенного AdRobot")
    parser.add_argument("--geo", default="AU")
    parser.add_argument("--keep", action="store_true", help="не архивировать тестовую кампанию")
    args = parser.parse_args()
    load_env()
    if not os.environ.get("KEITARO_BASE_URL") or not os.environ.get("KEITARO_API_KEY"):
        raise SystemExit("Нужны KEITARO_BASE_URL и KEITARO_API_KEY (в .env или в окружении).")
    app, kt = App(args.app), Keitaro()

    print("1. Связь и справочники")
    health = app.call("GET", "/api/health")
    check("AdRobot видит Keitaro", health["keitaro"]["reachable"] is True, health)
    offers = app.call("GET", "/api/offers?limit=100")
    check("в трекере есть минимум 3 активных оффера", len(offers) >= 3, len(offers))
    if len(offers) < 3:
        return 1
    a, b, c = (o["id"] for o in offers[:3])
    defaults = app.call("GET", "/api/meta/lookups")["defaults"]

    print("2. Создаватор: имя + гео + оффер")
    name = f"adrobot-e2e-{int(time.time())}"
    body = {"name": name, "geo": args.geo, "offer_id": a}
    plan = app.call("POST", "/api/campaigns", {**body, "dry_run": True})
    check("dry-run ничего не создал", plan["dry_run"] and plan["results"][0]["status"] == "planned")
    key = {"Idempotency-Key": name}
    created = app.call("POST", "/api/campaigns", body, headers=key)["results"][0]
    check("кампания создана", created["status"] == "created", created)
    replay = app.call("POST", "/api/campaigns", body, headers=key)
    check("повтор с тем же ключом не создал дубль", replay.get("replayed") is True)
    campaign_id, kt_id = created["campaign_id"], created["keitaro_campaign_id"]

    try:
        row = kt.campaign(kt_id)
        check("в Keitaro проставлены домен, группа, источник",
              (row["domain_id"], row["group_id"], row["traffic_source_id"]) ==
              (defaults["default_domain_id"], defaults["default_group_id"],
               defaults["default_traffic_source_id"]), row)
        check("параметры источника скопированы в кампанию", bool(row.get("parameters")))
        flow1, flow2 = sorted(kt.streams(kt_id), key=lambda s: s["position"])
        check("Flow 1: страна → редирект на Google",
              flow1["schema"] == "redirect" and flow1["action_payload"] == "https://google.com"
              and [(f["name"], f["mode"], f["payload"]) for f in flow1["filters"]] ==
              [("country", "accept", [args.geo])], flow1)
        check("Flow 2: остальные → оффер 100%", flow2["schema"] == "landings"
              and {o["offer_id"]: o["share"] for o in flow2["offers"]} == {a: 100}, flow2["offers"])
        kt_stream = flow2["id"]
        stream_id = next(s["id"] for s in app.call("GET", f"/api/campaigns/{campaign_id}")["streams"]
                         if s["keitaro_id"] == kt_stream)

        def view() -> dict:
            return app.call("GET", f"/api/streams/{stream_id}")

        def shares() -> dict[int, int]:
            return {o["offer_id"]: o["share"] for o in view()["offers"] if o["state"] == "active"}

        def binding(offer_id: int) -> int:
            return next(o["id"] for o in view()["offers"] if o["offer_id"] == offer_id)

        def post(tail: str, payload: dict | None = None, expect: int = 200) -> dict:
            return app.call("POST", f"/api/streams/{stream_id}{tail}", payload, expect)

        print("3. Add → поток «жёлтый» → Push to KT")
        added = post("/offers", {"offer_id": b})
        check("после Add доли 50/50, поток не опубликован", shares() == {a: 50, b: 50} and added["is_dirty"])
        check("в Keitaro до Push всё по-прежнему", kt.offers(kt_stream) == {a: 100})
        post("/push", {})
        check("после Push в Keitaro 50/50", kt.offers(kt_stream) == {a: 50, b: 50})

        print("4. Cancel")
        post("/offers", {"offer_id": c})
        check("три оффера: 33/33/34, остаток у последнего", shares() == {a: 33, b: 33, c: 34})
        cancelled = post("/cancel")
        check("Cancel вернул 50/50, поток «белый», привязка исчезла",
              shares() == {a: 50, b: 50} and not cancelled["is_dirty"]
              and c not in [o["offer_id"] for o in cancelled["offers"]])

        print("5. Remove → архив → Bring back")
        post("/offers", {"offer_id": c})
        post("/push", {})
        check("в Keitaro 33/33/34", kt.offers(kt_stream) == {a: 33, b: 33, c: 34})
        app.call("DELETE", f"/api/streams/{stream_id}/offers/{binding(b)}")
        check("Remove: 50/50, удалённый с долей 0 в архиве", shares() == {a: 50, c: 50}
              and [o["share"] for o in view()["offers"] if o["state"] == "removed"] == [0])
        post("/push", {})
        check("после Push оффер исчез из Keitaro", kt.offers(kt_stream) == {a: 50, c: 50})
        post(f"/offers/{binding(b)}/bring-back")
        check("Bring back: возвращённый встал последним и получил остаток",
              shares() == {a: 33, c: 33, b: 34})
        post("/push", {})
        check("оффер вернулся в Keitaro", kt.offers(kt_stream) == {a: 33, c: 33, b: 34})

        print("6. Правка руками в Keitaro → Fetch streams again")
        kt.set_offers(kt_stream, {c: 33, b: 34})
        fetched = app.call("POST", f"/api/campaigns/{campaign_id}/fetch", {})
        check("исчезнувший оффер ушёл в архив, доли НЕ пересчитаны",
              fetched["result"]["archived_offers"] == [a] and shares() == {c: 33, b: 34}
              and not view()["is_dirty"])

        print("7. Pin")
        app.call("PUT", f"/api/streams/{stream_id}/offers/{binding(b)}/pin", {"pinned": True})
        check("pin не меняет цифры и не «желтит» поток",
              shares() == {c: 33, b: 34} and not view()["is_dirty"])
        post(f"/offers/{binding(a)}/bring-back")
        check("закреплённые 34% не тронуты, остальные делят остаток 33/33",
              shares() == {b: 34, c: 33, a: 33})
        post("/push", {})
        in_kt = kt.offers(kt_stream)
        check("в Keitaro те же цифры, сумма 100",
              in_kt == {b: 34, c: 33, a: 33} and sum(in_kt.values()) == 100, in_kt)

        print("8. Конфликт: поток поменяли в Keitaro, пока у нас черновик")
        app.call("PUT", f"/api/streams/{stream_id}/offers/{binding(a)}/share", {"share": 40})
        kt.set_offers(kt_stream, {b: 50, c: 50})
        conflict = post("/push", {}, expect=409)
        check("Push остановлен с кодом conflict", conflict["error"]["code"] == "conflict")
        check("чужие правки в Keitaro не затёрты", kt.offers(kt_stream) == {b: 50, c: 50})
        app.call("POST", f"/api/campaigns/{campaign_id}/fetch", {"discard_draft": True})
        check("Fetch со сбросом черновика зеркалит Keitaro",
              shares() == {b: 50, c: 50} and not view()["is_dirty"])

        print("9. Откат к снимку и защита от мусора")
        snapshots = app.call("GET", f"/api/streams/{stream_id}/snapshots")
        check("снимки перед публикациями сохраняются", len(snapshots) >= 4, len(snapshots))
        bad = post("/offers", {"offer_id": 999999999}, expect=422)
        check("несуществующий оффер отклонён (Keitaro принял бы молча)",
              bad["error"]["code"] == "offer_not_usable")
        log = app.call("GET", f"/api/operations?keitaro_campaign_id={kt_id}&limit=100")
        check("все операции записаны в журнал", log["total"] >= 15, log["total"])
    finally:
        if args.keep:
            print(f"Кампания оставлена: {created['admin_url']}")
        else:
            app.call("DELETE", f"/api/campaigns/{campaign_id}")
            print("Тестовая кампания отправлена в архив Keitaro.")

    print(f"\nИтог: {len(PASSED)} проверок пройдено, {len(FAILED)} провалено.")
    for name in FAILED:
        print(f"  провал: {name}")
    return 1 if FAILED else 0


if __name__ == "__main__":
    sys.exit(main())
