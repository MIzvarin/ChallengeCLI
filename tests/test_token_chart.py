"""Cumulative token history and modal chart regressions; no real API requests."""

import asyncio
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import Mock, patch

from textual.widgets import Input, RichLog

from fitagent.chart import cumulative_chart
from fitagent.domain import validate_profile
from fitagent.logs import LogsPanel
from fitagent.service import FitnessService
from fitagent.storage import Storage
from fitagent.token_chart import TokenChart, TokensScreen
from fitagent.token_summary import TokenSummary
from fitagent.ui import FitAgentApp

NOW = datetime(2026, 11, 11, tzinfo=timezone.utc)


def fixture(directory):
    store = Storage(directory)
    client = SimpleNamespace(model="test-model", generate=Mock(return_value="Ответ"))
    service = FitnessService(store, client, clock=lambda: NOW)
    service.save(validate_profile(180, 80, "мужской", 30, "Стать сильнее", ""))
    return store, client, service


def add_usage(store, purpose="answer", incoming=100, outgoing=20, now=NOW):
    store.event("api_request", {"purpose": purpose}, now)
    store.event(
        "api_usage",
        {
            "purpose": purpose,
            "input_tokens": incoming,
            "output_tokens": outgoing,
            "cached_tokens": 80,
        },
        now,
    )


def assert_inside(widget, width, height):
    region = widget.region
    assert region.width > 0 and region.height > 0, region
    assert 0 <= region.x < region.right <= width, region
    assert 0 <= region.y < region.bottom <= height, region


def test_history_and_summary_cover_every_call_and_survive_restart():
    with tempfile.TemporaryDirectory() as directory:
        store, client, service = fixture(directory)
        purposes = ("answer", "summarization", "goal_assessment")
        for index in range(300):
            add_usage(store, purposes[index % 3], now=NOW - timedelta(days=300 - index))
        report = Storage(directory).token_usage(include_history=True)
        assert len(report["history"]) == report["requests"] == 300
        assert report["history"][0] == {"request": 1, "total_tokens": 120, "incomplete": False}
        assert report["history"][-1] == {
            "request": 300,
            "total_tokens": 36_000,
            "incomplete": False,
        }
        assert report["history"][-1]["total_tokens"] == report["total_tokens"]
        assert {
            key: value for key, value in report.items() if key != "history"
        } == store.token_usage()
        service.clear()
        assert Storage(directory).token_usage(include_history=True)["history"] == []
        client.generate.assert_not_called()


def test_history_preserves_unknown_partial_zero_and_orphan_usage():
    with tempfile.TemporaryDirectory() as directory:
        store = Storage(directory)
        store.event("api_request", {"purpose": "answer"}, NOW)
        add_usage(store, "goal_assessment", incoming=30, outgoing=None)
        add_usage(store, "summarization", incoming=10, outgoing=5)
        # Existing databases may also contain usage without a matching request event.
        store.event("api_usage", {"purpose": "answer", "input_tokens": 0, "output_tokens": 0}, NOW)
        report = store.token_usage(include_history=True)
        assert [point["total_tokens"] for point in report["history"]] == [None, 30, 45, 45]
        assert [point["incomplete"] for point in report["history"]] == [True, True, False, False]
        assert [point["request"] for point in report["history"]] == [1, 2, 3, 4]
        assert report["incomplete_requests"] == 2
        assert report["total_tokens"] == 45
        assert "?" in cumulative_chart(report["history"], 70, 12).plain


def test_long_chart_is_bounded_and_retains_endpoints_and_incomplete_buckets():
    points = [
        {"request": index, "total_tokens": index * 123, "incomplete": index == 3}
        for index in range(1, 5001)
    ]
    text = cumulative_chart(points, 80, 20).plain
    lines = text.splitlines()
    assert len(lines) == 20
    assert all(len(line) <= 80 for line in lines)
    assert "615 000" in lines[0] and lines[0].endswith("●")
    assert lines[-1].strip().startswith("0") and lines[-1].endswith("5000")
    assert "?" in text  # The incomplete third call must survive compression into a column.


def test_chart_empty_unknown_single_zero_and_small_window_states():
    assert "пока нет" in cumulative_chart([], 80, 15).plain
    unknown = [{"request": 1, "total_tokens": None, "incomplete": True}]
    text = cumulative_chart(unknown, 80, 15).plain
    assert "API не сообщил" in text and "└" not in text
    zero = [{"request": 1, "total_tokens": 0, "incomplete": False}]
    assert "└" in cumulative_chart(zero, 80, 15).plain
    assert "?" not in cumulative_chart(zero, 80, 15).plain
    single = [{"request": 1, "total_tokens": 120, "incomplete": False}]
    assert "120" in cumulative_chart(single, 80, 15).plain
    assert "Увеличьте окно" in cumulative_chart(single, 12, 3).plain


