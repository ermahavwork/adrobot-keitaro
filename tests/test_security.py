"""Безопасность: токен доступа, заголовки и CSP, вычистка секретов из логов, кривой ввод."""

from __future__ import annotations

import asyncio
import io
import logging
import re
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path
from urllib.parse import quote

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr

from app import db
from app.config import Settings
from app.logging_conf import SecretRedactingFilter, setup_logging
from app.main import CSP_APP, SECURITY_HEADERS, WEB_DIR, create_app
from tests.conftest import Editor, make_settings
from tests.fake_keitaro import API_KEY, FakeKeitaro

TOKEN = "fake-tok"  # не секрет: токен тестового приложения
BEARER = {"Authorization": f"Bearer {TOKEN}"}
LOG_SECRET = "fake-secret-0123456789abcdef"  # не секрет: приметная строка для поиска в логах  # gitleaks:allow  # noqa: E501
OFFER = 3749
EVIL_NAME = "<script>alert(1)</script>\"'><img src=x onerror=alert(1)>"


def create_schema(database_url: str) -> None:
    async def run() -> None:
        engine = db.create_engine(database_url)
        async with engine.begin() as connection:
            await connection.run_sync(db.Base.metadata.create_all)
        await engine.dispose()

    asyncio.run(run())


@contextmanager
def running_app(fake: FakeKeitaro, tmp_path: Path, *, raise_server_exceptions: bool = True,
                **overrides) -> Iterator[TestClient]:
    """Приложение с нестандартными настройками на своей временной базе (как `conftest.client`)."""
    settings = make_settings(tmp_path, **overrides)
    create_schema(settings.database_url)
    db.override_engine(db.create_engine(settings.database_url))
    try:
        app = create_app(settings, transport=fake.transport())
        with TestClient(app, raise_server_exceptions=raise_server_exceptions) as test_client:
            yield test_client
    finally:
        engine = db.get_engine()
        db.override_engine(None)
        asyncio.run(engine.dispose())


@pytest.fixture
def secured(fake: FakeKeitaro, tmp_path: Path) -> Iterator[TestClient]:
    """Приложение в режиме с токеном: ADROBOT_AUTH_TOKEN задан."""
    with running_app(fake, tmp_path, adrobot_auth_token=SecretStr(TOKEN)) as test_client:
        yield test_client


class TestTokenMode:
    @pytest.mark.parametrize(("method", "path"), [
        ("GET", "/api/health"), ("GET", "/api/meta/lookups"), ("GET", "/api/meta/countries"),
        ("GET", "/api/offers"), ("POST", "/api/offers/refresh"), ("GET", "/api/settings"),
        ("PUT", "/api/settings"), ("GET", "/api/operations"), ("GET", "/api/campaigns"),
        ("POST", "/api/campaigns"), ("POST", "/api/campaigns/import"),
        ("POST", "/api/campaigns/open/1"), ("GET", "/api/campaigns/1"),
        ("POST", "/api/campaigns/1/fetch"), ("GET", "/api/campaigns/1/stats"),
        ("DELETE", "/api/campaigns/1"), ("GET", "/api/streams/1"),
        ("POST", "/api/streams/1/offers"), ("POST", "/api/streams/1/push"),
        ("POST", "/api/streams/1/cancel"), ("GET", "/api/streams/1/snapshots"),
    ])
    def test_every_api_route_needs_the_token(self, secured, method, path):
        response = secured.request(method, path)
        assert response.status_code == 401
        assert response.headers["WWW-Authenticate"] == "Bearer"
        error = response.json()["error"]
        assert error["code"] == "http_error" and "токен" in error["message"]

    @pytest.mark.parametrize("authorization", [
        "Bearer wrong-token", f"Bearer {TOKEN}x", f"Bearer {TOKEN[:-1]}", "Bearer", "Bearer ",
        f"Basic {TOKEN}", f"Token {TOKEN}", f"Bearer {TOKEN.upper()}",
    ])
    def test_wrong_token_is_rejected(self, secured, authorization):
        response = secured.get("/api/health", headers={"Authorization": authorization})
        assert response.status_code == 401

    def test_right_token_opens_the_api(self, secured):
        response = secured.get("/api/health", headers=BEARER)
        assert response.status_code == 200 and response.json()["status"] == "ok"

    def test_token_in_query_string_does_not_count(self, secured):
        response = secured.get("/api/health", params={"token": TOKEN, "authorization": TOKEN})
        assert response.status_code == 401, "токен в URL осел бы в логах прокси и истории браузера"

    def test_auth_mode_is_open_and_honest(self, secured):
        response = secured.get("/api/auth/mode")
        assert response.status_code == 200
        assert response.json() == {"token_required": True}

    def test_auth_mode_says_no_token_needed_by_default(self, client):
        assert client.get("/api/auth/mode").json() == {"token_required": False}
        assert client.get("/api/health").status_code == 200, "без токена в настройках вход открыт"

    @pytest.mark.parametrize("path", ["/", "/static/js/app.js", "/static/css/app.css"])
    def test_interface_loads_without_token_to_ask_for_it(self, secured, path):
        assert secured.get(path).status_code == 200

    def test_rejected_request_touches_neither_keitaro_nor_journal(self, secured, fake):
        body = {"name": "Intruder", "geo": "AU", "offer_id": OFFER}
        assert secured.post("/api/campaigns", json=body).status_code == 401
        assert secured.post("/api/campaigns/import").status_code == 401
        assert fake.requests == [] and not fake.campaigns
        assert secured.get("/api/operations", headers=BEARER).json()["total"] == 0

    def test_token_and_key_are_never_sent_to_the_browser(self, secured):
        pages = [secured.get(path, headers=BEARER).text for path in (
            "/", "/api/auth/mode", "/api/health", "/api/settings", "/api/meta/lookups",
            "/openapi.json")]
        pages.append(secured.get("/api/health", headers={"Authorization": "Bearer nope"}).text)
        for text in pages:
            assert TOKEN not in text and API_KEY not in text


