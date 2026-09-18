"""Редактор офферов потока — логика оригинального AdRobot, воспроизведённая по видео из ТЗ.

Модель работы: **черновик → публикация**.

* `fetch_streams`  — «Fetch streams from KT»: забирает потоки кампании из Keitaro. Офферы,
  которые AdRobot знал, а в Keitaro их больше нет, не пропадают, а уходят в архив
  (`removed`, 0%) — их можно вернуть кнопкой Bring back. Доли при этом НЕ пересчитываются:
  интерфейс зеркалит то, что реально стоит в трекере.
* `add_offer` / `remove_offer` / `bring_back` — меняют только черновик в нашей базе и
  пересчитывают доли (см. `weights.py`). Поток становится «жёлтым» — не опубликован.
* `push`   — «Push to KT»: отправляет черновик в Keitaro одним запросом.
* `cancel` — «Cancel»: выбрасывает черновик. Привязки, добавленные до публикации, удаляются
  (сам оффер остаётся в справочнике), остальное возвращается к опубликованному состоянию.
* `set_pin` — закрепляет долю: дальнейшие пересчёты её не трогают.

Сверх оригинала: ручной ввод доли, проверка конфликтов перед публикацией (кто-то поправил
поток прямо в Keitaro), снимки и откат, понятные ошибки вместо молчаливого принятия мусора.
"""

from __future__ import annotations

import logging
from typing import Any

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.keitaro.client import KeitaroClient
from app.models import (
    STATE_ACTIVE,
    STATE_DISABLED,
    STATE_REMOVED,
    Campaign,
    Offer,
    Stream,
    StreamOffer,
    StreamSnapshot,
    utcnow,
)
from app.services import weights
from app.services.dictionaries import DictionaryService
from app.services.locks import CampaignLocks
from app.services.weights import WeightItem, WeightsError

logger = logging.getLogger(__name__)

OFFERS_SCHEMA = "landings"  # схема потока Keitaro «Landing pages & offers»
MAX_SNAPSHOTS_PER_STREAM = 30


class EditorError(Exception):
    """Ошибка бизнес-логики редактора с понятным пользователю текстом."""

    def __init__(self, code: str, message: str, *, http_status: int = 422,
                 details: Any = None) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.http_status = http_status
        self.details = details


# Замок на кампанию вынесен в `services/locks.py` (работает и между процессами).
StreamLocks = CampaignLocks


# ---------------------------------------------------------------------- загрузка из базы


async def load_campaign(session: AsyncSession, campaign_id: int) -> Campaign:
    stmt = (
        select(Campaign)
        .where(Campaign.id == campaign_id)
        .options(selectinload(Campaign.streams).selectinload(Stream.bindings))
        .execution_options(populate_existing=True)
    )
    campaign = (await session.scalars(stmt)).first()
    if campaign is None:
        raise EditorError("campaign_not_found", "Кампания не найдена в AdRobot.", http_status=404)
    return campaign


async def load_stream(session: AsyncSession, stream_id: int) -> Stream:
    stmt = (
        select(Stream)
        .where(Stream.id == stream_id)
        .options(selectinload(Stream.bindings), selectinload(Stream.campaign))
        .execution_options(populate_existing=True)
    )
    stream = (await session.scalars(stmt)).first()
    if stream is None:
        raise EditorError("stream_not_found", "Поток не найден в AdRobot.", http_status=404)
    return stream


def _binding(stream: Stream, binding_id: int) -> StreamOffer:
    for binding in stream.bindings:
        if binding.id == binding_id:
            return binding
    raise EditorError("binding_not_found", "Оффер не найден в этом потоке.", http_status=404)


# ---------------------------------------------------------------------- пересчёт долей


def _active(stream: Stream) -> list[StreamOffer]:
    return [b for b in stream.bindings if b.state == STATE_ACTIVE]


def _weight_items(bindings: list[StreamOffer]) -> list[WeightItem]:
    return [WeightItem(b.id, b.share, b.is_pinned, b.sort_index) for b in bindings]


def _apply_shares(bindings: list[StreamOffer], shares: dict[int, int]) -> None:
    for binding in bindings:
        binding.share = shares[binding.id]


