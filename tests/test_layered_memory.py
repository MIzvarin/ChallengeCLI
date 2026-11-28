"""Chat onboarding, profile changes, measurements and confirmed lifecycle regressions."""

import json
import sqlite3
import tempfile
import unittest
from datetime import timedelta

from support import EMPTY_TURN, seed_accepted_goal
from test_fitagent import NOW, fixture, profile
from test_memory import error, extract, operation

from fitagent.domain import age_on, validate_birth_date
from fitagent.memory_layers import memory_layer
from fitagent.service import FitnessService
from fitagent.storage import Storage

ANSWERS = (
    "1996-01-01",
    "180",
    "80",
    "мужской",
    "Стать сильнее",
    "нет",
    "арахис",
    "не люблю творог",
    "три раза в день",
    "креатин, доза неизвестна",
    "лекарство А, дозу не помню",
    "силовые три раза в неделю",
    "07:30",
)


def turn(client, **changes):
    value = json.loads(EMPTY_TURN)
    value.update(changes)
    client.generate_structured.return_value = json.dumps(value, ensure_ascii=False)


def test_first_start_is_resumable_stepwise_chat_and_saves_all_required_categories():
    with tempfile.TemporaryDirectory() as directory:
        store, client, service = fixture(directory)
        assert "дату рождения" in service.start_chat()
        error(lambda: service.chat("30"), "ГГГГ-ММ-ДД")
        assert not service.onboarding.state()["answers"]
        service.chat(ANSWERS[0])
        reopened = FitnessService(Storage(directory), client, clock=lambda: NOW)
        assert reopened.start_chat() is None
        assert "рост" in store.chat_messages()[-1]["content"]
        for answer in ANSWERS[1:-2]:
            reopened.chat(answer)
        client.generate.assert_not_called()
        client.generate_safety.assert_not_called()
        error(lambda: reopened.chat("да"), "Сразу опишите")
        assert "встаёте" in reopened.chat(ANSWERS[-2])
        error(lambda: reopened.chat("24:00"), "ЧЧ:ММ")
        response = reopened.chat(ANSWERS[-1])
        assert "сохранены" in response
        assert "составь программу тренировок" in response
        assert reopened.memory.repository.load(NOW)["training_program"] is None
        assert reopened.current_profile()["birth_date"] == ANSWERS[0]
        assert store.wake_time() == "07:30"
        facts = reopened.memory.repository.load(NOW)["facts"]
        assert {f["category"] for f in facts} == {
            "measurements",
            "food_restrictions",
            "food_preferences",
            "meal_routine",
            "supplements",
            "medications",
            "training_routine",
        }
        assert all(
            f["value"]["quantity"] is None
            for f in facts
            if f["category"] in {"supplements", "medications"}
        )
        assert reopened.onboarding.state() is None
        assert "1/4" in reopened.start_chat()
        with store.connect() as db:
            assert db.execute("SELECT COUNT(*) FROM fact_sources").fetchone()[0] == 7
            assert db.execute("SELECT COUNT(*) FROM memory_journal").fetchone()[0] == 7
        assert "только чтение" in reopened.profile_text()
        assert memory_layer("food_restrictions") == "safety"
        assert memory_layer("food_preferences") == "preferences"
        assert memory_layer("medications") == "medium"
        assert memory_layer("equipment") == "long"
        assert memory_layer("symptoms") == "short"


def test_onboarding_failure_rolls_back_final_profile_and_is_retryable():
    with tempfile.TemporaryDirectory() as directory:
        store, client, service = fixture(directory)
        service.start_chat()
        for answer in ANSWERS[:-1]:
            service.chat(answer)
        client.generate_safety.side_effect = OSError("PRIVATE")
        assert "PRIVATE" not in error(lambda: service.chat(ANSWERS[-1]))
        assert store.latest("profiles") is None
        assert not service.memory.repository.load(NOW)["facts"]
        assert service.onboarding.state()["answers"]["training"] == ANSWERS[-2]
        assert store.wake_time() is None


def test_birth_date_is_validated_and_age_changes_without_overwriting_birth_date():
    assert age_on("2000-11-17", NOW.date()) == 25
    assert age_on("2000-11-09", NOW.date()) == 26
    for value in ("", "30", "2000-02-30", "2015-01-01", "1900-01-01", "2000-1-01"):
        error(lambda: validate_birth_date(value, NOW.date()))
    with tempfile.TemporaryDirectory() as directory:
        store, client, service = fixture(directory)
        service.save(profile(birth_date="2000-11-10"))
        assert service.current_profile()["age"] == 25
        service.clock = lambda: NOW + timedelta(days=1)
        assert service.current_profile()["age"] == 26
        assert service.current_profile()["birth_date"] == "2000-11-10"


