"""Actual API token usage, durable totals and the inline TUI; no paid requests."""

import asyncio
import tempfile
import unittest
from datetime import datetime, timezone
from unittest.mock import patch

import httpx
from rich.text import Text
from textual.widgets import Input, RichLog, Static

from fitagent.domain import validate_profile
from fitagent.logs import LogsPanel
from fitagent.provider import ModelClient
from fitagent.service import FitnessService
from fitagent.storage import Storage
from fitagent.tokens import api_usage, usage_text
from fitagent.ui import FitAgentApp, ProfileScreen

NOW = datetime(2026, 11, 11, tzinfo=timezone.utc)


def fixture(directory):
    store = Storage(directory)
    client = ModelClient(
        "TEST_SECRET",
        "https://example.test/chat",
        "test-model",
        store.session_id(),
        lambda kind, details: store.event(kind, details, NOW),
    )
    service = FitnessService(store, client, clock=lambda: NOW)
    service.save(validate_profile(180, 80, "мужской", 30, "Стать сильнее", ""))
    return store, client, service


def reply(input_tokens=100, output_tokens=20, **changes):
    data = {
        "choices": [{"message": {"content": "Ответ"}, "finish_reason": "stop"}],
        "usage": {
            "prompt_tokens": input_tokens,
            "completion_tokens": output_tokens,
            "prompt_tokens_details": {"cached_tokens": 80},
        },
    }
    data.update(changes)
    return httpx.Response(200, json=data)


def assert_error(callback):
    try:
        callback()
    except ValueError:
        return
    raise AssertionError("Expected a safe API error")


def test_totals_cover_all_purposes_restart_and_more_than_visible_logs():
    with tempfile.TemporaryDirectory() as directory:
        store, client, service = fixture(directory)
        with patch("fitagent.provider.httpx.Client") as factory:
            transport = factory.return_value.__enter__.return_value
            for purpose, incoming, outgoing in (
                ("goal_assessment", 100, 10),
                ("summarization", 200, 20),
                ("answer", 300, 30),
            ):
                transport.post.return_value = reply(incoming, outgoing)
                client.generate([], None, purpose)
        for _ in range(210):
            store.event("profile_check", {"due": False}, NOW)
        assert "api_usage" not in store.logs()
        report = Storage(directory).token_usage()
        assert report == {
            "requests": 3,
            "incomplete_requests": 0,
            "last_answer": {"input_tokens": 300, "output_tokens": 30},
            "input_tokens": 600,
            "output_tokens": 60,
            "total_tokens": 660,
        }
        service.clear()
        empty = Storage(directory).token_usage()
        assert empty["requests"] == 0
        assert empty["total_tokens"] == 0
        assert empty["last_answer"] is None


def test_missing_partial_failed_and_pending_statistics_are_not_zero():
    with tempfile.TemporaryDirectory() as directory:
        store, client, service = fixture(directory)
        with patch("fitagent.provider.httpx.Client") as factory:
            transport = factory.return_value.__enter__.return_value
            transport.post.return_value = reply(usage=None)
            client.generate([], None, "answer")
            report = store.token_usage()
            assert report["last_answer"] == {"input_tokens": None, "output_tokens": None}
            assert report["total_tokens"] is None
            assert report["input_tokens"] is None
            assert "нет данных" in usage_text(report)
            transport.post.return_value = reply(usage={"prompt_tokens": 150})
            client.generate([], None, "answer")
            transport.post.return_value = httpx.Response(500, text="TEST_SECRET")
            assert_error(lambda: client.generate([], None, "answer"))
        store.event("api_request", {"purpose": "answer"}, NOW)
        report = store.token_usage()
        assert report["requests"] == report["incomplete_requests"] == 4
        assert report["input_tokens"] == report["total_tokens"] == 150
        assert report["output_tokens"] is None
        assert report["last_answer"]["input_tokens"] is None
        assert "без полной статистики: 4" in usage_text(report)
        assert "TEST_SECRET" not in store.logs()


