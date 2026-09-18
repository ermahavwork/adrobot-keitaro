"""«Создаватор»: что уходит в Keitaro, валидация до сети, идемпотентность, компенсация."""

from __future__ import annotations

import httpx
import pytest

OFFER = 3749


def create(client, expect: int = 200, headers: dict | None = None, **body):
    payload = {"name": "Test AU", "geo": "AU", "offer_id": OFFER, **body}
    response = client.post("/api/campaigns", json=payload, headers=headers or {})
    assert response.status_code == expect, response.text
    return response.json()


def only_campaign(fake):
    assert len(fake.campaigns) == 1
    return next(iter(fake.campaigns.values()))


class TestHappyPath:
    def test_creates_campaign_with_two_streams_like_in_the_task(self, client, fake):
        result = create(client)["results"][0]
        assert result["status"] == "created"

        campaign = only_campaign(fake)
        assert campaign["name"] == "Test AU"
        assert (campaign["domain_id"], campaign["group_id"], campaign["traffic_source_id"]) == \
            (11, 22, 33), "домен, группа и источник проставлены"
        assert campaign["type"] == "position" and campaign["cost_type"] == "CPC"
        assert campaign["parameters"]["creative_id"]["name"] == "utm_creative", \
            "параметры источника скопированы в кампанию (API сам этого не делает)"

        flow1, flow2 = sorted((s for s in fake.streams.values()), key=lambda s: s["position"])
        assert flow1["schema"] == "redirect" and flow1["action_type"] == "http"
        assert flow1["action_payload"] == "https://google.com"
        assert [(f["name"], f["mode"], f["payload"]) for f in flow1["filters"]] == \
            [("country", "accept", ["AU"])]
        assert flow2["schema"] == "landings" and flow2["filters"] == []
        assert [(o["offer_id"], o["share"]) for o in flow2["offers"]] == [(OFFER, 100)]
        assert (flow1["position"], flow2["position"]) == (1, 2), "гео-поток проверяется первым"

    def test_result_has_links_and_editor_is_ready_without_fetch(self, client, fake):
        result = create(client)["results"][0]
        kt_id = result["keitaro_campaign_id"]
        assert result["admin_url"] == f"https://tracker.test/admin/#!/campaigns/{kt_id}"
        assert result["campaign_url"] == f"https://in.example.test/{result['alias']}"
        view = client.get(f"/api/campaigns/{result['campaign_id']}").json()
        assert view["origin"] == "adrobot" and view["geo"][0]["code"] == "AU"
        assert [s["name"] for s in view["streams"]] == ["Flow 1", "Flow 2"]
        assert view["streams"][1]["offers"][0]["share"] == 100
        assert view["is_dirty"] is False

    def test_several_offers_split_evenly(self, client, fake):
        create(client, offer_ids=[3717, 13972])
        flow2 = next(s for s in fake.streams.values() if s["schema"] == "landings")
        assert [(o["offer_id"], o["share"]) for o in flow2["offers"]] == \
            [(3749, 33), (3717, 33), (13972, 34)]

    def test_multi_geo_in_one_campaign(self, client, fake):
        create(client, geo="mx, au; Румыния")
        flow1 = next(s for s in fake.streams.values() if s["schema"] == "redirect")
        assert flow1["filters"][0]["payload"] == ["MX", "AU", "RO"]

    def test_split_by_geo_creates_campaign_per_country(self, client, fake):
        results = create(client, name="Spring", geo="MX,AU", split_by_geo=True)["results"]
        assert [r["name"] for r in results] == ["Spring [MX]", "Spring [AU]"]
        assert sorted(c["name"] for c in fake.campaigns.values()) == ["Spring [AU]", "Spring [MX]"]

    def test_uk_alias_is_fixed_to_gb(self, client, fake):
        create(client, geo="UK")
        flow1 = next(s for s in fake.streams.values() if s["schema"] == "redirect")
        assert flow1["filters"][0]["payload"] == ["GB"], "Keitaro принял бы UK молча"

    def test_dry_run_sends_nothing(self, client, fake):
        result = create(client, dry_run=True)
        assert result["dry_run"] is True
        requests = result["results"][0]["plan"]["requests"]
        assert [r["path"] for r in requests] == ["/campaigns", "/streams", "/streams"]
        assert not fake.campaigns and not fake.streams
        assert not [r for r in fake.requests if r[0] in ("POST", "PUT", "DELETE")]


