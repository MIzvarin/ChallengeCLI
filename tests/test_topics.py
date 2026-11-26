"""Automatic topic branches, scoped facts and checkpoints; mocked API only."""

import asyncio
import copy
import json
import sqlite3
import tempfile
import unittest
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import Mock, patch

import httpx
from support import safety_json
from test_memory import (
    DEFAULT_TOPIC,
    NOW,
    error,
    facts,
    fixture,
    operation,
    real_client,
    responses_reply,
)
from textual.widgets import Input, RichLog

from fitagent.memory_schema import MEMORY_ERROR
from fitagent.service import FitnessService
from fitagent.storage import Storage
from fitagent.topic_schema import TURN_SCHEMA
from fitagent.ui import FitAgentApp


def route(
    client,
    action="continue",
    topic_id=None,
    title="",
    related=(),
    operations=(),
    clarification=None,
):
    client.generate_structured.return_value = json.dumps(
        {
            "operations": operations,
            "profile_change": None,
            "completion": "none",
            "topic": {
                "action": action,
                "topic_id": topic_id,
                "title": title,
                "related_topic_ids": list(related),
                "clarification": clarification,
            },
        },
        ensure_ascii=False,
    )


def active(service):
    return service.topics.repository.snapshot()["active_topic_id"]


def sent(client):
    return client.generate.call_args.args[0][1]["content"]


def checkpoint_reply(client, text):
    client.generate.side_effect = lambda messages, limit, purpose: (
        text if purpose == "checkpoint" else "Ответ"
    )


def context_state(client):
    return json.loads(
        sent(client)
        .split("ТЕМА И CHECKPOINT (JSON):\n", 1)[1]
        .split("\nИСТОРИЯ АКТИВНОЙ ТЕМЫ", 1)[0]
    )


def test_topic_choice_shares_the_single_strict_extraction_request():
    with tempfile.TemporaryDirectory() as directory:
        store, client, service = fixture(directory)
        route(client, title="Меню")
        service.answer("MENU_PRIVATE_QUESTION")
        first = active(service)
        route(client, action="switch", title="Тренировки")
        checkpoint_reply(client, "MENU_CHECKPOINT")
        service.answer("TRAINING_PRIVATE_QUESTION")
        assert active(service) != first
        assert client.generate_structured.call_count == 2
        assert [call.args[2] for call in client.generate.call_args_list] == [
            "answer",
            "checkpoint",
            "answer",
        ]
        analysis_messages, schema = client.generate_structured.call_args.args
        assert schema is TURN_SCHEMA and set(schema["required"]) == {
            "operations",
            "topic",
            "profile_change",
            "completion",
        }
        assert schema["additionalProperties"] is False
        payload = json.loads(analysis_messages[1]["content"])
        assert payload["dialogue"]["active_topic_id"] == first
        assert payload["dialogue"]["topics"][0]["title"] == "Меню"
        assert "не скоростью сообщений" in analysis_messages[0]["content"]
        assert "MENU_PRIVATE_QUESTION" not in sent(client)
        assert "MENU_CHECKPOINT" not in sent(client)
        assert "PRIVATE_QUESTION" not in store.logs() and "MENU_CHECKPOINT" not in store.logs()


def test_switch_resume_isolate_histories_and_restore_the_correct_checkpoint():
    with tempfile.TemporaryDirectory() as directory:
        store, client, service = fixture(directory)
        route(client, title="Меню")
        service.answer("MENU_ORIGINAL")
        menu = active(service)
        checkpoint_reply(client, "CHECKPOINT_MENU")
        route(client, action="switch", title="Тренировки")
        service.answer("TRAINING_ORIGINAL")
        training = active(service)
        menu_cp = service.topics.repository.checkpoint(menu)
        assert menu_cp["first_message_id"] == 1 and menu_cp["covered_message_id"] == 2
        assert menu_cp["message_count"] == 2
        checkpoint_reply(client, "CHECKPOINT_TRAINING")
        route(client, action="resume", topic_id=menu)
        service.answer("Вернёмся к меню")
        assert active(service) == menu
        assert "CHECKPOINT_MENU" in sent(client)
        assert "TRAINING_ORIGINAL" not in sent(client) and "CHECKPOINT_TRAINING" not in sent(client)
        assert context_state(client)["related_checkpoints"] == []
        assert service.topics.repository.checkpoint(training)["covered_message_id"] == 4
        assert [row["content"] for row in store.chat_messages()] == [
            "MENU_ORIGINAL",
            "Ответ",
            "TRAINING_ORIGINAL",
            "Ответ",
            "Вернёмся к меню",
            "Ответ",
        ]


