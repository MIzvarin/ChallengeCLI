"""Immediate report priority, explicit critical replacements and transactional public logs."""

import json
import sqlite3
import tempfile
import unittest
from datetime import timedelta

from support import seed_accepted_goal
from test_memory import NOW, error, extract, facts, fixture, operation

from fitagent.activity import format_activity
from fitagent.memory_schema import CRITICAL_CATEGORIES, EXTRACTION_PROMPT
from fitagent.service import FitnessService
from fitagent.storage import Storage


def changes(store):
    with store.connect() as db:
        return [
            json.loads(row[0])
            for row in db.execute(
                "SELECT details FROM events WHERE kind='fact_changed' ORDER BY id"
            )
        ]


def latest_action(store):
    with store.connect() as db:
        return db.execute("SELECT MAX(id) FROM pending_actions").fetchone()[0]


def test_critical_conflicts_require_question_and_confirm_before_replacing_after_restart():
    for category in sorted(CRITICAL_CATEGORIES - {"measurements"}):
        with tempfile.TemporaryDirectory() as directory:
            store, client, service = fixture(directory)
            old = operation("Прежние сведения PRIVATE", category=category, key="reported_fact")
            extract(client, old)
            service.answer(old["source_quote"])
            original = facts(service)[0]
            old_changes = changes(store)
            service.clock = lambda: NOW + timedelta(days=1)
            new = operation(
                "Новые сведения PRIVATE",
                category=category,
                key=old["key"],
                fact_id=original["id"],
            )
            extract(client, new)
            response = service.answer(new["source_quote"])
            assert "Уточните: заменить" in response
            assert old["value"]["text"] in response and new["value"]["text"] in response
            assert "До подтверждения действуют прежние данные" in response
            assert facts(service)[0] == original
            assert changes(store) == old_changes
            assert "Уточнение замены фактов" in store.activity_logs()
            action_id = latest_action(store)
            reopened = FitnessService(Storage(directory), client, clock=service.clock)
            reopened.chat(f"/confirm {action_id}")
            replacement = facts(reopened)[0]
            assert replacement["id"] == original["id"]
            assert replacement["value"] == new["value"]
            assert replacement["lineage_id"] == original["lineage_id"]
            assert changes(store)[-1] == {
                "action": "updated",
                "fact_id": replacement["id"],
            }
            assert f"Факт №{original['id']} обновлён" in store.activity_logs()
            assert "PRIVATE" not in store.logs() + store.activity_logs()
            with store.connect() as db:
                assert (
                    db.execute(
                        "SELECT COUNT(*) FROM fact_sources WHERE fact_id=?", (replacement["id"],)
                    ).fetchone()[0]
                    == 2
                )


def test_critical_repeat_refreshes_fact_and_logs_update_without_question():
    for category in sorted(CRITICAL_CATEGORIES - {"measurements"}):
        with tempfile.TemporaryDirectory() as directory:
            store, client, service = fixture(directory)
            item = operation("Прежнее значение", category=category, key="reported_fact")
            extract(client, item)
            service.answer(item["source_quote"])
            original = facts(service)[0]
            service.clock = lambda: NOW + timedelta(days=1)
            item["fact_id"] = original["id"]
            extract(client, item)
            response = service.answer(item["source_quote"])
            assert "/confirm" not in response
            current = facts(service)[0]
            assert current["id"] == original["id"]
            assert current["updated_at"] == service.clock().isoformat()
            assert changes(store)[-1]["action"] == "confirmed"
            assert "Актуальность факта подтверждена" in store.activity_logs()


def test_cancel_and_failed_confirmation_leave_old_fact_and_no_replacement_log():
    with tempfile.TemporaryDirectory() as directory:
        store, client, service = fixture(directory)
        item = operation("Принимаю препарат А", category="medications", key="drug")
        extract(client, item)
        service.answer(item["source_quote"])
        old = facts(service)[0]
        revised = operation(
            "Принимаю другую дозу", category="medications", key="drug", fact_id=old["id"]
        )
        extract(client, revised)
        service.answer(revised["source_quote"])
        baseline = changes(store)
        client.generate_safety.side_effect = OSError("PRIVATE")
        assert "PRIVATE" not in error(lambda: service.chat("/confirm 1"))
        assert facts(service)[0] == old
        assert changes(store) == baseline
        assert latest_action(store) == 1
        service.chat("/cancel 1")
        assert latest_action(store) is None
        assert facts(service)[0] == old and changes(store) == baseline


