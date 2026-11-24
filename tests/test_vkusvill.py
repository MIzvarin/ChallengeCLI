"""MCP protocol and privacy regressions using HTTPX mock transport only."""

import copy
import json
import unittest
from unittest.mock import Mock, call, patch

import httpx

from fitagent.vkusvill import MCP_ERROR, MCP_URL, PROTOCOL, VkusvillClient, trusted_link


def tool_result(data):
    return {
        "content": [{"type": "text", "text": json.dumps({"ok": True, "data": data})}],
        "isError": False,
    }


def transport_for(data, calls, sse=False):
    def handler(request):
        body = json.loads(request.content)
        calls.append((request, body))
        assert str(request.url) == MCP_URL
        assert "authorization" not in request.headers
        assert "cookie" not in request.headers
        assert "x-opencode-session" not in request.headers
        if body["method"] == "initialize":
            result = {
                "protocolVersion": PROTOCOL,
                "capabilities": {},
                "serverInfo": {"name": "VV", "version": "0"},
            }
        elif body["method"] == "notifications/initialized":
            assert request.headers["mcp-session-id"] == "test-session"
            assert "id" not in body
            return httpx.Response(202)
        else:
            assert request.headers["mcp-session-id"] == "test-session"
            result = data
        value = {"jsonrpc": "2.0", "id": body["id"], "result": result}
        if sse:
            text = ": heartbeat\r\nevent: message\r\ndata: " + json.dumps(value) + "\r\n\r\n"
            return httpx.Response(
                200,
                text=text,
                headers={"content-type": "text/event-stream", "mcp-session-id": "test-session"},
            )
        return httpx.Response(200, json=value, headers={"mcp-session-id": "test-session"})

    return httpx.MockTransport(handler)


def fails(callback):
    try:
        callback()
    except ValueError as exc:
        assert str(exc) == MCP_ERROR
    else:
        raise AssertionError("Invalid MCP response accepted")


def test_anonymous_mcp_handshake_search_and_cart_have_only_allowed_fields():
    for sse in (False, True):
        calls = []
        client = VkusvillClient(transport_for(tool_result({"items": [{"id": 1}]}), calls, sse))
        assert client.search("гречка", cheaper=True) == [{"id": 1}]
        assert [body["method"] for _, body in calls] == [
            "initialize",
            "notifications/initialized",
            "tools/call",
        ]
        assert calls[-1][1]["params"] == {
            "name": "vkusvill_products_search",
            "arguments": {
                "q": "гречка",
                "page": 1,
                "limit": 5,
                "mode": "full",
                "sort": "price_asc",
            },
        }
        calls = []
        client = VkusvillClient(
            transport_for(
                tool_result({"link": "https://vkusvill.ru/mobile?basket&share=123"}), calls, sse
            )
        )
        assert "share=123" in client.cart_link([{"id": 17681, "q": 1}])
        assert calls[-1][1]["params"]["arguments"] == {"products": [{"id": 17681, "q": 1}]}


def test_personal_tools_extra_fields_and_invalid_quantities_never_reach_network():
    calls = []
    client = VkusvillClient(transport_for(tool_result({}), calls))
    for name in ("vkusvill_orders_history", "vkusvill_discounts_personal", "unknown"):
        fails(lambda: client.call(name, {}))
    fails(lambda: client.call("vkusvill_products_search", {"q": "гречка", "profile": "PRIVATE"}))
    for query in (
        "",
        "гречка для 30 лет",
        "https://evil.test",
        "user@example.test",
        "гречка\nPRIVATE",
    ):
        fails(lambda: client.search(query))
    for products in (
        [],
        [{"id": 1, "q": 0}],
        [{"id": 1, "q": 41}],
        [{"id": True, "q": 1}],
        [{"id": 1, "q": float("nan")}],
        [{"id": 1, "q": 1, "profile": "PRIVATE"}],
        [{"id": 1, "q": 1}, {"id": 1, "q": 2}],
    ):
        fails(lambda: client.cart_link(products))
    assert calls == []


def test_permanent_mcp_errors_are_russian_without_external_text_and_without_retries():
    for data in (
        {"isError": True, "content": [{"type": "text", "text": "PRIVATE exception"}]},
        {"content": [{"type": "text", "text": "PRIVATE not JSON"}]},
        tool_result({}),
        {"content": [None]},
        {"content": [{"type": "text", "text": '{"ok": false, "message": "PRIVATE"}'}]},
    ):
        calls = []
        client = VkusvillClient(transport_for(data, calls))
        fails(lambda: client.search("гречка"))
        assert len(calls) == 3
    for status in (400, 401, 403, 404, 422, 501, 302):
        requests = []

        def handler(request):
            requests.append(request)
            return httpx.Response(status, text="PRIVATE", headers={"location": "https://evil.test"})

        client = VkusvillClient(httpx.MockTransport(handler))
        fails(lambda: client.search("гречка"))
        assert len(requests) == 1


