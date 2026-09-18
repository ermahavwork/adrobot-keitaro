"""Инструменты сверх оригинала: где используется оффер, массовые операции, советник, замки."""

from __future__ import annotations

import asyncio
import datetime as dt
import re
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from app import db
from app.main import create_app
from app.models import CampaignLock, utcnow
from app.services.locks import CampaignLocks, LockBusyError
from tests.conftest import _create_schema, make_settings

A_0009, A_0008, FITO, OXYS, SPARE = 3749, 3717, 11111, 11112, 13972


def second_campaign(editor):
    """Вторая кампания в трекере + загрузка всего в AdRobot."""
    kt_campaign, _flow1, flow2 = editor.fake.seed_campaign("campaign 3", offers=[(A_0009, 100)])
    assert editor.client.post("/api/campaigns/sync-all", json={}).status_code == 200
    return kt_campaign, flow2


class TestOfferUsage:
    def test_used_and_available_streams(self, editor):
        second_campaign(editor)
        usage = editor.client.get(f"/api/offers/{FITO}/usage").json()
        assert [(r["campaign_name"], r["share"], r["state"]) for r in usage["used"]] == \
            [("campaign 2", 34, "active")]
        assert [r["campaign_name"] for r in usage["available"]] == ["campaign 3"]
        assert usage["offer"]["name"].startswith("11111 FitoMishki")

    def test_streams_without_offers_are_never_offered(self, editor):
        second_campaign(editor)
        usage = editor.client.get(f"/api/offers/{OXYS}/usage").json()
        assert {r["stream_name"] for r in usage["available"]} == {"Flow 2"}

    def test_archived_and_draft_states_are_visible(self, editor):
        editor.remove(FITO)
        row = editor.client.get(f"/api/offers/{FITO}/usage").json()["used"][0]
        assert (row["state"], row["pending"], row["in_keitaro"]) == ("removed", True, True)

    def test_unsynced_campaigns_are_flagged(self, editor):
        editor.fake.seed_campaign("campaign 3")
        editor.client.post("/api/campaigns/import")
        assert editor.client.get(f"/api/offers/{FITO}/usage").json()["has_unsynced_campaigns"] is True
        editor.client.post("/api/campaigns/sync-all", json={"only_missing": True})
        assert editor.client.get(f"/api/offers/{FITO}/usage").json()["has_unsynced_campaigns"] is False

    def test_huge_offer_id_is_rejected_politely(self, editor):
        assert editor.client.get(f"/api/offers/{2**63}/usage").status_code == 422


