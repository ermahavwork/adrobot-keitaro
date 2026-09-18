"""Гонки: одновременные запросы к одному приложению.

Запросы идут через `httpx.AsyncClient` + `ASGITransport` в ОДНОМ цикле событий
(`asyncio.gather`) — так же, как внутри одного воркера uvicorn. У эмулятора Keitaro ответ
мгновенный и синхронный, поэтому «сети» добавлены настоящие точки переключения задач:

* крошечная задержка перед каждым ответом — без неё запросы физически не переплетаются;
* `Network.hold(...)` — ворота на выбранном сетевом вызове: первые запросы ждут остальных
  (не дольше 50 мс), чтобы гонка воспроизводилась детерминированно, а не «как повезёт».
"""

from __future__ import annotations

import asyncio
import contextlib
import re
from collections import Counter
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

import httpx

from app import db
from app.main import create_app
from tests.conftest import make_settings
from tests.fake_keitaro import FakeKeitaro

A_0009, A_0008, FITO, OXYS, SPARE = 3749, 3717, 11111, 11112, 13972
RACE_BODY = {"name": "Race", "geo": "AU", "offer_id": A_0009}


class Network:
    """«Сеть» между приложением и эмулятором Keitaro."""

    def __init__(self, fake: FakeKeitaro, latency: float = 0.001) -> None:
        self._inner = fake.transport()
        self._latency = latency
        self._gate: dict[str, Any] | None = None

    def hold(self, method: str, pattern: str, parties: int, patience: float = 0.05) -> None:
        """Ближайшие `parties` запросов `method pattern` получат ответ одновременно."""
        self._gate = {"method": method, "pattern": re.compile(pattern), "parties": parties,
                      "patience": patience, "waiting": 0, "open": asyncio.Event()}

    async def __call__(self, request: httpx.Request) -> httpx.Response:
        gate = self._gate
        path = request.url.path.removeprefix("/admin_api/v1")
        if (gate and not gate["open"].is_set() and request.method == gate["method"]
                and gate["pattern"].search(path)):
            gate["waiting"] += 1
            if gate["waiting"] >= gate["parties"]:
                gate["open"].set()
            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(gate["open"].wait(), gate["patience"])
            gate["open"].set()  # не дождались остальных — дальше никого не держим
        else:
            await asyncio.sleep(self._latency)
        return self._inner.handle_request(request)


Scenario = Callable[[httpx.AsyncClient, Network], Awaitable[Any]]


def serve(fake: FakeKeitaro, tmp_path: Path, scenario: Scenario, *, network: Any = None,
          **overrides: Any) -> Any:
    """Поднимает приложение на временной базе и выполняет сценарий в одном цикле событий.

    `network` — свой обработчик «сети» вместо `Network` (для особо хитрой хореографии).
    """
    settings = make_settings(tmp_path, **overrides)

    async def main() -> Any:
        nonlocal network
        engine = db.create_engine(settings.database_url)
        async with engine.begin() as connection:
            await connection.run_sync(db.Base.metadata.create_all)
        db.override_engine(engine)
        network = network or Network(fake)
        app = create_app(settings, transport=httpx.MockTransport(network))
        try:
            async with app.router.lifespan_context(app):
                # 500 нужен нам ответом, а не исключением: гонка обычно выглядит именно так.
                transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
                async with httpx.AsyncClient(transport=transport,
                                             base_url="http://adrobot.test") as http:
                    return await scenario(http, network)
        finally:
            db.override_engine(None)
            await engine.dispose()

    return asyncio.run(main())


async def open_campaign(http: httpx.AsyncClient, kt_campaign: int,
                        kt_stream: int) -> tuple[int, int]:
    """Открывает кампанию в редакторе и делает Fetch. -> (локальный ID кампании, ID потока)."""
    opened = await http.post(f"/api/campaigns/open/{kt_campaign}")
    assert opened.status_code == 200, opened.text
    campaign = opened.json()["id"]
    fetched = await http.post(f"/api/campaigns/{campaign}/fetch", json={})
    assert fetched.status_code == 200, fetched.text
    return campaign, next(s["id"] for s in fetched.json()["campaign"]["streams"]
                          if s["keitaro_id"] == kt_stream)