class TestSecurityHeaders:
    @pytest.mark.parametrize("path", ["/", "/api/health?deep=false", "/static/js/app.js"])
    def test_security_headers_and_csp_are_set(self, client, path):
        response = client.get(path)
        assert response.status_code == 200
        for name, value in SECURITY_HEADERS.items():
            assert response.headers[name] == value
        assert response.headers["Content-Security-Policy"] == CSP_APP

    def test_csp_forbids_inline_scripts_foreign_hosts_and_framing(self):
        directives = dict(part.strip().split(" ", 1) for part in CSP_APP.split(";"))
        assert directives["default-src"] == "'self'" and directives["script-src"] == "'self'"
        assert directives["frame-ancestors"] == "'none'" and directives["base-uri"] == "'none'"
        assert "unsafe-inline" not in CSP_APP and "unsafe-eval" not in CSP_APP
        assert "http:" not in CSP_APP and "https:" not in CSP_APP and "*" not in CSP_APP

    @pytest.mark.parametrize("path", ["/docs", "/openapi.json"])
    def test_swagger_pages_do_not_get_our_csp(self, client, path):
        response = client.get(path)
        assert response.status_code == 200
        assert "Content-Security-Policy" not in response.headers, "Swagger UI — inline-скрипт"
        assert response.headers["X-Content-Type-Options"] == "nosniff"
        assert response.headers["X-Frame-Options"] == "DENY"

    @pytest.mark.parametrize(("method", "path", "status"), [
        ("GET", "/api/health?deep=false", 200), ("GET", "/api/campaigns/999", 404),
        ("GET", "/api/nope", 404), ("POST", "/api/campaigns", 422), ("PATCH", "/api/campaigns", 405),
    ])
    def test_api_answers_are_never_cached(self, client, method, path, status):
        response = client.request(method, path)
        assert response.status_code == status
        assert response.headers["Cache-Control"] == "no-store"

    def test_unauthorized_answer_is_not_cached_and_carries_headers(self, secured):
        response = secured.get("/api/campaigns")
        assert response.status_code == 401
        assert response.headers["Cache-Control"] == "no-store"
        assert response.headers["Content-Security-Policy"] == CSP_APP

    def test_index_is_revalidated_not_stored_forever(self, client):
        assert client.get("/").headers["Cache-Control"] == "no-cache"

    def test_index_html_obeys_its_own_csp(self):
        html = (WEB_DIR / "templates" / "index.html").read_text(encoding="utf-8")
        assert not re.search(r"<script(?![^>]*\bsrc=)", html), "inline-скрипт CSP заблокирует"
        assert not re.search(r"\son[a-z]+\s*=", html), "inline-обработчики CSP заблокирует"
        assert "<style" not in html and "style=" not in html
        assert not re.search(r"""(?:src|href)\s*=\s*["'](?:https?:)?//""", html), "чужие домены"

    def test_frontend_never_builds_html_from_strings(self):
        sinks = re.compile(r"\.\s*(?:innerHTML|outerHTML)\b|insertAdjacentHTML\s*\(|"
                           r"document\.write\s*\(|\beval\s*\(|new\s+Function\b")
        scripts = sorted((WEB_DIR / "static" / "js").rglob("*.js"))
        assert scripts, "интерфейс на месте"
        for script in scripts:
            assert not sinks.search(script.read_text(encoding="utf-8")), script.name

    def test_stylesheets_load_nothing_from_foreign_hosts(self):
        for sheet in sorted((WEB_DIR / "static" / "css").glob("*.css")):
            assert not re.search(r"https?://|url\(\s*[\"']?//", sheet.read_text(encoding="utf-8"))

    @pytest.mark.parametrize("path", [
        "/static/../main.py", "/static/%2e%2e/main.py", "/static/..%2fmain.py",
        "/static/%2e%2e%2f%2e%2e%2fconfig.py", "/static//etc/passwd",
        "/static/js/../../../config.py",
    ])
    def test_static_mount_does_not_leak_source_files(self, client, path):
        response = client.get(path)
        assert response.status_code == 404
        assert "import" not in response.text


