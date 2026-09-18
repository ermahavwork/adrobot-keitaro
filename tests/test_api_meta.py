"""Служебные маршруты и список кампаний: здоровье, справочники, страны, поиск офферов,
настройки, журнал, импорт/открытие/архив кампаний, формат ответов 404."""

from __future__ import annotations

import asyncio
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr

from app import __version__, db
from app.main import create_app
from tests.conftest import Editor, make_settings
from tests.fake_keitaro import FakeKeitaro

A_0009, A_0008, FITO, OXYS, SPARE = 3749, 3717, 11111, 11112, 13972


def create_schema(database_url: str) -> None:
    async def run() -> None:
        engine = db.create_engine(database_url)
        async with engine.begin() as connection:
            await connection.run_sync(db.Base.metadata.create_all)
        await engine.dispose()

    asyncio.run(run())


@contextmanager
def running_app(fake: FakeKeitaro, tmp_path: Path, **overrides) -> Iterator[TestClient]:
    """Приложение с нестандартными настройками на своей временной базе (как `conftest.client`)."""
    settings = make_settings(tmp_path, **overrides)
    create_schema(settings.database_url)
    db.override_engine(db.create_engine(settings.database_url))
    try:
        with TestClient(create_app(settings, transport=fake.transport())) as test_client:
            yield test_client
    finally:
        engine = db.get_engine()
        db.override_engine(None)
        asyncio.run(engine.dispose())


def down(request: httpx.Request) -> Exception:
    return httpx.ConnectError("connection refused", request=request)


def create(client: TestClient, expect: int = 200, **body) -> dict:
    payload = {"name": "Test AU", "geo": "AU", "offer_id": A_0009, **body}
    response = client.post("/api/campaigns", json=payload)
    assert response.status_code == expect, response.text
    return response.json()


def calls(fake: FakeKeitaro, method: str, path: str) -> int:
    return len([r for r in fake.requests if r[:2] == (method, path)])


class TestHealth:
    def test_everything_fine(self, client):
        body = client.get("/api/health").json()
        assert body == {
            "status": "ok", "version": __version__, "database": "ok",
            "keitaro": {"configured": True, "reachable": True, "message": ""},
            "admin_url": "https://tracker.test/admin/"}

    def test_deep_check_is_one_light_request(self, client, fake):
        client.get("/api/health")
        assert [r[:2] for r in fake.requests] == [("GET", "/groups")]

    def test_shallow_check_does_not_touch_keitaro(self, client, fake):
        body = client.get("/api/health", params={"deep": "false"}).json()
        assert body["status"] == "ok" and body["keitaro"]["reachable"] is None
        assert fake.requests == [], "liveness-проба не должна нагружать трекер"

    def test_unreachable_keitaro_is_degraded_not_500(self, client, fake):
        fake.fail_next("GET", r"^/groups$", times=50, exception=down)
        response = client.get("/api/health")
        assert response.status_code == 200
        body = response.json()
        assert body["status"] == "degraded" and body["database"] == "ok"
        assert body["keitaro"]["reachable"] is False
        assert "Нет связи с Keitaro" in body["keitaro"]["message"]

    def test_rejected_key_is_degraded_with_a_hint(self, client, fake):
        fake.fail_next("GET", r"^/groups$", status=401, body={"error": "Unauthorized"})
        body = client.get("/api/health").json()
        assert body["status"] == "degraded"
        assert "KEITARO_API_KEY" in body["keitaro"]["message"]

    def test_cloudflare_in_front_of_tracker_is_named(self, client, fake):
        fake.fail_next("GET", r"^/groups$", status=403, body=fake.cloudflare_body())
        assert "Cloudflare" in client.get("/api/health").json()["keitaro"]["message"]

    def test_missing_key_is_setup_required_without_network(self, fake, tmp_path):
        with running_app(fake, tmp_path, keitaro_api_key=SecretStr("")) as unconfigured:
            body = unconfigured.get("/api/health").json()
        assert body["status"] == "setup_required"
        assert body["keitaro"]["configured"] is False and body["keitaro"]["reachable"] is None
        assert "KEITARO_API_KEY" in body["keitaro"]["message"]
        assert fake.requests == []

    def test_unconfigured_app_answers_503_in_our_format(self, fake, tmp_path):
        with running_app(fake, tmp_path, keitaro_base_url="") as unconfigured:
            response = unconfigured.get("/api/offers")
        assert response.status_code == 503
        assert response.json()["error"]["code"] == "keitaro_not_configured"