async def open_stream(http: httpx.AsyncClient, kt_campaign: int, kt_stream: int) -> int:
    return (await open_campaign(http, kt_campaign, kt_stream))[1]


def statuses(responses: list[httpx.Response]) -> dict[int, int]:
    return dict(Counter(response.status_code for response in responses))


def error_codes(responses: list[httpx.Response]) -> list[str]:
    return sorted(r.json()["error"]["code"] for r in responses if r.status_code >= 400)


def active_shares(view: dict[str, Any]) -> dict[int, int]:
    return {o["offer_id"]: o["share"] for o in view["offers"] if o["state"] == "active"}


def binding_id(view: dict[str, Any], offer_id: int) -> int:
    return next(o["id"] for o in view["offers"] if o["offer_id"] == offer_id)


class TestOneStream:
    def test_ten_simultaneous_adds_of_same_offer_leave_single_binding(self, fake, tmp_path):
        kt_campaign, _, kt_stream = fake.seed_campaign()

        async def scenario(http, _network):
            stream = await open_stream(http, kt_campaign, kt_stream)
            responses = await asyncio.gather(*[
                http.post(f"/api/streams/{stream}/offers", json={"offer_id": OXYS})
                for _ in range(10)])
            view = (await http.get(f"/api/streams/{stream}")).json()
            journal = (await http.get("/api/operations", params={"limit": 200})).json()["items"]
            return responses, view, journal

        responses, view, journal = serve(fake, tmp_path, scenario)
        assert statuses(responses) == {200: 1, 409: 9}
        assert error_codes(responses) == ["offer_already_in_stream"] * 9
        assert [o["offer_id"] for o in view["offers"]].count(OXYS) == 1
        assert active_shares(view) == {A_0009: 25, A_0008: 25, FITO: 25, OXYS: 25}
        assert view["total_share"] == 100
        adds = Counter(i["status"] for i in journal if i["action"] == "add_offer")
        assert adds == {"ok": 1, "error": 9}, "в журнале — каждая попытка"

    def test_simultaneous_adds_of_different_offers_keep_sum_100(self, fake, tmp_path):
        kt_campaign, _, kt_stream = fake.seed_campaign()
        extra = list(range(20000, 20008))
        for offer_id in extra:
            fake.add_offer(offer_id, f"параллельный оффер {offer_id}")

        async def scenario(http, _network):
            stream = await open_stream(http, kt_campaign, kt_stream)
            responses = await asyncio.gather(*[
                http.post(f"/api/streams/{stream}/offers", json={"offer_id": offer_id})
                for offer_id in extra])
            view = (await http.get(f"/api/streams/{stream}")).json()
            pushed = await http.post(f"/api/streams/{stream}/push", json={})
            return responses, view, pushed

        responses, view, pushed = serve(fake, tmp_path, scenario)
        assert statuses(responses) == {200: 8}
        shares = active_shares(view)
        assert sorted(shares) == sorted([A_0009, A_0008, FITO, *extra]), "все на месте, без дублей"
        assert len(view["offers"]) == 11 and sum(shares.values()) == 100
        assert max(shares.values()) - min(shares.values()) <= 1
        assert view["problems"] == [] and pushed.status_code == 200
        assert dict(fake.stream_offers(kt_stream)) == shares

    def test_two_simultaneous_pushes_publish_once(self, fake, tmp_path):
        kt_campaign, _, kt_stream = fake.seed_campaign()

        async def scenario(http, _network):
            stream = await open_stream(http, kt_campaign, kt_stream)
            await http.post(f"/api/streams/{stream}/offers", json={"offer_id": OXYS})
            fake.requests.clear()
            responses = await asyncio.gather(*[http.post(f"/api/streams/{stream}/push", json={})
                                               for _ in range(2)])
            view = (await http.get(f"/api/streams/{stream}")).json()
            snapshots = (await http.get(f"/api/streams/{stream}/snapshots")).json()
            return responses, view, snapshots

        responses, view, snapshots = serve(fake, tmp_path, scenario)
        assert statuses(responses) == {200: 1, 409: 1}
        assert error_codes(responses) == ["nothing_to_push"]
        assert len([r for r in fake.requests if r[0] == "PUT"]) == 1, "в Keitaro ушёл один PUT"
        assert dict(fake.stream_offers(kt_stream)) == {A_0009: 25, A_0008: 25, FITO: 25, OXYS: 25}
        assert view["is_dirty"] is False and len(snapshots) == 1

    def test_push_and_add_at_once_never_mix_into_half_state(self, fake, tmp_path):
        kt_campaign, _, kt_stream = fake.seed_campaign()

        async def scenario(http, _network):
            stream = await open_stream(http, kt_campaign, kt_stream)
            await http.post(f"/api/streams/{stream}/offers", json={"offer_id": OXYS})
            responses = await asyncio.gather(
                http.post(f"/api/streams/{stream}/push", json={}),
                http.post(f"/api/streams/{stream}/offers", json={"offer_id": SPARE}))
            return responses, (await http.get(f"/api/streams/{stream}")).json()

        responses, view = serve(fake, tmp_path, scenario)
        assert statuses(responses) == {200: 2}
        in_keitaro = dict(fake.stream_offers(kt_stream))
        assert sum(in_keitaro.values()) == 100 and view["total_share"] == 100
        if view["is_dirty"]:  # Push успел первым: пятый оффер остался черновиком
            assert in_keitaro == {A_0009: 25, A_0008: 25, FITO: 25, OXYS: 25}
            assert [(c["offer_id"], c["type"]) for c in view["diff"] if c["type"] == "add"] == \
                [(SPARE, "add")]
        else:  # Add успел первым: опубликованы все пять
            assert in_keitaro == {A_0009: 20, A_0008: 20, FITO: 20, OXYS: 20, SPARE: 20}

    def test_fetch_and_add_at_once_keep_the_draft(self, fake, tmp_path):
        kt_campaign, _, kt_stream = fake.seed_campaign()

        async def scenario(http, _network):
            campaign, stream = await open_campaign(http, kt_campaign, kt_stream)
            responses = await asyncio.gather(
                http.post(f"/api/campaigns/{campaign}/fetch", json={}),
                http.post(f"/api/streams/{stream}/offers", json={"offer_id": OXYS}))
            return responses, (await http.get(f"/api/streams/{stream}")).json()

        responses, view = serve(fake, tmp_path, scenario)
        assert statuses(responses) == {200: 2}
        assert active_shares(view) == {A_0009: 25, A_0008: 25, FITO: 25, OXYS: 25}
        assert view["is_dirty"] is True

    def test_double_click_on_remove(self, fake, tmp_path):
        kt_campaign, _, kt_stream = fake.seed_campaign()

        async def scenario(http, _network):
            stream = await open_stream(http, kt_campaign, kt_stream)
            fito = binding_id((await http.get(f"/api/streams/{stream}")).json(), FITO)
            responses = await asyncio.gather(*[
                http.delete(f"/api/streams/{stream}/offers/{fito}") for _ in range(2)])
            return responses, (await http.get(f"/api/streams/{stream}")).json()

        responses, view = serve(fake, tmp_path, scenario)
        assert statuses(responses) == {200: 1, 409: 1}
        assert error_codes(responses) == ["already_removed"]
        assert active_shares(view) == {A_0009: 50, A_0008: 50}

    def test_double_click_on_bring_back(self, fake, tmp_path):
        kt_campaign, _, kt_stream = fake.seed_campaign()

        async def scenario(http, _network):
            stream = await open_stream(http, kt_campaign, kt_stream)
            fito = binding_id((await http.get(f"/api/streams/{stream}")).json(), FITO)
            await http.delete(f"/api/streams/{stream}/offers/{fito}")
            responses = await asyncio.gather(*[
                http.post(f"/api/streams/{stream}/offers/{fito}/bring-back") for _ in range(2)])
            return responses, (await http.get(f"/api/streams/{stream}")).json()

        responses, view = serve(fake, tmp_path, scenario)
        assert statuses(responses) == {200: 1, 409: 1}
        assert error_codes(responses) == ["not_removed"]
        assert active_shares(view) == {A_0009: 33, A_0008: 33, FITO: 34}

    def test_two_manual_shares_at_once_end_in_the_same_state_in_any_order(self, fake, tmp_path):
        kt_campaign, _, kt_stream = fake.seed_campaign()

        async def scenario(http, _network):
            stream = await open_stream(http, kt_campaign, kt_stream)
            view = (await http.get(f"/api/streams/{stream}")).json()
            responses = await asyncio.gather(*[
                http.put(f"/api/streams/{stream}/offers/{binding_id(view, offer)}/share",
                         json={"share": 40}) for offer in (A_0009, A_0008)])
            return responses, (await http.get(f"/api/streams/{stream}")).json()

        responses, view = serve(fake, tmp_path, scenario)
        assert statuses(responses) == {200: 2}
        assert active_shares(view) == {A_0009: 40, A_0008: 40, FITO: 20}

    def test_double_click_on_cancel(self, fake, tmp_path):
        kt_campaign, _, kt_stream = fake.seed_campaign()

        async def scenario(http, _network):
            stream = await open_stream(http, kt_campaign, kt_stream)
            await http.post(f"/api/streams/{stream}/offers", json={"offer_id": OXYS})
            responses = await asyncio.gather(*[http.post(f"/api/streams/{stream}/cancel")
                                               for _ in range(2)])
            return responses, (await http.get(f"/api/streams/{stream}")).json()

        responses, view = serve(fake, tmp_path, scenario)
        assert statuses(responses) == {200: 2}
        assert view["is_dirty"] is False
        assert active_shares(view) == {A_0009: 33, A_0008: 33, FITO: 34}