class TestBulkOperations:
    def test_bulk_add_writes_only_drafts(self, editor):
        _kt, flow2 = second_campaign(editor)
        targets = [r["stream_id"] for r in editor.client.get(f"/api/offers/{OXYS}/usage").json()["available"]]
        assert len(targets) == 2
        result = editor.client.post(f"/api/offers/{OXYS}/bulk-add", json={"stream_ids": targets}).json()
        assert result["added"] == 2
        assert editor.shares() == {A_0009: 25, A_0008: 25, FITO: 25, OXYS: 25}
        assert OXYS not in editor.in_keitaro(), "до Push трекер не тронут"
        assert dict(editor.fake.stream_offers(flow2)) == {A_0009: 100}

    def test_bulk_add_reports_skipped_streams(self, editor):
        stream_id = editor.stream["id"]
        result = editor.client.post(f"/api/offers/{FITO}/bulk-add",
                                    json={"stream_ids": [stream_id, 999999]}).json()
        assert [(r["status"], r.get("code")) for r in result["results"]] == \
            [("skipped", "offer_already_in_stream"), ("skipped", "stream_not_found")]

    def test_bulk_add_of_unknown_offer_is_refused(self, editor):
        response = editor.client.post("/api/offers/99999999/bulk-add",
                                      json={"stream_ids": [editor.stream["id"]]})
        assert response.status_code == 422 and response.json()["error"]["code"] == "offer_not_usable"

    def test_push_many_publishes_each_stream_with_usual_checks(self, editor):
        _kt, flow2 = second_campaign(editor)
        targets = [r["stream_id"] for r in editor.client.get(f"/api/offers/{OXYS}/usage").json()["available"]]
        editor.client.post(f"/api/offers/{OXYS}/bulk-add", json={"stream_ids": targets})
        drafts = editor.client.get("/api/streams/drafts").json()["stream_ids"]
        assert sorted(drafts) == sorted(targets)
        # один из потоков успели поменять прямо в Keitaro — его публикация должна быть пропущена
        editor.fake.set_stream_offers(flow2, [(A_0009, 60), (A_0008, 40)])
        result = editor.client.post("/api/streams/push-many", json={"stream_ids": drafts}).json()
        statuses = {r["campaign_name"]: (r["status"], r.get("code")) for r in result["results"]}
        assert statuses == {"campaign 2": ("pushed", None), "campaign 3": ("error", "conflict")}
        assert OXYS in editor.in_keitaro()
        assert dict(editor.fake.stream_offers(flow2)) == {A_0009: 60, A_0008: 40}, "чужие правки целы"

    def test_sync_all_stops_hammering_dead_tracker(self, editor):
        second_campaign(editor)
        editor.fake.requests.clear()
        editor.fake.fail_next("GET", r"/streams$", times=50,
                              exception=lambda r: httpx.ConnectError("down", request=r))
        result = editor.client.post("/api/campaigns/sync-all", json={}).json()
        assert result["synced"] == 0 and len(result["failed"]) == 1
        stream_calls = [r for r in editor.fake.requests if r[1].endswith("/streams")]
        assert len(stream_calls) <= 3, "после отказа трекера остальные кампании не дёргаем"


class TestAdvisorRoutes:
    def _stats(self, editor, rows):
        stream = editor.kt_stream_id
        editor.fake.report_rows = [{"stream_id": stream, "offer_id": o, "clicks": c, "conversions": v,
                                    "campaign_unique_clicks": c, "revenue": v * 10.0, "cr": 0, "epc": 0}
                                   for o, c, v in rows]

    def test_advice_does_not_change_anything(self, editor):
        self._stats(editor, [(A_0009, 1000, 100), (A_0008, 1000, 10), (FITO, 1000, 10)])
        advice = editor.client.get(f"/api/streams/{editor.stream['id']}/advice").json()
        assert advice["ready"] and advice["changed"]
        best = next(i for i in advice["items"] if i["offer_id"] == A_0009)
        assert best["proposed"] > best["current"]
        assert sum(i["proposed"] for i in advice["items"]) == 100
        assert editor.shares() == {A_0009: 33, A_0008: 33, FITO: 34} and not editor.stream["is_dirty"]

    def test_apply_goes_to_draft_then_usual_push(self, editor):
        self._stats(editor, [(A_0009, 1000, 100), (A_0008, 1000, 10), (FITO, 1000, 10)])
        stream_id = editor.stream["id"]
        advice = editor.client.get(f"/api/streams/{stream_id}/advice").json()
        shares = {str(i["binding_id"]): i["proposed"] for i in advice["items"]}
        applied = editor.client.post(f"/api/streams/{stream_id}/apply-shares", json={"shares": shares}).json()
        assert applied["is_dirty"] and applied["total_share"] == 100
        assert editor.in_keitaro() == {A_0009: 33, A_0008: 33, FITO: 34}
        editor.push()
        assert sum(editor.in_keitaro().values()) == 100 and editor.in_keitaro()[A_0009] > 33

    def test_not_enough_data_says_wait(self, editor):
        self._stats(editor, [(A_0009, 5, 1)])
        advice = editor.client.get(f"/api/streams/{editor.stream['id']}/advice").json()
        assert advice["ready"] is False and "рано" in advice["message"].lower()

    def test_reports_unavailable_is_not_an_error(self, editor):
        editor.fake.fail_next("POST", r"/report/build", status=403, body={"error": "no access"}, times=5)
        response = editor.client.get(f"/api/streams/{editor.stream['id']}/advice")
        assert response.status_code == 200 and response.json()["ready"] is False

    @pytest.mark.parametrize(("shares", "code"), [
        ({"0": 50}, "binding_not_found"),
        ("sum", "invalid_distribution"),
        ("pinned", "pinned_share"),
    ])
    def test_apply_shares_guards(self, editor, shares, code):
        rows = {o["offer_id"]: o["id"] for o in editor.stream["offers"]}
        if shares == "sum":
            shares = {str(rows[A_0009]): 90}
        elif shares == "pinned":
            editor.pin(FITO)
            shares = {str(rows[FITO]): 40, str(rows[A_0009]): 30, str(rows[A_0008]): 30}
        response = editor.client.post(f"/api/streams/{editor.stream['id']}/apply-shares",
                                      json={"shares": shares})
        assert response.json()["error"]["code"] == code
        assert not editor.stream["is_dirty"]