def test_weight_measurements_keep_dates_but_latest_report_has_priority():
    with tempfile.TemporaryDirectory() as directory:
        store, client, service = fixture(directory)
        service.save(profile())
        for index, (days, value) in enumerate(((1, 79), (-2, 82))):
            service.clock = lambda: NOW + timedelta(days=2)
            item = operation(
                f"Вес {value} кг",
                category="measurements",
                key=f"weight_measurement_{index}",
                valid_from=(NOW + timedelta(days=days)).isoformat(),
                value={
                    "text": f"Вес {value} кг",
                    "quantity": value,
                    "unit": "кг",
                    "qualifier": None,
                },
            )
            extract(client, item)
            assert "Память обновлена" in service.answer(item["source_quote"])
        assert [row["weight"] for row in store.weights()] == [82, 80, 79]
        assert service.current_profile()["weight"] == 82
        current = json.loads(service.memory.context())["measurements"]
        assert current[0]["value"]["quantity"] == 82


def test_profile_goal_change_is_proposed_checked_and_confirmed_without_fake_weight_measurement():
    with tempfile.TemporaryDirectory() as directory:
        store, client, service = fixture(directory)
        service.save(profile())
        patch = dict(
            birth_date=None,
            height=None,
            sex=None,
            goal="Бегать 5 км",
            target=None,
            clear_target=False,
            source_quote="Новая цель — бегать 5 км",
        )
        turn(client, profile_change=patch)
        response = service.chat(patch["source_quote"])
        assert "/confirm 1" in response
        assert store.latest("goals")["text"] == "Стать сильнее"
        assert len(store.weights()) == 1
        service.chat("/confirm 1")
        assert store.latest("goals")["text"] == "Бегать 5 км"
        assert len(store.weights()) == 1
        assert [call.args[1] for call in client.generate_safety.call_args_list] == [
            "goal_proposal",
            "goal_proposal",
        ]
        assert "/confirm 1" in store.chat_messages()[-3]["content"]


def test_stale_confirmation_and_failed_final_write_do_not_partially_change_profile():
    with tempfile.TemporaryDirectory() as directory:
        store, client, service = fixture(directory)
        service.save(profile())
        patch = dict(
            birth_date=None,
            height=181,
            sex=None,
            goal=None,
            target=None,
            clear_target=False,
            source_quote="Рост 181",
        )
        turn(client, profile_change=patch)
        service.answer("Рост 181")
        with store.connect() as db:
            db.execute(
                "CREATE TRIGGER fail_profile BEFORE INSERT ON profiles BEGIN SELECT RAISE(ABORT,'PRIVATE'); END"
            )
        try:
            service.chat("/confirm 1")
        except sqlite3.IntegrityError:
            pass
        else:
            raise AssertionError("Expected transactional failure")
        assert service.current_profile()["height"] == 180
        with store.connect() as db:
            assert db.execute("SELECT COUNT(*) FROM pending_actions").fetchone()[0] == 1
            db.execute("DROP TRIGGER fail_profile")
        turn(client)
        service.answer("Другой вопрос")
        error(lambda: service.chat("/confirm 1"), "данные изменились")


def test_task_completion_requires_confirmation_and_closed_memory_never_returns():
    with tempfile.TemporaryDirectory() as directory:
        store, client, service = fixture(directory)
        service.save(profile())
        seed_accepted_goal(service)
        service.answer("PRIVATE_TASK_CONTENT")
        turn(client, completion="task")
        assert "/confirm 1" in service.answer("Задача решена")
        assert service.topics.repository.snapshot()["topics"]
        service.chat("/confirm 1")
        assert not service.topics.repository.snapshot()["topics"]
        turn(client)
        service.answer("Новая задача")
        sent = str(client.generate.call_args.args[0])
        assert "PRIVATE_TASK_CONTENT" not in sent
        payload = json.loads(client.generate_structured.call_args.args[0][1]["content"])
        assert not payload["previous_exchange"]
        assert any(row["content"] == "PRIVATE_TASK_CONTENT" for row in store.chat_messages())


