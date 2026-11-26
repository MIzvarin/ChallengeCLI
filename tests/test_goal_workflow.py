"""Goal pipeline integration tests with a mocked model."""

import copy
import json
import sqlite3
import tempfile
import unittest
from datetime import timedelta
from unittest.mock import patch

from support import EMPTY_TURN, safe_response
from test_fitagent import NOW, fixture, profile
from test_layered_memory import ANSWERS
from test_memory import error, operation, responses_reply

from fitagent.goal_planner import PLAN_ERROR, PLAN_PROMPT, PLAN_SCHEMA, GoalPlanner
from fitagent.goal_state import (
    DONE,
    EXECUTION,
    PLANNING,
    TRANSITIONS,
    VALIDATION,
    initial_state,
    read_state,
    reset_assessment,
    transition,
)
from fitagent.goal_workflow import required_information
from fitagent.provider import DEFAULT_MODEL, MEMORY_URL, ModelClient
from fitagent.safety_schema import SAFETY_PROMPT
from fitagent.service import FitnessService
from fitagent.storage import Storage

PLAN = {
    "action": "propose",
    "question": "",
    "plan": "Три умеренные силовые тренировки в неделю, отдых между ними; записывать нагрузки и самочувствие.",
    "criterion": "Выполнены четыре недели тренировок без ухудшения самочувствия.",
}
CONTINUE = {"action": "continue", "question": "", "plan": "", "criterion": ""}


def configure(client, proposal=None, turn=None):
    def structured(messages, schema, purpose="memory_extraction"):
        payload = json.loads(messages[-1]["content"])
        if purpose == "goal_planning":
            value = proposal(payload) if callable(proposal) else proposal or PLAN
        else:
            value = turn(payload) if callable(turn) else turn or json.loads(EMPTY_TURN)
        return value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)

    client.generate_structured.side_effect = structured


def prepared(directory, target=None):
    store, client, service = fixture(directory)
    service.start_chat()
    answers = list(ANSWERS)
    if target:
        answers[5] = str(target)
    for answer in answers:
        service.chat(answer)
    configure(client)
    return store, client, service


def execute_plan(service):
    assert "Черновик плана" in service.chat("продолжить")
    assert service.workflow.state()["stage"] == VALIDATION
    assert "Начинаем выполнение" in service.chat("подтверждаю план")
    assert service.workflow.state()["stage"] == EXECUTION


def test_exact_transition_graph_and_terminal_state():
    for source in TRANSITIONS:
        for destination in TRANSITIONS:
            state = dict(
                initial_state(1),
                stage=source,
                plan="План",
                criterion="Результат",
                fingerprint="checked",
                assessment={"status": "allow", "description": "Хорошо"},
            )
            if destination in TRANSITIONS[source]:
                transition(state, destination)
                assert state["stage"] == destination
            else:
                error(lambda: transition(state, destination), "запрещён")
    state = dict(initial_state(1), stage=VALIDATION)
    error(lambda: transition(state, EXECUTION), "Сначала проверьте")
    state.update(plan="План", criterion="Результат", fingerprint="checked")
    state["assessment"] = {"status": "block", "description": "Опасность"}
    error(lambda: transition(state, EXECUTION), "Сначала проверьте")


def test_onboarding_records_unvalidated_goal_without_executing_or_blocking_on_bmi():
    with tempfile.TemporaryDirectory() as directory:
        store, client, service = prepared(directory, target=90)
        assert store.latest("profiles")
        assert service.workflow.state()["stage"] == PLANNING
        assert not store.latest("goals")["risk_confirmed"]
        client.generate.assert_not_called()
        response = service.chat("продолжить")
        assert "Предупреждение — рекомендация" in response
        assert "Опасность" not in response
        assert service.workflow.state()["stage"] == VALIDATION
        service.chat("подтверждаю план")
        assert service.workflow.state()["stage"] == EXECUTION
        assert store.latest("goals")["risk_confirmed"]


