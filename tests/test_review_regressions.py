"""Regression cases from the memory/safety review; disposable databases and fake API only."""

import json
import subprocess
import sys
import tempfile
import unittest
from datetime import timedelta
from pathlib import Path

from support import safe_response, safety_json
from test_fitagent import NOW, fixture, profile
from test_layered_memory import ANSWERS, turn
from test_memory import error, extract, operation
from test_topics import active, checkpoint_reply, route

from fitagent.memory import project
from fitagent.memory_schema import EXTRACTION_SCHEMA, validate_shape
from fitagent.safety_schema import parse_safety


def patch_goal(text="Бегать 5 км"):
    return dict(
        birth_date=None,
        height=None,
        sex=None,
        goal=text,
        target=None,
        clear_target=False,
        source_quote="Хочу новую цель: " + text,
    )


def safe_or_block_goal(messages, stage):
    value = json.loads(safe_response(messages, stage))
    if stage == "goal_response":
        value["status"] = "block"
        value["checks"][0]["status"] = "block"
        value["description"] = "Оценка содержит небезопасный совет."
    return json.dumps(value)


def assert_shared_context(client, goal_text, *markers):
    main = client.generate.call_args.args[0][1]["content"]
    for call in client.generate_safety.call_args_list[-2:]:
        payload = json.loads(call.args[0][1]["content"])
        assert call.args[1] in {"request", "response"}
        assert payload["goal"]["text"] == goal_text
        assert payload["goal"]["target"] == 70
        assert payload["dialogue"] in main
        for marker in markers:
            assert marker in payload["dialogue"]


def test_validator_sees_current_goal_and_the_same_checkpoints_and_history_as_main():
    with tempfile.TemporaryDirectory() as directory:
        store, client, service = fixture(directory)
        service.save(profile(goal="CURRENT_GOAL", target=70))
        route(client, title="Лекарства")
        service.answer("Обсудим ASPIRIN_ANTECEDENT")
        medicine = active(service)
        route(client, action="switch", title="Тренировки")
        checkpoint_reply(client, "MEDICINE_CHECKPOINT ASPIRIN_ANTECEDENT")
        service.answer("TRAINING_CONTEXT")
        training = active(service)
        route(client, action="resume", topic_id=medicine)
        checkpoint_reply(client, "TRAINING_CHECKPOINT")
        service.answer("Можно его добавить?")
        assert_shared_context(client, "CURRENT_GOAL", "MEDICINE_CHECKPOINT", "ASPIRIN_ANTECEDENT")
        route(client, action="mixed", topic_id=training, related=[medicine])
        checkpoint_reply(client, "UPDATED_MEDICINE_CHECKPOINT ASPIRIN_ANTECEDENT")
        service.answer("Как это совместить?")
        assert_shared_context(
            client, "CURRENT_GOAL", "UPDATED_MEDICINE_CHECKPOINT", "TRAINING_CHECKPOINT"
        )


def test_resolved_followup_is_blocked_before_answer_generation():
    with tempfile.TemporaryDirectory() as directory:
        store, client, service = fixture(directory)
        service.save(profile())
        item = operation("Принимаю варфарин", category="medications", key="warfarin")
        extract(client, item)
        service.answer(item["source_quote"])
        extract(client)
        service.answer("Обсудим ASPIRIN_ANTECEDENT")

        def validate(messages, stage):
            payload = json.loads(messages[1]["content"])
            if (
                payload["question"] == "Можно его добавить?"
                and "ASPIRIN_ANTECEDENT" in payload["dialogue"]
            ):
                value = json.loads(safe_response(messages, stage))
                value["status"] = "block"
                value["checks"][0]["status"] = "block"
                value["description"] = "Запрошенное сочетание небезопасно."
                return json.dumps(value)
            return safe_response(messages, stage)

        client.generate_safety.side_effect = validate
        client.generate.reset_mock()
        error(lambda: service.answer("Можно его добавить?"), "Не могу выполнить эту просьбу")
        client.generate.assert_not_called()


