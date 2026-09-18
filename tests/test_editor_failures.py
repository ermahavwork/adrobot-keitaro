"""Редактор под нагрузкой сбоев: всё, что может пойти не так между AdRobot и Keitaro."""

from __future__ import annotations

import httpx
import pytest

A_0009, A_0008, FITO, OXYS, SPARE = 3749, 3717, 11111, 11112, 13972


class TestWeightsGuards:
    def test_manual_share_pins_and_rebalances(self, editor):
        view = editor.share(FITO, 50)
        assert editor.shares() == {FITO: 50, A_0009: 25, A_0008: 25}
        assert next(o for o in view["offers"] if o["offer_id"] == FITO)["is_pinned"] is True

    @pytest.mark.parametrize("value", [0, 101, -1])
    def test_share_out_of_range_rejected_by_schema(self, editor, value):
        assert editor.share(FITO, value, expect=422)["error"]["code"] == "validation"

    def test_share_leaving_nothing_for_others(self, editor):
        error = editor.share(FITO, 99, expect=422)["error"]
        assert error["code"] == "not_enough_for_free"
        assert editor.shares() == {A_0009: 33, A_0008: 33, FITO: 34}, "ничего не изменилось"

    def test_add_when_everything_is_pinned_to_100(self, editor):
        editor.share(A_0009, 40)
        editor.share(A_0008, 30)
        editor.share(FITO, 30)
        error = editor.add(OXYS, expect=422)["error"]
        assert error["code"] == "not_enough_for_free"
        assert OXYS not in editor.order(), "неудачное добавление не оставляет следов"

    def test_all_pinned_after_remove_blocks_push_with_hint(self, editor):
        editor.pin(A_0009)
        editor.pin(A_0008)
        view = editor.remove(FITO)
        assert editor.shares() == {A_0009: 33, A_0008: 33}
        assert any("66%" in p and "закреплены" in p for p in view["problems"])
        assert editor.push(expect=422)["error"]["code"] == "invalid_distribution"
        fixed = editor.client.post(f"/api/streams/{view['id']}/recalculate",
                                   json={"drop_pins": True}).json()
        assert fixed["problems"] == [] and editor.shares() == {A_0009: 50, A_0008: 50}
        editor.push()
        assert editor.in_keitaro() == {A_0009: 50, A_0008: 50}

    def test_keitaro_sum_not_100_is_mirrored_with_warning_and_fixable(self, editor):
        editor.fake.set_stream_offers(editor.kt_stream_id, [(A_0009, 60), (A_0008, 60)])
        editor.fetch()
        view = editor.stream
        assert editor.shares() == {A_0009: 60, A_0008: 60} and not view["is_dirty"]
        assert any("120%" in p for p in view["problems"])
        editor.client.post(f"/api/streams/{view['id']}/recalculate")
        assert editor.shares() == {A_0009: 50, A_0008: 50} and editor.stream["is_dirty"]

    def test_removing_last_offer_needs_explicit_confirmation(self, editor):
        for offer in (A_0009, A_0008, FITO):
            editor.remove(offer)
        error = editor.push(expect=422)["error"]
        assert error["code"] == "empty_stream" and error["details"]["needs"] == "allow_empty"
        editor.push(allow_empty=True)
        assert editor.in_keitaro() == {}