def test_continue_short_followup_has_no_checkpoint_or_extra_branch():
    with tempfile.TemporaryDirectory() as directory:
        store, client, service = fixture(directory)
        route(client, title="Силовой план")
        service.answer("Обсудим силовой план")
        previous = active(service)
        route(client)
        service.answer("А сколько подходов?")
        assert active(service) == previous
        assert len(service.topics.repository.snapshot()["topics"]) == 1
        assert service.topics.repository.checkpoint(previous) is None
        assert [call.args[2] for call in client.generate.call_args_list] == ["answer", "answer"]
        assert "Обсудим силовой план" in sent(client)
        assert service.topics.repository.notice() is None


def test_global_and_topic_facts_have_independent_applicability_and_source_topic():
    with tempfile.TemporaryDirectory() as directory:
        store, client, service = fixture(directory)
        allergy = operation("ALLERGY_NUTS", category="food_restrictions", key="nuts")
        menu_budget = operation(
            "MENU_BUDGET", category="cooking_budget", key="budget", applicability="topic"
        )
        route(client, title="Меню", operations=[allergy, menu_budget])
        service.answer("ALLERGY_NUTS MENU_BUDGET")
        menu = active(service)
        saved = facts(service)
        assert all(fact["source_topic_id"] == menu for fact in saved)
        assert saved[0]["applicability"] == "global" and saved[0]["topic_id"] is None
        assert saved[1]["applicability"] == "topic" and saved[1]["topic_id"] == menu
        view = service.memory.command("/memory")
        assert "Применение: общее" in view and "тема-источник: «Меню»" in view
        assert "Применение: «Меню»" in view
        route(
            client,
            action="switch",
            title="План тренировок",
            operations=[
                operation(
                    "TRAINING_FREQUENCY",
                    category="training_routine",
                    key="frequency",
                    applicability="topic",
                ),
            ],
        )
        checkpoint_reply(client, "Сохранённое обсуждение меню")
        service.answer("TRAINING_FREQUENCY")
        training = active(service)
        data = json.loads(service.memory.context(topic_id=training))["facts"]
        assert {fact["value"]["text"] for fact in data} == {"ALLERGY_NUTS", "TRAINING_FREQUENCY"}
        assert "MENU_BUDGET" not in sent(client)
        assert facts(service)[-1]["source_topic_id"] == training
        with store.connect() as db:
            assert [
                row[0]
                for row in db.execute(
                    "SELECT source_topic_id FROM fact_sources ORDER BY message_id,fact_id"
                )
            ] == [menu, menu, training]


def test_identical_category_key_scope_can_coexist_in_two_plans_and_globally():
    with tempfile.TemporaryDirectory() as directory:
        store, client, service = fixture(directory)
        first = operation(
            "PLAN_A_FREQUENCY", category="training_routine", key="frequency", applicability="topic"
        )
        general = operation("GENERAL_FREQUENCY", category="training_routine", key="frequency")
        route(client, title="Первый план", operations=[first, general])
        service.answer("PLAN_A_FREQUENCY GENERAL_FREQUENCY")
        a = active(service)
        second = operation(
            "PLAN_B_FREQUENCY", category="training_routine", key="frequency", applicability="topic"
        )
        route(client, action="switch", title="Второй план", operations=[second])
        checkpoint_reply(client, "Первый план сохранён")
        service.answer("PLAN_B_FREQUENCY")
        b = active(service)
        assert len(facts(service)) == 3
        assert {
            f["value"]["text"] for f in json.loads(service.memory.context(topic_id=a))["facts"]
        } == {"PLAN_A_FREQUENCY", "GENERAL_FREQUENCY"}
        assert {
            f["value"]["text"] for f in json.loads(service.memory.context(topic_id=b))["facts"]
        } == {"PLAN_B_FREQUENCY", "GENERAL_FREQUENCY"}