class TestValidationBeforeNetwork:
    @pytest.mark.parametrize(("body", "needle"), [
        ({"geo": "ZZ"}, "Не распознаны"),
        ({"geo": ""}, "хотя бы одну страну"),
        ({"offer_id": 99999999}, "не найден"),
        ({"group_id": 123456}, "Группы #123456"),
        ({"traffic_source_id": 123456}, "Источника трафика #123456"),
        ({"domain_id": 123456}, "Домена #123456"),
        ({"redirect_url": "javascript:alert(1)"}, "Адрес редиректа"),
        ({"redirect_url": "google.com"}, "Адрес редиректа"),
    ])
    def test_garbage_is_rejected_before_any_write(self, client, fake, body, needle):
        error = create(client, expect=422, **body)["error"]
        assert needle in error["message"]
        assert not fake.campaigns, "Keitaro принял бы это молча — не даём до него дойти"

    def test_all_problems_reported_at_once(self, client):
        error = create(client, expect=422, geo="ZZ", offer_id=99999999)["error"]
        assert len(error["details"]["errors"]) == 2

    def test_archived_offer_is_rejected(self, client, fake):
        fake.add_offer(555, "old offer", state="deleted")
        assert "deleted" in create(client, expect=422, offer_id=555)["error"]["message"]

    def test_empty_name_and_missing_offer(self, client):
        assert create(client, expect=422, name="   ")["error"]["code"] == "validation"
        response = client.post("/api/campaigns", json={"name": "x", "geo": "AU"})
        assert response.status_code == 422

    def test_duplicate_name_needs_confirmation(self, client, fake):
        create(client)
        error = create(client, expect=409)["error"]
        assert error["code"] == "duplicate_name"
        create(client, allow_duplicate_name=True)
        assert len(fake.campaigns) == 2

    def test_custom_alias_conflict_is_reported_not_silently_replaced(self, client, fake):
        create(client, alias="my-alias")
        error = create(client, expect=200, name="Other", alias="my-alias")["results"][0]["error"]
        assert error["code"] == "alias_taken"

    def test_domain_hidden_from_api_key_is_inferred_from_campaigns(self, client, fake):
        fake.seed_campaign("existing")
        fake.domains_visible = False
        result = create(client)["results"][0]
        assert fake.campaigns[result["keitaro_campaign_id"]]["domain_id"] == 11
        assert any("не виден справочник доменов" in w for w in result["warnings"])


class TestIdempotencyAndCompensation:
    def test_same_key_does_not_create_twice(self, client, fake):
        first = create(client, headers={"Idempotency-Key": "k1"})
        second = create(client, headers={"Idempotency-Key": "k1"})
        assert second["replayed"] is True
        assert second["results"] == first["results"]
        assert len(fake.campaigns) == 1

    def test_same_key_with_other_body_is_conflict(self, client):
        create(client, headers={"Idempotency-Key": "k2"})
        error = create(client, expect=409, name="Other", headers={"Idempotency-Key": "k2"})
        assert error["error"]["code"] == "idempotency_mismatch"

    def test_failed_attempt_releases_key_for_retry(self, client, fake):
        fake.fail_next("POST", r"^/campaigns$", status=500, body="boom")
        failed = create(client, headers={"Idempotency-Key": "k3"})["results"][0]
        assert failed["status"] == "error"
        assert create(client, headers={"Idempotency-Key": "k3"})["results"][0]["status"] == "created"

    def test_stream_failure_archives_half_created_campaign(self, client, fake):
        fake.fail_next("POST", r"^/streams$", status=500, body="boom", times=1)
        result = create(client)["results"][0]
        assert result["status"] == "error"
        assert result["error"]["details"]["rolled_back"] is True
        assert only_campaign(fake)["state"] == "deleted", "половинчатая кампания убрана в архив"
        assert client.get("/api/campaigns").json()["total"] == 0

    def test_second_stream_failure_also_compensated(self, client, fake):
        # первый POST /streams (гео-поток) проходит, второй (поток с оффером) рвётся
        fake.fail_next("POST", r"^/streams$", skip=1,
                       exception=lambda r: httpx.ConnectError("reset", request=r))
        result = create(client)["results"][0]
        assert result["status"] == "error"
        assert only_campaign(fake)["state"] == "deleted"
        assert len([r for r in fake.requests if r[:2] == ("POST", "/streams")]) == 2

    def test_failed_compensation_is_reported_loudly(self, client, fake):
        fake.fail_next("POST", r"^/streams$", status=500, body="boom")
        fake.fail_next("DELETE", r"^/campaigns/\d+$", status=500, body="boom", times=10)
        error = create(client)["results"][0]["error"]
        assert error["details"]["rolled_back"] is False
        assert "удалите её вручную" in error["message"]

    def test_timeout_on_create_is_not_retried_and_warns(self, client, fake):
        fake.fail_next("POST", r"^/campaigns$", after_effect=True,
                       exception=lambda r: httpx.ReadTimeout("slow", request=r))
        error = create(client)["results"][0]["error"]
        assert "мог выполниться" in error["message"]
        assert len([r for r in fake.requests if r[:2] == ("POST", "/campaigns")]) == 1

    def test_operations_log_records_success_and_failure(self, client, fake):
        create(client)
        create(client, expect=422, geo="ZZ")
        items = client.get("/api/operations").json()["items"]
        assert [(i["action"], i["status"]) for i in items][:2] == \
            [("create_campaign", "error"), ("create_campaign", "ok")]
