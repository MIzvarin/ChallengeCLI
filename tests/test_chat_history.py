"""Persistent chat display and the independent weekly LLM window; no real API calls."""

import asyncio
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import Mock, patch

from textual.widgets import Input, RichLog

from fitagent.domain import validate_profile
from fitagent.service import FitnessService
from fitagent.storage import Storage
from fitagent.ui import FitAgentApp

NOW = datetime(2026, 11, 10, 12, tzinfo=timezone.utc)


def fixture(directory):
    store = Storage(directory)
    client = SimpleNamespace(model="test-model", generate=Mock(return_value="Свежий ответ"))
    service = FitnessService(store, client, clock=lambda: NOW)
    return store, client, service


def chat_text(app):
    log = app.screen_stack[0].query_one("#chat", RichLog)
    return "\n".join(line.text for line in log.lines)


def test_chat_messages_include_all_dates_and_statuses():
    with tempfile.TemporaryDirectory() as directory:
        store, client, service = fixture(directory)
        service.save(validate_profile(180, 80, "мужской", 30, "Стать сильнее", ""))
        for role, content, stamp, status in (
            ("user", "Старый вопрос", NOW - timedelta(days=30), "complete"),
            ("assistant", "Старый ответ", NOW - timedelta(days=30), "complete"),
            ("user", "Неудачный вопрос", NOW, "failed"),
            ("user", "Прерванный вопрос", NOW, "pending"),
        ):
            store.message(role, content, stamp, status)
        messages = Storage(directory).chat_messages()
        assert [message["content"] for message in messages] == [
            "Старый вопрос",
            "Старый ответ",
            "Неудачный вопрос",
            "Прерванный вопрос",
        ]
        assert [message["status"] for message in messages] == [
            "complete",
            "complete",
            "failed",
            "pending",
        ]
        assert messages[0]["created_at"] == (NOW - timedelta(days=30)).isoformat()
        assert not client.generate.called
        service.clear()
        assert Storage(directory).chat_messages() == []


async def restart_flow():
    with tempfile.TemporaryDirectory() as directory:
        store, client, service = fixture(directory)
        service.save(validate_profile(180, 80, "мужской", 30, "Стать сильнее", ""))
        store.message("user", "Старый вопрос", NOW - timedelta(days=8))
        store.message("assistant", "**Старый ответ**", NOW - timedelta(days=8))
        store.message("user", "Неудачный вопрос", NOW, "failed")
        store.message("user", "Прерванный вопрос", NOW, "pending")
        app = FitAgentApp(service)
        async with app.run_test() as pilot:
            await pilot.pause()
            text = chat_text(app)
            assert "Старый вопрос" in text and "Старый ответ" in text
            assert "**Старый ответ**" not in text  # Markdown is rendered, not printed verbatim.
            assert "2026-11-02" in text
            assert "Ответ не получен" in text and "Запрос не завершён" in text
            assert not client.generate.called
            app.query_one("#question", Input).value = "Свежий вопрос"
            await pilot.press("enter")
            await app.workers.wait_for_complete()

        reopened, new_client, new_service = fixture(directory)
        app = FitAgentApp(new_service)
        async with app.run_test() as pilot:
            await pilot.pause()
            text = chat_text(app)
            expected = [
                "Старый вопрос",
                "Старый ответ",
                "Неудачный вопрос",
                "Прерванный вопрос",
                "Свежий вопрос",
                "Свежий ответ",
            ]
            positions = [text.index(content) for content in expected]
            assert positions == sorted(positions)
            assert all(text.count(content) == 1 for content in expected)
            assert len(reopened.chat_messages()) == 6
            assert not new_client.generate.called
            app.query_one("#question", Input).value = "Продолжение"
            await pilot.press("enter")
            await app.workers.wait_for_complete()
            sent = new_client.generate.call_args.args[0][1]["content"]
            assert "Свежий вопрос" in sent and "Свежий ответ" in sent
            assert "Старый вопрос" not in sent and "Старый ответ" not in sent
            assert "Неудачный вопрос" not in sent and "Прерванный вопрос" not in sent
            assert new_client.generate.call_count == 1


def test_chat_restored_after_restart_but_llm_context_stays_weekly():
    asyncio.run(restart_flow())


async def long_history_flow():
    with tempfile.TemporaryDirectory() as directory:
        store, client, service = fixture(directory)
        service.save(validate_profile(180, 80, "мужской", 30, "Стать сильнее", ""))
        store.message("user", "НАЧАЛО\n" + "Строка\n" * 3100 + "КОНЕЦ", NOW)
        app = FitAgentApp(service)
        async with app.run_test() as pilot:
            await pilot.pause()
            log = app.query_one("#chat", RichLog)
            assert log.max_lines is None
            assert len(log.lines) > 3000
            assert "НАЧАЛО" in chat_text(app) and "КОНЕЦ" in chat_text(app)
            assert not client.generate.called
        service.clear()
        app = FitAgentApp(service)
        async with app.run_test() as pilot:
            await pilot.pause()
            assert "НАЧАЛО" not in chat_text(app)
            assert "Начните с вопроса" in chat_text(app)


def test_entire_history_remains_scrollable_and_clear_persists():
    asyncio.run(long_history_flow())


async def read_error_flow():
    with tempfile.TemporaryDirectory() as directory:
        store, client, service = fixture(directory)
        app = FitAgentApp(service)
        with patch.object(store, "chat_messages", side_effect=OSError("SECRET technical failure")):
            async with app.run_test() as pilot:
                await pilot.pause()
                text = chat_text(app)
                assert "Не удалось загрузить переписку" in text
                assert "Перезапустите приложение" in text
                assert "SECRET" not in text and "technical failure" not in text
                assert not client.generate.called


def test_history_read_error_is_understandable():
    asyncio.run(read_error_flow())


def load_tests(loader, tests, pattern):
    return unittest.TestSuite(
        unittest.FunctionTestCase(value)
        for name, value in globals().items()
        if name.startswith("test_") and callable(value)
    )