def test_scope_correction_keeps_lineage_and_delete_removes_all_its_versions():
    with tempfile.TemporaryDirectory() as directory:
        store, client, service = fixture(directory)
        route(
            client,
            title="Меню",
            operations=[operation("5000 на продукты", category="cooking_budget", key="budget")],
        )
        service.answer("5000 на продукты")
        original = facts(service)[0]
        route(
            client,
            operations=[
                operation(
                    "5000 на продукты",
                    source_quote="Только для этого меню",
                    category="cooking_budget",
                    key="budget",
                    fact_id=original["id"],
                    applicability="topic",
                )
            ],
        )
        service.answer("Только для этого меню")
        current = facts(service)[0]
        assert current["id"] == original["id"] and current["lineage_id"] == original["lineage_id"]
        assert current["applicability"] == "topic" and current["topic_id"] == active(service)
        assert not json.loads(service.memory.context(topic_id=999))["facts"]
        service.memory.command(f"/memory delete {current['id']}")
        with store.connect() as db:
            assert (
                db.execute(
                    "SELECT COUNT(*) FROM sticky_facts WHERE category!='measurements'"
                ).fetchone()[0]
                == 0
            )
            assert db.execute("SELECT COUNT(*) FROM fact_sources").fetchone()[0] == 0


def test_scoped_clarifications_do_not_leak_into_another_topic():
    with tempfile.TemporaryDirectory() as directory:
        store, client, service = fixture(directory)
        route(client, title="Меню", operations=[operation("MENU_LOCAL", applicability="topic")])
        service.answer("MENU_LOCAL")
        menu = active(service)
        fact = facts(service)[0]
        route(
            client,
            operations=[
                operation(
                    "MENU_LOCAL",
                    action="clarify",
                    fact_id=fact["id"],
                    applicability="topic",
                    topic_id=menu,
                    source_quote="Иногда",
                    clarification="MENU_ONLY_QUESTION",
                )
            ],
        )
        service.answer("Иногда")
        route(client, action="switch", title="Тренировки")
        checkpoint_reply(client, "Краткая сводка меню")
        service.answer("Обсудим тренировки")
        assert "MENU_ONLY_QUESTION" not in service.memory.context(topic_id=active(service))
        assert "MENU_ONLY_QUESTION" in service.memory.context(topic_id=menu)


def test_medical_constraints_cannot_be_limited_to_one_topic():
    with tempfile.TemporaryDirectory() as directory:
        store, client, service = fixture(directory)
        for category in ("food_restrictions", "health_constraints"):
            route(
                client,
                title="Меню",
                operations=[operation("Ограничение", category=category, applicability="topic")],
            )
            assert error(lambda: service.answer("Ограничение")) == MEMORY_ERROR
            assert not facts(service) and active(service) is None
        client.generate.assert_not_called()


def test_detour_creates_child_branch_and_resume_returns_to_parent_without_timer():
    with tempfile.TemporaryDirectory() as directory:
        store, client, service = fixture(directory)
        route(client, title="Меню")
        service.answer("Основное меню")
        menu = active(service)
        route(client, action="detour", title="Разминка")
        checkpoint_reply(client, "MENU_CHECKPOINT")
        service.answer("Кстати, как размяться?")
        detour = active(service)
        assert service.topics.repository.snapshot()["topics"][-1]["parent_topic_id"] == menu
        route(client)
        service.clock = lambda: NOW + timedelta(hours=1)
        service.answer("А для плеч?")
        assert active(service) == detour
        route(client, action="resume", topic_id=menu)
        service.answer("Вернёмся к меню")
        assert active(service) == menu and "MENU_CHECKPOINT" in sent(client)


