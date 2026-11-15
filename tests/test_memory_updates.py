"""Appending future stages and normalizing implicit fact targets before projection."""

import json
import tempfile
import unittest
from datetime import timedelta

from test_memory import NOW, error, extract, facts, fixture, operation
from test_memory_revisions import context_facts, routine, schedule
from test_topics import active, route

from fitagent.memory import project
from fitagent.memory_schema import MEMORY_ERROR, STAGE_ERROR, parse_extraction
from fitagent.service import FitnessService
from fitagent.storage import Storage


def initial_routine(service, client):
    extract(client, routine("Три тренировки"))
    service.answer("Три тренировки")
    schedule(service, client, "С 21 ноября четыре тренировки", NOW + timedelta(days=7))


def test_append_preserves_each_future_stage_and_boundaries_after_restart():
    with tempfile.TemporaryDirectory() as directory:
        store, client, service = fixture(directory)
        initial_routine(service, client)
        middle = facts(service)[0]
        schedule(
            service,
            client,
            "Затем с 28 ноября пять тренировок",
            NOW + timedelta(days=14),
            update_mode="append",
        )
        last = facts(service)[0]
        reopened = FitnessService(Storage(directory), client, clock=lambda: NOW)
        for stamp, expected in (
            (NOW, "Три тренировки"),
            (NOW + timedelta(days=7) - timedelta(microseconds=1), "Три тренировки"),
            (NOW + timedelta(days=7), "С 21 ноября четыре тренировки"),
            (NOW + timedelta(days=10), "С 21 ноября четыре тренировки"),
            (NOW + timedelta(days=14), "Затем с 28 ноября пять тренировок"),
        ):
            current = context_facts(reopened, stamp)
            assert [fact["value"]["text"] for fact in current] == [expected]
        assert context_facts(reopened, NOW + timedelta(days=10))[0]["id"] == middle["id"]
        assert context_facts(reopened, NOW + timedelta(days=14))[0]["id"] == last["id"]
        with store.connect() as db:
            assert (
                db.execute(
                    "SELECT superseded_at FROM sticky_facts WHERE id=?", (middle["id"],)
                ).fetchone()[0]
                == (NOW + timedelta(days=14)).isoformat()
            )
        service.clock = lambda: NOW + timedelta(days=10)
        extract(client)
        service.answer("Какой режим сегодня?")
        sent = client.generate.call_args.args[0][1]["content"]
        fact_block = json.loads(
            sent.split("ЗАКРЕПЛЁННЫЕ ФАКТЫ (JSON):\n", 1)[1].split("\nТЕМА И CHECKPOINT", 1)[0]
        )
        assert [fact["id"] for fact in fact_block["facts"]] == [middle["id"]]


def test_replacing_last_stage_does_not_cancel_earlier_appended_stage():
    with tempfile.TemporaryDirectory() as directory:
        store, client, service = fixture(directory)
        initial_routine(service, client)
        schedule(
            service,
            client,
            "Затем с 28 ноября пять тренировок",
            NOW + timedelta(days=14),
            update_mode="append",
        )
        schedule(service, client, "Последний этап переносим на 5 декабря", NOW + timedelta(days=21))
        assert [
            fact["value"]["text"] for fact in context_facts(service, NOW + timedelta(days=17))
        ] == ["С 21 ноября четыре тренировки"]
        assert [
            fact["value"]["text"] for fact in context_facts(service, NOW + timedelta(days=21))
        ] == ["Последний этап переносим на 5 декабря"]
        schedule(
            service,
            client,
            "Потом с 12 декабря шесть тренировок",
            NOW + timedelta(days=28),
            update_mode="append",
        )
        assert [
            fact["value"]["text"] for fact in context_facts(service, NOW + timedelta(days=22))
        ] == ["Последний этап переносим на 5 декабря"]
        assert [
            fact["value"]["text"] for fact in context_facts(service, NOW + timedelta(days=28))
        ] == ["Потом с 12 декабря шесть тренировок"]


def test_append_does_not_extend_predecessors_own_end_or_resume_expired_versions():
    with tempfile.TemporaryDirectory() as directory:
        store, client, service = fixture(directory)
        initial_routine(service, client)
        schedule(
            service,
            client,
            "Четыре тренировки до 24 ноября",
            NOW + timedelta(days=7),
            valid_until=(NOW + timedelta(days=10)).isoformat(),
        )
        schedule(
            service,
            client,
            "Затем с 28 ноября пять постоянно",
            NOW + timedelta(days=14),
            update_mode="append",
            valid_until_action="clear",
        )
        assert (
            context_facts(service, NOW + timedelta(days=9))[0]["valid_until"]
            == (NOW + timedelta(days=10)).isoformat()
        )
        assert not context_facts(service, NOW + timedelta(days=12))
        assert (
            context_facts(service, NOW + timedelta(days=14))[0]["value"]["text"]
            == "Затем с 28 ноября пять постоянно"
        )


