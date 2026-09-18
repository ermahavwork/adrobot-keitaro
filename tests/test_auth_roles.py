"""Именованные токены: проверенный автор в журнале и роль «только чтение»."""

from __future__ import annotations

import asyncio
from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr

from app import db
from app.main import create_app
from tests.conftest import _create_schema, make_settings
from tests.fake_keitaro import FakeKeitaro

EDITOR = "fake-editor-tok"  # gitleaks:allow
VIEWER = "fake-viewer-tok"  # gitleaks:allow


@pytest.fixture
def secured(tmp_path, fake: FakeKeitaro) -> Iterator[TestClient]:
    settings = make_settings(
        tmp_path, adrobot_auth_tokens=SecretStr(f"andrey:{EDITOR}, viewer:{VIEWER}:ro"))
    _create_schema(settings.database_url)
    db.override_engine(db.create_engine(settings.database_url))
    with TestClient(create_app(settings, transport=fake.transport())) as client:
        yield client
    engine = db.get_engine()
    db.override_engine(None)
    asyncio.run(engine.dispose())


def bearer(token: str, **extra: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}", **extra}


def test_auth_mode_reports_token_required(secured):
    assert secured.get("/api/auth/mode").json() == {"token_required": True}


@pytest.mark.parametrize("headers", [{}, bearer("wrong-token-value"), bearer("short"), bearer("")])
def test_unknown_token_is_401(secured, headers):
    assert secured.get("/api/campaigns", headers=headers).status_code == 401


@pytest.mark.parametrize("raw", ["broken", "andrey:short7", ":no-name-token", "ok:good-token-1, x:short"])
def test_broken_entry_refuses_to_start_instead_of_opening_the_api(tmp_path, raw):
    """Опечатка в токенах не должна молча выключать защиту (fail-closed)."""
    with pytest.raises(ValueError, match="ADROBOT_AUTH_TOKENS"):
        make_settings(tmp_path, adrobot_auth_tokens=SecretStr(raw))


def test_token_may_contain_colons(tmp_path, fake):
    token = "abcdefgh:ijklmnop"  # gitleaks:allow
    settings = make_settings(tmp_path, adrobot_auth_tokens=SecretStr(f"andrey:{token}"))
    assert settings.auth_tokens() == [("andrey", token, False)]
    assert make_settings(tmp_path, adrobot_auth_tokens=SecretStr(f"v:{token}:ro")).auth_tokens() == \
        [("v", token, True)]


def test_viewer_can_read_but_not_write(secured, fake):
    assert secured.get("/api/campaigns", headers=bearer(VIEWER)).status_code == 200
    response = secured.post("/api/campaigns", headers=bearer(VIEWER),
                            json={"name": "x", "geo": "AU", "offer_id": 3749})
    assert response.status_code == 403
    assert "только на чтение" in response.json()["error"]["message"]
    assert not fake.campaigns


def test_token_name_is_verified_actor_and_beats_self_declared_header(secured, fake):
    headers = bearer(EDITOR, **{"X-AdRobot-User": "somebody-else"})
    created = secured.post("/api/campaigns", headers=headers,
                           json={"name": "by token", "geo": "AU", "offer_id": 3749})
    assert created.status_code == 200 and len(fake.campaigns) == 1
    last = secured.get("/api/operations", headers=bearer(EDITOR)).json()["items"][0]
    assert last["actor"] == "andrey"


def test_shared_and_named_tokens_work_together(tmp_path, fake):
    settings = make_settings(tmp_path, adrobot_auth_token=SecretStr("fake-shared"),
                             adrobot_auth_tokens=SecretStr(f"viewer:{VIEWER}:ro"))
    _create_schema(settings.database_url)
    db.override_engine(db.create_engine(settings.database_url))
    try:
        with TestClient(create_app(settings, transport=fake.transport())) as client:
            assert client.get("/api/campaigns", headers=bearer("fake-shared")).status_code == 200
            assert client.get("/api/campaigns", headers=bearer(VIEWER)).status_code == 200
            assert client.post("/api/campaigns/import", headers=bearer(VIEWER)).status_code == 403
            assert client.post("/api/campaigns/import", headers=bearer("fake-shared")).status_code == 200
    finally:
        engine = db.get_engine()
        db.override_engine(None)
        asyncio.run(engine.dispose())