def test_mixed_question_uses_only_selected_related_checkpoints_and_scoped_facts():
    with tempfile.TemporaryDirectory() as directory:
        store, client, service = fixture(directory)
        route(client, title="Меню", operations=[operation("MENU_LOCAL", applicability="topic")])
        service.answer("MENU_LOCAL")
        menu = active(service)
        route(client, action="switch", title="Тренировки")
        checkpoint_reply(client, "MENU_CHECKPOINT")
        service.answer("TRAINING_ORIGINAL")
        training = active(service)
        route(client, action="switch", title="Сон")
        checkpoint_reply(client, "TRAINING_CHECKPOINT")
        service.answer("SLEEP_ORIGINAL")
        sleep = active(service)
        route(client, action="mixed", topic_id=training, related=[menu])
        checkpoint_reply(client, "SLEEP_CHECKPOINT")
        service.answer("Как совместить меню и тренировки?")
        state = context_state(client)
        assert active(service) == training
        assert [cp["topic_id"] for cp in state["related_checkpoints"]] == [menu]
        assert "MENU_LOCAL" in sent(client) and "MENU_CHECKPOINT" in sent(client)
        assert "SLEEP_CHECKPOINT" not in sent(client) and "SLEEP_ORIGINAL" not in sent(client)
        assert service.topics.repository.checkpoint(sleep)


def test_uncertain_topic_continues_and_passes_clarification_without_manual_switch():
    with tempfile.TemporaryDirectory() as directory:
        store, client, service = fixture(directory)
        route(client, title="Меню")
        service.answer("Меню")
        topic = active(service)
        route(client, clarification="Вы спрашиваете о меню или тренировке?")
        service.answer("А завтра?")
        assert active(service) == topic and len(service.topics.repository.snapshot()["topics"]) == 1
        assert context_state(client)["clarification"] == "Вы спрашиваете о меню или тренировке?"


def test_invalid_routes_reject_the_whole_turn_before_any_checkpoint_or_answer():
    with tempfile.TemporaryDirectory() as directory:
        store, client, service = fixture(directory)
        route(client, title="Меню")
        service.answer("Меню")
        a = active(service)
        route(client, action="switch", title="Тренировки")
        service.answer("Тренировки")
        b = active(service)
        before = service.topics.repository.snapshot()
        cases = [
            {"action": "continue", "topic_id": a},
            {"action": "resume", "topic_id": b},
            {"action": "resume", "topic_id": 999},
            {"action": "switch", "topic_id": a, "title": "Тема"},
            {"action": "switch", "title": " "},
            {"action": "detour", "topic_id": b},
            {"action": "mixed", "topic_id": a, "related_topic_ids": [999]},
            {"action": "mixed", "topic_id": a, "related_topic_ids": [b, b]},
            {"action": "continue", "related_topic_ids": [a]},
            {"topic_id": True},
            {"action": "switch", "title": "Новая", "clarification": "Вопрос?"},
            {"action": "continue", "unexpected": "SECRET"},
        ]
        client.generate.reset_mock()
        for change in cases:
            client.generate_structured.return_value = json.dumps(
                {"operations": [], "topic": DEFAULT_TOPIC | change}
            )
            assert "SECRET" not in error(lambda: service.answer("Вопрос"))
            assert service.topics.repository.snapshot() == before
        client.generate.assert_not_called()
        assert "SECRET" not in store.logs()


def test_invalid_fact_topic_reference_does_not_commit_valid_routing_decision():
    with tempfile.TemporaryDirectory() as directory:
        store, client, service = fixture(directory)
        for change in ({"applicability": "topic", "topic_id": 999}, {"topic_id": 1}):
            route(client, action="switch", title="Меню", operations=[operation(**change)])
            error(lambda: service.answer("Не люблю брокколи"))
            assert not facts(service) and active(service) is None