class TestLookups:
    def test_shape_for_the_create_form(self, client):
        body = client.get("/api/meta/lookups").json()
        assert body["groups"] == [{"id": 4970, "name": "FORTESTS"}]
        assert body["traffic_sources"] == [{"id": 473, "name": "FORTESTS",
                                            "template_name": "facebook"}]
        assert body["domains"] == [{"id": 4622, "name": "https://in.example.test/"}]
        assert body["domains_visible"] is True and body["inferred_domain_id"] is None
        assert body["warnings"] == []
        assert body["defaults"]["default_group_id"] == 4970

    def test_inactive_sources_and_domains_are_not_offered(self, client, fake):
        fake.sources.append({"id": 474, "name": "old source", "state": "deleted"})
        fake.domains.append({"id": 4623, "name": "https://parked.test/", "state": "deleted"})
        body = client.get("/api/meta/lookups").json()
        assert [s["id"] for s in body["traffic_sources"]] == [473]
        assert [d["id"] for d in body["domains"]] == [4622]

    def test_hidden_domains_are_inferred_from_campaigns_with_warning(self, client, fake):
        fake.seed_campaign("existing")
        fake.domains_visible = False
        body = client.get("/api/meta/lookups").json()
        assert body["domains"] == [] and body["domains_visible"] is False
        assert body["inferred_domain_id"] == 4622
        assert any("не виден справочник доменов" in w and "#4622" in w for w in body["warnings"])
        assert (body["defaults"]["default_domain_id"],
                body["defaults"]["default_domain_id_source"]) == (4622, "авто")

    def test_most_popular_domain_wins_the_inference(self, client, fake):
        for index in range(3):
            campaign_id, _, _ = fake.seed_campaign(f"campaign {index}")
            fake.campaigns[campaign_id]["domain_id"] = 9999 if index == 0 else 4622
        fake.domains_visible = False
        assert client.get("/api/meta/lookups").json()["inferred_domain_id"] == 4622

    def test_hidden_domains_and_no_campaigns_ask_for_explicit_setting(self, client, fake):
        fake.domains_visible = False
        body = client.get("/api/meta/lookups").json()
        assert body["inferred_domain_id"] is None
        assert any("DEFAULT_DOMAIN_ID" in w for w in body["warnings"])
        assert (body["defaults"]["default_domain_id"],
                body["defaults"]["default_domain_id_source"]) == (None, "не задано")

    def test_forbidden_domains_are_treated_as_hidden(self, client, fake):
        fake.seed_campaign("existing")
        fake.fail_next("GET", r"^/domains$", status=403, body={"error": "Access denied"}, times=50)
        body = client.get("/api/meta/lookups").json()
        assert body["domains_visible"] is False and body["inferred_domain_id"] == 4622

    def test_several_groups_mean_no_automatic_choice(self, client, fake):
        fake.groups.append({"id": 5000, "name": "SECOND", "position": 2, "type": "campaigns"})
        defaults = client.get("/api/meta/lookups").json()["defaults"]
        assert (defaults["default_group_id"], defaults["default_group_id_source"]) == \
            (None, "не задано")

    def test_groups_of_other_types_are_not_mixed_in(self, client, fake):
        fake.groups.append({"id": 6000, "name": "offers group", "position": 1, "type": "offers"})
        assert [g["id"] for g in client.get("/api/meta/lookups").json()["groups"]] == [4970]

    def test_lookups_are_cached_until_refresh_is_asked(self, fake, tmp_path):
        with running_app(fake, tmp_path, dictionary_ttl_seconds=300) as cached:
            cached.get("/api/meta/lookups")
            cached.get("/api/meta/lookups")
            assert calls(fake, "GET", "/groups") == 1, "второй раз — из кэша"
            fake.groups.append({"id": 5000, "name": "NEW", "position": 2, "type": "campaigns"})
            assert len(cached.get("/api/meta/lookups").json()["groups"]) == 1
            fresh = cached.get("/api/meta/lookups", params={"refresh": "true"}).json()
            assert [g["id"] for g in fresh["groups"]] == [4970, 5000]

    def test_keitaro_down_is_a_504_in_our_format(self, client, fake):
        fake.fail_next("GET", r"^/groups$", times=50, exception=down)
        response = client.get("/api/meta/lookups")
        assert response.status_code == 504
        assert response.json()["error"]["code"] == "keitaro_unreachable"


class TestCountries:
    def test_exact_code_comes_first(self, client):
        found = client.get("/api/meta/countries", params={"q": "ro"}).json()
        assert found[0] == {"code": "RO", "name_en": "Romania", "name_ru": "Румыния"}

    @pytest.mark.parametrize(("q", "code"), [("румы", "RO"), ("РУМЫ", "RO"), ("austral", "AU"),
                                             ("UK", "GB"), ("сша", "US")])
    def test_search_by_name_in_both_languages_and_aliases(self, client, q, code):
        assert client.get("/api/meta/countries", params={"q": q}).json()[0]["code"] == code

    def test_empty_query_gives_first_countries_alphabetically(self, client):
        codes = [c["code"] for c in client.get("/api/meta/countries", params={"limit": 5}).json()]
        assert len(codes) == 5 and codes == sorted(codes)

    def test_nothing_found_is_empty_list(self, client):
        assert client.get("/api/meta/countries", params={"q": "нарния"}).json() == []

    @pytest.mark.parametrize(("limit", "status"), [(0, 422), (1, 200), (300, 200), (301, 422),
                                                   ("x", 422)])
    def test_limit_bounds(self, client, limit, status):
        response = client.get("/api/meta/countries", params={"limit": limit})
        assert response.status_code == status
        if status == 200:
            assert 0 < len(response.json()) <= limit


