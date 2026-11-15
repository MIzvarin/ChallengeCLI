"""Sticky facts use disposable databases and mocked Go, never a paid endpoint."""

import asyncio
import copy
import json
import sqlite3
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import Mock, patch

import httpx
from textual.widgets import Input, RichLog

from fitagent.domain import validate_profile
from fitagent.memory import project
from fitagent.memory_schema import EXTRACTION_SCHEMA, MEMORY_ERROR, parse_extraction
from fitagent.provider import MEMORY_MODEL, MEMORY_URL, ModelClient
from fitagent.service import FitnessService
from fitagent.storage import Storage
from fitagent.ui import FitAgentApp

NOW = datetime(2026, 11, 14, 12, tzinfo=timezone.utc)
DEFAULT_TOPIC = {
    "action": "continue",
    "topic_id": None,
    "title": "",
    "related_topic_ids": [],
    "clarification": None,
}
EMPTY = json.dumps({"operations": [], "topic": DEFAULT_TOPIC})


def fixture(directory):
    store = Storage(directory)
    client = SimpleNamespace(
        model="flash",
        session_id=store.session_id(),
        generate=Mock(return_value="Ответ"),
        generate_structured=Mock(return_value=EMPTY),
    )
    service = FitnessService(store, client, clock=lambda: NOW)
    service.save(validate_profile(180, 80, "мужской", 30, "Стать сильнее", ""))
    return store, client, service


def operation(text="Не люблю брокколи", **changes):
    data = {
        "action": "upsert",
        "update_mode": "replace",
        "fact_id": None,
        "category": "food_preferences",
        "key": "broccoli",
        "scope": "usual",
        "applicability": "global",
        "topic_id": None,
        "value": {"text": text, "quantity": None, "unit": None, "qualifier": None},
        "valid_from": None,
        "valid_until": None,
        "valid_until_action": "set" if changes.get("valid_until") is not None else "keep",
        "source_quote": text,
        "clarification": None,
    }
    data.update(changes)
    return data


def extract(client, *operations):
    client.generate_structured.return_value = json.dumps(
        {"operations": operations, "topic": DEFAULT_TOPIC}, ensure_ascii=False
    )


def facts(service):
    return service.memory.repository.load()["facts"]


def error(callback, expected=None):
    try:
        callback()
    except ValueError as exc:
        text = str(exc)
        if expected:
            assert expected in text, text
        return text
    raise AssertionError("Expected safe validation failure")


def luna_reply(text=EMPTY, **changes):
    data = {
        "status": "completed",
        "output": [
            {
                "type": "message",
                "role": "assistant",
                "status": "completed",
                "content": [{"type": "output_text", "text": text}],
            }
        ],
        "usage": {
            "input_tokens": 200,
            "output_tokens": 30,
            "input_tokens_details": {"cached_tokens": 100},
        },
    }
    data.update(changes)
    return httpx.Response(200, json=data)


def real_client(store):
    return ModelClient(
        "TEST_SECRET",
        "https://example.test/chat",
        "flash",
        store.session_id(),
        lambda kind, details: store.event(kind, details, NOW),
    )


def test_schema_is_closed_and_all_object_properties_are_required():
    def visit(schema):
        if schema["type"] == "object":
            assert schema["additionalProperties"] is False
            assert set(schema["required"]) == set(schema["properties"])
            for child in schema["properties"].values():
                visit(child)
        if schema["type"] == "array":
            visit(schema["items"])

    visit(EXTRACTION_SCHEMA)