def test_allow_plan_needs_explicit_acceptance_and_confirmation_does_not_call_model():
    with tempfile.TemporaryDirectory() as directory:
        store, client, service = prepared(directory)
        response = service.chat("продолжить")
        assert "всё хорошо" in response and PLAN["criterion"] in response
        assert service.workflow.state()["stage"] == VALIDATION
        service.chat("да")
        assert service.workflow.state()["stage"] == VALIDATION
        before = (client.generate_structured.call_count, client.generate_safety.call_count)
        service.chat("Подтверждаю план")
        assert service.workflow.state()["stage"] == EXECUTION
        assert before == (client.generate_structured.call_count, client.generate_safety.call_count)
        client.generate.assert_not_called()
        checks = [
            args for args in client.generate_safety.call_args_list if args.args[1] == "goal_plan"
        ]
        payload = json.loads(checks[-1].args[0][1]["content"])
        assert PLAN["plan"] in payload["draft"]
        assert PLAN["criterion"] in payload["draft"]


def test_warning_blocks_execution_until_user_accepts_it_with_plan():
    with tempfile.TemporaryDirectory() as directory:
        store, client, service = prepared(directory)

        def warning(messages, stage):
            result = json.loads(safe_response(messages, stage))
            if stage == "goal_plan":
                for check in result["checks"]:
                    check.update(status="warning", strictness="advisory")
                result.update(
                    status="warning", description="Уменьшите стартовую нагрузку при усталости."
                )
            return json.dumps(result)

        client.generate_safety.side_effect = warning
        response = service.chat("продолжить")
        assert "Предупреждение — рекомендация" in response
        assert "принимаете это предупреждение" in response
        assert service.workflow.state()["stage"] == VALIDATION
        service.chat("подтверждаю план")
        assert service.workflow.state()["stage"] == EXECUTION
        assert store.latest("goals")["risk_confirmed"]


def test_danger_cannot_be_confirmed_and_safe_or_user_corrections_are_revalidated():
    with tempfile.TemporaryDirectory() as directory:
        store, client, service = prepared(directory)
        dangerous = True

        def safety(messages, stage):
            result = json.loads(safe_response(messages, stage))
            if stage == "goal_plan" and dangerous:
                for check in result["checks"]:
                    check.update(status="block", strictness="strict")
                result.update(
                    status="block",
                    description="Есть угроза тяжёлого вреда. Исключите опасную нагрузку и выберите щадящий вариант.",
                )
            return json.dumps(result)

        client.generate_safety.side_effect = safety
        configure(client, dict(PLAN, plan="PRIVATE_UNSAFE_DRAFT"))
        response = service.chat("продолжить")
        assert "Опасность" in response and "щадящий вариант" in response
        assert "PRIVATE_UNSAFE_DRAFT" not in response
        assert "свои изменения" in response
        assert service.workflow.state()["stage"] == VALIDATION
        assert "заблокировано" in service.chat("подтверждаю план")
        assert "Опасность" in service.chat("хорошо")
        assert service.workflow.state()["stage"] == VALIDATION
        dangerous = False
        seen = []

        def correction(payload):
            seen.append(copy.deepcopy(payload))
            return dict(PLAN, plan="Щадящие тренировки с отдыхом.")

        configure(client, correction)
        response = service.chat("предложи корректировки")
        assert "Щадящие" in response and "всё хорошо" in response
        assert seen[0]["state"]["assessment"]["status"] == "block"
        assert service.workflow.state()["stage"] == VALIDATION
        service.chat("подтверждаю план")
        assert service.workflow.state()["stage"] == EXECUTION
        configure(client, dict(PLAN, plan="Два занятия вместо трёх."))
        assert "Два занятия" in service.chat("Хочу два занятия вместо трёх")
        assert service.workflow.state()["stage"] == VALIDATION