def test_profile_confirmation_keeps_the_context_and_uses_the_proposed_goal():
    with tempfile.TemporaryDirectory() as directory:
        store, client, service = fixture(directory)
        service.save(profile())
        service.answer("PRIOR_CONTEXT")
        patch = patch_goal()
        turn(client, profile_change=patch)
        service.answer(patch["source_quote"])
        service.chat("/confirm 1")
        payload = json.loads(client.generate_safety.call_args.args[0][1]["content"])
        assert payload["goal"]["text"] == patch["goal"]
        assert "PRIOR_CONTEXT" in payload["dialogue"]


def test_correcting_old_weight_keeps_its_date_and_does_not_replace_current_weight():
    with tempfile.TemporaryDirectory() as directory:
        store, client, service = fixture(directory)
        service.save(profile())
        old = service.memory.repository.load(NOW)["facts"][0]
        service.clock = lambda: NOW + timedelta(days=1)
        item = operation(
            "Вес 79 кг",
            category="measurements",
            key="weight_measurement_next",
            value={"text": "Вес 79 кг", "quantity": 79, "unit": "кг", "qualifier": None},
        )
        extract(client, item)
        service.answer(item["source_quote"])
        service.clock = lambda: NOW + timedelta(days=2)
        correction = operation(
            "Вес 81 кг",
            category="measurements",
            key=old["key"],
            fact_id=old["id"],
            source_quote="Исправь измерение №1: 81 кг",
            value={"text": "Вес 81 кг", "quantity": 81, "unit": "кг", "qualifier": None},
        )
        extract(client, correction)
        service.answer(correction["source_quote"])
        rows = store.weights()
        assert [(r["confirmed_at"], r["weight"]) for r in rows] == [
            (NOW.isoformat(), 81),
            ((NOW + timedelta(days=1)).isoformat(), 79),
        ]
        assert service.current_profile()["weight"] == 79
        sent_profile, _ = json.JSONDecoder().raw_decode(
            client.generate.call_args.args[0][0]["content"].split("ПОЛНЫЕ ДАННЫЕ (JSON):\n")[1]
        )
        assert sent_profile["profile"]["weight"] == 79
        with store.connect() as db:
            record = db.execute(
                "SELECT * FROM memory_journal WHERE lineage_id=? ORDER BY id DESC LIMIT 1",
                (old["lineage_id"],),
            ).fetchone()
            assert record["recorded_at"] == service.clock().isoformat()
            assert record["effective_at"] == NOW.isoformat()


def test_measurement_correction_can_still_explicitly_change_its_date():
    with tempfile.TemporaryDirectory() as directory:
        store, client, service = fixture(directory)
        service.save(profile())
        old = service.memory.repository.load(NOW)["facts"][0]
        earlier = (NOW - timedelta(days=1)).isoformat()
        correction = operation(
            "Измерение было вчера",
            category="measurements",
            key=old["key"],
            fact_id=old["id"],
            value=old["value"],
            valid_from=earlier,
        )
        extract(client, correction)
        service.answer(correction["source_quote"])
        assert store.weights()[0]["confirmed_at"] == earlier


def test_confirmed_safety_edit_cannot_overfill_memory_or_consume_the_proposal():
    with tempfile.TemporaryDirectory() as directory:
        store, client, service = fixture(directory)
        service.save(profile())
        original = service.memory.repository.load(NOW)
        selected = None
        for count in range(1, 45):
            items = [
                operation("a" * 200, category="food_restrictions", key=f"rule_{i}")
                for i in range(count)
            ]
            candidate = project(original, items, None, NOW, manual=True)
            try:
                service.memory.check_inventory(candidate)
                service.memory.context(candidate)
            except ValueError:
                break
            selected = candidate
        assert selected is not None
        service.memory.repository.save_manual(selected)
        saved = service.memory.repository.load(NOW)
        service.memory.check_inventory(saved)
        old = next(fact for fact in saved["facts"] if fact["category"] == "food_restrictions")
        item = operation("я" * 490, category="food_restrictions", key=old["key"], fact_id=old["id"])
        extract(client, item)
        service.answer(item["source_quote"])
        before = service.memory.repository.load(NOW)
        client.generate_safety.reset_mock()
        error(lambda: service.chat("/confirm 1"), "Память")
        client.generate_safety.assert_not_called()
        assert service.memory.repository.load(NOW) == before
        with store.connect() as db:
            assert db.execute("SELECT COUNT(*) FROM pending_actions").fetchone()[0] == 1
        service.chat("/cancel 1")
        smaller = operation(
            "Аллергия A", category="food_restrictions", key=old["key"], fact_id=old["id"]
        )
        extract(client, smaller)
        service.answer(smaller["source_quote"])
        service.chat("/confirm 2")
        service.memory.check_inventory(service.memory.repository.load(NOW))
        extract(client)
        assert service.answer("Другой вопрос") == "Ответ"


