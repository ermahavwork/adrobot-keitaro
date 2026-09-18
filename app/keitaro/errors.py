"""Ошибки общения с Keitaro. У каждой есть понятное человеку сообщение и HTTP-код для ответа."""

from __future__ import annotations

from typing import Any


class KeitaroError(Exception):
    """Базовая ошибка клиента Keitaro."""

    http_status = 502  # каким кодом AdRobot ответит своему фронтенду
    code = "keitaro_error"

    def __init__(
        self,
        message: str,
        *,
        upstream_status: int | None = None,
        details: Any = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.upstream_status = upstream_status
        self.details = details

    def to_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "message": self.message,
            "upstream_status": self.upstream_status,
            "details": self.details,
        }


class KeitaroNotConfiguredError(KeitaroError):
    http_status = 503
    code = "keitaro_not_configured"


class KeitaroAuthError(KeitaroError):
    """401: ключ API неверный или отозван."""

    http_status = 502
    code = "keitaro_unauthorized"


class KeitaroForbiddenError(KeitaroError):
    """403: у ключа нет прав на раздел либо запрос отбил Cloudflare."""

    http_status = 502
    code = "keitaro_forbidden"


class KeitaroNotFoundError(KeitaroError):
    http_status = 404
    code = "keitaro_not_found"


class KeitaroValidationError(KeitaroError):
    """422: Keitaro отверг данные; `details` — словарь {поле: [сообщения]}."""

    http_status = 422
    code = "keitaro_validation"


class KeitaroRateLimitError(KeitaroError):
    http_status = 503
    code = "keitaro_rate_limited"


class KeitaroServerError(KeitaroError):
    """5xx от Keitaro или от прокси перед ним."""

    http_status = 502
    code = "keitaro_server_error"


class KeitaroNetworkError(KeitaroError):
    """Таймаут, обрыв соединения, DNS."""

    http_status = 504
    code = "keitaro_unreachable"


class KeitaroProtocolError(KeitaroError):
    """Ответ пришёл, но это не тот JSON, который обещает документация."""

    http_status = 502
    code = "keitaro_bad_response"
