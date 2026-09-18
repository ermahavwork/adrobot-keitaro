"""Подключение к базе данных (SQLAlchemy 2.0, асинхронный движок).

По умолчанию — SQLite в файле `data/adrobot.sqlite3` (ничего ставить не нужно).
Для PostgreSQL достаточно поменять `DATABASE_URL` на `postgresql+asyncpg://...`.
Схему создают миграции Alembic (`alembic upgrade head`), а не `create_all`.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path

from sqlalchemy import event
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import DeclarativeBase

from app.config import get_settings


class Base(DeclarativeBase):
    """Базовый класс моделей."""


def _ensure_sqlite_dir(database_url: str) -> None:
    url = make_url(database_url)
    if url.get_backend_name() == "sqlite" and url.database and url.database != ":memory:":
        Path(url.database).expanduser().resolve().parent.mkdir(parents=True, exist_ok=True)


def create_engine(database_url: str | None = None) -> AsyncEngine:
    """Создаёт движок. Для SQLite включает внешние ключи, WAL и ожидание блокировки."""
    database_url = database_url or get_settings().database_url
    _ensure_sqlite_dir(database_url)
    engine = create_async_engine(database_url, future=True)

    if engine.dialect.name == "sqlite":

        @event.listens_for(engine.sync_engine, "connect")
        def _sqlite_pragmas(dbapi_connection, _record):  # type: ignore[no-untyped-def]
            cursor = dbapi_connection.cursor()
            cursor.execute("PRAGMA foreign_keys=ON")
            cursor.execute("PRAGMA journal_mode=WAL")
            cursor.execute("PRAGMA busy_timeout=5000")
            cursor.close()

    return engine


_engine: AsyncEngine | None = None
_sessionmaker: async_sessionmaker[AsyncSession] | None = None


def get_engine() -> AsyncEngine:
    global _engine
    if _engine is None:
        _engine = create_engine()
    return _engine


def get_sessionmaker() -> async_sessionmaker[AsyncSession]:
    global _sessionmaker
    if _sessionmaker is None:
        _sessionmaker = async_sessionmaker(get_engine(), expire_on_commit=False)
    return _sessionmaker


def override_engine(engine: AsyncEngine | None) -> None:
    """Подмена движка (тесты) или сброс при `None`."""
    global _engine, _sessionmaker
    _engine = engine
    _sessionmaker = async_sessionmaker(engine, expire_on_commit=False) if engine else None


async def get_session() -> AsyncIterator[AsyncSession]:
    """Зависимость FastAPI: одна сессия на запрос; коммит делает сервисный слой."""
    async with get_sessionmaker()() as session:
        yield session