class TestGeoParse:
    def test_mixed_separators_cases_and_russian_names(self, client):
        body = client.get("/api/meta/geo/parse", params={"raw": "MX, au; Румыния"}).json()
        assert body == {"codes": [{"code": "MX", "name": "Мексика"},
                                  {"code": "AU", "name": "Австралия"},
                                  {"code": "RO", "name": "Румыния"}], "unknown": []}

    def test_unknown_tokens_are_reported_not_dropped(self, client):
        body = client.get("/api/meta/geo/parse", params={"raw": "AU, ZZ, Нарния"}).json()
        assert [c["code"] for c in body["codes"]] == ["AU"]
        assert body["unknown"] == ["ZZ", "Нарния"]

    def test_duplicates_collapse_and_uk_becomes_gb(self, client):
        body = client.get("/api/meta/geo/parse", params={"raw": "UK au AU Австралия gb"}).json()
        assert [c["code"] for c in body["codes"]] == ["GB", "AU"]

    def test_multi_word_names_survive(self, client):
        body = client.get("/api/meta/geo/parse",
                          params={"raw": "United States; Южная Корея"}).json()
        assert [c["code"] for c in body["codes"]] == ["US", "KR"] and body["unknown"] == []

    @pytest.mark.parametrize("raw", ["", "   ", ",;|"])
    def test_blank_input_is_empty_answer(self, client, raw):
        assert client.get("/api/meta/geo/parse", params={"raw": raw}).json() == \
            {"codes": [], "unknown": []}


class TestOfferSearch:
    @staticmethod
    def ids(client: TestClient, **params) -> list[int]:
        response = client.get("/api/offers", params=params)
        assert response.status_code == 200, response.text
        return [offer["id"] for offer in response.json()]

    def test_empty_query_lists_active_offers(self, client):
        assert self.ids(client) == [A_0008, A_0009, FITO, OXYS, SPARE]

    def test_search_by_id_prefix(self, client):
        assert self.ids(client, q="37") == [A_0008, A_0009]
        assert self.ids(client, q="1111") == [FITO, OXYS]

    def test_exact_id_beats_prefix_and_name_matches(self, client, fake):
        fake.add_offer(37, "short id")
        fake.add_offer(370, "offer mentioning 37 in name")
        fake.add_offer(9000, "37 signs of a good offer")
        assert self.ids(client, q="37") == [37, 370, A_0008, A_0009, 9000]

    @pytest.mark.parametrize("q", ["румыния", "РУМЫНИЯ", "РуМыНиЯ", "основной, рум"])
    def test_cyrillic_substring_ignores_case(self, client, q):
        assert self.ids(client, q=q) == [A_0008, A_0009]

    def test_latin_substring_ignores_case(self, client):
        assert self.ids(client, q="fitomishki") == [FITO]

    def test_name_prefix_ranks_above_inner_match(self, client, fake):
        fake.add_offer(100, "zeta test offer")
        fake.add_offer(200, "test offer alpha")
        assert self.ids(client, q="test") == [200, 100, SPARE]

    def test_query_is_trimmed(self, client):
        assert self.ids(client, q="  3749 ") == [A_0009]

    def test_nothing_found(self, client):
        assert self.ids(client, q="нет такого оффера") == []

    def test_label_is_ready_for_autocomplete(self, client):
        (offer,) = client.get("/api/offers", params={"q": "13972"}).json()
        assert offer == {"id": SPARE, "name": "another test offer 2", "state": "active",
                         "country": ["Romania"], "label": "[13972] another test offer 2"}

    def test_inactive_offers_are_hidden_unless_asked(self, client, fake):
        fake.add_offer(555, "archived offer", state="deleted")
        assert 555 not in self.ids(client, q="archived")
        found = client.get("/api/offers", params={"q": "archived",
                                                  "include_inactive": "true"}).json()
        assert [(o["id"], o["state"]) for o in found] == [(555, "deleted")]

    def test_offer_gone_from_keitaro_is_no_longer_suggested(self, client, fake):
        assert self.ids(client, q="13972") == [SPARE]
        del fake.offers[SPARE]
        assert self.ids(client, q="13972") == []
        assert self.ids(client, q="13972", include_inactive="true") == []

    def test_offer_renamed_in_keitaro_is_found_by_new_name(self, client, fake):
        self.ids(client)
        fake.offers[SPARE]["name"] = "Переименованный оффер"
        assert self.ids(client, q="переименованный") == [SPARE]
        assert self.ids(client, q="another") == []

    @pytest.mark.parametrize(("limit", "status"), [(0, 422), (1, 200), (100, 200), (101, 422),
                                                   (-1, 422), ("много", 422)])
    def test_limit_bounds(self, client, limit, status):
        response = client.get("/api/offers", params={"limit": limit})
        assert response.status_code == status
        if status == 422:
            assert response.json()["error"]["code"] == "validation"

    def test_limit_cuts_the_list(self, client):
        assert self.ids(client, limit=2) == [A_0008, A_0009]

    def test_offers_are_cached_between_searches(self, fake, tmp_path):
        with running_app(fake, tmp_path, dictionary_ttl_seconds=300) as cached:
            for q in ("3", "37", "374"):
                cached.get("/api/offers", params={"q": q})
        assert calls(fake, "GET", "/offers") == 1, "автокомплит не должен долбить трекер"


