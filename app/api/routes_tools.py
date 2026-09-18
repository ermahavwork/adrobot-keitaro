"""Инструменты сверх оригинала: «где используется оффер», массовое добавление,
публикация нескольких потоков, синхронизация всего трекера и советник долей.

Ни один из них не обходит обычный рабочий цикл: массовое добавление пишет только в черновики,
публикация идёт через тот же Push с проверкой конфликтов, советник лишь предлагает цифры.
"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Path, Query
from pydantic import BaseModel, ConfigDict, Field

from app.api.deps import ClientDep, DictionariesDep, LocksDep, SessionDep
from app.api.routes_campaigns import audited, load_offer_names
from app.models import STATE_ACTIVE
from app.services import advisor, editor, stats, usage
from app.services.advisor import AdvisorInput

router = APIRouter(tags=["Инструменты"])

MAX_ID = 2_147_483_647
OfferId = Annotated[int, Path(gt=0, le=MAX_ID)]
StreamId = Annotated[int, Path(gt=0, le=MAX_ID)]


class StreamIdsRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    stream_ids: list[Annotated[int, Field(gt=0, le=MAX_ID)]] = Field(min_length=1, max_length=100)


class ApplySharesRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    shares: dict[int, Annotated[int, Field(ge=1, le=100)]] = Field(min_length=1, max_length=200)


class SyncAllRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    only_missing: bool = Field(default=False, description="Только кампании, которые ещё не загружались")


@router.get("/offers/{offer_id}/usage", summary="Где используется оффер и куда его можно добавить")
async def offer_usage(offer_id: OfferId, session: SessionDep,
                      dictionaries: DictionariesDep) -> dict[str, Any]:
    data = await usage.offer_usage(session, offer_id)
    offer = (await dictionaries.get_offers_map(session, {offer_id})).get(offer_id)
    data["offer"] = ({"id": offer.keitaro_id, "name": offer.name, "state": offer.state}
                     if offer else {"id": offer_id, "name": f"Оффер #{offer_id}", "state": "unknown"})
    return data


@router.post("/offers/{offer_id}/bulk-add", summary="Добавить оффер в несколько потоков (в черновики)")
async def bulk_add(offer_id: OfferId, body: StreamIdsRequest, session: SessionDep,
                   dictionaries: DictionariesDep, locks: LocksDep) -> dict[str, Any]:
    async with audited(session, "bulk_add_offer") as info:
        results = await usage.bulk_add(session, dictionaries, locks, offer_id, body.stream_ids)
        added = sum(1 for r in results if r["status"] == "added")
        info["details"] = {"offer_id": offer_id, "results": results}
        info["summary"] = (f"оффер #{offer_id}: добавлен в черновики {added} из {len(results)} потоков; "
                           "в Keitaro ничего не отправлено")
    return {"results": results, "added": added}


@router.get("/streams/drafts", summary="Потоки с неопубликованными изменениями")
async def drafts(session: SessionDep) -> dict[str, list[int]]:
    return {"stream_ids": await usage.dirty_stream_ids(session)}


@router.post("/streams/push-many", summary="Опубликовать несколько потоков подряд")
async def push_many(body: StreamIdsRequest, session: SessionDep, client: ClientDep,
                    dictionaries: DictionariesDep, locks: LocksDep) -> dict[str, Any]:
    """Каждый поток проходит обычный Push (конфликт-детект, снимок, сверка ответа)."""
    async with audited(session, "push_many") as info:
        results = await usage.push_many(session, client, dictionaries, locks, body.stream_ids)
        pushed = sum(1 for r in results if r["status"] == "pushed")
        info["details"] = {"results": results}
        info["summary"] = f"опубликовано потоков: {pushed} из {len(results)}"
    return {"results": results, "pushed": pushed}


@router.post("/campaigns/sync-all", summary="Fetch streams по всем кампаниям трекера")
async def sync_all(session: SessionDep, client: ClientDep, locks: LocksDep,
                   dictionaries: DictionariesDep,
                   body: SyncAllRequest | None = None) -> dict[str, Any]:
    body = body or SyncAllRequest()
    async with audited(session, "sync_all") as info:
        result = await usage.sync_all(session, client, locks, only_missing=body.only_missing)
        info["details"] = result
        info["summary"] = f"синхронизировано кампаний: {result['synced']}, с ошибкой: {len(result['failed'])}"
    await load_offer_names(session, dictionaries)
    return result


@router.get("/streams/{stream_id}/advice", summary="Советник долей: предложение по статистике")
async def advice(stream_id: StreamId, session: SessionDep, client: ClientDep,
                 dictionaries: DictionariesDep,
                 period: Annotated[str, Query(pattern="^(today|7d|30d)$")] = "7d",
                 metric: Annotated[str, Query(pattern="^(cr|epc)$")] = "cr") -> dict[str, Any]:
    """Ничего не меняет. Применить предложение — отдельным вызовом `apply-shares`."""
    stream = await editor.load_stream(session, stream_id)
    report = await stats.campaign_stats(client, stream.campaign.keitaro_id, period)
    if not report.get("available"):
        return {"ready": False, "metric": metric, "period": period, "items": [],
                "message": "Статистика недоступна: " + str(report.get("reason") or "нет доступа к отчётам.")}
    active = [b for b in stream.bindings if b.state == STATE_ACTIVE]
    rows = report["offers"]
    inputs = []
    for binding in active:
        row = rows.get(f"{stream.keitaro_id}:{binding.offer_id}", {})
        inputs.append(AdvisorInput(key=binding.id, share=binding.share, pinned=binding.is_pinned,
                                   clicks=int(row.get("clicks", 0)),
                                   conversions=int(row.get("conversions", 0)),
                                   revenue=float(row.get("revenue", 0.0))))
    result = advisor.advise(inputs, metric=metric)
    names = await dictionaries.get_offers_map(session, {b.offer_id for b in active})
    offer_of = {b.id: b.offer_id for b in active}
    return {
        "ready": result.ready, "changed": result.changed, "message": result.message,
        "metric": result.metric, "period": period,
        "items": [{"binding_id": a.key, "offer_id": offer_of[a.key],
                   "offer_name": names[offer_of[a.key]].name if offer_of[a.key] in names
                   else f"Оффер #{offer_of[a.key]}",
                   "current": a.current, "proposed": a.proposed, "score": a.score, "reason": a.reason}
                  for a in result.items],
    }


@router.post("/streams/{stream_id}/apply-shares", summary="Применить набор долей в черновик")
async def apply_shares(stream_id: StreamId, body: ApplySharesRequest, session: SessionDep,
                       dictionaries: DictionariesDep, locks: LocksDep) -> dict[str, Any]:
    campaign_id = (await editor.load_stream(session, stream_id)).campaign_id
    async with locks.for_campaign(campaign_id):
        stream = await editor.load_stream(session, stream_id)
        async with audited(session, "apply_shares", keitaro_campaign_id=stream.campaign.keitaro_id,
                           keitaro_stream_id=stream.keitaro_id) as info:
            info["details"] = {"shares": body.shares}
            await editor.apply_shares(session, stream, body.shares)
            info["summary"] = f"поток «{stream.name}»: доли из совета применены в черновик"
        stream = await editor.load_stream(session, stream_id)
        offers = await dictionaries.get_offers_map(session, {b.offer_id for b in stream.bindings})
        return editor.stream_view(stream, offers)
