"""Редактор: редкие ветки, которых нет в основном сценарии и в тестах сбоев.

Отказы 409/404 на «неправильные» кнопки, чужие привязки и снимки, лимит снимков, лендинги
потока, странные данные из Keitaro во входе Fetch, много офферов, повторный Fetch, кампания
без потоков и поток, у которого в Keitaro сменили схему.
"""

from __future__ import annotations

import pytest

from tests.conftest import Editor

A_0009, A_0008, FITO, OXYS, SPARE = 3749, 3717, 11111, 11112, 13972


def call(editor: Editor, method: str, tail: str, expect: int, body: dict | None = None) -> dict:
    response = editor.client.request(method, f"/api/streams/{editor.stream['id']}{tail}", json=body)
    assert response.status_code == expect, response.text
    return response.json()


def binding(editor: Editor, offer_id: int) -> int:
    return next(o["id"] for o in editor.stream["offers"] if o["offer_id"] == offer_id)


def row(editor: Editor, offer_id: int) -> dict:
    return next(o for o in editor.stream["offers"] if o["offer_id"] == offer_id)


def snapshots(editor: Editor) -> list[dict]:
    return call(editor, "GET", "/snapshots", 200)  # type: ignore[return-value]


def last_operation(editor: Editor) -> dict:
    return editor.client.get("/api/operations").json()["items"][0]


class TestWrongButtonIsRefusedPolitely:
    def test_recalculate_with_no_active_offers(self, editor):
        for offer in (A_0009, A_0008, FITO):
            editor.remove(offer)
        error = call(editor, "POST", "/recalculate", 409)["error"]
        assert error["code"] == "no_active_offers"
        assert call(editor, "POST", "/recalculate", 409, {"drop_pins": True})["error"]["code"] == \
            "no_active_offers"

    def test_recalculate_on_stream_without_offers(self, editor):
        campaign = editor.client.get(f"/api/campaigns/{editor.campaign_id}").json()
        flow1 = campaign["streams"][0]["id"]
        response = editor.client.post(f"/api/streams/{flow1}/recalculate")
        assert response.status_code == 409
        assert response.json()["error"]["code"] == "no_active_offers"

    def test_pin_of_removed_offer(self, editor):
        editor.remove(FITO)
        assert editor.pin(FITO, expect=409)["error"]["code"] == "not_active"
        assert row(editor, FITO)["is_pinned"] is False

    def test_share_of_removed_offer(self, editor):
        editor.remove(FITO)
        assert editor.share(FITO, 40, expect=409)["error"]["code"] == "not_active"
        assert editor.shares() == {A_0009: 50, A_0008: 50}

    def test_pin_and_share_of_offer_disabled_in_keitaro(self, editor):
        editor.fake.streams[editor.kt_stream_id]["offers"][0]["state"] = "disabled"
        editor.fetch()
        assert row(editor, A_0009)["state"] == "disabled"
        assert editor.pin(A_0009, expect=409)["error"]["code"] == "not_active"
        assert editor.share(A_0009, 40, expect=409)["error"]["code"] == "not_active"

    def test_bring_back_of_active_offer(self, editor):
        assert editor.bring_back(FITO, expect=409)["error"]["code"] == "not_removed"
        assert editor.shares() == {A_0009: 33, A_0008: 33, FITO: 34}
        assert not editor.stream["is_dirty"]

    def test_remove_twice(self, editor):
        editor.remove(FITO)
        assert editor.remove(FITO, expect=409)["error"]["code"] == "already_removed"
        assert editor.shares() == {A_0009: 50, A_0008: 50}, "второе нажатие ничего не сломало"

    def test_forget_of_offer_removed_only_in_draft(self, editor):
        editor.remove(FITO)  # в Keitaro оффер ещё стоит — забывать рано
        error = call(editor, "DELETE", f"/offers/{binding(editor, FITO)}/forget", 409)["error"]
        assert error["code"] == "not_archived"
        assert FITO in editor.removed()

    def test_push_of_stream_deleted_in_keitaro(self, editor):
        editor.add(OXYS)
        editor.fake.streams[editor.kt_stream_id]["state"] = "deleted"
        editor.fetch()
        assert editor.push(expect=409)["error"]["code"] == "stream_deleted"

    def test_cancel_with_nothing_to_cancel_is_harmless(self, editor):
        assert editor.cancel()["is_dirty"] is False
        assert editor.shares() == {A_0009: 33, A_0008: 33, FITO: 34}
        assert "отменено изменений — 0" in last_operation(editor)["summary"]

    def test_refusals_are_journaled_with_their_reason(self, editor):
        editor.bring_back(FITO, expect=409)
        record = last_operation(editor)
        assert (record["action"], record["status"]) == ("bring_back", "error")
        assert "не в архиве" in record["error"]


