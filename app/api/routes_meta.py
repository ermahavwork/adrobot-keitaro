"""Служебные маршруты: здоровье, справочники, страны, поиск офферов, настройки, журнал."""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Query
from sqlalchemy import func, select, text

from app import __version__
from app.api.deps import ClientDep, DictionariesDep, SessionDep, SettingsDep
from app.keitaro.countries import country_name, parse_geo_input, search_countries
from app.keitaro.errors import KeitaroError
from app.models import Operation
from app.schemas import SettingsUpdateRequest
from app.services.creator import validate_redirect_url

router = APIRouter(tags=["Служебное"])


@router.get("/health", summary="Проверка: база, настройки, связь с Keitaro")
async def health(session: SessionDep, client: ClientDep, settings: SettingsDep,
                 deep: bool = True) -> dict[str, Any]:
    """`deep=false` — без обращения к Keitaro (для частых liveness-проверок)."""
    result: dict[str, Any] = {
        "status": "ok", "version": __version__, "database": "ok",
        "keitaro": {"configured": client.configured, "reachable": None, "message": ""},
        "admin_url": settings.admin_url,
    }
    try:
        await session.execute(text("SELECT 1 FROM campaigns LIMIT 1"))
    except Exception as exc:  # любая ошибка БД = нездоров, а не 500
        await session.rollback()
        missing = "no such table" in str(exc).lower() or "does not exist" in str(exc).lower()
        result.update(status="error", database=(
            "нет таблиц: выполните `alembic upgrade head`" if missing
            else f"error: {type(exc).__name__}"))
    if not client.configured:
        result["keitaro"]["message"] = "Задайте KEITARO_BASE_URL и KEITARO_API_KEY в .env."
        result["status"] = "setup_required" if result["status"] == "ok" else result["status"]
    elif deep:
        try:
            await client.ping()
            result["keitaro"]["reachable"] = True
        except KeitaroError as exc:
            result["keitaro"].update(reachable=False, message=exc.message)
            result["status"] = "degraded" if result["status"] == "ok" else result["status"]
    return result


@router.get("/meta/lookups", summary="Группы, источники, домены и значения по умолчанию")
async def lookups(session: SessionDep, dictionaries: DictionariesDep,
                  refresh: bool = False) -> dict[str, Any]:
    data = await dictionaries.get_lookups(force=refresh)
    return {
        "groups": data.groups,
        "traffic_sources": [{"id": s["id"], "name": s["name"],
                             "template_name": s.get("template_name", "")}
                            for s in data.traffic_sources],
        "domains": data.domains,
        "domains_visible": data.domains_visible,
        "inferred_domain_id": data.inferred_domain_id,
        "warnings": data.warnings,
        "defaults": await dictionaries.resolve_defaults(session),
    }


@router.get("/meta/countries", summary="Поиск стран для поля Geo")
async def countries(q: str = "", limit: Annotated[int, Query(ge=1, le=300)] = 20) -> list[dict]:
    return search_countries(q, limit)


@router.get("/meta/geo/parse", summary="Разбор строки гео: `MX, au; Румыния` → коды")
async def geo_parse(raw: str = "") -> dict[str, Any]:
    codes, unknown = parse_geo_input(raw)
    return {"codes": [{"code": c, "name": country_name(c)} for c in codes], "unknown": unknown}


@router.get("/offers", summary="Поиск офферов (автокомплит): по ID или части названия")
async def offers(session: SessionDep, dictionaries: DictionariesDep, q: str = "",
                 limit: Annotated[int, Query(ge=1, le=100)] = 20,
                 include_inactive: bool = False) -> list[dict[str, Any]]:
    found = await dictionaries.search_offers(session, q, limit=limit,
                                             include_inactive=include_inactive)
    return [{"id": o.keitaro_id, "name": o.name, "state": o.state, "country": o.country,
             "label": f"[{o.keitaro_id}] {o.name}"} for o in found]


@router.post("/offers/refresh", summary="Перечитать справочник офферов из Keitaro")
async def offers_refresh(session: SessionDep, dictionaries: DictionariesDep) -> dict[str, int]:
    return {"offers": await dictionaries.refresh_offers(session, force=True)}


@router.get("/settings", summary="Значения по умолчанию «создаватора»")
async def settings_get(session: SessionDep, dictionaries: DictionariesDep) -> dict[str, Any]:
    return await dictionaries.resolve_defaults(session)


@router.put("/settings", summary="Сохранить значения по умолчанию")
async def settings_put(body: SettingsUpdateRequest, session: SessionDep,
                       dictionaries: DictionariesDep) -> dict[str, Any]:
    # Пустое значение = «вернуть автоматическое».
    for field in ("default_redirect_url", "tracking_domain_url"):
        value = getattr(body, field)
        if value:
            validate_redirect_url(value)  # тот же контроль, что в «создаваторе»: только http(s)
    await dictionaries.save_overrides(session, body.model_dump())
    return await dictionaries.resolve_defaults(session)


@router.get("/operations", summary="Журнал операций")
async def operations(session: SessionDep, keitaro_campaign_id: int | None = None,
                     status: str | None = None,
                     limit: Annotated[int, Query(ge=1, le=200)] = 50,
                     offset: Annotated[int, Query(ge=0)] = 0) -> dict[str, Any]:
    stmt = select(Operation)
    if keitaro_campaign_id is not None:
        stmt = stmt.where(Operation.keitaro_campaign_id == keitaro_campaign_id)
    if status in ("ok", "error"):
        stmt = stmt.where(Operation.status == status)
    total = await session.scalar(select(func.count()).select_from(stmt.subquery()))
    rows = (await session.scalars(stmt.order_by(Operation.id.desc()).limit(limit)
                                  .offset(offset))).all()
    return {"total": total or 0, "items": [
        {"id": op.id, "created_at": op.created_at.isoformat(), "action": op.action,
         "actor": op.actor, "status": op.status, "keitaro_campaign_id": op.keitaro_campaign_id,
         "keitaro_stream_id": op.keitaro_stream_id, "summary": op.summary,
         "error": op.error, "duration_ms": op.duration_ms, "details": op.details}
        for op in rows]}