def render_log(emit: Callable[[logging.Logger], None], secrets: list[str]) -> str:
    """Что окажется в файле лога: отдельный логгер с форматтером и нашим фильтром."""
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.setFormatter(logging.Formatter("%(levelname)s %(name)s: %(message)s"))
    handler.addFilter(SecretRedactingFilter(secrets))
    logger = logging.getLogger("tests.redaction")
    logger.addHandler(handler)
    logger.setLevel(logging.DEBUG)
    logger.propagate = False
    try:
        emit(logger)
    finally:
        logger.removeHandler(handler)
        logger.propagate = True
    return stream.getvalue()


@pytest.fixture
def restore_logging() -> Iterator[None]:
    """`setup_logging` правит корневой логгер — возвращаем как было."""
    root = logging.getLogger()
    level, handlers = root.level, list(root.handlers)
    filters = {handler: list(handler.filters) for handler in handlers}
    yield
    root.setLevel(level)
    for handler in list(root.handlers):
        if handler not in handlers:
            root.removeHandler(handler)
    for handler, saved in filters.items():
        handler.filters = saved


class TestLogRedaction:
    def test_known_secret_is_cut_from_message(self):
        text = render_log(lambda log: log.warning(f"ключ {LOG_SECRET} отклонён"), [LOG_SECRET])
        assert LOG_SECRET not in text and "ключ *** отклонён" in text

    def test_known_secret_is_cut_from_args(self):
        text = render_log(lambda log: log.warning("запрос %s с ключом %s", "GET /offers",
                                                  LOG_SECRET), [LOG_SECRET])
        assert LOG_SECRET not in text and "GET /offers" in text

    def test_known_secret_is_cut_from_nested_structures_in_args(self):
        headers = {"Api-Key": LOG_SECRET, "Accept": "application/json"}
        text = render_log(lambda log: log.error("заголовки: %s", headers), [LOG_SECRET])
        assert LOG_SECRET not in text and "application/json" in text

    def test_exception_object_as_message_is_redacted(self):
        text = render_log(lambda log: log.error(RuntimeError(f"bad key {LOG_SECRET}")),
                          [LOG_SECRET])
        assert LOG_SECRET not in text

    @pytest.mark.parametrize("line", [
        "api_key=fakeVALUE123", "Api-Key: fakeVALUE123", "'api-key': 'fakeVALUE123'",
        "APIKEY=fakeVALUE123", "Authorization: Bearer fakeVALUE123", "authorization=fakeVALUE123",
    ])
    def test_unknown_secret_is_cut_by_key_value_pattern(self, line):
        text = render_log(lambda log: log.info("outgoing %s done", line), [])
        assert "fakeVALUE123" not in text and "***" in text and text.rstrip().endswith("done")

    def test_short_or_empty_secrets_do_not_shred_the_log(self):
        text = render_log(lambda log: log.info("кампания создана, оффер 3749"), ["", "а", "3749"])
        assert "кампания создана, оффер 3749" in text

    def test_harmless_record_is_left_untouched(self):
        record = logging.LogRecord("x", logging.INFO, __file__, 1, "поток %s: долей %d", ("A", 3),
                                   None)
        assert SecretRedactingFilter([LOG_SECRET]).filter(record) is True
        assert record.args == ("A", 3), "аргументы не трогаем без нужды"

    def test_broken_format_string_does_not_crash_the_filter(self):
        record = logging.LogRecord("x", logging.INFO, __file__, 1, "число %d", ("не число",), None)
        assert SecretRedactingFilter([LOG_SECRET]).filter(record) is True

    def test_setup_logging_guards_root_handlers(self, restore_logging, caplog):
        setup_logging("INFO", secrets=[LOG_SECRET, ""])
        logging.getLogger("third.party.lib").warning("sending headers %s", {"Api-Key": LOG_SECRET})
        assert LOG_SECRET not in caplog.text and "***" in caplog.text

    def test_setup_logging_twice_keeps_single_filter_with_fresh_secrets(self, restore_logging,
                                                                         caplog):
        setup_logging("INFO", secrets=["old-secret-value"])
        setup_logging("INFO", secrets=[LOG_SECRET])
        for handler in logging.getLogger().handlers:
            assert len([f for f in handler.filters if isinstance(f, SecretRedactingFilter)]) == 1
        logging.getLogger("third.party.lib").warning("key rotated to %s", LOG_SECRET)
        assert LOG_SECRET not in caplog.text

    def test_application_start_registers_both_secrets(self, fake, tmp_path, restore_logging,
                                                      caplog):
        with running_app(fake, tmp_path, keitaro_api_key=SecretStr(LOG_SECRET),
                         adrobot_auth_token=SecretStr(TOKEN)):
            setup_filters = [f for h in logging.getLogger().handlers for f in h.filters
                             if isinstance(f, SecretRedactingFilter)]
            assert setup_filters, "create_app вешает фильтр на обработчики корневого логгера"
            assert setup_filters[0].redact(f"{LOG_SECRET} / {TOKEN}") == "*** / ***"

    def test_secret_inside_traceback_is_redacted_too(self):
        # Регрессия: текст исключения форматтер дописывает ПОСЛЕ фильтра, и logger.exception(...)
        # печатал секрет как есть.
        def emit(log: logging.Logger) -> None:
            try:
                raise RuntimeError(f"request failed, headers={{'Api-Key': '{LOG_SECRET}'}}")
            except RuntimeError:
                log.exception("необработанная ошибка")

        text = render_log(emit, [LOG_SECRET])
        assert "необработанная ошибка" in text
        assert "Traceback (most recent call last)" in text and "RuntimeError" in text
        assert LOG_SECRET not in text and "***" in text

    def test_secret_inside_chained_exception_is_redacted(self):
        def emit(log: logging.Logger) -> None:
            try:
                try:
                    raise ValueError(f"inner cause with {LOG_SECRET}")
                except ValueError as inner:
                    raise RuntimeError("outer failure") from inner
            except RuntimeError:
                log.error("сбой", exc_info=True)

        text = render_log(emit, [LOG_SECRET])
        assert "inner cause with ***" in text and LOG_SECRET not in text

    def test_secret_inside_stack_info_is_redacted(self):
        def emit(log: logging.Logger) -> None:
            marker = f"local value {LOG_SECRET}"
            log.warning("где мы: %s", len(marker), stack_info=True)

        record_text = render_log(emit, [LOG_SECRET])
        assert "Stack (most recent call last)" in record_text
        assert LOG_SECRET not in record_text

    def test_traceback_without_secrets_is_left_as_is(self):
        def emit(log: logging.Logger) -> None:
            try:
                raise KeyError("offer_id")
            except KeyError:
                log.exception("нет поля")

        text = render_log(emit, [LOG_SECRET])
        assert text.rstrip().endswith("KeyError: 'offer_id'")

    def test_exception_logged_outside_except_block_does_not_crash(self):
        text = render_log(lambda log: log.exception("нет активного исключения"), [LOG_SECRET])
        assert "нет активного исключения" in text


