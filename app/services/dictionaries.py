"""Справочники Keitaro: офферы, группы, источники, домены — и значения по умолчанию.

Офферы кэшируются в базе (нужны названия в редакторе и поиск для автокомплита),
остальное — в памяти с коротким временем жизни.

Значения по умолчанию для «создаватора» определяются так (первое найденное):
1. явная настройка из интерфейса (таблица `app_settings`);
2. переменная окружения (`DEFAULT_DOMAIN_ID` и т.д.);
3. автоматически: единственный элемент справочника, а для домена — если ключу API
   справочник доменов не виден (так настроена тестовая площадка) — самый частый
   `domain_id` среди существующих кампаний.
"""

from __future__ import annotations

import datetime as dt
import time
from collections import Counter
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings
from app.keitaro.client import KeitaroClient
from app.keitaro.errors import KeitaroForbiddenError
from app.models import AppSetting, Offer, utcnow

SETTING_KEYS = (
    "default_domain_id",
    "default_group_id",
    "default_traffic_source_id",
    "default_redirect_url",
    "tracking_domain_url",
)


@dataclass
class Lookups:
    """Снимок справочников для формы создания кампании."""

    groups: list[dict[str, Any]] = field(default_factory=list)
    traffic_sources: list[dict[str, Any]] = field(default_factory=list)
    domains: list[dict[str, Any]] = field(default_factory=list)
    domains_visible: bool = True
    inferred_domain_id: int | None = None
    warnings: list[str] = field(default_factory=list)
    loaded_at: float = 0.0

    def group_ids(self) -> set[int]:
        return {g["id"] for g in self.groups}

    def source_ids(self) -> set[int]:
        return {s["id"] for s in self.traffic_sources}

    def domain_ids(self) -> set[int]:
        ids = {d["id"] for d in self.domains}
        if self.inferred_domain_id is not None:
            ids.add(self.inferred_domain_id)
        return ids

    def source_by_id(self, source_id: int | None) -> dict[str, Any] | None:
        return next((s for s in self.traffic_sources if s["id"] == source_id), None)


