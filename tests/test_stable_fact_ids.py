"""Fact IDs are permanent; journal entries, not new fact rows, retain previous values."""

import json
import sqlite3
import tempfile
import unittest
from datetime import timedelta

from support import safe_response, safety_json, seed_legacy_fact_versions
from test_memory import NOW, error, extract, facts, fixture, operation

from fitagent.service import FitnessService
from fitagent.storage import Storage


def test_chat_edit_repeat_and_manual_edit_keep_one_id_and_full_journal_after_restart():
    with tempfile.TemporaryDirectory() as directory:
        store, client, service = fixture(directory)
        extract(client, operation("Брокколи не люблю"))
        service.answer("Брокколи не люблю")
        first = facts(service)[0]
        expected_next = service.memory.repository.load(NOW)["next_id"]
        values = ["Брокколи не люблю", "Брокколи нравится", "Брокколи очень нравится"]
        for day, text in enumerate(values[1:], 1):
            service.clock = lambda: NOW + timedelta(days=day)
            extract(client, operation(text, fact_id=first["id"]))
            service.answer(text)
            current = facts(service)[0]
            assert current["id"] == first["id"]
            assert current["created_at"] == first["created_at"]
            assert current["value"]["text"] == text
            assert service.memory.repository.load(NOW)["next_id"] == expected_next
        service.clock = lambda: NOW + timedelta(days=3)
        service.answer(values[-1])
        service.memory.command(f"/memory edit {first['id']} Предпочитаю брокколи на пару")
        reopened = FitnessService(Storage(directory), client, clock=service.clock)
        assert facts(reopened)[0]["id"] == first["id"]
        assert facts(reopened)[0]["value"]["text"] == "Предпочитаю брокколи на пару"
        history = reopened.memory.command(f"/memory history {first['id']}")
        assert f"Журнал изменений факта №{first['id']}" in history
        assert all(text in history for text in values)
        assert "Предпочитаю брокколи на пару" in history
        assert "актуальность подтверждена" in history
        with store.connect() as db:
            assert (
                db.execute(
                    "SELECT COUNT(*) FROM sticky_facts WHERE category='food_preferences'"
                ).fetchone()[0]
                == 1
            )
            records = db.execute(
                "SELECT action,old_value,new_value FROM memory_journal WHERE lineage_id=? ORDER BY id",
                (first["lineage_id"],),
            ).fetchall()
            assert [r["action"] for r in records] == [
                "update",
                "update",
                "update",
                "confirm",
                "update",
            ]
            assert json.loads(records[1]["old_value"])["text"] == values[0]
            assert json.loads(records[1]["new_value"])["text"] == values[1]
            assert (
                db.execute(
                    "SELECT COUNT(*) FROM fact_sources WHERE fact_id=?", (first["id"],)
                ).fetchone()[0]
                == 4
            )
        public = store.activity_logs()
        assert public.count(f"Факт №{first['id']} обновлён") == 3
        assert f"Актуальность факта подтверждена  №{first['id']}" in public
        assert "→" not in public


def test_updating_a_fact_does_not_consume_the_number_of_a_new_independent_fact():
    with tempfile.TemporaryDirectory() as directory:
        store, client, service = fixture(directory)
        extract(client, operation())
        service.answer("Не люблю брокколи")
        original = facts(service)[0]
        next_id = service.memory.repository.load(NOW)["next_id"]
        extract(client, operation("Люблю брокколи", fact_id=original["id"]))
        service.answer("Люблю брокколи")
        extract(client, operation("Есть гантели", category="equipment", key="dumbbells"))
        service.answer("Есть гантели")
        equipment = next(f for f in facts(service) if f["category"] == "equipment")
        assert equipment["id"] == next_id
        history = service.memory.command(f"/memory history {equipment['id']}")
        assert "Есть гантели" in history and "брокколи" not in history


def test_stable_fact_update_rolls_back_value_sources_and_journal_with_failed_turn():
    with tempfile.TemporaryDirectory() as directory:
        store, client, service = fixture(directory)
        extract(client, operation())
        service.answer("Не люблю брокколи")
        original = facts(service)[0]
        history = service.memory.command(f"/memory history {original['id']}")
        with store.connect() as db:
            db.execute(
                "CREATE TRIGGER fail_stable BEFORE INSERT ON memory_turns BEGIN SELECT RAISE(ABORT,'PRIVATE'); END"
            )
        extract(client, operation("Люблю брокколи", fact_id=original["id"]))
        try:
            service.answer("Люблю брокколи")
        except sqlite3.IntegrityError:
            pass
        else:
            raise AssertionError("Expected rolled back update")
        assert facts(service)[0] == original
        assert service.memory.command(f"/memory history {original['id']}") == history
        assert f"Факт №{original['id']} обновлён" not in store.activity_logs()
        with store.connect() as db:
            assert (
                db.execute(
                    "SELECT COUNT(*) FROM fact_sources WHERE fact_id=?", (original["id"],)
                ).fetchone()[0]
                == 1
            )


