"""Memory commands render readable Russian text, never stored JSON objects."""

import asyncio
import json
import tempfile
import unittest

from test_memory import NOW, error, extract, facts, fixture, operation
from textual.widgets import Input, RichLog

from fitagent.memory_display import (
    ARCHIVE_ERROR,
    HISTORY_ERROR,
    display_archives,
    display_history,
    display_value,
)
from fitagent.ui import FitAgentApp


def test_value_text_preserves_numbers_units_and_qualifiers_without_json():
    assert (
        display_value(
            {
                "text": "Бюджет 4700 рублей в неделю",
                "quantity": 4700,
                "unit": "рублей в неделю",
                "qualifier": None,
            }
        )
        == "Бюджет 4700 рублей в неделю"
    )
    rendered = display_value(
        {
            "text": "Принимаю препарат А",
            "quantity": 2.5,
            "unit": "мг",
            "qualifier": "два раза в день",
        }
    )
    assert rendered == "Принимаю препарат А (2,5 мг; два раза в день)"
    assert (
        display_value(
            {
                "text": "Обычно три приёма пищи",
                "quantity": 3,
                "unit": "раза в день",
                "qualifier": "обычно",
            }
        )
        == "Обычно три приёма пищи (3 раза в день)"
    )


def test_history_formats_values_dates_sources_and_confirmations_without_changing_storage():
    with tempfile.TemporaryDirectory() as directory:
        store, client, service = fixture(directory)
        extract(client, operation())
        service.answer("Не люблю брокколи")
        original = facts(service)[0]
        extract(client, operation("Люблю брокколи", fact_id=original["id"]))
        service.answer("Люблю брокколи")
        service.answer("Люблю брокколи")
        before_calls = client.generate_structured.call_count, client.generate_safety.call_count
        raw_before = store.logs()
        history = service.memory.command(f"/memory history {original['id']}")
        assert f"Журнал изменений факта №{original['id']}" in history
        assert "14.11.2026 12:00:00 +0000" in history
        assert "факт добавлен" in history
        assert "Было: Не люблю брокколи" in history
        assert "Стало: Люблю брокколи" in history
        assert "актуальность подтверждена" in history
        assert "Значение: Люблю брокколи" in history
        assert "Источник: сообщение №1" in history
        assert not any(
            key in history for key in ('"text"', '"quantity"', '"unit"', '"qualifier"', "null", "{")
        )
        assert store.logs() == raw_before
        assert (
            client.generate_structured.call_count,
            client.generate_safety.call_count,
        ) == before_calls
        with store.connect() as db:
            latest = db.execute(
                "SELECT new_value FROM memory_journal WHERE lineage_id=? ORDER BY id DESC LIMIT 1",
                (original["lineage_id"],),
            ).fetchone()[0]
            assert json.loads(latest)["text"] == "Люблю брокколи"


def test_revoked_history_explains_old_value_and_revocation_without_null():
    with tempfile.TemporaryDirectory() as directory:
        store, client, service = fixture(directory)
        extract(client, operation())
        service.answer("Не люблю брокколи")
        fact_id = facts(service)[0]["id"]
        extract(client, operation("Больше не учитывай этот вкус", action="revoke", fact_id=fact_id))
        service.answer("Больше не учитывай этот вкус")
        history = service.memory.command(f"/memory history {fact_id}")
        assert "Было: Не люблю брокколи" in history
        assert "Стало: факт отозван" in history
        assert "null" not in history and "{" not in history