class DictionaryService:
    """Кэш справочников. Один экземпляр на приложение."""

    def __init__(self, client: KeitaroClient, settings: Settings) -> None:
        self._client = client
        self._settings = settings
        self._lookups: Lookups | None = None
        self._offers_loaded_at = 0.0

    # ------------------------------------------------------------------ офферы

    async def refresh_offers(self, session: AsyncSession, *, force: bool = False) -> int:
        """Обновляет кэш офферов из Keitaro, если он устарел. Возвращает число офферов."""
        fresh = time.monotonic() - self._offers_loaded_at < self._settings.dictionary_ttl_seconds
        if fresh and not force:
            return -1
        rows = await self._client.list_offers()
        now = utcnow()
        known = {o.keitaro_id: o for o in (await session.scalars(select(Offer))).all()}
        seen: set[int] = set()
        for row in rows:
            offer_id = row.get("id")
            if not isinstance(offer_id, int):
                continue
            seen.add(offer_id)
            offer = known.get(offer_id) or Offer(keitaro_id=offer_id)
            offer.name = str(row.get("name") or f"Offer #{offer_id}")[:512]
            offer.state = str(row.get("state") or "active")
            offer.group_id = row.get("group_id") if isinstance(row.get("group_id"), int) else None
            network = row.get("affiliate_network_id")
            offer.affiliate_network_id = network if isinstance(network, int) else None
            country = row.get("country")
            offer.country = [str(c) for c in country] if isinstance(country, list) else []
            offer.offer_type = str(row.get("offer_type") or "")
            offer.payout_value = str(row.get("payout_value") if row.get("payout_value") is not None
                                     else "")
            offer.payout_currency = str(row.get("payout_currency") or "")
            offer.is_missing = False
            offer.fetched_at = now
            session.add(offer)
        for offer_id, offer in known.items():
            if offer_id not in seen:
                offer.is_missing = True
        await session.commit()
        self._offers_loaded_at = time.monotonic()
        return len(seen)

    async def search_offers(
        self, session: AsyncSession, query: str, *, limit: int = 20, include_inactive: bool = False
    ) -> list[Offer]:
        """Поиск для автокомплита: по ID (префикс) и по названию (подстрока, без регистра)."""
        await self.refresh_offers(session)
        query = (query or "").strip()
        stmt = select(Offer).where(Offer.is_missing.is_(False))
        if not include_inactive:
            stmt = stmt.where(Offer.state == "active")
        offers = list((await session.scalars(stmt)).all())
        if query:
            needle = query.casefold()
            # Фильтруем в Python: casefold корректно работает с кириллицей, а LIKE в SQLite — нет.
            offers = [o for o in offers
                      if needle in o.name.casefold() or str(o.keitaro_id).startswith(needle)]

        def rank(offer: Offer) -> tuple[int, int]:
            if query and str(offer.keitaro_id) == query:
                return (0, offer.keitaro_id)
            if query and str(offer.keitaro_id).startswith(query):
                return (1, offer.keitaro_id)
            if query and offer.name.casefold().startswith(query.casefold()):
                return (2, offer.keitaro_id)
            return (3, offer.keitaro_id)

        return sorted(offers, key=rank)[: max(1, min(limit, 100))]

    async def get_offers_map(self, session: AsyncSession, offer_ids: set[int]) -> dict[int, Offer]:
        if not offer_ids:
            return {}
        stmt = select(Offer).where(Offer.keitaro_id.in_(offer_ids))
        return {o.keitaro_id: o for o in (await session.scalars(stmt)).all()}

    async def ensure_offers_usable(self, session: AsyncSession, offer_ids: list[int]) -> list[str]:
        """Проверка перед отправкой в Keitaro (сам он несуществующие ID принимает молча)."""
        await self.refresh_offers(session)
        found = await self.get_offers_map(session, set(offer_ids))
        if any(i not in found or found[i].is_missing for i in offer_ids):
            await self.refresh_offers(session, force=True)
            found = await self.get_offers_map(session, set(offer_ids))
        problems: list[str] = []
        for offer_id in offer_ids:
            offer = found.get(offer_id)
            if offer is None or offer.is_missing:
                problems.append(f"Оффер #{offer_id} не найден в Keitaro (или недоступен ключу API).")
            elif offer.state != "active":
                problems.append(f"Оффер #{offer_id} «{offer.name}» в состоянии «{offer.state}».")
        return problems

    # ------------------------------------------------------------------ группы/источники/домены

    async def get_lookups(self, *, force: bool = False) -> Lookups:
        cached = self._lookups
        ttl = self._settings.dictionary_ttl_seconds
        if cached and not force and time.monotonic() - cached.loaded_at < ttl:
            return cached
        lookups = Lookups(loaded_at=time.monotonic())
        lookups.groups = [self._slim(g) for g in await self._client.list_groups("campaigns")]
        lookups.traffic_sources = [
            {**self._slim(s), "parameters": s.get("parameters") or {},
             "template_name": s.get("template_name") or ""}
            for s in await self._client.list_traffic_sources()
            if s.get("state", "active") == "active"
        ]
        try:
            domains = await self._client.list_domains()
        except KeitaroForbiddenError:
            domains = []
        lookups.domains = [self._slim(d) for d in domains if d.get("state", "active") == "active"]
        if not lookups.domains:
            lookups.domains_visible = False
            lookups.inferred_domain_id = await self._infer_domain_id()
            lookups.warnings.append(
                "Ключу API не виден справочник доменов. "
                + (f"Домен #{lookups.inferred_domain_id} определён по существующим кампаниям."
                   if lookups.inferred_domain_id else
                   "Укажите DEFAULT_DOMAIN_ID в .env или в настройках.")
            )
        self._lookups = lookups
        return lookups

    async def _infer_domain_id(self) -> int | None:
        counter = Counter(
            c["domain_id"] for c in await self._client.list_campaigns()
            if isinstance(c.get("domain_id"), int) and c["domain_id"] > 0
        )
        return counter.most_common(1)[0][0] if counter else None

    @staticmethod
    def _slim(row: dict[str, Any]) -> dict[str, Any]:
        return {"id": row.get("id"), "name": str(row.get("name") or f"#{row.get('id')}")}

    # ------------------------------------------------------------------ значения по умолчанию

    async def get_overrides(self, session: AsyncSession) -> dict[str, Any]:
        rows = (await session.scalars(select(AppSetting).where(
            or_(*[AppSetting.key == k for k in SETTING_KEYS])))).all()
        return {row.key: row.value for row in rows if row.value not in (None, "")}

    async def save_overrides(self, session: AsyncSession, values: dict[str, Any]) -> None:
        for key, value in values.items():
            if key not in SETTING_KEYS:
                continue
            row = await session.get(AppSetting, key) or AppSetting(key=key)
            row.value = value
            row.updated_at = dt.datetime.now(dt.timezone.utc)
            session.add(row)
        await session.commit()

    async def resolve_defaults(self, session: AsyncSession) -> dict[str, Any]:
        """Итоговые значения по умолчанию + откуда каждое взялось (для показа в интерфейсе)."""
        lookups = await self.get_lookups()
        overrides = await self.get_overrides(session)
        env = self._settings

        def pick(key: str, env_value: Any, auto: Any) -> tuple[Any, str]:
            if overrides.get(key) not in (None, ""):
                return overrides[key], "настройки"
            if env_value not in (None, ""):
                return env_value, ".env"
            return auto, "авто" if auto is not None else "не задано"

        auto_group = lookups.groups[0]["id"] if len(lookups.groups) == 1 else None
        auto_source = (lookups.traffic_sources[0]["id"]
                       if len(lookups.traffic_sources) == 1 else None)
        auto_domain = (lookups.domains[0]["id"] if len(lookups.domains) == 1
                       else lookups.inferred_domain_id)
        result: dict[str, Any] = {}
        for key, env_value, auto in (
            ("default_domain_id", env.default_domain_id, auto_domain),
            ("default_group_id", env.default_group_id, auto_group),
            ("default_traffic_source_id", env.default_traffic_source_id, auto_source),
            ("default_redirect_url", env.default_redirect_url, None),
            ("tracking_domain_url", env.tracking_domain_url, None),
        ):
            value, source = pick(key, env_value, auto)
            result[key] = value
            result[f"{key}_source"] = source
        return result
