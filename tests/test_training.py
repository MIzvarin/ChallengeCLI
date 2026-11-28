"""Training negotiation, persistence, and local MCP contracts."""

import asyncio
import io
import json
import tempfile
import unittest
from datetime import timedelta
from unittest.mock import patch

from support import EMPTY_TURN, safe_response, safety_json
from test_fitagent import NOW, fixture, profile
from test_memory import error, extract, operation

from fitagent import training
from fitagent.service import FitnessService
from fitagent.storage import Storage
from fitagent.training_mcp import request_training, serve


def proposal(exercise="Приседания"):
    days = {day: [] for day in training.DAY_KEYS}
    days["mon"] = [{"name": exercise, "load": "3 подхода по 8 повторений"}]
    return {"question": "", "days": days}


def agree(service, client, exercise="Приседания"):
    client.generate_structured.return_value = json.dumps(proposal(exercise), ensure_ascii=False)
    assert exercise in service.chat("/training")
    assert "сохранена" in service.chat(training.ACCEPT)


def test_training_mcp_discovery_validation_and_real_stdio_without_model():
    with tempfile.TemporaryDirectory() as directory:
        _, _, service = fixture(directory)
        packets = [
            ("initialize", {}),
            ("tools/list", {}),
            ("tools/call", {"name": training.TODAY, "arguments": {"profile": "PRIVATE"}}),
        ]
        source = io.StringIO(
            "\n".join(
                json.dumps({"jsonrpc": "2.0", "id": index, "method": method, "params": params})
                for index, (method, params) in enumerate(packets)
            )
        )
        sink = io.StringIO()
        serve(service, source, sink)
        hello, discovered, invalid = [
            json.loads(line)["result"] for line in sink.getvalue().splitlines()
        ]
        assert hello["serverInfo"]["name"] == "fitagent-training"
        assert {tool["name"] for tool in discovered["tools"]} == {
            training.CREATE,
            training.CONFIRM,
            training.TODAY,
        }
        assert invalid["isError"] and invalid["content"][0]["text"] == training.ARGUMENT_ERROR
        assert "PRIVATE" not in service.store.activity_logs()
        assert asyncio.run(request_training(directory, training.TODAY, {})) == {
            "status": "unknown",
            "exercises": [],
        }
        service.save(profile())
        agree(service, service.client)
        result = asyncio.run(request_training(directory, training.TODAY, {}))
        assert result["status"] in {"training", "rest"}


def test_clarification_revision_explicit_acceptance_and_training_or_rest():
    with tempfile.TemporaryDirectory() as directory:
        store, client, service = fixture(directory)
        service.save(profile())
        clarification = {"question": "Какое оборудование доступно?", "days": None}
        revision_question = {"question": "Какие выпады вам удобны?", "days": None}
        proposals = iter((clarification, proposal(), revision_question, proposal("Выпады")))

        def structured(messages, schema, purpose):
            if purpose == "training_route":
                return json.dumps({"training_reply": True})
            return json.dumps(next(proposals), ensure_ascii=False)

        client.generate_structured.side_effect = structured
        start = "Составь программу тренировок в понедельник"
        assert clarification["question"] in service.chat(start)
        error(lambda: service.chat(training.ACCEPT), "Сначала ответьте")
        assert "Приседания" in service.chat("Дома, доступна гантель.")
        assert revision_question["question"] in service.chat("Замени приседания на выпады.")
        assert "Выпады" in service.chat("Обратные выпады.")
        data = json.loads(client.generate_structured.call_args.args[0][-1]["content"])
        assert data["turns"] == [
            {"requirements": start},
            {"question": clarification["question"]},
            {"requirements": "Дома, доступна гантель."},
            {"requirements": "Замени приседания на выпады."},
            {"question": revision_question["question"]},
            {"requirements": "Обратные выпады."},
        ]
        assert data["profile"]["height"] == 180
        assert data["goal"]["text"] == profile()["goal"]
        assert data["previous_draft"] == proposal()["days"]
        assert "Выпады" not in service.memory.command("/memory")
        assert service.training_tool(training.TODAY, {})["status"] == "unknown"
        draft_id = service.training_chat.draft_id
        assert "сохранена" in service.chat(training.ACCEPT)
        reopened = FitnessService(Storage(directory), client, clock=lambda: NOW)
        assert reopened.training_tool(training.CONFIRM, {"draft_id": draft_id}) == {
            "message": training.SAVED
        }
        assert "Выпады" in reopened.memory.command("/memory")
        assert (
            json.loads(reopened.memory.context())["confirmed_training_program"]
            == proposal("Выпады")["days"]
        )
        assert reopened.training_tool(training.TODAY, {}) == {
            "status": "training",
            "exercises": proposal("Выпады")["days"]["mon"],
        }
        reopened.clock = lambda: NOW + timedelta(days=1)
        assert reopened.training_tool(training.TODAY, {}) == {"status": "rest", "exercises": []}
        assert store.daily_plan_status(NOW) == "missing"


