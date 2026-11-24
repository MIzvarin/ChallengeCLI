"""Anonymous, allowlisted Streamable HTTP MCP access to VkusVill."""

import json
import math
import re
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from time import sleep
from urllib.parse import urlsplit

import httpx

MCP_URL = "https://mcp.vkusvill.ru/mcp"
PROTOCOL = "2025-03-26"
MCP_ERROR = "Не удалось получить товары ВкусВилла. Рекомендуемый рацион доступен без корзины."
TOOLS = {"vkusvill_products_search", "vkusvill_product_details", "vkusvill_cart_link_create"}
SEARCH_BACKOFF = (0.5, 1.0)
RETRYABLE_STATUS = {408, 429, 500, 502, 503, 504}
MAX_RETRY_AFTER = 5


def search_retry(error, backoff):
    if isinstance(error, httpx.HTTPStatusError):
        response = error.response
        if response.status_code not in RETRYABLE_STATUS:
            return None
        delay = backoff
        header = response.headers.get("Retry-After", "").strip()
        if header:
            try:
                seconds = (
                    int(header)
                    if header.isascii() and header.isdecimal()
                    else (
                        parsedate_to_datetime(header).astimezone(timezone.utc)
                        - datetime.now(timezone.utc)
                    ).total_seconds()
                )
                delay = max(backoff, seconds)
            except (ValueError, OverflowError):
                pass
        if delay > MAX_RETRY_AFTER:
            return None
        reason = {408: "timeout", 429: "rate_limit"}.get(response.status_code, "server")
        return reason, delay
    if isinstance(error, httpx.TimeoutException):
        return "timeout", backoff
    if isinstance(error, (httpx.NetworkError, httpx.RemoteProtocolError)):
        return "network", backoff
    return None


def trusted_link(value):
    if (
        not isinstance(value, str)
        or len(value) > 2000
        or any(c.isspace() or not c.isprintable() for c in value)
    ):
        raise ValueError(MCP_ERROR)
    parsed = urlsplit(value)
    if (
        parsed.scheme != "https"
        or parsed.hostname != "vkusvill.ru"
        or parsed.username
        or parsed.password
        or parsed.port not in (None, 443)
        or any(c in value for c in "<>\\")
    ):
        raise ValueError(MCP_ERROR)
    return value


def validate_arguments(name, arguments):
    if name not in TOOLS or not isinstance(arguments, dict):
        raise ValueError(MCP_ERROR)
    if name == "vkusvill_products_search":
        if set(arguments) != {"q", "page", "limit", "mode", "sort"}:
            raise ValueError(MCP_ERROR)
        if (
            not isinstance(arguments["q"], str)
            or not re.fullmatch(r"[а-яёА-ЯЁ][а-яёА-ЯЁ -]{0,79}", arguments["q"])
            or type(arguments["page"]) is not int
            or arguments["page"] != 1
            or type(arguments["limit"]) is not int
            or arguments["limit"] != 5
            or arguments["mode"] != "full"
            or arguments["sort"] not in {"popularity", "price_asc"}
        ):
            raise ValueError(MCP_ERROR)
    elif name == "vkusvill_product_details":
        if (
            set(arguments) != {"id"}
            or type(arguments["id"]) is not int
            or not 1 <= arguments["id"] <= 999999999
        ):
            raise ValueError(MCP_ERROR)
    else:
        products = arguments.get("products")
        if (
            set(arguments) != {"products"}
            or not isinstance(products, list)
            or not 1 <= len(products) <= 100
        ):
            raise ValueError(MCP_ERROR)
        seen = set()
        for item in products:
            if not isinstance(item, dict) or set(item) != {"id", "q"}:
                raise ValueError(MCP_ERROR)
            if (
                type(item["id"]) is not int
                or not 1 <= item["id"] <= 999999999
                or item["id"] in seen
            ):
                raise ValueError(MCP_ERROR)
            if (
                type(item["q"]) not in (int, float)
                or not math.isfinite(item["q"])
                or not 0.01 <= item["q"] <= 40
            ):
                raise ValueError(MCP_ERROR)
            seen.add(item["id"])