def test_usage_is_counted_before_rejecting_bad_answers():
    with tempfile.TemporaryDirectory() as directory:
        store, client, service = fixture(directory)
        with patch("fitagent.provider.httpx.Client") as factory:
            transport = factory.return_value.__enter__.return_value
            for choices in (
                [{"message": {"content": "обрезано"}, "finish_reason": "length"}],
                [{"message": {"content": " "}}],
                [],
            ):
                transport.post.return_value = reply(choices=choices)
                assert_error(lambda: client.generate([], None, "answer"))
        report = store.token_usage()
        assert report["requests"] == 3
        assert report["incomplete_requests"] == 0
        assert report["total_tokens"] == 360


def test_new_request_does_not_reuse_previous_answer_usage():
    with tempfile.TemporaryDirectory() as directory:
        store, client, service = fixture(directory)
        with patch("fitagent.provider.httpx.Client") as factory:
            transport = factory.return_value.__enter__.return_value
            transport.post.return_value = reply()
            client.generate([], None, "answer")
            client.generate([], None, "goal_assessment")
        assert store.token_usage()["last_answer"] == {"input_tokens": 100, "output_tokens": 20}
        store.event("api_request", {"purpose": "answer"}, NOW)
        pending = store.token_usage()
        assert pending["last_answer"] == {"input_tokens": None, "output_tokens": None}
        assert pending["total_tokens"] == 240
        store.event(
            "api_usage", {"purpose": "answer", "input_tokens": 200, "output_tokens": 40}, NOW
        )
        complete = store.token_usage()
        assert complete["last_answer"] == {"input_tokens": 200, "output_tokens": 40}
        assert complete["requests"] == 3 and complete["incomplete_requests"] == 0
        assert complete["total_tokens"] == 480


def test_legacy_usage_and_invalid_numbers():
    with tempfile.TemporaryDirectory() as directory:
        store = Storage(directory)
        store.event("api_usage", {"purpose": "answer", "input_tokens": 10, "output_tokens": 0}, NOW)
        report = store.token_usage()
        assert report["requests"] == 1 and report["total_tokens"] == 10
        assert report["last_answer"]["output_tokens"] == 0
    for invalid in (None, -1, True, "100", 1.5, [], {"secret": "DO_NOT_LOG"}):
        counts = api_usage({"usage": {"prompt_tokens": invalid, "completion_tokens": invalid}})
        assert counts["input_tokens"] is None and counts["output_tokens"] is None
    for invalid in (None, [], "DO_NOT_LOG", 1):
        assert api_usage({"usage": invalid})["input_tokens"] is None
        assert api_usage(invalid)["output_tokens"] is None
    assert api_usage({"usage": {"prompt_cache_hit_tokens": 12}})["cached_tokens"] == 12


async def panel_flow():
    with tempfile.TemporaryDirectory() as directory:
        store, client, service = fixture(directory)
        app = FitAgentApp(service)
        with patch("fitagent.provider.httpx.Client") as factory:
            transport = factory.return_value.__enter__.return_value
            transport.post.return_value = reply()
            async with app.run_test(size=(80, 24)) as pilot:
                await pilot.pause()
                panel = app.query_one(LogsPanel)
                field = app.query_one("#question", Input)
                assert not panel.display
                field.value = "/logs"
                await pilot.press("enter")
                assert panel.display and len(app.screen_stack) == 1
                field.value = "Как тренироваться?"
                await pilot.press("enter")
                await app.workers.wait_for_complete()
                await pilot.pause()
                counts = str(panel.query_one("#token-counts", Static).renderable)
                assert "контекст: 100" in counts and "ответ: 20" in counts
                assert "всего: 120" in counts
                sent = transport.post.call_args.kwargs["json"]["messages"]
                assert "ПОЛНЫЕ ДАННЫЕ" in sent[0]["content"]
                assert "Как тренироваться?" in sent[1]["content"]
                assert not field.disabled
                assert "api_usage" in "\n".join(
                    line.text for line in panel.query_one("#events", RichLog).lines
                )
                # The timer refreshes new events without reopening the panel.
                store.event("live_event", {}, NOW)
                await pilot.pause(2.1)
                assert "live_event" in "\n".join(
                    line.text for line in panel.query_one("#events", RichLog).lines
                )
                await pilot.press("ctrl+l")
                assert not panel.display
                await pilot.press("ctrl+l")
                assert panel.display
                field.value = "/clear"
                await pilot.press("enter")
                await pilot.pause()
                assert isinstance(app.screen, ProfileScreen)
                counts = str(panel.query_one("#token-counts", Static).renderable)
                assert "всего: 0" in counts and "контекст: 100" not in counts


