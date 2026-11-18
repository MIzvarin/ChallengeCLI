"""Regressions for clinical consent, completion evidence and per-window plan offers."""

import json
import tempfile
import unittest
from unittest.mock import patch

from support import EMPTY_TURN, safe_response
from test_fitagent import NOW
from test_goal_workflow import CONTINUE, PLAN, configure, execute_plan, prepared
from test_memory import error, operation

from fitagent.goal_state import DONE, EXECUTION, PLANNING, VALIDATION, read_state
from fitagent.service import FitnessService
from fitagent.storage import Storage


def completion_turn(kind="goal", weight=None):
    turn = json.loads(EMPTY_TURN)
    turn["completion"] = kind
    if weight is not None:
        turn["operations"] = [
            operation(
                f"Сегодня вес {weight} кг, цель достигнута",
                category="measurements",
                key="weight_measurement_achievement",
                value={
                    "text": f"Вес {weight} кг",
                    "quantity": weight,
                    "unit": "кг",
                    "qualifier": None,
                },
            )
        ]
    return turn


def pending_goal(directory):
    store, client, service = prepared(directory, target=75)
    configure(client, dict(PLAN, criterion="Достичь веса 75 кг"))
    execute_plan(service)
    configure(client, "Планировщик не должен вызываться", completion_turn(weight=75))
    response = service.chat("Сегодня вес 75 кг, цель достигнута")
    assert "/confirm 1" in response
    return store, client, service


def test_local_clinical_changes_invalidate_consent_for_global_and_topic_facts():
    for category in ("symptoms", "recovery"):
        for applicability in ("global", "topic"):
            with tempfile.TemporaryDirectory() as directory:
                store, client, service = prepared(directory)
                turn = json.loads(EMPTY_TURN)
                turn["operations"] = [
                    operation(
                        "Самочувствие хорошее",
                        category=category,
                        key="wellbeing",
                        applicability=applicability,
                    )
                ]
                configure(client, PLAN, turn)
                service.chat("Самочувствие хорошее")
                assert service.workflow.state()["stage"] == VALIDATION
                fact = next(
                    f
                    for f in service.memory.repository.load(NOW)["facts"]
                    if f["category"] == category
                )
                service.memory.command(
                    f"/memory edit {fact['id']} Острая боль в груди при нагрузке"
                )
                response = service.chat("подтверждаю план")
                assert "Согласие не принято" in response
                state = service.workflow.state()
                assert state["stage"] == VALIDATION and state["assessment"] is None
                assert not store.latest("goals")["risk_confirmed"]


def test_opening_does_not_redisplay_stale_clinical_assessment_as_checked():
    with tempfile.TemporaryDirectory() as directory:
        store, client, service = prepared(directory)
        turn = json.loads(EMPTY_TURN)
        turn["operations"] = [operation("Восстановился", category="recovery", key="rest")]
        configure(client, PLAN, turn)
        service.chat("Восстановился")
        service.chat("подтверждаю план")
        fact = next(
            f for f in service.memory.repository.load(NOW)["facts"] if f["category"] == "recovery"
        )
        service.memory.command(f"/memory edit {fact['id']} Острая боль при нагрузке")
        calls = client.generate_safety.call_count
        response = service.start_chat()
        assert service.workflow.state()["stage"] == VALIDATION
        assert "всё хорошо" not in response and "Нужна проверка" in response
        assert client.generate_safety.call_count == calls


def test_achievement_weight_proposes_completion_without_a_new_plan():
    with tempfile.TemporaryDirectory() as directory:
        store, client, service = pending_goal(directory)
        goal_id = store.latest("goals")["id"]
        assert service.current_profile()["weight"] == 75
        assert [row["weight"] for row in store.weights()] == [80, 75]
        assert service.workflow.state()["stage"] == EXECUTION
        assert service.workflow.state()["plan"] == PLAN["plan"]
        assert not any(
            call.kwargs.get("purpose") == "goal_planning"
            for call in client.generate_structured.call_args_list[-1:]
        )
        calls = (client.generate_structured.call_count, client.generate_safety.call_count)
        response = service.chat("/confirm 1")
        assert "4/4" in response
        assert store.latest("goals") is None
        with store.connect() as db:
            assert read_state(db, goal_id)["stage"] == DONE
            assert db.execute("SELECT COUNT(*) FROM memory_archives").fetchone()[0] == 1
        assert calls == (client.generate_structured.call_count, client.generate_safety.call_count)
        assert service.current_profile()["weight"] == 75