class TestInvisibleOffers:
    def test_offer_hidden_from_api_key_can_be_brought_back(self, editor):
        """Оффер стоял в потоке, но ключу его не показывают (права в Keitaro) — вернуть можно."""
        del editor.fake.offers[FITO]
        editor.client.post("/api/offers/refresh")
        editor.remove(FITO)
        editor.push()
        editor.bring_back(FITO)
        # «Не виден ключу» снаружи неотличимо от «удалён в трекере», а Keitaro примет и
        # несуществующий ID — поэтому публикация такого оффера требует явного подтверждения.
        error = editor.push(expect=409)["error"]
        assert error["code"] == "unknown_offers" and error["details"]["offer_ids"] == [FITO]
        assert FITO not in editor.in_keitaro()
        editor.push(allow_unknown_offers=True)
        assert editor.in_keitaro() == {A_0009: 33, A_0008: 33, FITO: 34}

    def test_offer_known_as_deleted_is_still_blocked(self, editor):
        editor.remove(FITO)
        editor.push()
        editor.fake.offers[FITO]["state"] = "deleted"
        assert editor.bring_back(FITO, expect=422)["error"]["code"] == "offer_not_usable"

    def test_never_published_unknown_offer_cannot_be_added(self, editor):
        assert editor.add(424242, expect=422)["error"]["code"] == "offer_not_usable"


