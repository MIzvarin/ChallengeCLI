"""Temporal revisions and consistent reads, using disposable SQLite and mocked Go."""

import json
import sqlite3
import tempfile
import unittest
from contextlib import contextmanager
from datetime import timedelta
from unittest.mock import patch

from test_memory import NOW, error, extract, facts, fixture, operation

from fitagent.memory import FactMemory
from fitagent.memory_schema import MEMORY_ERROR, PERIOD_ERROR
from fitagent.service import FitnessService
from fitagent.storage import Storage


def routine(text, **changes):
    return operation(text, category="training_routine", key="frequency", **changes)


def context_facts(service, when):
    service.memory.clock = lambda: when
    return json.loads(service.memory.context())["facts"]


def schedule(service, client, text, start, **changes):
    extract(
        client,
        routine(text, fact_id=facts(service)[0]["id"], valid_from=start.isoformat(), **changes),
    )
    service.answer(text)


def test_concurrent_delete_between_fact_and_revision_reads_cannot_restore_memory():
    with tempfile.TemporaryDirectory() as directory:
        store, client, service = fixture(directory)
        extract(client, operation())
        service.answer("Не люблю брокколи")
        fact_id = facts(service)[0]["id"]
        # WAL lets a second connection commit while the first retains its read snapshot.
        with store.connect() as db:
            db.execute("PRAGMA journal_mode=WAL")
        other = FactMemory(Storage(directory), client, lambda: NOW)
        connect = store.connect
        deleted = []

        def interleave(sql):
            if not deleted and "sqlite_sequence" in sql and "sticky_facts" in sql:
                other.command(f"/memory delete {fact_id}")
                deleted.append(True)

        @contextmanager
        def traced_connection():
            with connect() as db:
                db.set_trace_callback(interleave)
                yield db

        extract(client)
        with patch.object(store, "connect", traced_connection):
            error(lambda: service.answer("Что приготовить?"), "Память изменена")
        assert deleted and not facts(service)
        assert [row["status"] for row in store.chat_messages()] == [
            "complete",
            "complete",
            "failed",
        ]
        with store.connect() as db:
            assert db.execute("SELECT COUNT(*) FROM fact_sources").fetchone()[0] == 0


def test_unmentioned_end_is_kept_but_explicit_indefinite_update_clears_it():
    with tempfile.TemporaryDirectory() as directory:
        store, client, service = fixture(directory)
        end = NOW + timedelta(days=2)
        extract(client, routine("Три тренировки до 16 ноября", valid_until=end.isoformat()))
        service.answer("Три тренировки до 16 ноября")
        service.clock = lambda: NOW + timedelta(days=1)
        extract(client, routine("Теперь четыре тренировки", fact_id=facts(service)[0]["id"]))
        service.answer("Теперь четыре тренировки")
        assert facts(service)[0]["valid_until"] == end.isoformat()
        service.clock = lambda: NOW + timedelta(days=3)
        extract(
            client,
            routine(
                "Теперь пять постоянно", fact_id=facts(service)[0]["id"], valid_until_action="clear"
            ),
        )
        service.answer("Теперь пять постоянно")
        current = facts(service)[0]
        assert current["valid_until"] is None
        assert current["valid_from"] == (NOW + timedelta(days=3)).isoformat()
        assert (
            context_facts(service, NOW + timedelta(days=4))[0]["value"]["text"]
            == "Теперь пять постоянно"
        )
        assert facts(FitnessService(Storage(directory), client))[0]["valid_until"] is None


def test_inherited_expired_end_is_rejected_before_answer_and_without_invalid_revision():
    with tempfile.TemporaryDirectory() as directory:
        store, client, service = fixture(directory)
        extract(
            client,
            routine("Три тренировки до завтра", valid_until=(NOW + timedelta(days=1)).isoformat()),
        )
        service.answer("Три тренировки до завтра")
        original = facts(service)
        service.clock = lambda: NOW + timedelta(days=2)
        client.generate.reset_mock()
        extract(client, routine("Теперь четыре тренировки", fact_id=original[0]["id"]))
        assert error(lambda: service.answer("Теперь четыре тренировки")) == PERIOD_ERROR
        assert "дата окончания" in PERIOD_ERROR and "бессрочно" in PERIOD_ERROR
        assert facts(service) == original
        client.generate.assert_not_called()
        with store.connect() as db:
            assert (
                db.execute(
                    "SELECT COUNT(*) FROM sticky_facts WHERE category!='measurements'"
                ).fetchone()[0]
                == 1
            )


def test_end_actions_require_consistent_fields_and_preserve_safety_constraints():
    with tempfile.TemporaryDirectory() as directory:
        store, client, service = fixture(directory)
        for changes in (
            {"valid_until_action": "set"},
            {"valid_until_action": "keep", "valid_until": (NOW + timedelta(days=1)).isoformat()},
            {"valid_until_action": "clear", "valid_until": (NOW + timedelta(days=1)).isoformat()},
            {"category": "food_restrictions", "valid_until": (NOW + timedelta(days=1)).isoformat()},
        ):
            extract(client, operation(**changes))
            assert error(lambda: service.answer("Не люблю брокколи")) == MEMORY_ERROR
        assert not facts(service)
        client.generate.assert_not_called()