class TestOffersRefresh:
    def test_returns_number_of_offers_in_keitaro(self, client, fake):
        assert client.post("/api/offers/refresh").json() == {"offers": 5}
        fake.add_offer(777, "brand new")
        del fake.offers[SPARE]
        assert client.post("/api/offers/refresh").json() == {"offers": 5}
        found = [o["id"] for o in client.get("/api/offers", params={"q": "7"}).json()]
        assert found == [777]

    def test_refresh_ignores_cache_lifetime(self, fake, tmp_path):
        with running_app(fake, tmp_path, dictionary_ttl_seconds=300) as cached:
            cached.post("/api/offers/refresh")
            cached.post("/api/offers/refresh")
        assert calls(fake, "GET", "/offers") == 2

    def test_rows_without_numeric_id_are_skipped(self, client, fake):
        fake.offers["junk"] = {"id": "junk", "name": "broken row"}
        assert client.post("/api/offers/refresh").json() == {"offers": 5}

    def test_keitaro_failure_is_reported_in_our_format(self, client, fake):
        fake.fail_next("GET", r"^/offers$", status=500, body="boom")
        response = client.post("/api/offers/refresh")
        assert response.status_code == 502
        assert response.json()["error"]["code"] == "keitaro_server_error"


class TestSettings:
    def test_defaults_come_with_their_origin(self, client):
        assert client.get("/api/settings").json() == {
            "default_domain_id": 4622, "default_domain_id_source": "авто",
            "default_group_id": 4970, "default_group_id_source": "авто",
            "default_traffic_source_id": 473, "default_traffic_source_id_source": "авто",
            "default_redirect_url": "https://google.com", "default_redirect_url_source": ".env",
            "tracking_domain_url": "https://in.example.test", "tracking_domain_url_source": ".env"}

    def test_saved_value_wins_and_is_marked(self, client):
        saved = client.put("/api/settings", json={"default_group_id": 5,
                                                  "default_redirect_url": "https://example.org/x"})
        assert saved.status_code == 200
        body = client.get("/api/settings").json()
        assert body == saved.json()
        assert (body["default_group_id"], body["default_group_id_source"]) == (5, "настройки")
        assert (body["default_redirect_url"], body["default_redirect_url_source"]) == \
            ("https://example.org/x", "настройки")
        assert body["default_domain_id_source"] == "авто", "остальное не тронуто"

    @pytest.mark.parametrize("blank", [None, ""])
    def test_blank_value_returns_the_automatic_one(self, client, blank):
        client.put("/api/settings", json={"default_group_id": 5,
                                          "default_redirect_url": "https://example.org/x"})
        body = client.put("/api/settings", json={"default_group_id": None,
                                                 "default_redirect_url": blank}).json()
        assert (body["default_group_id"], body["default_group_id_source"]) == (4970, "авто")
        assert (body["default_redirect_url"], body["default_redirect_url_source"]) == \
            ("https://google.com", ".env")

    def test_put_replaces_the_whole_form(self, client):
        client.put("/api/settings", json={"default_group_id": 5, "default_domain_id": 7})
        body = client.put("/api/settings", json={"default_domain_id": 8}).json()
        assert (body["default_domain_id"], body["default_domain_id_source"]) == (8, "настройки")
        assert body["default_group_id_source"] == "авто", "не присланное поле = «вернуть авто»"

    def test_strings_are_trimmed(self, client):
        body = client.put("/api/settings",
                          json={"tracking_domain_url": "  https://go.example.org  "}).json()
        assert body["tracking_domain_url"] == "https://go.example.org"

    def test_env_value_is_marked_and_can_be_overridden(self, fake, tmp_path):
        with running_app(fake, tmp_path, default_group_id=4970, default_domain_id=4622) as app:
            body = app.get("/api/settings").json()
            assert (body["default_group_id"], body["default_group_id_source"]) == (4970, ".env")
            body = app.put("/api/settings", json={"default_group_id": 12}).json()
            assert (body["default_group_id"], body["default_group_id_source"]) == (12, "настройки")
            assert body["default_domain_id_source"] == ".env"

    def test_saved_redirect_and_tracking_domain_are_used_by_creator(self, client, fake):
        client.put("/api/settings", json={"default_redirect_url": "https://example.org/away",
                                          "tracking_domain_url": "https://go.example.org/"})
        result = create(client)["results"][0]
        flow1 = next(s for s in fake.streams.values() if s["schema"] == "redirect")
        assert flow1["action_payload"] == "https://example.org/away"
        assert result["campaign_url"] == f"https://go.example.org/{result['alias']}"

    def test_saved_group_that_is_not_in_keitaro_is_caught_by_creator(self, client, fake):
        client.put("/api/settings", json={"default_group_id": 5})
        error = create(client, expect=422)["error"]
        assert "Группы #5 нет в Keitaro" in error["message"] and not fake.campaigns

    def test_settings_survive_application_restart(self, fake, tmp_path):
        with running_app(fake, tmp_path) as first:
            first.put("/api/settings", json={"default_group_id": 5})
        settings = make_settings(tmp_path)
        db.override_engine(db.create_engine(settings.database_url))
        try:
            with TestClient(create_app(settings, transport=fake.transport())) as second:
                assert second.get("/api/settings").json()["default_group_id"] == 5
        finally:
            engine = db.get_engine()
            db.override_engine(None)
            asyncio.run(engine.dispose())