class TestCampaignLocks:
    def test_second_process_waits_then_gives_up(self, client):
        async def scenario():
            ours, theirs = CampaignLocks(acquire_timeout=5), CampaignLocks(acquire_timeout=0.4)
            async with ours.for_campaign(7):
                with pytest.raises(LockBusyError):
                    async with theirs.for_campaign(7):
                        pass
                async with theirs.for_campaign(8):  # другая кампания не заблокирована
                    pass
            async with theirs.for_campaign(7):  # после освобождения — свободно
                pass

        asyncio.run(scenario())

    def test_abandoned_lock_expires(self, client):
        async def scenario():
            async with db.get_sessionmaker()() as session:
                session.add(CampaignLock(campaign_id=9, owner="dead-process",
                                         expires_at=utcnow() - dt.timedelta(seconds=1)))
                await session.commit()
            async with CampaignLocks(acquire_timeout=0.5).for_campaign(9):
                pass

        asyncio.run(scenario())

    def test_idle_locks_are_dropped_but_queue_survives(self, client):
        async def scenario():
            locks = CampaignLocks(acquire_timeout=5)
            order: list[str] = []

            async def work(name: str) -> None:
                async with locks.for_campaign(7):
                    order.append(f"{name}:in")
                    assert 7 in locks._local, "пока кто-то внутри или в очереди, замок жив"
                    await asyncio.sleep(0.02)
                    order.append(f"{name}:out")

            await asyncio.gather(work("a"), work("b"), work("c"))
            assert order == ["a:in", "a:out", "b:in", "b:out", "c:in", "c:out"], "строго по очереди"
            for campaign_id in range(100, 140):
                async with locks.for_campaign(campaign_id):
                    pass
            assert locks._local == {} and not locks._users, "словарь замков не растёт с числом кампаний"

        asyncio.run(scenario())

    def test_lock_is_dropped_even_when_work_fails(self, client):
        async def scenario():
            locks = CampaignLocks(acquire_timeout=1)
            with pytest.raises(RuntimeError):
                async with locks.for_campaign(5):
                    raise RuntimeError("boom")
            assert locks._local == {} and not locks._users
            async with locks.for_campaign(5):  # и кампания после сбоя не осталась запертой
                pass

        asyncio.run(scenario())

    def test_busy_campaign_answers_409(self, editor):
        async def occupy():
            async with db.get_sessionmaker()() as session:
                session.add(CampaignLock(campaign_id=editor.campaign_id, owner="other-worker",
                                         expires_at=utcnow() + dt.timedelta(minutes=5)))
                await session.commit()

        asyncio.run(occupy())
        editor.client.app.state.locks = CampaignLocks(acquire_timeout=0.3)
        response = editor.client.post(f"/api/streams/{editor.stream['id']}/offers", json={"offer_id": OXYS})
        assert response.status_code == 409 and response.json()["error"]["code"] == "campaign_busy"


class TestOfflineDocs:
    def test_swagger_page_uses_only_local_assets(self, client):
        page = client.get("/docs")
        assert page.status_code == 200
        assert '"static/vendor/swagger-ui/swagger-ui-bundle.js"' in page.text, "относительный путь"
        assert "cdn." not in page.text and "unpkg" not in page.text
        assert client.get("/static/vendor/swagger-ui/swagger-ui.css").status_code == 200
        assert client.get("/openapi.json").status_code == 200


class TestSubPathDeployment:
    """AdRobot за обратным прокси в подкаталоге (https://host/adrobot/, прокси отрезает префикс)."""

    WEB = Path(__file__).resolve().parents[1] / "app" / "web"

    def test_page_and_scripts_use_only_relative_addresses(self, client):
        page = client.get("/").text
        assert not re.findall(r'(?:href|src)="/(?!/)', page), "абсолютный путь уведёт мимо подкаталога"
        for source in (self.WEB / "static" / "js").rglob("*.js"):
            if "vendor" in source.parts:
                continue
            text = source.read_text(encoding="utf-8")
            assert "fetch(\"/" not in text and "fetch('/" not in text and "fetch(`/" not in text, source.name
            assert not re.findall(r'(?:href|src):\s*["\'`]/(?!/)', text), source.name
        styles = (self.WEB / "static" / "css").rglob("*.css")
        assert not [s.name for s in styles if re.search(r"url\(\s*['\"]?/(?!/)", s.read_text("utf-8"))]

    def test_api_module_builds_addresses_from_page_location(self):
        text = (self.WEB / "static" / "js" / "api.js").read_text(encoding="utf-8")
        assert 'new URL(".", document.baseURI).pathname' in text

    def test_swagger_knows_the_prefix(self, tmp_path, fake):
        settings = make_settings(tmp_path, root_path="/adrobot/")
        _create_schema(settings.database_url)
        db.override_engine(db.create_engine(settings.database_url))
        try:
            with TestClient(create_app(settings, transport=fake.transport())) as prefixed:
                assert prefixed.get("/openapi.json").json()["servers"] == [{"url": "/adrobot"}]
                docs = prefixed.get("/docs").text
                assert '"openapi.json"' in docs or "'openapi.json'" in docs
                assert 'src="static/vendor/swagger-ui/' in docs and 'href="static/vendor/swagger-ui/' in docs
                assert prefixed.get("/api/auth/mode").status_code == 200, "сам API префикса не требует"
        finally:
            engine = db.get_engine()
            db.override_engine(None)
            asyncio.run(engine.dispose())