def test_invalid_append_rejects_whole_batch_before_answer_or_writes():
    with tempfile.TemporaryDirectory() as directory:
        store, client, service = fixture(directory)
        initial_routine(service, client)
        original = service.memory.repository.load(NOW)
        head = facts(service)[0]
        for changes in (
            {"valid_from": None},
            {"valid_from": NOW.isoformat()},
            {"valid_from": (NOW + timedelta(days=6)).isoformat()},
            {"valid_from": head["valid_from"]},
            {"action": "revoke"},
            {"action": "clarify", "clarification": "Новый этап?"},
            {"fact_id": None, "key": "another_routine"},
        ):
            bad = routine(
                "Следующий этап",
                fact_id=head["id"],
                update_mode="append",
                valid_from=(NOW + timedelta(days=14)).isoformat(),
            )
            bad.update(changes)
            extract(client, operation("Есть гантели", category="equipment", key="dumbbells"), bad)
            client.generate.reset_mock()
            assert error(lambda: service.answer("Есть гантели. Следующий этап")) == STAGE_ERROR
            assert "дату начала" in STAGE_ERROR and "позже" in STAGE_ERROR
            assert service.memory.repository.load(NOW) == original
            client.generate.assert_not_called()


def test_implicit_append_resolves_existing_fact_id():
    with tempfile.TemporaryDirectory() as directory:
        store, client, service = fixture(directory)
        initial_routine(service, client)
        extract(
            client,
            routine(
                "С 28 ноября пять тренировок",
                update_mode="append",
                valid_from=(NOW + timedelta(days=14)).isoformat(),
            ),
        )
        service.answer("С 28 ноября пять тренировок")
        assert (
            context_facts(service, NOW + timedelta(days=10))[0]["value"]["text"]
            == "С 21 ноября четыре тренировки"
        )


def test_append_failure_leaves_full_schedule_unchanged():
    with tempfile.TemporaryDirectory() as directory:
        store, client, service = fixture(directory)
        initial_routine(service, client)
        original = service.memory.repository.load(NOW)
        client.generate.side_effect = ValueError("Не удалось получить ответ.")
        extract(
            client,
            routine(
                "Затем пять тренировок",
                fact_id=facts(service)[0]["id"],
                update_mode="append",
                valid_from=(NOW + timedelta(days=14)).isoformat(),
            ),
        )
        error(lambda: service.answer("Затем пять тренировок"))
        assert service.memory.repository.load(NOW) == original


def test_implicit_and_explicit_updates_of_same_fact_are_rejected_in_both_orders():
    for reverse in (False, True):
        with tempfile.TemporaryDirectory() as directory:
            store, client, service = fixture(directory)
            route(
                client,
                title="Меню",
                operations=[operation("Бюджет 5000", category="cooking_budget", key="budget")],
            )
            service.answer("Бюджет 5000")
            original = service.memory.repository.load(NOW)
            topics = service.topics.repository.snapshot()
            quote = "Бюджет 6000 только для этого меню"
            operations = [
                operation(quote, category="cooking_budget", key="budget"),
                operation(
                    quote,
                    category="cooking_budget",
                    key="budget",
                    fact_id=facts(service)[0]["id"],
                    applicability="topic",
                    topic_id=active(service),
                ),
            ]
            route(client, operations=list(reversed(operations)) if reverse else operations)
            client.generate.reset_mock()
            assert error(lambda: service.answer(quote)) == MEMORY_ERROR
            assert service.memory.repository.load(NOW) == original
            assert service.topics.repository.snapshot() == topics
            assert (
                json.loads(service.memory.context(topic_id=999))["facts"][0]["value"]["text"]
                == "Бюджет 5000"
            )
            client.generate.assert_not_called()


def test_single_implicit_update_is_normalized_and_keeps_lineage():
    with tempfile.TemporaryDirectory() as directory:
        store, client, service = fixture(directory)
        extract(client, operation())
        service.answer("Не люблю брокколи")
        old = facts(service)[0]
        update = operation("Теперь люблю брокколи")
        normalized = parse_extraction(
            json.dumps({"operations": [update]}), update["source_quote"], [old], NOW
        )
        assert normalized[0]["fact_id"] == old["id"]
        extract(client, update)
        service.answer("Теперь люблю брокколи")
        assert facts(service)[0]["lineage_id"] == old["lineage_id"]


def test_projection_rejects_a_missing_explicit_target_instead_of_creating_a_fact():
    with tempfile.TemporaryDirectory() as directory:
        store, client, service = fixture(directory)
        snapshot = service.memory.repository.load(NOW)
        assert error(lambda: project(snapshot, [operation(fact_id=123)], 1, NOW)) == MEMORY_ERROR
        assert service.memory.repository.load(NOW) == snapshot


def load_tests(loader, tests, pattern):
    return unittest.TestSuite(
        unittest.FunctionTestCase(value)
        for name, value in globals().items()
        if name.startswith("test_") and callable(value)
    )
