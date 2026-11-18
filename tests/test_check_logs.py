"""Warnings and check failures are visible in public logs; successful checks stay hidden."""

import asyncio
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from support import safe_response
from test_memory import NOW, error, fixture
from textual.widgets import RichLog

from fitagent.activity import SAFETY_STAGES, format_activity
from fitagent.logs import LogsPanel
from fitagent.storage import Storage
from fitagent.ui import FitAgentApp


def check_events(store):
    for stage in SAFETY_STAGES:
        for status in ("allow", "warning", "block"):
            store.event(
                "safety_checked",
                {"stage": stage, "status": status, "description": "PRIVATE_SECRET"},
                NOW,
            )
        store.event("safety_failed", {"stage": stage, "type": "PRIVATE_SECRET"}, NOW)
    store.event("memory_failed", {"type": "PRIVATE_SECRET"}, NOW)


def test_only_warning_block_and_failures_are_displayed_with_safe_russian_stages():
    with tempfile.TemporaryDirectory() as directory:
        store = Storage(directory)
        check_events(store)
        public = store.activity_logs()
        assert len(public.splitlines()) == len(SAFETY_STAGES) * 3 + 1
        assert public.count("· предупреждение") == len(SAFETY_STAGES)
        assert public.count("· заблокировано") == len(SAFETY_STAGES)
        assert public.count("Ошибка проверки безопасности") == len(SAFETY_STAGES)
        assert public.count("Ошибка проверки памяти") == 1
        assert all(stage in public for stage in SAFETY_STAGES.values())
        assert "факты не изменены; повторите запрос" in public
        assert "запрос остановлен; повторите попытку" in public
        for hidden in (
            "PRIVATE_SECRET",
            "allow",
            "warning",
            "block",
            "ValueError",
            "goal_response",
        ):
            assert hidden not in public
        assert "PRIVATE_SECRET" in store.logs()


def test_bad_check_metadata_is_skipped_without_leaking_raw_values():
    cases = [
        ("safety_checked", {}),
        ("safety_checked", {"stage": "request", "status": "allow"}),
        ("safety_checked", {"stage": "PRIVATE_SECRET", "status": "block"}),
        ("safety_checked", {"stage": ["PRIVATE_SECRET"], "status": "warning"}),
        ("safety_checked", {"stage": "request", "status": ["PRIVATE_SECRET"]}),
        ("safety_checked", {"stage": "request", "status": "PRIVATE_SECRET"}),
        ("safety_failed", {"stage": "PRIVATE_SECRET", "type": "ValueError"}),
        ("safety_failed", {"stage": "request"}),
        ("safety_failed", {"stage": "response", "type": []}),
        ("memory_failed", {}),
        ("memory_failed", {"type": None}),
        ("memory_failed", {"type": ""}),
    ]
    for kind, details in cases:
        assert (
            format_activity(
                {"created_at": NOW.isoformat(), "kind": kind, "details": json.dumps(details)}
            )
            is None
        )
    for details in ("PRIVATE_SECRET", "null", "[]"):
        assert (
            format_activity(
                {"created_at": NOW.isoformat(), "kind": "memory_failed", "details": details}
            )
            is None
        )


def test_hidden_successes_do_not_displace_warnings_or_change_usage_on_restart():
    with tempfile.TemporaryDirectory() as directory:
        store = Storage(directory)
        check_events(store)
        store.event("api_request", {"purpose": "safety_request"}, NOW)
        store.event(
            "api_usage",
            {"purpose": "safety_request", "input_tokens": 100, "output_tokens": 10},
            NOW,
        )
        before = store.token_usage(include_history=True)
        for _ in range(250):
            store.event("safety_checked", {"stage": "request", "status": "allow"}, NOW)
        public = store.activity_logs(limit=2)
        assert len(public.splitlines()) == 2
        assert "Ошибка проверки безопасности" in public and "Ошибка проверки памяти" in public
        reopened = Storage(directory)
        assert reopened.activity_logs(limit=2) == public
        assert reopened.token_usage(include_history=True) == before
        reopened.clear()
        assert reopened.activity_logs() == ""


def test_actual_block_warning_protocol_and_memory_failures_reach_public_logs():
    with tempfile.TemporaryDirectory() as directory:
        store, client, service = fixture(directory)

        def block(messages, stage):
            value = json.loads(safe_response(messages, stage))
            value["status"] = "block"
            value["checks"][0]["status"] = "block"
            return json.dumps(value)

        client.generate_safety.side_effect = block
        error(lambda: service.answer("PRIVATE_BLOCKED_QUESTION"), "Не могу выполнить эту просьбу")
        assert "Проверка безопасности  запрос · заблокировано" in store.activity_logs()
        client.generate.assert_not_called()

        def warning(messages, stage):
            value = json.loads(safe_response(messages, stage))
            value["status"] = "warning"
            value["checks"][0].update(status="warning", strictness="advisory")
            return json.dumps(value)

        client.generate_safety.side_effect = warning
        assert "Предупреждение безопасности" in service.answer("PRIVATE_SAFE_QUESTION")
        public = store.activity_logs()
        assert "Проверка безопасности  запрос · предупреждение" in public
        assert "Проверка безопасности  ответ · предупреждение" in public
        client.generate_safety.side_effect = lambda messages, stage: "PRIVATE_INVALID_JSON"
        error(lambda: service.answer("PRIVATE_PROTOCOL_QUESTION"), "Это сбой проверки")
        assert "Ошибка проверки безопасности  запрос" in store.activity_logs()
        client.generate_structured.return_value = "PRIVATE_INVALID_JSON"
        error(
            lambda: service.answer("PRIVATE_MEMORY_QUESTION"),
            "Не удалось проверить обновление памяти",
        )
        assert "Ошибка проверки памяти" in store.activity_logs()
        assert "PRIVATE" not in store.activity_logs() + store.logs()


def test_cli_shows_saved_check_warnings_and_failures_without_api_requests():
    with tempfile.TemporaryDirectory() as directory:
        store = Storage(directory)
        check_events(store)
        before = store.token_usage(include_history=True)
        result = subprocess.run(
            [str(Path(sys.executable).with_name("fitagent")), "logs"],
            env=dict(os.environ, FITAGENT_DATA_DIR=directory, OPENCODE_API_KEY="TEST_ONLY"),
            capture_output=True,
            text=True,
            check=False,
        )
        assert result.returncode == 0 and not result.stderr
        assert result.stdout.strip() == store.activity_logs()
        assert "PRIVATE" not in result.stdout
        assert store.token_usage(include_history=True) == before


async def visible_checks_flow():
    with tempfile.TemporaryDirectory() as directory:
        store, client, service = fixture(directory)
        app = FitAgentApp(service)
        async with app.run_test(size=(100, 40)) as pilot:
            await pilot.pause()
            app.action_logs()
            panel = app.query_one(LogsPanel)
            assert panel.display
            check_events(store)
            panel.refresh_logs()
            await pilot.pause()
            log = panel.query_one("#events", RichLog)
            text = "\n".join(line.text for line in log.lines)
            assert "Проверка безопасности" in text
            assert "Ошибка проверки безопасности" in text
            assert "Ошибка проверки памяти" in text
            assert "PRIVATE_SECRET" not in text
            client.generate.assert_not_called()
            client.generate_safety.assert_not_called()


def test_inline_log_panel_refreshes_check_warnings_and_failures():
    asyncio.run(visible_checks_flow())


def load_tests(loader, tests, pattern):
    return unittest.TestSuite(
        unittest.FunctionTestCase(value)
        for name, value in globals().items()
        if name.startswith("test_") and callable(value)
    )