def test_preexisting_versioned_fact_keeps_its_current_number_and_older_journal():
    with tempfile.TemporaryDirectory() as directory:
        store, client, service = fixture(directory)
        extract(client, operation("Не люблю брокколи"))
        service.answer("Не люблю брокколи")
        old = dict(facts(service)[0], valid_from=(NOW - timedelta(days=1)).isoformat())
        extract(client, operation("Люблю брокколи", fact_id=old["id"]))
        service.answer("Люблю брокколи")
        seed_legacy_fact_versions(store, [old, facts(service)[0]])
        reopened = FitnessService(Storage(directory), client, clock=lambda: NOW)
        current = facts(reopened)[0]
        assert current["id"] != old["id"]  # Only this fixture simulates the historical format.
        extract(client, operation("Очень люблю брокколи", fact_id=current["id"]))
        reopened.answer("Очень люблю брокколи")
        assert facts(reopened)[0]["id"] == current["id"]
        history = reopened.memory.command(f"/memory history {current['id']}")
        assert all(
            text in history
            for text in ("Не люблю брокколи", "Люблю брокколи", "Очень люблю брокколи")
        )
        assert len(facts(reopened)) == 1


def test_critical_update_requires_both_old_and_new_safety_checks_for_one_fact_number():
    with tempfile.TemporaryDirectory() as directory:
        store, client, service = fixture(directory)
        extract(client, operation("Аллергия на орехи", category="food_restrictions", key="nuts"))
        service.answer("Аллергия на орехи")
        old = facts(service)[0]
        revised = operation(
            "Аллергии на орехи нет", category="food_restrictions", key="nuts", fact_id=old["id"]
        )
        extract(client, revised)
        service.answer(revised["source_quote"])
        client.generate_safety.side_effect = lambda messages, stage: safety_json(
            ["system", f"fact:{old['id']}"]
        )
        error(lambda: service.chat("/confirm 1"), "сбой проверки")
        assert facts(service)[0] == old
        client.generate_safety.side_effect = safe_response
        service.chat("/confirm 1")
        assert facts(service)[0]["id"] == old["id"]
        assert facts(service)[0]["value"] == revised["value"]
        payload = json.loads(client.generate_safety.call_args.args[0][1]["content"])
        assert {r["rule_id"] for r in payload["rules"] + payload["previous_rules"]} == {
            f"fact:{old['id']}",
            f"previous:{old['id']}",
        }


def test_old_fact_without_journal_gets_one_honest_initial_snapshot_without_renumbering():
    with tempfile.TemporaryDirectory() as directory:
        store, client, service = fixture(directory)
        extract(client, operation())
        service.answer("Не люблю брокколи")
        original = facts(service)[0]
        with store.connect() as db:
            db.execute("DELETE FROM memory_journal WHERE lineage_id=?", (original["lineage_id"],))
        client.generate.reset_mock()
        client.generate_structured.reset_mock()
        client.generate_safety.reset_mock()
        reopened = FitnessService(Storage(directory), client, clock=lambda: NOW)
        history = reopened.memory.command(f"/memory history {original['id']}")
        assert "снимок сохранённых данных при обновлении приложения" in history
        assert "Не люблю брокколи" in history
        assert facts(reopened)[0] == original
        with store.connect() as db:
            rows = db.execute(
                "SELECT action,old_value,new_value FROM memory_journal WHERE lineage_id=?",
                (original["lineage_id"],),
            ).fetchall()
            assert len(rows) == 1 and rows[0]["action"] == "snapshot"
            assert rows[0]["old_value"] is None
        again = FitnessService(Storage(directory), client, clock=lambda: NOW)
        assert again.memory.command(f"/memory history {original['id']}") == history
        client.generate.assert_not_called()
        client.generate_structured.assert_not_called()
        client.generate_safety.assert_not_called()


def load_tests(loader, tests, pattern):
    return unittest.TestSuite(
        unittest.FunctionTestCase(value)
        for name, value in globals().items()
        if name.startswith("test_") and callable(value)
    )