def _rebalance(stream: Stream) -> None:
    active = _active(stream)
    try:
        _apply_shares(active, weights.rebalance(_weight_items(active)))
    except WeightsError as exc:
        raise EditorError(exc.code, exc.message) from exc


def _next_sort_index(stream: Stream) -> int:
    return max((b.sort_index for b in stream.bindings), default=0) + 1


def _ensure_editable(stream: Stream) -> None:
    if stream.is_deleted:
        raise EditorError("stream_deleted", "Этого потока больше нет в Keitaro. Нажмите Fetch.",
                          http_status=409)
    if stream.schema != OFFERS_SCHEMA:
        raise EditorError(
            "stream_without_offers",
            "Это поток без офферов (редирект или действие) — офферы в него не добавляются.",
            http_status=409)


# ---------------------------------------------------------------------- импорт кампаний


async def import_campaigns(session: AsyncSession, client: KeitaroClient) -> dict[str, int]:
    """Синхронизирует список кампаний с Keitaro (только шапки, без потоков).

    Двойной клик «Импорт» (или второй воркер) вставляет те же кампании одновременно — тогда
    проигравший натыкается на уникальный `keitaro_id`. Это не ошибка: откатываемся и проходим
    ещё раз, теперь уже видя строки соседа.
    """
    rows = await client.list_campaigns()
    for attempt in range(3):
        try:
            return await _import_campaign_rows(session, rows)
        except IntegrityError:
            await session.rollback()
            if attempt == 2:
                raise
    raise AssertionError("unreachable")  # pragma: no cover


async def _import_campaign_rows(session: AsyncSession, rows: list[dict[str, Any]]) -> dict[str, int]:
    known = {c.keitaro_id: c for c in (await session.scalars(select(Campaign))).all()}
    seen: set[int] = set()
    created = 0
    for row in rows:
        keitaro_id = row.get("id")
        if not isinstance(keitaro_id, int):
            continue
        seen.add(keitaro_id)
        campaign = known.get(keitaro_id)
        if campaign is None:
            campaign = Campaign(keitaro_id=keitaro_id, origin="keitaro", geo=[])
            created += 1
        _fill_campaign(campaign, row)
        session.add(campaign)
    gone = 0
    for keitaro_id, campaign in known.items():
        if keitaro_id not in seen and not campaign.is_deleted:
            campaign.is_deleted = True
            gone += 1
    await session.commit()
    return {"total": len(seen), "created": created, "gone": gone}


def _fill_campaign(campaign: Campaign, row: dict[str, Any]) -> None:
    campaign.name = str(row.get("name") or f"Campaign #{campaign.keitaro_id}")[:255]
    campaign.alias = str(row.get("alias") or "")[:255]
    campaign.state = str(row.get("state") or "active")
    for attr in ("group_id", "traffic_source_id", "domain_id"):
        value = row.get(attr)
        setattr(campaign, attr, value if isinstance(value, int) and value > 0 else None)
    campaign.is_deleted = False


async def get_or_import_campaign(
    session: AsyncSession, client: KeitaroClient, keitaro_id: int
) -> Campaign:
    """Открыть в редакторе любую существующую кампанию Keitaro по её ID."""
    stmt = select(Campaign).where(Campaign.keitaro_id == keitaro_id)
    campaign = (await session.scalars(stmt)).first()
    if campaign is None:
        row = await client.get_campaign(keitaro_id)
        campaign = Campaign(keitaro_id=keitaro_id, origin="keitaro", geo=[])
        _fill_campaign(campaign, row)
        session.add(campaign)
        try:
            await session.commit()
        except IntegrityError:
            # Двойной клик «Открыть» или две вкладки: строку успел вставить соседний запрос.
            await session.rollback()
            campaign = (await session.scalars(stmt)).one()
    return campaign


# ---------------------------------------------------------------------- Fetch streams


def _stream_summary(row: dict[str, Any]) -> dict[str, Any]:
    """Справка о потоке для показа. Логика редактора её не использует."""
    filters = []
    for flt in row.get("filters") or []:
        if isinstance(flt, dict):
            filters.append({"name": flt.get("name"), "mode": flt.get("mode"),
                            "payload": flt.get("payload")})
    return {
        "action_payload": str(row.get("action_payload") or "")[:300],
        "filters": filters,
        "landings": len(row.get("landings") or []),
    }