def test_wrong_rpc_id_version_oversized_and_network_errors_are_rejected():
    envelopes = [
        {"jsonrpc": "2.0", "id": 9, "result": {}},
        {"jsonrpc": "2.0", "id": True, "result": {}},
        {"jsonrpc": "2.0", "id": 1, "result": {"protocolVersion": "unknown"}},
        {"jsonrpc": "2.0", "id": 1, "error": {"message": "PRIVATE"}},
    ]
    for envelope in envelopes:
        client = VkusvillClient(
            httpx.MockTransport(lambda request: httpx.Response(200, json=envelope))
        )
        fails(lambda: client.search("гречка"))
    client = VkusvillClient(
        httpx.MockTransport(lambda request: httpx.Response(200, content=b"x" * 1_000_001))
    )
    fails(lambda: client.search("гречка"))


def test_search_recovers_from_timeout_and_rate_limit_in_a_fresh_session():
    calls = []
    audit = Mock()
    transport = transport_for(tool_result({"items": [{"id": 1}]}), calls)
    attempts = []

    def handler(request):
        body = json.loads(request.content)
        if body["method"] == "initialize":
            assert "mcp-session-id" not in request.headers
        elif body["method"] == "tools/call":
            assert request.headers["mcp-session-id"] == "test-session"
            attempts.append(body["params"])
            if len(attempts) == 1:
                raise httpx.ReadTimeout("PRIVATE", request=request)
            if len(attempts) == 2:
                return httpx.Response(429, headers={"Retry-After": "2"})
        return transport.handle_request(request)

    client = VkusvillClient(httpx.MockTransport(handler), audit=audit)
    with patch("fitagent.vkusvill.sleep") as sleep:
        assert client.search("гречка", cheaper=True) == [{"id": 1}]
    assert attempts == [attempts[0]] * 3
    assert sum(body["method"] == "initialize" for _, body in calls) == 3
    assert sleep.call_args_list == [call(0.5), call(2)]
    assert audit.call_args_list == [
        call("mcp_search_retry", {"attempt": 2, "reason": "timeout"}),
        call("mcp_search_retry", {"attempt": 3, "reason": "rate_limit"}),
    ]


def test_search_stops_after_two_retries_and_respects_long_retry_after():
    cases = (
        (httpx.Response(503), 3, [call(0.5), call(1.0)], "server"),
        (httpx.Response(503, headers={"Retry-After": "60"}), 1, [], None),
        (httpx.ConnectError("PRIVATE"), 3, [call(0.5), call(1.0)], "network"),
    )
    for outcome, limit, delays, reason in cases:
        attempts = []

        def handler(request):
            attempts.append(json.loads(request.content)["method"])
            if isinstance(outcome, Exception):
                raise outcome
            return httpx.Response(outcome.status_code, headers=outcome.headers)

        audit = Mock()
        client = VkusvillClient(httpx.MockTransport(handler), audit=audit)
        with patch("fitagent.vkusvill.sleep") as sleep:
            fails(lambda: client.search("гречка"))
        assert attempts == ["initialize"] * limit
        assert sleep.call_args_list == delays
        assert audit.call_args_list == [
            call("mcp_search_retry", {"attempt": attempt, "reason": reason})
            for attempt in range(2, limit + 1)
        ]


def test_cart_creation_does_not_retry_after_a_timeout():
    calls = []
    audit = Mock()
    transport = transport_for(tool_result({}), calls)
    cart_calls = []

    def handler(request):
        body = json.loads(request.content)
        if body["method"] == "tools/call":
            cart_calls.append(body["params"]["name"])
            raise httpx.ReadTimeout("PRIVATE", request=request)
        return transport.handle_request(request)

    client = VkusvillClient(httpx.MockTransport(handler), audit=audit)
    with patch("fitagent.vkusvill.sleep") as sleep:
        fails(lambda: client.cart_link([{"id": 1, "q": 1}]))
    assert cart_calls == ["vkusvill_cart_link_create"]
    audit.assert_not_called()
    sleep.assert_not_called()


def test_untrusted_or_malformed_links_are_rejected():
    for link in (
        None,
        "http://vkusvill.ru/mobile",
        "https://evil.test/mobile",
        "https://vkusvill.ru.evil.test",
        "https://user:pass@vkusvill.ru/",
        "https://vkusvill.ru:444/",
        "https://vkusvill.ru/\nPRIVATE",
        "https://vkusvill.ru/>PRIVATE",
        "https://vkusvill.ru/\x1bPRIVATE",
    ):
        fails(lambda: trusted_link(link))
    assert trusted_link("https://vkusvill.ru/mobile?basket&share=123").endswith("share=123")
    calls = []
    client = VkusvillClient(transport_for(tool_result({"link": "https://evil.test/"}), calls))
    fails(lambda: client.cart_link([{"id": 1, "q": 1}]))


def test_structured_product_response_does_not_accept_nonlist_items():
    data = tool_result({"items": "PRIVATE"})
    calls = []
    client = VkusvillClient(transport_for(copy.deepcopy(data), calls))
    fails(lambda: client.search("гречка"))


def load_tests(loader, tests, pattern):
    return unittest.TestSuite(
        unittest.FunctionTestCase(value)
        for name, value in globals().items()
        if name.startswith("test_") and callable(value)
    )