class TestSettingsNormalization:
    @pytest.mark.parametrize("raw", [
        "https://tracker.example.com", "https://tracker.example.com/",
        "https://tracker.example.com///", "  https://tracker.example.com  ",
        "https://tracker.example.com/admin", "https://tracker.example.com/admin/",
        "https://tracker.example.com/admin/#!", "https://tracker.example.com/admin/#!/",
        "https://tracker.example.com/admin_api", "https://tracker.example.com/admin_api/v1",
        "https://tracker.example.com/admin_api/v1/", "tracker.example.com",
        "tracker.example.com/admin/", "tracker.example.com/admin_api/v1",
    ])
    def test_base_url_is_normalized(self, raw):
        assert Settings(_env_file=None, keitaro_base_url=raw).keitaro_base_url == \
            "https://tracker.example.com"

    def test_plain_http_is_respected(self):
        settings = Settings(_env_file=None, keitaro_base_url="http://10.0.0.5:8080/admin/")
        assert settings.keitaro_base_url == "http://10.0.0.5:8080"

    def test_tracking_domain_is_normalized_the_same_way(self):
        settings = Settings(_env_file=None, tracking_domain_url="in.example.com/")
        assert settings.tracking_domain_url == "https://in.example.com"

    def test_empty_url_stays_empty_and_means_not_configured(self):
        settings = Settings(_env_file=None, keitaro_base_url="   ", keitaro_api_key=SecretStr("k"))
        assert settings.keitaro_base_url == "" and settings.keitaro_configured is False
        assert settings.admin_url == "" and settings.campaign_admin_url(5) == ""

    def test_admin_links_are_built_from_normalized_url(self):
        settings = Settings(_env_file=None, keitaro_base_url="tracker.example.com/admin/")
        assert settings.admin_url == "https://tracker.example.com/admin/"
        assert settings.campaign_admin_url(42) == "https://tracker.example.com/admin/#!/campaigns/42"

    @pytest.mark.parametrize(("raw", "expected"), [("", None), ("   ", None), ("11", 11)])
    def test_blank_default_ids_from_env_mean_auto(self, raw, expected):
        settings = Settings(_env_file=None, default_domain_id=raw, default_group_id=raw,
                            default_traffic_source_id=raw)
        assert (settings.default_domain_id, settings.default_group_id,
                settings.default_traffic_source_id) == (expected, expected, expected)

    def test_secrets_are_masked_in_settings_repr(self, tmp_path):
        settings = make_settings(tmp_path, keitaro_api_key=SecretStr(LOG_SECRET),
                                 adrobot_auth_token=SecretStr(TOKEN))
        for text in (repr(settings), str(settings), settings.model_dump_json()):
            assert LOG_SECRET not in text and TOKEN not in text

    @pytest.mark.parametrize("raw", [
        "https://tracker.example.com/admin/#!/campaigns",
        "https://tracker.example.com/admin/#!/campaigns/1234",
        "https://tracker.example.com/admin/?object=campaigns.list",
        "https://tracker.example.com/admin/?object=campaigns.list#!/streams/5",
        "https://tracker.example.com/admin_api/v1/campaigns?limit=500",
        "https://tracker.example.com?utm=1#top",
        "HTTPS://tracker.example.com", "Https://tracker.example.com/admin",
        "https://tracker.example.com /admin", "\thttps://tracker.example.com/admin/\n",
        "//tracker.example.com/admin", "ftp://tracker.example.com",
    ])
    def test_address_copied_from_browser_is_normalized(self, raw):
        # Регрессия: …/admin/#!/campaigns оставался как есть (запросы уходили на /admin/ и
        # возвращали HTML), а схема в верхнем регистре давала https://HTTPS://….
        assert Settings(_env_file=None, keitaro_base_url=raw).keitaro_base_url == \
            "https://tracker.example.com"

    @pytest.mark.parametrize(("raw", "expected"), [
        ("https://host.example.com/kt", "https://host.example.com/kt"),
        ("https://host.example.com/kt/", "https://host.example.com/kt"),
        ("https://host.example.com/kt/admin/#!/campaigns/7", "https://host.example.com/kt"),
        ("host.example.com:8443/kt/admin_api/v1", "https://host.example.com:8443/kt"),
        ("http://10.0.0.5:8080/admin/?object=x", "http://10.0.0.5:8080"),
        ("localhost:8080", "https://localhost:8080"),
        ("https://in.example.com/go", "https://in.example.com/go"),
    ])
    def test_custom_base_path_and_port_are_kept(self, raw, expected):
        assert Settings(_env_file=None, keitaro_base_url=raw).keitaro_base_url == expected

    @pytest.mark.parametrize("raw", ["https://", "http:///admin", "://", "https:///"])
    def test_address_without_host_means_not_configured(self, raw):
        settings = Settings(_env_file=None, keitaro_base_url=raw, keitaro_api_key=SecretStr("k"))
        assert settings.keitaro_base_url == "" and settings.keitaro_configured is False

    def test_client_really_talks_to_normalized_address(self, fake, tmp_path):
        with running_app(fake, tmp_path,
                         keitaro_base_url="https://tracker.test/admin/#!/campaigns/15") as app:
            assert app.get("/api/health").json()["keitaro"]["reachable"] is True
            assert app.get("/api/health").json()["admin_url"] == "https://tracker.test/admin/"
        assert [r[:2] for r in fake.requests] == [("GET", "/groups")] * 2