def test_checkpoint_failure_answer_failure_and_transaction_failure_are_atomic():
    with tempfile.TemporaryDirectory() as directory:
        store, client, service = fixture(directory)
        route(client, title="Меню", operations=[operation()])
        service.answer("Не люблю брокколи")
        before_topics, before_facts = (
            service.topics.repository.snapshot(),
            copy.deepcopy(facts(service)),
        )
        route(
            client,
            action="switch",
            title="Тренировки",
            operations=[operation("Есть гантели", category="equipment", key="dumbbells")],
        )
        client.generate.side_effect = OSError("SECRET")
        try:
            service.answer("Есть гантели")
        except (OSError, ValueError):
            pass
        else:
            raise AssertionError("Expected checkpoint failure")
        assert (
            service.topics.repository.snapshot() == before_topics and facts(service) == before_facts
        )
        assert service.topics.repository.checkpoint(active(service)) is None

        def fail_answer(messages, limit, purpose):
            if purpose == "answer":
                raise ValueError("Не удалось получить ответ.")
            return "MENU_CHECKPOINT"

        client.generate.side_effect = fail_answer
        error(lambda: service.answer("Есть гантели"))
        assert (
            service.topics.repository.snapshot() == before_topics and facts(service) == before_facts
        )
        assert service.topics.repository.checkpoint(active(service)) is None
        client.generate.side_effect = None
        with store.connect() as db:
            db.execute("""CREATE TRIGGER fail_topic BEFORE INSERT ON topic_transitions
                          BEGIN SELECT RAISE(ABORT,'SECRET'); END""")
        try:
            service.answer("Есть гантели")
        except sqlite3.IntegrityError:
            pass
        else:
            raise AssertionError("Expected transaction rollback")
        assert (
            service.topics.repository.snapshot() == before_topics and facts(service) == before_facts
        )
        assert service.topics.repository.checkpoint(active(service)) is None
        assert "SECRET" not in store.logs()
        assert len([row for row in store.chat_messages() if row["role"] == "assistant"]) == 1


def test_checkpoint_does_not_promote_model_advice_to_sticky_facts():
    with tempfile.TemporaryDirectory() as directory:
        store, client, service = fixture(directory)
        route(client, title="Меню")
        client.generate.return_value = "Совет ассистента: есть брокколи"
        service.answer("Что приготовить?")
        route(client, action="switch", title="Тренировки")
        checkpoint_reply(client, "Рекомендация, не решение: есть брокколи")
        service.answer("Как тренироваться?")
        assert not facts(service)
        call = next(call for call in client.generate.call_args_list if call.args[2] == "checkpoint")
        assert "явно принятые пользователем решения" in call.args[0][0]["content"]
        assert "не подтверждённые действия" in call.args[0][0]["content"]
        assert "Совет ассистента" in call.args[0][1]["content"]


def test_restart_and_old_checkpoint_restore_topic_with_current_profile():
    with tempfile.TemporaryDirectory() as directory:
        store, client, service = fixture(directory)
        route(client, title="Меню")
        service.answer("MENU_ORIGINAL")
        menu = active(service)
        route(client, action="switch", title="Тренировки")
        checkpoint_reply(client, "OLD_MENU_CHECKPOINT")
        service.answer("TRAINING_ORIGINAL")
        store = Storage(directory)
        reopened = FitnessService(store, client, clock=lambda: NOW + timedelta(days=60))
        client.generate.reset_mock()
        client.generate_structured.reset_mock()
        assert active(reopened) != menu
        client.generate.assert_not_called()
        client.generate_structured.assert_not_called()
        profile = {
            "height": 180,
            "weight": 79,
            "sex": "мужской",
            "birth_date": "1996-01-01",
            "goal": "NEW_GOAL",
            "target": None,
        }
        reopened.save(profile)
        route(client, action="resume", topic_id=menu)
        checkpoint_reply(client, "TRAINING_CHECKPOINT")
        reopened.answer("Вернёмся к меню")
        state = context_state(client)
        assert state["checkpoint"]["content"] == "OLD_MENU_CHECKPOINT"
        assert state["checkpoint"]["profile_changed"] is True
        assert "NEW_GOAL" in client.generate.call_args.args[0][0]["content"]
        assert "TRAINING_ORIGINAL" not in sent(client)