def test_ordinary_change_with_future_date_is_immediate_and_manual_changes_are_logged():
    with tempfile.TemporaryDirectory() as directory:
        store, client, service = fixture(directory)
        extract(client, operation("Не люблю брокколи"))
        service.answer("Не люблю брокколи")
        old = facts(service)[0]
        service.clock = lambda: NOW + timedelta(days=1)
        extract(
            client,
            operation(
                "Люблю брокколи",
                fact_id=old["id"],
                valid_from=(NOW + timedelta(days=30)).isoformat(),
            ),
        )
        response = service.answer("Люблю брокколи")
        assert "/confirm" not in response
        current = facts(service)[0]
        assert current["valid_from"] == service.clock().isoformat()
        assert json.loads(service.memory.context())["facts"][0]["value"]["text"] == "Люблю брокколи"
        service.memory.command(f"/memory edit {current['id']} Предпочитаю цветную капусту")
        current = facts(service)[0]
        service.memory.command(f"/memory delete {current['id']}")
        assert [e["action"] for e in changes(store)][-4:] == [
            "added",
            "updated",
            "updated",
            "deleted",
        ]
        assert "Факт удалён" in store.activity_logs()
        assert "брокколи" not in store.logs()


def test_latest_report_is_used_consistently_for_profile_and_measurement_context():
    with tempfile.TemporaryDirectory() as directory:
        store, client, service = fixture(directory)
        for index, (days, value) in enumerate(((0, 79), (-5, 82))):
            service.clock = lambda: NOW + timedelta(days=1)
            item = operation(
                f"Вес {value} кг",
                category="measurements",
                key=f"weight_report_{index}",
                valid_from=(NOW + timedelta(days=days)).isoformat(),
                value={
                    "text": f"Вес {value} кг",
                    "quantity": value,
                    "unit": "кг",
                    "qualifier": None,
                },
            )
            extract(client, item)
            service.answer(item["source_quote"])
        assert service.current_profile()["weight"] == 82
        assert json.loads(service.memory.context())["measurements"][0]["value"]["quantity"] == 82
        reopened = FitnessService(Storage(directory), client, clock=service.clock)
        assert reopened.current_profile()["weight"] == 82
        weights = store.weights()
        assert [r["weight"] for r in weights] == [82, 80, 79]


def test_measurement_repeat_at_same_clock_uses_source_message_as_priority_tiebreaker():
    with tempfile.TemporaryDirectory() as directory:
        store, client, service = fixture(directory)
        old = service.memory.repository.load(NOW)["facts"][0]
        new = operation(
            "Вес 79 кг",
            category="measurements",
            key="weight_new",
            value={"text": "Вес 79 кг", "quantity": 79, "unit": "кг", "qualifier": None},
        )
        extract(client, new)
        service.answer(new["source_quote"])
        assert service.current_profile()["weight"] == 79
        extract(
            client,
            operation(
                old["value"]["text"],
                category="measurements",
                key=old["key"],
                fact_id=old["id"],
                value=old["value"],
            ),
        )
        response = service.answer(old["value"]["text"])
        assert "/confirm" not in response
        assert service.current_profile()["weight"] == 80
        assert json.loads(service.memory.context())["measurements"][0]["value"]["quantity"] == 80


def test_critical_pending_change_stops_execution_and_cannot_complete_goal():
    with tempfile.TemporaryDirectory() as directory:
        store, client, service = fixture(directory)
        seed_accepted_goal(service)
        old = next(f for f in facts(service) if f["category"] == "medications")
        item = operation(
            "Теперь принимаю препарат А", category="medications", key=old["key"], fact_id=old["id"]
        )
        extract(client, item)
        data = json.loads(client.generate_structured.return_value)
        data["completion"] = "goal"
        client.generate_structured.return_value = json.dumps(data)
        response = service.chat(item["source_quote"])
        assert "Уточните: заменить" in response
        assert service.workflow.state()["stage"] == "validation"
        with store.connect() as db:
            assert [row[0] for row in db.execute("SELECT kind FROM pending_actions")] == ["facts"]
        assert next(f for f in facts(service) if f["id"] == old["id"])["value"] == old["value"]