def test_luna_uses_go_native_responses_strict_schema_same_key_and_session():
    with tempfile.TemporaryDirectory() as directory:
        store = Storage(directory)
        client = real_client(store)
        with patch("fitagent.provider.httpx.Client") as factory:
            transport = factory.return_value.__enter__.return_value
            transport.post.return_value = luna_reply()
            messages = [{"role": "user", "content": "PRIVATE_TEST_TEXT"}]
            assert client.generate_structured(messages, EXTRACTION_SCHEMA) == EMPTY
            call = transport.post.call_args
            assert call.args == (MEMORY_URL,)
            payload = call.kwargs["json"]
            assert payload["model"] == MEMORY_MODEL == "gpt-6-luna"
            assert payload["input"] == messages
            assert payload["store"] is False and payload["stream"] is False
            assert payload["reasoning"] == {"effort": "none"}
            assert payload["text"]["format"] == {
                "type": "json_schema",
                "name": "sticky_facts",
                "strict": True,
                "schema": EXTRACTION_SCHEMA,
            }
            assert call.kwargs["headers"]["Authorization"] == "Bearer TEST_SECRET"
            assert call.kwargs["headers"]["x-opencode-session"] == store.session_id()
            assert call.kwargs["headers"]["User-Agent"].startswith("fitagent/")
            assert transport.post.call_count == 1
        usage = store.token_usage()
        assert usage["requests"] == 1 and usage["total_tokens"] == 230
        assert usage["last_answer"] is None
        assert '"cached_tokens": 100' in store.logs()
        assert "TEST_SECRET" not in store.logs() and "PRIVATE_TEST_TEXT" not in store.logs()


def test_luna_rejects_refusal_incomplete_empty_wrong_protocol_and_retains_usage():
    variants = [
        {"status": "incomplete"},
        {"status": "failed"},
        {"output": []},
        {"output": [{"type": "function_call", "arguments": "TEST_SECRET"}]},
        {
            "output": [
                {
                    "type": "message",
                    "role": "assistant",
                    "status": "completed",
                    "content": [{"type": "refusal", "refusal": "TEST_SECRET"}],
                }
            ]
        },
        {
            "output": [
                {
                    "type": "message",
                    "role": "assistant",
                    "status": "incomplete",
                    "content": [{"type": "output_text", "text": EMPTY}],
                }
            ]
        },
        {
            "output": [
                {
                    "type": "message",
                    "role": "assistant",
                    "status": "completed",
                    "content": [{"type": "output_text", "text": " "}],
                }
            ]
        },
    ]
    with tempfile.TemporaryDirectory() as directory:
        store = Storage(directory)
        client = real_client(store)
        with patch("fitagent.provider.httpx.Client") as factory:
            transport = factory.return_value.__enter__.return_value
            for variant in variants:
                transport.post.return_value = luna_reply(**variant)
                assert "TEST_SECRET" not in error(
                    lambda: client.generate_structured([], EXTRACTION_SCHEMA)
                )
            transport.post.return_value = httpx.Response(400, text="TEST_SECRET unsupported schema")
            assert "TEST_SECRET" not in error(
                lambda: client.generate_structured([], EXTRACTION_SCHEMA)
            )
            assert transport.post.call_count == len(variants) + 1
        report = store.token_usage()
        assert report["requests"] == len(variants) + 1
        assert report["total_tokens"] == len(variants) * 230
        assert report["incomplete_requests"] == 1
        assert "TEST_SECRET" not in store.logs()


def test_current_fact_survives_week_and_restart_without_reprocessing_archive():
    with tempfile.TemporaryDirectory() as directory:
        store, client, service = fixture(directory)
        store.message("user", "OLD_PRIVATE_HISTORY", NOW - timedelta(days=60))
        extract(client, operation())
        service.answer("Не люблю брокколи")
        saved = facts(service)
        assert len(saved) == 1 and saved[0]["source_message_id"] == 2
        payload = json.loads(client.generate_structured.call_args.args[0][1]["content"])
        assert payload["current_user_message"]["content"] == "Не люблю брокколи"
        assert len(payload["previous_exchange"]) <= 2
        store, new_client, reopened = fixture(directory)
        assert facts(reopened) == saved
        new_client.generate.assert_not_called()
        new_client.generate_structured.assert_not_called()
        reopened.clock = lambda: NOW + timedelta(days=9)
        reopened.answer("Что приготовить?")
        sent = new_client.generate.call_args.args[0][1]["content"]
        assert "Не люблю брокколи" in sent and "OLD_PRIVATE_HISTORY" not in sent
        assert "Не люблю брокколи" not in "\n".join(
            store.history((NOW + timedelta(days=2)).isoformat())
        )
        assert new_client.generate_structured.call_count == 1