def test_large_inactive_topic_is_excluded_before_history_compaction():
    with tempfile.TemporaryDirectory() as directory:
        store, client, service = fixture(directory)
        route(client, title="Большая тема")
        service.answer("OLD_TOPIC")
        old = active(service)
        for _ in range(10):
            number = store.message("user", "INACTIVE_LARGE " * 3000, NOW)
            with store.connect() as db:
                db.execute("INSERT INTO topic_messages VALUES (?,?)", (number, old))
        route(client, action="switch", title="Короткая тема")
        checkpoint_reply(client, "OLD_CHECKPOINT")
        service.answer("NEW_SHORT_TOPIC")
        assert "INACTIVE_LARGE" not in sent(client)
        assert not any(call.args[2] == "summarization" for call in client.generate.call_args_list)
        assert sum(call.args[2] == "checkpoint" for call in client.generate.call_args_list) > 1
        assert service.topics.repository.checkpoint(old)["message_count"] == 12


def test_clear_removes_topics_checkpoints_links_and_restores_no_old_context():
    with tempfile.TemporaryDirectory() as directory:
        store, client, service = fixture(directory)
        route(client, title="Меню")
        service.answer("OLD_PRIVATE")
        route(client, action="switch", title="Тренировки")
        service.answer("OLD_TRAINING")
        service.clear()
        for table in ("topics", "topic_messages", "topic_transitions", "checkpoints"):
            with store.connect() as db:
                assert db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] == 0
        assert service.topics.repository.snapshot()["active_topic_id"] is None
        assert service.topics.repository.notice() is None


def test_legacy_fact_migration_preserves_values_sources_and_clarifications():
    with tempfile.TemporaryDirectory() as directory:
        value = json.dumps(operation()["value"])
        with sqlite3.connect(directory + "/fitagent.sqlite3") as db:
            db.executescript("""
                CREATE TABLE sticky_facts (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,category TEXT NOT NULL,fact_key TEXT NOT NULL,
                    scope TEXT NOT NULL,value TEXT NOT NULL,valid_from TEXT NOT NULL,valid_until TEXT,
                    review_at TEXT,created_at TEXT NOT NULL,updated_at TEXT NOT NULL,
                    source_message_id INTEGER,origin TEXT NOT NULL,status TEXT NOT NULL DEFAULT 'active'
                );
                CREATE UNIQUE INDEX sticky_fact_identity ON sticky_facts(category,fact_key,scope) WHERE status='active';
                CREATE TABLE fact_sources (fact_id INTEGER,message_id INTEGER,quote TEXT,PRIMARY KEY(fact_id,message_id));
                CREATE TABLE memory_issues (category TEXT,fact_key TEXT,scope TEXT,question TEXT,source_message_id INTEGER,created_at TEXT,
                    PRIMARY KEY(category,fact_key,scope));
            """)
            db.execute(
                "INSERT INTO sticky_facts VALUES (1,'food_preferences','broccoli','usual',?,?,NULL,NULL,?,?,1,'user','active')",
                (value, NOW.isoformat(), NOW.isoformat(), NOW.isoformat()),
            )
            db.execute("INSERT INTO fact_sources VALUES (1,1,'PRIVATE_QUOTE')")
            db.execute(
                "INSERT INTO memory_issues VALUES ('food_preferences','broccoli','usual','PRIVATE_QUESTION',1,?)",
                (NOW.isoformat(),),
            )
        store = Storage(directory)
        client = SimpleNamespace(generate=Mock(), generate_structured=Mock())
        service = FitnessService(store, client, clock=lambda: NOW)
        data = facts(service)[0]
        assert data["value"] == operation()["value"] and data["applicability"] == "global"
        assert data["source_topic_id"] is None and data["lineage_id"] == 1
        assert "PRIVATE_QUESTION" in service.memory.context()
        with store.connect() as db:
            assert db.execute("SELECT quote FROM fact_sources").fetchone()[0] == "PRIVATE_QUOTE"
        assert Storage(directory).chat_messages() == []
        client.generate.assert_not_called()
        client.generate_structured.assert_not_called()


