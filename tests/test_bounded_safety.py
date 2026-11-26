"""Exact rule coverage prevents unbounded safety responses, without dropping restrictions."""

import json
import tempfile
import unittest
from unittest.mock import patch

from support import safe_response, safety_json
from test_memory import error, fixture, real_client, responses_reply

from fitagent.provider import DEFAULT_MODEL, MEMORY_OUTPUT_LIMIT
from fitagent.safety_schema import (
    SAFETY_ERROR,
    SAFETY_SCHEMA,
    SafetyValidationError,
    parse_safety,
    safety_request_schema,
)
from fitagent.storage import Storage


def test_runtime_schema_has_exact_dynamic_coverage_instead_of_an_unbounded_array():
    for count in (1, 4, 65, 130):
        required = ["system", *(f"fact:{index}" for index in range(1, count))]
        schema = safety_request_schema(required)
        checks = schema["properties"]["checks"]
        assert checks["minItems"] == checks["maxItems"] == count
        assert checks["items"]["properties"]["rule_id"]["enum"] == required
        assert checks["items"]["properties"]["description"]["maxLength"] == 80
        assert schema["properties"]["description"]["maxLength"] == 700
        assert parse_safety(safety_json(required), iter(required), exact=True)["status"] == "allow"
    assert "maxItems" not in SAFETY_SCHEMA["properties"]["checks"]
    assert "enum" not in SAFETY_SCHEMA["properties"]["checks"]["items"]["properties"]["rule_id"]


def test_real_client_sends_the_bounded_schema_without_extra_requests_or_larger_token_limits():
    with tempfile.TemporaryDirectory() as directory:
        store = Storage(directory)
        client = real_client(store)
        required = ["system", "fact:1", "fact:4", "fact:5"]
        messages = [{"role": "user", "content": json.dumps({"required_rule_ids": required})}]
        with patch("fitagent.provider.httpx.Client") as factory:
            transport = factory.return_value.__enter__.return_value
            transport.post.return_value = responses_reply(safety_json(required))
            raw = client.generate_safety(messages, "response")
            assert parse_safety(raw, required, exact=True)["status"] == "allow"
            payload = transport.post.call_args.kwargs["json"]
            schema = payload["text"]["format"]["schema"]
            # Emit the verdict only after all rule checks, but keep rejecting mismatches.
            assert (
                list(schema["properties"])
                == schema["required"]
                == ["checks", "status", "description"]
            )
            checks = schema["properties"]["checks"]
            assert checks["minItems"] == checks["maxItems"] == len(required)
            assert checks["items"]["properties"]["rule_id"]["enum"] == required
            assert payload["model"] == DEFAULT_MODEL == "deepseek-v4.1-flash"
            assert payload["max_output_tokens"] == MEMORY_OUTPUT_LIMIT == 8192
            assert payload["text"]["format"]["strict"] is True
            assert transport.post.call_count == 1


def test_missing_extra_duplicate_and_unknown_checks_are_rejected_not_trimmed_or_allowed():
    required = ["system", "fact:1"]
    original = json.loads(safety_json(required))
    cases = []
    for checks in (
        [],
        original["checks"][:1],
        original["checks"] + [original["checks"][0]],
        [original["checks"][0], original["checks"][0]],
    ):
        cases.append(json.dumps(original | {"checks": checks}))
    wrong_id = json.loads(safety_json(required))
    wrong_id["checks"][1]["rule_id"] = "fact:999"
    cases.append(json.dumps(wrong_id))
    long_description = json.loads(safety_json(required))
    long_description["checks"][0]["description"] = "x" * 81
    cases.append(json.dumps(long_description))
    for raw in cases:
        assert error(lambda: parse_safety(raw, required, exact=True)) == SAFETY_ERROR
    assert (
        parse_safety(safety_json(required, status="block"), required, exact=True)["status"]
        == "block"
    )
    assert (
        parse_safety(
            safety_json(required, status="warning", strictness="advisory"), required, exact=True
        )["status"]
        == "warning"
    )


def test_invalid_safety_request_cannot_silently_fall_back_to_unbounded_generation():
    with tempfile.TemporaryDirectory() as directory:
        store = Storage(directory)
        client = real_client(store)
        with patch("fitagent.provider.httpx.Client") as factory:
            for messages in (
                [],
                [{"role": "user", "content": "PRIVATE invalid JSON"}],
                [{"role": "user", "content": '{"required_rule_ids":[]}'}],
                [{"role": "user", "content": '{"required_rule_ids":["system","system"]}'}],
                [{"role": "user", "content": '{"required_rule_ids":["PRIVATE"]}'}],
            ):
                try:
                    client.generate_safety(messages, "request")
                except SafetyValidationError as exc:
                    assert exc.diagnostics == {"reason": "invalid_request"}
                else:
                    raise AssertionError("Invalid required rules must fail before any API call")
            factory.assert_not_called()


def test_guard_passes_unique_required_ids_and_rejects_extraneous_context_checks():
    with tempfile.TemporaryDirectory() as directory:
        store, client, service = fixture(directory)
        snapshot = service.memory.repository.load(service.clock())
        service.safety.check("request", "Вопрос", snapshot, service.current_profile())
        sent = json.loads(client.generate_safety.call_args.args[0][1]["content"])
        assert sent["required_rule_ids"] == ["system"]

        def extra_check(messages, stage):
            value = json.loads(safe_response(messages, stage))
            value["checks"].append(value["checks"][0] | {"rule_id": "fact:999"})
            return json.dumps(value)

        client.generate_safety.side_effect = extra_check
        assert error(lambda: service.answer("Другой вопрос")) == SAFETY_ERROR
        client.generate.assert_not_called()
        assert "invalid_length" in store.logs()


def load_tests(loader, tests, pattern):
    return unittest.TestSuite(
        unittest.FunctionTestCase(value)
        for name, value in globals().items()
        if name.startswith("test_") and callable(value)
    )
