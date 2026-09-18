"""Эмулятор Admin API Keitaro для тестов.

Повторяет поведение настоящего трекера, снятое живыми запросами (docs/KEITARO_API_NOTES.md),
включая его «причуды»:

* `PUT /streams/{id}` — частичное обновление; массив `offers` заменяет набор целиком,
  привязка к тому же офферу сохраняет свой id, новая получает следующий;
* сумма долей, существование оффера/группы/домена и коды стран НЕ проверяются;
* дубль оффера в одном потоке молча схлопывается;
* 404 приходит обычным текстом, 422 — словарём {поле: [сообщения]}, 401 — {"error": ...}.

Сбои включаются через `fail_next(...)`: коды ответа, обрыв связи, таймаут,
страница Cloudflare, не-JSON.
"""

from __future__ import annotations

import copy
import itertools
import json
import re
from collections.abc import Callable
from typing import Any

import httpx

API_KEY = "fake-key"  # не секрет: ключ эмулятора


class FakeKeitaro:
    def __init__(self) -> None:
        self._ids = itertools.count(1000)
        self._binding_ids = itertools.count(900000)
        self.requests: list[tuple[str, str, Any]] = []
        self._failures: list[dict[str, Any]] = []
        self.domains_visible = True
        self.groups = [{"id": 22, "name": "FORTESTS", "position": 1, "type": "campaigns"}]
        self.sources = [
            {
                "id": 33,
                "name": "FORTESTS",
                "state": "active",
                "template_name": "facebook",
                "parameters": {
                    "creative_id": {"name": "utm_creative", "placeholder": "{{ad.name}}"},
                    "sub_id_1": {"name": "utm_placement", "placeholder": "{{placement}}",
                                 "alias": "Placement"},
                },
            }
        ]
        self.domains = [{"id": 11, "name": "https://in.example.test/", "state": "active"}]
        self.offers: dict[int, dict[str, Any]] = {}
        self.campaigns: dict[int, dict[str, Any]] = {}
        self.streams: dict[int, dict[str, Any]] = {}
        self.report_rows: list[dict[str, Any]] = []
        # Необязательный генератор строк отчёта: получает тело запроса /report/build.
        self.report_handler: Callable[[dict[str, Any]], list[dict[str, Any]]] | None = None
        for offer_id, name in [
            (3749, "Miaflow [BEAUTY-RO-BE_0009] [pl - - spin ro -] 2. Основной, Румыния JS"),
            (3717, "Miaflow [BEAUTY-RO-BE_0008] [pl - - prize ro -] 1. Основной, Румыния JS"),
            (11111, "11111 FitoMishki [Weight Loss-CO-DT_0309] [pl es -]"),
            (11112, "11112 Oxys [BEAUTY-CL-BE_0155] [pl es -]"),
            (13972, "another test offer 2"),
        ]:
            self.add_offer(offer_id, name)

    # ------------------------------------------------------------------ наполнение

    def add_offer(self, offer_id: int, name: str, state: str = "active") -> dict[str, Any]:
        offer = {
            "id": offer_id, "name": name, "state": state, "group_id": 978,
            "affiliate_network_id": 62, "country": ["Romania"], "offer_type": "local",
            "payout_value": 0, "payout_currency": "USD", "payout_type": "CPA",
        }
        self.offers[offer_id] = offer
        return offer

    def seed_campaign(
        self, name: str = "campaign 2", offers: list[tuple[int, int]] | None = None
    ) -> tuple[int, int, int]:
        """Эталон из видео: Flow 1 (AU → google) и Flow 2 с офферами. -> (campaign, flow1, flow2)."""
        campaign = self._create_campaign(
            {"name": name, "alias": f"alias{next(self._ids)}", "group_id": 22,
             "traffic_source_id": 33, "domain_id": 11}
        )
        flow1 = self._create_stream(
            {"campaign_id": campaign["id"], "name": "Flow 1", "position": 1, "schema": "redirect",
             "action_type": "http", "action_payload": "https://google.com",
             "filters": [{"name": "country", "mode": "accept", "payload": ["AU"]}]}
        )
        flow2 = self._create_stream(
            {"campaign_id": campaign["id"], "name": "Flow 2", "position": 2, "schema": "landings",
             "action_type": "http",
             "offers": [{"offer_id": o, "share": s, "state": "active"}
                        for o, s in (offers or [(3749, 33), (3717, 33), (11111, 34)])]}
        )
        return campaign["id"], flow1["id"], flow2["id"]

    def stream_offers(self, stream_id: int) -> list[tuple[int, int]]:
        """[(offer_id, share)] — как это видно в админке Keitaro."""
        return [(o["offer_id"], o["share"]) for o in self.streams[stream_id]["offers"]]

    def set_stream_offers(self, stream_id: int, offers: list[tuple[int, int]]) -> None:
        """Правка «руками в Keitaro» в обход AdRobot."""
        self._apply_offers(self.streams[stream_id], [
            {"offer_id": o, "share": s, "state": "active"} for o, s in offers])

    # ------------------------------------------------------------------ сбои

    def fail_next(
        self,
        method: str,
        path_pattern: str,
        *,
        status: int | None = None,
        body: Any = None,
        exception: Callable[[httpx.Request], Exception] | None = None,
        times: int = 1,
        skip: int = 0,
        after_effect: bool = False,
    ) -> None:
        """Следующие `times` запросов, подходящих под метод и regex пути, завершатся сбоем.

        `skip` — сколько подходящих запросов пропустить, прежде чем начать ломать.
        `after_effect=True` — запрос СНАЧАЛА выполняется, и только потом «рвётся связь»:
        так выглядит таймаут на создании, когда сущность на самом деле создалась.
        """
        self._failures.append({
            "method": method.upper(), "pattern": re.compile(path_pattern), "status": status,
            "body": body, "exception": exception, "times": times, "skip": skip,
            "after_effect": after_effect,
        })

    @staticmethod
    def cloudflare_body() -> dict[str, Any]:
        return {"title": "Error 1010: Access denied", "status": 403, "error_code": 1010,
                "error_name": "browser_signature_banned", "cloudflare_error": True}

    # ------------------------------------------------------------------ транспорт

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self._handle)

    def _handle(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path.removeprefix("/admin_api/v1")
        method = request.method.upper()
        body = json.loads(request.content) if request.content else None
        self.requests.append((method, path, body))

        failure = self._match_failure(method, path)
        if failure and not failure["after_effect"]:
            return self._fail(failure, request)

        if request.headers.get("Api-Key") != API_KEY:
            return httpx.Response(401, json={"error": "Unauthorized"})
        response = self._route(method, path, dict(request.url.params), body)
        if failure:
            return self._fail(failure, request)
        return response

    def _match_failure(self, method: str, path: str) -> dict[str, Any] | None:
        for failure in self._failures:
            if failure["method"] == method and failure["pattern"].search(path):
                if failure["skip"] > 0:
                    failure["skip"] -= 1
                    return None
                failure["times"] -= 1
                if failure["times"] <= 0:
                    self._failures.remove(failure)
                return failure
        return None

    @staticmethod
    def _fail(failure: dict[str, Any], request: httpx.Request) -> httpx.Response:
        if failure["exception"]:
            raise failure["exception"](request)
        body = failure["body"]
        if isinstance(body, (dict, list)):
            return httpx.Response(failure["status"], json=body)
        return httpx.Response(failure["status"], text=body or "")

    # ------------------------------------------------------------------ маршруты

    def _route(self, method: str, path: str, params: dict[str, str], body: Any) -> httpx.Response:
        if method == "GET" and path == "/groups":
            return httpx.Response(200, json=[g for g in self.groups
                                             if g["type"] == params.get("type", "campaigns")])
        if method == "GET" and path == "/traffic_sources":
            return httpx.Response(200, json=self.sources)
        if method == "GET" and path == "/domains":
            return httpx.Response(200, json=self.domains if self.domains_visible else [])
        if method == "GET" and path == "/offers":
            return httpx.Response(200, json=list(self.offers.values()))
        if method == "POST" and path == "/report/build":
            rows = self.report_handler(body or {}) if self.report_handler else self.report_rows
            return httpx.Response(200, json={"rows": rows, "total": len(rows)})
        if path == "/campaigns":
            if method == "GET":
                rows = [self._public(c) for c in self.campaigns.values() if c["state"] != "deleted"]
                offset = int(params.get("offset", 0))
                limit = int(params.get("limit", len(rows) or 1))
                return httpx.Response(200, json=rows[offset:offset + limit])
            if method == "POST":
                return self._post_campaign(body or {})
        match = re.fullmatch(r"/campaigns/(\d+)(/streams)?", path)
        if match:
            campaign = self.campaigns.get(int(match.group(1)))
            if not campaign or campaign["state"] == "deleted":
                return httpx.Response(
                    404, text=f"Traffic\\Model\\Campaign #{match.group(1)} not found")
            if match.group(2) and method == "GET":
                rows = [s for s in self.streams.values()
                        if s["campaign_id"] == campaign["id"] and s["state"] != "deleted"]
                return httpx.Response(200, json=copy.deepcopy(sorted(rows, key=lambda s: s["position"])))
            if method == "GET":
                return httpx.Response(200, json=self._public(campaign))
            if method == "DELETE":
                campaign["state"] = "deleted"
                return httpx.Response(200, json=[self._public(campaign)])
        if method == "POST" and path == "/streams":
            if (body or {}).get("campaign_id") not in self.campaigns:
                return httpx.Response(422, json={"campaign_id": ["Campaign not found"]})
            return httpx.Response(200, json=copy.deepcopy(self._create_stream(body or {})))
        match = re.fullmatch(r"/streams/(\d+)", path)
        if match:
            stream = self.streams.get(int(match.group(1)))
            if not stream or stream["state"] == "deleted":
                return httpx.Response(404, text=f"Traffic\\Model\\Stream #{match.group(1)} not found")
            if method == "GET":
                return httpx.Response(200, json=copy.deepcopy(stream))
            if method == "PUT":
                for key, value in (body or {}).items():
                    if key == "offers":
                        self._apply_offers(stream, value)
                    elif key not in ("id", "campaign_id"):
                        stream[key] = value
                return httpx.Response(200, json=copy.deepcopy(stream))
            if method == "DELETE":
                stream["state"] = "deleted"
                return httpx.Response(200, text="")
        return httpx.Response(404, text=f"No route {method} {path}")

    # ------------------------------------------------------------------ сущности

    def _post_campaign(self, body: dict[str, Any]) -> httpx.Response:
        errors: dict[str, list[str]] = {}
        if not str(body.get("name") or "").strip():
            errors["name"] = ["Name is required"]
        alias = str(body.get("alias") or "").strip()
        if not alias:
            errors["alias"] = ["Alias is required"]
        elif any(c["alias"] == alias for c in self.campaigns.values()):
            errors["alias"] = ["Alias has already used"]
        if errors:
            return httpx.Response(422, json=errors)
        return httpx.Response(200, json=self._public(self._create_campaign(body)))

    def _create_campaign(self, body: dict[str, Any]) -> dict[str, Any]:
        campaign = {
            "id": next(self._ids), "type": "position", "state": "active", "cost_type": "CPC",
            "cost_currency": "USD", "token": "fake-token", "parameters": {},
            "domain": None, "created_at": "2026-01-01 00:00:00", "updated_at": "2026-01-01 00:00:00",
            **copy.deepcopy(body),
        }
        group = next((g for g in self.groups if g["id"] == campaign.get("group_id")), None)
        campaign["group"] = group["name"] if group else ""
        self.campaigns[campaign["id"]] = campaign
        return campaign

    @staticmethod
    def _public(campaign: dict[str, Any]) -> dict[str, Any]:
        return copy.deepcopy(campaign)

    def _create_stream(self, body: dict[str, Any]) -> dict[str, Any]:
        stream = {
            "id": next(self._ids), "type": "regular", "name": "", "position": 1, "state": "active",
            "action_type": "http", "action_payload": "", "schema": "landings", "weight": 100,
            "collect_clicks": True, "filter_or": False, "offer_selection": "before_click",
            "filters": [], "triggers": [], "landings": [], "offers": [],
            **{k: copy.deepcopy(v) for k, v in body.items() if k != "offers"},
        }
        for index, flt in enumerate(stream["filters"]):
            flt.update({"id": stream["id"] * 10 + index, "stream_id": stream["id"]})
        self.streams[stream["id"]] = stream
        self._apply_offers(stream, body.get("offers") or [])
        return stream

    def _apply_offers(self, stream: dict[str, Any], offers: list[dict[str, Any]]) -> None:
        previous = {o["offer_id"]: o for o in stream["offers"]}
        result: list[dict[str, Any]] = []
        seen: set[int] = set()
        for row in offers:
            offer_id = row["offer_id"]
            if offer_id in seen:  # дубль молча схлопывается, как в настоящем Keitaro
                continue
            seen.add(offer_id)
            binding = previous.get(offer_id) or {
                "id": next(self._binding_ids), "stream_id": stream["id"], "offer_id": offer_id}
            binding.update({"share": row.get("share", 0), "state": row.get("state", "active")})
            result.append(binding)
        stream["offers"] = result