class TestForeignIds:
    @pytest.fixture
    def stranger(self, editor) -> int:
        """ID привязки из потока ДРУГОЙ кампании."""
        other = Editor(editor.client, editor.fake, *editor.fake.seed_campaign("other")[::2])
        other.remove(FITO)
        return binding(other, FITO)

    @pytest.mark.parametrize(("method", "tail", "body"), [
        ("DELETE", "/offers/{id}", None),
        ("POST", "/offers/{id}/bring-back", None),
        ("PUT", "/offers/{id}/pin", {"pinned": True}),
        ("PUT", "/offers/{id}/share", {"share": 40}),
        ("DELETE", "/offers/{id}/forget", None),
    ])
    def test_binding_of_another_stream_is_not_found(self, editor, stranger, method, tail, body):
        error = call(editor, method, tail.format(id=stranger), 404, body)["error"]
        assert error["code"] == "binding_not_found"
        assert editor.shares() == {A_0009: 33, A_0008: 33, FITO: 34} and not editor.stream["is_dirty"]

    @pytest.mark.parametrize(("method", "tail", "body"), [
        ("DELETE", "/offers/424242", None),
        ("POST", "/offers/424242/bring-back", None),
        ("PUT", "/offers/424242/pin", {"pinned": False}),
        ("PUT", "/offers/424242/share", {"share": 1}),
        ("DELETE", "/offers/424242/forget", None),
    ])
    def test_unknown_binding_is_not_found(self, editor, method, tail, body):
        assert call(editor, method, tail, 404, body)["error"]["code"] == "binding_not_found"

    def test_snapshot_of_another_stream_cannot_be_restored(self, editor):
        other = Editor(editor.client, editor.fake, *editor.fake.seed_campaign("other")[::2])
        other.add(OXYS)
        other.push()
        foreign = snapshots(other)[0]["id"]
        editor.add(SPARE)
        error = call(editor, "POST", f"/snapshots/{foreign}/restore", 404)["error"]
        assert error["code"] == "snapshot_not_found"
        assert SPARE in editor.shares(), "черновик не тронут"

    def test_unknown_snapshot_cannot_be_restored(self, editor):
        assert call(editor, "POST", "/snapshots/424242/restore", 404)["error"]["code"] == \
            "snapshot_not_found"

    def test_snapshot_cannot_be_restored_into_stream_without_offers(self, editor):
        editor.add(OXYS)
        editor.push()
        snapshot = snapshots(editor)[0]["id"]
        flow1 = editor.client.get(f"/api/campaigns/{editor.campaign_id}").json()["streams"][0]["id"]
        response = editor.client.post(f"/api/streams/{flow1}/snapshots/{snapshot}/restore")
        assert response.status_code == 409
        assert response.json()["error"]["code"] == "stream_without_offers"