def test_duplicates_keep_one_fact_and_multiple_provenance_sources():
    with tempfile.TemporaryDirectory() as directory:
        store, client, service = fixture(directory)
        extract(client, operation())
        service.answer("Не люблю брокколи")
        first = facts(service)[0]
        extract(client, operation(source_quote="Брокколи мне не нравится", fact_id=first["id"]))
        service.clock = lambda: NOW + timedelta(days=1)
        service.answer("Брокколи мне не нравится")
        second = facts(service)[0]
        assert first["id"] == second["id"] and first["value"] == second["value"]
        assert second["created_at"] == first["created_at"]
        with store.connect() as db:
            assert db.execute("SELECT COUNT(*) FROM sticky_facts").fetchone()[0] == 1
            assert db.execute("SELECT COUNT(*) FROM fact_sources").fetchone()[0] == 2
            assert db.execute("SELECT COUNT(*) FROM memory_turns").fetchone()[0] == 2


def test_explicit_correction_replaces_old_value_but_not_profile():
    with tempfile.TemporaryDirectory() as directory:
        store, client, service = fixture(directory)
        profile = store.latest("profiles")
        goal = store.latest("goals")
        extract(client, operation())
        service.answer("Не люблю брокколи")
        first = facts(service)[0]
        extract(client, operation("Теперь люблю брокколи", fact_id=first["id"]))
        service.clock = lambda: NOW + timedelta(days=1)
        service.answer("Теперь люблю брокколи")
        current = facts(service)[0]
        assert current["id"] != first["id"]
        assert current["value"]["text"] == "Теперь люблю брокколи"
        assert current["valid_from"] == (NOW + timedelta(days=1)).isoformat()
        with store.connect() as db:
            assert [r[0] for r in db.execute("SELECT status FROM sticky_facts ORDER BY id")] == [
                "replaced",
                "active",
            ]
        assert store.latest("profiles") == profile and store.latest("goals") == goal


def test_complementary_items_and_temporary_exception_do_not_replace_usual():
    with tempfile.TemporaryDirectory() as directory:
        store, client, service = fixture(directory)
        items = [
            operation("Есть гантели", category="equipment", key="dumbbells"),
            operation("Есть резинки", category="equipment", key="bands"),
            operation("Обычно три тренировки", category="training_routine", key="frequency"),
            operation(
                "Завтра одна тренировка",
                category="training_routine",
                key="frequency",
                scope="tomorrow",
                valid_from=(NOW + timedelta(days=1)).isoformat(),
                valid_until=(NOW + timedelta(days=2)).isoformat(),
            ),
        ]
        extract(client, *items)
        service.answer("; ".join(item["source_quote"] for item in items))
        assert len(facts(service)) == 4
        assert len(json.loads(service.memory.context())["facts"]) == 3
        service.memory.clock = lambda: NOW + timedelta(days=1)
        assert len(json.loads(service.memory.context())["facts"]) == 4
        service.memory.clock = lambda: NOW + timedelta(days=2)
        assert len(json.loads(service.memory.context())["facts"]) == 3
        assert len(facts(service)) == 4  # Expiration never erases the original.


