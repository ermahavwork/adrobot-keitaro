"""Сценарий из видео ТЗ (jNloscEWCjA), шаг за шагом, с теми же цифрами.

Офферы как в ролике: 3749 = Miaflow…0009, 3717 = Miaflow…0008, 11111 = FitoMishki,
11112 = Oxys. Каждая проверка подписана таймкодом ролика.
"""

from __future__ import annotations

A_0009, A_0008, FITO, OXYS = 3749, 3717, 11111, 11112


def test_full_video_walkthrough(editor):
    # 0:36 Fetch streams: поток с офферами показывает офферы, поток без офферов — только шапку
    campaign = editor.client.get(f"/api/campaigns/{editor.campaign_id}").json()
    flow1, flow2 = campaign["streams"]
    assert (flow1["name"], flow1["offers"], flow1["supports_offers"]) == ("Flow 1", [], False)
    assert flow2["name"] == "Flow 2" and not flow2["is_dirty"]
    assert editor.shares() == {A_0009: 33, A_0008: 33, FITO: 34}
    assert editor.order() == [FITO, A_0009, A_0008], "сортировка как в оригинале: по доле ↓"

    # 2:01 Add: четыре оффера по 25%, поток «жёлтый», в Keitaro пока три
    added = editor.add(OXYS)
    assert added["is_dirty"] is True
    assert editor.shares() == {A_0009: 25, A_0008: 25, FITO: 25, OXYS: 25}
    assert editor.order() == [A_0009, A_0008, FITO, OXYS]
    assert editor.in_keitaro() == {A_0009: 33, A_0008: 33, FITO: 34}

    # 2:28 Push to KT: в Keitaro четыре оффера по 25, поток снова «белый»
    pushed = editor.push()
    assert pushed["is_dirty"] is False
    assert editor.in_keitaro() == {A_0009: 25, A_0008: 25, FITO: 25, OXYS: 25}

    # 2:55 Cancel: добавили ещё один оффер и отменили — привязка исчезла, поток «белый»
    editor.add(13972)
    assert editor.shares() == {A_0009: 20, A_0008: 20, FITO: 20, OXYS: 20, 13972: 20}
    cancelled = editor.cancel()
    assert cancelled["is_dirty"] is False
    assert editor.shares() == {A_0009: 25, A_0008: 25, FITO: 25, OXYS: 25}
    assert 13972 not in editor.order(), "привязка удалена из базы"
    assert editor.client.get("/api/offers", params={"q": "13972"}).json(), "оффер остался"

    # 3:25 Remove: оффер сереет, 0%, остальные 33/33/34 (34 — у последнего добавленного)
    removed = editor.remove(A_0008)
    assert removed["is_dirty"] is True
    assert editor.shares() == {A_0009: 33, FITO: 33, OXYS: 34}
    assert editor.removed() == [A_0008]
    assert editor.order() == [OXYS, A_0009, FITO, A_0008], "удалённый — в конце списка"
    assert editor.in_keitaro()[A_0008] == 25, "до Push в Keitaro ничего не меняется"

    # 3:40 Push: оффер исчез из Keitaro, но остался в архиве AdRobot с кнопкой Bring back
    editor.push()
    assert editor.in_keitaro() == {A_0009: 33, FITO: 33, OXYS: 34}
    assert editor.removed() == [A_0008] and not editor.stream["is_dirty"]

    # 3:56 Bring back → поток жёлтый → Push → оффер вернулся в Keitaro
    assert editor.bring_back(A_0008)["is_dirty"] is True
    assert editor.shares() == {A_0009: 25, FITO: 25, OXYS: 25, A_0008: 25}
    editor.push()
    assert editor.in_keitaro() == {A_0009: 25, FITO: 25, OXYS: 25, A_0008: 25}

    # 4:13 В Keitaro руками удалили два оффера; 4:30 Fetch streams again:
    # они не пропали, а стали removed; доли оставшихся НЕ пересчитались (25/25, как в Keitaro)
    editor.fake.set_stream_offers(editor.kt_stream_id, [(OXYS, 25), (A_0008, 25)])
    fetched = editor.fetch()
    assert sorted(fetched["result"]["archived_offers"]) == [A_0009, FITO]
    assert editor.shares() == {A_0008: 25, OXYS: 25}
    assert sorted(editor.removed()) == [A_0009, FITO]
    assert editor.stream["is_dirty"] is False
    assert editor.order()[:2] == [A_0008, OXYS], "при равных долях — порядок появления в AdRobot"

    # 5:01 Pin 25% у 0008: цифры те же, поток НЕ желтеет
    pinned = editor.pin(A_0008)
    assert pinned["is_dirty"] is False
    assert editor.shares() == {A_0008: 25, OXYS: 25}

    # 5:21 Bring back 0009: закреплённый остался 25, остальные 37/38 (38 — возвращённому)
    editor.bring_back(A_0009)
    assert editor.shares() == {A_0008: 25, OXYS: 37, A_0009: 38}
    assert editor.order() == [A_0009, OXYS, A_0008, FITO]

    # 5:43 Push: в Keitaro 37 / 25 / 38 — ровно как в финальном кадре ролика
    editor.push()
    assert editor.in_keitaro() == {OXYS: 37, A_0008: 25, A_0009: 38}
    assert sum(editor.in_keitaro().values()) == 100


def test_push_sends_single_partial_put_with_only_offers(editor):
    editor.add(OXYS)
    editor.fake.requests.clear()
    editor.push()
    puts = [(m, p, b) for m, p, b in editor.fake.requests if m == "PUT"]
    assert len(puts) == 1
    _, path, body = puts[0]
    assert path == f"/streams/{editor.kt_stream_id}"
    assert list(body) == ["offers"], "остальные поля потока (фильтры, лендинги) не трогаем"
    assert [o["offer_id"] for o in body["offers"]] == [A_0009, A_0008, FITO, OXYS]
    assert all(o["state"] == "active" for o in body["offers"])


def test_cancel_restores_removed_offer_and_its_share(editor):
    editor.remove(FITO)
    assert editor.shares() == {A_0009: 50, A_0008: 50}
    editor.cancel()
    assert editor.shares() == {A_0009: 33, A_0008: 33, FITO: 34}
    assert editor.removed() == []


def test_cancel_after_bring_back_returns_offer_to_archive(editor):
    editor.remove(FITO)
    editor.push()
    editor.bring_back(FITO)
    editor.cancel()
    assert editor.removed() == [FITO]
    assert editor.shares() == {A_0009: 50, A_0008: 50}
    assert not editor.stream["is_dirty"]


def test_remove_of_unpublished_offer_then_push_never_reaches_keitaro(editor):
    editor.add(OXYS)
    editor.remove(OXYS)
    assert editor.shares() == {A_0009: 33, A_0008: 33, FITO: 34}
    assert editor.stream["is_dirty"] is False, "добавил и убрал до публикации — изменений нет"
    editor.push(expect=409)


def test_adding_archived_offer_is_bring_back(editor):
    editor.remove(FITO)
    editor.push()
    editor.add(FITO)
    assert editor.shares() == {A_0009: 33, A_0008: 33, FITO: 34}
    assert editor.order().count(FITO) == 1


def test_stream_without_offers_rejects_add(editor):
    campaign = editor.client.get(f"/api/campaigns/{editor.campaign_id}").json()
    flow1 = campaign["streams"][0]
    response = editor.client.post(f"/api/streams/{flow1['id']}/offers", json={"offer_id": OXYS})
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "stream_without_offers"
