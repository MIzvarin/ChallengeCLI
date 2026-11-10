"""Full reset is tested only against temporary, disposable databases."""

import asyncio
import sqlite3
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import httpx
from textual.widgets import Input, RichLog

from fitagent.domain import validate_profile
from fitagent.provider import ModelClient
from fitagent.service import FitnessService
from fitagent.storage import Storage
from fitagent.ui import FitAgentApp, ProfileScreen

NOW = datetime(2026, 11, 10, tzinfo=timezone.utc)
TABLES = ("profiles", "goals", "messages", "changes", "summaries", "events")


def fixture(directory):
    store = Storage(directory)
    client = SimpleNamespace(
        session_id=store.session_id(),
        model="test-model",
        generate=Mock(return_value='{"warning":""}'),
    )
    service = FitnessService(store, client, clock=lambda: NOW)
    profile = validate_profile(180, 80, "мужской", 30, "OLD_GOAL", "")
    service.save(profile)
    store.message("user", "OLD_MESSAGE", NOW)
    store.message("assistant", "OLD_ANSWER", NOW)
    store.cache_put("OLD_KEY", "OLD_SUMMARY", NOW)
    store.event("OLD_EVENT", {}, NOW)
    return store, client, service, profile


def assert_empty(store):
    with store.connect() as db:
        for table in TABLES:
            assert db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] == 0, table
        assert db.execute("SELECT COUNT(*) FROM settings").fetchone()[0] == 1


def test_clear_all_records_and_session():
    with tempfile.TemporaryDirectory() as directory:
        store, client, service, profile = fixture(directory)
        env = Path(directory) / ".env"
        env.write_text("OPENCODE_API_KEY=KEEP_THIS_TEST_VALUE\n", encoding="utf-8")
        old_session = client.session_id
        service.clear()
        assert_empty(store)
        assert client.session_id != old_session
        assert client.session_id == store.session_id()
        assert Storage(directory).session_id() == client.session_id
        assert store.path.is_file()
        assert env.read_text(encoding="utf-8") == "OPENCODE_API_KEY=KEEP_THIS_TEST_VALUE\n"
        assert store.history(NOW.isoformat()) == []
        assert store.weights() == []
        assert store.cache_get("OLD_KEY") is None
        assert not store.logs()
        assert "OLD_GOAL" not in service.system()
        profile["goal"] = "NEW_GOAL"
        service.save(profile)
        service.answer("NEW_QUESTION")
        messages = client.generate.call_args.args[0]
        assert "NEW_GOAL" in messages[0]["content"]
        assert "OLD_" not in str(messages)


def test_clear_rollback_on_storage_error():
    with tempfile.TemporaryDirectory() as directory:
        store, client, service, profile = fixture(directory)
        old_session = client.session_id
        with store.connect() as db:
            db.execute("""CREATE TRIGGER fail_clear BEFORE DELETE ON events
                          BEGIN SELECT RAISE(ABORT, 'test failure'); END""")
        try:
            service.clear()
        except sqlite3.IntegrityError:
            pass
        else:
            raise AssertionError("Expected a simulated storage failure")
        with store.connect() as db:
            for table in TABLES:
                assert db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] > 0, table
        assert client.session_id == old_session == store.session_id()


def test_new_session_is_used_by_transport():
    with tempfile.TemporaryDirectory() as directory:
        store = Storage(directory)
        client = ModelClient(
            "test-key",
            "https://example.test/chat",
            "test-model",
            store.session_id(),
            lambda *args: None,
        )
        service = FitnessService(store, client)
        old_session = client.session_id
        service.clear()
        with patch("fitagent.provider.httpx.Client") as factory:
            transport = factory.return_value.__enter__.return_value
            transport.post.return_value = httpx.Response(
                200, json={"choices": [{"message": {"content": "ok"}, "finish_reason": "stop"}]}
            )
            assert client.generate([], None, "answer") == "ok"
            sent_id = transport.post.call_args.kwargs["headers"]["x-opencode-session"]
            assert sent_id == store.session_id() != old_session
            assert client.key == "test-key"


async def tui_clear_flow():
    with tempfile.TemporaryDirectory() as directory:
        store, client, service, profile = fixture(directory)
        app = FitAgentApp(service)
        async with app.run_test(size=(100, 35)) as pilot:
            await pilot.pause()
            root = app.screen
            log = root.query_one("#chat", RichLog)
            log.write("OLD_VISIBLE_MESSAGE")
            field = root.query_one("#question", Input)
            for _ in range(2):
                old_session = client.session_id
                field.value = "/clear"
                await pilot.press("enter")
                await pilot.pause()
                assert isinstance(app.screen, ProfileScreen)
                assert len(app.screen_stack) == 2  # Initial profile, not a confirmation dialog.
                assert client.session_id != old_session
                assert_empty(store)
                assert not client.generate.called
                for name in ("height", "weight", "sex", "age", "goal", "target"):
                    assert app.screen.query_one(f"#{name}", Input).value == ""
                assert not field.value
                assert "OLD_VISIBLE_MESSAGE" not in "\n".join(line.text for line in log.lines)
                # Populate a new profile without touching the API, then exercise /clear again.
                service.save(profile)
                app.screen.dismiss(True)
                await pilot.pause()


def test_tui_clear_command():
    asyncio.run(tui_clear_flow())


async def tui_clear_error_flow():
    with tempfile.TemporaryDirectory() as directory:
        store, client, service, profile = fixture(directory)
        app = FitAgentApp(service)
        async with app.run_test(size=(100, 35)) as pilot:
            await pilot.pause()
            old_session = client.session_id
            with patch.object(store, "clear", side_effect=OSError("SECRET technical failure")):
                app.query_one("#question", Input).value = "/clear"
                await pilot.press("enter")
                await pilot.pause()
            assert len(app.screen_stack) == 1
            text = "\n".join(line.text for line in app.query_one("#chat", RichLog).lines)
            assert "Не удалось очистить данные" in text
            assert "SECRET" not in text
            assert store.latest("profiles")
            assert client.session_id == old_session


def test_tui_clear_error():
    asyncio.run(tui_clear_error_flow())


def load_tests(loader, tests, pattern):
    return unittest.TestSuite(
        unittest.FunctionTestCase(value)
        for name, value in globals().items()
        if name.startswith("test_") and callable(value)
    )