async def warm_up(http: httpx.AsyncClient) -> None:
    """Справочники уже в кэше, как в работающей системе: первым сетевым вызовом «создаватора»
    становится проверка названия (GET /campaigns)."""
    assert (await http.get("/api/offers")).status_code == 200
    assert (await http.get("/api/meta/lookups")).status_code == 200


class TestCreator:
    def test_same_idempotency_key_twice_at_once_creates_single_campaign(self, fake, tmp_path):
        async def scenario(http, network):
            await warm_up(http)
            # Оба запроса прошли проверку ключа и названия — дальше решает резервирование ключа.
            network.hold("GET", r"^/campaigns$", parties=2)
            responses = await asyncio.gather(*[
                http.post("/api/campaigns", json=RACE_BODY, headers={"Idempotency-Key": "same"})
                for _ in range(2)])
            retry = await http.post("/api/campaigns", json=RACE_BODY,
                                    headers={"Idempotency-Key": "same"})
            return responses, retry

        responses, retry = serve(fake, tmp_path, scenario, dictionary_ttl_seconds=300)
        assert len(fake.campaigns) == 1 and len(fake.streams) == 2, "в Keitaro ровно одна кампания"
        winners = [r for r in responses if r.status_code == 200 and not r.json().get("replayed")]
        assert len(winners) == 1 and winners[0].json()["results"][0]["status"] == "created"
        (loser,) = [r for r in responses if r is not winners[0]]
        assert (loser.status_code == 409 and loser.json()["error"]["code"] == "in_progress") or \
            (loser.status_code == 200 and loser.json()["replayed"] is True)
        assert retry.status_code == 200 and retry.json()["replayed"] is True
        assert retry.json()["results"] == winners[0].json()["results"]

    def test_burst_of_same_key_still_creates_single_campaign(self, fake, tmp_path):
        async def scenario(http, network):
            await warm_up(http)
            network.hold("GET", r"^/campaigns$", parties=5)
            return await asyncio.gather(*[
                http.post("/api/campaigns", json=RACE_BODY, headers={"Idempotency-Key": "burst"})
                for _ in range(5)])

        responses = serve(fake, tmp_path, scenario, dictionary_ttl_seconds=300)
        assert len(fake.campaigns) == 1
        assert statuses(responses) == {200: 1, 409: 4}
        assert error_codes(responses) == ["in_progress"] * 4

    def test_lagging_twin_request_is_not_told_about_duplicate_name(self, fake, tmp_path):
        # Регрессия: ключ идемпотентности занимался ПОСЛЕ проверки формы и названия. Отставший
        # дубль (двойной клик с паузой 100–300 мс) проверял ключ, пока первый его ещё не занял,
        # а название — когда первый уже создал кампанию, и получал 409 duplicate_name про свою
        # же кампанию вместо in_progress/replayed.
        inner = fake.transport()
        state: dict[str, Any] = {"arrived": 0}

        async def choreography(request: httpx.Request) -> httpx.Response:
            """Первый запрос проходит проверку названия, второй — только когда первый создал
            кампанию (но оба уже проверили ключ: ждём у ворот обоих)."""
            path = request.url.path.removeprefix("/admin_api/v1")
            if (request.method, path) == ("GET", "/campaigns") and "both" in state:
                state["arrived"] += 1
                second = state["arrived"] == 2
                if second:
                    state["both"].set()
                with contextlib.suppress(asyncio.TimeoutError):
                    await asyncio.wait_for(state["both"].wait(), 0.05)
                if second:
                    with contextlib.suppress(asyncio.TimeoutError):
                        await asyncio.wait_for(state["created"].wait(), 0.05)
            else:
                await asyncio.sleep(0.001)
            response = inner.handle_request(request)
            if (request.method, path) == ("POST", "/campaigns") and "created" in state:
                state["created"].set()
            return response

        async def scenario(http, _network):
            await warm_up(http)
            state.update(both=asyncio.Event(), created=asyncio.Event())
            return await asyncio.gather(*[
                http.post("/api/campaigns", json=RACE_BODY, headers={"Idempotency-Key": "twin"})
                for _ in range(2)])

        responses = serve(fake, tmp_path, scenario, network=choreography,
                          dictionary_ttl_seconds=300)
        assert len(fake.campaigns) == 1, "дубля в Keitaro нет в любом случае"
        (loser,) = [r for r in responses if r.status_code != 200 or r.json().get("replayed")]
        assert (loser.status_code == 409 and loser.json()["error"]["code"] == "in_progress") or \
            (loser.status_code == 200 and loser.json()["replayed"] is True)

    def test_first_requests_after_install_do_not_collide_on_offers_cache(self, fake, tmp_path):
        # Регрессия: на пустой базе каждый запрос сам вставлял весь справочник офферов, и все,
        # кроме первого, падали на первичном ключе offers (IntegrityError → 500).
        async def scenario(http, network):
            network.hold("GET", r"^/offers$", parties=3)
            return await asyncio.gather(*[
                http.post("/api/campaigns", headers={"Idempotency-Key": f"cold-{index}"},
                          json={**RACE_BODY, "name": f"Cold start {index}"})
                for index in range(3)])

        responses = serve(fake, tmp_path, scenario)
        assert statuses(responses) == {200: 3}
        assert [r.json()["results"][0]["status"] for r in responses] == ["created"] * 3
        assert len(fake.campaigns) == 3

    def test_brand_new_offer_added_to_two_campaigns_at_once(self, fake, tmp_path):
        # Регрессия: оффера ещё нет в кэше → оба запроса форсят обновление справочника и оба
        # пытались вставить одну и ту же строку.
        first, second = fake.seed_campaign("first"), fake.seed_campaign("second")

        async def scenario(http, network):
            streams = [await open_stream(http, kt_campaign, kt_stream)
                       for kt_campaign, _, kt_stream in (first, second)]
            fake.add_offer(777, "только что заведённый оффер")
            network.hold("GET", r"^/offers$", parties=2)
            return await asyncio.gather(*[
                http.post(f"/api/streams/{stream}/offers", json={"offer_id": 777})
                for stream in streams])

        responses = serve(fake, tmp_path, scenario)
        assert statuses(responses) == {200: 2}
        assert all(active_shares(r.json())[777] == 25 for r in responses)

    def test_autocomplete_burst_loads_offers_from_keitaro_once(self, fake, tmp_path):
        async def scenario(http, _network):
            return await asyncio.gather(*[http.get("/api/offers", params={"q": q})
                                          for q in ("3", "37", "371", "3717", "37")])

        responses = serve(fake, tmp_path, scenario, dictionary_ttl_seconds=300)
        assert statuses(responses) == {200: 5}
        assert [o["id"] for o in responses[1].json()] == [A_0008, A_0009]
        assert [o["id"] for o in responses[3].json()] == [A_0008]
        assert len([r for r in fake.requests if r[:2] == ("GET", "/offers")]) == 1, \
            "остальные дождались первой загрузки, а не пошли в трекер сами"