def _kt_offer_rows(row: dict[str, Any]) -> list[dict[str, Any]]:
    result = []
    for offer in row.get("offers") or []:
        if isinstance(offer, dict) and isinstance(offer.get("offer_id"), int):
            state = STATE_DISABLED if offer.get("state") == STATE_DISABLED else STATE_ACTIVE
            share = offer.get("share")
            result.append({
                "offer_id": offer["offer_id"],
                "share": int(share) if isinstance(share, (int, float)) else 0,
                "state": state,
                "binding_id": offer.get("id") if isinstance(offer.get("id"), int) else None,
            })
    return result


def _sync_bindings(stream: Stream, kt_offers: list[dict[str, Any]], *, discard_draft: bool) -> None:
    """Сводит привязки потока с тем, что сейчас в Keitaro.

    Если в потоке есть неопубликованный черновик и его не просили выбросить, работает
    трёхстороннее слияние по каждой привязке: нетронутые пользователем привязки
    принимают состояние Keitaro, изменённые — сохраняют черновик, обновляется только
    «опубликованная» половина.
    """
    by_offer = {b.offer_id: b for b in stream.bindings}
    seen: set[int] = set()
    now = utcnow()
    for row in kt_offers:
        offer_id = row["offer_id"]
        if offer_id in seen:
            continue
        seen.add(offer_id)
        binding = by_offer.get(offer_id)
        if binding is None:
            stream.bindings.append(StreamOffer(
                offer_id=offer_id, state=row["state"], share=row["share"],
                kt_state=row["state"], kt_share=row["share"], kt_binding_id=row["binding_id"],
                was_published=True, sort_index=_next_sort_index(stream)))
            continue
        keep_draft = binding.is_dirty and not discard_draft
        binding.kt_state, binding.kt_share = row["state"], row["share"]
        binding.kt_binding_id = row["binding_id"]
        binding.was_published = True
        if not keep_draft:
            if binding.state == STATE_REMOVED:
                # Оффер вернули прямо в Keitaro. Это та же активация, что и Bring back: он встаёт
                # в конец очереди, и неделимый остаток при следующем пересчёте достанется ему.
                binding.sort_index = _next_sort_index(stream)
            binding.state, binding.share, binding.removed_at = row["state"], row["share"], None

    for binding in list(stream.bindings):
        if binding.offer_id in seen:
            continue
        keep_draft = binding.is_dirty and not discard_draft
        binding.kt_state = binding.kt_share = binding.kt_binding_id = None
        if keep_draft:
            continue
        if not binding.was_published:
            stream.bindings.remove(binding)  # неопубликованное добавление выброшено
            continue
        if binding.state != STATE_REMOVED:
            binding.removed_at = now
        binding.state, binding.share, binding.is_pinned = STATE_REMOVED, 0, False


def _mirror_signature(campaign: Campaign) -> list[tuple[Any, ...]]:
    """Слепок того, что AdRobot знает о потоках кампании: по нему видно, изменил ли что-то Fetch."""
    return sorted(
        (s.keitaro_id, s.name, s.position, s.state, s.is_deleted, str(s.summary),
         tuple(sorted((b.offer_id, b.state, b.share, b.kt_state or "", b.kt_share or 0,
                       b.kt_binding_id or 0) for b in s.bindings)))
        for s in campaign.streams)