def test_memory_and_archive_views_use_readable_values_instead_of_serialized_objects():
    with tempfile.TemporaryDirectory() as directory:
        store, client, service = fixture(directory)
        item = operation(
            "Недельный бюджет",
            category="cooking_budget",
            key="budget",
            value={
                "text": "Недельный бюджет",
                "quantity": 4700,
                "unit": "рублей",
                "qualifier": "примерно",
            },
        )
        extract(client, item)
        service.answer(item["source_quote"])
        current = facts(service)[0]
        with store.connect() as db:
            db.execute(
                "INSERT INTO memory_archives(goal_id,created_at,facts) VALUES (?,?,?)",
                (
                    1,
                    NOW.isoformat(),
                    json.dumps({"goal": {"text": "Недельное меню"}, "facts": [current]}),
                ),
            )
        memory = service.memory.command("/memory")
        archive = service.memory.command("/memory archive")
        assert "Недельный бюджет (4700 рублей; примерно)" in memory
        assert "Недельный бюджет (4700 рублей; примерно)" in archive
        assert "Цель: Недельное меню" in archive
        assert all(token not in memory + archive for token in ('"text"', '"quantity"', "null", "{"))


def test_bad_journal_records_have_safe_russian_errors_without_exposing_the_payload():
    record = {
        "recorded_at": NOW.isoformat(),
        "effective_at": NOW.isoformat(),
        "action": "update",
        "old_value": None,
        "new_value": "PRIVATE_SECRET malformed JSON",
        "source_message_id": None,
    }
    for raw in ("PRIVATE_SECRET malformed JSON", '{"text":"PRIVATE_SECRET"}', "[]"):
        record["new_value"] = raw
        message = error(lambda: display_history(7, [record]))
        assert message == HISTORY_ERROR
        assert "PRIVATE_SECRET" not in message and "Traceback" not in message
    record["new_value"] = json.dumps(operation()["value"])
    record["recorded_at"] = "PRIVATE_SECRET invalid date"
    assert error(lambda: display_history(7, [record])) == HISTORY_ERROR


def test_empty_history_is_readable():
    assert display_history(7, []) == "Журнал изменений факта №7\n\nЗаписанных изменений пока нет."


def test_corrupt_archive_is_not_dumped_to_the_user():
    record = {"id": 1, "created_at": NOW.isoformat(), "facts": "PRIVATE_SECRET broken JSON"}
    assert error(lambda: display_archives([record])) == ARCHIVE_ERROR


async def human_history_flow():
    with tempfile.TemporaryDirectory() as directory:
        store, client, service = fixture(directory)
        extract(client, operation())
        service.answer("Не люблю брокколи")
        fact_id = facts(service)[0]["id"]
        extract(client, operation("Люблю брокколи", fact_id=fact_id))
        service.answer("Люблю брокколи")
        client.generate.reset_mock()
        client.generate_structured.reset_mock()
        client.generate_safety.reset_mock()
        app = FitAgentApp(service)
        async with app.run_test(size=(100, 40)) as pilot:
            await pilot.pause()
            chat = app.query_one("#chat", RichLog)
            for _ in range(80):
                chat.write("Предыдущее сообщение")
            await pilot.pause()
            chat.scroll_home(animate=False, force=True)
            await pilot.pause()
            assert not chat.is_vertical_scroll_end
            app.query_one("#question", Input).value = f"/memory history {fact_id}"
            await pilot.press("enter")
            await pilot.pause()
            assert chat.is_vertical_scroll_end
            text = "\n".join(line.text for line in chat.lines)
            assert f"Журнал изменений факта №{fact_id}" in text
            assert "Было: Не люблю брокколи" in text and "Стало: Люблю брокколи" in text
            assert not any(key in text for key in ('"text"', '"quantity"', '"qualifier"'))
            assert "Проверка завершена" not in text
            client.generate.assert_not_called()
            client.generate_structured.assert_not_called()
            client.generate_safety.assert_not_called()


def test_history_command_renders_readable_text_without_json_or_a_test_completion_banner():
    asyncio.run(human_history_flow())


def load_tests(loader, tests, pattern):
    return unittest.TestSuite(
        unittest.FunctionTestCase(value)
        for name, value in globals().items()
        if name.startswith("test_") and callable(value)
    )