def test_missing_information_and_stale_facts_do_not_get_silently_skipped():
    with tempfile.TemporaryDirectory() as directory:
        store, client, service = fixture(directory)
        service.save(profile())
        required = []

        def ask(payload):
            required.extend(payload["required_information"])
            return {
                "action": "clarify",
                "question": "Какие лекарства вы принимаете? Или напишите «нет».",
                "plan": "",
                "criterion": "",
            }

        configure(client, ask)
        response = service.chat("продолжить")
        assert "лекарства" in response
        assert any("Принимаемые лекарства" in item for item in required)
        assert service.workflow.state()["stage"] == PLANNING
        configure(client, PLAN)
        assert PLAN_ERROR == error(lambda: service.chat("продолжить"))
        assert service.workflow.state()["stage"] == PLANNING


def test_freshness_uses_existing_category_deadlines_without_expiring_allergy():
    with tempfile.TemporaryDirectory() as directory:
        store, client, service = prepared(directory)
        memory = service.memory.repository.load(NOW)
        assert not required_information(memory, service.current_profile(), NOW)
        due = required_information(memory, service.current_profile(), NOW + timedelta(days=31))
        assert any("добавки" in item for item in due)
        assert any("лекарства" in item for item in due)
        assert any("тренировок" in item for item in due)
        assert not any("арахис" in item for item in due)
        assert not any(item.startswith("Сообщите текущий вес") for item in due)
        assert not any("Нет сведений: Пищевые ограничения" in item for item in due)
        missing_weight = required_information(memory, {"weight": None}, NOW)
        assert "Сообщите текущий вес в килограммах" in missing_weight


def test_changed_data_invalidates_previous_consent_and_only_returns_to_validation():
    with tempfile.TemporaryDirectory() as directory:
        store, client, service = prepared(directory)
        service.chat("продолжить")
        fact = next(
            f
            for f in service.memory.repository.load(NOW)["facts"]
            if f["category"] == "meal_routine"
        )
        service.memory.command(f"/memory edit {fact['id']} Четыре приёма пищи")
        assert "Согласие не принято" in service.chat("подтверждаю план")
        assert service.workflow.state()["stage"] == VALIDATION
        execute_plan(service)
        service.clock = lambda: NOW + timedelta(days=31)
        before = client.generate_structured.call_count
        response = service.start_chat()
        assert "2/4" in response
        assert service.workflow.state()["stage"] == VALIDATION
        assert before == client.generate_structured.call_count


def test_every_opening_asks_progress_without_api_and_restart_restores_plan():
    with tempfile.TemporaryDirectory() as directory:
        store, client, service = prepared(directory)
        execute_plan(service)
        before = (client.generate_structured.call_count, client.generate_safety.call_count)
        reopened = FitnessService(Storage(directory), client, clock=lambda: NOW)
        for _ in range(2):
            assert "Как продвигается план" in reopened.start_chat()
        assert reopened.workflow.state()["plan"] == PLAN["plan"]
        assert reopened.workflow.state()["stage"] == EXECUTION
        assert before == (client.generate_structured.call_count, client.generate_safety.call_count)


def test_progress_uses_accepted_plan_and_correction_requires_new_confirmation():
    with tempfile.TemporaryDirectory() as directory:
        store, client, service = prepared(directory)
        execute_plan(service)
        configure(client, CONTINUE)
        assert service.chat("Всё идёт по плану") == "Ответ"
        assert service.workflow.state()["stage"] == EXECUTION
        assert PLAN["plan"] in client.generate.call_args.args[0][0]["content"]
        safety = json.loads(client.generate_safety.call_args.args[0][1]["content"])
        assert safety["goal"]["plan"] == PLAN["plan"]
        configure(client, dict(PLAN, plan="Снизить нагрузку, добавить отдых."))
        response = service.chat("Слишком устаю, нужно изменить план")
        assert "Снизить нагрузку" in response
        assert service.workflow.state()["stage"] == VALIDATION
        service.chat("подтверждаю план")
        assert service.workflow.state()["stage"] == EXECUTION