def test_initial_goal_warning_is_checked_before_display_or_persistence():
    with tempfile.TemporaryDirectory() as directory:
        store, client, service = fixture(directory)
        marker = "UNSAFE_GOAL_ASSESSMENT"
        client.generate.side_effect = lambda messages, limit, purpose: json.dumps(
            {"warning": marker}
        )
        client.generate_safety.side_effect = safe_or_block_goal
        service.start_chat()
        for answer in ANSWERS[:-1]:
            service.chat(answer)
        error(lambda: service.chat(ANSWERS[-1]), "Не могу показать эту оценку цели")
        assert store.latest("profiles") is None
        assert "warning" not in service.onboarding.state()
        assert marker not in str(store.chat_messages()) + store.logs()
        payload = json.loads(client.generate_safety.call_args.args[0][1]["content"])
        assert payload["draft"] == marker
        assert payload["goal"]["text"] == ANSWERS[4]
        assert any(rule["category"] == "food_restrictions" for rule in payload["rules"])


def test_cached_onboarding_warning_is_rechecked_before_confirmation():
    with tempfile.TemporaryDirectory() as directory:
        store, client, service = fixture(directory)
        client.generate.side_effect = lambda messages, limit, purpose: (
            '{"warning":"Обсудите цель со специалистом."}'
        )
        service.start_chat()
        for answer in ANSWERS:
            service.chat(answer)
        assert store.latest("profiles") is None
        calls = client.generate.call_count
        client.generate_safety.side_effect = safe_or_block_goal
        error(lambda: service.chat("подтверждаю"), "Не могу показать эту оценку цели")
        assert store.latest("profiles") is None
        assert client.generate.call_count == calls


def test_profile_goal_warning_is_checked_before_it_can_be_added_to_a_proposal():
    with tempfile.TemporaryDirectory() as directory:
        store, client, service = fixture(directory)
        service.save(profile())
        patch = patch_goal()
        turn(client, profile_change=patch)
        marker = "UNSAFE_PROPOSAL_WARNING"
        client.generate.side_effect = lambda messages, limit, purpose: (
            json.dumps({"warning": marker}) if purpose == "goal_assessment" else "Ответ"
        )
        client.generate_safety.side_effect = safe_or_block_goal
        error(lambda: service.answer(patch["source_quote"]), "Не могу показать эту оценку цели")
        assert store.latest("goals")["text"] == "Стать сильнее"
        assert marker not in str(store.chat_messages()) + store.logs()
        assert all(call.args[2] != "answer" for call in client.generate.call_args_list)
        with store.connect() as db:
            assert db.execute("SELECT COUNT(*) FROM pending_actions").fetchone()[0] == 0


def test_advisory_warning_does_not_create_a_confirmation_loop():
    with tempfile.TemporaryDirectory() as directory:
        store, client, service = fixture(directory)
        service.save(profile())
        patch = patch_goal()
        turn(client, profile_change=patch)
        client.generate.side_effect = lambda messages, limit, purpose: (
            '{"warning":"Проверьте срок цели."}' if purpose == "goal_assessment" else "Ответ"
        )
        service.answer(patch["source_quote"])

        def warn(messages, stage):
            value = json.loads(safe_response(messages, stage))
            if stage == "goal_response":
                value["status"] = "warning"
                value["description"] = "Нужно уточнить подробности."
                value["checks"][0]["status"] = "warning"
                value["checks"][0]["strictness"] = "advisory"
            return json.dumps(value)

        client.generate_safety.side_effect = warn
        response = service.chat("/confirm 1")
        assert "Нужно уточнить подробности" in response
        assert store.latest("goals")["text"] == patch["goal"]


