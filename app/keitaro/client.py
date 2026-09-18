"""Асинхронный клиент Admin API Keitaro (v1).

Что учтено по итогам проверки на живом трекере (см. docs/KEITARO_API_NOTES.md):

* авторизация — заголовок `Api-Key`; ключ не попадает ни в логи, ни в тексты ошибок;
* перед трекером может стоять Cloudflare, который банит User-Agent `Python-urllib`
  (ошибка 1010) — поэтому всегда шлём собственный User-Agent;
* ошибки приходят в трёх форматах: JSON `{"error": ...}`, JSON `{поле: [сообщения]}` (422)
  и просто текст (404) — разбираем все;
* `PUT /streams/{id}` — частичное обновление: можно прислать только `offers`,
  остальные поля потока не трогаются; массив `offers` заменяет набор целиком;
* Keitaro НЕ проверяет: сумму долей, существование оффера/группы/источника/домена,
  коды стран, дубли офферов в потоке. Всё это проверяет AdRobot до отправки.

Повторы: GET и PUT идемпотентны — повторяем при сетевых сбоях, 429 и 502/503/504
с экспоненциальной паузой. POST (создание) при таймауте НЕ повторяем: неизвестно,
создалась ли сущность, и повтор наплодил бы дубли.
"""

from __future__ import annotations

import asyncio
import logging
import random
from typing import Any

import httpx

from app.keitaro.errors import (
    KeitaroAuthError,
    KeitaroError,
    KeitaroForbiddenError,
    KeitaroNetworkError,
    KeitaroNotConfiguredError,
    KeitaroNotFoundError,
    KeitaroProtocolError,
    KeitaroRateLimitError,
    KeitaroServerError,
    KeitaroValidationError,
)

logger = logging.getLogger(__name__)

_RETRY_STATUSES = {429, 502, 503, 504}
_IDEMPOTENT_METHODS = {"GET", "PUT", "DELETE"}
_PAGE_SIZE = 500
_MAX_PAGES = 100
_MAX_BACKOFF_SECONDS = 15.0


