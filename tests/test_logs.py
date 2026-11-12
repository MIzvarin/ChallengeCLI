"""Compaction event highlighting in the audit panel; no external API calls."""

import asyncio
import tempfile
import unittest
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import patch

from rich.text import Text
from textual.widgets import RichLog

from fitagent.domain import validate_profile
from fitagent.logs import LogsPanel
from fitagent.service import FitnessService
from fitagent.storage import Storage
from fitagent.ui import FitAgentApp

NOW = datetime(2026, 11, 12, tzinfo=timezone.utc)


async def compaction_highlighting_flow():
    for size in ((80, 24), (140, 45)):
        with tempfile.TemporaryDirectory() as directory:
            store = Storage(directory)
            service = FitnessService(store, SimpleNamespace(), clock=lambda: NOW)
            service.save(validate_profile(180, 80, "мужской", 30, "Стать сильнее", ""))
            for kind in ("summary_started", "summary_finished", "api_usage", "other_event"):
                store.event(
                    kind,
                    {"note": "summary_started  summary_finished  [red]текст[/red]"},
                    NOW,
                )
            app = FitAgentApp(service)
            async with app.run_test(size=size) as pilot:
                await pilot.pause()
                panel = app.query_one(LogsPanel)
                log = panel.query_one("#events", RichLog)
                with patch.object(log, "write", wraps=log.write) as write:
                    app.action_logs()
                    await pilot.pause()
                    for names in (
                        ("summary_started", "summary_finished"),
                        ("summary_started", "summary_finished", "summary_cache_hit"),
                    ):
                        if "summary_cache_hit" in names:
                            store.event("summary_cache_hit", {"key": "test"}, NOW)
                            panel.refresh_logs()
                        rendered = write.call_args.args[0]
                        assert isinstance(rendered, Text)
                        assert rendered.plain == store.logs()
                        assert len(rendered.spans) == len(names)
                        for span, name in zip(rendered.spans, names):
                            assert span.style == "blue"
                            assert rendered.plain[span.start : span.end] == name
                            assert rendered.plain[span.start - 2 : span.start] == "  "
                            assert rendered.plain[span.end : span.end + 2] == "  "
                        calls = write.call_count
                        panel.refresh_logs()
                        assert write.call_count == calls


def test_only_compaction_names_are_blue_on_display_and_refresh():
    asyncio.run(compaction_highlighting_flow())


def load_tests(loader, tests, pattern):
    return unittest.TestSuite(
        [unittest.FunctionTestCase(test_only_compaction_names_are_blue_on_display_and_refresh)]
    )