def test_achievement_confirmation_survives_restart_and_read_only_openings():
    with tempfile.TemporaryDirectory() as directory:
        store, client, service = pending_goal(directory)
        reopened = FitnessService(Storage(directory), client, clock=lambda: NOW)
        calls = client.generate_structured.call_count
        for _ in range(2):
            assert "/confirm 1" in reopened.start_chat()
            assert reopened.workflow.state()["stage"] == EXECUTION
        error(lambda: reopened.chat("подтверждаю план"), "/confirm 1")
        assert reopened.workflow.state()["stage"] == EXECUTION
        reopened.chat("/confirm 1")
        assert store.latest("goals") is None
        assert client.generate_structured.call_count == calls


def test_cancelling_achievement_does_not_authorize_plan_against_changed_weight():
    with tempfile.TemporaryDirectory() as directory:
        store, client, service = pending_goal(directory)
        service.chat("/cancel 1")
        state = service.workflow.state()
        assert state["stage"] == VALIDATION and state["assessment"] is None
        assert service.current_profile()["weight"] == 75
        with store.connect() as db:
            assert not db.execute("SELECT id FROM pending_actions").fetchall()
        assert "Согласие не принято" in service.chat("подтверждаю план")


def test_changes_after_achievement_proposal_still_reject_old_completion():
    with tempfile.TemporaryDirectory() as directory:
        store, client, service = pending_goal(directory)
        fact = next(
            f
            for f in service.memory.repository.load(NOW)["facts"]
            if f["category"] == "training_routine"
        )
        service.memory.command(f"/memory edit {fact['id']} Другая программа тренировок")
        error(lambda: service.chat("/confirm 1"), "данные изменились")
        assert store.latest("goals") is not None
        assert "2/4" in service.start_chat()
        assert service.workflow.state()["stage"] == VALIDATION


def test_completion_intent_cannot_skip_planning_or_validation():
    with tempfile.TemporaryDirectory() as directory:
        store, client, service = prepared(directory)
        configure(client, PLAN, completion_turn())
        response = service.chat("Цель достигнута")
        assert service.workflow.state()["stage"] == VALIDATION
        assert "/confirm" not in response
        response = service.chat("Цель уже достигнута")
        assert service.workflow.state()["stage"] == VALIDATION
        assert "/confirm" not in response
        with store.connect() as db:
            assert not db.execute("SELECT id FROM pending_actions").fetchall()


def test_independent_task_completion_remains_available_after_goal_done():
    with tempfile.TemporaryDirectory() as directory:
        store, client, service = prepared(directory)
        execute_plan(service)
        configure(client, CONTINUE, completion_turn())
        service.chat("Цель достигнута")
        service.chat("/confirm 1")
        configure(client, CONTINUE, completion_turn("task"))
        response = service.chat("Меню обсудили, эту задачу можно завершить")
        assert "/confirm 2" in response
        assert service.topics.repository.snapshot()["topics"]
        service.chat("/confirm 2")
        assert not service.topics.repository.snapshot()["topics"]
        assert store.latest("goals") is None


def test_independent_task_completion_does_not_require_executing_goal():
    with tempfile.TemporaryDirectory() as directory:
        store, client, service = prepared(directory)
        assert service.workflow.state()["stage"] == PLANNING
        configure(client, PLAN, completion_turn("task"))
        response = service.chat("Обсуждение меню закончено")
        assert "/confirm 1" in response
        assert service.workflow.state()["stage"] == VALIDATION
        service.chat("/confirm 1")
        assert not service.topics.repository.snapshot()["topics"]
        assert service.workflow.state()["stage"] == VALIDATION


