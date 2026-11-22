"""Safety protocol, independent calls, warnings and fail-closed transactions. No network."""

import copy
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from support import safe_response, safety_json
from test_memory import error, extract, facts, fixture, operation

from fitagent.safety_schema import SAFETY_ERROR, SAFETY_SCHEMA, parse_safety


def test_safety_contract_rejects_malformed_inconsistent_and_incomplete_verdicts():
    assert parse_safety(safety_json())["status"] == "allow"
    assert parse_safety(safety_json(status="warning", strictness="advisory"))["status"] == "warning"
    original = json.loads(safety_json())
    cases = ["not json PRIVATE", "[]", '{"status":"allow","status":"block"}']
    for change in (
        {"status": "yes"},
        {"checks": []},
        {"description": " "},
        {"extra": "PRIVATE"},
        {"status": True},
        {"checks": [original["checks"][0], original["checks"][0]]},
    ):
        cases.append(json.dumps(original | change))
    cases += [safety_json(status="block", strictness="advisory"), safety_json(rules=["other"])]
    mismatch = copy.deepcopy(original)
    mismatch["checks"][0]["status"] = "block"
    cases.append(json.dumps(mismatch))
    for raw in cases:
        assert error(lambda: parse_safety(raw)) == SAFETY_ERROR
    assert error(lambda: parse_safety(safety_json(), ["system", "fact:1"])) == SAFETY_ERROR
    assert SAFETY_SCHEMA["additionalProperties"] is False


def test_input_block_only_stops_this_request_before_generation_and_preserves_memory():
    with tempfile.TemporaryDirectory() as directory:
        store, client, service = fixture(directory)
        client.generate_safety.side_effect = None
        client.generate_safety.return_value = safety_json(
            status="block", description="Известная несовместимость."
        )
        extract(client, operation())
        error(lambda: service.answer("Не люблю брокколи"), "задать другой вопрос")
        assert not facts(service)
        client.generate.assert_not_called()
        assert not any(row["role"] == "assistant" for row in store.chat_messages())
        client.generate_safety.side_effect = safe_response
        extract(client)
        assert service.answer("Другой вопрос") == "Ответ"


def test_draft_block_and_protocol_failure_never_show_or_commit_the_draft():
    for failure in ("block", "protocol", "network"):
        with tempfile.TemporaryDirectory() as directory:
            store, client, service = fixture(directory)
            extract(client, operation())
            client.generate.return_value = "PRIVATE_UNSAFE_DRAFT"

            def validate(messages, stage):
                if stage == "request":
                    return safe_response(messages, stage)
                if failure == "network":
                    raise OSError("PRIVATE_SECRET")
                return (
                    safety_json(status="block", description="Небезопасный совет.")
                    if failure == "block"
                    else "PRIVATE_SECRET invalid json"
                )

            client.generate_safety.side_effect = validate
            message = error(lambda: service.answer("Не люблю брокколи"))
            assert "PRIVATE" not in message and "PRIVATE" not in store.logs()
            assert not facts(service)
            assert not any(row["role"] == "assistant" for row in store.chat_messages())


def test_luna_assigns_warning_for_unknown_details_and_main_only_receives_public_verdict():
    with tempfile.TemporaryDirectory() as directory:
        store, client, service = fixture(directory)
        extract(client, operation("Принимаю лекарство А", category="medications", key="medicine_a"))

        def warning(messages, stage):
            data = json.loads(safe_response(messages, stage))
            data["status"] = "warning"
            data["description"] = "Дозировка неизвестна; уточните её."
            for check in data["checks"]:
                check.update(
                    status="warning", strictness="advisory", description="PRIVATE_INTERNAL_CHECK"
                )
            return json.dumps(data)

        client.generate_safety.side_effect = warning
        response = service.answer("Принимаю лекарство А")
        assert "Предупреждение безопасности" in response and "Память обновлена" in response
        main = str(client.generate.call_args.args[0])
        assert "Дозировка неизвестна" in main
        assert "PRIVATE_INTERNAL_CHECK" not in main and '"checks"' not in main
        assert client.generate_safety.call_count == 2
        assert client.generate_structured.call_count == 1
        payload = json.loads(client.generate_safety.call_args.args[0][1]["content"])
        assert payload["rules"][0]["category"] == "medications"
        assert facts(service)[0]["value"]["quantity"] is None
        assert store.chat_messages()[-1]["content"] == response