class TestOperationsJournal:
    @pytest.fixture
    def busy(self, client, fake) -> Editor:
        """Пять записей: create ok, create error, fetch ok, add ok, add error."""
        create(client)
        create(client, expect=422, geo="ZZ")
        editor = Editor(client, fake, *fake.seed_campaign("second")[::2])
        editor.add(OXYS)
        editor.add(99999999, expect=422)
        return editor

    def test_newest_first_with_full_shape(self, client, busy):
        body = client.get("/api/operations").json()
        assert body["total"] == 5
        assert [(i["action"], i["status"]) for i in body["items"]] == [
            ("add_offer", "error"), ("add_offer", "ok"), ("fetch_streams", "ok"),
            ("create_campaign", "error"), ("create_campaign", "ok")]
        newest = body["items"][0]
        assert set(newest) == {"id", "created_at", "action", "actor", "status",
                               "keitaro_campaign_id", "keitaro_stream_id", "summary", "error",
                               "duration_ms", "details"}
        assert newest["keitaro_stream_id"] == busy.kt_stream_id
        assert newest["created_at"].endswith("+00:00") and newest["duration_ms"] >= 0

    @pytest.mark.parametrize(("status", "expected"), [("ok", 3), ("error", 2), ("мусор", 5),
                                                      ("", 5), ("OK", 5)])
    def test_status_filter(self, client, busy, status, expected):
        body = client.get("/api/operations", params={"status": status}).json()
        assert body["total"] == expected == len(body["items"])
        if status in ("ok", "error"):
            assert {i["status"] for i in body["items"]} == {status}

    def test_campaign_filter(self, client, busy):
        kt_id = client.get(f"/api/campaigns/{busy.campaign_id}").json()["keitaro_id"]
        body = client.get("/api/operations", params={"keitaro_campaign_id": kt_id}).json()
        assert [i["action"] for i in body["items"]] == ["add_offer", "add_offer", "fetch_streams"]
        assert client.get("/api/operations",
                          params={"keitaro_campaign_id": 424242}).json() == {"total": 0,
                                                                            "items": []}

    def test_filters_combine(self, client, busy):
        kt_id = client.get(f"/api/campaigns/{busy.campaign_id}").json()["keitaro_id"]
        body = client.get("/api/operations",
                          params={"keitaro_campaign_id": kt_id, "status": "error"}).json()
        assert [(i["action"], i["status"]) for i in body["items"]] == [("add_offer", "error")]

    def test_pages_do_not_overlap_and_total_stays(self, client, busy):
        pages = [client.get("/api/operations", params={"limit": 2, "offset": offset}).json()
                 for offset in (0, 2, 4, 6)]
        assert [len(p["items"]) for p in pages] == [2, 2, 1, 0]
        assert {p["total"] for p in pages} == {5}
        ids = [i["id"] for p in pages for i in p["items"]]
        assert ids == sorted(ids, reverse=True) and len(set(ids)) == 5

    @pytest.mark.parametrize("params", [{"limit": 0}, {"limit": 201}, {"offset": -1},
                                        {"limit": "x"}, {"keitaro_campaign_id": "abc"}])
    def test_bad_paging_is_422(self, client, params):
        response = client.get("/api/operations", params=params)
        assert response.status_code == 422 and response.json()["error"]["code"] == "validation"

    def test_push_record_keeps_diff_and_published_set(self, client, busy):
        busy.push()
        record = client.get("/api/operations").json()["items"][0]
        assert record["action"] == "push" and record["status"] == "ok"
        assert {"add", "share"} == {change["type"] for change in record["details"]["diff"]}
        assert sorted(o["offer_id"] for o in record["details"]["published"]) == \
            sorted([A_0009, A_0008, FITO, OXYS])


