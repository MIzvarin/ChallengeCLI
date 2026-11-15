"""Retained identities and safe stage ordering, with local databases and fake API calls."""

import copy
import json
import sqlite3
import tempfile
import unittest
from datetime import timedelta

from test_memory import NOW, error, extract, facts, fixture, operation
from test_memory_revisions import context_facts, routine, schedule
from test_memory_updates import initial_routine
from test_topics import active, route

from fitagent.memory import project
from fitagent.memory_schema import FACT_CONFLICT_ERROR, STAGE_ORDER_ERROR, parse_extraction
from fitagent.service import FitnessService
from fitagent.storage import Storage


def scoped_budget(service, client, end=None):
    route(
        client,
        title="Меню",
        operations=[
            operation(
                "Общий лимит 5000",
                category="cooking_budget",
                key="budget",
                valid_until=end,
            )
        ],
    )
    service.answer("Общий лимит 5000")
    original = facts(service)[0]
    quote = "С 21 ноября лимит 5000 только для этого меню, бессрочно"
    route(
        client,
        operations=[
            operation(
                "Лимит меню 5000",
                category="cooking_budget",
                key="budget",
                fact_id=original["id"],
                applicability="topic",
                valid_from=(NOW + timedelta(days=7)).isoformat(),
                valid_until_action="clear",
                source_quote=quote,
            )
        ],
    )
    service.answer(quote)
    return original, facts(service)[0], active(service)


def update_budget(client, fact_id=None):
    quote = "Общий лимит сейчас 6000, запланированный лимит меню не меняем"
    route(
        client,
        operations=[
            operation(
                "Общий лимит 6000",
                category="cooking_budget",
                key="budget",
                fact_id=fact_id,
                source_quote=quote,
            )
        ],
    )
    return quote


def test_crossing_or_equal_stage_start_rejects_whole_batch_without_cancelling_stages():
    with tempfile.TemporaryDirectory() as directory:
        store, client, service = fixture(directory)
        initial_routine(service, client)
        schedule(
            service,
            client,
            "С 28 ноября пять тренировок",
            NOW + timedelta(days=14),
            update_mode="append",
        )
        original = service.memory.repository.load(NOW)
        topics = service.topics.repository.snapshot()
        with store.connect() as db:
            original_sources = [
                tuple(row)
                for row in db.execute("SELECT * FROM fact_sources ORDER BY fact_id,message_id")
            ]
        for day in (0, 4, 7):
            quote = "Последний этап переносим, предыдущий этап не отменяем"
            extract(
                client,
                operation("Есть гантели", category="equipment", key="dumbbells"),
                routine(
                    quote,
                    fact_id=facts(service)[0]["id"],
                    valid_from=(NOW + timedelta(days=day)).isoformat(),
                ),
            )
            client.generate.reset_mock()
            assert error(lambda: service.answer("Есть гантели. " + quote)) == STAGE_ORDER_ERROR
            assert "позже начала предыдущего" in STAGE_ORDER_ERROR
            assert service.memory.repository.load(NOW) == original
            assert service.topics.repository.snapshot() == topics
            client.generate.assert_not_called()
        assert (
            context_facts(service, NOW + timedelta(days=10))[0]["value"]["text"]
            == "С 21 ноября четыре тренировки"
        )
        with store.connect() as db:
            assert [
                tuple(row)
                for row in db.execute("SELECT * FROM fact_sources ORDER BY fact_id,message_id")
            ] == original_sources


def test_valid_earlier_transfer_preserves_predecessor_and_exact_boundary():
    with tempfile.TemporaryDirectory() as directory:
        store, client, service = fixture(directory)
        initial_routine(service, client)
        schedule(
            service,
            client,
            "С 28 ноября пять тренировок",
            NOW + timedelta(days=14),
            update_mode="append",
        )
        earlier = NOW + timedelta(days=8)
        schedule(service, client, "Последний этап с 22 ноября", earlier)
        assert (
            context_facts(service, earlier - timedelta(microseconds=1))[0]["value"]["text"]
            == "С 21 ноября четыре тренировки"
        )
        assert context_facts(service, earlier)[0]["value"]["text"] == "Последний этап с 22 ноября"


def test_pending_transfer_cannot_erase_the_start_of_current_predecessor():
    with tempfile.TemporaryDirectory() as directory:
        store, client, service = fixture(directory)
        initial_routine(service, client)
        original = service.memory.repository.load(NOW)
        for stamp in (NOW, NOW - timedelta(days=1)):
            quote = "Переносим последний этап раньше"
            extract(
                client,
                routine(quote, fact_id=facts(service)[0]["id"], valid_from=stamp.isoformat()),
            )
            assert error(lambda: service.answer(quote)) == STAGE_ORDER_ERROR
            assert service.memory.repository.load(NOW) == original


