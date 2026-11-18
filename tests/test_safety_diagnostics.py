"""Protocol diagnostics contain fixed codes and safe metadata, never model payloads."""

import json
import tempfile
import unittest
from unittest.mock import patch

import httpx
from support import safe_response, safety_json
from test_memory import error, fixture, luna_reply, real_client

from fitagent.memory_schema import MEMORY_ERROR, validate_shape
from fitagent.safety_schema import (
    SAFETY_ERROR,
    SAFETY_FAILURE_REASONS,
    SafetyValidationError,
    parse_safety,
)
from fitagent.storage import Storage


def failure(raw, required=("system",)):
    try:
        parse_safety(raw, required)
    except SafetyValidationError as exc:
        assert str(exc) == SAFETY_ERROR
        assert "PRIVATE" not in str(exc) + json.dumps(exc.diagnostics)
        return exc.diagnostics
    raise AssertionError("Expected a rejected safety result")


def test_shape_and_json_diagnostics_distinguish_failures_without_logging_values():
    cases = (
        ("PRIVATE invalid json", "invalid_json", None),
        ('{"PRIVATE":"a","PRIVATE":"b"}', "duplicate_json_key", None),
        (json.dumps({"PRIVATE": "secret"}), "invalid_fields", "$"),
        (safety_json(status="PRIVATE"), "invalid_enum", "$.status"),
    )
    for raw, reason, field in cases:
        details = failure(raw)
        assert details["reason"] == reason
        assert details.get("field") == field
    value = json.loads(safety_json())
    value["checks"] = "PRIVATE"
    assert failure(json.dumps(value)) == {"reason": "invalid_type", "field": "$.checks"}
    value = json.loads(safety_json())
    value["description"] = "PRIVATE" + "x" * 701
    assert failure(json.dumps(value)) == {
        "reason": "invalid_length",
        "field": "$.description",
        "actual_length": 708,
        "minimum_length": 1,
        "maximum_length": 700,
    }
    value = json.loads(safety_json())
    value["checks"][0]["strictness"] = "PRIVATE"
    assert failure(json.dumps(value)) == {
        "reason": "invalid_enum",
        "field": "$.checks[0].strictness",
    }


def test_semantic_diagnostics_name_coverage_duplicates_strictness_and_status_conflicts():
    assert failure(safety_json(description=" ")) == {
        "reason": "blank_description",
        "field": "$.description",
    }
    value = json.loads(safety_json())
    value["checks"] = []
    assert failure(json.dumps(value)) == {"reason": "empty_checks", "field": "$.checks"}
    value = json.loads(safety_json())
    value["checks"][0]["description"] = " "
    assert failure(json.dumps(value)) == {
        "reason": "blank_description",
        "field": "$.checks[0].description",
    }
    value = json.loads(safety_json())
    value["checks"].append(value["checks"][0])
    assert failure(json.dumps(value)) == {
        "reason": "duplicate_rule",
        "field": "$.checks[1].rule_id",
    }
    assert failure(safety_json(status="block", strictness="advisory")) == {
        "reason": "advisory_block",
        "field": "$.checks[0].status",
    }
    assert failure(safety_json(), ("system", "PRIVATE_RULE", "PRIVATE_RULE")) == {
        "reason": "missing_rules",
        "field": "$.checks",
        "required_count": 2,
        "covered_count": 1,
        "missing_count": 1,
    }
    value = json.loads(safety_json(description="PRIVATE_DESCRIPTION"))
    value["checks"][0]["status"] = "block"
    assert failure(json.dumps(value)) == {
        "reason": "status_mismatch",
        "field": "$.status",
        "expected_status": "block",
        "actual_status": "allow",
    }


