"""Маршруты кампаний: список, импорт из Keitaro, «создаватор», Fetch streams, статистика."""

from __future__ import annotations

from contextlib import asynccontextmanager, suppress
from time import perf_counter
from typing import Annotated, Any

from fastapi import APIRouter, Header, Query
from sqlalchemy import and_, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import (
    ClientDep,
    CreatorDep,
    DictionariesDep,
    LocksDep,
    SessionDep,
    SettingsDep,
)
from app.config import Settings
from app.keitaro.countries import country_name
from app.keitaro.errors import KeitaroError
from app.models import STATE_REMOVED, Campaign, Stream, StreamOffer
from app.schemas import CampaignCreateRequest, FetchRequest
from app.services import audit, editor, stats
from app.services.creator import CreatorError
from app.services.dictionaries import DictionaryService
from app.services.editor import EditorError

router = APIRouter(prefix="/campaigns", tags=["Кампании"])


@asynccontextmanager
async def audited(session: AsyncSession, action: str, **ids: int | None):
    """Оборачивает операцию записью в журнал — и при успехе, и при ошибке."""
    info: dict[str, Any] = {"summary": "", "details": {}}
    started = perf_counter()
    try:
        yield info
    except (EditorError, CreatorError, KeitaroError) as exc:
        await audit.record_failure(session, action, exc, summary=info["summary"],
                                   details=info["details"],
                                   duration_ms=int((perf_counter() - started) * 1000), **ids)
        raise
    if info.get("skip"):
        return
    # Операция может завершиться без исключения и всё же неудачно (создание: «создано 0, ошибок 1»).
    await audit.record(session, action, status=info.get("status", "ok"), error=info.get("error"),
                       summary=info["summary"], details=info["details"],
                       duration_ms=int((perf_counter() - started) * 1000), **ids)
    await session.commit()


async def load_offer_names(session: AsyncSession, dictionaries: DictionaryService) -> None:
    """Подгружает справочник офферов там, где и так идём в Keitaro (импорт, Fetch, синхронизация).

    Кампания отдаётся редактору «из базы, без сети». Если к этому моменту справочник ни разу не
    загружали, редактор пометил бы каждый оффер «нет в справочнике», хотя оффер на месте.
    Обновление не принудительное: свежий справочник (DICTIONARY_TTL_SECONDS) повторно не качаем.
    """
    with suppress(KeitaroError):  # названия офферов — не повод ронять основную операцию
        await dictionaries.refresh_offers(session)


# SQL-двойник свойства StreamOffer.is_dirty — чтобы находить кампании с черновиками запросом.
DIRTY_BINDING = or_(
    and_(StreamOffer.state == STATE_REMOVED, StreamOffer.kt_state.is_not(None)),
    and_(StreamOffer.state != STATE_REMOVED,
         or_(StreamOffer.kt_state.is_(None), StreamOffer.state != StreamOffer.kt_state,
             StreamOffer.share != func.coalesce(StreamOffer.kt_share, 0))),
)


async def campaign_view(session: AsyncSession, campaign: Campaign,
                        dictionaries: DictionaryService, settings: Settings) -> dict[str, Any]:
    offer_ids = {b.offer_id for s in campaign.streams for b in s.bindings}
    offers = await dictionaries.get_offers_map(session, offer_ids)
    # Без resolve_defaults: тому нужны справочники Keitaro, а кампания отдаётся «из базы, без сети».
    tracking = await dictionaries.tracking_domain_url(session)
    streams = [editor.stream_view(s, offers) for s in campaign.streams]
    return {
        "id": campaign.id,
        "keitaro_id": campaign.keitaro_id,
        "name": campaign.name,
        "alias": campaign.alias,
        "state": campaign.state,
        "geo": [{"code": c, "name": country_name(c)} for c in campaign.geo or []],
        "origin": campaign.origin,
        "is_deleted": campaign.is_deleted,
        "group_id": campaign.group_id,
        "traffic_source_id": campaign.traffic_source_id,
        "domain_id": campaign.domain_id,
        "admin_url": settings.campaign_admin_url(campaign.keitaro_id),
        "campaign_url": f"{tracking}/{campaign.alias}" if tracking and campaign.alias else "",
        "streams_fetched_at": (campaign.streams_fetched_at.isoformat()
                               if campaign.streams_fetched_at else None),
        "is_dirty": any(s["is_dirty"] for s in streams),
        "streams": streams,
    }