class TestSnapshots:
    def test_only_the_newest_snapshots_are_kept(self, editor, monkeypatch):
        monkeypatch.setattr("app.services.editor.MAX_SNAPSHOTS_PER_STREAM", 3)
        for value in (40, 41, 42, 43, 44):
            editor.share(FITO, value)
            editor.push()
        kept = snapshots(editor)
        assert len(kept) == 3
        assert [s["id"] for s in kept] == sorted((s["id"] for s in kept), reverse=True)
        fito_before_push = [next(o["share"] for o in s["offers"] if o["offer_id"] == FITO)
                            for s in kept]
        assert fito_before_push == [43, 42, 41], "остались три последних состояния «до Push»"

    def test_limit_is_per_stream(self, editor, monkeypatch):
        monkeypatch.setattr("app.services.editor.MAX_SNAPSHOTS_PER_STREAM", 1)
        other = Editor(editor.client, editor.fake, *editor.fake.seed_campaign("other")[::2])
        for stream in (editor, other, editor, other):
            stream.share(FITO, 40 if stream.shares()[FITO] != 40 else 50)
            stream.push()
        assert len(snapshots(editor)) == 1 and len(snapshots(other)) == 1

    def test_snapshot_is_saved_before_put_and_not_duplicated_by_retry(self, editor):
        """Снимок фиксируется ДО отправки: если PUT применится, а ответ потеряется, точка отката
        уже есть. Повтор публикации того же состояния второй такой же снимок не плодит."""
        editor.add(OXYS)
        editor.fake.fail_next("PUT", r"^/streams/\d+$", status=500, body="boom")
        editor.push(expect=502)
        first = snapshots(editor)
        assert len(first) == 1
        assert {o["offer_id"]: o["share"] for o in first[0]["offers"]} == editor.in_keitaro()
        editor.push()
        assert len(snapshots(editor)) == 1, "то же состояние трекера — тот же снимок"

    def test_snapshot_shows_offer_names_and_placeholder_for_unknown(self, editor):
        editor.add(OXYS)
        editor.push()
        del editor.fake.offers[FITO]
        editor.client.post("/api/offers/refresh")
        names = {o["offer_id"]: o["offer_name"] for o in snapshots(editor)[0]["offers"]}
        assert names[A_0009].startswith("Miaflow") and names[FITO]

    def test_restore_recreates_offer_that_was_forgotten(self, editor):
        editor.remove(FITO)
        editor.push()  # снимок №1: 33/33/34 с FITO
        call(editor, "DELETE", f"/offers/{binding(editor, FITO)}/forget", 200)
        assert FITO not in editor.order()
        oldest = snapshots(editor)[-1]["id"]
        restored = call(editor, "POST", f"/snapshots/{oldest}/restore", 200)
        assert restored["is_dirty"] is True
        assert editor.shares() == {A_0009: 33, A_0008: 33, FITO: 34}
        assert row(editor, FITO)["change"] == "add"
        editor.push()
        assert editor.in_keitaro() == {A_0009: 33, A_0008: 33, FITO: 34}

    def test_restore_drops_pins_and_unpublished_additions(self, editor):
        editor.add(OXYS)
        editor.push()
        editor.pin(A_0009)
        editor.add(SPARE)
        call(editor, "POST", f"/snapshots/{snapshots(editor)[0]['id']}/restore", 200)
        assert SPARE not in editor.order(), "неопубликованное добавление просто исчезает"
        assert editor.removed() == [OXYS]
        assert row(editor, A_0009)["is_pinned"] is False


