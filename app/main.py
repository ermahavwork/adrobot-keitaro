"""Точка входа: сборка приложения FastAPI.

Запуск:  uvicorn app.main:app --reload
Схема БД создаётся миграциями (`alembic upgrade head`), не при старте.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlsplit

import httpx
from fastapi import Depends, FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.openapi.docs import get_swagger_ui_html
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy.exc import OperationalError
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.middleware.trustedhost import TrustedHostMiddleware

from app import __version__
from app.api import routes_campaigns, routes_meta, routes_streams, routes_tools
from app.api.deps import require_token
from app.config import Settings, get_settings
from app.keitaro.client import KeitaroClient
from app.keitaro.errors import KeitaroError
from app.logging_conf import setup_logging
from app.services import editor
from app.services.audit import current_actor
from app.services.creator import CampaignCreator, CreatorError
from app.services.dictionaries import DictionaryService
from app.services.editor import EditorError
from app.services.locks import CampaignLocks, LockBusyError

logger = logging.getLogger(__name__)
WEB_DIR = Path(__file__).parent / "web"

SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}
SECURITY_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "no-referrer",
    "Permissions-Policy": "camera=(), microphone=(), geolocation=()",
}
# Cross-Origin-Opener-Policy не ставим: на http-адресе в локальной сети браузер его игнорирует
# и пишет об этом ошибку в консоль, а всплывающих окон у интерфейса нет.
# Интерфейс не использует ни inline-скриптов, ни eval, ни сторонних доменов.
CSP_APP = ("default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; "
           "connect-src 'self'; base-uri 'none'; form-action 'self'; frame-ancestors 'none'")


def _error(code: str, message: str, status: int, details: Any = None) -> JSONResponse:
    return JSONResponse(status_code=status,
                        content={"error": {"code": code, "message": message, "details": details}})


def create_app(
    settings: Settings | None = None,
    *,
    transport: httpx.AsyncBaseTransport | None = None,
) -> FastAPI:
    """Фабрика приложения. `transport` подменяет сеть в тестах (эмулятор Keitaro)."""
    settings = settings or get_settings()
    setup_logging(settings.log_level, secrets=[
        settings.keitaro_api_key.get_secret_value(),
        settings.adrobot_auth_token.get_secret_value(),
        *[token for _name, token, _ro in settings.auth_tokens()],
    ])

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        client = KeitaroClient(
            settings.keitaro_base_url,
            settings.keitaro_api_key.get_secret_value(),
            timeout=settings.keitaro_timeout_seconds,
            max_retries=settings.keitaro_max_retries,
            max_concurrency=settings.keitaro_max_concurrency,
            user_agent=settings.keitaro_user_agent,
            transport=transport,
            backoff_base=0.0 if transport is not None else 0.4,
        )
        dictionaries = DictionaryService(client, settings)
        app.state.settings = settings
        app.state.client = client
        app.state.dictionaries = dictionaries
        app.state.creator = CampaignCreator(client, dictionaries, settings)
        app.state.locks = CampaignLocks()
        editor.MAX_SNAPSHOTS_PER_STREAM = settings.snapshots_per_stream
        if not settings.keitaro_configured:
            logger.warning("Keitaro не настроен: заполните KEITARO_BASE_URL и KEITARO_API_KEY")
        yield
        await client.aclose()

    app = FastAPI(
        title="AdRobot · Keitaro",
        version=__version__,
        description=(
            "«Создаватор» кампаний и редактор офферов потока поверх Admin API Keitaro.\n\n"
            "Все кнопки интерфейса — это методы ниже; их можно дёргать прямо отсюда."
        ),
        lifespan=lifespan,
        docs_url=None,  # свою страницу /docs отдаём ниже: со скриптами из репозитория, без CDN
        redoc_url=None,
        # ROOT_PATH не отдаём в маршрутизацию намеренно. Прокси уже отрезал префикс, и адреса
        # приходят без него. Starlette при заданном root_path ломает примонтированные каталоги:
        # StaticFiles начинает искать файл на уровень глубже, и вся статика отвечает 404.
        # Префикс нужен ровно в одном месте — чтобы кнопка Try it out в Swagger UI била по
        # верным адресам. Для этого хватает записи servers в схеме.
        servers=([{"url": prefix}] if (prefix := settings.root_path.rstrip("/")) else None),
    )

    allowed_hosts = [host.strip() for host in settings.allowed_hosts.split(",") if host.strip()]
    if allowed_hosts:
        # Защита от DNS-rebinding: отвечаем только на запросы к перечисленным именам.
        app.add_middleware(TrustedHostMiddleware, allowed_hosts=allowed_hosts)

    @app.get("/docs", include_in_schema=False)
    async def swagger_ui() -> HTMLResponse:
        # Адреса относительные: страница одинаково работает и в корне, и в подкаталоге за прокси.
        return get_swagger_ui_html(
            openapi_url="openapi.json", title="AdRobot · API",
            swagger_js_url="static/vendor/swagger-ui/swagger-ui-bundle.js",
            swagger_css_url="static/vendor/swagger-ui/swagger-ui.css",
            swagger_favicon_url="static/img/favicon.svg",
        )

    @app.middleware("http")
    async def security_and_actor(request: Request, call_next):  # type: ignore[no-untyped-def]
        # Изменяющий запрос с чужого сайта отклоняем. Без этого страница-ловушка могла бы одной
        # скрытой формой нажать Push у того, кто держит AdRobot открытым на localhost (CSRF):
        # токен по умолчанию не требуется, а POST без тела браузер шлёт без предварительного запроса.
        if request.method not in SAFE_METHODS and request.url.path.startswith("/api/"):
            origin = request.headers.get("origin")
            # За обратным прокси «наш» адрес может приехать в X-Forwarded-Host, а не в Host.
            own_hosts = {request.headers.get("host", ""), request.headers.get("x-forwarded-host", "")}
            foreign_origin = bool(origin) and urlsplit(origin).netloc not in own_hosts
            if request.headers.get("sec-fetch-site") == "cross-site" or foreign_origin:
                return _error("cross_site_request", "Запрос пришёл с другого сайта и отклонён. "
                              "Откройте AdRobot по его адресу.", 403)
        # Имя приходит percent-encoded: в HTTP-заголовках нельзя передавать кириллицу как есть.
        actor = unquote(request.headers.get("X-AdRobot-User") or "").strip()[:64]
        token = current_actor.set(actor)
        try:
            response = await call_next(request)
        finally:
            current_actor.reset(token)
        for name, value in SECURITY_HEADERS.items():
            response.headers.setdefault(name, value)
        if not request.url.path.startswith(("/docs", "/redoc", "/openapi.json")):
            response.headers.setdefault("Content-Security-Policy", CSP_APP)
        if request.url.path.startswith("/api/"):
            response.headers.setdefault("Cache-Control", "no-store")
        return response

    @app.exception_handler(EditorError)
    async def _editor_error(_: Request, exc: EditorError) -> JSONResponse:
        return _error(exc.code, exc.message, exc.http_status, exc.details)

    @app.exception_handler(CreatorError)
    async def _creator_error(_: Request, exc: CreatorError) -> JSONResponse:
        details = exc.details if exc.details is not None else ({"errors": exc.errors}
                                                                if exc.errors else None)
        return _error(exc.code, exc.message, exc.http_status, details)

    @app.exception_handler(KeitaroError)
    async def _keitaro_error(_: Request, exc: KeitaroError) -> JSONResponse:
        return _error(exc.code, exc.message, exc.http_status, exc.details)

    @app.exception_handler(LockBusyError)
    async def _lock_busy(_: Request, exc: LockBusyError) -> JSONResponse:
        return _error(exc.code, exc.message, exc.http_status)

    @app.exception_handler(RequestValidationError)
    async def _validation_error(_: Request, exc: RequestValidationError) -> JSONResponse:
        problems = []
        for err in exc.errors():
            field = ".".join(str(p) for p in err.get("loc", []) if p not in ("body", "query"))
            problems.append(f"{field}: {err.get('msg', '')}".strip(": "))
        return _error("validation", "Проверьте данные запроса: " + "; ".join(problems), 422,
                      {"errors": problems})

    @app.exception_handler(OverflowError)
    async def _number_too_big(_: Request, exc: OverflowError) -> JSONResponse:
        # ID из пути или query (…/campaigns/9223372036854775808) не помещается в INTEGER базы:
        # sqlite3 бросает OverflowError. Это ошибка запроса, а не сервера.
        logger.warning("число вне диапазона базы данных: %s", exc)
        problem = "число слишком велико для идентификатора"
        return _error("validation", f"Проверьте данные запроса: {problem}.", 422,
                      {"errors": [problem]})

    @app.exception_handler(StarletteHTTPException)
    async def _http_error(_: Request, exc: StarletteHTTPException) -> JSONResponse:
        response = _error("http_error", str(exc.detail), exc.status_code)
        for name, value in (exc.headers or {}).items():
            response.headers[name] = value
        return response

    @app.exception_handler(OperationalError)
    async def _db_error(_: Request, exc: OperationalError) -> JSONResponse:
        logger.error("ошибка базы данных: %s", exc.orig)
        hint = ("Таблицы не созданы — выполните `alembic upgrade head`."
                if "no such table" in str(exc.orig).lower() or "does not exist" in str(exc.orig).lower()
                else "База данных недоступна или занята — повторите запрос.")
        return _error("database_error", hint, 503)

    @app.exception_handler(Exception)
    async def _unexpected(_: Request, exc: Exception) -> JSONResponse:
        logger.exception("необработанная ошибка: %s", type(exc).__name__)
        return _error("internal_error", "Внутренняя ошибка AdRobot. Подробности — в логе сервера.",
                      500)

    protected = [Depends(require_token)]
    app.include_router(routes_meta.router, prefix="/api", dependencies=protected)
    app.include_router(routes_campaigns.router, prefix="/api", dependencies=protected)
    # routes_tools раньше routes_streams: у него есть точные пути (/streams/drafts, /streams/push-many),
    # которые иначе перехватил бы шаблон /streams/{stream_id}.
    app.include_router(routes_tools.router, prefix="/api", dependencies=protected)
    app.include_router(routes_streams.router, prefix="/api", dependencies=protected)

    @app.get("/api/auth/me", tags=["Служебное"], dependencies=protected,
             summary="Чей это токен и можно ли с ним что-то менять")
    async def auth_me(request: Request) -> dict[str, Any]:
        """Интерфейс спрашивает это один раз: с токеном «только чтение» он не делает авто-Fetch."""
        return {"name": getattr(request.state, "token_name", ""),
                "read_only": getattr(request.state, "read_only", False)}

    @app.get("/api/auth/mode", tags=["Служебное"], summary="Нужен ли интерфейсу токен доступа")
    async def auth_mode() -> dict[str, bool]:
        return {"token_required": settings.auth_enabled}

    app.mount("/static", StaticFiles(directory=WEB_DIR / "static"), name="static")

    @app.get("/", include_in_schema=False)
    async def index() -> FileResponse:
        return FileResponse(WEB_DIR / "templates" / "index.html",
                            headers={"Cache-Control": "no-cache"})

    return app


app = create_app()
