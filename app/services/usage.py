"""Работа с оффером сразу по многим кампаниям.

* `offer_usage`   — «где используется оффер»: все потоки, где он стоит, стоял (архив) или
  добавлен в черновик. В админке Keitaro такого поиска нет.
* `bulk_add`      — добавить оффер в несколько потоков разом. Только в ЧЕРНОВИКИ: доли
  пересчитываются по тем же правилам, что и при обычном Add, в трекер ничего не уходит.
* `push_many`     — опубликовать несколько потоков подряд; каждый проходит те же проверки,
  что и обычный Push (конфликт, сверка ответа, снимок). Сбой одного не останавливает остальные.
* `sync_all`      — Fetch по всем кампаниям, чтобы поиск «где используется» видел весь трекер.

Новой логики долей здесь нет: всё идёт через функции редактора.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.keitaro.client import KeitaroClient
from app.keitaro.errors import KeitaroError
from app.models import STATE_REMOVED, Campaign, Stream, StreamOffer
from app.services import editor
from app.services.dictionaries import DictionaryService
from app.services.editor import OFFERS_SCHEMA, EditorError
from app.services.locks import CampaignLocks, LockBusyError

logger = logging.getLogger(__name__)
MAX_BULK_STREAMS = 100
_sync_running = asyncio.Lock()  # второй одновременный «sync-all» не нужен: он лишь удвоит нагрузку


@asynccontextmanager
async def _rollback_on_error(session: AsyncSession) -> AsyncIterator[None]:
    """Откат ДО выхода из замка кампании.

    Порядок важен: замок снимается отдельным соединением с базой, и если наша сессия ещё держит
    незавершённую запись (SQLite — один писатель), снятие замка упрётся в «database is locked»,
    а строка замка провисит до истечения TTL. Поэтому сначала откат, потом освобождение.
    """
    try:
        yield
    except BaseException:
        await session.rollback()
        raise


async def offer_usage(session: AsyncSession, offer_id: int) -> dict[str, Any]:
    """Где стоит оффер (по данным базы AdRobot) и куда его ещё можно добавить."""
    stmt = (select(Stream).join(Campaign)
            .where(Campaign.is_deleted.is_(False), Stream.is_deleted.is_(False),
                   Stream.schema == OFFERS_SCHEMA)
            .options(selectinload(Stream.bindings), selectinload(Stream.campaign))
            .order_by(Campaign.keitaro_id.desc(), Stream.position))
    used, available = [], []
    for stream in (await session.scalars(stmt)).all():
        binding = next((b for b in stream.bindings if b.offer_id == offer_id), None)
        row = {
            "stream_id": stream.id, "keitaro_stream_id": stream.keitaro_id, "stream_name": stream.name,
            "campaign_id": stream.campaign.id, "keitaro_campaign_id": stream.campaign.keitaro_id,
            "campaign_name": stream.campaign.name, "geo": stream.campaign.geo or [],
            "active_offers": sum(1 for b in stream.bindings if b.state == "active"),
            "is_dirty": editor.stream_is_dirty(stream),
        }
        if binding is None or (binding.state == STATE_REMOVED and binding.kt_state is None):
            available.append({**row, "archived_here": binding is not None})
        else:
            used.append({**row, "state": binding.state, "share": binding.share,
                         "kt_share": binding.kt_share, "is_pinned": binding.is_pinned,
                         "in_keitaro": binding.kt_state is not None,
                         "pending": binding.is_dirty})
    unsynced = await session.scalar(
        select(Campaign.id).where(Campaign.is_deleted.is_(False),
                                  Campaign.streams_fetched_at.is_(None)).limit(1))
    return {"offer_id": offer_id, "used": used, "available": available,
            "has_unsynced_campaigns": unsynced is not None}


async def bulk_add(session: AsyncSession, dictionaries: DictionaryService, locks: CampaignLocks,
                   offer_id: int, stream_ids: list[int]) -> list[dict[str, Any]]:
    """Добавляет оффер в черновики выбранных потоков. Итог по каждому потоку отдельно."""
    problems = await dictionaries.ensure_offers_usable(session, [offer_id])
    if problems:
        raise EditorError("offer_not_usable", " ".join(problems))
    results = []
    for stream_id in list(dict.fromkeys(stream_ids))[:MAX_BULK_STREAMS]:
        item: dict[str, Any] = {"stream_id": stream_id}
        try:
            stream = await editor.load_stream(session, stream_id)
            item.update(stream_name=stream.name, campaign_name=stream.campaign.name)
            async with locks.for_campaign(stream.campaign_id), _rollback_on_error(session):
                stream = await editor.load_stream(session, stream_id)
                binding = await editor.add_offer(session, dictionaries, stream, offer_id)
            item.update(status="added", share=binding.share)
        except (EditorError, LockBusyError) as exc:
            await session.rollback()
            item.update(status="skipped", code=exc.code, message=exc.message)
        results.append(item)
    return results


async def push_many(session: AsyncSession, client: KeitaroClient, dictionaries: DictionaryService,
                    locks: CampaignLocks, stream_ids: list[int]) -> list[dict[str, Any]]:
    """Публикует потоки по очереди. Конфликт или сбой одного потока остальным не мешает."""
    results = []
    for stream_id in list(dict.fromkeys(stream_ids))[:MAX_BULK_STREAMS]:
        item: dict[str, Any] = {"stream_id": stream_id}
        try:
            stream = await editor.load_stream(session, stream_id)
            item.update(stream_name=stream.name, campaign_name=stream.campaign.name)
            async with locks.for_campaign(stream.campaign_id), _rollback_on_error(session):
                stream = await editor.load_stream(session, stream_id)
                published = await editor.push(session, client, dictionaries, stream)
            item.update(status="pushed", offers=published["offers"])
        except (EditorError, KeitaroError, LockBusyError) as exc:
            await session.rollback()
            item.update(status="error", code=exc.code, message=exc.message)
        results.append(item)
    return results


async def sync_all(session: AsyncSession, client: KeitaroClient, locks: CampaignLocks,
                   *, only_missing: bool = False) -> dict[str, Any]:
    """Fetch streams по всем кампаниям. Черновики сохраняются (как при обычном Fetch)."""
    if _sync_running.locked():
        raise EditorError("sync_running", "Синхронизация всех кампаний уже идёт — дождитесь её окончания.",
                          http_status=409)
    async with _sync_running:
        return await _sync_all(session, client, locks, only_missing=only_missing)


async def _sync_all(session: AsyncSession, client: KeitaroClient, locks: CampaignLocks,
                    *, only_missing: bool) -> dict[str, Any]:
    await editor.import_campaigns(session, client)
    stmt = select(Campaign.id).where(Campaign.is_deleted.is_(False)).order_by(Campaign.keitaro_id.desc())
    if only_missing:
        stmt = stmt.where(Campaign.streams_fetched_at.is_(None))
    synced, failed = 0, []
    for campaign_id in (await session.scalars(stmt)).all():
        try:
            async with locks.for_campaign(campaign_id), _rollback_on_error(session):
                campaign = await editor.load_campaign(session, campaign_id)
                await editor.fetch_streams(session, client, campaign)
            synced += 1
        except (EditorError, KeitaroError, LockBusyError) as exc:
            await session.rollback()
            failed.append({"campaign_id": campaign_id, "message": exc.message})
            if isinstance(exc, KeitaroError) and exc.code in (
                    "keitaro_unreachable", "keitaro_unauthorized", "keitaro_rate_limited",
                    "keitaro_forbidden"):
                break  # трекер недоступен целиком или просит сбавить темп — не стучимся в каждую кампанию
    return {"synced": synced, "failed": failed}


async def dirty_stream_ids(session: AsyncSession) -> list[int]:
    """ID потоков с неопубликованными изменениями (для кнопки «Опубликовать все»)."""
    stmt = (select(Stream).join(StreamOffer).where(Stream.is_deleted.is_(False))
            .options(selectinload(Stream.bindings)).distinct())
    return [s.id for s in (await session.scalars(stmt)).unique().all() if editor.stream_is_dirty(s)]