class TestLandingsSurvivePush:
    LANDINGS = ({"landing_id": 1, "share": 60, "state": "active"},
                {"landing_id": 2, "share": 40, "state": "active"})

    def test_push_sends_offers_only_and_landings_stay(self, editor):
        stream = editor.fake.streams[editor.kt_stream_id]
        stream["landings"] = [dict(landing) for landing in self.LANDINGS]
        stream["filters"] = [{"name": "os", "mode": "accept", "payload": ["Android"]}]
        editor.fetch()
        assert editor.stream["summary"]["landings"] == 2
        editor.add(OXYS)
        editor.remove(A_0008)
        editor.fake.requests.clear()
        editor.push()
        assert stream["landings"] == [dict(landing) for landing in self.LANDINGS]
        assert stream["filters"][0]["payload"] == ["Android"] and stream["schema"] == "landings"
        bodies = [body for method, _, body in editor.fake.requests if method == "PUT"]
        assert [list(body) for body in bodies] == [["offers"]]

    def test_force_push_and_empty_push_keep_landings_too(self, editor):
        stream = editor.fake.streams[editor.kt_stream_id]
        stream["landings"] = [dict(landing) for landing in self.LANDINGS]
        editor.fetch()
        for offer in (A_0009, A_0008, FITO):
            editor.remove(offer)
        editor.fake.set_stream_offers(editor.kt_stream_id, [(A_0009, 100)])
        editor.push(force=True, allow_empty=True)
        assert editor.in_keitaro() == {}
        assert stream["landings"] == [dict(landing) for landing in self.LANDINGS]


class TestStrangeDataFromKeitaro:
    def test_duplicate_offer_and_wrong_sum_do_not_break_fetch(self, editor):
        editor.fake.streams[editor.kt_stream_id]["offers"] = [
            {"id": 1, "offer_id": A_0009, "share": 70, "state": "active"},
            {"id": 2, "offer_id": A_0009, "share": 10, "state": "active"},  # дубль: берём первый
            {"id": 3, "offer_id": A_0008, "share": 70, "state": "active"},
        ]
        fetched = editor.fetch()
        assert fetched["result"]["archived_offers"] == [FITO]
        assert editor.order().count(A_0009) == 1
        assert editor.shares() == {A_0009: 70, A_0008: 70}
        view = editor.stream
        assert view["total_share"] == 140 and any("140%" in p for p in view["problems"])
        assert view["is_dirty"] is False, "зеркалим трекер как есть, ничего не «чиним» молча"

    def test_wrong_sum_is_fixed_by_recalculate_and_forced_push(self, editor):
        editor.fake.streams[editor.kt_stream_id]["offers"] = [
            {"id": 1, "offer_id": A_0009, "share": 70, "state": "active"},
            {"id": 2, "offer_id": A_0009, "share": 10, "state": "active"},
            {"id": 3, "offer_id": A_0008, "share": 70, "state": "active"},
        ]
        editor.fetch()
        call(editor, "POST", "/recalculate", 200)
        assert editor.push(expect=409)["error"]["code"] == "conflict", "дубль в трекере ≠ наша база"
        editor.push(force=True)
        assert editor.in_keitaro() == {A_0009: 50, A_0008: 50}
        editor.fetch()
        assert not editor.stream["is_dirty"] and editor.stream["problems"] == []

    def test_junk_rows_among_offers_are_skipped(self, editor):
        editor.fake.streams[editor.kt_stream_id]["offers"] = [
            {"id": 1, "offer_id": A_0009, "share": 100, "state": "active"},
            {"id": 2, "offer_id": "not-a-number", "share": 5, "state": "active"},
            {"id": 3, "share": 5}, "junk", None, 42,
        ]
        editor.fetch()
        assert editor.shares() == {A_0009: 100}

    @pytest.mark.parametrize(("share", "expected"), [(33.9, 33), (None, 0), ("50", 0), (-5, -5)])
    def test_odd_share_values_do_not_break_fetch(self, editor, share, expected):
        editor.fake.streams[editor.kt_stream_id]["offers"] = [
            {"id": 1, "offer_id": A_0009, "share": share, "state": "active"}]
        editor.fetch()
        assert editor.shares() == {A_0009: expected}
        assert editor.stream["problems"], "не 100% — интерфейс об этом говорит"

    def test_unknown_binding_state_is_treated_as_active(self, editor):
        editor.fake.streams[editor.kt_stream_id]["offers"][0]["state"] = "paused"
        editor.fetch()
        assert row(editor, A_0009)["state"] == "active"

    def test_stream_without_offers_key_or_name(self, editor):
        stream = editor.fake.streams[editor.kt_stream_id]
        del stream["offers"]
        stream["name"] = None
        try:
            fetched = editor.fetch()
        finally:
            stream["offers"] = []  # эмулятору ключ нужен для следующих запросов
        assert sorted(fetched["result"]["archived_offers"]) == sorted([A_0009, A_0008, FITO])
        assert editor.stream["name"] == "" and editor.shares() == {}

    def test_offer_unknown_to_dictionary_gets_placeholder_name(self, editor):
        editor.fake.set_stream_offers(editor.kt_stream_id, [(A_0009, 50), (987654, 50)])
        editor.fetch()
        unknown = row(editor, 987654)
        assert unknown["offer_known"] is False and unknown["offer_name"] == "Оффер #987654"
        assert unknown["offer_state"] == "unknown" and unknown["share"] == 50