@router.get("", summary="Список кампаний из базы AdRobot (поиск, фильтры, страницы)")
async def list_campaigns(
    session: SessionDep, settings: SettingsDep, q: str = "", origin: str = "",
    only_drafts: bool = False, include_deleted: bool = False,
    limit: Annotated[int, Query(ge=1, le=200)] = 50, offset: Annotated[int, Query(ge=0)] = 0,
) -> dict[str, Any]:
    dirty_ids = set((await session.scalars(
        select(Stream.campaign_id).join(StreamOffer).where(DIRTY_BINDING).distinct())).all())
    stmt = select(Campaign)
    if not include_deleted:
        stmt = stmt.where(Campaign.is_deleted.is_(False))
    if origin in ("adrobot", "keitaro"):
        stmt = stmt.where(Campaign.origin == origin)
    if only_drafts:
        stmt = stmt.where(Campaign.id.in_(dirty_ids or {-1}))
    rows = list((await session.scalars(stmt.order_by(Campaign.keitaro_id.desc()))).all())
    needle = q.strip().casefold()
    if needle:  # casefold в Python: LIKE в SQLite не умеет кириллицу без учёта регистра
        rows = [c for c in rows if needle in c.name.casefold() or needle in c.alias.casefold()
                or str(c.keitaro_id).startswith(needle)]
    return {"total": len(rows), "items": [
        {"id": c.id, "keitaro_id": c.keitaro_id, "name": c.name, "alias": c.alias,
         "state": c.state, "origin": c.origin, "geo": c.geo or [], "is_deleted": c.is_deleted,
         "has_draft": c.id in dirty_ids, "admin_url": settings.campaign_admin_url(c.keitaro_id),
         "streams_fetched_at": c.streams_fetched_at.isoformat() if c.streams_fetched_at else None}
        for c in rows[offset:offset + limit]]}


@router.post("/import", summary="Подтянуть список кампаний из Keitaro")
async def import_campaigns(session: SessionDep, client: ClientDep,
                           dictionaries: DictionariesDep) -> dict[str, int]:
    async with audited(session, "import_campaigns") as info:
        result = await editor.import_campaigns(session, client)
        info["summary"] = (f"кампаний в Keitaro: {result['total']}, новых: {result['created']}, "
                           f"пропало: {result['gone']}")
    await load_offer_names(session, dictionaries)
    return result


@router.post("", summary="«Создаватор»: имя + гео + оффер → кампания с двумя потоками")
async def create_campaign(
    body: CampaignCreateRequest, session: SessionDep, creator: CreatorDep,
    idempotency_key: Annotated[str | None, Header(max_length=128)] = None,
) -> dict[str, Any]:
    """Заголовок `Idempotency-Key` защищает от дублей при двойном клике и повторах."""
    action = "create_campaign_dry_run" if body.dry_run else "create_campaign"
    async with audited(session, action) as info:
        info["details"] = {"name": body.name, "geo": body.geo, "offer_ids": body.offer_ids,
                           "split_by_geo": body.split_by_geo}
        result = await creator.create(session, body, idempotency_key=idempotency_key)
        created = [r for r in result["results"] if r["status"] == "created"]
        failed = [r for r in result["results"] if r["status"] == "error"]
        info["details"]["keitaro_campaign_ids"] = [r["keitaro_campaign_id"] for r in created]
        if failed and not created:
            info["status"] = "error"
            info["error"] = "; ".join(f"{r['name']}: {r['error']['message']}" for r in failed)[:2000]
        info["summary"] = (
            f"«{body.name}»: план из {len(result['results'])} кампаний" if body.dry_run else
            f"«{body.name}»: создано {len(created)}, ошибок {len(failed)}"
            + (" (повтор запроса — отдан прежний результат)" if result.get("replayed") else ""))
    return result