def test_previous_program_survives_unconfirmed_revision_and_stale_acceptance():
    with tempfile.TemporaryDirectory() as directory:
        _, client, service = fixture(directory)
        service.save(profile())
        agree(service, client)
        second = FitnessService(Storage(directory), client, clock=lambda: NOW)
        error(lambda: second.chat(training.ACCEPT), "в этом окне")
        client.generate_structured.return_value = json.dumps(proposal("Выпады"))
        service.chat("/training")
        client.generate_structured.return_value = json.dumps(proposal("Планка"))
        second.chat("/training")
        error(lambda: service.chat(training.ACCEPT), "Черновик или данные изменились")
        assert "Приседания" in service.memory.command("/memory")
        assert "не изменилась" in second.chat("/training выход")
        error(lambda: second.chat(training.ACCEPT), "в этом окне")
        assert "Планка" not in second.memory.command("/memory")


def test_training_rejects_invalid_or_blocked_proposals_and_stale_context():
    with tempfile.TemporaryDirectory() as directory:
        _, client, service = fixture(directory)
        service.save(profile())
        agree(service, client)
        invalid = proposal()
        del invalid["days"]["tue"]
        client.generate_structured.return_value = json.dumps(invalid)
        error(lambda: service.chat("/training"), "Не удалось обработать")
        client.generate_structured.return_value = json.dumps(proposal("Планка"))

        def check(messages, stage):
            payload = json.loads(messages[-1]["content"])
            blocked = stage == "response" and "Планка" in (payload["draft"] or "")
            return safety_json(
                payload["required_rule_ids"],
                status="block" if blocked else "allow",
                description="Нагрузка несовместима с ограничением.",
            )

        client.generate_safety.side_effect = check
        error(lambda: service.chat("/training"), "Не могу показать")
        assert not service.store.training_paused()
        client.generate_safety.side_effect = safe_response
        service.chat("/training")
        other = FitnessService(Storage(directory), client, clock=lambda: NOW)
        extract(client, operation("Тренируюсь дома", category="equipment", key="home"))
        other.answer("Тренируюсь дома")
        error(lambda: service.chat(training.ACCEPT), "данные изменились")
        assert "Приседания" in service.memory.command("/memory")
        assert "Планка" not in service.memory.command("/memory")


def test_training_command_does_not_consume_onboarding_answer():
    with tempfile.TemporaryDirectory() as directory:
        _, _, service = fixture(directory)
        service.start_chat()
        error(lambda: service.chat("/training"), "Сначала заполните профиль")
        assert not service.onboarding.state()["answers"]


def test_revisions_keep_confirmed_constraints_for_generation_and_safety():
    with tempfile.TemporaryDirectory() as directory:
        _, client, service = fixture(directory)
        service.save(profile())
        constraint = "Из-за ограничений колена могу тренироваться только сидя."
        abandoned = "Только в бассейне."
        client.generate_structured.return_value = json.dumps(proposal("Жим сидя"))
        service.chat("/training " + constraint)
        service.chat(training.ACCEPT)
        service.chat("/training " + abandoned)
        service.chat("/training выход")

        for request in ("Сократи длительность занятий.", "Перенеси занятие на субботу."):
            service = FitnessService(Storage(directory), client, clock=lambda: NOW)
            service.chat("/training " + request)
            payload = json.loads(client.generate_structured.call_args.args[0][-1]["content"])
            assert constraint in payload["confirmed_dialogue"]
            assert abandoned not in payload["confirmed_dialogue"]
            for check in client.generate_safety.call_args_list[-2:]:
                safety = json.loads(check.args[0][-1]["content"])
                dialogue = json.loads(safety["dialogue"])
                assert dialogue["confirmed_dialogue"] == payload["confirmed_dialogue"]
            service.chat(training.ACCEPT)