class TestManyOffers:
    def test_forty_offers_share_100_percent_fairly(self, editor):
        extra = list(range(30000, 30037))
        for offer_id in extra:
            editor.fake.add_offer(offer_id, f"массовый оффер {offer_id}")
            editor.add(offer_id)
        shares = editor.shares()
        assert len(shares) == 40 and sum(shares.values()) == 100
        assert sorted(set(shares.values())) == [2, 3], "100 / 40: двадцать по 2% и двадцать по 3%"
        assert [shares[offer_id] for offer_id in extra[-20:]] == [3] * 20, "остаток — последним"
        editor.push()
        assert editor.in_keitaro() == shares

    def test_hundred_offers_get_one_percent_each_and_one_more_does_not_fit(self, editor):
        offers = [(40000 + index, 1) for index in range(100)]
        for offer_id, _ in offers:
            editor.fake.add_offer(offer_id, f"оффер {offer_id}")
        editor.fake.set_stream_offers(editor.kt_stream_id, offers)
        editor.fetch()
        assert editor.stream["active_count"] == 100 and editor.stream["problems"] == []
        error = editor.add(OXYS, expect=422)["error"]
        assert error["code"] == "not_enough_for_free"
        assert editor.stream["active_count"] == 100 and not editor.stream["is_dirty"]


class TestRepeatedFetch:
    @staticmethod
    def frozen(editor: Editor) -> dict:
        view = editor.client.get(f"/api/campaigns/{editor.campaign_id}").json()
        view.pop("streams_fetched_at")
        return view

    def test_second_fetch_changes_nothing(self, editor):
        before = self.frozen(editor)
        fetched = editor.fetch()
        assert fetched["result"] == {"streams": 2, "gone_streams": 0, "archived_offers": [],
                                     "changed": False}
        assert self.frozen(editor) == before

    def test_second_fetch_keeps_draft_pins_and_archive_as_they_are(self, editor):
        editor.remove(A_0008)
        editor.push()
        editor.add(OXYS)
        editor.pin(FITO)
        before = self.frozen(editor)
        for _ in range(2):
            assert editor.fetch()["result"]["archived_offers"] == []
        assert self.frozen(editor) == before

    def test_fetch_time_is_updated(self, editor):
        first = editor.client.get(f"/api/campaigns/{editor.campaign_id}").json()["streams_fetched_at"]
        editor.fetch()
        second = editor.client.get(f"/api/campaigns/{editor.campaign_id}").json()["streams_fetched_at"]
        assert first and second and second >= first

    def test_new_stream_appears_and_vanished_one_is_marked(self, editor):
        kt_campaign = next(iter(editor.fake.campaigns))
        editor.fake.streams[5555] = {
            "id": 5555, "campaign_id": kt_campaign, "name": "Flow 3", "position": 3,
            "schema": "landings", "type": "regular", "state": "active", "action_type": "http",
            "filters": [], "landings": [], "offers": []}
        editor.fake.set_stream_offers(5555, [(OXYS, 100)])
        editor.fake.streams[editor.kt_stream_id]["state"] = "deleted"
        result = editor.fetch()["result"]
        assert (result["streams"], result["gone_streams"]) == (2, 1)
        streams = {s["name"]: s for s in
                   editor.client.get(f"/api/campaigns/{editor.campaign_id}").json()["streams"]}
        assert streams["Flow 3"]["offers"][0]["offer_id"] == OXYS
        assert streams["Flow 2"]["is_deleted"] is True and len(streams["Flow 2"]["offers"]) == 3
        editor.fake.streams[editor.kt_stream_id]["state"] = "active"
        assert editor.fetch()["result"]["gone_streams"] == 0
        assert editor.stream["is_deleted"] is False, "поток восстановили в Keitaro"