def test_goal_done_only_after_confirmation_and_archive_transition_is_atomic():
    with tempfile.TemporaryDirectory() as directory:
        store, client, service = prepared(directory)
        execute_plan(service)
        goal_id = store.latest("goals")["id"]
        turn = json.loads(EMPTY_TURN)
        turn["completion"] = "goal"
        configure(client, CONTINUE, turn)
        response = service.chat("Критерий выполнен, цель достигнута")
        assert "/confirm 1" in response
        assert service.workflow.state()["stage"] == EXECUTION
        service.chat("/confirm 1")
        assert store.latest("goals") is None
        with store.connect() as db:
            assert read_state(db, goal_id)["stage"] == DONE
            assert db.execute("SELECT COUNT(*) FROM memory_archives").fetchone()[0] == 1
        assert service.start_chat() is None


def test_completion_cannot_skip_pipeline_even_with_old_low_level_proposal():
    with tempfile.TemporaryDirectory() as directory:
        store, client, service = prepared(directory)
        turn = json.loads(EMPTY_TURN)
        turn["completion"] = "goal"
        configure(client, PLAN, turn)
        service.answer("Цель достигнута")
        error(lambda: service.chat("/confirm 1"), "только при выполнении")
        assert service.workflow.state()["stage"] == PLANNING
        assert store.latest("goals")


def test_invalid_planner_and_safety_failure_do_not_change_state_or_facts():
    with tempfile.TemporaryDirectory() as directory:
        store, client, service = prepared(directory)
        before = service.memory.repository.load(NOW)
        configure(client, "PRIVATE invalid json")
        assert error(lambda: service.chat("продолжить")) == PLAN_ERROR
        assert service.workflow.state()["stage"] == PLANNING
        assert service.memory.repository.load(NOW) == before
        configure(client)
        client.generate_safety.side_effect = OSError("PRIVATE")
        message = error(lambda: service.chat("продолжить"))
        assert "PRIVATE" not in message and "сбой проверки" in message
        assert service.workflow.state()["stage"] == PLANNING
        assert service.memory.repository.load(NOW) == before
        assert store.chat_messages()[-1]["status"] == "failed"


def test_failed_final_write_rolls_back_plan_state_with_other_turn_data():
    with tempfile.TemporaryDirectory() as directory:
        store, client, service = prepared(directory)
        state = service.workflow.state()
        before = service.memory.repository.load(NOW)
        with store.connect() as db:
            db.execute(
                "CREATE TRIGGER fail_goal_reply BEFORE INSERT ON messages WHEN NEW.role='assistant' BEGIN SELECT RAISE(ABORT,'PRIVATE'); END"
            )
        try:
            service.chat("продолжить")
        except sqlite3.IntegrityError:
            pass
        else:
            raise AssertionError("Expected atomic write failure")
        assert service.workflow.state() == state
        assert service.memory.repository.load(NOW) == before
        with store.connect() as db:
            db.execute("DROP TRIGGER fail_goal_reply")
        assert "Черновик плана" in service.chat("продолжить")


def test_concurrent_goal_state_change_cannot_be_overwritten_by_old_model_result():
    with tempfile.TemporaryDirectory() as directory:
        store, client, service = prepared(directory)

        def concurrent_open(payload):
            service.workflow.opening()
            return PLAN

        configure(client, concurrent_open)
        before = service.memory.repository.load(NOW)
        error(lambda: service.chat("продолжить"), "План изменился")
        assert service.workflow.state()["stage"] == PLANNING
        assert service.memory.repository.load(NOW) == before


