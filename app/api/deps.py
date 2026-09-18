"""Зависимости FastAPI: доступ к сервисам из `app.state`, сессия БД, проверка токена."""

from __future__ import annotations

import hmac
from typing import Annotated

from fastapi import Depends, HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings, get_settings
from app.db import get_session
from app.keitaro.client import KeitaroClient
from app.services.audit import current_actor
from app.services.creator import CampaignCreator
from app.services.dictionaries import DictionaryService
from app.services.locks import CampaignLocks


def get_client(request: Request) -> KeitaroClient:
    return request.app.state.client


def get_dictionaries(request: Request) -> DictionaryService:
    return request.app.state.dictionaries


def get_creator(request: Request) -> CampaignCreator:
    return request.app.state.creator


def get_locks(request: Request) -> CampaignLocks:
    return request.app.state.locks


def get_app_settings(request: Request) -> Settings:
    return getattr(request.app.state, "settings", None) or get_settings()


# Схема безопасности, а не простой заголовок: так в Swagger UI (/docs) появляется кнопка
# Authorize, и «Try it out» действительно отправляет токен.
_bearer = HTTPBearer(auto_error=False, description="Токен из ADROBOT_AUTH_TOKEN или ADROBOT_AUTH_TOKENS")


async def require_token(
    request: Request,
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(_bearer)] = None,
) -> None:
    """Доступ к /api по токену: `Authorization: Bearer <токен>`.

    Два режима, можно оба сразу: общий `ADROBOT_AUTH_TOKEN` и именованные `ADROBOT_AUTH_TOKENS`
    (`имя:токен[:ro]`). У именованного токена имя становится автором в журнале операций, а
    пометка `ro` разрешает только чтение. Если ничего не задано — доступ открыт (localhost).
    """
    settings = get_app_settings(request)
    if not settings.auth_enabled:
        return
    supplied = (credentials.credentials if credentials else "").strip().encode()
    shared = settings.adrobot_auth_token.get_secret_value().encode()
    # Сравниваем со всеми токенами без раннего выхода: время ответа не выдаёт, какой «почти подошёл».
    matched: tuple[str, bool] | None = None
    if supplied and shared and hmac.compare_digest(supplied, shared):
        matched = ("", False)
    for name, token, read_only in settings.auth_tokens():
        if supplied and hmac.compare_digest(supplied, token.encode()) and matched is None:
            matched = (name, read_only)
    if matched is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Нужен токен доступа AdRobot.",
                            headers={"WWW-Authenticate": "Bearer"})
    name, read_only = matched
    request.state.token_name, request.state.read_only = name, read_only  # для GET /api/auth/me
    if name:
        current_actor.set(name)  # проверенное имя важнее подписи из X-AdRobot-User
    if read_only and request.method not in ("GET", "HEAD", "OPTIONS"):
        raise HTTPException(status.HTTP_403_FORBIDDEN,
                            "Этот токен даёт доступ только на чтение: изменения запрещены.")


SessionDep = Annotated[AsyncSession, Depends(get_session)]
ClientDep = Annotated[KeitaroClient, Depends(get_client)]
DictionariesDep = Annotated[DictionaryService, Depends(get_dictionaries)]
CreatorDep = Annotated[CampaignCreator, Depends(get_creator)]
LocksDep = Annotated[CampaignLocks, Depends(get_locks)]
SettingsDep = Annotated[Settings, Depends(get_app_settings)]