class TestOfferValidation:
    def test_unknown_offer_cannot_be_added(self, editor):
        assert editor.add(99999999, expect=422)["error"]["code"] == "offer_not_usable"

    def test_duplicate_offer_is_conflict(self, editor):
        assert editor.add(FITO, expect=409)["error"]["code"] == "offer_already_in_stream"

    def test_offer_archived_in_keitaro_before_push_blocks_it(self, editor):
        editor.add(SPARE)
        editor.fake.offers[SPARE]["state"] = "deleted"
        assert editor.push(expect=422)["error"]["code"] == "offer_not_usable"
        assert SPARE not in editor.in_keitaro()

    def test_offer_invisible_to_api_key_is_shown_with_placeholder(self, editor):
        del editor.fake.offers[FITO]
        editor.client.post("/api/offers/refresh")
        row = next(o for o in editor.stream["offers"] if o["offer_id"] == FITO)
        assert row["offer_known"] is False and row["share"] == 34
        editor.remove(A_0009)  # остальные операции с потоком продолжают работать
        editor.push()
        assert editor.in_keitaro() == {A_0008: 50, FITO: 50}

    def test_disabled_offer_is_passed_through_untouched(self, editor):
        stream = editor.fake.streams[editor.kt_stream_id]
        stream["offers"][0]["state"] = "disabled"  # 3749 выключили в админке Keitaro
        editor.fetch()
        editor.add(OXYS)
        assert editor.shares() == {A_0008: 33, FITO: 33, OXYS: 34}, "выключенный не в расчёте"
        editor.push()
        saved = {o["offer_id"]: (o["share"], o["state"]) for o in stream["offers"]}
        assert saved[A_0009] == (33, "disabled"), "как был выключен, так и остался"


class TestConflicts:
    def test_push_detects_edit_made_in_keitaro(self, editor):
        editor.add(OXYS)
        editor.fake.set_stream_offers(editor.kt_stream_id, [(A_0009, 50), (A_0008, 50)])
        error = editor.push(expect=409)["error"]
        assert error["code"] == "conflict"
        assert {o["offer_id"] for o in error["details"]["keitaro"]} == {A_0009, A_0008}
        assert editor.in_keitaro() == {A_0009: 50, A_0008: 50}, "чужие правки не затёрты"

    def test_force_push_overwrites(self, editor):
        editor.add(OXYS)
        editor.fake.set_stream_offers(editor.kt_stream_id, [(A_0009, 50), (A_0008, 50)])
        editor.push(force=True)
        assert editor.in_keitaro() == {A_0009: 25, A_0008: 25, FITO: 25, OXYS: 25}

    def test_fetch_keeps_draft_and_merges_untouched_offers(self, editor):
        editor.add(OXYS)  # черновик: четыре по 25
        editor.fake.set_stream_offers(editor.kt_stream_id, [(A_0009, 50), (A_0008, 50)])
        editor.fetch()
        assert OXYS in editor.shares(), "свой неопубликованный оффер не потерян"
        assert editor.stream["is_dirty"]
        editor.push()  # базовая линия обновлена — конфликта больше нет
        assert OXYS in editor.in_keitaro()

    def test_fetch_with_discard_draft_mirrors_keitaro(self, editor):
        editor.add(OXYS)
        editor.remove(FITO)
        editor.fetch(discard_draft=True)
        assert editor.shares() == {A_0009: 33, A_0008: 33, FITO: 34}
        assert OXYS not in editor.order() and not editor.stream["is_dirty"]

    def test_stream_deleted_in_keitaro(self, editor):
        editor.fake.streams[editor.kt_stream_id]["state"] = "deleted"
        editor.fetch()
        assert editor.stream["is_deleted"] is True
        assert editor.add(OXYS, expect=409)["error"]["code"] == "stream_deleted"

    def test_campaign_deleted_in_keitaro(self, editor):
        next(iter(editor.fake.campaigns.values()))["state"] = "deleted"
        response = editor.client.post(f"/api/campaigns/{editor.campaign_id}/fetch", json={})
        assert response.status_code == 404
        assert response.json()["error"]["code"] == "keitaro_not_found"