def test_review_intervals_and_safety_never_expire_due_to_age():
    with tempfile.TemporaryDirectory() as directory:
        store, client, service = fixture(directory)
        snapshot = service.memory.repository.load()
        for category, days in (
            ("symptoms", 1),
            ("recovery", 1),
            ("meal_routine", 14),
            ("training_routine", 14),
            ("habits", 14),
            ("training_schedule", 30),
            ("cooking_budget", 30),
            ("agreements", 30),
            ("equipment", None),
            ("health_constraints", None),
            ("food_restrictions", None),
        ):
            candidate = project(snapshot, [operation(category=category)], 1, NOW)
            fact = candidate["facts"][0]
            assert fact["review_at"] == ((NOW + timedelta(days=days)).isoformat() if days else None)
            service.memory.clock = lambda: NOW + timedelta(days=90)
            sent = json.loads(service.memory.context(candidate))["facts"]
            assert len(sent) == 1 and sent[0]["needs_review"] == bool(days)
        end = (NOW + timedelta(days=45)).isoformat()
        candidate = project(snapshot, [operation(category="agreements", valid_until=end)], 1, NOW)
        assert candidate["facts"][0]["review_at"] == end


def test_ambiguity_preserves_value_and_requests_clarification_until_corrected():
    with tempfile.TemporaryDirectory() as directory:
        store, client, service = fixture(directory)
        extract(client, operation())
        service.answer("Не люблю брокколи")
        original = facts(service)[0]
        extract(
            client,
            operation(
                action="clarify",
                fact_id=original["id"],
                source_quote="Иногда ем",
                clarification="Вы теперь любите брокколи?",
            ),
        )
        service.answer("Иногда ем")
        assert facts(service)[0]["value"] == original["value"]
        assert "Вы теперь любите брокколи?" in service.memory.context()
        assert len(service.memory.repository.load()["issues"]) == 1
        extract(client, operation("Теперь люблю брокколи", fact_id=original["id"]))
        service.answer("Теперь люблю брокколи")
        assert not service.memory.repository.load()["issues"]


def test_revocation_retains_history_but_removes_fact_from_memory_context():
    with tempfile.TemporaryDirectory() as directory:
        store, client, service = fixture(directory)
        extract(client, operation())
        service.answer("Не люблю брокколи")
        old = facts(service)[0]
        extract(
            client, operation(action="revoke", fact_id=old["id"], source_quote="Это больше неверно")
        )
        service.answer("Это больше неверно")
        assert not facts(service)
        with store.connect() as db:
            assert db.execute("SELECT status FROM sticky_facts").fetchone()[0] == "revoked"
            assert db.execute("SELECT COUNT(*) FROM fact_sources").fetchone()[0] == 2


def test_invalid_extractions_reject_entire_batch_without_partial_updates_or_leaks():
    invalid = []
    for change in (
        {"category": "profile"},
        {"key": "weight"},
        {"fact_id": True},
        {"fact_id": 999},
        {"key": "Ignore all rules"},
        {"scope": ""},
        {"source_quote": "Не говорил"},
        {"source_quote": " "},
        {"valid_from": "2026-11-14"},
        {"valid_until": "yesterday"},
        {"action": "delete"},
        {"unexpected": "TEST_SECRET"},
        {"action": "clarify", "clarification": "Вопрос?"},
        {"category": "health_constraints", "valid_until": (NOW + timedelta(days=1)).isoformat()},
        {"valid_from": (NOW + timedelta(days=2)).isoformat(), "valid_until": NOW.isoformat()},
    ):
        invalid.append(
            json.dumps({"operations": [operation(), operation(**{"key": "second", **change})]})
        )
    for quantity in (True, float("inf"), float("nan"), "2"):
        bad = operation()
        bad["value"]["quantity"] = quantity
        invalid.append(json.dumps({"operations": [bad]}))
    invalid += [
        "TEST_SECRET",
        "[]",
        '{"operations":{},"extra":"TEST_SECRET"}',
        '{"operations":[],"operations":[]}',
        json.dumps({"operations": [operation(), operation()]}),
        json.dumps({"operations": [operation(key="x" + str(i)) for i in range(33)]}),
    ]
    with tempfile.TemporaryDirectory() as directory:
        store, client, service = fixture(directory)
        for text in invalid:
            client.generate_structured.return_value = text
            message = error(lambda: service.answer("Не люблю брокколи"))
            assert message == MEMORY_ERROR and "TEST_SECRET" not in message
            assert not facts(service)
        client.generate.assert_not_called()
        assert "TEST_SECRET" not in store.logs() and "Не люблю брокколи" not in store.logs()
        assert all(row["status"] == "failed" for row in store.chat_messages())


