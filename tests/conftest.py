"""Общие фикстуры: приложение на временной SQLite + эмулятор Keitaro вместо сети."""

from __future__ import annotations

import asyncio
from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr

from app import db
from app.config import Settings
from app.main import create_app
from tests.fake_keitaro import API_KEY, FakeKeitaro


def make_settings(tmp_path: Path, **overrides) -> Settings:
    values = {
        "keitaro_base_url": "https://tracker.test",
        "keitaro_api_key": SecretStr(API_KEY),
        "adrobot_auth_token": SecretStr(""),
        "database_url": f"sqlite+aiosqlite:///{tmp_path / 'test.sqlite3'}",
        "keitaro_max_retries": 2,
        "dictionary_ttl_seconds": 0,
        "tracking_domain_url": "https://in.example.test",
        "default_domain_id": None,
        "default_group_id": None,
        "default_traffic_source_id": None,
        "log_level": "WARNING",
    }
    values.update(overrides)
    return Settings(_env_file=None, **values)


def _create_schema(database_url: str) -> None:
    async def run() -> None:
        engine = db.create_engine(database_url)
        async with engine.begin() as connection:
            await connection.run_sync(db.Base.metadata.create_all)
        await engine.dispose()

    asyncio.run(run())


@pytest.fixture(autouse=True)
def _no_real_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Тесты не должны зависеть от переменных окружения разработчика (и его ключей)."""
    for name in ("KEITARO_BASE_URL", "KEITARO_API_KEY", "ADROBOT_AUTH_TOKEN", "DATABASE_URL",
                 "DEFAULT_DOMAIN_ID", "DEFAULT_GROUP_ID", "DEFAULT_TRAFFIC_SOURCE_ID"):
        monkeypatch.delenv(name, raising=False)


@pytest.fixture
def fake() -> FakeKeitaro:
    return FakeKeitaro()


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return make_settings(tmp_path)


@pytest.fixture
def client(fake: FakeKeitaro, settings: Settings) -> Iterator[TestClient]:
    """HTTP-клиент к приложению; Keitaro подменён эмулятором `fake`."""
    _create_schema(settings.database_url)
    db.override_engine(db.create_engine(settings.database_url))
    app = create_app(settings, transport=fake.transport())
    with TestClient(app) as test_client:
        yield test_client
    engine = db.get_engine()
    db.override_engine(None)
    asyncio.run(engine.dispose())


class Editor:
    """Помощник сценарных тестов: открывает кампанию и жмёт кнопки редактора через API."""

    def __init__(self, client: TestClient, fake: FakeKeitaro, keitaro_campaign_id: int,
                 keitaro_stream_id: int) -> None:
        self.client, self.fake = client, fake
        self.kt_stream_id = keitaro_stream_id
        opened = client.post(f"/api/campaigns/open/{keitaro_campaign_id}")
        assert opened.status_code == 200, opened.text
        self.campaign_id = opened.json()["id"]
        self.fetch()

    # --- кнопки
    def fetch(self, **body) -> dict:
        response = self.client.post(f"/api/campaigns/{self.campaign_id}/fetch", json=body)
        assert response.status_code == 200, response.text
        return response.json()

    @property
    def stream(self) -> dict:
        campaign = self.client.get(f"/api/campaigns/{self.campaign_id}").json()
        return next(s for s in campaign["streams"] if s["keitaro_id"] == self.kt_stream_id)

    def _binding_id(self, offer_id: int) -> int:
        return next(o["id"] for o in self.stream["offers"] if o["offer_id"] == offer_id)

    def _url(self, tail: str = "") -> str:
        return f"/api/streams/{self.stream['id']}{tail}"

    def _call(self, method: str, tail: str, expect: int, body: dict | None = None) -> dict:
        response = self.client.request(method, self._url(tail), json=body)
        assert response.status_code == expect, response.text
        return response.json()

    def add(self, offer_id: int, expect: int = 200) -> dict:
        return self._call("POST", "/offers", expect, {"offer_id": offer_id})

    def remove(self, offer_id: int, expect: int = 200) -> dict:
        return self._call("DELETE", f"/offers/{self._binding_id(offer_id)}", expect)

    def bring_back(self, offer_id: int, expect: int = 200) -> dict:
        return self._call("POST", f"/offers/{self._binding_id(offer_id)}/bring-back", expect)

    def pin(self, offer_id: int, pinned: bool = True, expect: int = 200) -> dict:
        return self._call("PUT", f"/offers/{self._binding_id(offer_id)}/pin", expect,
                          {"pinned": pinned})

    def share(self, offer_id: int, value: int, expect: int = 200) -> dict:
        return self._call("PUT", f"/offers/{self._binding_id(offer_id)}/share", expect,
                          {"share": value})

    def push(self, expect: int = 200, **body) -> dict:
        return self._call("POST", "/push", expect, body)

    def cancel(self, expect: int = 200) -> dict:
        return self._call("POST", "/cancel", expect)

    # --- что видно
    def shares(self) -> dict[int, int]:
        """{offer_id: доля} активных офферов в интерфейсе AdRobot."""
        return {o["offer_id"]: o["share"] for o in self.stream["offers"] if o["state"] == "active"}

    def removed(self) -> list[int]:
        return [o["offer_id"] for o in self.stream["offers"] if o["state"] == "removed"]

    def order(self) -> list[int]:
        return [o["offer_id"] for o in self.stream["offers"]]

    def in_keitaro(self) -> dict[int, int]:
        """{offer_id: доля} — что реально лежит в (эмулированном) Keitaro."""
        return dict(self.fake.stream_offers(self.kt_stream_id))


@pytest.fixture
def editor(client: TestClient, fake: FakeKeitaro) -> Editor:
    """Редактор, открытый на эталонной кампании из видео: Flow 2 = 33/33/34."""
    campaign_id, _flow1, flow2 = fake.seed_campaign()
    return Editor(client, fake, campaign_id, flow2)