def test_error_metadata_uses_a_closed_allowlist():
    details = SafetyValidationError(
        "PRIVATE_REASON",
        field="$.PRIVATE",
        missing_count=True,
        required_count=-1,
        covered_count=2**100,
        actual_length="PRIVATE",
        expected_status="PRIVATE",
        actual_status={"PRIVATE": True},
        description="PRIVATE",
        response="PRIVATE",
        api_key="PRIVATE",
    ).diagnostics
    assert details == {"reason": "validation_error"}
    assert SafetyValidationError(
        "invalid_length", field="$.checks[123].description", actual_length=1000
    ).diagnostics == {
        "reason": "invalid_length",
        "field": "$.checks[123].description",
        "actual_length": 1000,
    }


def test_guard_records_the_failed_phase_and_safe_cause_without_showing_the_draft():
    for failed_stage in ("request", "response"):
        with tempfile.TemporaryDirectory() as directory:
            store, client, service = fixture(directory)
            client.generate.return_value = "PRIVATE_DRAFT"

            def validate(messages, stage):
                result = json.loads(safe_response(messages, stage))
                if stage == failed_stage:
                    result["description"] = "PRIVATE_DESCRIPTION"
                    result["checks"][0]["status"] = "block"
                return json.dumps(result)

            client.generate_safety.side_effect = validate
            assert error(lambda: service.answer("PRIVATE_QUESTION")) == SAFETY_ERROR
            with store.connect() as db:
                rows = db.execute(
                    "SELECT details FROM events WHERE kind='safety_failed'"
                ).fetchall()
            assert len(rows) == 1
            assert json.loads(rows[0][0]) == {
                "stage": failed_stage,
                "type": "SafetyValidationError",
                "phase": "contract",
                "reason": "status_mismatch",
                "field": "$.status",
                "expected_status": "block",
                "actual_status": "allow",
            }
            assert SAFETY_FAILURE_REASONS["status_mismatch"] in store.activity_logs()
            assert "PRIVATE" not in store.logs() + store.activity_logs()
            assert not any(message["role"] == "assistant" for message in store.chat_messages())


def test_service_failure_is_distinct_from_invalid_result_and_has_no_exception_text():
    with tempfile.TemporaryDirectory() as directory:
        store, client, service = fixture(directory)
        client.generate_safety.side_effect = OSError("PRIVATE_KEY_AND_PAYLOAD")
        assert error(lambda: service.answer("Вопрос")) == SAFETY_ERROR
        with store.connect() as db:
            details = json.loads(
                db.execute("SELECT details FROM events WHERE kind='safety_failed'").fetchone()[0]
            )
        assert details == {
            "stage": "request",
            "type": "OSError",
            "phase": "service_response",
            "reason": "service_error",
        }
        assert SAFETY_FAILURE_REASONS["service_error"] in store.activity_logs()
        assert "PRIVATE" not in store.logs() + store.activity_logs()
        client.generate.assert_not_called()


def test_public_causes_remain_safe_and_older_errors_remain_readable():
    with tempfile.TemporaryDirectory() as directory:
        store, client, service = fixture(directory)
        for reason in (*SAFETY_FAILURE_REASONS, "PRIVATE_UNKNOWN", ["PRIVATE"]):
            store.event(
                "safety_failed",
                {
                    "stage": "response",
                    "type": "ValueError",
                    "reason": reason,
                    "description": "PRIVATE",
                    "field": "PRIVATE",
                },
                service.clock(),
            )
        store.event("safety_failed", {"stage": "request", "type": "ValueError"}, service.clock())
        public = store.activity_logs()
        assert len(public.splitlines()) == len(SAFETY_FAILURE_REASONS) + 3
        assert all(reason in public for reason in SAFETY_FAILURE_REASONS.values())
        assert "PRIVATE" not in public and "ValueError" not in public
        assert public.splitlines()[-1].endswith(
            "Ошибка проверки безопасности  запрос · запрос остановлен; повторите попытку"
        )
        assert Storage(directory).activity_logs() == public


