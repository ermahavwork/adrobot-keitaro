"""Клиент Admin API Keitaro напрямую: разбор ошибок, повторы, страницы, заголовки, секреты.

Сеть подменена: либо эмулятором (`fake.transport()`), либо собственным обработчиком
`httpx.MockTransport`. Плагина pytest-asyncio в проекте нет, поэтому каждый сценарий
выполняется в свежем цикле событий через `asyncio.run`.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Awaitable, Callable
from typing import Any

import httpx
import pytest

from app.keitaro.client import KeitaroClient
from app.keitaro.errors import (
    KeitaroAuthError,
    KeitaroError,
    KeitaroForbiddenError,
    KeitaroNetworkError,
    KeitaroNotConfiguredError,
    KeitaroNotFoundError,
    KeitaroProtocolError,
    KeitaroRateLimitError,
    KeitaroServerError,
    KeitaroValidationError,
)
from tests.fake_keitaro import API_KEY, FakeKeitaro

BASE_URL = "https://tracker.test"
LONG_KEY = "kt-key-0123456789abcdef0123456789"  # не секрет: приметный ключ для поиска утечек  # gitleaks:allow  # noqa: E501

Handler = Callable[[httpx.Request], Any]
Action = Callable[[KeitaroClient], Awaitable[Any]]


def run(network: httpx.MockTransport | Handler, action: Action, *, base_url: str = BASE_URL,
        api_key: str = API_KEY, **options: Any) -> Any:
    """Создаёт клиента, выполняет `action(client)` и закрывает его — всё в одном цикле событий."""
    transport = network if isinstance(network, httpx.MockTransport) else httpx.MockTransport(network)

    async def scenario() -> Any:
        client = KeitaroClient(base_url, api_key, transport=transport,
                               **{"backoff_base": 0.0, "max_retries": 2, **options})
        try:
            return await action(client)
        finally:
            await client.aclose()

    return asyncio.run(scenario())


def always(status: int, *, body: Any = None, headers: dict[str, str] | None = None) -> Handler:
    """Обработчик, который на любой запрос отвечает одним и тем же."""
    def handler(_: httpx.Request) -> httpx.Response:
        if isinstance(body, (dict, list)):
            return httpx.Response(status, json=body, headers=headers)
        return httpx.Response(status, text=body or "", headers=headers)
    return handler


def count(fake: FakeKeitaro, method: str, path: str) -> int:
    return len([r for r in fake.requests if r[:2] == (method, path)])


def offers(client: KeitaroClient) -> Awaitable[Any]:
    return client.list_offers()


class TestConfiguration:
    @pytest.mark.parametrize(("base_url", "api_key"), [("", API_KEY), (BASE_URL, ""), ("", "")])
    def test_unconfigured_client_refuses_before_any_network(self, base_url, api_key):
        calls: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            calls.append(request)
            return httpx.Response(200, json=[])

        async def action(client: KeitaroClient) -> None:
            assert client.configured is False
            await client.list_offers()

        with pytest.raises(KeitaroNotConfiguredError) as caught:
            run(handler, action, base_url=base_url, api_key=api_key)
        assert "KEITARO_BASE_URL" in caught.value.message
        assert "KEITARO_API_KEY" in caught.value.message
        assert (caught.value.http_status, caught.value.code) == (503, "keitaro_not_configured")
        assert calls == [], "без настроек в сеть не ходим вовсе"

    def test_api_key_user_agent_and_prefix_really_leave_the_process(self):
        seen: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen.append(request)
            return httpx.Response(200, json=[])

        run(handler, lambda c: c.list_groups(), api_key=LONG_KEY, user_agent="AdRobot-Test/9.9")
        request = seen[0]
        assert request.headers["Api-Key"] == LONG_KEY
        assert request.headers["User-Agent"] == "AdRobot-Test/9.9"
        assert request.headers["Accept"] == "application/json"
        assert str(request.url) == f"{BASE_URL}/admin_api/v1/groups?type=campaigns"

    def test_default_user_agent_is_not_pythonic(self):
        seen: list[str] = []
        run(lambda r: seen.append(r.headers["User-Agent"]) or httpx.Response(200, json=[]), offers)
        assert seen == ["AdRobot-Keitaro/1.0"]
        assert "python" not in seen[0].lower(), "Cloudflare банит питоновские UA (ошибка 1010)"

    def test_trailing_slash_in_base_url_gives_no_double_slash(self):
        seen: list[str] = []
        run(lambda r: seen.append(str(r.url)) or httpx.Response(200, json=[]), offers,
            base_url=BASE_URL + "/")
        assert seen == [f"{BASE_URL}/admin_api/v1/offers"]

    def test_json_body_is_sent_as_is(self):
        bodies: list[Any] = []

        def handler(request: httpx.Request) -> httpx.Response:
            bodies.append((request.method, request.url.path, json.loads(request.content)))
            return httpx.Response(200, json={"id": 7, "offers": []})

        payload = [{"offer_id": 1, "share": 100, "state": "active"}]
        run(handler, lambda c: c.update_stream_offers(7, payload))
        assert bodies == [("PUT", "/admin_api/v1/streams/7", {"offers": payload})]

    def test_redirect_is_not_followed_so_the_key_never_reaches_another_host(self):
        hosts: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            hosts.append(request.url.host)
            return httpx.Response(302, headers={"Location": "https://evil.test/steal"})

        with pytest.raises(KeitaroError):
            run(handler, offers)
        assert hosts == ["tracker.test"]


class TestErrorMapping:
    @pytest.mark.parametrize(("status", "body", "error_class", "needle"), [
        (401, {"error": "Unauthorized"}, KeitaroAuthError, "KEITARO_API_KEY"),
        (402, "Payment Required", KeitaroForbiddenError, "лицензия"),
        (403, {"error": "Access denied for this resource"}, KeitaroForbiddenError,
         "Access denied for this resource"),
        (403, "", KeitaroForbiddenError, "нет прав у ключа API"),
        (404, "Traffic\\Model\\Campaign #5 not found", KeitaroNotFoundError,
         "Campaign #5 not found"),
        (404, "", KeitaroNotFoundError, "/offers"),
        (400, "Malformed request", KeitaroValidationError, "Malformed request"),
        (422, {"error": "Offer is broken"}, KeitaroValidationError, "Offer is broken"),
        (500, "<h1>Whoops</h1>", KeitaroServerError, "(500) на GET /offers"),
        (418, {"error": "teapot"}, KeitaroError, "Неожиданный ответ Keitaro (418)"),
    ])
    def test_status_becomes_the_right_error_with_a_human_message(self, status, body, error_class,
                                                                  needle):
        with pytest.raises(KeitaroError) as caught:
            run(always(status, body=body), offers)
        assert type(caught.value) is error_class
        assert needle in caught.value.message
        assert caught.value.upstream_status == status

    def test_422_field_errors_are_kept_as_details_and_flattened_into_message(self):
        fields = {"alias": ["Alias has already used"], "name": ["Name is required", "Too short"]}
        with pytest.raises(KeitaroValidationError) as caught:
            run(always(422, body=fields), lambda c: c.create_campaign({"name": ""}))
        assert caught.value.details == fields
        assert "alias: Alias has already used" in caught.value.message
        assert "name: Name is required, Too short" in caught.value.message
        assert (caught.value.http_status, caught.value.code) == (422, "keitaro_validation")

    def test_429_after_retries_is_rate_limit_error(self):
        with pytest.raises(KeitaroRateLimitError) as caught:
            run(always(429, body={"error": "Too many requests"}), offers)
        assert "сбавить темп" in caught.value.message
        assert (caught.value.http_status, caught.value.code) == (503, "keitaro_rate_limited")

    def test_wrong_key_against_emulator_is_auth_error(self, fake):
        with pytest.raises(KeitaroAuthError) as caught:
            run(fake.transport(), offers, api_key="wrong-key")
        assert (caught.value.http_status, caught.value.code) == (502, "keitaro_unauthorized")

    def test_cloudflare_block_is_named_and_explained(self, fake):
        fake.fail_next("GET", r"^/offers$", status=403, body=fake.cloudflare_body())
        with pytest.raises(KeitaroForbiddenError) as caught:
            run(fake.transport(), offers)
        assert "Cloudflare" in caught.value.message
        assert "browser_signature_banned" in caught.value.message
        assert "KEITARO_USER_AGENT" in caught.value.message, "подсказываем, что крутить"

    def test_html_with_status_200_is_protocol_error(self):
        with pytest.raises(KeitaroProtocolError) as caught:
            run(always(200, body="<html>login page</html>"), offers)
        assert "не JSON" in caught.value.message and "KEITARO_BASE_URL" in caught.value.message
        assert (caught.value.http_status, caught.value.code) == (502, "keitaro_bad_response")

    @pytest.mark.parametrize(("action", "body"), [
        (offers, {"rows": []}),
        (offers, "just a string"),
        (lambda c: c.get_stream(1), [{"id": 1}, {"id": 2}]),
        (lambda c: c.get_campaign(1), 42),
    ])
    def test_json_of_unexpected_shape_is_protocol_error(self, action, body):
        def handler(_: httpx.Request) -> httpx.Response:
            return httpx.Response(200, content=json.dumps(body).encode(),
                                  headers={"Content-Type": "application/json"})

        with pytest.raises(KeitaroProtocolError):
            run(handler, action)

    def test_single_element_list_is_unwrapped_into_object(self):
        campaign = run(always(200, body=[{"id": 5, "name": "clone"}]), lambda c: c.get_campaign(5))
        assert campaign == {"id": 5, "name": "clone"}

    def test_non_object_rows_are_dropped_from_lists(self):
        rows = run(always(200, body=[{"id": 1}, "junk", None, 7, {"id": 2}]), offers)
        assert rows == [{"id": 1}, {"id": 2}]

    def test_long_upstream_text_is_trimmed(self):
        with pytest.raises(KeitaroNotFoundError) as caught:
            run(always(404, body="x" * 5000), offers)
        assert len(caught.value.message) < 400

    def test_error_is_serializable_for_api_answer(self):
        with pytest.raises(KeitaroError) as caught:
            run(always(422, body={"alias": ["taken"]}), offers)
        assert caught.value.to_dict() == {
            "code": "keitaro_validation", "message": caught.value.message,
            "upstream_status": 422, "details": {"alias": ["taken"]}}


class TestRetries:
    @pytest.mark.parametrize("status", [429, 502, 503, 504])
    def test_get_is_retried_on_transient_status(self, fake, status):
        fake.fail_next("GET", r"^/offers$", status=status, body="try later", times=2)
        assert len(run(fake.transport(), offers)) == len(fake.offers)
        assert count(fake, "GET", "/offers") == 3, "две неудачи и успешная третья попытка"

    @pytest.mark.parametrize("make_exception", [
        lambda r: httpx.ConnectError("refused", request=r),
        lambda r: httpx.ReadTimeout("slow", request=r),
        lambda r: httpx.ReadError("reset by peer", request=r),
        lambda r: httpx.RemoteProtocolError("server disconnected", request=r),
    ], ids=["connect", "read-timeout", "read-error", "disconnect"])
    def test_get_is_retried_on_network_failures(self, fake, make_exception):
        fake.fail_next("GET", r"^/offers$", exception=make_exception, times=2)
        assert len(run(fake.transport(), offers)) == len(fake.offers)
        assert count(fake, "GET", "/offers") == 3

    def test_get_gives_up_after_max_retries(self, fake):
        fake.fail_next("GET", r"^/offers$", status=503, body="maintenance", times=50)
        with pytest.raises(KeitaroServerError):
            run(fake.transport(), offers, max_retries=2)
        assert count(fake, "GET", "/offers") == 3, "1 попытка + 2 повтора, не больше"

    def test_zero_retries_means_single_attempt(self, fake):
        fake.fail_next("GET", r"^/offers$", status=503, times=50)
        with pytest.raises(KeitaroServerError):
            run(fake.transport(), offers, max_retries=0)
        assert count(fake, "GET", "/offers") == 1

    def test_plain_500_is_not_retried(self, fake):
        fake.fail_next("GET", r"^/offers$", status=500, body="boom", times=50)
        with pytest.raises(KeitaroServerError):
            run(fake.transport(), offers)
        assert count(fake, "GET", "/offers") == 1, "500 — не временный сбой, долбить бессмысленно"

    def test_unreachable_tracker_is_explained_without_stack_trace_words(self, fake):
        fake.fail_next("GET", r"^/offers$", times=50,
                       exception=lambda r: httpx.ConnectError("[Errno 111] refused", request=r))
        with pytest.raises(KeitaroNetworkError) as caught:
            run(fake.transport(), offers)
        assert "Нет связи с Keitaro (GET /offers): ConnectError" in caught.value.message
        assert "Errno" not in caught.value.message
        assert (caught.value.http_status, caught.value.code) == (504, "keitaro_unreachable")

    def test_get_timeout_does_not_scare_with_maybe_executed(self, fake):
        fake.fail_next("GET", r"^/offers$", times=50,
                       exception=lambda r: httpx.ReadTimeout("slow", request=r))
        with pytest.raises(KeitaroNetworkError) as caught:
            run(fake.transport(), offers)
        assert "не ответил вовремя" in caught.value.message
        assert "мог выполниться" not in caught.value.message

    def test_post_timeout_is_not_retried_and_warns_it_may_have_worked(self, fake):
        fake.fail_next("POST", r"^/campaigns$", after_effect=True, times=50,
                       exception=lambda r: httpx.ReadTimeout("slow", request=r))
        with pytest.raises(KeitaroNetworkError) as caught:
            run(fake.transport(), lambda c: c.create_campaign({"name": "Once", "alias": "once"}))
        assert "мог выполниться" in caught.value.message
        assert count(fake, "POST", "/campaigns") == 1
        assert len(fake.campaigns) == 1, "кампания и правда создалась — повтор наплодил бы дубль"

    @pytest.mark.parametrize("status", [429, 502, 503, 504])
    def test_post_is_not_retried_on_transient_status(self, fake, status):
        fake.fail_next("POST", r"^/streams$", status=status, body="try later", times=50)
        with pytest.raises(KeitaroError):
            run(fake.transport(), lambda c: c.create_stream({"campaign_id": 1, "name": "Flow"}))
        assert count(fake, "POST", "/streams") == 1

    def test_report_build_is_post_but_retried_because_it_only_reads(self, fake):
        fake.report_rows = [{"stream_id": 1, "offer_id": 2, "clicks": 3}]
        fake.fail_next("POST", r"^/report/build$", status=502, body="bad gateway")
        fake.fail_next("POST", r"^/report/build$",
                       exception=lambda r: httpx.ReadTimeout("slow", request=r))
        report = run(fake.transport(), lambda c: c.build_report({"range": {"interval": "today"}}))
        assert report["rows"] == fake.report_rows
        assert count(fake, "POST", "/report/build") == 3

    def test_report_build_timeout_has_no_maybe_executed_warning(self, fake):
        fake.fail_next("POST", r"^/report/build$", times=50,
                       exception=lambda r: httpx.ReadTimeout("slow", request=r))
        with pytest.raises(KeitaroNetworkError) as caught:
            run(fake.transport(), lambda c: c.build_report({}))
        assert "мог выполниться" not in caught.value.message

    def test_delete_is_retried(self, fake):
        campaign_id, _, _ = fake.seed_campaign()
        fake.fail_next("DELETE", r"^/campaigns/\d+$", status=503, times=2)
        run(fake.transport(), lambda c: c.archive_campaign(campaign_id))
        assert fake.campaigns[campaign_id]["state"] == "deleted"
        assert count(fake, "DELETE", f"/campaigns/{campaign_id}") == 3

    @pytest.mark.parametrize("retry_after", ["0", "0.01", "-5", "soon",
                                             "Wed, 21 Oct 2015 07:28:00 GMT", ""])
    def test_any_retry_after_value_is_survived_and_request_repeated(self, retry_after):
        calls: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            calls.append(request)
            if len(calls) == 1:
                return httpx.Response(429, headers={"Retry-After": retry_after},
                                      json={"error": "slow down"})
            return httpx.Response(200, json=[{"id": 1}])

        assert run(handler, offers) == [{"id": 1}]
        assert len(calls) == 2

    @pytest.mark.parametrize(("retry_after", "expected_pause"), [
        ("7", 7.0), ("2.5", 2.5), ("3600", 15.0), ("soon", 0.0), (None, 0.0)])
    def test_retry_after_sets_the_pause_and_pause_is_capped(self, monkeypatch, retry_after,
                                                            expected_pause):
        pauses: list[float] = []

        async def instant_sleep(delay: float, *_: Any) -> None:
            pauses.append(delay)  # настоящей паузы нет — только запоминаем, сколько бы ждали

        monkeypatch.setattr(asyncio, "sleep", instant_sleep)
        calls: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            calls.append(request)
            headers = {"Retry-After": retry_after} if retry_after is not None else {}
            return (httpx.Response(503, headers=headers) if len(calls) == 1
                    else httpx.Response(200, json=[]))

        run(handler, offers, backoff_base=0.0)
        assert pauses == [expected_pause]

    def test_backoff_grows_exponentially_with_small_jitter(self, monkeypatch, fake):
        pauses: list[float] = []

        async def instant_sleep(delay: float, *_: Any) -> None:
            pauses.append(delay)

        monkeypatch.setattr(asyncio, "sleep", instant_sleep)
        fake.fail_next("GET", r"^/offers$", status=502, times=3)
        run(fake.transport(), offers, backoff_base=0.4, max_retries=3)
        assert len(pauses) == 3
        for pause, base in zip(pauses, (0.4, 0.8, 1.6), strict=True):
            assert base <= pause <= base * 1.25

    def test_retries_are_logged_without_the_key(self, fake, caplog):
        fake.fail_next("GET", r"^/offers$", status=502, times=2)
        with caplog.at_level(logging.WARNING, logger="app.keitaro.client"):
            run(fake.transport(), offers)
        retries = [r.getMessage() for r in caplog.records if r.name == "app.keitaro.client"]
        assert len(retries) == 2 and "попытка 1/3" in retries[0] and "попытка 2/3" in retries[1]
        assert API_KEY not in caplog.text


class TestConcurrencyLimit:
    def test_no_more_than_max_concurrency_requests_in_flight(self):
        state = {"now": 0, "peak": 0}

        async def handler(_: httpx.Request) -> httpx.Response:
            state["now"] += 1
            state["peak"] = max(state["peak"], state["now"])
            await asyncio.sleep(0.005)
            state["now"] -= 1
            return httpx.Response(200, json=[])

        async def action(client: KeitaroClient) -> None:
            await asyncio.gather(*[client.list_offers() for _ in range(9)])

        run(handler, action, max_concurrency=2)
        assert state["peak"] == 2


class TestArchive:
    def test_archive_moves_campaign_to_keitaro_archive(self, fake):
        campaign_id, _, _ = fake.seed_campaign()
        run(fake.transport(), lambda c: c.archive_campaign(campaign_id))
        assert fake.campaigns[campaign_id]["state"] == "deleted"

    def test_archive_of_missing_campaign_is_success(self, fake):
        assert run(fake.transport(), lambda c: c.archive_campaign(424242)) is None
        assert count(fake, "DELETE", "/campaigns/424242") == 1, "404 не повторяем"

    def test_archive_twice_is_success(self, fake):
        campaign_id, _, _ = fake.seed_campaign()

        async def twice(client: KeitaroClient) -> None:
            await client.archive_campaign(campaign_id)
            await client.archive_campaign(campaign_id)

        run(fake.transport(), twice)
        assert fake.campaigns[campaign_id]["state"] == "deleted"

    def test_archive_does_not_swallow_other_errors(self, fake):
        campaign_id, _, _ = fake.seed_campaign()
        fake.fail_next("DELETE", r"^/campaigns/\d+$", status=403, body={"error": "read only key"})
        with pytest.raises(KeitaroForbiddenError):
            run(fake.transport(), lambda c: c.archive_campaign(campaign_id))
        assert fake.campaigns[campaign_id]["state"] == "active"


def fill_campaigns(fake: FakeKeitaro, amount: int) -> None:
    for index in range(1, amount + 1):
        fake.campaigns[index] = {"id": index, "name": f"bulk {index}", "alias": f"bulk{index}",
                                 "state": "active", "domain_id": 4622}


def with_query_log(fake: FakeKeitaro) -> tuple[httpx.MockTransport, list[dict[str, str]]]:
    """Эмулятор + запись query-параметров (`fake.requests` хранит только метод, путь и тело)."""
    queries: list[dict[str, str]] = []
    inner = fake.transport()

    def handler(request: httpx.Request) -> httpx.Response:
        queries.append(dict(request.url.params))
        return inner.handle_request(request)

    return httpx.MockTransport(handler), queries


class TestCampaignPages:
    def test_more_than_one_page_arrives_whole_and_without_duplicates(self, fake):
        fill_campaigns(fake, 1203)
        transport, queries = with_query_log(fake)
        rows = run(transport, lambda c: c.list_campaigns())
        assert sorted(row["id"] for row in rows) == list(range(1, 1204))
        assert queries == [{"limit": "500", "offset": "0"}, {"limit": "500", "offset": "500"},
                           {"limit": "500", "offset": "1000"}]

    def test_exactly_one_full_page_costs_one_extra_empty_request(self, fake):
        fill_campaigns(fake, 500)
        transport, queries = with_query_log(fake)
        assert len(run(transport, lambda c: c.list_campaigns())) == 500
        assert [q["offset"] for q in queries] == ["0", "500"]

    def test_small_tracker_is_read_with_single_request(self, fake):
        fill_campaigns(fake, 3)
        transport, queries = with_query_log(fake)
        assert len(run(transport, lambda c: c.list_campaigns())) == 3
        assert len(queries) == 1

    def test_old_keitaro_ignoring_limit_is_read_with_single_request(self):
        everything = [{"id": i, "name": f"c{i}"} for i in range(1, 701)]
        calls: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            calls.append(request)
            return httpx.Response(200, json=everything)

        assert len(run(handler, lambda c: c.list_campaigns())) == 700
        assert len(calls) == 1

    def test_campaign_that_moved_between_pages_is_not_duplicated(self):
        def handler(request: httpx.Request) -> httpx.Response:
            offset = int(request.url.params["offset"])
            # Пока читали первую страницу, в начало списка добавили кампанию: #500 сползла
            # на вторую страницу и пришла дважды.
            ids = range(1, 501) if offset == 0 else range(500, 621)
            return httpx.Response(200, json=[{"id": i, "name": f"c{i}"} for i in ids])

        rows = run(handler, lambda c: c.list_campaigns())
        assert sorted(row["id"] for row in rows) == list(range(1, 621))

    def test_rows_without_numeric_id_are_dropped(self):
        body = [{"id": 1, "name": "ok"}, {"id": "2", "name": "string id"}, {"name": "no id"}]
        assert run(always(200, body=body), lambda c: c.list_campaigns()) == [{"id": 1, "name": "ok"}]

    @pytest.mark.xfail(strict=True, reason=(
        "app/keitaro/client.py:list_campaigns — если трекер игнорирует limit/offset (комментарий "
        "в коде сам допускает такие версии) и кампаний ровно 500, клиент делает 100 одинаковых "
        "запросов (_MAX_PAGES). Нужен выход из цикла, когда страница не принесла новых id."))
    def test_tracker_ignoring_paging_with_exactly_500_campaigns_is_not_hammered(self):
        page = [{"id": i, "name": f"c{i}"} for i in range(1, 501)]
        calls: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            calls.append(request)
            return httpx.Response(200, json=page)

        assert len(run(handler, lambda c: c.list_campaigns())) == 500
        assert len(calls) <= 2


def leak_scenarios() -> list[Any]:
    def timeout(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("slow", request=request)

    def refused(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused", request=request)

    create = lambda c: c.create_campaign({"name": "x", "alias": "x"})  # noqa: E731
    return [
        pytest.param(always(401, body={"error": "Unauthorized"}), offers, id="401"),
        pytest.param(always(403, body={"error": "Forbidden"}), offers, id="403"),
        pytest.param(always(403, body=FakeKeitaro.cloudflare_body()), offers, id="cloudflare"),
        pytest.param(always(404, body="not found"), offers, id="404"),
        pytest.param(always(422, body={"alias": ["taken"]}), create, id="422"),
        pytest.param(always(429), offers, id="429"),
        pytest.param(always(500, body="boom"), offers, id="500"),
        pytest.param(always(200, body="<html>"), offers, id="not-json"),
        pytest.param(timeout, create, id="post-timeout"),
        pytest.param(refused, offers, id="connect-error"),
    ]


class TestKeyNeverLeaks:
    @pytest.mark.parametrize(("handler", "action"), leak_scenarios())
    def test_no_error_text_contains_the_key(self, handler, action):
        with pytest.raises(KeitaroError) as caught:
            run(handler, action, api_key=LONG_KEY)
        error = caught.value
        for text in (str(error), repr(error), error.message, json.dumps(error.to_dict())):
            assert LONG_KEY not in text

    def test_client_object_does_not_print_the_key(self):
        async def action(client: KeitaroClient) -> str:
            return f"{client!r} {client!s}"

        assert LONG_KEY not in run(always(200, body=[]), action, api_key=LONG_KEY)

    @pytest.mark.xfail(strict=True, reason=(
        "app/keitaro/client.py:_error_from_response — текст ответа трекера вставляется в "
        "сообщение как есть. Если прокси/отладочная страница перед Keitaro вернёт в теле "
        "заголовки запроса, ключ уйдёт во фронтенд и в журнал операций (колонка error). "
        "Docstring модуля обещает, что ключ в тексты ошибок не попадает: перед сборкой "
        "сообщения вырезать api_key из upstream-текста."))
    @pytest.mark.parametrize(("status", "body"), [
        (403, {"error": f"Invalid Api-Key {LONG_KEY}"}),
        (404, f"No route. Request headers: Api-Key: {LONG_KEY}"),
        (422, {"error": f"debug dump: {LONG_KEY}"}),
    ])
    def test_key_echoed_by_upstream_is_cut_out_of_the_message(self, status, body):
        with pytest.raises(KeitaroError) as caught:
            run(always(status, body=body), offers, api_key=LONG_KEY)
        assert LONG_KEY not in caught.value.message