class TestCampaignWithoutStreams:
    @pytest.fixture
    def bare(self, client, fake) -> tuple[int, int]:
        """Кампания, у которой в Keitaro нет ни одного потока. -> (локальный ID, Keitaro ID)."""
        kt_campaign, flow1, flow2 = fake.seed_campaign("no streams yet")
        del fake.streams[flow1], fake.streams[flow2]
        return client.post(f"/api/campaigns/open/{kt_campaign}").json()["id"], kt_campaign

    def test_fetch_gives_empty_editor(self, client, bare):
        local, _ = bare
        fetched = client.post(f"/api/campaigns/{local}/fetch", json={}).json()
        assert fetched["result"] == {"streams": 0, "gone_streams": 0, "archived_offers": [],
                                     "changed": False}
        view = client.get(f"/api/campaigns/{local}").json()
        assert view["streams"] == [] and view["is_dirty"] is False and view["streams_fetched_at"]

    def test_campaign_is_not_listed_as_draft(self, client, bare):
        client.post(f"/api/campaigns/{bare[0]}/fetch", json={})
        assert client.get("/api/campaigns", params={"only_drafts": "true"}).json()["total"] == 0
        assert client.get("/api/campaigns").json()["items"][0]["has_draft"] is False

    def test_stream_created_later_is_picked_up_by_next_fetch(self, client, fake, bare):
        local, kt_campaign = bare
        client.post(f"/api/campaigns/{local}/fetch", json={})
        fake.streams[7777] = {
            "id": 7777, "campaign_id": kt_campaign, "name": "Flow 1", "position": 1,
            "schema": "landings", "type": "regular", "state": "active", "action_type": "http",
            "filters": [], "landings": [], "offers": []}
        fake.set_stream_offers(7777, [(A_0009, 100)])
        editor = Editor(client, fake, kt_campaign, 7777)
        editor.add(OXYS)
        editor.push()
        assert editor.in_keitaro() == {A_0009: 50, OXYS: 50}

    def test_stats_and_archive_work_without_streams(self, client, fake, bare):
        local, kt_campaign = bare
        stats = client.get(f"/api/campaigns/{local}/stats").json()
        assert stats["available"] is True and stats["offers"] == {}
        assert client.delete(f"/api/campaigns/{local}").status_code == 200
        assert fake.campaigns[kt_campaign]["state"] == "deleted"