class KeitaroClient:
    """Тонкая типизированная обёртка над REST API. Бизнес-логики здесь нет."""

    def __init__(
        self,
        base_url: str,
        api_key: str,
        *,
        timeout: float = 20.0,
        max_retries: int = 3,
        max_concurrency: int = 4,
        user_agent: str = "AdRobot-Keitaro/1.0",
        transport: httpx.AsyncBaseTransport | None = None,
        backoff_base: float = 0.4,
    ) -> None:
        self._configured = bool(base_url and api_key)
        # Ключ, который вырезаем из текстов ошибок; совсем короткий не трогаем — изрежет обычный текст.
        self._secret = api_key if len(api_key) >= 6 else ""
        self._max_retries = max(0, max_retries)
        self._backoff_base = backoff_base
        self._semaphore = asyncio.Semaphore(max(1, max_concurrency))
        self._http = httpx.AsyncClient(
            base_url=f"{base_url.rstrip('/')}/admin_api/v1" if base_url else "http://unconfigured",
            headers={
                "Api-Key": api_key,
                "Accept": "application/json",
                "User-Agent": user_agent,
            },
            timeout=httpx.Timeout(timeout, connect=min(timeout, 10.0)),
            transport=transport,
            follow_redirects=False,
        )

    @property
    def configured(self) -> bool:
        return self._configured

    async def aclose(self) -> None:
        await self._http.aclose()

    # ------------------------------------------------------------------ транспорт

    async def _request(
        self,
        method: str,
        path: str,
        *,
        json: Any = None,
        params: dict[str, Any] | None = None,
        idempotent: bool | None = None,
    ) -> Any:
        if not self._configured:
            raise KeitaroNotConfiguredError(
                "Keitaro не настроен: задайте KEITARO_BASE_URL и KEITARO_API_KEY в файле .env."
            )
        if idempotent is None:
            idempotent = method in _IDEMPOTENT_METHODS
        attempts = 1 + (self._max_retries if idempotent else 0)
        last_error: KeitaroError | None = None
        for attempt in range(1, attempts + 1):
            retry_after = 0.0
            try:
                async with self._semaphore:
                    response = await self._http.request(method, path, json=json, params=params)
            except httpx.TimeoutException:
                last_error = KeitaroNetworkError(
                    f"Keitaro не ответил вовремя ({method} {path})."
                    + ("" if idempotent else
                       " Запрос на создание мог выполниться — проверьте трекер перед повтором.")
                )
            except httpx.HTTPError as exc:
                # Обрыв на чтении ответа (ReadError) случается уже ПОСЛЕ того, как трекер принял
                # запрос, поэтому для создания предупреждаем так же, как при таймауте.
                last_error = KeitaroNetworkError(
                    f"Нет связи с Keitaro ({method} {path}): {type(exc).__name__}."
                    + ("" if idempotent or isinstance(exc, httpx.ConnectError) else
                       " Запрос на создание мог выполниться — проверьте трекер перед повтором.")
                )
            else:
                if response.status_code in _RETRY_STATUSES and attempt < attempts:
                    last_error = self._scrub(self._error_from_response(response, method, path))
                    retry_after = self._retry_after(response)
                else:
                    return self._parse(response, method, path)
            if attempt < attempts:
                # Случайная добавка к паузе — не криптография: разводит повторы по времени.
                jitter = 1 + random.random() / 4  # noqa: S311  # nosec B311
                delay = self._backoff_base * (2 ** (attempt - 1)) * jitter
                delay = min(max(delay, retry_after), _MAX_BACKOFF_SECONDS)
                logger.warning("Keitaro %s %s: попытка %s/%s не удалась, повтор через %.1f с",
                               method, path, attempt, attempts, delay)
                await asyncio.sleep(delay)
        # Сюда попадаем только после неудачной последней попытки: ошибка уже записана.
        raise last_error or KeitaroNetworkError(f"Не удалось выполнить {method} {path}.")

    @staticmethod
    def _retry_after(response: httpx.Response) -> float:
        """Пауза из заголовка Retry-After (секунды); даты и мусор игнорируем."""
        try:
            return max(0.0, float(response.headers.get("Retry-After", "0")))
        except ValueError:
            return 0.0

    def _scrub(self, error: KeitaroError) -> KeitaroError:
        """Вырезает ключ API из текста и деталей ошибки.

        Прокси или отладочная страница перед трекером могут вернуть в теле ответа заголовки
        запроса — без этого ключ ушёл бы во фронтенд и в журнал операций.
        """
        if self._secret:
            error.message = self._scrub_value(error.message)
            error.args = (error.message,)  # str(error) читает args, а не message
            error.details = self._scrub_value(error.details)
        return error

    def _scrub_value(self, value: Any) -> Any:
        if isinstance(value, str):
            return value.replace(self._secret, "***")
        if isinstance(value, list):
            return [self._scrub_value(item) for item in value]
        if isinstance(value, dict):
            return {self._scrub_value(k): self._scrub_value(v) for k, v in value.items()}
        return value

    def _parse(self, response: httpx.Response, method: str, path: str) -> Any:
        if response.status_code >= 400:
            raise self._scrub(self._error_from_response(response, method, path))
        if 300 <= response.status_code < 400:
            # Admin API не перенаправляет. Редирект = перед нами не трекер (страница входа, прокси),
            # и считать такой ответ успехом нельзя — особенно на DELETE/PUT с пустым телом.
            raise KeitaroProtocolError(
                f"Keitaro ответил перенаправлением ({response.status_code}) на {method} {path}. "
                "Проверьте KEITARO_BASE_URL: это должен быть адрес трекера, а не страницы входа.",
                upstream_status=response.status_code)
        if not response.content:
            return None
        try:
            return response.json()
        except ValueError as exc:
            raise KeitaroProtocolError(
                f"Keitaro вернул не JSON на {method} {path} (HTTP {response.status_code}). "
                "Проверьте KEITARO_BASE_URL: возможно, это не адрес трекера.",
                upstream_status=response.status_code,
            ) from exc

    @staticmethod
    def _error_from_response(response: httpx.Response, method: str, path: str) -> KeitaroError:
        status = response.status_code
        payload: Any = None
        try:
            payload = response.json()
        except ValueError:
            payload = None
        text = (response.text or "").strip()[:300]

        if isinstance(payload, dict) and payload.get("cloudflare_error"):
            return KeitaroForbiddenError(
                "Запрос заблокировал Cloudflare перед трекером "
                f"({payload.get('error_name') or payload.get('title') or 'access denied'}). "
                "Попробуйте другой KEITARO_USER_AGENT или добавьте IP сервера в allowlist.",
                upstream_status=status,
            )
        upstream_message = ""
        if isinstance(payload, dict) and isinstance(payload.get("error"), str):
            upstream_message = payload["error"]
        elif not isinstance(payload, (dict, list)):
            upstream_message = text

        if status == 401:
            return KeitaroAuthError(
                "Keitaro отклонил ключ API (401). Проверьте KEITARO_API_KEY.",
                upstream_status=status)
        if status == 402:
            return KeitaroForbiddenError(
                "Keitaro ответил 402: Admin API недоступно — лицензия не оплачена либо в этой "
                "редакции трекера API не предусмотрено.",
                upstream_status=status)
        if status == 403:
            return KeitaroForbiddenError(
                f"Keitaro запретил доступ (403): {upstream_message or 'нет прав у ключа API'}.",
                upstream_status=status)
        if status == 404:
            return KeitaroNotFoundError(
                f"В Keitaro не найдено: {upstream_message or path}.", upstream_status=status)
        if status in (400, 406, 422):
            fields = payload if isinstance(payload, dict) and "error" not in payload else None
            if fields:
                flat = "; ".join(
                    f"{name}: {', '.join(map(str, msgs)) if isinstance(msgs, list) else msgs}"
                    for name, msgs in fields.items()
                )
                message = f"Keitaro не принял данные: {flat}."
            else:
                message = f"Keitaro не принял данные: {upstream_message or text or status}."
            return KeitaroValidationError(message, upstream_status=status, details=fields)
        if status == 429:
            return KeitaroRateLimitError(
                "Keitaro просит сбавить темп (429). Повторите чуть позже.", upstream_status=status)
        if status >= 500:
            return KeitaroServerError(
                f"Keitaro ответил ошибкой сервера ({status}) на {method} {path}.",
                upstream_status=status)
        return KeitaroError(
            f"Неожиданный ответ Keitaro ({status}) на {method} {path}: {upstream_message}",
            upstream_status=status)

    @staticmethod
    def _expect_list(data: Any, what: str) -> list[dict[str, Any]]:
        if not isinstance(data, list):
            raise KeitaroProtocolError(f"Keitaro вернул {what} не списком.")
        return [row for row in data if isinstance(row, dict)]

    @staticmethod
    def _expect_dict(data: Any, what: str) -> dict[str, Any]:
        # Некоторые методы (clone, delete) возвращают список из одного объекта.
        if isinstance(data, list) and len(data) == 1 and isinstance(data[0], dict):
            return data[0]
        if not isinstance(data, dict):
            raise KeitaroProtocolError(f"Keitaro вернул {what} в неожиданном формате.")
        return data

    # ------------------------------------------------------------------ справочники

    async def ping(self) -> bool:
        """Проверка связи и ключа: самый лёгкий запрос."""
        await self._request("GET", "/groups", params={"type": "campaigns"})
        return True

    async def list_offers(self) -> list[dict[str, Any]]:
        return self._expect_list(await self._request("GET", "/offers"), "офферы")

    async def list_groups(self, group_type: str = "campaigns") -> list[dict[str, Any]]:
        data = await self._request("GET", "/groups", params={"type": group_type})
        return self._expect_list(data, "группы")

    async def list_traffic_sources(self) -> list[dict[str, Any]]:
        return self._expect_list(await self._request("GET", "/traffic_sources"), "источники")

    async def list_domains(self) -> list[dict[str, Any]]:
        return self._expect_list(await self._request("GET", "/domains"), "домены")

    # ------------------------------------------------------------------ кампании

    async def list_campaigns(self) -> list[dict[str, Any]]:
        """Все кампании, доступные ключу (постранично: limit/offset)."""
        unique: dict[int, dict[str, Any]] = {}
        for page in range(_MAX_PAGES):
            chunk = self._expect_list(
                await self._request(
                    "GET", "/campaigns", params={"limit": _PAGE_SIZE, "offset": page * _PAGE_SIZE}
                ),
                "кампании",
            )
            known = len(unique)
            for row in chunk:
                if isinstance(row.get("id"), int):
                    unique.setdefault(row["id"], row)
            # Старые версии Keitaro игнорируют limit и отдают всё сразу — тогда одной страницы
            # достаточно; повторный запрос вернул бы те же данные. Страница без единой новой
            # кампании значит, что трекер игнорирует и offset: дальше пойдут те же строки.
            if len(chunk) != _PAGE_SIZE or len(unique) == known:
                break
        return list(unique.values())

    async def get_campaign(self, campaign_id: int) -> dict[str, Any]:
        return self._expect_dict(await self._request("GET", f"/campaigns/{campaign_id}"), "кампанию")

    async def create_campaign(self, payload: dict[str, Any]) -> dict[str, Any]:
        return self._expect_dict(await self._request("POST", "/campaigns", json=payload), "кампанию")

    async def archive_campaign(self, campaign_id: int) -> None:
        """Удаление в Keitaro — это перенос в архив; оттуда кампанию можно восстановить.

        Повторное удаление уже удалённой кампании (404) считаем успехом: цель достигнута.
        """
        try:
            await self._request("DELETE", f"/campaigns/{campaign_id}")
        except KeitaroNotFoundError:
            logger.info("кампания %s уже отсутствует в Keitaro", campaign_id)

    # ------------------------------------------------------------------ потоки

    async def get_campaign_streams(self, campaign_id: int) -> list[dict[str, Any]]:
        data = await self._request("GET", f"/campaigns/{campaign_id}/streams")
        return self._expect_list(data, "потоки")

    async def get_stream(self, stream_id: int) -> dict[str, Any]:
        return self._expect_dict(await self._request("GET", f"/streams/{stream_id}"), "поток")

    async def create_stream(self, payload: dict[str, Any]) -> dict[str, Any]:
        return self._expect_dict(await self._request("POST", "/streams", json=payload), "поток")

    async def update_stream_offers(
        self, stream_id: int, offers: list[dict[str, Any]]
    ) -> dict[str, Any]:
        """Публикует набор офферов потока. Остальные поля потока Keitaro не трогает."""
        data = await self._request("PUT", f"/streams/{stream_id}", json={"offers": offers})
        return self._expect_dict(data, "поток")

    # ------------------------------------------------------------------ отчёты

    async def build_report(self, payload: dict[str, Any]) -> dict[str, Any]:
        # Отчёт — это чтение, хоть и методом POST: повторять безопасно.
        data = await self._request("POST", "/report/build", json=payload, idempotent=True)
        return self._expect_dict(data, "отчёт")