def test_generic_shape_validation_preserves_the_previous_memory_error_contract():
    try:
        validate_shape("PRIVATE", {"type": "array", "items": {"type": "string"}})
    except ValueError as exc:
        assert type(exc) is ValueError
        assert str(exc) == MEMORY_ERROR
    else:
        raise AssertionError("Expected shape validation to fail")


def test_provider_diagnostics_distinguish_incomplete_truncated_refused_and_empty_results():
    message = {"type": "message", "role": "assistant", "status": "completed"}
    cases = (
        ({"status": "incomplete"}, "response_incomplete"),
        (
            {"status": "incomplete", "incomplete_details": {"reason": "max_output_tokens"}},
            "output_limit",
        ),
        ({"output": []}, "response_empty"),
        ({"output": [{**message, "status": "incomplete"}]}, "response_incomplete"),
        ({"output": [{"type": "function_call", "arguments": "PRIVATE"}]}, "response_protocol"),
        (
            {"output": [{**message, "content": [{"type": "refusal", "refusal": "PRIVATE"}]}]},
            "response_refusal",
        ),
    )
    with tempfile.TemporaryDirectory() as directory:
        store, client, service = fixture(directory)
        service.safety.client = real_client(store)
        snapshot = service.memory.repository.load(service.clock())
        with patch("fitagent.provider.httpx.Client") as factory:
            transport = factory.return_value.__enter__.return_value
            for value, reason in cases:
                transport.post.return_value = luna_reply(**value)
                assert (
                    error(
                        lambda: service.safety.check(
                            "response",
                            "PRIVATE",
                            snapshot,
                            service.current_profile(),
                            draft="PRIVATE",
                        )
                    )
                    == SAFETY_ERROR
                )
                with store.connect() as db:
                    details = json.loads(
                        db.execute(
                            "SELECT details FROM events WHERE kind='safety_failed' ORDER BY id DESC LIMIT 1"
                        ).fetchone()[0]
                    )
                assert details == {
                    "stage": "response",
                    "type": "SafetyValidationError",
                    "phase": "service_response",
                    "reason": reason,
                }
            assert transport.post.call_count == len(cases)
        assert store.token_usage()["total_tokens"] == 230 * len(cases)
        assert "PRIVATE" not in store.logs() + store.activity_logs()


def test_provider_diagnostics_cover_http_network_timeout_and_bad_envelopes():
    cases = (
        (httpx.Response(429, text="PRIVATE"), {"reason": "http_error", "http_status": 429}),
        (httpx.Response(200, text="PRIVATE bad JSON"), {"reason": "invalid_json"}),
        (httpx.Response(200, json=[]), {"reason": "response_protocol"}),
        (httpx.ConnectError("PRIVATE"), {"reason": "network_error"}),
        (httpx.ReadTimeout("PRIVATE"), {"reason": "request_timeout"}),
    )
    with tempfile.TemporaryDirectory() as directory:
        store = Storage(directory)
        client = real_client(store)
        with patch("fitagent.provider.httpx.Client") as factory:
            transport = factory.return_value.__enter__.return_value
            for value, expected in cases:
                transport.post.side_effect = [value]
                try:
                    client.generate_safety(
                        [{"role": "user", "content": '{"required_rule_ids":["system"]}'}],
                        "request",
                    )
                except SafetyValidationError as exc:
                    assert str(exc) == SAFETY_ERROR
                    assert exc.diagnostics == expected
                else:
                    raise AssertionError("Expected a rejected service response")
            assert transport.post.call_count == len(cases)
        assert "PRIVATE" not in store.logs()
        client.key = ""
        try:
            client.generate_safety(
                [{"role": "user", "content": '{"required_rule_ids":["system"]}'}], "request"
            )
        except SafetyValidationError as exc:
            assert exc.diagnostics == {"reason": "missing_api_key"}
        else:
            raise AssertionError("Expected the missing key to fail")


def load_tests(loader, tests, pattern):
    return unittest.TestSuite(
        unittest.FunctionTestCase(value)
        for name, value in globals().items()
        if name.startswith("test_") and callable(value)
    )
