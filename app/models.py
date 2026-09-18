"""Модели базы данных.

AdRobot хранит у себя «зеркало» кампаний и потоков Keitaro плюс то, чего в Keitaro нет:
черновик изменений (до нажатия Push), архив удалённых офферов (для Bring back),
закреплённые доли (pin), снимки для отката и журнал операций.

Главная таблица логики — `stream_offers` (привязка оффера к потоку). У неё две пары полей:

* `state` / `share`       — черновик: то, что видит пользователь в AdRobot;
* `kt_state` / `kt_share` — то, что сейчас опубликовано в Keitaro (по последней синхронизации).

Поток считается «жёлтым» (есть неопубликованные изменения), когда эти пары расходятся.
"""

from __future__ import annotations

import datetime as dt
from typing import Any

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship
from sqlalchemy.types import TypeDecorator

from app.db import Base

# --- состояния привязки оффера к потоку ---
STATE_ACTIVE = "active"  # участвует в ротации и в пересчёте долей
STATE_DISABLED = "disabled"  # выключен в Keitaro; передаём как есть, в пересчёте не участвует
STATE_REMOVED = "removed"  # удалён из потока, лежит в архиве AdRobot (Bring back)


def utcnow() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


class UTCDateTime(TypeDecorator):
    """DateTime, который всегда возвращает aware-время в UTC (SQLite теряет таймзону)."""

    impl = DateTime(timezone=True)
    cache_ok = True

    def process_bind_param(self, value: dt.datetime | None, dialect: Any) -> dt.datetime | None:
        if value is not None and value.tzinfo is None:
            value = value.replace(tzinfo=dt.timezone.utc)
        return value

    def process_result_value(self, value: dt.datetime | None, dialect: Any) -> dt.datetime | None:
        if value is not None and value.tzinfo is None:
            value = value.replace(tzinfo=dt.timezone.utc)
        return value


class Campaign(Base):
    """Кампания Keitaro, известная AdRobot (создана у нас или импортирована из трекера)."""

    __tablename__ = "campaigns"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    keitaro_id: Mapped[int] = mapped_column(Integer, unique=True, index=True)
    name: Mapped[str] = mapped_column(String(255))
    alias: Mapped[str] = mapped_column(String(255), default="")
    state: Mapped[str] = mapped_column(String(32), default="active")
    # Гео, с которым кампанию создал «создаватор» (для импортированных — пусто).
    geo: Mapped[list[str]] = mapped_column(JSON, default=list)
    group_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    traffic_source_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    domain_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    origin: Mapped[str] = mapped_column(String(16), default="keitaro")  # adrobot | keitaro
    is_deleted: Mapped[bool] = mapped_column(Boolean, default=False)  # исчезла из Keitaro
    created_at: Mapped[dt.datetime] = mapped_column(UTCDateTime, default=utcnow)
    updated_at: Mapped[dt.datetime] = mapped_column(UTCDateTime, default=utcnow, onupdate=utcnow)
    streams_fetched_at: Mapped[dt.datetime | None] = mapped_column(UTCDateTime, nullable=True)

    streams: Mapped[list[Stream]] = relationship(
        back_populates="campaign", cascade="all, delete-orphan", order_by="Stream.position"
    )


class Stream(Base):
    """Поток кампании. Содержимое потоков без офферов не разбирается — только шапка."""

    __tablename__ = "streams"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    campaign_id: Mapped[int] = mapped_column(ForeignKey("campaigns.id", ondelete="CASCADE"))
    keitaro_id: Mapped[int] = mapped_column(Integer, unique=True, index=True)
    name: Mapped[str] = mapped_column(String(255), default="")
    position: Mapped[int] = mapped_column(Integer, default=0)
    type: Mapped[str] = mapped_column(String(32), default="regular")
    schema: Mapped[str] = mapped_column(String(32), default="")
    state: Mapped[str] = mapped_column(String(32), default="active")
    action_type: Mapped[str] = mapped_column(String(64), default="")
    # Короткая справка «куда ведёт / какие фильтры» — только для показа, логика её не читает.
    summary: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    is_deleted: Mapped[bool] = mapped_column(Boolean, default=False)  # исчез из Keitaro
    fetched_at: Mapped[dt.datetime | None] = mapped_column(UTCDateTime, nullable=True)

    campaign: Mapped[Campaign] = relationship(back_populates="streams")
    bindings: Mapped[list[StreamOffer]] = relationship(
        back_populates="stream", cascade="all, delete-orphan", order_by="StreamOffer.sort_index"
    )
    snapshots: Mapped[list[StreamSnapshot]] = relationship(
        back_populates="stream", cascade="all, delete-orphan"
    )