class TestCrossSiteProtection:
    """Без токена (режим по умолчанию) чужая страница не должна уметь нажимать кнопки за пользователя."""

    def test_cross_site_post_is_rejected(self, editor):
        editor.add(OXYS)
        url = f"/api/streams/{editor.stream['id']}/push"
        for headers in ({"Sec-Fetch-Site": "cross-site"}, {"Origin": "https://evil.example"}):
            response = editor.client.post(url, headers=headers)
            assert response.status_code == 403
            assert response.json()["error"]["code"] == "cross_site_request"
        assert OXYS not in editor.in_keitaro()

    def test_same_origin_and_scripts_still_work(self, editor):
        editor.add(OXYS)
        url = f"/api/streams/{editor.stream['id']}/push"
        ok = editor.client.post(url, json={}, headers={"Origin": "http://testserver",
                                                       "Sec-Fetch-Site": "same-origin"})
        assert ok.status_code == 200 and OXYS in editor.in_keitaro()

    def test_reverse_proxy_host_is_accepted(self, editor):
        response = editor.client.post(f"/api/campaigns/{editor.campaign_id}/fetch", json={}, headers={
            "Origin": "https://adrobot.example.com", "X-Forwarded-Host": "adrobot.example.com"})
        assert response.status_code == 200

    def test_reads_are_never_blocked(self, editor):
        response = editor.client.get("/api/campaigns", headers={"Sec-Fetch-Site": "cross-site"})
        assert response.status_code == 200