def create(client: TestClient, **body):
    return client.post("/api/campaigns", json={"name": "Test AU", "geo": "AU", "offer_id": OFFER,
                                               **body})


class TestInputHardening:
    def test_html_in_offer_name_travels_as_plain_json_string(self, client, fake):
        fake.add_offer(777, EVIL_NAME)
        response = client.get("/api/offers", params={"q": "777"})
        assert response.status_code == 200
        assert response.headers["Content-Type"] == "application/json"
        assert response.headers["X-Content-Type-Options"] == "nosniff"
        (offer,) = response.json()
        assert offer["name"] == EVIL_NAME and offer["label"] == f"[777] {EVIL_NAME}"

    def test_html_in_offer_name_does_not_break_the_editor(self, client, fake):
        fake.add_offer(777, EVIL_NAME)
        editor = Editor(client, fake, *fake.seed_campaign()[::2])
        view = editor.add(777)
        row = next(o for o in view["offers"] if o["offer_id"] == 777)
        assert row["offer_name"] == EVIL_NAME
        editor.push()
        assert 777 in editor.in_keitaro()

    def test_html_in_campaign_name_is_stored_and_returned_verbatim(self, client, fake):
        result = create(client, name=EVIL_NAME).json()["results"][0]
        assert result["status"] == "created"
        assert client.get(f"/api/campaigns/{result['campaign_id']}").json()["name"] == EVIL_NAME
        journal = client.get("/api/operations").json()["items"][0]
        assert EVIL_NAME in journal["summary"]

    @pytest.mark.parametrize("q", ["' OR 1=1 --", "%'; DROP TABLE campaigns; --", "\\", "%",
                                   "\x00", "🙂" * 50])
    def test_search_strings_are_data_not_sql(self, client, fake, q):
        fake.seed_campaign("кампания для поиска")
        client.post("/api/campaigns/import")
        assert client.get("/api/campaigns", params={"q": q}).json()["total"] == 0
        assert client.get("/api/offers", params={"q": q}).json() == []
        assert client.get("/api/campaigns").json()["total"] == 1, "таблица на месте"

    def test_like_wildcard_is_a_literal_character_in_search(self, client):
        found = {offer["id"] for offer in client.get("/api/offers", params={"q": "_"}).json()}
        assert found == {3749, 3717, 11111, 11112}, "только названия с настоящим подчёркиванием"

    @pytest.mark.parametrize("body", [
        {"name": "N" * 10_000}, {"name": ""}, {"name": None}, {"name": ["list"]},
        {"is_admin": True}, {"offer_id": -5}, {"offer_id": 0}, {"offer_id": "abc"},
        {"offer_id": 1.5}, {"offer_id": None, "offer_ids": []}, {"offer_ids": [3717, -1]},
        {"offer_ids": list(range(1, 52))}, {"offer_ids": "3717"}, {"alias": "my alias"},
        {"alias": "алиас"}, {"alias": "a/b"}, {"alias": "a" * 65}, {"alias": ""},
        {"geo": None}, {"geo": [1, 2]}, {"geo": {"code": "AU"}}, {"domain_id": 0},
        {"group_id": -1}, {"traffic_source_id": "x"}, {"redirect_url": "https://e.test/" + "a" * 600},
        {"dry_run": "maybe"}, {"split_by_geo": [True]},
    ], ids=lambda body: ",".join(body))
    def test_garbage_in_create_form_is_a_422_not_a_crash(self, client, fake, body):
        response = create(client, **body)
        assert response.status_code == 422
        error = response.json()["error"]
        assert error["code"] == "validation" and error["details"]["errors"]
        assert not fake.campaigns, "до Keitaro мусор не дошёл"

    @pytest.mark.parametrize("kwargs", [
        {"content": b"{oops", "headers": {"Content-Type": "application/json"}},
        {"json": [1, 2, 3]}, {"json": "just a string"}, {"json": None}, {"content": b""},
        {"content": b"name=x&geo=AU", "headers": {"Content-Type": "text/plain"}},
        {"content": "{\"name\": \"\ud83d\"}".encode("utf-8", "surrogatepass"),
         "headers": {"Content-Type": "application/json"}},
    ], ids=["broken-json", "array", "string", "null", "empty", "form", "lone-surrogate"])
    def test_malformed_body_is_a_422_in_our_format(self, client, kwargs):
        response = client.post("/api/campaigns", **kwargs)
        assert response.status_code == 422
        assert response.json()["error"]["code"] == "validation"

    def test_oversized_idempotency_key_is_rejected(self, client, fake):
        response = client.post("/api/campaigns", headers={"Idempotency-Key": "k" * 129},
                               json={"name": "Test AU", "geo": "AU", "offer_id": OFFER})
        assert response.status_code == 422 and not fake.campaigns

    @pytest.mark.parametrize(("path", "body"), [
        ("/offers", {"offer_id": "3749; DROP TABLE offers"}), ("/offers", {"offer_id": 0}),
        ("/offers", {"offer_id": OFFER, "share": 100}), ("/offers", {}),
        ("/push", {"force": "yes please"}), ("/push", {"forse": True}),
        ("/recalculate", {"drop_pins": 5}),
    ])
    def test_garbage_in_editor_bodies_is_a_422(self, editor, path, body):
        response = editor.client.post(f"/api/streams/{editor.stream['id']}{path}", json=body)
        assert response.status_code == 422
        assert response.json()["error"]["code"] == "validation"
        assert not editor.stream["is_dirty"]

    @pytest.mark.parametrize("field", ["default_redirect_url", "tracking_domain_url"])
    @pytest.mark.parametrize("url", [
        "javascript:alert(1)", "data:text/html,<script>alert(1)</script>", "file:///etc/passwd",
        "//evil.test", "evil.test", "https://", "https://exa mple.test", "ftp://evil.test",
    ])
    def test_settings_accept_only_http_urls(self, client, field, url):
        before = client.get("/api/settings").json()
        response = client.put("/api/settings", json={field: url})
        assert response.status_code == 422
        assert response.json()["error"]["code"] == "bad_redirect_url"
        assert client.get("/api/settings").json() == before, "ничего не сохранилось"

    @pytest.mark.parametrize("body", [{"unknown": 1}, {"default_domain_id": 0},
                                      {"default_group_id": -3}, {"default_traffic_source_id": "x"},
                                      {"default_redirect_url": "https://e.test/" + "a" * 600}])
    def test_settings_reject_garbage(self, client, body):
        response = client.put("/api/settings", json=body)
        assert response.status_code == 422 and response.json()["error"]["code"] == "validation"

    @pytest.mark.parametrize(("method", "path", "body"), [
        ("POST", "/api/campaigns", {"name": "Big", "geo": "AU", "offer_id": 2**63}),
        ("POST", "/api/campaigns", {"name": "Big", "geo": "AU", "offer_ids": [OFFER, 2**63]}),
        ("POST", "/api/campaigns", {"name": "Big", "geo": "AU", "offer_id": OFFER,
                                    "domain_id": 2**31}),
        ("POST", "/api/campaigns", {"name": "Big", "geo": "AU", "offer_id": OFFER,
                                    "group_id": 10**30, "traffic_source_id": 10**30}),
        ("POST", "/api/streams/{stream}/offers", {"offer_id": 2**63}),
        ("PUT", "/api/settings", {"default_group_id": 10**30}),
        ("GET", f"/api/campaigns/{2**63}", None),
        ("POST", f"/api/campaigns/{2**63}/fetch", None),
        ("DELETE", f"/api/campaigns/{2**63}", None),
        ("GET", f"/api/streams/{2**63}", None),
        ("DELETE", "/api/streams/{stream}/offers/" + str(2**63), None),
        ("POST", f"/api/campaigns/open/{2**63}", None),
        ("GET", f"/api/operations?keitaro_campaign_id={2**63}", None),
    ], ids=["create-offer-id", "create-offer-ids", "create-domain-id", "create-group-source",
            "add-offer-id", "settings-id", "campaign-id", "fetch-id", "archive-id", "stream-id",
            "binding-id", "open-id", "journal-filter"])
    def test_huge_integers_are_client_errors_not_500(self, fake, tmp_path, method, path, body):
        # Регрессия: 2**63 доходило до sqlite3 → OverflowError → 500 internal_error.
        with running_app(fake, tmp_path, raise_server_exceptions=False) as lenient:
            editor = Editor(lenient, fake, *fake.seed_campaign()[::2])
            url = path.format(stream=editor.stream["id"])
            fake.requests.clear()
            response = lenient.request(method, url, json=body)
        assert response.status_code in (404, 422), response.text
        assert response.json()["error"]["code"] in ("validation", "binding_not_found")
        assert not [r for r in fake.requests if r[0] != "GET"], "в Keitaro ничего не записано"

    def test_biggest_allowed_id_is_still_a_normal_request(self, client, fake):
        response = create(client, offer_id=2_147_483_647)
        assert response.status_code == 422
        assert "не найден" in response.json()["error"]["message"], "дошло до проверки оффера"