async def topic_ui_flow():
    from support import allow_goal_continue, seed_accepted_goal

    with tempfile.TemporaryDirectory() as directory:
        store, client, service = fixture(directory)
        seed_accepted_goal(service)
        allow_goal_continue(client)
        route(client, title="Меню")
        service.answer("Меню")
        menu = active(service)
        route(client, action="switch", title="Тренировки [red]")
        app = FitAgentApp(service)
        async with app.run_test(size=(80, 24)) as pilot:
            await pilot.pause()
            app.query_one("#question", Input).value = "Тренировки"
            await pilot.press("enter")
            await app.workers.wait_for_complete()
            text = "\n".join(line.text for line in app.query_one("#chat", RichLog).lines)
            assert "Новая тема: Тренировки [red]" in text
            route(client, action="resume", topic_id=menu)
            app.query_one("#question", Input).value = "Вернёмся к меню"
            await pilot.press("enter")
            await app.workers.wait_for_complete()
            text = "\n".join(line.text for line in app.query_one("#chat", RichLog).lines)
            assert "Возвращаемся к теме: Меню" in text
            app.query_one("#question", Input).value = "/resume 1"
            await pilot.press("enter")
            await pilot.pause()
            assert active(service) == menu
        calls = client.generate_structured.call_count
        app = FitAgentApp(FitnessService(Storage(directory), client, clock=lambda: NOW))
        async with app.run_test(size=(80, 24)) as pilot:
            await pilot.pause()
            text = "\n".join(line.text for line in app.query_one("#chat", RichLog).lines)
            assert "Новая тема: Тренировки [red]" in text and "Возвращаемся к теме: Меню" in text
        assert client.generate_structured.call_count == calls


def test_live_and_restored_topic_markers_without_manual_switch_commands():
    asyncio.run(topic_ui_flow())