def test_unrelated_question_uses_ordinary_chat_without_invalidating_draft():
    with tempfile.TemporaryDirectory() as directory:
        store, client, service = fixture(directory)
        service.save(profile())
        questions = ("Как изменить целевой вес?", "Составь рацион под мою программу тренировок.")

        def structured(messages, schema, purpose="memory_extraction"):
            payload = json.loads(messages[-1]["content"])
            if purpose == "training_program":
                return json.dumps(proposal())
            if purpose == "training_route":
                assert "Приседания" in payload["last_training_reply"]
                assert payload["message"] in questions
                return json.dumps({"training_reply": False})
            if purpose == "goal_planning":
                return json.dumps(
                    {
                        "action": "clarify",
                        "question": "Какой вес вы хотите указать?",
                        "plan": "",
                        "criterion": "",
                    }
                )
            assert purpose == "memory_extraction"
            return EMPTY_TURN

        client.generate_structured.side_effect = structured
        service.chat("/training")
        draft_id = service.training_chat.draft_id
        for question in questions:
            assert "Черновик программы тренировок" not in service.chat(question)
            assert service.training_chat.draft_id == draft_id
            assert any(
                row["content"] == question and row["status"] == "complete"
                for row in store.chat_messages()
            )
        assert (
            sum(
                call.kwargs.get("purpose") == "training_program"
                for call in client.generate_structured.call_args_list
            )
            == 1
        )
        assert training.SAVED == service.chat(training.ACCEPT)


def test_routing_error_keeps_draft_and_uses_a_russian_message():
    with tempfile.TemporaryDirectory() as directory:
        _, client, service = fixture(directory)
        service.save(profile())
        client.generate_structured.return_value = json.dumps(proposal())
        service.chat("/training")
        client.generate_structured.return_value = '{"training_reply":"PRIVATE"}'
        problem = error(
            lambda: service.chat("Сколько нужно отдыхать между подходами?"),
            "Не удалось определить",
        )
        assert "PRIVATE" not in problem
        assert training.SAVED == service.chat(training.ACCEPT)


def test_completed_training_exchange_cannot_reappear_after_clear_in_another_window():
    with tempfile.TemporaryDirectory() as directory:
        store, client, service = fixture(directory)
        service.save(profile())
        client.generate_structured.return_value = json.dumps(proposal())
        other = FitnessService(Storage(directory), client, clock=lambda: NOW)
        run_tool = service.training_tool

        def finish_then_clear(name, arguments):
            result = run_tool(name, arguments)
            other.clear()
            assert not store.chat_messages()
            return result

        request = "/training Тренируюсь дома с личными ограничениями"
        with patch.object(service, "training_tool", side_effect=finish_then_clear):
            error(lambda: service.chat(request), "Данные очищены в другом окне")
        assert not store.chat_messages()
        assert service.training_chat.draft_id is None
        assert service.training_tool(training.TODAY, {})["status"] == "unknown"


def test_long_confirmed_dialogue_keeps_the_original_constraint_verbatim():
    with tempfile.TemporaryDirectory() as directory:
        _, client, service = fixture(directory)
        service.save(profile())
        constraint = "Из-за ограничений колена могу тренироваться только сидя."
        client.generate_structured.return_value = json.dumps(proposal("Жим сидя"))
        service.chat("/training " + constraint)
        service.chat(training.ACCEPT)
        details = "Предпочитаю тренироваться дома; расписание недельное. " * 28
        for revision in range(20):
            service.chat(f"/training Уточнение {revision}. {details}")
            service.chat(training.ACCEPT)
        service.chat("/training Перенеси занятие на субботу.")
        payload = json.loads(client.generate_structured.call_args.args[0][-1]["content"])
        dialogue = payload["confirmed_dialogue"]
        assert len(dialogue.encode()) > 50_000
        assert constraint in dialogue
        for check in client.generate_safety.call_args_list[-3:]:
            safety = json.loads(check.args[0][-1]["content"])
            assert json.loads(safety["dialogue"])["confirmed_dialogue"] == dialogue
        client.generate.assert_not_called()


def load_tests(loader, tests, pattern):
    return unittest.TestSuite(
        unittest.FunctionTestCase(test)
        for name, test in globals().items()
        if name.startswith("test_")
    )