class TestReviewRegressions:
    def test_pin_survives_remove_then_cancel(self, editor):
        editor.pin(FITO)
        editor.remove(FITO)
        editor.cancel()
        row = next(o for o in editor.stream["offers"] if o["offer_id"] == FITO)
        assert (row["state"], row["share"], row["is_pinned"]) == ("active", 34, True)

    def test_cancel_drops_pin_made_by_manual_share(self, editor):
        editor.share(FITO, 50)
        editor.cancel()
        assert not any(o["is_pinned"] for o in editor.stream["offers"])
        editor.add(OXYS)
        assert editor.shares() == {A_0009: 25, A_0008: 25, FITO: 25, OXYS: 25}

    def test_draft_of_stream_deleted_in_keitaro_is_dropped(self, editor):
        editor.add(OXYS)
        editor.fake.streams[editor.kt_stream_id]["state"] = "deleted"
        editor.fetch()
        campaign = editor.client.get(f"/api/campaigns/{editor.campaign_id}").json()
        assert campaign["is_dirty"] is False, "такой черновик нельзя ни отправить, ни отменить"

    def test_dry_run_ignores_idempotency_key(self, client, fake):
        body = {"name": "Dry", "geo": "AU", "offer_id": A_0009}
        client.post("/api/campaigns", json=body, headers={"Idempotency-Key": "same"})
        plan = client.post("/api/campaigns", json={**body, "dry_run": True, "allow_duplicate_name": True},
                           headers={"Idempotency-Key": "same"}).json()
        assert plan["dry_run"] is True and "replayed" not in plan

    def test_split_by_geo_keeps_suffix_for_single_country(self, client, fake):
        client.post("/api/campaigns", json={"name": "Spring", "geo": "AU", "offer_id": A_0009,
                                            "split_by_geo": True})
        assert [c["name"] for c in fake.campaigns.values()] == ["Spring [AU]"]

    def test_local_save_failure_is_compensated(self, client, fake, monkeypatch):
        from app.services.creator import CampaignCreator

        async def boom(*_args, **_kwargs):
            raise RuntimeError("disk full")

        monkeypatch.setattr(CampaignCreator, "_save_locally", staticmethod(boom))
        result = client.post("/api/campaigns", json={"name": "X", "geo": "AU", "offer_id": A_0009}).json()
        assert result["results"][0]["status"] == "error"
        assert [c["state"] for c in fake.campaigns.values()] == ["deleted"], "кампания убрана из трекера"

    @pytest.mark.parametrize("how", ["sync_all", "open", "import"])
    def test_offers_are_named_without_manual_refresh(self, client, fake, how):
        """Свежая база: кампанию отдают «из базы, без сети», значит справочник грузим заранее."""
        kt_campaign, _flow1, _flow2 = fake.seed_campaign()
        if how == "sync_all":
            client.post("/api/campaigns/sync-all", json={})
            campaign_id = client.get("/api/campaigns").json()["items"][0]["id"]
        elif how == "open":
            campaign_id = client.post(f"/api/campaigns/open/{kt_campaign}").json()["id"]
        else:
            client.post("/api/campaigns/import")
            campaign_id = client.get("/api/campaigns").json()["items"][0]["id"]
        fake.fail_next("GET", r"/offers$", status=500, times=50)  # дальше справочник недоступен
        view = client.post(f"/api/campaigns/{campaign_id}/fetch").json()["campaign"]
        offers = [o for s in view["streams"] for o in s["offers"]]
        assert offers and all(o["offer_known"] for o in offers)
        assert all(not o["offer_name"].startswith("Оффер #") for o in offers)

    def test_offer_dictionary_failure_does_not_break_import(self, client, fake):
        fake.seed_campaign()
        fake.fail_next("GET", r"/offers$", status=500, times=50)
        assert client.post("/api/campaigns/import").status_code == 200
        assert client.post("/api/campaigns/sync-all", json={}).json()["synced"] == 1

    def test_auto_fetch_does_not_flood_the_journal(self, editor):
        def fetch_records() -> int:
            items = editor.client.get("/api/operations", params={"limit": 200}).json()["items"]
            return sum(1 for item in items if item["action"] == "fetch_streams")

        start = fetch_records()
        for _ in range(3):  # редактор открыли трижды — в Keitaro ничего не менялось
            assert editor.fetch(auto=True)["result"]["changed"] is False
        assert fetch_records() == start
        editor.fetch()  # ручной Fetch записывается всегда
        assert fetch_records() == start + 1
        editor.fake.set_stream_offers(editor.kt_stream_id, [(A_0009, 50), (A_0008, 50)])
        assert editor.fetch(auto=True)["result"]["changed"] is True
        assert fetch_records() == start + 2, "автообновление, которое что-то нашло, в журнале есть"

    def test_offer_revived_in_keitaro_goes_last_like_bring_back(self, editor):
        editor.remove(A_0009)
        editor.push()
        assert editor.removed() == [A_0009]
        editor.fake.set_stream_offers(editor.kt_stream_id, [(A_0009, 34), (A_0008, 33), (FITO, 33)])
        editor.fetch()
        assert editor.shares() == {A_0009: 34, A_0008: 33, FITO: 33}, "Fetch — зеркало, без пересчёта"
        editor.add(OXYS)
        editor.remove(OXYS)  # пересчёт на троих: 33/33/34, остаток — последнему по активации
        assert editor.shares() == {A_0008: 33, FITO: 33, A_0009: 34}

    def test_bulk_add_failure_does_not_leave_campaign_locked(self, editor):
        for offer in (A_0009, A_0008, FITO):
            editor.pin(offer)  # всё закреплено на 100% — новому офферу места нет
        stream_id = editor.stream["id"]
        result = editor.client.post(f"/api/offers/{OXYS}/bulk-add", json={"stream_ids": [stream_id]}).json()
        assert result["results"][0]["status"] == "skipped"
        editor.pin(FITO, pinned=False)  # следующая правка проходит сразу, а не ждёт замок 20 секунд
        assert editor.add(OXYS)["is_dirty"]
