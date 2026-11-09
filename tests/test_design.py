"""Layout regressions for the monochrome TUI; no external API calls."""

import asyncio
import tempfile
import unittest
from types import SimpleNamespace

from textual.widgets import Button, Input

from fitagent.service import FitnessService
from fitagent.storage import Storage
from fitagent.ui import FitAgentApp, LogsScreen, ProfileScreen, RiskScreen


def assert_inside(widget, width, height):
    region = widget.region
    assert region.width > 0 and region.height > 0, region
    assert 0 <= region.x < region.right <= width, region
    assert 0 <= region.y < region.bottom <= height, region


async def layout_flow():
    for width, height in ((80, 24), (140, 45)):
        with tempfile.TemporaryDirectory() as directory:
            service = FitnessService(Storage(directory), SimpleNamespace())
            app = FitAgentApp(service)
            async with app.run_test(size=(width, height)) as pilot:
                await pilot.pause()
                assert isinstance(app.screen, ProfileScreen)
                for name in ("save", "later"):
                    button = app.screen.query_one(f"#{name}", Button)
                    assert_inside(button, width, height)
                    background = button.styles.background
                    assert background.r == background.g == background.b
                background = app.screen.query_one("#dialog").styles.background
                assert background.r == background.g == background.b
                await app.push_screen(RiskScreen("Длинное предупреждение. " * 200))
                await pilot.pause()
                for name in ("edit", "confirm"):
                    assert_inside(app.screen.query_one(f"#{name}", Button), width, height)
                app.pop_screen()
                app.pop_screen()
                await pilot.pause()
                assert_inside(app.query_one("#question", Input), width, height)
                assert not any(edge[0] for edge in app.query_one("#chat").styles.border)
                app.action_logs()
                await pilot.pause()
                assert isinstance(app.screen, LogsScreen)
                assert_inside(app.screen.query_one("#close", Button), width, height)


def test_monochrome_layout():
    asyncio.run(layout_flow())


def load_tests(loader, tests, pattern):
    return unittest.TestSuite([unittest.FunctionTestCase(test_monochrome_layout)])