def test_managed_goal_change_is_a_proposal_not_permission_to_execute_it():
    with tempfile.TemporaryDirectory() as directory:
        store, client, service = prepared(directory)
        execute_plan(service)
        turn = json.loads(EMPTY_TURN)
        turn["profile_change"] = dict(
            birth_date=None,
            height=None,
            sex=None,
            goal="Бегать 5 км",
            target=None,
            clear_target=False,
            source_quote="Новая цель — бегать 5 км",
        )
        configure(client, CONTINUE, turn)
        response = service.chat("Новая цель — бегать 5 км")
        assert "/confirm 1" in response
        assert service.workflow.state()["stage"] == VALIDATION
        service.chat("/confirm 1")
        assert store.latest("goals")["text"] == "Бегать 5 км"
        assert service.workflow.state()["stage"] == PLANNING
        assert not store.latest("goals")["risk_confirmed"]


def test_prompt_separates_critical_danger_from_bmi_and_requests_corrections():
    assert "прямая угроза жизни" in SAFETY_PROMPT
    assert "небольшого отклонения ИМТ" in SAFETY_PROMPT
    assert "конкретную безопасную корректировку" in SAFETY_PROMPT
    assert "устрани указанный риск" in PLAN_PROMPT
    assert "Известную аллергию не надо заново подтверждать" in PLAN_PROMPT


def test_goal_planner_uses_flash_and_strict_responses_schema_with_chat_override():
    client = ModelClient(
        "TEST_KEY", "https://example.test/chat", "flash", "test-session", lambda *args: None
    )
    payload = {"state": {"stage": PLANNING}, "required_information": []}
    with patch("fitagent.provider.httpx.Client") as factory:
        transport = factory.return_value.__enter__.return_value
        transport.post.return_value = responses_reply(json.dumps(PLAN))
        assert GoalPlanner(client).propose(payload) == PLAN
        sent = transport.post.call_args.kwargs["json"]
        assert transport.post.call_args.args[0] == MEMORY_URL
        assert sent["model"] == DEFAULT_MODEL == "deepseek-v4.1-flash"
        assert sent["text"]["format"]["strict"] is True
        assert sent["text"]["format"]["schema"] == PLAN_SCHEMA
        assert transport.post.call_count == 1


def test_planner_rejects_inconsistent_actions_empty_plans_and_extra_fields():
    invalid = [
        dict(PLAN, plan="   "),
        dict(PLAN, criterion=""),
        dict(PLAN, extra="PRIVATE"),
        dict(PLAN, question="Лишний вопрос"),
        CONTINUE,
        {"action": "clarify", "question": "", "plan": "", "criterion": ""},
    ]
    with tempfile.TemporaryDirectory() as directory:
        store, client, service = prepared(directory)
        before = service.memory.repository.load(NOW)
        for value in invalid:
            configure(client, value)
            assert error(lambda: service.chat("продолжить")) == PLAN_ERROR
            assert service.workflow.state()["stage"] == PLANNING
            assert service.memory.repository.load(NOW) == before


def test_reconfirming_unchanged_routine_does_not_restart_validation():
    with tempfile.TemporaryDirectory() as directory:
        store, client, service = prepared(directory)
        execute_plan(service)
        fact = next(
            f
            for f in service.memory.repository.load(NOW)["facts"]
            if f["category"] == "meal_routine"
        )
        service.clock = lambda: NOW + timedelta(days=1)
        turn = json.loads(EMPTY_TURN)
        turn["operations"] = [
            operation(
                fact["value"]["text"],
                category=fact["category"],
                key=fact["key"],
                fact_id=fact["id"],
                value=fact["value"],
            )
        ]
        configure(client, CONTINUE, turn)
        assert "Ответ" in service.chat(fact["value"]["text"])
        assert service.workflow.state()["stage"] == EXECUTION
        updated = next(
            f
            for f in service.memory.repository.load(service.clock())["facts"]
            if f["id"] == fact["id"]
        )
        assert updated["review_at"] != fact["review_at"]