async def fetch_streams(
    session: AsyncSession,
    client: KeitaroClient,
    campaign: Campaign,
    *,
    discard_draft: bool = False,
) -> dict[str, Any]:
    """«Fetch streams from KT». Возвращает сводку: сколько потоков, что ушло в архив."""
    rows = await client.get_campaign_streams(campaign.keitaro_id)
    before = _mirror_signature(campaign)
    by_kt_id = {s.keitaro_id: s for s in campaign.streams}
    seen: set[int] = set()
    now = utcnow()
    archived_before = {b.id for s in campaign.streams for b in s.bindings
                       if b.state == STATE_REMOVED}
    for row in rows:
        keitaro_id = row.get("id")
        if not isinstance(keitaro_id, int):
            continue
        seen.add(keitaro_id)
        stream = by_kt_id.get(keitaro_id)
        if stream is None:
            stream = Stream(keitaro_id=keitaro_id)
            campaign.streams.append(stream)
        stream.name = str(row.get("name") or "")[:255]
        stream.position = row.get("position") if isinstance(row.get("position"), int) else 0
        stream.type = str(row.get("type") or "regular")
        stream.schema = str(row.get("schema") or "")
        stream.state = str(row.get("state") or "active")
        stream.action_type = str(row.get("action_type") or "")
        stream.summary = _stream_summary(row)
        stream.is_deleted = False
        stream.fetched_at = now
        _sync_bindings(stream, _kt_offer_rows(row), discard_draft=discard_draft)
    for keitaro_id, stream in by_kt_id.items():
        if keitaro_id not in seen:
            stream.is_deleted = True
            # Потока в трекере больше нет — публиковать его черновик некуда. Сбрасываем его,
            # иначе кампания навсегда осталась бы «с черновиком», который нельзя ни отправить,
            # ни отменить. Офферы потока уходят в архив как обычно.
            _sync_bindings(stream, [], discard_draft=True)
    campaign.streams_fetched_at = now
    campaign.is_deleted = False
    await session.commit()
    campaign = await load_campaign(session, campaign.id)
    newly_archived = [b.offer_id for s in campaign.streams for b in s.bindings
                      if b.state == STATE_REMOVED and b.id not in archived_before]
    return {
        "streams": len(seen),
        "gone_streams": sum(1 for s in campaign.streams if s.is_deleted),
        "archived_offers": newly_archived,
        "changed": before != _mirror_signature(campaign),
    }


# ---------------------------------------------------------------------- правки черновика


async def _offer_problems(session: AsyncSession, dictionaries: DictionaryService,
                          bindings: list[StreamOffer]) -> list[str]:
    """Почему офферы этих привязок нельзя отправить в Keitaro (пусто = можно).

    Оффер может быть просто не виден нашему ключу API (права в Keitaro). Если привязка уже
    бывала в трекере — оффер существует, и вернуть его в поток можно. А вот оффер, который
    справочник ЗНАЕТ как выключенный или удалённый, блокируется всегда.
    """
    if not bindings:
        return []
    offer_ids = [b.offer_id for b in bindings]
    if not await dictionaries.ensure_offers_usable(session, offer_ids):
        return []
    known = await dictionaries.get_offers_map(session, set(offer_ids))
    to_check = []
    for binding in bindings:
        offer = known.get(binding.offer_id)
        invisible = offer is None or offer.is_missing
        if not (invisible and binding.was_published):
            to_check.append(binding.offer_id)
    return await dictionaries.ensure_offers_usable(session, to_check) if to_check else []


async def add_offer(
    session: AsyncSession, dictionaries: DictionaryService, stream: Stream, offer_id: int
) -> StreamOffer:
    """ADD: новый оффер в поток. Если оффер лежит в архиве потока — это Bring back."""
    _ensure_editable(stream)
    existing = next((b for b in stream.bindings if b.offer_id == offer_id), None)
    if existing is not None and existing.state != STATE_REMOVED:
        raise EditorError("offer_already_in_stream", "Этот оффер уже есть в потоке.",
                          http_status=409)
    if existing is not None:
        return await bring_back(session, dictionaries, stream, existing.id)
    problems = await dictionaries.ensure_offers_usable(session, [offer_id])
    if problems:
        raise EditorError("offer_not_usable", " ".join(problems))
    binding = StreamOffer(offer_id=offer_id, state=STATE_ACTIVE, share=0,
                          sort_index=_next_sort_index(stream))
    stream.bindings.append(binding)
    await session.flush()  # нужен id привязки для калькулятора
    _rebalance(stream)
    await session.commit()
    return binding