class TestSchemaSwitchedInKeitaro:
    """Поток «Лендинги и офферы» в Keitaro переделали в редирект уже после нашего Fetch."""

    @staticmethod
    def switch_to_redirect(editor: Editor) -> None:
        stream = editor.fake.streams[editor.kt_stream_id]
        stream.update(schema="redirect", action_payload="https://example.org", offers=[])

    def test_offers_go_to_archive_and_view_stays_sane(self, editor):
        self.switch_to_redirect(editor)
        fetched = editor.fetch()
        assert sorted(fetched["result"]["archived_offers"]) == sorted([A_0009, A_0008, FITO])
        view = editor.stream
        assert (view["schema"], view["supports_offers"]) == ("redirect", False)
        assert view["is_dirty"] is False and view["problems"] == [] and view["total_share"] == 0
        assert sorted(editor.removed()) == sorted([A_0009, A_0008, FITO])
        assert view["summary"]["action_payload"] == "https://example.org"

    def test_editing_buttons_are_refused_on_switched_stream(self, editor):
        self.switch_to_redirect(editor)
        editor.fetch()
        assert editor.add(OXYS, expect=409)["error"]["code"] == "stream_without_offers"
        assert editor.bring_back(FITO, expect=409)["error"]["code"] == "stream_without_offers"
        assert editor.push(expect=409)["error"]["code"] == "stream_without_offers"

    def test_unpublished_draft_cannot_be_pushed_but_can_be_cancelled(self, editor):
        editor.add(OXYS)
        self.switch_to_redirect(editor)
        editor.fetch()
        view = editor.stream
        assert view["is_dirty"] is True and view["problems"] == [], "черновик сохранён, не мешает"
        assert editor.push(expect=409)["error"]["code"] == "stream_without_offers"
        assert editor.in_keitaro() == {}, "в редирект-поток офферы не уехали"
        assert editor.cancel()["is_dirty"] is False
        assert OXYS not in editor.order() and sorted(editor.removed()) == sorted([A_0009, A_0008,
                                                                                  FITO])

    def test_discard_draft_on_switched_stream(self, editor):
        editor.add(OXYS)
        self.switch_to_redirect(editor)
        editor.fetch(discard_draft=True)
        assert not editor.stream["is_dirty"] and OXYS not in editor.order()

    def test_switching_back_returns_offers_from_keitaro(self, editor):
        self.switch_to_redirect(editor)
        editor.fetch()
        editor.fake.streams[editor.kt_stream_id]["schema"] = "landings"
        editor.fake.set_stream_offers(editor.kt_stream_id, [(A_0009, 50), (FITO, 50)])
        editor.fetch()
        assert editor.shares() == {A_0009: 50, FITO: 50} and editor.removed() == [A_0008]
        assert editor.stream["supports_offers"] is True and not editor.stream["is_dirty"]


class TestPinsAndOrder:
    def test_bring_back_does_not_resurrect_old_pin(self, editor):
        editor.share(FITO, 50)
        editor.remove(FITO)
        editor.bring_back(FITO)
        assert row(editor, FITO)["is_pinned"] is False
        assert editor.shares() == {A_0009: 33, A_0008: 33, FITO: 34}

    def test_remove_and_bring_back_before_push_is_not_a_change(self, editor):
        editor.remove(FITO)
        editor.bring_back(FITO)
        assert editor.shares() == {A_0009: 33, A_0008: 33, FITO: 34}
        assert editor.stream["is_dirty"] is False and editor.stream["diff"] == []

    def test_unpinned_share_joins_next_rebalance(self, editor):
        editor.share(FITO, 50)
        editor.pin(FITO, pinned=False)
        assert editor.shares()[FITO] == 50, "снятие закрепления само цифр не меняет"
        editor.add(OXYS)
        assert editor.shares() == {A_0009: 25, A_0008: 25, FITO: 25, OXYS: 25}

    def test_share_of_single_offer_can_only_be_100(self, editor):
        editor.remove(A_0009)
        editor.remove(A_0008)
        assert editor.shares() == {FITO: 100}
        assert editor.share(FITO, 60, expect=422)["error"]["code"] == "pinned_sum_mismatch"
        editor.share(FITO, 100)
        assert row(editor, FITO)["is_pinned"] is True

    def test_diff_lists_every_kind_of_change(self, editor):
        editor.remove(A_0008)
        editor.add(OXYS)
        changes = {c["offer_id"]: (c["type"], c["from"], c["to"]) for c in editor.stream["diff"]}
        assert changes == {A_0008: ("remove", 33, None), FITO: ("share", 34, 33),
                           OXYS: ("add", None, 34)}, "0009 остался при своих 33% — его в diff нет"
