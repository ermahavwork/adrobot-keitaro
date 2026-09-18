"""Маршруты редактора потока: Add / Remove / Bring back / Pin / Share / Push / Cancel / откат.

Каждая правка возвращает свежий вид потока — интерфейс всегда показывает то, что в базе.
Все правки одной кампании идут строго по очереди (замок), поэтому двойной клик безопасен.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter

from app.api.deps import ClientDep, DictionariesDep, LocksDep, SessionDep
from app.api.routes_campaigns import audited
from app.models import Stream
from app.schemas import AddOfferRequest, PinRequest, PushRequest, RecalculateRequest, ShareRequest
from app.services import editor

router = APIRouter(prefix="/streams", tags=["Редактор потока"])


async def _view(session: SessionDep, dictionaries: DictionariesDep, stream_id: int) -> dict:
    stream = await editor.load_stream(session, stream_id)
    offers = await dictionaries.get_offers_map(session, {b.offer_id for b in stream.bindings})
    return editor.stream_view(stream, offers)


async def _campaign_id(session: SessionDep, stream_id: int) -> int:
    return (await editor.load_stream(session, stream_id)).campaign_id


def _ids(stream: Stream) -> dict[str, int]:
    return {"keitaro_campaign_id": stream.campaign.keitaro_id,
            "keitaro_stream_id": stream.keitaro_id}


@router.get("/{stream_id}", summary="Поток с офферами, долями, diff и проблемами")
async def get_stream(stream_id: int, session: SessionDep,
                     dictionaries: DictionariesDep) -> dict[str, Any]:
    return await _view(session, dictionaries, stream_id)


@router.post("/{stream_id}/offers", summary="ADD: добавить оффер в поток (черновик)")
async def add_offer(stream_id: int, body: AddOfferRequest, session: SessionDep,
                    dictionaries: DictionariesDep, locks: LocksDep) -> dict[str, Any]:
    async with locks.for_campaign(await _campaign_id(session, stream_id)):
        stream = await editor.load_stream(session, stream_id)
        async with audited(session, "add_offer", **_ids(stream)) as info:
            info["summary"] = f"оффер #{body.offer_id} → поток «{stream.name}»"
            await editor.add_offer(session, dictionaries, stream, body.offer_id)
        return await _view(session, dictionaries, stream_id)


@router.delete("/{stream_id}/offers/{binding_id}", summary="REMOVE: убрать оффер (черновик)")
async def remove_offer(stream_id: int, binding_id: int, session: SessionDep,
                       dictionaries: DictionariesDep, locks: LocksDep) -> dict[str, Any]:
    async with locks.for_campaign(await _campaign_id(session, stream_id)):
        stream = await editor.load_stream(session, stream_id)
        async with audited(session, "remove_offer", **_ids(stream)) as info:
            binding = await editor.remove_offer(session, stream, binding_id)
            info["summary"] = f"оффер #{binding.offer_id} убран из потока «{stream.name}»"
        return await _view(session, dictionaries, stream_id)


@router.post("/{stream_id}/offers/{binding_id}/bring-back", summary="BRING BACK: вернуть из архива")
async def bring_back(stream_id: int, binding_id: int, session: SessionDep,
                     dictionaries: DictionariesDep, locks: LocksDep) -> dict[str, Any]:
    async with locks.for_campaign(await _campaign_id(session, stream_id)):
        stream = await editor.load_stream(session, stream_id)
        async with audited(session, "bring_back", **_ids(stream)) as info:
            binding = await editor.bring_back(session, dictionaries, stream, binding_id)
            info["summary"] = f"оффер #{binding.offer_id} возвращён в поток «{stream.name}»"
        return await _view(session, dictionaries, stream_id)


@router.put("/{stream_id}/offers/{binding_id}/pin", summary="PIN: закрепить/открепить долю")
async def pin(stream_id: int, binding_id: int, body: PinRequest, session: SessionDep,
              dictionaries: DictionariesDep, locks: LocksDep) -> dict[str, Any]:
    async with locks.for_campaign(await _campaign_id(session, stream_id)):
        stream = await editor.load_stream(session, stream_id)
        async with audited(session, "pin" if body.pinned else "unpin", **_ids(stream)) as info:
            binding = await editor.set_pin(session, stream, binding_id, body.pinned)
            info["summary"] = (f"доля {binding.share}% оффера #{binding.offer_id} "
                               + ("закреплена" if body.pinned else "откреплена"))
        return await _view(session, dictionaries, stream_id)


@router.put("/{stream_id}/offers/{binding_id}/share", summary="Задать долю вручную (закрепляется)")
async def share(stream_id: int, binding_id: int, body: ShareRequest, session: SessionDep,
                dictionaries: DictionariesDep, locks: LocksDep) -> dict[str, Any]:
    async with locks.for_campaign(await _campaign_id(session, stream_id)):
        stream = await editor.load_stream(session, stream_id)
        async with audited(session, "set_share", **_ids(stream)) as info:
            binding = await editor.set_share(session, stream, binding_id, body.share)
            info["summary"] = f"оффер #{binding.offer_id}: доля {body.share}% (закреплена)"
        return await _view(session, dictionaries, stream_id)


@router.delete("/{stream_id}/offers/{binding_id}/forget",
               summary="Убрать оффер из архива потока насовсем")
async def forget(stream_id: int, binding_id: int, session: SessionDep,
                 dictionaries: DictionariesDep, locks: LocksDep) -> dict[str, Any]:
    async with locks.for_campaign(await _campaign_id(session, stream_id)):
        stream = await editor.load_stream(session, stream_id)
        async with audited(session, "forget_offer", **_ids(stream)) as info:
            await editor.forget_binding(session, stream, binding_id)
            info["summary"] = "оффер убран из архива потока"
        return await _view(session, dictionaries, stream_id)


@router.post("/{stream_id}/recalculate", summary="Пересчитать доли / выровнять поровну")
async def recalculate(stream_id: int, session: SessionDep, dictionaries: DictionariesDep,
                      locks: LocksDep, body: RecalculateRequest | None = None) -> dict[str, Any]:
    body = body or RecalculateRequest()
    async with locks.for_campaign(await _campaign_id(session, stream_id)):
        stream = await editor.load_stream(session, stream_id)
        async with audited(session, "equalize" if body.drop_pins else "recalculate",
                           **_ids(stream)) as info:
            await editor.recalculate(session, stream, drop_pins=body.drop_pins)
            info["summary"] = ("закрепления сняты, доли поровну" if body.drop_pins
                               else "доли пересчитаны с учётом закреплений")
        return await _view(session, dictionaries, stream_id)


@router.post("/{stream_id}/push", summary="PUSH TO KT: опубликовать черновик в Keitaro")
async def push(stream_id: int, session: SessionDep, client: ClientDep,
               dictionaries: DictionariesDep, locks: LocksDep,
               body: PushRequest | None = None) -> dict[str, Any]:
    """409 `conflict` — поток меняли в Keitaro после последнего Fetch (см. `details`)."""
    body = body or PushRequest()
    async with locks.for_campaign(await _campaign_id(session, stream_id)):
        stream = await editor.load_stream(session, stream_id)
        async with audited(session, "push", **_ids(stream)) as info:
            info["details"] = {"diff": editor.stream_diff(stream), "force": body.force}
            result = await editor.push(session, client, dictionaries, stream,
                                       force=body.force, allow_empty=body.allow_empty)
            info["details"]["published"] = result["offers"]
            info["summary"] = (f"поток «{stream.name}»: опубликовано офферов "
                               f"{len(result['offers'])}" + (" (принудительно)" if body.force else ""))
        return await _view(session, dictionaries, stream_id)


@router.post("/{stream_id}/cancel", summary="CANCEL: выбросить неопубликованные изменения")
async def cancel(stream_id: int, session: SessionDep, dictionaries: DictionariesDep,
                 locks: LocksDep) -> dict[str, Any]:
    async with locks.for_campaign(await _campaign_id(session, stream_id)):
        stream = await editor.load_stream(session, stream_id)
        async with audited(session, "cancel", **_ids(stream)) as info:
            reverted = await editor.cancel(session, stream)
            info["summary"] = f"поток «{stream.name}»: отменено изменений — {reverted}"
        return await _view(session, dictionaries, stream_id)


@router.get("/{stream_id}/snapshots", summary="Снимки потока перед публикациями (для отката)")
async def snapshots(stream_id: int, session: SessionDep,
                    dictionaries: DictionariesDep) -> list[dict[str, Any]]:
    await editor.load_stream(session, stream_id)
    rows = await editor.list_snapshots(session, stream_id)
    ids = {o["offer_id"] for s in rows for o in s.offers}
    names = {i: o.name for i, o in (await dictionaries.get_offers_map(session, ids)).items()}
    return [{"id": s.id, "created_at": s.created_at.isoformat(), "reason": s.reason,
             "offers": [{**o, "offer_name": names.get(o["offer_id"], f"Оффер #{o['offer_id']}")}
                        for o in s.offers]} for s in rows]


@router.post("/{stream_id}/snapshots/{snapshot_id}/restore",
             summary="Откат: загрузить снимок в черновик (публикация — отдельным Push)")
async def restore(stream_id: int, snapshot_id: int, session: SessionDep,
                  dictionaries: DictionariesDep, locks: LocksDep) -> dict[str, Any]:
    async with locks.for_campaign(await _campaign_id(session, stream_id)):
        stream = await editor.load_stream(session, stream_id)
        async with audited(session, "restore_snapshot", **_ids(stream)) as info:
            await editor.restore_snapshot(session, stream, snapshot_id)
            info["summary"] = f"поток «{stream.name}»: снимок #{snapshot_id} загружен в черновик"
        return await _view(session, dictionaries, stream_id)