async def remove_offer(session: AsyncSession, stream: Stream, binding_id: int) -> StreamOffer:
    """REMOVE: оффер сереет, доля 0%, остальные пересчитываются. В Keitaro уйдёт после Push."""
    binding = _binding(stream, binding_id)
    if binding.state == STATE_REMOVED:
        raise EditorError("already_removed", "Оффер уже удалён из потока.", http_status=409)
    if not binding.was_published and binding.kt_state is None:
        # Добавили и передумали до публикации: в Keitaro его не было — архивировать нечего.
        stream.bindings.remove(binding)
    else:
        # Закрепление не сбрасываем: если Remove отменят через Cancel, оффер вернётся таким,
        # каким был, — с долей И с закреплением. В расчёте участвуют только активные привязки,
        # так что флаг удалённой привязки ни на что не влияет; Bring back его снимает.
        binding.state, binding.share = STATE_REMOVED, 0
        binding.removed_at = utcnow()
    try:
        _rebalance(stream)
    except EditorError:
        # Удаление не должно упираться в нерешаемые закрепления: доли остаются как есть,
        # а проблему покажет `stream_problems` (публикация будет заблокирована до исправления).
        logger.info("поток %s: после удаления оффера доли не пересчитаны", stream.keitaro_id)
    await session.commit()
    return binding


async def bring_back(
    session: AsyncSession,
    dictionaries: DictionaryService,
    stream: Stream,
    binding_id: int,
    *,
    check_offer: bool = True,
) -> StreamOffer:
    """BRING BACK: оффер из архива снова активен и встаёт в конец очереди пересчёта."""
    _ensure_editable(stream)
    binding = _binding(stream, binding_id)
    if binding.state != STATE_REMOVED:
        raise EditorError("not_removed", "Оффер не в архиве — возвращать нечего.", http_status=409)
    if check_offer:
        problems = await _offer_problems(session, dictionaries, [binding])
        if problems:
            raise EditorError("offer_not_usable", " ".join(problems))
    binding.state, binding.share, binding.removed_at = STATE_ACTIVE, 0, None
    binding.is_pinned = False  # возвращённый оффер получает долю заново — старое закрепление не в силе
    binding.sort_index = _next_sort_index(stream)
    _rebalance(stream)
    await session.commit()
    return binding


async def set_pin(session: AsyncSession, stream: Stream, binding_id: int, pinned: bool) -> StreamOffer:
    """Закрепить/открепить долю. Сам по себе pin цифр не меняет и поток не «желтит»."""
    binding = _binding(stream, binding_id)
    if binding.state != STATE_ACTIVE:
        raise EditorError("not_active", "Закрепить можно только долю активного оффера.",
                          http_status=409)
    binding.is_pinned = pinned
    await session.commit()
    return binding


async def set_share(session: AsyncSession, stream: Stream, binding_id: int, value: int) -> StreamOffer:
    """Ручной ввод доли: значение закрепляется, остальные незакреплённые пересчитываются."""
    binding = _binding(stream, binding_id)
    if binding.state != STATE_ACTIVE:
        raise EditorError("not_active", "Долю можно задать только активному офферу.",
                          http_status=409)
    active = _active(stream)
    try:
        shares = weights.set_share(_weight_items(active), binding.id, value)
    except WeightsError as exc:
        raise EditorError(exc.code, exc.message) from exc
    binding.is_pinned = True
    _apply_shares(active, shares)
    await session.commit()
    return binding


async def recalculate(session: AsyncSession, stream: Stream, *, drop_pins: bool = False) -> None:
    """«Пересчитать» (с учётом закреплений) или «Выровнять» (снять закрепления, всем поровну)."""
    active = _active(stream)
    if not active:
        raise EditorError("no_active_offers", "В потоке нет активных офферов.", http_status=409)
    if drop_pins:
        for binding in active:
            binding.is_pinned = False
    _rebalance(stream)
    await session.commit()


async def apply_shares(session: AsyncSession, stream: Stream, shares: dict[int, int]) -> None:
    """Применить готовый набор долей (совет советника) в ЧЕРНОВИК.

    Закрепления не ставятся и не снимаются; закреплённую долю изменить нельзя. Набор обязан
    давать ровно 100% вместе с теми офферами, которых он не касается.
    """
    _ensure_editable(stream)
    active = {b.id: b for b in _active(stream)}
    unknown = set(shares) - set(active)
    if unknown:
        raise EditorError("binding_not_found", "В наборе есть офферы, которых нет среди активных "
                          "офферов потока. Обновите страницу.", http_status=404)
    for binding_id, value in shares.items():
        binding = active[binding_id]
        if not 1 <= value <= weights.TOTAL:
            raise EditorError("share_out_of_range", "Доля должна быть целым числом от 1 до 100.")
        if binding.is_pinned and value != binding.share:
            raise EditorError("pinned_share", f"Доля оффера #{binding.offer_id} закреплена — "
                              "сначала снимите закрепление.", http_status=409)
    total = sum(shares.get(binding_id, binding.share) for binding_id, binding in active.items())
    if total != weights.TOTAL:
        raise EditorError("invalid_distribution", f"Сумма долей получится {total}%, а должна быть 100%.")
    for binding_id, value in shares.items():
        active[binding_id].share = value
    await session.commit()