class TestNetworkFailures:
    def test_put_is_retried_on_502(self, editor):
        editor.add(OXYS)
        editor.fake.fail_next("PUT", r"^/streams/\d+$", status=502, body="bad gateway", times=2)
        editor.push()
        assert OXYS in editor.in_keitaro()

    def test_push_failure_keeps_draft_for_retry(self, editor):
        editor.add(OXYS)
        editor.fake.fail_next("PUT", r"^/streams/\d+$", status=500, body="boom")
        error = editor.push(expect=502)["error"]
        assert error["code"] == "keitaro_server_error"
        assert editor.stream["is_dirty"] and OXYS in editor.shares()
        editor.push()
        assert OXYS in editor.in_keitaro()

    def test_put_applied_but_connection_lost_is_healed_by_retry(self, editor):
        editor.add(OXYS)
        editor.fake.fail_next("PUT", r"^/streams/\d+$", after_effect=True,
                              exception=lambda r: httpx.ReadTimeout("slow", request=r))
        editor.push()  # повтор PUT идемпотентен: тот же набор офферов
        assert editor.in_keitaro() == {A_0009: 25, A_0008: 25, FITO: 25, OXYS: 25}
        assert not editor.stream["is_dirty"]

    def test_keitaro_down_on_fetch(self, editor):
        editor.fake.fail_next("GET", r"/streams$", times=10,
                              exception=lambda r: httpx.ConnectError("down", request=r))
        response = editor.client.post(f"/api/campaigns/{editor.campaign_id}/fetch", json={})
        assert response.status_code == 504
        assert response.json()["error"]["code"] == "keitaro_unreachable"
        assert editor.shares() == {A_0009: 33, A_0008: 33, FITO: 34}, "локальные данные целы"

    def test_cloudflare_block_is_explained(self, editor):
        editor.fake.fail_next("GET", r"/streams$", status=403, body=editor.fake.cloudflare_body())
        response = editor.client.post(f"/api/campaigns/{editor.campaign_id}/fetch", json={})
        assert "Cloudflare" in response.json()["error"]["message"]

    def test_html_instead_of_json(self, editor):
        editor.fake.fail_next("GET", r"/streams$", status=200, body="<html>login</html>")
        response = editor.client.post(f"/api/campaigns/{editor.campaign_id}/fetch", json={})
        assert response.json()["error"]["code"] == "keitaro_bad_response"

    def test_keitaro_saved_something_else(self, editor):
        editor.add(OXYS)
        editor.fake.fail_next("PUT", r"^/streams/\d+$", status=200,
                              body={"id": 1, "offers": [{"offer_id": A_0009, "share": 100,
                                                         "state": "active"}]})
        error = editor.push(expect=502)["error"]
        assert error["code"] == "push_mismatch"
        assert editor.stream["is_dirty"], "черновик не помечен опубликованным"

    def test_failures_are_written_to_operations_log(self, editor):
        editor.add(99999999, expect=422)
        last = editor.client.get("/api/operations").json()["items"][0]
        assert (last["action"], last["status"]) == ("add_offer", "error")
        assert "не найден" in last["error"]


class TestSnapshotsAndRollback:
    def test_rollback_goes_through_draft_not_straight_to_keitaro(self, editor):
        editor.add(OXYS)
        editor.push()
        snapshots = editor.client.get(f"/api/streams/{editor.stream['id']}/snapshots").json()
        assert [(o["offer_id"], o["share"]) for o in snapshots[0]["offers"]] == \
            [(A_0009, 33), (A_0008, 33), (FITO, 34)]
        restored = editor.client.post(
            f"/api/streams/{editor.stream['id']}/snapshots/{snapshots[0]['id']}/restore").json()
        assert restored["is_dirty"] is True
        assert editor.shares() == {A_0009: 33, A_0008: 33, FITO: 34} and editor.removed() == [OXYS]
        assert OXYS in editor.in_keitaro(), "до Push трекер не тронут"
        editor.push()
        assert editor.in_keitaro() == {A_0009: 33, A_0008: 33, FITO: 34}

    def test_forget_archived_offer(self, editor):
        editor.remove(FITO)
        editor.push()
        binding_id = next(o["id"] for o in editor.stream["offers"] if o["offer_id"] == FITO)
        editor.client.delete(f"/api/streams/{editor.stream['id']}/offers/{binding_id}/forget")
        assert FITO not in editor.order()

    def test_forget_is_refused_for_live_offer(self, editor):
        binding_id = next(o["id"] for o in editor.stream["offers"] if o["offer_id"] == FITO)
        response = editor.client.delete(
            f"/api/streams/{editor.stream['id']}/offers/{binding_id}/forget")
        assert response.status_code == 409