def test_scheduled_replacement_keeps_previous_version_until_boundary_after_restart():
    with tempfile.TemporaryDirectory() as directory:
        store, client, service = fixture(directory)
        extract(client, routine("Три тренировки"))
        service.answer("Три тренировки")
        original = facts(service)[0]
        start = NOW + timedelta(days=7)
        schedule(service, client, "С 21 ноября четыре тренировки", start)
        replacement = facts(service)[0]
        assert replacement["id"] != original["id"]
        current = context_facts(service, NOW)
        assert [fact["id"] for fact in current] == [original["id"]]
        assert current[0]["valid_until"] == start.isoformat()
        assert "Три тренировки" in service.memory.describe()
        reopened = FitnessService(Storage(directory), client, clock=lambda: NOW)
        assert [fact["id"] for fact in context_facts(reopened, start - timedelta(seconds=1))] == [
            original["id"]
        ]
        assert [fact["id"] for fact in context_facts(reopened, start)] == [replacement["id"]]
        assert not reopened.memory.repository.load(start)["previous_versions"]


def test_postponed_and_corrected_pending_version_does_not_activate_early():
    with tempfile.TemporaryDirectory() as directory:
        store, client, service = fixture(directory)
        extract(client, routine("Три тренировки"))
        service.answer("Три тренировки")
        start = NOW + timedelta(days=7)
        schedule(service, client, "С 21 ноября четыре тренировки", start)
        schedule(service, client, "Переносим на 28 ноября", NOW + timedelta(days=14))
        assert context_facts(service, start)[0]["value"]["text"] == "Три тренировки"
        extract(client, routine("Нет, пять тренировок", fact_id=facts(service)[0]["id"]))
        service.answer("Нет, пять тренировок")
        assert facts(service)[0]["valid_from"] == (NOW + timedelta(days=14)).isoformat()
        assert (
            context_facts(service, NOW + timedelta(days=10))[0]["value"]["text"] == "Три тренировки"
        )
        assert (
            context_facts(service, NOW + timedelta(days=14))[0]["value"]["text"]
            == "Нет, пять тренировок"
        )


def test_later_change_preserves_started_revision_and_immediate_change_stops_predecessors():
    with tempfile.TemporaryDirectory() as directory:
        store, client, service = fixture(directory)
        extract(client, routine("Три тренировки"))
        service.answer("Три тренировки")
        schedule(service, client, "С 21 ноября четыре тренировки", NOW + timedelta(days=7))
        service.clock = lambda: NOW + timedelta(days=8)
        schedule(service, client, "С 28 ноября пять тренировок", NOW + timedelta(days=14))
        assert (
            context_facts(service, NOW + timedelta(days=9))[0]["value"]["text"]
            == "С 21 ноября четыре тренировки"
        )
        extract(
            client,
            routine(
                "Сейчас шесть тренировок",
                fact_id=facts(service)[0]["id"],
                valid_from=service.clock().isoformat(),
            ),
        )
        service.answer("Сейчас шесть тренировок")
        assert [f["value"]["text"] for f in context_facts(service, NOW + timedelta(days=9))] == [
            "Сейчас шесть тренировок"
        ]


def test_revocation_and_local_deletion_stop_current_and_pending_versions():
    for delete in (False, True):
        with tempfile.TemporaryDirectory() as directory:
            store, client, service = fixture(directory)
            extract(client, routine("Три тренировки"))
            service.answer("Три тренировки")
            schedule(service, client, "С 21 ноября четыре тренировки", NOW + timedelta(days=7))
            head = facts(service)[0]
            if delete:
                service.memory.command(f"/memory delete {head['id']}")
            else:
                extract(client, routine("Отменяю этот режим", action="revoke", fact_id=head["id"]))
                service.answer("Отменяю этот режим")
            reopened = FitnessService(Storage(directory), client, clock=lambda: NOW)
            assert not context_facts(reopened, NOW)
            assert not context_facts(reopened, NOW + timedelta(days=8))


def test_failed_answer_cannot_change_scheduled_transition_or_clear_end():
    with tempfile.TemporaryDirectory() as directory:
        store, client, service = fixture(directory)
        extract(
            client, routine("Три тренировки", valid_until=(NOW + timedelta(days=30)).isoformat())
        )
        service.answer("Три тренировки")
        schedule(service, client, "С 21 ноября четыре тренировки", NOW + timedelta(days=7))
        original = service.memory.repository.load(NOW)
        client.generate.side_effect = ValueError("Не удалось получить ответ.")
        extract(
            client,
            routine(
                "Теперь пять бессрочно", fact_id=facts(service)[0]["id"], valid_until_action="clear"
            ),
        )
        error(lambda: service.answer("Теперь пять бессрочно"))
        assert service.memory.repository.load(NOW) == original


