"""Base activity messages stay safe, without changing the audit or billing."""

import asyncio
import contextlib
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from test_memory import NOW, fixture, operation
from test_topics import active, checkpoint_reply, route
from textual.widgets import RichLog

from fitagent.activity import format_activity
from fitagent.cli import main
from fitagent.logs import LogsPanel
from fitagent.storage import Storage
from fitagent.ui import FitAgentApp


def activities(store):
    for kind, details in (
        ("checkpoint_used", {"topic_id": 1, "first_message_id": 1, "covered_message_id": 8}),
        ("topic_routed", {"previous_topic_id": 1, "topic_id": 2, "action": "resume"}),
        ("facts_read", {"fact_ids": [1, 3]}),
        ("history_compacted", {"before_bytes": 60_000, "after_bytes": 12_000}),
    ):
        store.event(kind, dict(details, value="PRIVATE_SECRET", title="PRIVATE_SECRET"), NOW)


def test_public_log_keeps_base_russian_labels_and_raw_audit():
    with tempfile.TemporaryDirectory() as directory:
        store = Storage(directory)
        activities(store)
        for kind in (
            "api_request",
            "api_usage",
            "api_error",
            "context_budget",
            "profile_check",
            "profile_saved",
            "summary_started",
            "summary_finished",
            "summary_cache_hit",
            "checkpoint_started",
            "checkpoint_finished",
            "memory_finished",
            "ui_error",
        ):
            store.event(kind, {"private": "PRIVATE_SECRET"}, NOW)
        public = store.activity_logs()
        assert len(public.splitlines()) == 4
        for label in (
            "Checkpoint подключён",
            "Тема изменена",
            "Факты прочитаны в контекст",
            "История сжата",
        ):
            assert label in public
        assert "PRIVATE_SECRET" not in public and "api_" not in public and "summary_" not in public
        assert "60 000 → 12 000 байт UTF-8" in public
        assert "№1 → №2 · возврат" in public and "№1, №3 · всего: 2" in public
        assert "PRIVATE_SECRET" in store.logs()  # The public view doesn't rewrite old records.


def test_limit_is_applied_after_noise_and_unchanged_topics_are_filtered_out():
    with tempfile.TemporaryDirectory() as directory:
        store = Storage(directory)
        activities(store)
        for _ in range(250):
            store.event("profile_check", {"due": False}, NOW)
            store.event(
                "topic_routed", {"previous_topic_id": 2, "topic_id": 2, "action": "continue"}, NOW
            )
        assert len(store.activity_logs().splitlines()) == 4
        lines = store.activity_logs(limit=2).splitlines()
        assert len(lines) == 2 and "Факты прочитаны" in lines[0] and "История сжата" in lines[1]
        store.event(
            "topic_routed", {"previous_topic_id": None, "topic_id": 1, "action": "switch"}, NOW
        )
        store.event("topic_routed", {"previous_topic_id": 2, "topic_id": 2, "action": "mixed"}, NOW)
        assert len(store.activity_logs().splitlines()) == 4


def test_bad_public_metadata_is_skipped_without_exposing_raw_values():
    cases = [
        ("facts_read", {"fact_ids": ["PRIVATE_SECRET"]}),
        ("facts_read", {"fact_ids": [True]}),
        ("facts_read", {"fact_ids": [1, 1]}),
        ("facts_read", {"fact_ids": []}),
        (
            "checkpoint_used",
            {"topic_id": "PRIVATE_SECRET", "first_message_id": 1, "covered_message_id": 2},
        ),
        ("checkpoint_used", {"topic_id": 1, "first_message_id": 8, "covered_message_id": 2}),
        ("topic_routed", {"previous_topic_id": 1, "topic_id": 2, "action": ["PRIVATE_SECRET"]}),
        ("history_compacted", {"before_bytes": True, "after_bytes": 0}),
        ("history_compacted", {"before_bytes": 10, "after_bytes": 20}),
    ]
    for kind, details in cases:
        assert (
            format_activity(
                {"created_at": NOW.isoformat(), "kind": kind, "details": json.dumps(details)}
            )
            is None
        )
    for details in ("PRIVATE_SECRET invalid JSON", "[]", "null"):
        assert (
            format_activity(
                {"created_at": NOW.isoformat(), "kind": "facts_read", "details": details}
            )
            is None
        )
    assert (
        format_activity(
            {"created_at": "PRIVATE_SECRET", "kind": "facts_read", "details": '{"fact_ids":[1]}'}
        )
        is None
    )


def test_context_reads_are_logged_once_and_checkpoint_creation_is_not_usage():
    with tempfile.TemporaryDirectory() as directory:
        store, client, service = fixture(directory)
        route(client, title="Меню", operations=[operation()])
        service.answer("Не люблю брокколи")
        menu = active(service)
        service.memory.command("/memory")
        service.memory.context(topic_id=menu)
        assert store.activity_logs().count("Факты прочитаны в контекст") == 1
        route(client, action="switch", title="Тренировки")
        checkpoint_reply(client, "Сводка меню")
        service.answer("Тренировки")
        assert "Checkpoint подключён" not in store.activity_logs()
        assert service.topics.repository.checkpoint(menu) is not None
        route(client, action="resume", topic_id=menu)
        service.answer("Вернёмся к меню")
        public = store.activity_logs()
        assert public.count("Checkpoint подключён") == 1
        assert public.count("Факты прочитаны в контекст") == 3
        assert public.count("Тема изменена") == 2
        assert "Сводка меню" not in public and "Не люблю брокколи" not in public