class TestCampaignList:
    @pytest.fixture
    def listed(self, client, fake) -> dict[str, int]:
        """Три кампании: две из Keitaro (одна кириллицей) и одна созданная у нас."""
        ids = {"summer": fake.seed_campaign("Summer Sale RO")[0],
               "winter": fake.seed_campaign("Зимняя распродажа")[0]}
        fake.campaigns[ids["winter"]]["alias"] = "WinterPromo"
        client.post("/api/campaigns/import")
        ids["own"] = create(client, name="Created here", alias="own-alias")["results"][0][
            "keitaro_campaign_id"]
        return ids

    @staticmethod
    def found(client: TestClient, **params) -> list[int]:
        response = client.get("/api/campaigns", params=params)
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["total"] >= len(body["items"])
        return [item["keitaro_id"] for item in body["items"]]

    def test_everything_newest_first(self, client, listed):
        assert self.found(client) == sorted(listed.values(), reverse=True)

    @pytest.mark.parametrize(("q", "who"), [
        ("summer", ["summer"]), ("SALE ro", ["summer"]), ("зимняя", ["winter"]),
        ("ЗИМНЯЯ РАСПР", ["winter"]), ("winterpromo", ["winter"]), ("own-al", ["own"]),
        ("  created  ", ["own"]), ("нет такой", []),
    ])
    def test_search_by_name_and_alias_ignores_case(self, client, listed, q, who):
        assert self.found(client, q=q) == [listed[name] for name in who]

    def test_search_by_keitaro_id_prefix(self, client, listed):
        assert self.found(client, q=str(listed["winter"])) == [listed["winter"]]
        assert set(self.found(client, q="100")) == set(listed.values()), "префикс ID"

    def test_origin_filter(self, client, listed):
        assert self.found(client, origin="adrobot") == [listed["own"]]
        assert set(self.found(client, origin="keitaro")) == {listed["summer"], listed["winter"]}
        assert len(self.found(client, origin="мусор")) == 3, "непонятное значение = без фильтра"

    def test_only_drafts_shows_campaigns_with_unpublished_changes(self, client, fake, listed):
        assert self.found(client, only_drafts="true") == []
        flow2 = next(s["id"] for s in fake.streams.values()
                     if s["campaign_id"] == listed["summer"] and s["schema"] == "landings")
        editor = Editor(client, fake, listed["summer"], flow2)
        editor.add(OXYS)
        assert self.found(client, only_drafts="true") == [listed["summer"]]
        flags = {i["keitaro_id"]: i["has_draft"] for i in client.get("/api/campaigns").json()["items"]}
        assert flags == {listed["summer"]: True, listed["winter"]: False, listed["own"]: False}
        editor.push()
        assert self.found(client, only_drafts="true") == []

    def test_pin_alone_is_not_a_draft(self, client, fake, listed):
        flow2 = next(s["id"] for s in fake.streams.values()
                     if s["campaign_id"] == listed["summer"] and s["schema"] == "landings")
        Editor(client, fake, listed["summer"], flow2).pin(FITO)
        assert self.found(client, only_drafts="true") == []

    def test_pages_and_total(self, client, listed):
        first = client.get("/api/campaigns", params={"limit": 2}).json()
        rest = client.get("/api/campaigns", params={"limit": 2, "offset": 2}).json()
        assert (first["total"], len(first["items"]), len(rest["items"])) == (3, 2, 1)
        assert not {i["id"] for i in first["items"]} & {i["id"] for i in rest["items"]}

    @pytest.mark.parametrize("params", [{"limit": 0}, {"limit": 201}, {"offset": -1}])
    def test_bad_paging_is_422(self, client, params):
        assert client.get("/api/campaigns", params=params).status_code == 422

    def test_item_has_link_to_keitaro_admin(self, client, listed):
        item = client.get("/api/campaigns", params={"q": "summer"}).json()["items"][0]
        assert item["admin_url"] == f"https://tracker.test/admin/#!/campaigns/{listed['summer']}"
        assert item["origin"] == "keitaro" and item["streams_fetched_at"] is None


class TestImport:
    def test_new_campaigns_appear_once(self, client, fake):
        fake.seed_campaign("one")
        fake.seed_campaign("two")
        assert client.post("/api/campaigns/import").json() == {"total": 2, "created": 2, "gone": 0}
        assert client.post("/api/campaigns/import").json() == {"total": 2, "created": 0, "gone": 0}
        assert client.get("/api/campaigns").json()["total"] == 2

    def test_vanished_campaign_is_marked_and_hidden(self, client, fake):
        keep, _, _ = fake.seed_campaign("keep")
        gone, _, _ = fake.seed_campaign("gone")
        client.post("/api/campaigns/import")
        fake.campaigns[gone]["state"] = "deleted"
        assert client.post("/api/campaigns/import").json() == {"total": 1, "created": 0, "gone": 1}
        assert [i["keitaro_id"] for i in client.get("/api/campaigns").json()["items"]] == [keep]
        everything = client.get("/api/campaigns", params={"include_deleted": "true"}).json()["items"]
        assert {i["keitaro_id"]: i["is_deleted"] for i in everything} == {keep: False, gone: True}
        assert client.post("/api/campaigns/import").json()["gone"] == 0, "дважды не считаем"

    def test_restored_campaign_comes_back(self, client, fake):
        campaign_id, _, _ = fake.seed_campaign("phoenix")
        client.post("/api/campaigns/import")
        fake.campaigns[campaign_id]["state"] = "deleted"
        client.post("/api/campaigns/import")
        fake.campaigns[campaign_id]["state"] = "active"
        assert client.post("/api/campaigns/import").json() == {"total": 1, "created": 0, "gone": 0}
        assert client.get("/api/campaigns").json()["total"] == 1

    def test_rename_in_keitaro_is_picked_up(self, client, fake):
        campaign_id, _, _ = fake.seed_campaign("old name")
        client.post("/api/campaigns/import")
        fake.campaigns[campaign_id].update(name="Новое имя", alias="fresh")
        client.post("/api/campaigns/import")
        item = client.get("/api/campaigns").json()["items"][0]
        assert (item["name"], item["alias"]) == ("Новое имя", "fresh")

    def test_import_keeps_origin_and_draft_of_known_campaign(self, client, fake):
        result = create(client)["results"][0]
        stream_id = client.get(f"/api/campaigns/{result['campaign_id']}").json()["streams"][1]["id"]
        client.post(f"/api/streams/{stream_id}/offers", json={"offer_id": OXYS})
        client.post("/api/campaigns/import")
        view = client.get(f"/api/campaigns/{result['campaign_id']}").json()
        assert view["origin"] == "adrobot" and view["geo"][0]["code"] == "AU"
        assert view["is_dirty"] is True, "импорт шапок не трогает потоки и черновик"

    def test_more_than_one_page_of_campaigns(self, client, fake):
        for index in range(1, 502):
            fake.campaigns[index] = {"id": index, "name": f"bulk {index}", "alias": f"b{index}",
                                     "state": "active", "group_id": 4970, "domain_id": 4622}
        assert client.post("/api/campaigns/import").json() == {"total": 501, "created": 501,
                                                               "gone": 0}
        assert client.get("/api/campaigns", params={"limit": 1}).json()["total"] == 501

    def test_import_is_journaled(self, client, fake):
        fake.seed_campaign("one")
        client.post("/api/campaigns/import")
        record = client.get("/api/operations").json()["items"][0]
        assert (record["action"], record["status"]) == ("import_campaigns", "ok")
        assert "новых: 1" in record["summary"]

    def test_keitaro_failure_changes_nothing_and_is_journaled(self, client, fake):
        fake.seed_campaign("one")
        client.post("/api/campaigns/import")
        fake.fail_next("GET", r"^/campaigns$", times=50, exception=down)
        response = client.post("/api/campaigns/import")
        assert response.status_code == 504
        assert client.get("/api/campaigns").json()["total"] == 1, "сбой связи ≠ «кампании пропали»"
        record = client.get("/api/operations").json()["items"][0]
        assert (record["action"], record["status"]) == ("import_campaigns", "error")