def test_transaction_failure_rolls_back_rescheduled_predecessor_and_new_revision():
    with tempfile.TemporaryDirectory() as directory:
        store, client, service = fixture(directory)
        extract(client, routine("Три тренировки"))
        service.answer("Три тренировки")
        schedule(service, client, "С 21 ноября четыре тренировки", NOW + timedelta(days=7))
        original = service.memory.repository.load(NOW)
        with store.connect() as db:
            db.execute("""CREATE TRIGGER fail_scheduled_turn BEFORE INSERT ON memory_turns
                          BEGIN SELECT RAISE(ABORT, 'TEST_SECRET'); END""")
        extract(
            client,
            routine(
                "Переносим на 28 ноября",
                fact_id=facts(service)[0]["id"],
                valid_from=(NOW + timedelta(days=14)).isoformat(),
            ),
        )
        try:
            service.answer("Переносим на 28 ноября")
        except sqlite3.IntegrityError:
            pass
        else:
            raise AssertionError("Expected simulated transaction failure")
        assert service.memory.repository.load(NOW) == original
        assert [f["value"]["text"] for f in context_facts(service, NOW + timedelta(days=8))] == [
            "С 21 ноября четыре тренировки"
        ]
        assert "TEST_SECRET" not in store.logs()


def test_manual_text_edit_preserves_original_period_including_expired_or_pending():
    for future in (False, True):
        with tempfile.TemporaryDirectory() as directory:
            store, client, service = fixture(directory)
            start = NOW + timedelta(days=1) if future else NOW - timedelta(days=2)
            end = start + timedelta(days=1)
            extract(
                client,
                routine(
                    "Три тренировки", valid_from=start.isoformat(), valid_until=end.isoformat()
                ),
            )
            service.answer("Три тренировки")
            old = facts(service)[0]
            service.memory.command(f"/memory edit {old['id']} Четыре тренировки")
            new = facts(service)[0]
            assert (new["valid_from"], new["valid_until"]) == (
                old["valid_from"],
                old["valid_until"],
            )


def test_transition_preserves_timezone_and_microsecond_precision():
    with tempfile.TemporaryDirectory() as directory:
        store, client, service = fixture(directory)
        extract(client, routine("Три тренировки"))
        service.answer("Три тренировки")
        extract(
            client,
            routine(
                "Четыре тренировки с новой даты",
                fact_id=facts(service)[0]["id"],
                valid_from="2026-11-21T15:00:00.123456+03:00",
            ),
        )
        service.answer("Четыре тренировки с новой даты")
        start = (NOW + timedelta(days=7)).replace(microsecond=123456)
        assert (
            context_facts(service, start - timedelta(microseconds=1))[0]["value"]["text"]
            == "Три тренировки"
        )
        assert context_facts(service, start)[0]["value"]["text"] == "Четыре тренировки с новой даты"


def test_migration_restores_predecessor_of_old_pending_revision_without_reviving_revoked_fact():
    for revoked in (False, True):
        with tempfile.TemporaryDirectory() as directory:
            store, client, service = fixture(directory)
            extract(client, routine("Три тренировки"))
            service.answer("Три тренировки")
            schedule(service, client, "С 21 ноября четыре тренировки", NOW + timedelta(days=7))
            schedule(service, client, "Переносим на 28 ноября", NOW + timedelta(days=14))
            if revoked:
                extract(
                    client,
                    routine("Отменяю режим", action="revoke", fact_id=facts(service)[0]["id"]),
                )
                service.answer("Отменяю режим")
            # Rebuild only this disposable table in the pre-fix schema.
            with store.connect() as db:
                schema = db.execute(
                    "SELECT sql FROM sqlite_master WHERE name='sticky_facts'"
                ).fetchone()[0]
                columns = [
                    row["name"]
                    for row in db.execute("PRAGMA table_info(sticky_facts)")
                    if row["name"] != "superseded_at"
                ]
                db.execute(
                    schema.replace("sticky_facts", "old_facts", 1).replace(
                        ", superseded_at TEXT", ""
                    )
                )
                db.execute(f"INSERT INTO old_facts SELECT {','.join(columns)} FROM sticky_facts")
                db.execute("DROP TABLE sticky_facts")
                db.execute("ALTER TABLE old_facts RENAME TO sticky_facts")
            reopened = FitnessService(Storage(directory), client, clock=lambda: NOW)
            current = context_facts(reopened, NOW + timedelta(days=10))
            if revoked:
                assert not current
            else:
                assert [fact["value"]["text"] for fact in current] == ["Три тренировки"]
                assert current[0]["valid_until"] == (NOW + timedelta(days=14)).isoformat()
            # Opening the database again must not overwrite the migrated boundaries.
            again = FitnessService(Storage(directory), client, clock=lambda: NOW)
            assert context_facts(again, NOW + timedelta(days=10)) == current


def load_tests(loader, tests, pattern):
    return unittest.TestSuite(
        unittest.FunctionTestCase(value)
        for name, value in globals().items()
        if name.startswith("test_") and callable(value)
    )