class VkusvillClient:
    def __init__(self, transport=None, *, audit=None):
        self.transport = transport
        self.audit = audit

    def _rpc(self, client, method, params, request_id=None, session=None):
        headers = {
            "Accept": "application/json, text/event-stream",
            "MCP-Protocol-Version": PROTOCOL,
        }
        if session:
            headers["Mcp-Session-Id"] = session
        payload = {"jsonrpc": "2.0", "method": method, "params": params}
        if request_id is not None:
            payload["id"] = request_id
        with client.stream("POST", MCP_URL, headers=headers, json=payload) as response:
            response.raise_for_status()
            content = bytearray()
            for chunk in response.iter_bytes():
                content.extend(chunk)
                if len(content) > 1_000_000:
                    raise ValueError(MCP_ERROR)
            if request_id is None:
                return None, response.headers.get("mcp-session-id")
            text = content.decode("utf-8")
            if "text/event-stream" in response.headers.get("content-type", ""):
                messages = []
                for event in text.replace("\r\n", "\n").split("\n\n"):
                    data = "\n".join(
                        line[5:].lstrip() for line in event.splitlines() if line.startswith("data:")
                    )
                    if data:
                        messages.append(json.loads(data))
                matches = [
                    item
                    for item in messages
                    if isinstance(item, dict) and item.get("id") == request_id
                ]
                if len(matches) != 1:
                    raise ValueError(MCP_ERROR)
                value = matches[0]
            else:
                value = json.loads(text)
            if (
                not isinstance(value, dict)
                or value.get("jsonrpc") != "2.0"
                or type(value.get("id")) is not int
                or value.get("id") != request_id
                or "error" in value
                or not isinstance(value.get("result"), dict)
            ):
                raise ValueError(MCP_ERROR)
            return value["result"], response.headers.get("mcp-session-id")

    def call(self, name, arguments):
        validate_arguments(name, arguments)
        backoffs = SEARCH_BACKOFF if name == "vkusvill_products_search" else ()
        for attempt, backoff in enumerate((*backoffs, None), start=1):
            try:
                return self._call_once(name, arguments)
            except httpx.HTTPError as error:
                retry = search_retry(error, backoff) if backoff is not None else None
                if retry is None:
                    raise ValueError(MCP_ERROR) from None
                reason, delay = retry
                if self.audit:
                    self.audit("mcp_search_retry", {"attempt": attempt + 1, "reason": reason})
                sleep(delay)
            except (ValueError, KeyError, TypeError, AttributeError, RecursionError):
                raise ValueError(MCP_ERROR) from None

    def _call_once(self, name, arguments):
        with httpx.Client(
            timeout=httpx.Timeout(30, connect=10),
            transport=self.transport,
            follow_redirects=False,
            trust_env=False,
            headers={"User-Agent": "fitagent/0.1.0"},
        ) as client:
            initialized, session = self._rpc(
                client,
                "initialize",
                {
                    "protocolVersion": PROTOCOL,
                    "capabilities": {},
                    "clientInfo": {"name": "fitagent", "version": "0.1.0"},
                },
                1,
            )
            if initialized.get("protocolVersion") != PROTOCOL:
                raise ValueError(MCP_ERROR)
            self._rpc(client, "notifications/initialized", {}, session=session)
            result, _ = self._rpc(
                client, "tools/call", {"name": name, "arguments": arguments}, 2, session
            )
        if result.get("isError") or not isinstance(result.get("content"), list):
            raise ValueError(MCP_ERROR)
        texts = [item["text"] for item in result["content"] if item.get("type") == "text"]
        data = json.loads("\n".join(texts))
        if (
            not isinstance(data, dict)
            or data.get("ok") is not True
            or not isinstance(data.get("data"), dict)
        ):
            raise ValueError(MCP_ERROR)
        return data["data"]

    def search(self, query, cheaper=False):
        data = self.call(
            "vkusvill_products_search",
            {
                "q": query,
                "page": 1,
                "limit": 5,
                "mode": "full",
                "sort": "price_asc" if cheaper else "popularity",
            },
        )
        if not isinstance(data.get("items"), list):
            raise ValueError(MCP_ERROR)
        return data["items"]

    def cart_link(self, products):
        return trusted_link(
            self.call("vkusvill_cart_link_create", {"products": products}).get("link")
        )