def test_inline_panel_updates_and_toggles_without_blocking_chat():
    asyncio.run(panel_flow())


async def panel_error_flow():
    with tempfile.TemporaryDirectory() as directory:
        store, client, service = fixture(directory)
        app = FitAgentApp(service)
        async with app.run_test(size=(80, 24)) as pilot:
            await pilot.pause()
            with patch.object(store, "token_usage", side_effect=OSError("TEST_SECRET")):
                app.action_logs()
                panel = app.query_one(LogsPanel)
                text = str(panel.query_one("#token-counts", Static).renderable)
                assert "Не удалось загрузить" in text and "проверьте доступ" in text
                assert "TEST_SECRET" not in text
                assert not app.query_one("#question", Input).disabled
            panel.refresh_logs()
            text = str(panel.query_one("#token-counts", Static).renderable)
            assert "всего: 0" in text


def test_panel_storage_error_is_safe_and_recoverable():
    asyncio.run(panel_error_flow())


async def separated_panel_flow():
    for width, height in ((80, 24), (140, 45)):
        with tempfile.TemporaryDirectory() as directory:
            store, client, service = fixture(directory)
            store.event(
                "api_usage", {"purpose": "answer", "input_tokens": 100, "output_tokens": 20}, NOW
            )
            for index in range(80):
                store.event("test_event", {"index": index}, NOW)
            app = FitAgentApp(service)
            async with app.run_test(size=(width, height)) as pilot:
                await pilot.pause()
                app.action_logs()
                await pilot.pause()
                panel = app.query_one(LogsPanel)
                counts = panel.query_one("#token-counts", Static)
                events = panel.query_one("#events", RichLog)
                assert counts.border_title == "Расход токенов"
                assert events.border_title == "Лента событий"
                assert counts.styles.border_top[0] == "round"
                assert events.styles.border_top[0] == "solid"
                background = counts.styles.background
                assert background.r == background.g == background.b
                assert background != events.styles.background
                summary = counts.renderable
                assert isinstance(summary, Text)
                for word in ("За переписку", "100", "20", "всего: 120"):
                    offset = summary.plain.index(word)
                    assert summary.get_style_at_offset(app.console, offset).bold
                region = counts.region
                assert region.bottom <= events.region.y
                assert events.region.bottom <= panel.region.bottom
                assert events.max_scroll_y > 0
                events.scroll_to(y=0, animate=False, immediate=True)
                await pilot.pause()
                panel.refresh_logs()
                await pilot.pause()
                # Unchanged refresh does not reset the reader's position.
                assert events.scroll_y == 0
                assert counts.region == region
                assert counts.renderable == summary
                assert events.virtual_size.width <= events.scrollable_content_region.width
                assert not app.query_one("#question", Input).disabled


def test_token_summary_is_emphasized_and_stays_above_scrollable_events():
    asyncio.run(separated_panel_flow())


def load_tests(loader, tests, pattern):
    return unittest.TestSuite(
        unittest.FunctionTestCase(value)
        for name, value in globals().items()
        if name.startswith("test_") and callable(value)
    )