class TestOpenCampaign:
    def test_unknown_campaign_is_404_from_keitaro(self, client, fake):
        response = client.post("/api/campaigns/open/424242")
        assert response.status_code == 404
        error = response.json()["error"]
        assert error["code"] == "keitaro_not_found" and "424242" in error["message"]
        assert client.get("/api/campaigns", params={"include_deleted": "true"}).json()["total"] == 0

    def test_open_imports_header_only(self, client, fake):
        campaign_id, _, _ = fake.seed_campaign("to open")
        opened = client.post(f"/api/campaigns/open/{campaign_id}").json()
        assert opened["keitaro_id"] == campaign_id
        view = client.get(f"/api/campaigns/{opened['id']}").json()
        assert (view["name"], view["origin"], view["streams"]) == ("to open", "keitaro", [])
        assert view["streams_fetched_at"] is None and view["group_id"] == 4970

    def test_open_twice_is_same_campaign_and_single_request(self, client, fake):
        campaign_id, _, _ = fake.seed_campaign()
        first = client.post(f"/api/campaigns/open/{campaign_id}").json()
        second = client.post(f"/api/campaigns/open/{campaign_id}").json()
        assert first == second
        assert calls(fake, "GET", f"/campaigns/{campaign_id}") == 1

    @pytest.mark.parametrize("bad_id", ["abc", "1.5", "0x10"])
    def test_garbage_id_is_422(self, client, bad_id):
        assert client.post(f"/api/campaigns/open/{bad_id}").status_code == 422

    def test_keitaro_down_is_504(self, client, fake):
        campaign_id, _, _ = fake.seed_campaign()
        fake.fail_next("GET", r"^/campaigns/\d+$", times=50, exception=down)
        response = client.post(f"/api/campaigns/open/{campaign_id}")
        assert response.status_code == 504
        assert response.json()["error"]["code"] == "keitaro_unreachable"


class TestArchiveCampaign:
    def test_archives_in_keitaro_and_hides_from_list(self, editor, fake):
        client, kt_id = editor.client, next(iter(fake.campaigns))
        response = client.delete(f"/api/campaigns/{editor.campaign_id}")
        assert response.status_code == 200
        assert response.json() == {"archived": True, "keitaro_id": kt_id}
        assert fake.campaigns[kt_id]["state"] == "deleted"
        assert client.get("/api/campaigns").json()["total"] == 0
        hidden = client.get("/api/campaigns", params={"include_deleted": "true"}).json()["items"]
        assert [(i["keitaro_id"], i["is_deleted"], i["state"]) for i in hidden] == \
            [(kt_id, True, "deleted")]

    def test_archive_is_journaled_with_campaign_name(self, editor):
        editor.client.delete(f"/api/campaigns/{editor.campaign_id}")
        record = editor.client.get("/api/operations").json()["items"][0]
        assert (record["action"], record["status"]) == ("archive_campaign", "ok")
        assert "campaign 2" in record["summary"]

    def test_already_gone_in_keitaro_is_still_success(self, editor, fake):
        next(iter(fake.campaigns.values()))["state"] = "deleted"
        response = editor.client.delete(f"/api/campaigns/{editor.campaign_id}")
        assert response.status_code == 200 and response.json()["archived"] is True

    def test_archive_twice_is_harmless(self, editor):
        assert editor.client.delete(f"/api/campaigns/{editor.campaign_id}").status_code == 200
        assert editor.client.delete(f"/api/campaigns/{editor.campaign_id}").status_code == 200

    def test_keitaro_failure_leaves_campaign_visible(self, editor, fake):
        fake.fail_next("DELETE", r"^/campaigns/\d+$", status=500, body="boom")
        response = editor.client.delete(f"/api/campaigns/{editor.campaign_id}")
        assert response.status_code == 502
        assert next(iter(fake.campaigns.values()))["state"] == "active"
        assert editor.client.get("/api/campaigns").json()["total"] == 1
        record = editor.client.get("/api/operations").json()["items"][0]
        assert (record["action"], record["status"]) == ("archive_campaign", "error")

    def test_archived_campaign_can_still_be_viewed(self, editor):
        editor.client.delete(f"/api/campaigns/{editor.campaign_id}")
        view = editor.client.get(f"/api/campaigns/{editor.campaign_id}").json()
        assert view["is_deleted"] is True and len(view["streams"]) == 2

    def test_unknown_campaign_is_404_and_keitaro_is_not_called(self, client, fake):
        response = client.delete("/api/campaigns/999")
        assert response.status_code == 404
        assert response.json()["error"]["code"] == "campaign_not_found"
        assert not [r for r in fake.requests if r[0] == "DELETE"]


