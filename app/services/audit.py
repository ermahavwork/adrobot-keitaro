"""Журнал операций: каждая кнопка интерфейса оставляет запись «что, над чем, чем кончилось»."""

from __future__ import annotations

import logging
import time
from contextvars import ContextVar
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Operation

logger = logging.getLogger(__name__)

# Кто выполняет текущий запрос; выставляет middleware из заголовка X-AdRobot-User.
current_actor: ContextVar[str] = ContextVar("adrobot_actor", default="")


class Stopwatch:
    """Секундомер операции: `with Stopwatch() as sw: ...; sw.ms`."""

    def __enter__(self) -> Stopwatch:
        self._started = time.perf_counter()
        return self

    def __exit__(self, *_: object) -> None:
        pass

    @property
    def ms(self) -> int:
        return int((time.perf_counter() - self._started) * 1000)


async def record(
    session: AsyncSession,
    action: str,
    *,
    status: str = "ok",
    keitaro_campaign_id: int | None = None,
    keitaro_stream_id: int | None = None,
    summary: str = "",
    details: dict[str, Any] | None = None,
    error: str | None = None,
    duration_ms: int = 0,
) -> Operation:
    """Добавляет запись в журнал (без коммита — его делает вызывающий код)."""
    operation = Operation(
        action=action,
        actor=current_actor.get()[:64],
        status=status,
        keitaro_campaign_id=keitaro_campaign_id,
        keitaro_stream_id=keitaro_stream_id,
        summary=summary[:512],
        details=details or {},
        error=error,
        duration_ms=duration_ms,
    )
    session.add(operation)
    log = logger.info if status == "ok" else logger.warning
    log("операция %s [%s] кампания=%s поток=%s: %s", action, status, keitaro_campaign_id,
        keitaro_stream_id, summary or error or "")
    return operation


async def record_failure(session: AsyncSession, action: str, error: Exception, **kwargs: Any) -> None:
    """Откатывает незавершённую транзакцию и сохраняет в журнал запись об ошибке."""
    await session.rollback()
    await record(session, action, status="error", error=str(error)[:2000], **kwargs)
    await session.commit()
