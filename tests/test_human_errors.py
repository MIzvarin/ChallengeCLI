"""Readable refusals distinguish a known risk from a failed check. No external API."""

import asyncio
import json
import tempfile
import unittest

from support import allow_goal_continue, safe_response, safety_json, seed_accepted_goal
from test_memory import error, fixture
from textual.widgets import Input, RichLog

from fitagent.safety_schema import SAFETY_ERROR
from fitagent.ui import FitAgentApp

REASON = "Арахис может вызвать аллергическую реакцию: вы сообщили об аллергии на него."


def block(client):
    client.generate_safety.side_effect = lambda messages, stage: safety_json(
        status="block", description=REASON
    )


def test_refusal_explains_the_risk_and_next_step_without_blocking_the_chat():
    with tempfile.TemporaryDirectory() as directory:
        store, client, service = fixture(directory)
        before = service.memory.repository.load(service.clock())
        block(client)
        message = error(lambda: service.answer("Хочу перекус с арахисом"))
        assert message.startswith("Не могу выполнить эту просьбу.\nПричина: " + REASON)
        assert "попросить безопасный вариант или задать другой вопрос" in message
        assert "нужно будет подтвердить" in message
        assert "Сохранённые данные не изменены" in message
        assert "Запрос заблокирован проверкой безопасности" not in message
        assert service.memory.repository.load(service.clock()) == before
        client.generate.assert_not_called()
        assert REASON not in store.logs() + store.activity_logs()
        client.generate_safety.side_effect = safe_response
        assert service.answer("Предложи перекус без арахиса") == "Ответ"


def test_refusal_names_the_action_that_cannot_be_completed_at_each_stage():
    stages = {
        "onboarding": "Не могу сохранить профиль с такими данными.",
        "changes": "Не могу сохранить это изменение.",
        "response": "Не могу показать подготовленный ответ.",
        "goal_response": "Не могу показать эту оценку цели.",
    }
    with tempfile.TemporaryDirectory() as directory:
        store, client, service = fixture(directory)
        block(client)
        snapshot = service.memory.repository.load(service.clock())
        for stage, heading in stages.items():
            message = error(
                lambda: service.safety.check(stage, "Вопрос", snapshot, service.current_profile())
            )
            assert message.startswith(heading + "\nПричина: " + REASON)
            assert "другой вопрос" in message


def test_failed_check_is_not_presented_as_a_dangerous_question_and_leaks_no_payload():
    for failure in ("network", "protocol"):
        with tempfile.TemporaryDirectory() as directory:
            store, client, service = fixture(directory)
            if failure == "network":
                client.generate_safety.side_effect = OSError("PRIVATE_KEY_AND_PAYLOAD")
            else:
                client.generate_safety.side_effect = lambda messages, stage: "PRIVATE_BAD_JSON"
            message = error(lambda: service.answer("Как размяться перед прогулкой?"))
            assert message == SAFETY_ERROR
            assert "Причина:" in message
            assert "Это сбой проверки, а не вывод об опасности вашего вопроса" in message
            assert "Попробуйте ещё раз или задайте другой вопрос" in message
            assert "данные не изменены" in message
            assert "PRIVATE" not in message + store.logs() + store.activity_logs()
            assert "Запрос заблокирован" not in message
            client.generate.assert_not_called()


def test_failed_memory_processing_also_explains_that_this_is_not_a_safety_refusal():
    with tempfile.TemporaryDirectory() as directory:
        store, client, service = fixture(directory)
        client.generate_structured.return_value = "PRIVATE_BAD_JSON"
        message = error(lambda: service.answer("Вопрос о добавке"))
        assert "Причина:" in message
        assert "Это ошибка обработки, а не отказ из-за опасности вопроса" in message
        assert "Факты не изменены. Повторите вопрос" in message
        assert "PRIVATE" not in message + store.logs()
        client.generate_safety.assert_not_called()


def test_warning_still_returns_an_answer_instead_of_a_refusal():
    with tempfile.TemporaryDirectory() as directory:
        store, client, service = fixture(directory)
        client.generate_safety.side_effect = lambda messages, stage: safety_json(
            status="warning", strictness="advisory", description="Дозировка пока неизвестна."
        )
        response = service.answer("Какие сведения нужны до приёма добавки?")
        assert response.startswith("Ответ")
        assert "Предупреждение безопасности: Дозировка пока неизвестна." in response
        assert "Не могу" not in response
        assert "предупреждение" in store.activity_logs()


async def readable_refusal_flow():
    with tempfile.TemporaryDirectory() as directory:
        store, client, service = fixture(directory)
        seed_accepted_goal(service)
        allow_goal_continue(client)

        def blocked(messages, stage):
            value = json.loads(safe_response(messages, stage))
            value.update(status="block", description=REASON)
            for check in value["checks"]:
                check["status"] = "block"
            return json.dumps(value)

        client.generate_safety.side_effect = blocked
        app = FitAgentApp(service)
        async with app.run_test(size=(100, 40)) as pilot:
            await pilot.pause()
            app.ask("Хочу перекус с арахисом")
            await app.workers.wait_for_complete()
            await pilot.pause()
            text = "\n".join(line.text for line in app.query_one("#chat", RichLog).lines)
            assert "Не могу выполнить эту просьбу" in text and "Причина:" in text
            assert "аллергическую реакцию" in text and "безопасный вариант" in text
            assert "Запрос заблокирован проверкой безопасности" not in text
            assert not app.busy and not app.query_one("#question", Input).disabled


def test_tui_shows_a_readable_reason_and_keeps_input_available():
    asyncio.run(readable_refusal_flow())


def load_tests(loader, tests, pattern):
    return unittest.TestSuite(
        unittest.FunctionTestCase(value)
        for name, value in globals().items()
        if name.startswith("test_") and callable(value)
    )