def test_mixed_context_logs_each_included_checkpoint_but_not_same_topic_change():
    with tempfile.TemporaryDirectory() as directory:
        store, client, service = fixture(directory)
        route(client, title="Меню")
        service.answer("Меню")
        menu = active(service)
        route(client, action="switch", title="Тренировки")
        service.answer("Тренировки")
        training = active(service)
        route(client, action="resume", topic_id=menu)
        service.answer("Меню")
        with store.connect() as db:
            before = db.execute("SELECT COALESCE(MAX(id),0) FROM events").fetchone()[0]
        route(client, action="mixed", topic_id=menu, related=[training])
        service.answer("Совместим меню и тренировки")
        with store.connect() as db:
            rows = db.execute("SELECT * FROM events WHERE id>? ORDER BY id", (before,)).fetchall()
        visible = [line for row in rows if (line := format_activity(row)) is not None]
        assert len(visible) == 2
        assert all("Checkpoint подключён" in line for line in visible)
        assert sum("связанная тема" in line for line in visible) == 1


def test_history_compaction_is_one_event_per_application_including_cache_hits():
    with tempfile.TemporaryDirectory() as directory:
        store, client, service = fixture(directory)
        source = ["Л" * 30_000]
        compacted = service.compact(source, service.system())
        calls = client.generate.call_count
        assert service.compact(source, service.system()) == compacted
        assert client.generate.call_count == calls
        public = store.activity_logs()
        assert public.count("История сжата") == 2
        assert "summary_cache_hit" not in public and "summary_started" not in public
        with store.connect() as db:
            data = [
                json.loads(row[0])
                for row in db.execute("SELECT details FROM events WHERE kind='history_compacted'")
            ]
        assert all(
            item["before_bytes"] == 60_000 and item["after_bytes"] == len(compacted.encode())
            for item in data
        )


def test_public_view_does_not_change_token_totals_restart_or_clear():
    with tempfile.TemporaryDirectory() as directory:
        store = Storage(directory)
        store.event("api_request", {"purpose": "answer"}, NOW)
        store.event(
            "api_usage", {"purpose": "answer", "input_tokens": 100, "output_tokens": 20}, NOW
        )
        activities(store)
        before = store.token_usage(include_history=True)
        assert "api_" not in store.activity_logs()
        reopened = Storage(directory)
        assert reopened.activity_logs() == store.activity_logs()
        assert reopened.token_usage(include_history=True) == before
        reopened.clear()
        assert reopened.activity_logs() == "" and reopened.token_usage()["total_tokens"] == 0


def test_fitagent_logs_command_matches_public_view_and_errors_are_russian():
    with tempfile.TemporaryDirectory() as directory:
        store = Storage(directory)
        activities(store)
        env = dict(os.environ, FITAGENT_DATA_DIR=directory, OPENCODE_API_KEY="TEST_ONLY")
        result = subprocess.run(
            [str(Path(sys.executable).with_name("fitagent")), "logs"],
            env=env,
            capture_output=True,
            text=True,
            check=False,
        )
        assert result.returncode == 0 and result.stdout.strip() == store.activity_logs()
        assert "PRIVATE_SECRET" not in result.stdout and not result.stderr
        with (
            patch("fitagent.cli.Storage", return_value=store),
            patch("fitagent.cli.load_dotenv"),
            patch("sys.argv", ["fitagent", "logs"]),
        ):
            with patch.object(store, "activity_logs", side_effect=OSError("PRIVATE_SECRET")):
                output = io.StringIO()
                with contextlib.redirect_stderr(output):
                    try:
                        main()
                    except SystemExit as exc:
                        assert exc.code == 1
                    else:
                        raise AssertionError("Expected a safe CLI exit")
                assert "Не удалось загрузить журнал" in output.getvalue()
                assert "PRIVATE_SECRET" not in output.getvalue()


async def safe_panel_flow():
    with tempfile.TemporaryDirectory() as directory:
        store, client, service = fixture(directory)
        app = FitAgentApp(service)
        async with app.run_test(size=(80, 24)) as pilot:
            await pilot.pause()
            with patch.object(store, "activity_logs", side_effect=OSError("PRIVATE_SECRET")):
                app.action_logs()
                panel = app.query_one(LogsPanel)
                error_text = str(panel.query_one("#token-counts").renderable)
                assert "Не удалось загрузить журнал" in error_text
                assert "PRIVATE_SECRET" not in error_text
            activities(store)
            panel.refresh_logs()
            await pilot.pause()
            text = "\n".join(line.text for line in panel.query_one("#events", RichLog).lines)
            assert "Checkpoint подключён" in text and "PRIVATE_SECRET" not in text


def test_public_log_panel_error_is_safe_and_recoverable():
    asyncio.run(safe_panel_flow())


def load_tests(loader, tests, pattern):
    return unittest.TestSuite(
        unittest.FunctionTestCase(value)
        for name, value in globals().items()
        if name.startswith("test_") and callable(value)
    )