def test_safety_changes_require_explicit_confirmation_and_cover_previous_rule():
    with tempfile.TemporaryDirectory() as directory:
        store, client, service = fixture(directory)
        extract(client, operation("Аллергия на орехи", category="food_restrictions", key="nuts"))
        service.answer("Аллергия на орехи")
        old = facts(service)[0]
        extract(
            client,
            operation(
                "Ранее ошибся: аллергии на орехи нет",
                category="food_restrictions",
                key="nuts",
                fact_id=old["id"],
            ),
        )
        proposed = service.answer("Ранее ошибся: аллергии на орехи нет")
        assert facts(service)[0]["id"] == old["id"]
        assert "/confirm 1" in proposed and "Внимание" in proposed
        assert "требует проверки" in error(
            lambda: service.memory.command(f"/memory delete {old['id']}")
        )
        assert "обновлена" in service.chat("/confirm 1")
        current = facts(service)[0]
        assert current["id"] == old["id"]
        payload = json.loads(client.generate_safety.call_args.args[0][1]["content"])
        assert payload["confirmed"] is True
        assert payload["previous_rules"][0]["value"] == old["value"]
        assert payload["previous_rules"][0]["rule_id"] == f"previous:{old['id']}"
        assert payload["rules"][0]["rule_id"] == f"fact:{old['id']}"
        assert set(payload["required_rule_ids"]) == {
            "system",
            f"fact:{old['id']}",
            f"previous:{old['id']}",
        }
        history = service.memory.command(f"/memory history {current['id']}")
        assert old["value"]["text"] in history and current["value"]["text"] in history
        error(lambda: service.chat("/confirm 1"), "не найдено")


def test_new_restriction_is_checked_before_its_pending_correction_is_saved():
    with tempfile.TemporaryDirectory() as directory:
        store, client, service = fixture(directory)
        extract(
            client,
            operation("Аллергий нет", category="food_restrictions", key="reported_allergies"),
        )
        service.answer("Аллергий нет")
        old = facts(service)[0]
        extract(
            client,
            operation(
                "Аллергия на арахис",
                category="food_restrictions",
                key="reported_allergies",
                fact_id=old["id"],
            ),
        )
        service.answer("Аллергия на арахис")
        assert facts(service)[0]["value"] == old["value"]
        for call in client.generate_safety.call_args_list[-2:]:
            data = json.loads(call.args[0][1]["content"])
            assert data["rules"][0]["value"]["text"] == "Аллергий нет"
            assert data["proposed_rules"][0]["value"]["text"] == "Аллергия на арахис"
        client.generate_safety.side_effect = lambda messages, stage: safety_json(
            ["system", f"fact:{old['id']}"]
        )
        error(lambda: service.answer("Аллергия на арахис"), "Это сбой проверки")


def test_safety_script_checks_schema_without_api_and_reports_block_as_nonzero():
    script = Path(__file__).resolve().parents[1] / "scripts" / "check_safety.py"
    for content, expected in (
        (safety_json(), 0),
        (safety_json(status="block"), 2),
        ("PRIVATE invalid", 1),
    ):
        result = subprocess.run(
            [sys.executable, str(script), "-"], input=content, text=True, capture_output=True
        )
        assert result.returncode == expected, result.stderr
        assert "PRIVATE" not in result.stdout + result.stderr
    for args in ([], ["--PRIVATE"], ["-", "--rule"]):
        result = subprocess.run(
            [sys.executable, str(script), *args], text=True, capture_output=True
        )
        assert result.returncode == 1
        assert "Параметры команды неверны" in result.stderr
        assert "PRIVATE" not in result.stderr


def load_tests(loader, tests, pattern):
    return unittest.TestSuite(
        unittest.FunctionTestCase(value)
        for name, value in globals().items()
        if name.startswith("test_") and callable(value)
    )