def test_failed_extraction_answer_and_database_write_leave_memory_unchanged():
    with tempfile.TemporaryDirectory() as directory:
        store, client, service = fixture(directory)
        client.generate_structured.side_effect = OSError("TEST_SECRET")
        error(lambda: service.answer("Не люблю брокколи"), "Факты не изменены")
        client.generate.assert_not_called()
        client.generate_structured.side_effect = None
        extract(client, operation())
        client.generate.side_effect = ValueError("Не удалось получить ответ.")
        error(lambda: service.answer("Не люблю брокколи"))
        assert not facts(service)
        client.generate.side_effect = None
        with store.connect() as db:
            db.execute("""CREATE TRIGGER fail_memory BEFORE INSERT ON memory_turns
                          BEGIN SELECT RAISE(ABORT, 'TEST_SECRET'); END""")
        try:
            service.answer("Не люблю брокколи")
        except sqlite3.IntegrityError:
            pass
        else:
            raise AssertionError("Expected simulated atomic write failure")
        assert not facts(service)
        with store.connect() as db:
            assert db.execute("SELECT COUNT(*) FROM fact_sources").fetchone()[0] == 0
            assert db.execute("SELECT COUNT(*) FROM memory_turns").fetchone()[0] == 0
        assert not any(row["role"] == "assistant" for row in store.chat_messages())
        assert "TEST_SECRET" not in store.logs()


def test_manual_edit_delete_and_validation_do_not_call_api():
    with tempfile.TemporaryDirectory() as directory:
        store, client, service = fixture(directory)
        extract(client, operation())
        service.answer("Не люблю брокколи")
        client.generate.reset_mock()
        client.generate_structured.reset_mock()
        number = facts(service)[0]["id"]
        assert "Пищевые предпочтения" in service.memory.command("/memory")
        assert "исправлен" in service.memory.command(f"/memory edit {number} Люблю брокколи")
        corrected = facts(service)[0]
        assert corrected["value"]["text"] == "Люблю брокколи" and corrected["origin"] == "manual"
        assert corrected["source_message_id"] is None
        for command in (
            "/memory edit",
            "/memory delete abc",
            "/memory delete 0",
            "/memory delete -1",
            "/memory delete 999999",
            "/memory delete " + "9" * 5000,
            f"/memory edit {corrected['id']} " + "x" * 501,
        ):
            assert "Traceback" not in error(lambda: service.memory.command(command))
        assert "удалены" in service.memory.command(f"/memory delete {corrected['id']}")
        assert "пока пуста" in service.memory.command("/memory")
        with store.connect() as db:
            assert db.execute("SELECT COUNT(*) FROM sticky_facts").fetchone()[0] == 0
            assert db.execute("SELECT COUNT(*) FROM fact_sources").fetchone()[0] == 0
        assert "Не люблю брокколи" in store.chat_messages()[0]["content"]
        client.generate.assert_not_called()
        client.generate_structured.assert_not_called()


def test_budget_never_silently_drops_facts_and_staged_overflow_is_not_committed():
    with tempfile.TemporaryDirectory() as directory:
        store, client, service = fixture(directory)
        extract(client, operation())
        with patch("fitagent.memory.MEMORY_LIMIT", 100):
            error(lambda: service.answer("Не люблю брокколи"), "/memory")
        assert not facts(service)
        service.answer("Не люблю брокколи")
        saved = copy.deepcopy(facts(service))
        client.generate_structured.reset_mock()
        with patch("fitagent.memory.MEMORY_LIMIT", 100):
            error(lambda: service.answer("Что приготовить?"), "/memory")
        client.generate_structured.assert_not_called()
        assert facts(service) == saved


