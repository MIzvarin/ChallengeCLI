"""MCP activity reaches CLI and TUI without persisting requests or personal data."""

import asyncio
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from test_fitagent import NOW, fixture, profile
from test_nutrition import DIET, NONE, ROUTE, attach, raw_product
from textual.widgets import RichLog

from fitagent.activity import format_activity
from fitagent.logs import LogsPanel
from fitagent.ui import FitAgentApp


def mcp_events(store):
    with store.connect() as db:
        return [
            (row["kind"], json.loads(row["details"]))
            for row in db.execute(
                "SELECT kind,details FROM events WHERE kind IN "
                "('mcp_started','mcp_finished','mcp_failed','nutrition_cart_skipped') ORDER BY id"
            )
        ]


def test_mcp_success_records_only_operations_and_counts_and_is_visible_in_cli():
    with tempfile.TemporaryDirectory() as directory:
        store, client, service = fixture(directory)
        service.save(profile())
        catalog = attach(service, client)
        catalog.search.return_value = [raw_product(name="PRIVATE_PRODUCT")]
        service.chat("Подбери рацион на день, буду готовить")
        events = mcp_events(store)
        assert events == [
            ("mcp_started", {"operation": "search"}),
            ("mcp_finished", {"operation": "search", "count": 1}),
            ("mcp_started", {"operation": "cart_link"}),
            ("mcp_finished", {"operation": "cart_link", "count": 1}),
        ]
        assert "PRIVATE" not in json.dumps(events)
        public = store.activity_logs()
        assert "MCP ВкусВилла  поиск товаров · начато" in public
        assert "найдено товаров: 1" in public and "позиций в корзине: 1" in public
        assert "PRIVATE" not in public and "https://" not in public
        result = subprocess.run(
            [str(Path(sys.executable).with_name("fitagent")), "logs"],
            env=dict(os.environ, FITAGENT_DATA_DIR=directory, OPENCODE_API_KEY=""),
            capture_output=True,
            text=True,
            check=False,
        )
        assert result.returncode == 0 and not result.stderr
        assert result.stdout.strip() == public


def test_mcp_failures_are_logged_without_exception_text_and_link_failure_preserves_prices():
    for operation, reason in (("search", "mcp_error"), ("cart_link", "link_error")):
        with tempfile.TemporaryDirectory() as directory:
            store, client, service = fixture(directory)
            service.save(profile())
            catalog = attach(service, client)
            getattr(catalog, operation).side_effect = ValueError("PRIVATE_EXCEPTION")
            response = service.chat("Подбери рацион на день, буду готовить")
            if operation == "search":
                assert response == DIET
            else:
                assert DIET in response and "Итого за закупку: 148,00 ₽" in response
            events = mcp_events(store)
            assert ("mcp_failed", {"operation": operation}) in events
            assert events[-1] == ("nutrition_cart_skipped", {"reason": reason})
            if operation == "search":
                catalog.cart_link.assert_not_called()
            public = store.activity_logs()
            assert "Ошибка MCP ВкусВилла" in public
            assert "Корзина ВкусВилла не собрана" in public
            assert "PRIVATE" not in json.dumps(events) and "PRIVATE" not in public


def test_absent_basket_has_safe_reason_without_claiming_that_mcp_failed():
    with tempfile.TemporaryDirectory() as directory:
        store, client, service = fixture(directory)
        service.save(profile())
        catalog = attach(service, client, route=dict(ROUTE, budget_rub=100))
        response = service.chat("Меню на день для готовки, до 100 рублей")
        assert DIET in response and "Итого за закупку: 148,00 ₽" in response
        assert "превышает бюджет 100,00 ₽" in response
        catalog.cart_link.assert_not_called()
        assert mcp_events(store)[-1] == ("nutrition_cart_skipped", {"reason": "budget"})
        public = store.activity_logs()
        assert "закупка превышает указанный бюджет" in public
        assert "Ошибка MCP" not in public
        before = mcp_events(store)
        attach(service, client, route=NONE)
        service.answer("Что такое белок?")
        assert mcp_events(store) == before


def test_mcp_log_metadata_is_allowlisted_and_never_renders_arbitrary_text():
    for kind, details in (
        ("mcp_search_retry", {"attempt": 4, "reason": "timeout"}),
        ("mcp_search_retry", {"attempt": 2, "reason": "PRIVATE"}),
        ("mcp_search_retry", {"attempt": "PRIVATE", "reason": "network"}),
        ("mcp_started", {"operation": "PRIVATE"}),
        ("mcp_failed", {"operation": ["PRIVATE"]}),
        ("mcp_finished", {"operation": "search", "count": True}),
        ("mcp_finished", {"operation": "search", "count": -1}),
        ("mcp_finished", {"operation": "search", "count": "PRIVATE"}),
        ("mcp_finished", {"operation": "search", "count": 101}),
        ("nutrition_cart_skipped", {"reason": "PRIVATE"}),
        ("nutrition_cart_skipped", {"reason": ["PRIVATE"]}),
    ):
        assert (
            format_activity(
                {"created_at": NOW.isoformat(), "kind": kind, "details": json.dumps(details)}
            )
            is None
        )
    for kind, details in (
        ("mcp_search_retry", {"attempt": 2, "reason": "timeout"}),
        ("mcp_failed", {"operation": "search"}),
        ("nutrition_cart_skipped", {"reason": "mcp_error"}),
    ):
        rendered = format_activity(
            {
                "created_at": NOW.isoformat(),
                "kind": kind,
                "details": json.dumps(dict(details, exception="PRIVATE", query="PRIVATE")),
            }
        )
        assert rendered and "PRIVATE" not in rendered


async def mcp_panel_flow():
    for unavailable in (False, True):
        with tempfile.TemporaryDirectory() as directory:
            store, client, service = fixture(directory)
            service.save(profile())
            catalog = attach(service, client)
            if unavailable:
                catalog.search.side_effect = ValueError("PRIVATE")
            app = FitAgentApp(service)
            async with app.run_test(size=(120, 40)) as pilot:
                await pilot.pause()
                app.action_logs()
                app.ask("Меню на день для готовки")
                await app.workers.wait_for_complete()
                await pilot.pause()
                panel = app.query_one(LogsPanel)
                text = "\n".join(line.text for line in panel.query_one("#events", RichLog).lines)
                assert "MCP ВкусВилла" in text
                if unavailable:
                    assert "Ошибка MCP ВкусВилла" in text
                    assert "Корзина ВкусВилла не собрана" in text
                else:
                    assert "создание ссылки на корзину" in text
                assert "PRIVATE" not in text


def test_mcp_operations_and_fallback_are_visible_in_tui():
    asyncio.run(mcp_panel_flow())


def load_tests(loader, tests, pattern):
    return unittest.TestSuite(
        unittest.FunctionTestCase(value)
        for name, value in globals().items()
        if name.startswith("test_") and callable(value)
    )
