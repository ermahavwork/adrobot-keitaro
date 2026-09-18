"""Настройки приложения.

Все значения читаются из переменных окружения или файла `.env` (см. `.env.example`).
Секреты (API-ключ Keitaro, токен доступа к самому AdRobot) живут только здесь:
в базу данных, в логи и во фронтенд они не попадают.
"""

from __future__ import annotations

from functools import lru_cache

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Конфигурация AdRobot. Имена переменных окружения совпадают с именами полей."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # --- Keitaro ---
    keitaro_base_url: str = Field(
        default="",
        description="Адрес трекера без /admin_api, например https://tracker.example.com",
    )
    keitaro_api_key: SecretStr = Field(default=SecretStr(""), description="Ключ Admin API")
    keitaro_timeout_seconds: float = 20.0
    keitaro_max_retries: int = 3
    keitaro_max_concurrency: int = 4
    # Cloudflare перед трекером может банить «питоновские» User-Agent (ошибка 1010).
    keitaro_user_agent: str = "AdRobot-Keitaro/1.0"

    # --- значения по умолчанию для «создаватора» (пусто = определить автоматически) ---
    default_domain_id: int | None = None
    default_group_id: int | None = None
    default_traffic_source_id: int | None = None
    default_redirect_url: str = "https://google.com"
    default_geo_stream_name: str = "Flow 1"
    default_offer_stream_name: str = "Flow 2"
    # Публичный адрес трекинг-домена; нужен только чтобы показать готовую ссылку кампании,
    # когда ключу API не виден справочник доменов.
    tracking_domain_url: str = ""

    # --- приложение ---
    database_url: str = "sqlite+aiosqlite:///./data/adrobot.sqlite3"
    # Если задан — все запросы к /api требуют заголовок `Authorization: Bearer <токен>`.
    adrobot_auth_token: SecretStr = Field(default=SecretStr(""))
    dictionary_ttl_seconds: int = 300
    log_level: str = "INFO"
    environment: str = "development"

    @field_validator("keitaro_base_url", "tracking_domain_url")
    @classmethod
    def _strip_url(cls, value: str) -> str:
        value = (value or "").strip().rstrip("/")
        # Частая ошибка: вставляют адрес админки целиком.
        for suffix in ("/admin_api/v1", "/admin_api", "/admin/#!", "/admin"):
            if value.endswith(suffix):
                value = value[: -len(suffix)]
        if value and not value.startswith(("http://", "https://")):
            value = "https://" + value
        return value.rstrip("/")

    @field_validator("default_domain_id", "default_group_id", "default_traffic_source_id",
                     mode="before")
    @classmethod
    def _empty_to_none(cls, value: object) -> object:
        if isinstance(value, str) and not value.strip():
            return None
        return value

    @property
    def keitaro_configured(self) -> bool:
        return bool(self.keitaro_base_url and self.keitaro_api_key.get_secret_value())

    @property
    def admin_url(self) -> str:
        return f"{self.keitaro_base_url}/admin/" if self.keitaro_base_url else ""

    def campaign_admin_url(self, keitaro_campaign_id: int) -> str:
        """Ссылка «View in KT» на кампанию в админке Keitaro."""
        if not self.keitaro_base_url:
            return ""
        return f"{self.keitaro_base_url}/admin/#!/campaigns/{keitaro_campaign_id}"


@lru_cache
def get_settings() -> Settings:
    return Settings()