def test_goal_completion_archives_medium_memory_but_preserves_current_data_and_invariants():
    with tempfile.TemporaryDirectory() as directory:
        store, client, service = fixture(directory)
        service.save(profile())
        items = [
            operation("PRIVATE_PLAN", category="agreements", key="plan"),
            operation("Три приёма пищи", category="meal_routine", key="routine"),
            operation("Принимаю лекарство А", category="medications", key="medicine"),
            operation("Аллергия на орехи", category="food_restrictions", key="nuts"),
        ]
        extract(client, *items)
        service.answer("; ".join(item["source_quote"] for item in items))
        seed_accepted_goal(service)
        turn(client, completion="goal")
        service.answer("Цель достигнута")
        assert store.latest("goals") is not None
        service.chat("/confirm 1")
        assert store.latest("goals") is None
        remaining = service.memory.repository.load(NOW)["facts"]
        assert {fact["category"] for fact in remaining} == {
            "measurements",
            "meal_routine",
            "medications",
            "food_restrictions",
            "food_preferences",
            "supplements",
            "training_routine",
        }
        assert len(store.weights()) == 1
        assert "PRIVATE_PLAN" in service.memory.command("/memory archive")
        assert not store.profile_history(NOW.isoformat())
        turn(client)
        service.answer("Что дальше?")
        assert "PRIVATE_PLAN" not in str(client.generate.call_args.args[0])
        assert Storage(directory).latest("goals") is None


def test_onboarding_measurement_keeps_the_answer_date_not_the_later_save_date():
    with tempfile.TemporaryDirectory() as directory:
        store, client, service = fixture(directory)
        service.start_chat()
        for answer in ANSWERS[:-1]:
            service.chat(answer)
        service.clock = lambda: NOW + timedelta(days=3)
        service.chat(ANSWERS[-1])
        assert store.weights()[0]["confirmed_at"] == NOW.isoformat()
        assert store.latest("profiles")["confirmed_at"] == service.clock().isoformat()
        with store.connect() as db:
            weight = db.execute(
                "SELECT * FROM sticky_facts WHERE category='measurements'"
            ).fetchone()
            source = db.execute(
                "SELECT content FROM messages WHERE id=?", (weight["source_message_id"],)
            ).fetchone()
            assert source[0] == "80"
            record = db.execute(
                "SELECT * FROM memory_journal WHERE lineage_id=?", (weight["lineage_id"],)
            ).fetchone()
            assert record["recorded_at"] == service.clock().isoformat()
            assert record["effective_at"] == NOW.isoformat()


def test_onboarding_corrections_do_not_skip_questions_or_authorize_risky_plan():
    with tempfile.TemporaryDirectory() as directory:
        store, client, service = fixture(directory)
        service.start_chat()
        answers = list(ANSWERS)
        answers[5] = "50"
        for answer in answers[:-1]:
            service.chat(answer)
        assert store.latest("profiles") is None
        response = service.chat("аллергии: нет")
        assert "встаёте" in response
        assert service.onboarding.state()["answers"]["allergies"] == "нет"
        assert store.latest("profiles") is None
        service.chat(answers[-1])
        assert store.latest("profiles") is not None
        assert service.onboarding.state() is None
        assert service.workflow.state()["stage"] == "planning"
        assert not store.latest("goals")["risk_confirmed"]


def test_revoking_the_last_measurement_does_not_resurrect_the_profile_copy():
    with tempfile.TemporaryDirectory() as directory:
        store, client, service = fixture(directory)
        service.save(profile())
        measured = service.memory.repository.load(NOW)["facts"][0]
        item = operation(
            "Вес 80 кг",
            category="measurements",
            key=measured["key"],
            fact_id=measured["id"],
            action="revoke",
            source_quote="Удали ошибочное измерение",
            value=measured["value"],
        )
        extract(client, item)
        service.answer(item["source_quote"])
        assert store.weights()
        service.chat("/confirm 1")
        assert store.weights() == []
        assert service.current_profile()["weight"] is None
        assert "не указан" in service.profile_text()
        extract(client)
        service.answer("Какой мой вес?")
        sent = client.generate.call_args.args[0][0]["content"]
        assert '"weight": null' in sent


def test_corrupt_measurement_errors_are_safe_and_keep_private_payloads_out_of_output():
    with tempfile.TemporaryDirectory() as directory:
        store, client, service = fixture(directory)
        service.save(profile())
        with store.connect() as db:
            db.execute(
                "UPDATE sticky_facts SET value='PRIVATE invalid json' WHERE category='measurements'"
            )
        for callback in (store.weights, service.current_profile):
            try:
                callback()
            except RuntimeError as exc:
                assert "PRIVATE" not in str(exc)
                assert "измерения веса" in str(exc)
            else:
                raise AssertionError("Corrupt measurements must not be used")


def load_tests(loader, tests, pattern):
    return unittest.TestSuite(
        unittest.FunctionTestCase(value)
        for name, value in globals().items()
        if name.startswith("test_") and callable(value)
    )