async def cancel(session: AsyncSession, stream: Stream) -> int:
    """CANCEL: выбросить черновик. Возвращает число отменённых изменений."""
    reverted = 0
    for binding in list(stream.bindings):
        if not binding.is_dirty:
            continue
        reverted += 1
        if not binding.was_published:
            stream.bindings.remove(binding)  # привязка удаляется, оффер в справочнике остаётся
        elif binding.kt_state is None:
            binding.state, binding.share, binding.is_pinned = STATE_REMOVED, 0, False
            binding.removed_at = binding.removed_at or utcnow()
        else:
            if binding.state != STATE_REMOVED and binding.share != (binding.kt_share or 0):
                # Долю меняли вручную — а ручной ввод сам ставит закрепление. Отменяем и его,
                # иначе «отменённые» 34% остались бы закреплёнными и исказили следующий пересчёт.
                binding.is_pinned = False
            binding.state, binding.share = binding.kt_state, binding.kt_share or 0
            binding.removed_at = None
    await session.commit()
    return reverted


async def forget_binding(session: AsyncSession, stream: Stream, binding_id: int) -> None:
    """Убрать оффер из архива потока насовсем (в Keitaro его уже нет)."""
    binding = _binding(stream, binding_id)
    if binding.state != STATE_REMOVED or binding.kt_state is not None:
        raise EditorError("not_archived", "Насовсем убрать можно только оффер из архива, "
                          "которого уже нет в Keitaro.", http_status=409)
    stream.bindings.remove(binding)
    await session.commit()


# ---------------------------------------------------------------------- diff / публикация


def stream_is_dirty(stream: Stream) -> bool:
    return any(b.is_dirty for b in stream.bindings)


def stream_problems(stream: Stream) -> list[str]:
    """Что мешает публикации. Для потоков без офферов и чистых потоков — пусто."""
    if stream.schema != OFFERS_SCHEMA or not stream.bindings:
        return []
    active = _active(stream)
    if not active and not stream_is_dirty(stream):
        return []
    return weights.check_distribution(_weight_items(active))


def stream_diff(stream: Stream) -> list[dict[str, Any]]:
    """Список «было в Keitaro → станет после Push»."""
    changes: list[dict[str, Any]] = []
    for binding in sorted(stream.bindings, key=lambda b: b.sort_index):
        if not binding.is_dirty:
            continue
        if binding.state == STATE_REMOVED:
            kind, before, after = "remove", binding.kt_share, None
        elif binding.kt_state is None:
            kind, before, after = "add", None, binding.share
        elif binding.state != binding.kt_state:
            kind, before, after = "state", binding.kt_share, binding.share
        else:
            kind, before, after = "share", binding.kt_share, binding.share
        changes.append({"binding_id": binding.id, "offer_id": binding.offer_id, "type": kind,
                        "from": before, "to": after})
    return changes


def _push_payload(stream: Stream) -> list[dict[str, Any]]:
    rows = [b for b in stream.bindings if b.state in (STATE_ACTIVE, STATE_DISABLED)]
    return [{"offer_id": b.offer_id, "share": b.share, "state": b.state}
            for b in sorted(rows, key=lambda b: (b.sort_index, b.id))]


def _signature(rows: list[dict[str, Any]]) -> list[tuple[int, int, str]]:
    return sorted((r["offer_id"], r["share"], r["state"]) for r in rows)