async def chart_screen_flow():
    for width, height in ((80, 24), (140, 45)):
        with tempfile.TemporaryDirectory() as directory:
            store, client, service = fixture(directory)
            for _ in range(220):
                add_usage(store)
            app = FitAgentApp(service)
            async with app.run_test(size=(width, height)) as pilot:
                await pilot.pause()
                field = app.query_one("#question", Input)
                field.value = "/help"
                await pilot.press("enter")
                await pilot.pause()
                assert "/tokens" in "\n".join(
                    line.text for line in app.query_one("#chat", RichLog).lines
                )
                field.value = "/logs"
                await pilot.press("enter")
                await pilot.pause()
                root = app.screen
                panel = root.query_one(LogsPanel)
                summary = panel.query_one(TokenSummary).renderable.copy()
                chat = [line.text for line in root.query_one("#chat", RichLog).lines]
                field.value = "/tokens"
                await pilot.press("enter")
                await pilot.pause()
                assert isinstance(app.screen, TokensScreen)
                screen = app.screen
                counts = screen.query_one(TokenSummary)
                chart = screen.query_one(TokenChart)
                assert counts.renderable == summary
                assert counts.styles.background == panel.query_one(TokenSummary).styles.background
                assert counts.styles.border == panel.query_one(TokenSummary).styles.border
                for selector in (
                    "#chart-token-counts",
                    "#token-chart",
                    "#chart-note",
                    "#close-tokens",
                ):
                    assert_inside(screen.query_one(selector), width, height)
                assert counts.region.bottom <= chart.region.y
                assert chart.region.bottom <= screen.query_one("#chart-note").region.y
                assert len(chart.points) == 220
                assert chart.points[-1]["total_tokens"] == 26_400
                # Counts use readable thousands separators.
                assert "26400" not in chart.render().plain
                assert "26 400" in chart.render().plain
                await pilot.resize_terminal(100, 32)
                await pilot.pause()
                assert all(
                    len(line) <= chart.size.width for line in chart.render().plain.splitlines()
                )
                add_usage(store, "summarization", incoming=200, outgoing=50)
                if width == 80:
                    await pilot.pause(2.1)  # Automatic refresh, without reopening.
                else:
                    screen.refresh_usage()
                assert chart.points[-1]["total_tokens"] == 26_650
                assert "26 650" in str(counts.renderable)
                await pilot.press("escape")
                await pilot.pause()
                assert app.screen is root and field.has_focus
                assert [line.text for line in root.query_one("#chat", RichLog).lines] == chat
                field.value = "/tokens"
                await pilot.press("enter")
                await pilot.pause()
                await pilot.click("#close-tokens")
                await pilot.pause()
                assert app.screen is root
                assert store.chat_messages() == []
                client.generate.assert_not_called()


def test_tokens_command_opens_chart_with_duplicated_summary_and_returns_to_chat():
    asyncio.run(chart_screen_flow())


async def chart_error_flow():
    with tempfile.TemporaryDirectory() as directory:
        store, client, service = fixture(directory)
        app = FitAgentApp(service)
        async with app.run_test(size=(80, 24)) as pilot:
            await pilot.pause()
            app.action_tokens()
            await pilot.pause()
            screen = app.screen
            chart = screen.query_one(TokenChart)
            assert "пока нет" in chart.render().plain
            with patch.object(store, "token_usage", side_effect=OSError("TEST_SECRET")):
                screen.refresh_usage()
                text = str(screen.query_one(TokenSummary).renderable)
                assert "Не удалось загрузить" in text and "проверьте доступ" in text
                assert "TEST_SECRET" not in text and "TEST_SECRET" not in chart.render().plain
            add_usage(store, incoming=None, outgoing=None)
            screen.refresh_usage()
            assert "нет данных" in str(screen.query_one(TokenSummary).renderable)
            assert "API не сообщил" in chart.render().plain
            for selector in ("#chart-token-counts", "#token-chart", "#close-tokens"):
                assert_inside(screen.query_one(selector), 80, 24)
            service.clear()
            screen.refresh_usage()
            assert "пока нет" in chart.render().plain
            assert "всего: 0" in str(screen.query_one(TokenSummary).renderable)
            await pilot.press("ctrl+q")
            await pilot.pause()
            assert app._exit
            client.generate.assert_not_called()


def test_chart_empty_unknown_error_recovery_and_clear():
    asyncio.run(chart_error_flow())


def load_tests(loader, tests, pattern):
    return unittest.TestSuite(
        unittest.FunctionTestCase(value)
        for name, value in globals().items()
        if name.startswith("test_") and callable(value)
    )
