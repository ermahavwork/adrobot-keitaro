"""«Создаватор» кампаний: имя + гео + оффер → готовая кампания Keitaro с двумя потоками.

    Flow 1  ловит всех из указанной страны (фильтр country/accept) и редиректит на Google;
    Flow 2  ловит остальных и отправляет на выбранный оффер (или на несколько — поровну).

Кампании проставляются домен, группа и источник; параметры источника (utm_*, sub_id_*)
копируются в кампанию — админка Keitaro делает это сама, а API нет.

Создание — это три запроса подряд, поэтому оно оформлено как сага:

1. всё, что Keitaro принял бы молча (несуществующий оффер, кривой код страны, чужой
   ID группы), проверяем ДО первого запроса;
2. если кампания создалась, а поток — нет, кампания отправляется в архив (компенсация),
   чтобы в трекере не оставалось половинчатых сущностей;
3. повтор запроса с тем же ключом идемпотентности возвращает прежний результат,
   а не создаёт дубль (двойной клик, ретрай после обрыва связи).
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import logging
import secrets
import string
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlsplit

from sqlalchemy import delete, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.config import Settings
from app.keitaro.client import KeitaroClient
from app.keitaro.countries import country_name, parse_geo_input
from app.keitaro.errors import KeitaroError, KeitaroValidationError
from app.models import STATE_ACTIVE, Campaign, IdempotencyRecord, Stream, StreamOffer, utcnow
from app.schemas import CampaignCreateRequest
from app.services import weights
from app.services.dictionaries import DictionaryService
from app.services.weights import WeightItem

logger = logging.getLogger(__name__)

_ALIAS_ALPHABET = string.ascii_letters + string.digits
_ALIAS_ATTEMPTS = 4
MAX_CAMPAIGNS_PER_REQUEST = 30
# Сколько отметка «запрос выполняется» считается живой (создание занимает секунды).
IN_PROGRESS_TTL = dt.timedelta(minutes=5)


class CreatorError(Exception):
    """Ошибка создания с понятным текстом; `errors` — список проблем валидации."""

    def __init__(self, code: str, message: str, *, http_status: int = 422,
                 errors: list[str] | None = None, details: Any = None) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.http_status = http_status
        self.errors = errors or []
        self.details = details


@dataclass
class CampaignPlan:
    """Что именно будет отправлено в Keitaro для одной кампании."""

    name: str
    geo: list[str]
    offer_ids: list[int]
    campaign_payload: dict[str, Any]
    geo_stream_payload: dict[str, Any]
    offer_stream_payload: dict[str, Any]
    custom_alias: bool = False  # алиас задал пользователь: при конфликте не подменяем молча
    warnings: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "geo": self.geo,
            "offer_ids": self.offer_ids,
            "warnings": self.warnings,
            "requests": [
                {"method": "POST", "path": "/campaigns", "body": self.campaign_payload},
                {"method": "POST", "path": "/streams", "body": self.geo_stream_payload},
                {"method": "POST", "path": "/streams", "body": self.offer_stream_payload},
            ],
        }


def generate_alias(length: int = 8) -> str:
    """Случайный алиас в стиле админки Keitaro (`wVqN1R`). Keitaro требует его при создании."""
    return "".join(secrets.choice(_ALIAS_ALPHABET) for _ in range(length))


def validate_redirect_url(url: str) -> str:
    url = (url or "").strip()
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https") or not parts.netloc or " " in url:
        raise CreatorError("bad_redirect_url",
                           "Адрес редиректа должен быть полным URL вида https://google.com.")
    return url


def split_shares(offer_ids: list[int]) -> list[dict[str, Any]]:
    """Доли для офферов второго потока по тем же правилам, что и в редакторе."""
    shares = weights.rebalance([WeightItem(i, 0, False, n) for n, i in enumerate(offer_ids)])
    return [{"offer_id": i, "share": shares[i], "state": STATE_ACTIVE} for i in offer_ids]


class CampaignCreator:
    def __init__(self, client: KeitaroClient, dictionaries: DictionaryService,
                 settings: Settings) -> None:
        self._client = client
        self._dictionaries = dictionaries
        self._settings = settings

    # ------------------------------------------------------------------ планирование

    async def build_plans(
        self, session: AsyncSession, request: CampaignCreateRequest
    ) -> list[CampaignPlan]:
        """Проверяет ввод и готовит запросы. Ничего не отправляет в Keitaro на запись."""
        errors: list[str] = []
        geo_raw = request.geo if isinstance(request.geo, str) else ",".join(request.geo)
        geo, unknown = parse_geo_input(geo_raw)
        if unknown:
            errors.append("Не распознаны коды стран: " + ", ".join(unknown[:10])
                          + ". Нужны коды ISO 3166-1 alpha-2, например AU, RO, MX.")
        if not geo and not unknown:
            errors.append("Укажите хотя бы одну страну (например, AU).")

        errors.extend(await self._dictionaries.ensure_offers_usable(session, request.offer_ids))

        defaults = await self._dictionaries.resolve_defaults(session)
        lookups = await self._dictionaries.get_lookups()
        warnings = list(lookups.warnings)
        domain_id = request.domain_id or defaults["default_domain_id"]
        group_id = request.group_id or defaults["default_group_id"]
        source_id = request.traffic_source_id or defaults["default_traffic_source_id"]

        if group_id is None:
            errors.append("Не выбрана группа кампаний (в трекере их несколько или ни одной).")
        elif group_id not in lookups.group_ids():
            errors.append(f"Группы #{group_id} нет в Keitaro.")
        if source_id is None:
            errors.append("Не выбран источник трафика (в трекере их несколько или ни одного).")
        elif source_id not in lookups.source_ids():
            errors.append(f"Источника трафика #{source_id} нет в Keitaro.")
        if domain_id is None:
            errors.append("Не выбран домен. Укажите DEFAULT_DOMAIN_ID в .env или в настройках.")
        elif lookups.domains_visible and domain_id not in lookups.domain_ids():
            errors.append(f"Домена #{domain_id} нет в Keitaro.")
        elif not lookups.domains_visible and domain_id not in lookups.domain_ids():
            warnings.append(f"Домен #{domain_id} проверить нельзя: справочник доменов скрыт "
                            "от ключа API.")

        redirect_url = request.redirect_url or defaults["default_redirect_url"]
        try:
            redirect_url = validate_redirect_url(str(redirect_url))
        except CreatorError as exc:
            errors.append(exc.message)

        geo_sets = [[code] for code in geo] if request.split_by_geo and len(geo) > 1 else [geo]
        if len(geo_sets) > MAX_CAMPAIGNS_PER_REQUEST:
            errors.append(f"За один раз можно создать не больше {MAX_CAMPAIGNS_PER_REQUEST} "
                          "кампаний.")
        if request.alias and len(geo_sets) > 1:
            errors.append("Свой алиас можно задать только для одной кампании.")
        if errors:
            raise CreatorError("validation", "Проверьте форму: " + " ".join(errors), errors=errors)

        source = lookups.source_by_id(source_id) or {}
        plans = []
        for codes in geo_sets:
            name = request.name
            # {geo} подставляется всегда: одна страна — её код, несколько стран в одной
            # кампании — коды через «+» (AU+RO). Без плейсхолдера код дописывается только
            # при разбиении по странам, чтобы кампании различались.
            if "{geo}" in name:
                name = name.replace("{geo}", "+".join(codes))
            elif request.split_by_geo:
                # Суффикс ставим и при одной стране: если из пачки «MX, AU» создалась только MX,
                # повтор по одной AU должен дать то же имя «… [AU]», а не кампанию без суффикса.
                name = f"{name} [{codes[0]}]"
            plans.append(CampaignPlan(
                name=name, geo=codes, offer_ids=list(request.offer_ids), warnings=list(warnings),
                custom_alias=bool(request.alias),
                campaign_payload={
                    "name": name,
                    "alias": request.alias or generate_alias(),
                    "type": "position",
                    "state": "active",
                    "cost_type": "CPC",
                    "cost_auto": True,
                    "uniqueness_method": "ip_ua",
                    "cookies_ttl": 24,
                    "domain_id": domain_id,
                    "group_id": group_id,
                    "traffic_source_id": source_id,
                    "parameters": source.get("parameters") or {},
                },
                geo_stream_payload={
                    "type": "regular",
                    "name": self._settings.default_geo_stream_name,
                    "position": 1,
                    "schema": "redirect",
                    "action_type": "http",
                    "action_payload": redirect_url,
                    "state": "active",
                    "collect_clicks": True,
                    "filter_or": False,
                    "weight": 100,
                    "filters": [{"name": "country", "mode": "accept", "payload": codes}],
                    "comments": "Гео " + ", ".join(f"{c} ({country_name(c)})" for c in codes)
                                + " → редирект. Создано AdRobot.",
                },
                offer_stream_payload={
                    "type": "regular",
                    "name": self._settings.default_offer_stream_name,
                    "position": 2,
                    "schema": "landings",
                    "action_type": "http",
                    "state": "active",
                    "collect_clicks": True,
                    "filter_or": False,
                    "weight": 100,
                    "offers": split_shares(request.offer_ids),
                    "comments": "Все остальные → оффер(ы). Создано AdRobot.",
                },
            ))
        return plans

    # ------------------------------------------------------------------ идемпотентность

    @staticmethod
    def request_hash(request: CampaignCreateRequest) -> str:
        body = request.model_dump(mode="json", exclude={"dry_run"})
        return hashlib.sha256(json.dumps(body, sort_keys=True).encode()).hexdigest()

    async def find_previous(
        self, session: AsyncSession, key: str | None, request: CampaignCreateRequest
    ) -> dict[str, Any] | None:
        if not key:
            return None
        record = await session.get(IdempotencyRecord, key)
        if record is None:
            return None
        if record.request_hash != self.request_hash(request):
            raise CreatorError(
                "idempotency_mismatch",
                "Этот ключ идемпотентности уже использован для другого запроса.", http_status=409)
        if record.response.get("in_progress"):
            if utcnow() - record.created_at < IN_PROGRESS_TTL:
                raise CreatorError(
                    "in_progress",
                    "Такой же запрос на создание уже выполняется. Дождитесь результата — "
                    "повтор создал бы дубль кампании.", http_status=409)
            # Отметка осталась от процесса, который упал посреди создания: не держим ключ вечно.
            await session.delete(record)
            await session.commit()
            return None
        return {**record.response, "replayed": True}

    async def _reserve_key(self, session: AsyncSession, key: str,
                           request: CampaignCreateRequest) -> None:
        """Занимает ключ ДО обращения к Keitaro: из двух одновременных дублей пройдёт один."""
        session.add(IdempotencyRecord(key=key, request_hash=self.request_hash(request),
                                      response={"in_progress": True}))
        try:
            await session.commit()
        except IntegrityError as exc:
            await session.rollback()
            raise CreatorError(
                "in_progress", "Такой же запрос на создание уже выполняется.",
                http_status=409) from exc

    @staticmethod
    async def _release_key(session: AsyncSession, key: str,
                           response: dict[str, Any] | None) -> None:
        """Успех — сохраняем ответ для повторов; неудача — освобождаем ключ для новой попытки."""
        record = await session.get(IdempotencyRecord, key)
        if record is None:
            return
        if response is None:
            await session.delete(record)
        else:
            record.response = response
        await session.commit()

    # ------------------------------------------------------------------ создание

    async def create(
        self,
        session: AsyncSession,
        request: CampaignCreateRequest,
        *,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        if idempotency_key:
            # Ключ нужен минуты (двойной клик, ретрай после обрыва), а не вечно: старые записи убираем.
            cutoff = utcnow() - dt.timedelta(days=self._settings.idempotency_ttl_days)
            await session.execute(delete(IdempotencyRecord).where(IdempotencyRecord.created_at < cutoff))
            await session.commit()
        # Проверка без создания ключом не пользуется: она ничего не создаёт и должна показывать
        # план, а не «повтор» когда-то выполненного настоящего запроса.
        previous = None if request.dry_run else await self.find_previous(session, idempotency_key, request)
        if previous is not None:
            return previous

        # Ключ занимаем ДО проверок формы и названия. Иначе отставший на долю секунды дубль
        # (двойной клик) проходил проверку ключа, пока первый запрос его ещё не занял, а потом
        # натыкался на только что созданную кампанию и получал «название занято» вместо «уже выполняется».
        reserve = bool(idempotency_key) and not request.dry_run
        if reserve:
            await self._reserve_key(session, idempotency_key, request)
        try:
            plans = await self.build_plans(session, request)
            if not request.allow_duplicate_name:
                await self._ensure_names_free(plans)
            if request.dry_run:
                return {"dry_run": True, "results": [
                    {"status": "planned", "plan": plan.as_dict()} for plan in plans]}
            response = await self._create_all(session, plans)
        except BaseException:
            if reserve:
                await session.rollback()
                await self._release_key(session, idempotency_key, None)
            raise
        if reserve:
            created_any = any(r["status"] == "created" for r in response["results"])
            await self._release_key(session, idempotency_key, response if created_any else None)
        return response

    async def _create_all(self, session: AsyncSession,
                          plans: list[CampaignPlan]) -> dict[str, Any]:
        results = []
        for plan in plans:
            try:
                results.append({"status": "created",
                                **await self._create_one(session, plan), "warnings": plan.warnings})
            except (CreatorError, KeitaroError) as exc:
                await session.rollback()
                logger.warning("кампания «%s» не создана: %s", plan.name, exc)
                results.append({
                    "status": "error", "name": plan.name, "geo": plan.geo,
                    "error": {"code": getattr(exc, "code", "error"),
                              "message": getattr(exc, "message", str(exc)),
                              "details": getattr(exc, "details", None)},
                })
        return {"dry_run": False, "results": results}

    async def _ensure_names_free(self, plans: list[CampaignPlan]) -> None:
        existing = {str(c.get("name") or "").casefold(): c.get("id")
                    for c in await self._client.list_campaigns()}
        taken = [f"«{p.name}» (#{existing[p.name.casefold()]})" for p in plans
                 if p.name.casefold() in existing]
        if taken:
            raise CreatorError(
                "duplicate_name",
                "В Keitaro уже есть кампания с таким названием: " + ", ".join(taken)
                + ". Измените название или подтвердите создание дубля.",
                http_status=409, details={"needs": "allow_duplicate_name"})

    async def _create_one(self, session: AsyncSession, plan: CampaignPlan) -> dict[str, Any]:
        campaign_row = await self._post_campaign(plan)
        keitaro_id = campaign_row["id"]
        try:
            geo_stream = await self._client.create_stream(
                {**plan.geo_stream_payload, "campaign_id": keitaro_id})
            offer_stream = await self._client.create_stream(
                {**plan.offer_stream_payload, "campaign_id": keitaro_id})
            self._verify(plan, geo_stream, offer_stream)
            # Сохранение у себя — тоже часть саги: если оно упадёт, в трекере останется кампания,
            # о которой AdRobot не знает, а повтор упрётся в «название занято».
            campaign = await self._save_locally(session, plan, campaign_row, geo_stream, offer_stream)
        except Exception as exc:
            await session.rollback()
            rolled_back = await self._compensate(keitaro_id)
            tail = (" Недосозданная кампания отправлена в архив Keitaro." if rolled_back else
                    f" ВНИМАНИЕ: кампанию #{keitaro_id} не удалось убрать — удалите её вручную.")
            raise CreatorError(
                "stream_creation_failed",
                f"Кампания создалась, но довести её до конца не удалось: "
                f"{getattr(exc, 'message', None) or type(exc).__name__}.{tail}",
                http_status=502,
                details={"keitaro_campaign_id": keitaro_id, "rolled_back": rolled_back}) from exc

        tracking = await self._dictionaries.tracking_domain_url(session)
        return {
            "campaign_id": campaign.id,
            "keitaro_campaign_id": keitaro_id,
            "name": plan.name,
            "alias": campaign.alias,
            "geo": plan.geo,
            "offer_ids": plan.offer_ids,
            "stream_ids": [geo_stream["id"], offer_stream["id"]],
            "admin_url": self._settings.campaign_admin_url(keitaro_id),
            "campaign_url": f"{tracking}/{campaign.alias}" if tracking else "",
        }

    async def _post_campaign(self, plan: CampaignPlan) -> dict[str, Any]:
        payload = dict(plan.campaign_payload)
        for _attempt in range(_ALIAS_ATTEMPTS):
            try:
                row = await self._client.create_campaign(payload)
            except KeitaroValidationError as exc:
                alias_taken = isinstance(exc.details, dict) and "alias" in exc.details
                if not alias_taken:
                    raise
                if plan.custom_alias:
                    raise CreatorError(
                        "alias_taken", f"Алиас «{payload['alias']}» уже занят в Keitaro.",
                        http_status=409) from exc
                payload["alias"] = generate_alias()  # случайный алиас совпал — берём другой
                continue
            if not isinstance(row.get("id"), int):
                raise CreatorError("bad_response", "Keitaro не вернул ID созданной кампании.",
                                   http_status=502)
            return row
        raise CreatorError("alias_taken", "Не удалось подобрать свободный алиас.", http_status=502)

    @staticmethod
    def _verify(plan: CampaignPlan, geo_stream: dict[str, Any],
                offer_stream: dict[str, Any]) -> None:
        """Сверяем, что Keitaro сохранил именно то, что мы просили."""
        filters = geo_stream.get("filters") or []
        saved_geo = sorted(filters[0].get("payload") or []) if filters else []
        if saved_geo != sorted(plan.geo) or geo_stream.get("schema") != "redirect":
            raise CreatorError("verify_failed", "Keitaro сохранил гео-поток не так, как запрошено.",
                               http_status=502)
        saved = sorted((o.get("offer_id"), o.get("share")) for o in offer_stream.get("offers") or [])
        wanted = sorted((o["offer_id"], o["share"]) for o in plan.offer_stream_payload["offers"])
        if saved != wanted:
            raise CreatorError("verify_failed",
                               "Keitaro сохранил офферы потока не так, как запрошено.",
                               http_status=502)

    async def _compensate(self, keitaro_campaign_id: int) -> bool:
        try:
            await self._client.archive_campaign(keitaro_campaign_id)
        except KeitaroError:
            logger.exception("не удалось убрать недосозданную кампанию %s", keitaro_campaign_id)
            return False
        return True

    @staticmethod
    async def _save_locally(
        session: AsyncSession,
        plan: CampaignPlan,
        campaign_row: dict[str, Any],
        geo_stream: dict[str, Any],
        offer_stream: dict[str, Any],
    ) -> Campaign:
        """Кампания сразу попадает в базу AdRobot — редактор готов без отдельного Fetch."""
        stmt = (select(Campaign).where(Campaign.keitaro_id == campaign_row["id"])
                .options(selectinload(Campaign.streams).selectinload(Stream.bindings)))
        campaign = (await session.scalars(stmt)).first() or Campaign(keitaro_id=campaign_row["id"])
        campaign.streams.clear()  # на случай, если шапку успел подтянуть импорт списка кампаний
        payload = plan.campaign_payload
        campaign.name, campaign.alias = plan.name, str(campaign_row.get("alias") or payload["alias"])
        campaign.state, campaign.geo, campaign.origin = "active", plan.geo, "adrobot"
        campaign.group_id, campaign.domain_id = payload["group_id"], payload["domain_id"]
        campaign.traffic_source_id = payload["traffic_source_id"]
        now = utcnow()
        campaign.streams_fetched_at = now
        for row, sent in ((geo_stream, plan.geo_stream_payload),
                          (offer_stream, plan.offer_stream_payload)):
            stream = Stream(
                keitaro_id=row["id"], name=sent["name"], position=sent["position"],
                type=sent["type"], schema=sent["schema"], state="active",
                action_type=sent["action_type"], fetched_at=now,
                summary={"action_payload": sent.get("action_payload", ""),
                         "filters": [{k: f[k] for k in ("name", "mode", "payload")}
                                     for f in sent.get("filters", [])],
                         "landings": 0},
            )
            for index, offer in enumerate(row.get("offers") or [], start=1):
                stream.bindings.append(StreamOffer(
                    offer_id=offer["offer_id"], state=STATE_ACTIVE, share=offer["share"],
                    kt_state=STATE_ACTIVE, kt_share=offer["share"], kt_binding_id=offer.get("id"),
                    was_published=True, sort_index=index))
            campaign.streams.append(stream)
        session.add(campaign)
        await session.commit()
        return campaign