class TestNotFoundFormat:
    @pytest.mark.parametrize(("method", "path", "code"), [
        ("GET", "/api/campaigns/999", "campaign_not_found"),
        ("POST", "/api/campaigns/999/fetch", "campaign_not_found"),
        ("GET", "/api/campaigns/999/stats", "campaign_not_found"),
        ("GET", "/api/streams/999", "stream_not_found"),
        ("GET", "/api/streams/999/snapshots", "stream_not_found"),
        ("POST", "/api/streams/999/push", "stream_not_found"),
        ("POST", "/api/streams/999/cancel", "stream_not_found"),
        ("POST", "/api/streams/999/recalculate", "stream_not_found"),
        ("DELETE", "/api/streams/999/offers/1", "stream_not_found"),
        ("GET", "/api/nope", "http_error"),
        ("GET", "/api/campaigns/1/nope", "http_error"),
    ])
    def test_missing_things_answer_404_in_our_format(self, client, method, path, code):
        response = client.request(method, path)
        assert response.status_code == 404
        body = response.json()
        assert set(body) == {"error"} and set(body["error"]) == {"code", "message", "details"}
        assert body["error"]["code"] == code and body["error"]["message"]

    def test_wrong_method_is_405_in_our_format(self, client):
        response = client.patch("/api/campaigns")
        assert response.status_code == 405
        assert response.json()["error"]["code"] == "http_error"

    def test_non_numeric_id_is_422_not_404(self, client):
        response = client.get("/api/campaigns/abc")
        assert response.status_code == 422
        assert response.json()["error"]["code"] == "validation"


class TestCampaignViewIsLocal:
    """`GET /api/campaigns/{id}` обещает отдавать кампанию «из базы, без сети»."""

    REASON = (
        "app/api/routes_campaigns.py:campaign_view ради одной строки tracking_domain_url зовёт "
        "dictionaries.resolve_defaults(), а тот — get_lookups(): как только кэш справочников "
        "устарел (в бою раз в 5 минут, в тестах всегда), просмотр кампании делает три запроса "
        "в Keitaro (/groups, /traffic_sources, /domains), а при недоступном трекере отвечает "
        "504 — локальный черновик нельзя даже открыть. Правка: брать tracking_domain_url из "
        "get_overrides()/settings без справочников либо отдавать устаревший кэш при ошибке.")

    @pytest.mark.xfail(strict=True, reason=REASON)
    def test_viewing_a_campaign_does_not_call_keitaro(self, editor):
        editor.fake.requests.clear()
        assert editor.client.get(f"/api/campaigns/{editor.campaign_id}").status_code == 200
        assert editor.fake.requests == []

    @pytest.mark.xfail(strict=True, reason=REASON)
    def test_draft_can_be_viewed_while_keitaro_is_down(self, fake, tmp_path):
        with running_app(fake, tmp_path) as app:
            app_editor = Editor(app, fake, *fake.seed_campaign()[::2])
            app_editor.add(OXYS)
            fake.fail_next("GET", r".*", times=500, exception=down)
            response = app.get(f"/api/campaigns/{app_editor.campaign_id}")
        assert response.status_code == 200
        assert response.json()["is_dirty"] is True

    def test_single_stream_view_already_works_offline(self, editor):
        stream_id = editor.add(OXYS)["id"]
        editor.fake.requests.clear()
        editor.fake.fail_next("GET", r".*", times=500, exception=down)
        response = editor.client.get(f"/api/streams/{stream_id}")
        assert response.status_code == 200 and response.json()["is_dirty"] is True
        assert editor.fake.requests == [], "вид одного потока сеть не трогает — так и должно быть"


class TestGeoPlaceholderInName:
    @pytest.mark.xfail(strict=True, reason=(
        "app/services/creator.py:build_plans подставляет {geo} в название только когда кампаний "
        "получается несколько (len(geo_sets) > 1). Та же форма с галочкой «отдельная кампания на "
        "страну», но с одной страной создаёт в Keitaro кампанию с буквальным «{geo}» в имени "
        "(без галочки — тоже). Правка: заменять плейсхолдер всегда — на код страны, а для "
        "нескольких стран без разбиения на коды через «+»."))
    def test_placeholder_is_substituted_for_single_country_too(self, client, fake):
        result = create(client, name="Spring {geo}", geo="AU", split_by_geo=True)
        assert result["results"][0]["name"] == "Spring AU"
        assert "{geo}" not in next(iter(fake.campaigns.values()))["name"]

    def test_placeholder_works_when_split_gives_several_campaigns(self, client, fake):
        results = create(client, name="Spring {geo} promo", geo="MX,AU", split_by_geo=True)["results"]
        assert [r["name"] for r in results] == ["Spring MX promo", "Spring AU promo"]