class Offer(Base):
    """Кэш справочника офферов Keitaro (для автокомплита и показа названий)."""

    __tablename__ = "offers"

    keitaro_id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=False)
    name: Mapped[str] = mapped_column(String(512), default="")
    state: Mapped[str] = mapped_column(String(32), default="active")
    group_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    affiliate_network_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    country: Mapped[list[str]] = mapped_column(JSON, default=list)
    offer_type: Mapped[str] = mapped_column(String(32), default="")
    payout_value: Mapped[str] = mapped_column(String(32), default="")
    payout_currency: Mapped[str] = mapped_column(String(8), default="")
    # Оффер был в справочнике, но в последней выдаче Keitaro его уже нет.
    is_missing: Mapped[bool] = mapped_column(Boolean, default=False)
    fetched_at: Mapped[dt.datetime] = mapped_column(UTCDateTime, default=utcnow)


class StreamOffer(Base):
    """Привязка оффера к потоку: черновик + опубликованное состояние + pin."""

    __tablename__ = "stream_offers"
    __table_args__ = (
        UniqueConstraint("stream_id", "offer_id", name="uq_stream_offer"),
        Index("ix_stream_offers_stream_state", "stream_id", "state"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    stream_id: Mapped[int] = mapped_column(ForeignKey("streams.id", ondelete="CASCADE"))
    offer_id: Mapped[int] = mapped_column(Integer, index=True)  # ID оффера в Keitaro

    # черновик (то, что показывает интерфейс)
    state: Mapped[str] = mapped_column(String(16), default=STATE_ACTIVE)
    share: Mapped[int] = mapped_column(Integer, default=0)
    is_pinned: Mapped[bool] = mapped_column(Boolean, default=False)

    # опубликовано в Keitaro (по последней синхронизации); None = в Keitaro этой привязки нет
    kt_state: Mapped[str | None] = mapped_column(String(16), nullable=True)
    kt_share: Mapped[int | None] = mapped_column(Integer, nullable=True)
    kt_binding_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # Была ли привязка когда-нибудь в Keitaro. Cancel удаляет только те, что не были.
    was_published: Mapped[bool] = mapped_column(Boolean, default=False)

    # Порядок «активации»: новый и возвращённый (Bring back) оффер встаёт в конец.
    # Остаток от деления долей получает последний незакреплённый — как в оригинале.
    sort_index: Mapped[int] = mapped_column(Integer, default=0)

    created_at: Mapped[dt.datetime] = mapped_column(UTCDateTime, default=utcnow)
    updated_at: Mapped[dt.datetime] = mapped_column(UTCDateTime, default=utcnow, onupdate=utcnow)
    removed_at: Mapped[dt.datetime | None] = mapped_column(UTCDateTime, nullable=True)

    stream: Mapped[Stream] = relationship(back_populates="bindings")

    @property
    def is_dirty(self) -> bool:
        """Отличается ли черновик от опубликованного в Keitaro."""
        in_keitaro = self.kt_state is not None
        if self.state == STATE_REMOVED:
            return in_keitaro
        if not in_keitaro:
            return True
        return self.state != self.kt_state or self.share != (self.kt_share or 0)


class StreamSnapshot(Base):
    """Снимок офферов потока в Keitaro перед публикацией — основа для отката."""

    __tablename__ = "stream_snapshots"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    stream_id: Mapped[int] = mapped_column(ForeignKey("streams.id", ondelete="CASCADE"), index=True)
    reason: Mapped[str] = mapped_column(String(32), default="pre_push")
    offers: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list)
    created_at: Mapped[dt.datetime] = mapped_column(UTCDateTime, default=utcnow)

    stream: Mapped[Stream] = relationship(back_populates="snapshots")


class Operation(Base):
    """Журнал операций: кто что сделал, чем закончилось, сколько заняло."""

    __tablename__ = "operations"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    created_at: Mapped[dt.datetime] = mapped_column(UTCDateTime, default=utcnow, index=True)
    action: Mapped[str] = mapped_column(String(48), index=True)
    # Кто нажал кнопку: имя из заголовка X-AdRobot-User (журнал Keitaro видит только API-ключ).
    actor: Mapped[str] = mapped_column(String(64), default="")
    status: Mapped[str] = mapped_column(String(16), default="ok")  # ok | error
    keitaro_campaign_id: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
    keitaro_stream_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    summary: Mapped[str] = mapped_column(String(512), default="")
    details: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    duration_ms: Mapped[int] = mapped_column(Integer, default=0)


class IdempotencyRecord(Base):
    """Защита от дублей: повтор запроса с тем же ключом возвращает прежний результат."""

    __tablename__ = "idempotency_records"

    key: Mapped[str] = mapped_column(String(128), primary_key=True)
    request_hash: Mapped[str] = mapped_column(String(64))
    response: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    created_at: Mapped[dt.datetime] = mapped_column(UTCDateTime, default=utcnow)


class AppSetting(Base):
    """Настройки, которые меняются из интерфейса (значения по умолчанию «создаватора»)."""

    __tablename__ = "app_settings"

    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[Any] = mapped_column(JSON, nullable=True)
    updated_at: Mapped[dt.datetime] = mapped_column(UTCDateTime, default=utcnow, onupdate=utcnow)