@router.post("/open/{keitaro_id}", summary="Открыть в редакторе кампанию Keitaro по её ID")
async def open_campaign(keitaro_id: int, session: SessionDep, client: ClientDep,
                        dictionaries: DictionariesDep) -> dict[str, int]:
    campaign = await editor.get_or_import_campaign(session, client, keitaro_id)
    opened = {"id": campaign.id, "keitaro_id": campaign.keitaro_id}
    await load_offer_names(session, dictionaries)
    return opened


@router.get("/{campaign_id}", summary="Кампания с потоками и офферами (из базы, без сети)")
async def get_campaign(campaign_id: int, session: SessionDep, dictionaries: DictionariesDep,
                       settings: SettingsDep) -> dict[str, Any]:
    campaign = await editor.load_campaign(session, campaign_id)
    return await campaign_view(session, campaign, dictionaries, settings)


@router.post("/{campaign_id}/fetch", summary="Fetch streams from KT")
async def fetch_streams(campaign_id: int, session: SessionDep, client: ClientDep,
                        dictionaries: DictionariesDep, settings: SettingsDep, locks: LocksDep,
                        body: FetchRequest | None = None) -> dict[str, Any]:
    """Забирает потоки из Keitaro. Исчезнувшие там офферы уходят в архив (Bring back)."""
    body = body or FetchRequest()
    async with locks.for_campaign(campaign_id):
        campaign = await editor.load_campaign(session, campaign_id)
        async with audited(session, "fetch_streams",
                           keitaro_campaign_id=campaign.keitaro_id) as info:
            result = await editor.fetch_streams(session, client, campaign,
                                                discard_draft=body.discard_draft)
            # Редактор сам перечитывает кампанию при открытии. Если в Keitaro ничего не поменялось,
            # такая запись в журнале — шум: оставляем только ручные Fetch и те, что что-то изменили.
            info["skip"] = body.auto and not result["changed"]
            info["details"] = result
            info["summary"] = (f"потоков: {result['streams']}, ушло в архив офферов: "
                               f"{len(result['archived_offers'])}"
                               + (", черновик сброшен" if body.discard_draft else ""))
        await load_offer_names(session, dictionaries)
        campaign = await editor.load_campaign(session, campaign_id)
        view = await campaign_view(session, campaign, dictionaries, settings)
    return {"result": result, "campaign": view}


@router.get("/{campaign_id}/stats", summary="Клики/конверсии по офферам (колонки Stats/Trends)")
async def campaign_stats(campaign_id: int, session: SessionDep, client: ClientDep,
                         period: str = "7d") -> dict[str, Any]:
    campaign = await editor.load_campaign(session, campaign_id)
    return await stats.campaign_stats(client, campaign.keitaro_id, period)


@router.delete("/{campaign_id}", summary="Отправить кампанию в архив Keitaro")
async def archive_campaign(campaign_id: int, session: SessionDep, client: ClientDep,
                           locks: LocksDep) -> dict[str, Any]:
    """В Keitaro удаление = архив: кампанию можно восстановить из админки."""
    async with locks.for_campaign(campaign_id):
        campaign = await editor.load_campaign(session, campaign_id)
        async with audited(session, "archive_campaign",
                           keitaro_campaign_id=campaign.keitaro_id) as info:
            await client.archive_campaign(campaign.keitaro_id)
            campaign.is_deleted = True
            campaign.state = "deleted"
            info["summary"] = f"«{campaign.name}» отправлена в архив Keitaro"
    return {"archived": True, "keitaro_id": campaign.keitaro_id}