def test_current_retained_budget_update_keeps_future_scoped_plan_and_survives_restart():
    for explicit in (False, True):
        for day in (0, 1):
            with tempfile.TemporaryDirectory() as directory:
                store, client, service = fixture(directory)
                original, pending, menu = scoped_budget(service, client)
                service.clock = lambda: NOW + timedelta(days=day)
                quote = update_budget(client, original["id"] if explicit else None)
                service.answer(quote)
                current = next(fact for fact in facts(service) if fact["applicability"] == "global")
                assert current["lineage_id"] != pending["lineage_id"]
                assert (
                    next(fact for fact in facts(service) if fact["id"] == pending["id"]) == pending
                )
                reopened = FitnessService(Storage(directory), client, clock=service.clock)
                for moment in (service.clock(), NOW + timedelta(days=8)):
                    reopened.memory.clock = lambda: moment
                    general = json.loads(reopened.memory.context(topic_id=999))["facts"]
                    assert [fact["value"]["text"] for fact in general] == ["Общий лимит 6000"]
                reopened.memory.clock = lambda: NOW + timedelta(days=8)
                scoped = json.loads(reopened.memory.context(topic_id=menu))["facts"]
                assert {fact["value"]["text"] for fact in scoped} == {
                    "Общий лимит 6000",
                    "Лимит меню 5000",
                }
                with store.connect() as db:
                    old = dict(
                        db.execute(
                            "SELECT * FROM sticky_facts WHERE id=?", (original["id"],)
                        ).fetchone()
                    )
                    assert old["superseded_at"] == (service.clock().isoformat() if day else None)
                    assert (
                        db.execute(
                            "SELECT COUNT(*) FROM fact_sources WHERE fact_id=?", (current["id"],)
                        ).fetchone()[0]
                        == 1
                    )
                # Deleting the independently updated general fact must not erase the future plan.
                service.memory.command(f"/memory delete {current['id']}")
                service.memory.clock = lambda: NOW + timedelta(days=8)
                assert not json.loads(service.memory.context(topic_id=999))["facts"]
                assert (
                    json.loads(service.memory.context(topic_id=menu))["facts"][0]["id"]
                    == pending["id"]
                )


def test_retained_confirmation_keeps_transition_and_records_provenance_without_fork():
    with tempfile.TemporaryDirectory() as directory:
        store, client, service = fixture(directory)
        original, pending, menu = scoped_budget(service, client)
        service.clock = lambda: NOW + timedelta(days=1)
        route(
            client,
            operations=[
                operation(
                    "Общий лимит 5000",
                    category="cooking_budget",
                    key="budget",
                    source_quote="Подтверждаю общий лимит 5000",
                )
            ],
        )
        service.answer("Подтверждаю общий лимит 5000")
        snapshot = service.memory.repository.load(service.clock())
        assert [fact["id"] for fact in snapshot["facts"]] == [pending["id"]]
        assert snapshot["previous_versions"][0]["id"] == original["id"]
        assert snapshot["previous_versions"][0]["updated_at"] == service.clock().isoformat()
        assert snapshot["previous_versions"][0]["superseded_at"] == pending["valid_from"]
        with store.connect() as db:
            assert (
                db.execute(
                    "SELECT COUNT(*) FROM fact_sources WHERE fact_id=?", (original["id"],)
                ).fetchone()[0]
                == 2
            )


def test_retained_update_preserves_user_end_and_does_not_inherit_transition_as_user_end():
    with tempfile.TemporaryDirectory() as directory:
        store, client, service = fixture(directory)
        end = NOW + timedelta(days=3)
        original, pending, menu = scoped_budget(service, client, end.isoformat())
        service.clock = lambda: NOW + timedelta(days=1)
        service.answer(update_budget(client))
        general = next(fact for fact in facts(service) if fact["applicability"] == "global")
        assert general["valid_until"] == end.isoformat()
        service.memory.clock = lambda: NOW + timedelta(days=4)
        assert not json.loads(service.memory.context(topic_id=999))["facts"]
        service.memory.clock = lambda: NOW + timedelta(days=8)
        assert json.loads(service.memory.context(topic_id=menu))["facts"][0]["id"] == pending["id"]


