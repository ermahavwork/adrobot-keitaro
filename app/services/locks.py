"""Замок на кампанию: правки одной кампании идут строго по очереди.

Два уровня:

1. `asyncio.Lock` внутри процесса — быстрый путь, снимает гонки двойного клика и двух вкладок;
2. строка в таблице `campaign_locks` — работает между процессами и серверами, поэтому
   AdRobot можно запускать в несколько воркеров (`uvicorn --workers N`) на одной базе.

Строка-замок живёт ограниченное время (`LOCK_TTL`): если процесс убили посреди операции,
замок не останется навсегда — следующий желающий увидит, что срок вышел, и заберёт его.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import logging
import uuid
from collections import defaultdict
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from sqlalchemy import delete
from sqlalchemy.exc import IntegrityError, OperationalError

from app import db
from app.models import CampaignLock, utcnow

logger = logging.getLogger(__name__)

# Самая долгая операция — Push: два чтения справочника офферов, GET и PUT, каждый с повторами
# и паузами (в худшем случае ~5–6 минут). TTL обязан быть больше, иначе второй воркер войдёт,
# пока первый ещё работает.
LOCK_TTL = dt.timedelta(minutes=10)
ACQUIRE_TIMEOUT_SECONDS = 20.0
_POLL_SECONDS = 0.15


class LockBusyError(Exception):
    """Кампанию прямо сейчас правит кто-то другой, и дождаться своей очереди не удалось."""

    code = "campaign_busy"
    http_status = 409
    message = ("Эту кампанию прямо сейчас изменяет другой пользователь или процесс. "
               "Повторите через несколько секунд.")


class CampaignLocks:
    """Выдаёт замки по ID кампании. Один экземпляр на приложение."""

    def __init__(self, *, acquire_timeout: float = ACQUIRE_TIMEOUT_SECONDS) -> None:
        self._local: dict[int, asyncio.Lock] = {}
        # Сколько запросов держат замок кампании или стоят за ним в очереди. Когда не остаётся
        # никого, замок выбрасываем: иначе словарь рос бы на запись с каждой открытой кампанией.
        self._users: defaultdict[int, int] = defaultdict(int)
        self._acquire_timeout = acquire_timeout

    @asynccontextmanager
    async def for_campaign(self, campaign_id: int) -> AsyncIterator[None]:
        lock = self._local.setdefault(campaign_id, asyncio.Lock())
        self._users[campaign_id] += 1
        try:
            async with lock:
                owner = uuid.uuid4().hex
                await self._acquire(campaign_id, owner)
                try:
                    yield
                finally:
                    # shield: запрос могли отменить (клиент закрыл вкладку) — замок всё равно надо
                    # снять, иначе строка провисит до истечения TTL.
                    await asyncio.shield(self._release(campaign_id, owner))
        finally:
            # Между проверкой счётчика и удалением нет ни одного await — гонки быть не может.
            self._users[campaign_id] -= 1
            if not self._users[campaign_id]:
                del self._users[campaign_id]
                self._local.pop(campaign_id, None)

    async def _acquire(self, campaign_id: int, owner: str) -> None:
        deadline = asyncio.get_running_loop().time() + self._acquire_timeout
        sessionmaker = db.get_sessionmaker()
        while True:
            async with sessionmaker() as session:
                try:
                    # Просроченный замок считается брошенным: убираем и занимаем сами.
                    await session.execute(delete(CampaignLock).where(
                        CampaignLock.campaign_id == campaign_id, CampaignLock.expires_at < utcnow()))
                    session.add(CampaignLock(campaign_id=campaign_id, owner=owner,
                                             expires_at=utcnow() + LOCK_TTL))
                    await session.commit()
                    return
                except IntegrityError:
                    await session.rollback()  # замок занят другим процессом — ждём
                except OperationalError as exc:
                    await session.rollback()
                    text = str(exc.orig).lower()
                    if "no such table" in text or "does not exist" in text:
                        raise  # миграции не применены: ждать бессмысленно, пусть ответит подсказка
                    # иначе база просто занята (SQLite) — это тоже «подождать»
            if asyncio.get_running_loop().time() >= deadline:
                raise LockBusyError
            await asyncio.sleep(_POLL_SECONDS)

    @staticmethod
    async def _release(campaign_id: int, owner: str) -> None:
        for attempt in range(3):
            try:
                async with db.get_sessionmaker()() as session:
                    await session.execute(delete(CampaignLock).where(
                        CampaignLock.campaign_id == campaign_id, CampaignLock.owner == owner))
                    await session.commit()
                return
            except Exception:  # база могла быть занята — пробуем ещё; в крайнем случае истечёт TTL
                if attempt == 2:
                    logger.warning("не удалось снять замок кампании %s", campaign_id, exc_info=True)
                await asyncio.sleep(0.2)