async def push(
    session: AsyncSession,
    client: KeitaroClient,
    dictionaries: DictionaryService,
    stream: Stream,
    *,
    force: bool = False,
    allow_empty: bool = False,
    allow_unknown_offers: bool = False,
) -> dict[str, Any]:
    """«Push to KT»: публикует черновик. Порядок: проверки → конфликт → снимок → PUT → сверка."""
    _ensure_editable(stream)
    if not stream_is_dirty(stream):
        raise EditorError("nothing_to_push", "В потоке нет неопубликованных изменений.",
                          http_status=409)
    active = _active(stream)
    if not active and not allow_empty:
        raise EditorError(
            "empty_stream",
            "После публикации в потоке не останется активных офферов — трафику некуда идти. "
            "Подтвердите публикацию пустого потока.", details={"needs": "allow_empty"})
    if active:
        problems = weights.check_distribution(_weight_items(active))
        if problems:
            raise EditorError("invalid_distribution", " ".join(problems))
    returning = [b for b in active if b.kt_state is None]
    problems = await _offer_problems(session, dictionaries, returning)
    if problems:
        raise EditorError("offer_not_usable", " ".join(problems))
    # Оффер, которого нет в справочнике, мог быть не «невидим ключу», а удалён в трекере — снаружи
    # это неотличимо, а Keitaro молча примет и несуществующий ID. Вернуть такой оффер в черновик
    # можно, но отправить в трекер — только с явным подтверждением.
    known = await dictionaries.get_offers_map(session, {b.offer_id for b in returning})
    unknown = [b.offer_id for b in returning
               if b.offer_id not in known or known[b.offer_id].is_missing]
    if unknown and not allow_unknown_offers:
        listed = ", ".join(f"#{offer_id}" for offer_id in unknown)
        raise EditorError(
            "unknown_offers",
            f"Офферов {listed} нет в справочнике: либо они не видны этому ключу API, либо удалены "
            "в Keitaro (отличить нельзя, а трекер примет и несуществующий ID). Подтвердите публикацию, "
            "если уверены, что офферы существуют.",
            http_status=409, details={"needs": "allow_unknown_offers", "offer_ids": unknown})

    current = _kt_offer_rows(await client.get_stream(stream.keitaro_id))
    expected = [{"offer_id": b.offer_id, "share": b.kt_share or 0, "state": b.kt_state}
                for b in stream.bindings if b.kt_state is not None]
    if _signature(current) != _signature(expected) and not force:
        raise EditorError(
            "conflict",
            "Поток изменили в Keitaro после последней синхронизации. Нажмите Fetch, чтобы "
            "подтянуть изменения, или опубликуйте принудительно (перезапишет правки в Keitaro).",
            http_status=409,
            details={"keitaro": _public_rows(current), "expected": _public_rows(expected)})

    payload = _push_payload(stream)
    latest = (await session.scalars(
        select(StreamSnapshot).where(StreamSnapshot.stream_id == stream.id)
        .order_by(StreamSnapshot.id.desc()).limit(1))).first()
    if latest is None or latest.offers != _public_rows(current):  # повтор Push не плодит копии
        session.add(StreamSnapshot(stream_id=stream.id, reason="pre_push",
                                   offers=_public_rows(current)))
    # Снимок фиксируем ДО отправки: если PUT применится, а ответ потеряется (таймаут) или окажется
    # не тем (push_mismatch), трекер уже изменён — и точка отката обязана существовать.
    await session.commit()
    response = await client.update_stream_offers(stream.keitaro_id, payload)
    published = _kt_offer_rows(response)
    if _signature(published) != _signature(payload):
        await session.rollback()
        raise EditorError(
            "push_mismatch",
            "Keitaro сохранил не то, что мы отправили. Нажмите Fetch, чтобы увидеть фактическое "
            "состояние потока.", http_status=502,
            details={"sent": payload, "saved": _public_rows(published)})

    by_offer = {row["offer_id"]: row for row in published}
    for binding in stream.bindings:
        row = by_offer.get(binding.offer_id)
        if row is None:
            binding.kt_state = binding.kt_share = binding.kt_binding_id = None
        else:
            binding.kt_state, binding.kt_share = row["state"], row["share"]
            binding.kt_binding_id = row["binding_id"]
            binding.was_published = True
    await _trim_snapshots(session, stream.id)
    await session.commit()
    return {"offers": _public_rows(published)}