class TestCampaigns:
    def test_two_campaigns_are_edited_in_parallel_without_interference(self, fake, tmp_path):
        first, second = fake.seed_campaign("first"), fake.seed_campaign("second")

        async def scenario(http, _network):
            streams = [await open_stream(http, kt_campaign, kt_stream)
                       for kt_campaign, _, kt_stream in (first, second)]
            adds = await asyncio.gather(*[
                http.post(f"/api/streams/{stream}/offers", json={"offer_id": offer_id})
                for stream in streams for offer_id in (OXYS, SPARE)])
            pushes = await asyncio.gather(*[http.post(f"/api/streams/{stream}/push", json={})
                                            for stream in streams])
            return adds, pushes

        adds, pushes = serve(fake, tmp_path, scenario)
        assert statuses(adds) == {200: 4} and statuses(pushes) == {200: 2}
        expected = {A_0009: 20, A_0008: 20, FITO: 20, OXYS: 20, SPARE: 20}
        assert dict(fake.stream_offers(first[2])) == dict(fake.stream_offers(second[2])) == expected

    def test_double_click_on_archive(self, fake, tmp_path):
        kt_campaign, _, kt_stream = fake.seed_campaign()

        async def scenario(http, _network):
            campaign, _ = await open_campaign(http, kt_campaign, kt_stream)
            responses = await asyncio.gather(*[http.delete(f"/api/campaigns/{campaign}")
                                               for _ in range(2)])
            return responses, (await http.get("/api/campaigns")).json()

        responses, listing = serve(fake, tmp_path, scenario)
        assert statuses(responses) == {200: 2}
        assert fake.campaigns[kt_campaign]["state"] == "deleted" and listing["total"] == 0

    def test_double_click_on_import(self, fake, tmp_path):
        # 300 новых кампаний: вставка длится заметно дольше, чем соседний запрос добирается до
        # своего SELECT, — гонка воспроизводится всегда, а не «как повезёт».
        for index in range(1, 301):
            fake.campaigns[index] = {"id": index, "name": f"bulk {index}", "alias": f"bulk{index}",
                                     "state": "active", "group_id": 22, "domain_id": 11}

        async def scenario(http, network):
            network.hold("GET", r"^/campaigns$", parties=2)
            responses = await asyncio.gather(*[http.post("/api/campaigns/import")
                                               for _ in range(2)])
            return responses, (await http.get("/api/campaigns", params={"limit": 1})).json()

        responses, listing = serve(fake, tmp_path, scenario)
        assert statuses(responses) == {200: 2}
        assert listing["total"] == 300
        assert sum(r.json()["created"] for r in responses) == 300, "каждая кампания создана один раз"

    def test_same_campaign_opened_several_times_at_once(self, fake, tmp_path):
        # Регрессия: «проверил, что кампании нет → сходил в Keitaro → вставил» без защиты.
        # Двойной клик «Открыть» или две вкладки — все, кроме первого, падали на
        # UNIQUE campaigns.keitaro_id (IntegrityError → 500 internal_error).
        kt_campaign, _, _ = fake.seed_campaign()

        async def scenario(http, network):
            network.hold("GET", rf"^/campaigns/{kt_campaign}$", parties=3)
            responses = await asyncio.gather(*[http.post(f"/api/campaigns/open/{kt_campaign}")
                                               for _ in range(3)])
            return responses, (await http.get("/api/campaigns")).json()

        responses, listing = serve(fake, tmp_path, scenario)
        assert statuses(responses) == {200: 3}
        assert len({r.json()["id"] for r in responses}) == 1 and listing["total"] == 1