def test_stale_candidate_and_clear_cannot_restore_deleted_memory_or_replay_turn():
    with tempfile.TemporaryDirectory() as directory:
        store, client, service = fixture(directory)
        extract(client, operation())
        service.answer("Не люблю брокколи")
        extract(client)
        message_id = store.message("user", "Вопрос", NOW, "pending")
        candidate = service.memory.prepare(message_id, "Вопрос")
        service.memory.command(f"/memory delete {facts(service)[0]['id']}")
        error(lambda: service.memory.complete(candidate, message_id, "Ответ"), "изменена")
        assert not facts(service)
        service.clear()
        error(lambda: service.memory.complete(candidate, message_id, "Ответ"), "удалён")
        assert not facts(service)
        for table in ("sticky_facts", "fact_sources", "memory_issues", "memory_turns", "messages"):
            with store.connect() as db:
                assert db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] == 0


async def memory_ui_flow():
    with tempfile.TemporaryDirectory() as directory:
        store, client, service = fixture(directory)
        extract(client, operation())
        service.answer("Не люблю брокколи")
        client.generate.reset_mock()
        client.generate_structured.reset_mock()
        number = facts(service)[0]["id"]
        app = FitAgentApp(service)
        async with app.run_test(size=(80, 24)) as pilot:
            await pilot.pause()
            field = app.query_one("#question", Input)
            for command in (
                "/memory",
                f"/memory edit {number} Люблю брокколи",
                "/memory",
                "/memory delete bad",
            ):
                field.value = command
                await pilot.press("enter")
                await pilot.pause()
            text = "\n".join(line.text for line in app.query_one("#chat", RichLog).lines)
            assert "Люблю брокколи" in text and "положительное целое" in text
            current = facts(service)[0]["id"]
            field.value = f"/memory delete {current}"
            await pilot.press("enter")
            assert not facts(service)
            with patch.object(service.memory, "command", side_effect=OSError("TEST_SECRET")):
                field.value = "/memory"
                await pilot.press("enter")
                await pilot.pause()
            text = "\n".join(line.text for line in app.query_one("#chat", RichLog).lines)
            assert "Не удалось открыть или изменить память" in text
            assert "TEST_SECRET" not in text and not field.disabled
        client.generate.assert_not_called()
        client.generate_structured.assert_not_called()


def test_memory_commands_in_headless_tui_are_local_and_errors_are_safe():
    asyncio.run(memory_ui_flow())


def test_source_must_be_current_user_quote_not_assistant_or_archive():
    bad = json.dumps({"operations": [operation(source_quote="Совет ассистента")]})
    assert error(lambda: parse_extraction(bad, "Да", [], NOW)) == MEMORY_ERROR


def test_end_to_end_go_memory_usage_history_and_new_session():
    with tempfile.TemporaryDirectory() as directory:
        store = Storage(directory)
        client = real_client(store)
        service = FitnessService(store, client, clock=lambda: NOW)
        service.save(validate_profile(180, 80, "мужской", 30, "Стать сильнее", ""))
        with patch("fitagent.provider.httpx.Client") as factory:
            transport = factory.return_value.__enter__.return_value
            transport.post.side_effect = [
                luna_reply(json.dumps({"operations": [operation()], "topic": DEFAULT_TOPIC})),
                httpx.Response(
                    200,
                    json={
                        "choices": [{"message": {"content": "Ответ"}, "finish_reason": "stop"}],
                        "usage": {"prompt_tokens": 100, "completion_tokens": 10},
                    },
                ),
            ]
            service.answer("Не люблю брокколи")
            assert transport.post.call_args_list[0].args == (MEMORY_URL,)
            chat = transport.post.call_args_list[1].kwargs["json"]
            assert chat["model"] == "flash"
            assert "Не люблю брокколи" in chat["messages"][1]["content"]
            assert "ЗАКРЕПЛЁННЫЕ ФАКТЫ" in chat["messages"][1]["content"]
            report = store.token_usage(include_history=True)
            assert report["requests"] == 2 and report["total_tokens"] == 340
            assert report["last_answer"] == {"input_tokens": 100, "output_tokens": 10}
            assert [point["total_tokens"] for point in report["history"]] == [230, 340]
            old_session = client.session_id
            service.clear()
            assert not facts(service) and store.token_usage()["requests"] == 0
            transport.post.side_effect = None
            transport.post.return_value = luna_reply()
            client.generate_structured([], EXTRACTION_SCHEMA)
            assert client.session_id != old_session
            assert (
                transport.post.call_args.kwargs["headers"]["x-opencode-session"]
                == client.session_id
            )