def test_cached_profile_warning_can_block_confirmation_without_consuming_it():
    with tempfile.TemporaryDirectory() as directory:
        store, client, service = fixture(directory)
        service.save(profile())
        patch = patch_goal()
        turn(client, profile_change=patch)
        client.generate.side_effect = lambda messages, limit, purpose: (
            '{"warning":"Проверьте срок цели."}' if purpose == "goal_assessment" else "Ответ"
        )
        service.answer(patch["source_quote"])
        client.generate_safety.side_effect = safe_or_block_goal
        error(lambda: service.chat("/confirm 1"), "Не могу показать эту оценку цели")
        assert store.latest("goals")["text"] == "Стать сильнее"
        with store.connect() as db:
            assert db.execute("SELECT COUNT(*) FROM pending_actions").fetchone()[0] == 1


def test_rephrased_advisory_does_not_prevent_onboarding_confirmation():
    with tempfile.TemporaryDirectory() as directory:
        store, client, service = fixture(directory)
        client.generate.side_effect = lambda messages, limit, purpose: (
            '{"warning":"Обсудите срок со специалистом."}'
        )
        descriptions = iter(("Уточните подробности №1.", "Уточните подробности №2."))

        def warn(messages, stage):
            value = json.loads(safe_response(messages, stage))
            if stage == "goal_response":
                value["status"] = "warning"
                value["description"] = next(descriptions)
                value["checks"][0].update(status="warning", strictness="advisory")
            return json.dumps(value)

        client.generate_safety.side_effect = warn
        service.start_chat()
        for answer in ANSWERS:
            service.chat(answer)
        assert store.latest("profiles") is None
        response = service.chat("подтверждаю")
        assert "Уточните подробности №2" in response
        assert store.latest("profiles") is not None
        assert service.onboarding.state() is None


def test_all_32_new_safety_rules_and_proposals_can_be_checked_in_one_turn():
    with tempfile.TemporaryDirectory() as directory:
        store, client, service = fixture(directory)
        service.save(profile())
        items = [
            operation(f"rule_{i}", category="food_restrictions", key=f"rule_{i}") for i in range(32)
        ]
        extract(client, *items)
        service.answer("; ".join(item["source_quote"] for item in items))
        assert client.generate_safety.call_count == 2
        for call in client.generate_safety.call_args_list:
            result = json.loads(safe_response(*call.args))
            assert len(result["checks"]) == 65
            assert (
                parse_safety(json.dumps(result), [item["rule_id"] for item in result["checks"]])[
                    "status"
                ]
                == "allow"
            )
        saved = service.memory.repository.load(NOW)
        assert (
            len([fact for fact in saved["facts"] if fact["category"] == "food_restrictions"]) == 32
        )


def test_local_checker_has_no_implicit_array_cap_but_extraction_stays_bounded():
    required = ["system", *(f"fact:{i}" for i in range(129))]
    assert parse_safety(safety_json(required), required)["status"] == "allow"
    error(lambda: validate_shape([{}] * 33, EXTRACTION_SCHEMA["properties"]["operations"]))
    script = Path(__file__).resolve().parents[1] / "scripts" / "check_safety.py"
    result = subprocess.run(
        [sys.executable, str(script), "-"],
        input=safety_json(required),
        text=True,
        capture_output=True,
    )
    assert result.returncode == 0, result.stderr


def load_tests(loader, tests, pattern):
    return unittest.TestSuite(
        unittest.FunctionTestCase(value)
        for name, value in globals().items()
        if name.startswith("test_") and callable(value)
    )
