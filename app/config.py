"""Настройки приложения.

Все значения читаются из переменных окружения или файла `.env` (см. `.env.example`).
Секреты (API-ключ Keitaro, токен доступа к самому AdRobot) живут только здесь:
в базу данных, в логи и во фронтенд они не попадают.
"""

from __future__ import annotations

from functools import lru_cache
from urllib.parse import urlsplit

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Конфигурация AdRobot. Имена переменных окружения совпадают с именами полей."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8-sig",  # PowerShell 5 пишет .env с BOM — иначе первый ключ теряется
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
    # Несколько именованных токенов: `имя:токен` через запятую, суффикс `:ro` — только чтение.
    # Пример: `andrey:длинный-токен,viewer:другой-токен:ro`. Имя попадает в журнал операций
    # как ПРОВЕРЕННЫЙ автор (в отличие от подписи из заголовка X-AdRobot-User).
    adrobot_auth_tokens: SecretStr = Field(default=SecretStr(""))
    dictionary_ttl_seconds: int = 300
    snapshots_per_stream: int = Field(default=30, ge=1, le=1000)
    idempotency_ttl_days: int = Field(default=7, ge=1, le=365)
    log_level: str = "INFO"
    # Если AdRobot стоит за обратным прокси в подкаталоге (https://host/adrobot/), а прокси
    # отрезает этот префикс, укажите его здесь — тогда Swagger UI шлёт запросы на верные адреса.
    # Самому интерфейсу настройка не нужна: он работает из любого каталога.
    root_path: str = ""
    # Имена хостов, на которые AdRobot отвечает (через запятую; пусто = любые). Стоит задать,
    # если приложение доступно по сети: закрывает DNS-rebinding. Пример: adrobot.example.com,localhost
    allowed_hosts: str = ""

    @field_validator("keitaro_base_url", "tracking_domain_url")
    @classmethod
    def _strip_url(cls, value: str) -> str:
        value = "".join((value or "").split())  # пробелов в адресе не бывает — только опечатки
        if not value:
            return ""
        if "://" not in value:
            value = "https://" + value.lstrip("/")
        try:
            parts = urlsplit(value)  # схему приводит к нижнему регистру сам
        except ValueError:
            return value.rstrip("/")
        if not parts.netloc:
            return ""
        scheme = parts.scheme if parts.scheme in ("http", "https") else "https"
        # Частая ошибка: вставляют адрес админки целиком, прямо из строки браузера
        # (…/admin/#!/campaigns/123, …/admin/?object=…). Всё, начиная с сегмента /admin или
        # /admin_api, а также query и #-маршрут к адресу трекера не относятся.
        # Нестандартный базовый путь (https://host/kt) сохраняется.
        segments = parts.path.split("/")
        for index, segment in enumerate(segments):
            if segment in ("admin", "admin_api"):
                segments = segments[:index]
                break
        return f"{scheme}://{parts.netloc}{'/'.join(segments).rstrip('/')}"

    @field_validator("default_domain_id", "default_group_id", "default_traffic_source_id",
                     mode="before")
    @classmethod
    def _empty_to_none(cls, value: object) -> object:
        if isinstance(value, str) and not value.strip():
            return None
        return value

    @field_validator("adrobot_auth_tokens")
    @classmethod
    def _tokens_must_be_valid(cls, value: SecretStr) -> SecretStr:
        """Кривая запись = отказ стартовать. Молча пропустить её нельзя: если пропущенной окажется
        единственная запись, защита выключится, и API останется открытым без всякого токена."""
        cls._parse_tokens(value.get_secret_value())
        return value

    @staticmethod
    def _parse_tokens(raw: str) -> list[tuple[str, str, bool]]:
        result = []
        for index, chunk in enumerate(filter(None, (c.strip() for c in raw.split(","))), start=1):
            name, _, rest = chunk.partition(":")
            read_only = rest.endswith(":ro")
            token = rest[:-3] if read_only else rest  # сам токен может содержать двоеточия
            if not name.strip() or len(token) < 8:
                raise ValueError(
                    f"ADROBOT_AUTH_TOKENS: запись №{index} должна иметь вид имя:токен[:ro], "
                    "токен — не короче 8 символов")
            result.append((name.strip()[:64], token, read_only))
        return result

    def auth_tokens(self) -> list[tuple[str, str, bool]]:
        """Разобранный ADROBOT_AUTH_TOKENS: [(имя, токен, только_чтение)]."""
        return self._parse_tokens(self.adrobot_auth_tokens.get_secret_value())

    @property
    def auth_enabled(self) -> bool:
        return bool(self.adrobot_auth_token.get_secret_value() or self.auth_tokens())

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