def test_clear_populated_facts_is_atomic_and_erases_every_memory_table():
    with tempfile.TemporaryDirectory() as directory:
        store, client, service = fixture(directory)
        extract(client, operation())
        service.answer("Не люблю брокколи")
        original = facts(service)[0]
        extract(
            client,
            operation(
                action="clarify",
                fact_id=original["id"],
                source_quote="Иногда ем",
                clarification="Нужно уточнение?",
            ),
        )
        service.answer("Иногда ем")
        with store.connect() as db:
            db.execute("""CREATE TRIGGER fail_reset BEFORE DELETE ON memory_turns
                          BEGIN SELECT RAISE(ABORT, 'TEST_SECRET'); END""")
        try:
            service.clear()
        except sqlite3.IntegrityError:
            pass
        else:
            raise AssertionError("Expected rollback")
        assert facts(service) and service.memory.repository.load()["issues"]
        with store.connect() as db:
            db.execute("DROP TRIGGER fail_reset")
        service.clear()
        for table in ("sticky_facts", "fact_sources", "memory_turns", "memory_issues", "messages"):
            with store.connect() as db:
                assert db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] == 0


def test_opening_old_database_only_adds_schema_and_does_not_extract_history():
    with tempfile.TemporaryDirectory() as directory:
        path = directory + "/fitagent.sqlite3"
        with sqlite3.connect(path) as db:
            db.executescript("""
                CREATE TABLE messages (
                    id INTEGER PRIMARY KEY,day TEXT NOT NULL,created_at TEXT NOT NULL,
                    role TEXT NOT NULL,content TEXT NOT NULL,status TEXT NOT NULL
                );
                INSERT INTO messages VALUES (1,'2026-01-01','2026-01-01T00:00:00+00:00',
                                             'user','OLD_PRIVATE_HISTORY','complete');
            """)
        store = Storage(directory)
        client = SimpleNamespace(generate=Mock(), generate_structured=Mock())
        service = FitnessService(store, client, clock=lambda: NOW)
        assert store.chat_messages()[0]["content"] == "OLD_PRIVATE_HISTORY"
        assert "пуста" in service.memory.command("/memory")
        client.generate.assert_not_called()
        client.generate_structured.assert_not_called()


def test_deleted_fact_number_is_not_reused_and_bad_source_cannot_be_processed():
    with tempfile.TemporaryDirectory() as directory:
        store, client, service = fixture(directory)
        extract(client, operation())
        service.answer("Не люблю брокколи")
        old_id = facts(service)[0]["id"]
        service.memory.command(f"/memory delete {old_id}")
        service.answer("Не люблю брокколи")
        assert facts(service)[0]["id"] > old_id
        error(lambda: service.memory.prepare(1, "Не люблю брокколи"), "обработан")


def test_corrupt_saved_memory_is_not_rendered_as_a_raw_python_error():
    with tempfile.TemporaryDirectory() as directory:
        store, client, service = fixture(directory)
        extract(client, operation())
        service.answer("Не люблю брокколи")
        with store.connect() as db:
            db.execute("UPDATE sticky_facts SET value='TEST_SECRET not JSON'")
        try:
            service.memory.command("/memory")
        except RuntimeError as exc:
            assert "TEST_SECRET" not in str(exc) and "JSON" not in str(exc)
        else:
            raise AssertionError("Corrupt memory must not be accepted")


def load_tests(loader, tests, pattern):
    return unittest.TestSuite(
        unittest.FunctionTestCase(value)
        for name, value in globals().items()
        if name.startswith("test_") and callable(value)
    )
