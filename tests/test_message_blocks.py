"""Framed live and restored messages retain content and fit the chat viewport."""

import asyncio
import tempfile
import unittest
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import Mock

from rich.console import Console
from rich.panel import Panel
from rich.text import Text
from textual.widgets import Input, RichLog

from fitagent.chat import write_message
from fitagent.domain import validate_profile
from fitagent.logs import LogsPanel
from fitagent.service import FitnessService
from fitagent.storage import Storage
from fitagent.ui import FitAgentApp

NOW = datetime(2026, 11, 11, tzinfo=timezone.utc)


def rendered_block(role, content, status="complete"):
    chat = Mock()
    write_message(chat, role, content, "2026-11-11", status)
    block = chat.write.call_args_list[0].args[0]
    assert isinstance(block, Panel)
    assert block.padding == (1, 2)
    assert isinstance(block.title, Text) and block.title.style == "bold #d4d4d4"
    console = Console(width=60, color_system=None)
    with console.capture() as capture:
        console.print(block)
    return capture.get()


def test_message_blocks_have_author_padding_and_safe_formatting():
    user = rendered_block("user", "[red]**Текст пользователя**[/red]")
    assert "Вы · 2026-11-11" in user
    assert "[red]**Текст пользователя**[/red]" in user
    assert user.splitlines()[0].startswith("╭")
    assert user.splitlines()[-1].endswith("╯")
    assert user.splitlines()[1].strip("│ ") == ""
    assistant = rendered_block("assistant", "**Ответ модели**")
    assert "FitAgent" in assistant and "Ответ модели" in assistant
    assert "**Ответ модели**" not in assistant


def test_incomplete_question_notices_stay_inside_the_block():
    for status, notice in (("failed", "Ответ не получен"), ("pending", "Запрос не завершён")):
        text = rendered_block("user", "Мой вопрос", status)
        assert "Мой вопрос" in text and notice in text
        notice_line = next(line for line in text.splitlines() if notice in line)
        assert notice_line.startswith("│") and notice_line.endswith("│")


async def framed_chat_flow():
    for width, height in ((80, 24), (140, 45)):
        with tempfile.TemporaryDirectory() as directory:
            store = Storage(directory)
            client = SimpleNamespace(
                model="test-model", generate=Mock(return_value="**Новый ответ**")
            )
            service = FitnessService(store, client, clock=lambda: NOW)
            service.save(validate_profile(180, 80, "мужской", 30, "Стать сильнее", ""))
            store.message("user", "Старый вопрос", NOW)
            store.message("assistant", "**Старый ответ**", NOW)
            app = FitAgentApp(service)
            async with app.run_test(size=(width, height)) as pilot:
                await pilot.pause()
                chat = app.query_one("#chat", RichLog)
                text = "\n".join(line.text for line in chat.lines)
                assert text.count("╭") == text.count("╯") == 2
                assert "Старый вопрос" in text and "Старый ответ" in text
                assert "**Старый ответ**" not in text
                assert not client.generate.called
                app.query_one("#question", Input).value = "Новый вопрос"
                await pilot.press("enter")
                await app.workers.wait_for_complete()
                await pilot.pause()
                text = "\n".join(line.text for line in chat.lines)
                assert text.count("╭") == text.count("╯") == 4
                assert "Новый вопрос" in text and "Новый ответ" in text
                assert "**Новый ответ**" not in text
                assert chat.virtual_size.width <= chat.scrollable_content_region.width
                app.action_logs()
                await pilot.pause()
                assert app.query_one(LogsPanel).display
                assert chat.region.bottom <= app.query_one(LogsPanel).region.y
                assert chat.virtual_size.width <= chat.scrollable_content_region.width


def test_live_and_restored_messages_use_the_same_frames():
    asyncio.run(framed_chat_flow())


def load_tests(loader, tests, pattern):
    return unittest.TestSuite(
        unittest.FunctionTestCase(value)
        for name, value in globals().items()
        if name.startswith("test_") and callable(value)
    )