def test_checkpoint_requests_are_included_in_durable_token_totals():
    with tempfile.TemporaryDirectory() as directory:
        store = Storage(directory)
        client = real_client(store)
        service = FitnessService(store, client, clock=lambda: NOW)
        service.save(
            {
                "height": 180,
                "weight": 80,
                "sex": "мужской",
                "birth_date": "1996-01-01",
                "goal": "Сильнее",
                "target": None,
            }
        )
        first = json.dumps(
            {
                "operations": [],
                "topic": DEFAULT_TOPIC | {"title": "Меню"},
                "profile_change": None,
                "completion": "none",
            }
        )
        second = json.dumps(
            {
                "operations": [],
                "topic": DEFAULT_TOPIC | {"action": "switch", "title": "Тренировки"},
                "profile_change": None,
                "completion": "none",
            }
        )
        flash = httpx.Response(
            200,
            json={
                "choices": [{"message": {"content": "Сводка или ответ"}, "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 100, "completion_tokens": 10},
            },
        )
        with patch("fitagent.provider.httpx.Client") as factory:
            transport = factory.return_value.__enter__.return_value
            transport.post.side_effect = [
                responses_reply(first),
                responses_reply(safety_json()),
                flash,
                responses_reply(safety_json()),
                responses_reply(second),
                flash,
                responses_reply(safety_json()),
                flash,
                responses_reply(safety_json()),
            ]
            service.answer("Меню")
            service.answer("Тренировки")
            assert transport.post.call_count == 9
            schema = transport.post.call_args_list[0].kwargs["json"]["text"]["format"]
            assert schema["strict"] is True and schema["schema"] is TURN_SCHEMA
            # The outgoing checkpoint is prepared before the validator receives full context.
            calls = transport.post.call_args_list
            assert "Создай checkpoint" in calls[5].kwargs["json"]["messages"][0]["content"]
            checked = json.loads(calls[6].kwargs["json"]["input"][1]["content"])
            assert checked["dialogue"] in calls[7].kwargs["json"]["messages"][1]["content"]
        report = Storage(directory).token_usage()
        assert report["requests"] == 9 and report["total_tokens"] == 1710
        assert report["last_answer"] == {"input_tokens": 100, "output_tokens": 10}
        assert '"purpose": "checkpoint"' in store.logs()


def test_checkpoint_overflow_is_safe_and_does_not_commit_a_switch():
    with tempfile.TemporaryDirectory() as directory:
        store, client, service = fixture(directory)
        route(client, title="Меню")
        service.answer("Меню")
        original = active(service)
        route(client, action="switch", title="Тренировки")
        checkpoint_reply(client, "X" * 20_001)
        message = error(lambda: service.answer("Тренировки"), "Темы и факты не изменены")
        assert "Traceback" not in message and active(service) == original
        assert service.topics.repository.checkpoint(original) is None
        assert "checkpoint_failed" in store.logs()


def test_same_fact_cannot_be_updated_twice_with_different_scopes_in_one_turn():
    with tempfile.TemporaryDirectory() as directory:
        store, client, service = fixture(directory)
        route(client, title="Меню", operations=[operation()])
        service.answer("Не люблю брокколи")
        original = facts(service)[0]
        route(
            client,
            operations=[
                operation(fact_id=original["id"]),
                operation(fact_id=original["id"], applicability="topic"),
            ],
        )
        error(lambda: service.answer("Не люблю брокколи"))
        assert facts(service) == [original]


def test_profile_changes_remain_global_while_conversation_history_is_isolated():
    with tempfile.TemporaryDirectory() as directory:
        store, client, service = fixture(directory)
        route(client, title="Меню")
        service.answer("MENU_PRIVATE")
        service.save(
            {
                "height": 180,
                "weight": 79,
                "sex": "мужской",
                "birth_date": "1996-01-01",
                "goal": "Новая цель",
                "target": None,
            }
        )
        route(client, action="switch", title="Динамика веса")
        checkpoint_reply(client, "Сводка меню")
        service.answer("Как изменился вес?")
        assert "MENU_PRIVATE" not in sent(client)
        assert '"weight": 80.0' in sent(client) and '"weight": 79.0' in sent(client)


def test_global_fact_keeps_applicability_when_reconfirmed_in_another_topic():
    with tempfile.TemporaryDirectory() as directory:
        store, client, service = fixture(directory)
        route(
            client,
            title="Завтраки",
            operations=[operation("Аллергия", category="food_restrictions", key="allergy")],
        )
        service.answer("Аллергия")
        first_topic = active(service)
        fact = facts(service)[0]
        route(
            client,
            action="switch",
            title="Ужины",
            operations=[
                operation(
                    "Аллергия", category="food_restrictions", key="allergy", fact_id=fact["id"]
                )
            ],
        )
        service.answer("Аллергия")
        updated = facts(service)[0]
        assert updated["id"] == fact["id"] and updated["applicability"] == "global"
        assert updated["topic_id"] is None and updated["source_topic_id"] == active(service)
        assert updated["source_topic_id"] != first_topic
        with store.connect() as db:
            assert [
                row[0]
                for row in db.execute(
                    "SELECT source_topic_id FROM fact_sources ORDER BY message_id"
                )
            ] == [first_topic, active(service)]


def load_tests(loader, tests, pattern):
    return unittest.TestSuite(
        unittest.FunctionTestCase(value)
        for name, value in globals().items()
        if name.startswith("test_") and callable(value)
    )
