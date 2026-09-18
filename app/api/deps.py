"""Зависимости FastAPI: доступ к сервисам из `app.state`, сессия БД, проверка токена."""

from __future__ import annotations

import hmac
from typing import Annotated

from fastapi import Depends, Header, HTTPException, Request, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings, get_settings
from app.db import get_session
from app.keitaro.client import KeitaroClient
from app.services.creator import CampaignCreator
from app.services.dictionaries import DictionaryService
from app.services.editor import StreamLocks


def get_client(request: Request) -> KeitaroClient:
    return request.app.state.client


def get_dictionaries(request: Request) -> DictionaryService:
    return request.app.state.dictionaries


def get_creator(request: Request) -> CampaignCreator:
    return request.app.state.creator


def get_locks(request: Request) -> StreamLocks:
    return request.app.state.locks


def get_app_settings(request: Request) -> Settings:
    return getattr(request.app.state, "settings", None) or get_settings()


async def require_token(
    request: Request, authorization: Annotated[str | None, Header()] = None
) -> None:
    """Если задан ADROBOT_AUTH_TOKEN — пускаем только с `Authorization: Bearer <токен>`."""
    expected = get_app_settings(request).adrobot_auth_token.get_secret_value()
    if not expected:
        return
    supplied = (authorization or "").removeprefix("Bearer ").strip()
    if not supplied or not hmac.compare_digest(supplied.encode(), expected.encode()):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Нужен токен доступа AdRobot.",
                            headers={"WWW-Authenticate": "Bearer"})


SessionDep = Annotated[AsyncSession, Depends(get_session)]
ClientDep = Annotated[KeitaroClient, Depends(get_client)]
DictionariesDep = Annotated[DictionaryService, Depends(get_dictionaries)]
CreatorDep = Annotated[CampaignCreator, Depends(get_creator)]
LocksDep = Annotated[StreamLocks, Depends(get_locks)]
SettingsDep = Annotated[Settings, Depends(get_app_settings)]