def test_failed_answer_or_transaction_cannot_partially_end_retained_version():
    for fail_transaction in (False, True):
        with tempfile.TemporaryDirectory() as directory:
            store, client, service = fixture(directory)
            original, pending, menu = scoped_budget(service, client)
            service.clock = lambda: NOW + timedelta(days=1)
            snapshot = service.memory.repository.load(service.clock())
            if fail_transaction:
                with store.connect() as db:
                    db.execute("""CREATE TRIGGER fail_retained BEFORE INSERT ON memory_turns
                                  BEGIN SELECT RAISE(ABORT, 'TEST_SECRET'); END""")
            else:
                client.generate.side_effect = ValueError("Не удалось получить ответ.")
            quote = update_budget(client)
            try:
                service.answer(quote)
            except (ValueError, sqlite3.IntegrityError):
                pass
            else:
                raise AssertionError("Expected simulated answer/write failure")
            assert service.memory.repository.load(service.clock()) == snapshot
            assert "TEST_SECRET" not in store.logs()


def test_illegal_retained_target_changes_reject_without_modifying_pending_plan():
    with tempfile.TemporaryDirectory() as directory:
        store, client, service = fixture(directory)
        original, pending, menu = scoped_budget(service, client)
        service.clock = lambda: NOW + timedelta(days=1)
        snapshot = service.memory.repository.load(service.clock())
        for changes in (
            {"action": "revoke"},
            {"action": "clarify", "clarification": "Уточнить?"},
            {"update_mode": "append", "valid_from": (NOW + timedelta(days=2)).isoformat()},
            {"applicability": "topic", "topic_id": menu},
        ):
            update = operation(
                "Лимит изменяется", category="cooking_budget", key="budget", fact_id=original["id"]
            )
            update.update(changes)
            route(client, operations=[update])
            client.generate.reset_mock()
            assert error(lambda: service.answer("Лимит изменяется")) == FACT_CONFLICT_ERROR
            assert service.memory.repository.load(service.clock()) == snapshot
            client.generate.assert_not_called()


def test_duplicate_implicit_and_explicit_retained_updates_are_rejected():
    with tempfile.TemporaryDirectory() as directory:
        store, client, service = fixture(directory)
        original, pending, menu = scoped_budget(service, client)
        service.clock = lambda: NOW + timedelta(days=1)
        snapshot = service.memory.repository.load(service.clock())
        implicit = operation("Лимит 6000", category="cooking_budget", key="budget")
        explicit = operation(
            "Лимит 6000", category="cooking_budget", key="budget", fact_id=original["id"]
        )
        for operations in ([implicit, explicit], [explicit, implicit]):
            route(client, operations=operations)
            error(lambda: service.answer("Лимит 6000"))
            assert service.memory.repository.load(service.clock()) == snapshot


def test_effective_identity_collision_is_rejected_before_context_or_manual_save():
    with tempfile.TemporaryDirectory() as directory:
        store, client, service = fixture(directory)
        original, pending, menu = scoped_budget(service, client)
        snapshot = service.memory.repository.load(NOW)
        duplicate = copy.deepcopy(snapshot["previous_versions"][0])
        duplicate.update(
            id=999,
            lineage_id=999,
            valid_from=(NOW + timedelta(days=2)).isoformat(),
            superseded_at=(NOW + timedelta(days=3)).isoformat(),
        )
        snapshot["previous_versions"].append(duplicate)
        assert error(lambda: service.memory.context(snapshot, topic_id=999)) == FACT_CONFLICT_ERROR
        assert error(lambda: project(snapshot, [], 1, NOW)) == FACT_CONFLICT_ERROR
        assert "Периоды" in FACT_CONFLICT_ERROR and "/memory" in FACT_CONFLICT_ERROR


def test_direct_parser_resolves_retained_identity_and_disallows_missing_target_revoke():
    with tempfile.TemporaryDirectory() as directory:
        store, client, service = fixture(directory)
        original, pending, menu = scoped_budget(service, client)
        snapshot = service.memory.repository.load(NOW)
        update = operation("Лимит 6000", category="cooking_budget", key="budget")
        normalized = parse_extraction(
            json.dumps({"operations": [update]}),
            "Лимит 6000",
            snapshot["facts"],
            NOW,
            snapshot["previous_versions"],
        )
        assert normalized[0]["fact_id"] == original["id"]
        update["action"] = "revoke"
        error(
            lambda: parse_extraction(
                json.dumps({"operations": [update]}),
                "Лимит 6000",
                snapshot["facts"],
                NOW,
                snapshot["previous_versions"],
            )
        )


def load_tests(loader, tests, pattern):
    return unittest.TestSuite(
        unittest.FunctionTestCase(value)
        for name, value in globals().items()
        if name.startswith("test_") and callable(value)
    )