def test_state_corruption_and_wrong_confirmation_are_safe_and_do_not_mutate_data():
    with tempfile.TemporaryDirectory() as directory:
        store, client, service = prepared(directory)
        state = service.workflow.state()
        error(lambda: service.workflow.confirm("хорошо"), "подтверждаю план")
        assert service.workflow.state() == state
        with store.connect() as db:
            db.execute(
                "UPDATE chat_state SET value=? WHERE key=?",
                ("PRIVATE invalid json", f"goal_flow:{state['goal_id']}"),
            )
        message = error(lambda: service.workflow.state(), "состояние цели")
        assert "PRIVATE" not in message and "JSON" not in message


def test_clear_erases_goal_pipeline_and_new_onboarding_starts_fresh():
    with tempfile.TemporaryDirectory() as directory:
        store, client, service = prepared(directory)
        execute_plan(service)
        service.clear()
        assert service.workflow.state() is None
        with store.connect() as db:
            assert not db.execute(
                "SELECT value FROM chat_state WHERE key LIKE 'goal_flow:%'"
            ).fetchall()
        assert "дату рождения" in service.start_chat()


def test_reset_assessment_preserves_plan_and_only_returns_execution_to_validation():
    for stage in TRANSITIONS:
        state = dict(
            initial_state(1),
            stage=stage,
            version=5,
            plan=PLAN["plan"],
            criterion=PLAN["criterion"],
            assessment={"status": "warning", "description": "Рекомендация"},
            fingerprint="a" * 64,
            question="Уточните данные",
        )
        expected = dict(
            state,
            stage=VALIDATION if stage == EXECUTION else stage,
            assessment=None,
            fingerprint=None,
            question="",
        )
        reset_assessment(state)
        assert state == expected


def test_saved_execution_requires_the_same_checked_fields_as_transition():
    with tempfile.TemporaryDirectory() as directory:
        store, client, service = prepared(directory)
        execute_plan(service)
        checked = service.workflow.state()
        for invalid in (
            {"plan": ""},
            {"criterion": ""},
            {"fingerprint": None},
            {"assessment": None},
            {"assessment": {"status": "block", "description": "Опасность"}},
        ):
            state = dict(checked, **invalid)
            raw = json.dumps(state, ensure_ascii=False)
            key = f"goal_flow:{state['goal_id']}"
            with store.connect() as db:
                db.execute("UPDATE chat_state SET value=? WHERE key=?", (raw, key))
            error(lambda: service.workflow.state(), "состояние цели")
            with store.connect() as db:
                assert (
                    db.execute("SELECT value FROM chat_state WHERE key=?", (key,)).fetchone()[0]
                    == raw
                )


def test_completion_proposals_use_candidate_state_without_reading_stored_state():
    with tempfile.TemporaryDirectory() as directory:
        store, client, service = fixture(directory)
        for stage in (PLANNING, VALIDATION, EXECUTION):
            state = dict(initial_state(1), stage=stage)
            snapshot = {
                "goal_managed": True,
                "completion": "goal",
                "goal_flow": (state, 0),
                "dialogue": {"topic_id": 7},
            }
            with patch.object(service.workflow, "state") as stored_state:
                service.actions.prepare(snapshot, "Цель достигнута")
                stored_state.assert_not_called()
            expected = [{"kind": "goal", "payload": {"topic_id": 7}}] if stage == EXECUTION else []
            assert snapshot["proposals"] == expected
            del snapshot["goal_flow"]
            with patch.object(service.workflow, "state", return_value=state) as stored_state:
                service.actions.prepare(snapshot, "Цель достигнута")
                stored_state.assert_called_once_with()
            assert snapshot["proposals"] == expected


def load_tests(loader, tests, pattern):
    return unittest.TestSuite(
        unittest.FunctionTestCase(value)
        for name, value in globals().items()
        if name.startswith("test_") and callable(value)
    )
