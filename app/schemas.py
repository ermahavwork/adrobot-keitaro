"""Схемы запросов API (Pydantic). Ответы отдаются словарями из сервисного слоя."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class CampaignCreateRequest(BaseModel):
    """Форма «New campaign»: имя + гео + оффер. Остальное — необязательные уточнения."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    name: str = Field(min_length=1, max_length=200, description="Название кампании")
    geo: str | list[str] = Field(
        description="Код(ы) стран ISO 3166-1 alpha-2: `AU` или `MX,AU,RO`; понимает и названия"
    )
    offer_id: int | None = Field(default=None, gt=0, description="ID оффера Keitaro")
    offer_ids: list[int] = Field(
        default_factory=list, max_length=50,
        description="Несколько офферов во второй поток — доли поделятся поровну",
    )
    domain_id: int | None = Field(default=None, gt=0)
    group_id: int | None = Field(default=None, gt=0)
    traffic_source_id: int | None = Field(default=None, gt=0)
    redirect_url: str | None = Field(default=None, max_length=500)
    alias: str | None = Field(default=None, max_length=64, pattern=r"^[A-Za-z0-9_-]+$")
    split_by_geo: bool = Field(
        default=False, description="Отдельная кампания на каждую страну (имя + [ГЕО])"
    )
    dry_run: bool = Field(default=False, description="Ничего не создавать, показать план запросов")
    allow_duplicate_name: bool = False

    @field_validator("offer_ids")
    @classmethod
    def _positive_ids(cls, value: list[int]) -> list[int]:
        if any(i <= 0 for i in value):
            raise ValueError("ID оффера должен быть положительным числом")
        return value

    @model_validator(mode="after")
    def _collect_offers(self) -> CampaignCreateRequest:
        ids = ([self.offer_id] if self.offer_id else []) + self.offer_ids
        unique = list(dict.fromkeys(ids))
        if not unique:
            raise ValueError("Укажите хотя бы один оффер (offer_id)")
        self.offer_ids = unique
        return self


class AddOfferRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    offer_id: int = Field(gt=0)


class PinRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    pinned: bool


class ShareRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    share: int = Field(ge=1, le=100)


class PushRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    force: bool = Field(default=False, description="Опубликовать поверх правок, сделанных в Keitaro")
    allow_empty: bool = Field(default=False, description="Разрешить поток без активных офферов")


class FetchRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    discard_draft: bool = Field(
        default=False, description="Выбросить неопубликованные изменения и взять состояние Keitaro"
    )


class RecalculateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    drop_pins: bool = Field(default=False, description="Снять закрепления и поделить поровну")


class SettingsUpdateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    default_domain_id: int | None = Field(default=None, gt=0)
    default_group_id: int | None = Field(default=None, gt=0)
    default_traffic_source_id: int | None = Field(default=None, gt=0)
    default_redirect_url: str | None = Field(default=None, max_length=500)
    tracking_domain_url: str | None = Field(default=None, max_length=300)