class TestActorHeader:
    @staticmethod
    def last_actor(client: TestClient) -> str:
        return client.get("/api/operations").json()["items"][0]["actor"]

    def test_percent_encoded_cyrillic_name_lands_in_journal_decoded(self, client):
        client.post("/api/campaigns/import", headers={"X-AdRobot-User": quote("Андрей Ермохин")})
        assert self.last_actor(client) == "Андрей Ермохин"

    def test_name_is_cut_to_64_characters_after_decoding(self, client):
        client.post("/api/campaigns/import", headers={"X-AdRobot-User": quote("Я" * 100)})
        assert self.last_actor(client) == "Я" * 64

    def test_surrounding_spaces_are_dropped(self, client):
        client.post("/api/campaigns/import", headers={"X-AdRobot-User": "%20%20buyer-7%20"})
        assert self.last_actor(client) == "buyer-7"

    def test_failed_operation_remembers_the_actor_too(self, client):
        response = client.post("/api/campaigns", headers={"X-AdRobot-User": quote("Оля")},
                               json={"name": "Bad geo", "geo": "ZZ", "offer_id": OFFER})
        assert response.status_code == 422
        item = client.get("/api/operations").json()["items"][0]
        assert (item["status"], item["actor"]) == ("error", "Оля")

    def test_no_header_means_anonymous_and_does_not_stick_between_requests(self, client):
        client.post("/api/campaigns/import", headers={"X-AdRobot-User": "first"})
        client.post("/api/campaigns/import")
        actors = [item["actor"] for item in client.get("/api/operations").json()["items"]]
        assert actors == ["", "first"]

    @pytest.mark.parametrize("raw", ["%FF%FEbroken%", "%", "%%%", "%D0", "a%00b"])
    def test_broken_encoding_does_not_break_the_request(self, client, raw):
        response = client.post("/api/campaigns/import", headers={"X-AdRobot-User": raw})
        assert response.status_code == 200
        assert len(self.last_actor(client)) <= 64

    def test_frontend_cuts_the_name_before_encoding_it(self):
        source = (WEB_DIR / "static" / "js" / "api.js").read_text(encoding="utf-8")
        assert "X-AdRobot-User" in source, "заголовок по-прежнему ставит api.js"
        assert not re.search(r"encodeURIComponent\([^)]*\)\s*\.slice\(", source)

    def test_markup_in_name_is_kept_as_text(self, client):
        client.post("/api/campaigns/import", headers={"X-AdRobot-User": quote(EVIL_NAME)})
        assert self.last_actor(client) == EVIL_NAME