def _public_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [{"offer_id": r["offer_id"], "share": r["share"], "state": r["state"]} for r in rows]


async def _trim_snapshots(session: AsyncSession, stream_id: int) -> None:
    stmt = (select(StreamSnapshot).where(StreamSnapshot.stream_id == stream_id)
            .order_by(StreamSnapshot.id.desc()).offset(MAX_SNAPSHOTS_PER_STREAM))
    for snapshot in (await session.scalars(stmt)).all():
        await session.delete(snapshot)


# ---------------------------------------------------------------------- снимки и откат


async def list_snapshots(session: AsyncSession, stream_id: int) -> list[StreamSnapshot]:
    stmt = (select(StreamSnapshot).where(StreamSnapshot.stream_id == stream_id)
            .order_by(StreamSnapshot.id.desc()))
    return list((await session.scalars(stmt)).all())


async def restore_snapshot(session: AsyncSession, stream: Stream, snapshot_id: int) -> None:
    """Откат: снимок загружается в ЧЕРНОВИК. В Keitaro он уйдёт только после Push."""
    _ensure_editable(stream)
    snapshot = await session.get(StreamSnapshot, snapshot_id)
    if snapshot is None or snapshot.stream_id != stream.id:
        raise EditorError("snapshot_not_found", "Снимок не найден.", http_status=404)
    wanted = {row["offer_id"]: row for row in snapshot.offers}
    by_offer = {b.offer_id: b for b in stream.bindings}
    now = utcnow()
    for offer_id, row in wanted.items():
        binding = by_offer.get(offer_id)
        if binding is None:
            binding = StreamOffer(offer_id=offer_id, sort_index=_next_sort_index(stream))
            stream.bindings.append(binding)
        elif binding.state == STATE_REMOVED:
            binding.sort_index = _next_sort_index(stream)
        binding.state = row.get("state") or STATE_ACTIVE
        binding.share, binding.removed_at, binding.is_pinned = int(row.get("share") or 0), None, False
    for binding in list(stream.bindings):
        if binding.offer_id in wanted:
            continue
        if not binding.was_published and binding.kt_state is None:
            stream.bindings.remove(binding)
        elif binding.state != STATE_REMOVED:
            binding.state, binding.share, binding.is_pinned = STATE_REMOVED, 0, False
            binding.removed_at = now
    await session.commit()


# ---------------------------------------------------------------------- представление для UI


def _binding_order(binding: StreamOffer) -> tuple[int, int, int]:
    # Как в оригинале: активные по убыванию доли, затем выключенные, в конце архив.
    group = {STATE_ACTIVE: 0, STATE_DISABLED: 1}.get(binding.state, 2)
    return (group, -binding.share if group == 0 else 0, binding.id)


def stream_view(stream: Stream, offers: dict[int, Offer]) -> dict[str, Any]:
    diff = {change["binding_id"]: change for change in stream_diff(stream)}
    rows = []
    for binding in sorted(stream.bindings, key=_binding_order):
        offer = offers.get(binding.offer_id)
        rows.append({
            "id": binding.id,
            "offer_id": binding.offer_id,
            "offer_name": offer.name if offer else f"Оффер #{binding.offer_id}",
            "offer_known": offer is not None and not offer.is_missing,
            "offer_state": offer.state if offer else "unknown",
            "state": binding.state,
            "share": binding.share,
            "is_pinned": binding.is_pinned,
            "kt_share": binding.kt_share,
            "in_keitaro": binding.kt_state is not None,
            "was_published": binding.was_published,
            "is_dirty": binding.is_dirty,
            "change": diff.get(binding.id, {}).get("type"),
        })
    active = _active(stream)
    return {
        "id": stream.id,
        "keitaro_id": stream.keitaro_id,
        "name": stream.name,
        "position": stream.position,
        "type": stream.type,
        "schema": stream.schema,
        "state": stream.state,
        "supports_offers": stream.schema == OFFERS_SCHEMA,
        "summary": stream.summary or {},
        "is_deleted": stream.is_deleted,
        "is_dirty": stream_is_dirty(stream),
        "problems": stream_problems(stream),
        "diff": list(diff.values()),
        "total_share": sum(b.share for b in active),
        "active_count": len(active),
        "offers": rows,
    }