def test_critical_confirmation_transaction_failure_rolls_back_log_and_keeps_proposal():
    with tempfile.TemporaryDirectory() as directory:
        store, client, service = fixture(directory)
        item = operation("Принимаю добавку А", category="supplements", key="regimen")
        extract(client, item)
        service.answer(item["source_quote"])
        old = facts(service)[0]
        extract(
            client,
            operation(
                "Принимаю добавку иначе", category="supplements", key="regimen", fact_id=old["id"]
            ),
        )
        service.answer("Принимаю добавку иначе")
        baseline = changes(store)
        with store.connect() as db:
            db.execute(
                "CREATE TRIGGER fail_confirm BEFORE INSERT ON events WHEN NEW.kind='fact_changed' BEGIN SELECT RAISE(ABORT,'PRIVATE'); END"
            )
        try:
            service.chat("/confirm 1")
        except sqlite3.IntegrityError:
            pass
        else:
            raise AssertionError("Expected failed confirmation transaction")
        assert facts(service)[0] == old
        assert changes(store) == baseline and latest_action(store) == 1


def test_failed_turn_or_transaction_does_not_create_change_or_pending_events():
    for transactional in (False, True):
        with tempfile.TemporaryDirectory() as directory:
            store, client, service = fixture(directory)
            item = operation("Ограничение PRIVATE", category="health_constraints", key="condition")
            extract(client, item)
            service.answer(item["source_quote"])
            old = facts(service)[0]
            extract(
                client,
                operation(
                    "Нет ограничения PRIVATE",
                    category="health_constraints",
                    key="condition",
                    fact_id=old["id"],
                ),
            )
            baseline = store.activity_logs()
            if transactional:
                with store.connect() as db:
                    db.execute(
                        "CREATE TRIGGER fail_changes BEFORE INSERT ON memory_turns BEGIN SELECT RAISE(ABORT,'PRIVATE'); END"
                    )
            else:
                client.generate.side_effect = ValueError("Ошибка ответа")
            try:
                service.answer("Нет ограничения PRIVATE")
            except (ValueError, sqlite3.IntegrityError):
                pass
            else:
                raise AssertionError("Expected failed turn")
            assert facts(service)[0] == old
            assert latest_action(store) is None
            assert "Уточнение замены" not in store.activity_logs()
            assert changes(store)[-1]["action"] == "added"
            assert baseline in store.activity_logs()


def test_plan_local_edit_cannot_bypass_confirmation_and_unsafe_log_metadata_is_hidden():
    for category in ("nutrition_plan", "agreements"):
        with tempfile.TemporaryDirectory() as directory:
            store, client, service = fixture(directory)
            item = operation("Принятый план", category=category, key="plan")
            extract(client, item)
            service.answer(item["source_quote"])
            fact_id = facts(service)[0]["id"]
            for command in (f"/memory edit {fact_id} Другой план", f"/memory delete {fact_id}"):
                error(lambda: service.memory.command(command), "требует проверки")
    for details in (
        {"action": ["PRIVATE"], "fact_id": 1},
        {"action": "replaced", "fact_id": True, "previous_fact_id": 1},
        {"action": "replaced", "fact_id": 2, "previous_fact_id": "PRIVATE"},
    ):
        assert (
            format_activity(
                {
                    "created_at": NOW.isoformat(),
                    "kind": "fact_changed",
                    "details": json.dumps(details),
                }
            )
            is None
        )
    assert "Взаимоисключающие" in EXTRACTION_PROMPT
    assert "Приоритет имеет последнее сообщение" in EXTRACTION_PROMPT


def test_delayed_older_message_cannot_override_a_newer_completed_report():
    with tempfile.TemporaryDirectory() as directory:
        store, client, service = fixture(directory)
        older = store.message("user", "Не люблю брокколи", NOW, "pending")
        extract(client, operation("Люблю брокколи"))
        service.answer("Люблю брокколи")
        before = service.memory.repository.load(NOW)
        extract(client, operation("Не люблю брокколи"))
        client.generate_structured.reset_mock()
        error(lambda: service.memory.prepare(older, "Не люблю брокколи"), "более новое")
        assert service.memory.repository.load(NOW) == before
        client.generate_structured.assert_not_called()


def load_tests(loader, tests, pattern):
    return unittest.TestSuite(
        unittest.FunctionTestCase(value)
        for name, value in globals().items()
        if name.startswith("test_") and callable(value)
    )