def test_other_window_plan_criterion_or_warning_requires_redisplay_before_consent():
    for changed in ("plan", "criterion", "warning"):
        with tempfile.TemporaryDirectory() as directory:
            store, client, first = prepared(directory)
            shown = first.chat("продолжить")
            second = FitnessService(Storage(directory), client, clock=lambda: NOW)
            revised = dict(PLAN)
            if changed != "warning":
                revised[changed] = "Новое содержание, которого первое окно не показывало"

            def safety(messages, stage):
                value = json.loads(safe_response(messages, stage))
                if changed == "warning" and stage == "goal_plan":
                    value.update(
                        status="warning",
                        description="Новое предупреждение, которого первое окно не показывало",
                    )
                    value["checks"][0].update(status="warning", strictness="advisory")
                return json.dumps(value)

            client.generate_safety.side_effect = safety
            configure(client, revised)
            second.chat("Скорректируй план")
            response = first.chat("подтверждаю план")
            assert "Согласие не принято" in response
            assert "Новое" not in shown and "Новое" in response
            assert first.workflow.state()["stage"] == VALIDATION
            first.chat("подтверждаю план")
            assert first.workflow.state()["stage"] == EXECUTION
            assert bool(store.latest("goals")["risk_confirmed"]) == (changed == "warning")


def test_read_only_opening_elsewhere_does_not_invalidate_an_unchanged_offer():
    with tempfile.TemporaryDirectory() as directory:
        store, client, first = prepared(directory)
        first.chat("продолжить")
        second = FitnessService(Storage(directory), client, clock=lambda: NOW)
        assert "Черновик плана" in second.start_chat()
        assert "Начинаем выполнение" in first.chat("подтверждаю план")


def test_new_instance_cannot_accept_unseen_plan_without_showing_it_first():
    with tempfile.TemporaryDirectory() as directory:
        store, client, first = prepared(directory)
        first.chat("продолжить")
        second = FitnessService(Storage(directory), client, clock=lambda: NOW)
        response = second.chat("подтверждаю план")
        assert "Согласие не принято" in response and PLAN["plan"] in response
        assert second.workflow.state()["stage"] == VALIDATION
        assert "Начинаем выполнение" in second.chat("подтверждаю план")


def test_failed_opening_does_not_mark_new_plan_as_shown_in_that_window():
    with tempfile.TemporaryDirectory() as directory:
        store, client, first = prepared(directory)
        first.chat("продолжить")
        second = FitnessService(Storage(directory), client, clock=lambda: NOW)
        revised = dict(PLAN, plan="Другой план")
        configure(client, revised)
        second.chat("Измени план")
        with patch.object(first.store, "message", side_effect=OSError("PRIVATE")):
            try:
                first.start_chat()
            except OSError:
                pass
            else:
                raise AssertionError("Expected failed message persistence")
        response = first.chat("подтверждаю план")
        assert "Согласие не принято" in response and "Другой план" in response
        assert first.workflow.state()["stage"] == VALIDATION


def test_legacy_completion_proposal_requires_safe_renewal_with_clear_instructions():
    with tempfile.TemporaryDirectory() as directory:
        store, client, service = pending_goal(directory)
        with store.connect() as db:
            row = db.execute("SELECT payload FROM pending_actions WHERE id=1").fetchone()
            payload = json.loads(row[0])
            db.execute(
                "UPDATE pending_actions SET payload=? WHERE id=1",
                (json.dumps({"topic_id": payload["topic_id"]}),),
            )
        error(lambda: service.chat("/confirm 1"), "Сообщите о достижении цели ещё раз")
        assert store.latest("goals") is not None
        response = service.chat("Сегодня вес 75 кг, цель достигнута")
        assert "/confirm 2" in response
        service.chat("/confirm 2")
        assert store.latest("goals") is None


def load_tests(loader, tests, pattern):
    return unittest.TestSuite(
        unittest.FunctionTestCase(value)
        for name, value in globals().items()
        if name.startswith("test_") and callable(value)
    )
