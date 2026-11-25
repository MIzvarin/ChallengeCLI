"""Exercise the local stdio boundary and private catalog projection."""

import asyncio
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from datetime import timedelta
from pathlib import Path
from unittest.mock import Mock, patch

from support import EMPTY_TURN, safety_json
from test_fitagent import fixture, profile
from test_goal_workflow import prepared
from test_memory import extract, operation
from test_nutrition import DIET, NONE, ROUTE, attach, raw_product

from fitagent.cli import build_service
from fitagent.nutrition_schema import UNMAPPED_TERM
from fitagent.plan_mcp import ERROR, NAME, PROFILE_ERROR, request_plan, serve, tool_call
from fitagent.tokens import collect_usage


def test_real_stdio_client_and_public_error_log():
    with tempfile.TemporaryDirectory() as directory:
        store, _, _ = fixture(directory)
        try:
            asyncio.run(request_plan(directory, "PRIVATE"))
        except ValueError as exc:
            assert str(exc) == PROFILE_ERROR
        else:
            raise AssertionError("Empty profile accepted")
        logs = store.activity_logs()
        assert NAME in logs and "ошибка" in logs and "мс" in logs and "PRIVATE" not in logs


def test_discovery_and_invalid_arguments():
    with tempfile.TemporaryDirectory() as directory:
        _, _, service = fixture(directory)
        requests = [
            ("initialize", {}),
            ("tools/list", {}),
            ("tools/call", {"name": NAME, "arguments": {"profile": "PRIVATE"}}),
        ]
        source = io.StringIO(
            "".join(
                json.dumps({"jsonrpc": "2.0", "id": i, "method": m, "params": p}) + "\n"
                for i, (m, p) in enumerate(requests)
            )
        )
        sink = io.StringIO()
        serve(service, source, sink)
        _, discovery, call = [json.loads(line)["result"] for line in sink.getvalue().splitlines()]
        assert discovery["tools"][0]["name"] == NAME
        assert call["isError"] and "условия" in call["content"][0]["text"]


def test_plan_keeps_menu_and_only_sends_public_terms():
    for term in ("гречка", "Иван Иванов"):
        with tempfile.TemporaryDirectory() as directory:
            store, client, service = fixture(directory)
            service.save(profile())
            catalog = attach(service, client, queries=[term])
            previous = client.generate_structured.side_effect

            def structured(messages, schema, purpose="memory_extraction"):
                if purpose == "nutrition_plan":
                    payload = json.loads(messages[-1]["content"])
                    assert (
                        payload["goal"] and payload["profile"] and "PRIVATE" in payload["question"]
                    )
                    return json.dumps({"plan": DIET})
                return previous(messages, schema, purpose)

            client.generate_structured.side_effect = structured
            result = tool_call(service, {"requirements": "готовлю на день PRIVATE"})
            assert not result["isError"], result
            answer = result["content"][0]["text"]
            assert DIET in answer and store.chat_messages()[-1]["content"] == answer
            if term == "гречка":
                catalog.search.assert_called_once_with(term, cheaper=False)
                assert "Корзина" in answer
            else:
                catalog.search.assert_not_called()
                assert "Подбор товаров недоступен" in answer
            logs = store.activity_logs()
            assert "PRIVATE" not in logs and "Иван" not in logs and NAME in logs


def test_followup_after_explicit_plan_uses_context_and_commits_one_chat_turn():
    with tempfile.TemporaryDirectory() as directory:
        store, client, service = fixture(directory)
        service.save(profile())
        clarify = dict(NONE, action="clarify", question="На какой срок подобрать меню?")
        catalog = attach(service, client, route=clarify)
        first = tool_call(service, {"requirements": "готовлю дома"})
        assert first["content"][0]["text"] == clarify["question"]
        catalog.search.assert_not_called()
        catalog = attach(service, client)
        structured = client.generate_structured.side_effect

        def generate(messages, schema, purpose="memory_extraction"):
            if purpose == "nutrition_plan":
                return json.dumps({"plan": DIET})
            return structured(messages, schema, purpose)

        client.generate_structured.side_effect = generate

        def dispatch(turn):
            assert clarify["question"] in turn["payload"]["dialogue"]
            assert turn["payload"]["nutrition_request"] == ROUTE
            result = tool_call(service, {}, json.loads(json.dumps(turn)))
            assert not result["isError"], result
            return result["content"][0]["text"]

        service.chat_plan = dispatch
        before = len(store.chat_messages())
        response = service.chat("На день")
        assert DIET in response and "Корзина" in response
        saved = store.chat_messages()[before:]
        assert [row["content"] for row in saved] == ["На день", response]
        catalog.search.assert_called_once_with("гречка", cheaper=False)


def test_configured_chat_client_calls_real_stdio_tool_without_rerouting():
    with (
        tempfile.TemporaryDirectory() as directory,
        patch.dict(os.environ, {"OPENCODE_API_KEY": ""}),
    ):
        store, _, _ = fixture(directory)
        service = build_service(store)
        # The empty database would return PROFILE_ERROR on the standalone path.
        try:
            service.chat_plan({"payload": {}, "memory": {}, "shop": False})
        except ValueError as exc:
            assert str(exc) == ERROR
        else:
            raise AssertionError("A model request without a key succeeded")
        assert NAME in store.activity_logs() and "ошибка" in store.activity_logs()


def test_explicit_ready_day_skips_general_router_and_ignores_historical_goal_change():
    with tempfile.TemporaryDirectory() as directory:
        _, client, service = fixture(directory)
        service.save(profile())
        catalog = attach(service, client, route=NONE)
        previous = client.generate_structured.side_effect

        def structured(messages, schema, purpose="memory_extraction"):
            if purpose == "nutrition_plan_fields":
                return json.dumps(
                    {
                        "period": "завтра",
                        "food_format": "ready",
                        "budget_rub": None,
                        "cheaper": False,
                        "goal_change_quote": "измени цель",
                        "continues_menu": False,
                    }
                )
            if purpose == "nutrition_plan":
                return json.dumps({"plan": DIET})
            assert purpose not in ("nutrition_route", "goal_planning")
            return previous(messages, schema, purpose)

        client.generate_structured.side_effect = structured
        result = tool_call(service, {"requirements": "Подбери готовые блюда на завтра"})
        assert not result["isError"] and DIET in result["content"][0]["text"]
        # No approved ready dish corresponds to the synthetic buckwheat query.
        catalog.search.assert_not_called()


def test_unrelated_reply_leaves_explicit_plan_clarification():
    with tempfile.TemporaryDirectory() as directory:
        store, client, service = fixture(directory)
        service.save(profile())
        attach(service, client, route=dict(NONE, action="clarify"))
        assert "срок" in tool_call(service, {"requirements": "Готовые блюда"})["content"][0]["text"]
        catalog = attach(service, client, route=NONE)
        service.answer("Что такое белок?")
        catalog.search.assert_not_called()
        with store.connect() as db:
            assert not db.execute("SELECT 1 FROM chat_state WHERE key='pending_menu'").fetchone()


def test_general_question_does_not_call_plan_tool():
    with tempfile.TemporaryDirectory() as directory:
        _, client, service = fixture(directory)
        service.save(profile())
        attach(service, client, route=NONE)
        service.chat_plan = Mock()
        service.answer("Что такое белок?")
        service.chat_plan.assert_not_called()


def test_explicit_clarification_honors_safety_block_before_display():
    with tempfile.TemporaryDirectory() as directory:
        store, client, service = fixture(directory)
        service.save(profile())
        route = dict(NONE, action="clarify", question="PRIVATE уточнение?")
        catalog = attach(service, client, route=route)
        client.generate_safety.side_effect = lambda messages, stage: safety_json(
            status="block" if stage == "response" else "allow",
            description="Требуется безопасный вариант.",
        )
        result = tool_call(service, {})
        assert result["isError"] and "Не могу показать" in result["content"][0]["text"]
        assert "PRIVATE" not in str(result)
        assert store.chat_messages()[-1]["status"] == "failed"
        catalog.search.assert_not_called()


def test_goal_draft_rejects_personal_name_before_catalog_search():
    with tempfile.TemporaryDirectory() as directory:
        _, client, service = prepared(directory)
        catalog = attach(
            service,
            client,
            route=dict(ROUTE, goal_related=True),
            plan={
                "action": "propose",
                "question": "",
                "plan": DIET,
                "criterion": "Меню согласовано",
            },
            queries=["Иван Иванов"],
        )
        response = service.chat("Составь план питания для цели на день, буду готовить")
        assert "Черновик плана" in response
        catalog.search.assert_not_called()
        catalog.cart_link.assert_not_called()


def test_catalog_search_preserves_supported_groups_and_refuses_incomplete_cart():
    with tempfile.TemporaryDirectory() as directory:
        _, client, service = fixture(directory)
        for terms in ([UNMAPPED_TERM], ["гречка", UNMAPPED_TERM, "гречка"]):
            catalog = attach(service, client, queries=terms)
            proposal = service.nutrition.prepare(
                {"nutrition_request": ROUTE}, DIET, catalog_terms_only=True
            )
            assert proposal["products"] == []
            assert "без подходящей поисковой категории" in proposal["text"]
            if "гречка" in terms:
                catalog.search.assert_called_once_with("гречка", cheaper=False)
                assert "148,00 ₽" in proposal["text"]
            else:
                catalog.search.assert_not_called()
            service.nutrition.render(proposal, DIET)
            catalog.cart_link.assert_not_called()


def test_ready_meal_search_rejects_raw_ingredient_and_accepts_dish():
    with tempfile.TemporaryDirectory() as directory:
        _, client, service = fixture(directory)
        payload = {"nutrition_request": dict(ROUTE, food_format="ready")}
        catalog = attach(service, client, queries=["курица"])
        assert service.nutrition.prepare(payload, DIET, catalog_terms_only=True) is None
        catalog.search.assert_not_called()
        catalog = attach(service, client, queries=["курица с гречкой"], choices=[])
        proposal = service.nutrition.prepare(payload, DIET, catalog_terms_only=True)
        catalog.search.assert_called_once_with("курица с гречкой", cheaper=False)
        assert proposal["products"] == []


def test_explicit_plan_extracts_allergy_in_new_topic_and_audits_only_ids():
    with tempfile.TemporaryDirectory() as directory:
        store, client, service = fixture(directory)
        service.save(profile())
        service.answer("Чужой бюджет на мероприятие")
        old_topic = service.topics.repository.snapshot()["active_topic_id"]
        attach(service, client)
        previous = client.generate_structured.side_effect
        allergy = "У меня аллергия на арахис"

        def structured(messages, schema, purpose="memory_extraction"):
            if purpose == "memory_extraction":
                turn = json.loads(EMPTY_TURN)
                turn["topic"].update(action="switch", title="Рацион")
                turn["operations"] = [
                    operation(allergy, category="food_restrictions", key="peanuts")
                ]
                return json.dumps(turn)
            if purpose == "nutrition_plan":
                payload = json.loads(messages[-1]["content"])
                assert "Чужой бюджет" not in payload["dialogue"]
                assert any(f["value"]["text"] == allergy for f in payload["facts"]["facts"])
                return json.dumps({"plan": DIET})
            return previous(messages, schema, purpose)

        client.generate_structured.side_effect = structured
        result = tool_call(service, {"requirements": "На неделю. " + allergy})
        assert not result["isError"], result
        assert service.topics.repository.snapshot()["active_topic_id"] != old_topic
        fact = next(f for f in service.memory.repository.load()["facts"] if f["key"] == "peanuts")
        with store.connect() as db:
            details = json.loads(
                db.execute(
                    "SELECT details FROM events WHERE kind='facts_read' ORDER BY id DESC LIMIT 1"
                ).fetchone()[0]
            )
        assert set(details) == {"fact_ids"} and fact["id"] in details["fact_ids"]


def test_explicit_goal_change_is_a_draft_for_confirmation():
    with tempfile.TemporaryDirectory() as directory:
        _, client, service = prepared(directory)
        attach(
            service,
            client,
            route=dict(ROUTE, goal_related=True),
            plan={
                "action": "propose",
                "plan": DIET,
                "question": "",
                "criterion": "Согласовать меню",
            },
        )
        result = tool_call(service, {"requirements": "Измени план цели на неделю"})
        assert not result["isError"] and "Черновик плана" in result["content"][0]["text"]
        state = service.workflow.state()
        assert state["stage"] == "validation" and state["plan"] == DIET


def test_explicit_plan_compacts_long_history_before_routing():
    with tempfile.TemporaryDirectory() as directory:
        _, client, service = fixture(directory)
        service.save(profile())
        service.answer("длинная запись " * 4000)
        attach(service, client, route=dict(NONE, action="clarify", question="На какой срок?"))
        result = tool_call(service, {})
        assert not result["isError"]
        routing = next(
            c
            for c in client.generate_structured.call_args_list
            if c.kwargs.get("purpose") == "nutrition_plan_fields"
        )
        dialogue = json.loads(routing.args[0][-1]["content"])["dialogue"]
        assert "СВОДКА:" in dialogue and "длинная запись" not in dialogue


def test_generated_menu_counts_as_last_answer_after_catalog_selection():
    events = [
        ("api_usage", {"purpose": purpose, "input_tokens": i, "output_tokens": 2 * i})
        for i, purpose in enumerate(
            ("answer", "nutrition_route", "nutrition_plan", "nutrition_selection"), 1
        )
    ]
    report = collect_usage(events)
    assert report["last_answer"] == {"input_tokens": 3, "output_tokens": 6}
    assert report["requests"] == 4 and report["total_tokens"] == 30


def test_mcp_init_errors_leave_stdout_empty_and_hide_configuration():
    with tempfile.TemporaryDirectory() as directory:
        for data_dir, url in (("/dev/null", "https://example.test"), (directory, "private-config")):
            result = subprocess.run(
                [str(Path(sys.executable).with_name("fitagent")), "mcp"],
                env=dict(os.environ, FITAGENT_DATA_DIR=data_dir, FITAGENT_API_URL=url),
                input="",
                capture_output=True,
                text=True,
                timeout=10,
            )
            assert result.returncode and not result.stdout
            assert "Проверьте доступ к папке данных" in result.stderr
            assert (
                "Traceback" not in result.stderr
                and data_dir not in result.stderr
                and url not in result.stderr
            )


def test_replacements_search_until_complete_with_saved_memory_and_plain_request():
    with tempfile.TemporaryDirectory() as directory:
        store, client, service = fixture(directory)
        service.save(profile())
        preference = "Не люблю изюм"
        extract(client, operation(preference, key="raisins"))
        service.answer(preference)
        catalog = attach(service, client, queries=["гречка"], choices=[])
        catalog.search.side_effect = [[], [], [raw_product(id=43, name="Рис")]]
        previous = client.generate_structured.side_effect
        actions = iter(
            [
                {"queries": ["киноа"], "meals": [], "nutrition_summary": ""},
                {"queries": ["рис"], "meals": [], "nutrition_summary": ""},
                {
                    "queries": [],
                    "meals": [
                        {"title": "Обед", "portions": [{"id": 43, "amount": 100, "measure": "г"}]}
                    ],
                    "nutrition_summary": "Ориентировочно 346 ккал.",
                },
            ]
        )

        def structured(messages, schema, purpose="memory_extraction"):
            if purpose in ("nutrition_plan", "nutrition_replacements"):
                payload = json.loads(messages[-1]["content"])
                assert preference not in payload["question"]
                assert any(f["value"]["text"] == preference for f in payload["facts"]["facts"])
                if purpose == "nutrition_plan":
                    return json.dumps({"plan": DIET})
                assert not set(payload["searched"]) & set(
                    schema["properties"]["queries"]["items"]["enum"]
                )
                return json.dumps(next(actions))
            return previous(messages, schema, purpose)

        client.generate_structured.side_effect = structured
        result = tool_call(service, {"requirements": "Обед на завтра, готовлю дома."})
        assert not result["isError"], result
        assert [c.args[0] for c in catalog.search.call_args_list] == ["гречка", "киноа", "рис"]
        assert "Рис — 100 г" in result["content"][0]["text"]
        catalog.cart_link.assert_called_once_with([{"id": 43, "q": 1.0}])
        assert preference not in store.activity_logs()


def test_replacements_stop_without_cart_when_alternatives_exhausted_or_over_budget():
    from fitagent.menu_replacements import complete_menu

    for budget in (None, 10):
        with tempfile.TemporaryDirectory() as directory:
            _, client, service = fixture(directory)
            catalog = attach(service, client)
            payload = {"nutrition_request": dict(ROUTE, budget_rub=budget)}
            initial = service.nutrition.price_report([], ["гречка"], budget)
            cards = service.nutrition.search_products("рис", False)

            def decide(messages, schema, purpose):
                meals = []
                if budget and schema["properties"]["meals"]["maxItems"]:
                    meals = [
                        {
                            "title": "Обед",
                            "portions": [{"id": cards[0]["id"], "amount": 100, "measure": "г"}],
                        }
                    ]
                return json.dumps({"queries": [], "meals": meals, "nutrition_summary": ""})

            client.generate_structured.side_effect = decide
            result = complete_menu(service.nutrition, payload, DIET, initial, {"рис": cards})
            assert result == (DIET, initial, None)
            assert client.generate_structured.call_count == (3 if budget else 1)
            catalog.cart_link.assert_not_called()


def test_full_cart_needs_goal_fit_and_rechecks_revised_portions():
    with tempfile.TemporaryDirectory() as directory:
        store, client, service = fixture(directory)
        service.save(profile(goal="Набрать мышечную массу"))
        catalog = attach(service, client)
        fallback = client.generate_structured.side_effect
        assessed = []

        def structured(messages, schema, purpose="memory_extraction"):
            payload = json.loads(messages[-1]["content"])
            if purpose == "nutrition_plan":
                return json.dumps({"plan": DIET})
            if purpose == "nutrition_goal_check":
                assert payload["profile"] and "мышечную массу" in payload["goal"]["text"]
                assert payload["nutrition_request"]["period"] == ROUTE["period"]
                assert payload["complete_purchase"]
                assessed.append(payload["products"][0]["amount"])
                catalog.cart_link.assert_not_called()
                if len(assessed) == 1:
                    return json.dumps({"status": "revise", "description": "Увеличьте порции."})
                return json.dumps({"status": "fit", "description": "Порции пересмотрены."})
            if purpose == "nutrition_replacements":
                assert payload["goal_feedback"] == "Увеличьте порции."
                return json.dumps(
                    {
                        "queries": [],
                        "meals": [
                            {
                                "title": "Обед",
                                "portions": [{"id": 17681, "amount": 600, "measure": "г"}],
                            }
                        ],
                        "nutrition_summary": "Исправленный расчёт.",
                    }
                )
            return fallback(messages, schema, purpose)

        client.generate_structured.side_effect = structured
        response = tool_call(service, {"requirements": "На день, готовлю дома."})
        assert not response["isError"], response
        assert assessed == [100, 600]
        assert "Исправленный расчёт." in response["content"][0]["text"]
        catalog.cart_link.assert_called_once_with([{"id": 17681, "q": 2}])
        assert "Увеличьте порции" not in store.activity_logs()


def test_unverified_goal_keeps_cart_closed_with_actionable_answer():
    for status, reason in (("clarify", "нужны сведения"), ("revise", "рацион не подходит")):
        with tempfile.TemporaryDirectory() as directory:
            store, client, service = fixture(directory)
            service.save(profile())
            catalog = attach(service, client)
            fallback = client.generate_structured.side_effect
            description = "Укажите текущий вес." if status == "clarify" else "Нужны другие порции."

            def structured(messages, schema, purpose="memory_extraction"):
                if purpose == "nutrition_plan":
                    return json.dumps({"plan": DIET})
                if purpose == "nutrition_goal_check":
                    return json.dumps({"status": status, "description": description})
                if purpose == "nutrition_replacements":
                    assert status == "revise"
                    return json.dumps({"queries": [], "meals": [], "nutrition_summary": ""})
                return fallback(messages, schema, purpose)

            client.generate_structured.side_effect = structured
            response = tool_call(service, {"requirements": "Готовлю дома на день"})
            assert not response["isError"] and description in response["content"][0]["text"]
            assert DIET not in response["content"][0]["text"]
            catalog.cart_link.assert_not_called()
            assert reason in store.activity_logs() and description not in store.activity_logs()


def test_numeric_targets_reach_menu_and_replace_portions_before_cart():
    with tempfile.TemporaryDirectory() as directory:
        _, client, service = fixture(directory)
        service.save(profile())
        catalog = attach(service, client)
        fallback = client.generate_structured.side_effect
        targets = {
            "days": 1,
            "limits": [
                {
                    "metric": "kcal",
                    "scope": "daily",
                    "target": 692,
                    "minimum": None,
                    "maximum": None,
                },
                {
                    "metric": "protein_g",
                    "scope": "daily",
                    "target": None,
                    "minimum": 25,
                    "maximum": None,
                },
            ],
        }

        def structured(messages, schema, purpose="memory_extraction"):
            payload = json.loads(messages[-1]["content"])
            if purpose == "nutrition_targets":
                assert "692 ккал" in payload["question"]
                return json.dumps(targets)
            if purpose in {"nutrition_plan", "nutrition_replacements", "nutrition_goal_check"}:
                assert payload["nutrition_targets"] == targets
            if purpose == "nutrition_plan":
                return json.dumps({"plan": DIET})
            if purpose == "nutrition_replacements":
                assert "346.0" in payload["goal_feedback"]
                return json.dumps(
                    {
                        "queries": [],
                        "meals": [
                            {
                                "title": "Меню",
                                "portions": [{"id": 17681, "amount": 200, "measure": "г"}],
                            }
                        ],
                        "nutrition_summary": "692 ккал, 26 г белка.",
                    }
                )
            if purpose == "nutrition_goal_check":
                assert payload["products"][0]["amount"] == 200
            return fallback(messages, schema, purpose)

        client.generate_structured.side_effect = structured
        result = tool_call(
            service, {"requirements": "На день, готовлю дома, 692 ккал и не меньше 25 г белка."}
        )
        assert not result["isError"] and "692 ккал, 26 г белка" in result["content"][0]["text"]
        catalog.search.assert_called_once_with("гречка", cheaper=False)
        catalog.cart_link.assert_called_once_with([{"id": 17681, "q": 1}])


def test_numeric_targets_check_each_day_and_use_lower_supplier_value():
    from fitagent.nutrition_targets import check_targets

    targets = {
        "days": 2,
        "limits": [
            {
                "metric": "protein_g",
                "scope": "daily",
                "target": None,
                "minimum": 12,
                "maximum": None,
            }
        ],
    }
    product = {
        "measure": "г",
        "amount": 100,
        "properties": [
            {
                "name": "Пищевая ценность в 100 г",
                "value": "Поставщик А: белки 12–15 г<br>Поставщик Б: белки 10,5 г",
            }
        ],
    }
    assert "отдельно на каждый день" in check_targets(targets, [product])
    double = dict(product, amount=200)
    assert "10.5" in check_targets(targets, [product, double], {1: [double], 2: [product]})
    assert not check_targets(targets, [double, double], {1: [double], 2: [double]})


def test_conflicting_numeric_targets_show_a_russian_input_error():
    with tempfile.TemporaryDirectory() as directory:
        _, client, service = fixture(directory)
        service.save(profile())
        catalog = attach(service, client)
        fallback = client.generate_structured.side_effect

        def structured(messages, schema, purpose="memory_extraction"):
            if purpose == "nutrition_targets":
                return json.dumps(
                    {
                        "days": 1,
                        "limits": [
                            {
                                "metric": "kcal",
                                "scope": "daily",
                                "target": None,
                                "minimum": 2300,
                                "maximum": 2200,
                            }
                        ],
                    }
                )
            return fallback(messages, schema, purpose)

        client.generate_structured.side_effect = structured
        result = tool_call(service, {"requirements": "От 2300 до 2200 ккал на день."})
        assert result["isError"]
        assert "Минимум не должен превышать максимум" in result["content"][0]["text"]
        catalog.search.assert_not_called()


def test_selection_failure_reuses_catalog_and_audits_only_closed_reason():
    selection = {"query_index": 0, "id": 17681, "amount": 100, "measure": "г", "usage": "PRIVATE"}
    cases = {
        "provider_response": None,
        "invalid_json": "PRIVATE",
        "invalid_schema": json.dumps({"wrong": "PRIVATE"}),
        "duplicate_group": json.dumps({"items": [selection, selection]}),
        "unknown_group": json.dumps({"items": [dict(selection, query_index=1)]}),
        "unknown_product": json.dumps({"items": [dict(selection, id=999)]}),
    }
    for reason, broken in cases.items():
        with tempfile.TemporaryDirectory() as directory:
            store, client, service = fixture(directory)
            service.save(profile())
            catalog = attach(service, client)
            fallback = client.generate_structured.side_effect

            def structured(messages, schema, purpose="memory_extraction"):
                if purpose == "nutrition_plan":
                    return json.dumps({"plan": DIET})
                if purpose == "nutrition_selection":
                    if broken is None:
                        raise ValueError("PRIVATE")
                    return broken
                if purpose == "nutrition_replacements":
                    payload = json.loads(messages[-1]["content"])
                    assert [item["id"] for item in payload["catalog"]] == [17681]
                    assert payload["searched"] == ["гречка"]
                    catalog.cart_link.assert_not_called()
                    return json.dumps(
                        {
                            "queries": [],
                            "meals": [
                                {
                                    "title": "Обед",
                                    "portions": [{"id": 17681, "amount": 150, "measure": "г"}],
                                }
                            ],
                            "nutrition_summary": "Пересобранный набор",
                        }
                    )
                return fallback(messages, schema, purpose)

            client.generate_structured.side_effect = structured
            result = tool_call(service, {"requirements": "Готовлю дома на день"})
            assert not result["isError"] and "Пересобранный набор" in result["content"][0]["text"]
            catalog.search.assert_called_once_with("гречка", cheaper=False)
            catalog.cart_link.assert_called_once_with([{"id": 17681, "q": 1}])
            with store.connect() as db:
                events = db.execute(
                    "SELECT kind,details FROM events WHERE kind IN "
                    "('nutrition_selection_failed','nutrition_cart_skipped')"
                ).fetchall()
            assert [(kind, json.loads(details)) for kind, details in events] == [
                ("nutrition_selection_failed", {"reason": reason})
            ]
            assert (
                "Подбор товаров" in store.activity_logs() and "PRIVATE" not in store.activity_logs()
            )


def test_plan_receives_profile_snapshot_weight_and_uses_newer_measurement_when_available():
    from test_fitagent import NOW

    with tempfile.TemporaryDirectory() as directory:
        store, client, service = fixture(directory)
        store.save_profile(
            profile(weight=74, target=78), NOW, service.next_check(), record_weight=False
        )
        assert store.weights() == []
        assert "74 кг" in service.profile_text() and "записан в профиле" in service.profile_text()
        assert service.current_profile()["weight_recorded_at"] == NOW.isoformat()
        service.clock = lambda: NOW + timedelta(days=31)
        attach(service, client)
        fallback = client.generate_structured.side_effect
        sent = []

        def structured(messages, schema, purpose="memory_extraction"):
            if purpose in {"nutrition_plan", "nutrition_goal_check"}:
                payload = json.loads(messages[-1]["content"])
                weight = payload["profile"]
                sent.append(weight["weight"])
                assert not {"measured_at", "weight_recorded_at", "confirmed_at"} & weight.keys()
                assert "profile.weight актуален при любой дате" in messages[0]["content"]
                assert payload["goal"]["target"] == 78
                if purpose == "nutrition_plan":
                    return json.dumps({"plan": DIET})
            return fallback(messages, schema, purpose)

        client.generate_structured.side_effect = structured
        for weight in (74, 75):
            if weight == 75:
                service.save(profile(weight=weight, target=78))
            response = tool_call(service, {"requirements": "На день, готовлю дома."})
            assert not response["isError"], response
        assert sent == [74, 74, 75, 75]
        safety_payloads = [
            json.loads(call.args[0][-1]["content"])
            for call in client.generate_safety.call_args_list
        ]
        assert [payload["profile"]["weight"] for payload in safety_payloads] == sent
        assert all("weight_recorded_at" not in payload["profile"] for payload in safety_payloads)


def test_full_day_uses_profile_estimates_and_displays_catalog_totals():
    with tempfile.TemporaryDirectory() as directory:
        _, client, service = fixture(directory)
        service.save(profile(weight=74, sex="муж.", goal="Набрать мышечную массу", target=78))
        catalog = attach(service, client)
        fallback = client.generate_structured.side_effect
        catalog.search.return_value = [
            raw_product(
                properties=[
                    {"name": "Состав", "value": "Продукт для теста"},
                    {
                        "name": "Пищевая ценность в 100 г",
                        "value": "350 ккал, белки 22 г, жиры 5 г, углеводы 55 г",
                    },
                ]
            )
        ]
        corrected = iter([0, 500, 700])

        def structured(messages, schema, purpose="memory_extraction"):
            payload = json.loads(messages[-1]["content"])
            if purpose == "nutrition_targets":
                return json.dumps({"days": 1, "limits": []})
            if purpose == "nutrition_estimate_context":
                assert payload["profile"]["weight"] == 74
                return json.dumps(
                    {
                        "muscle_gain": True,
                        "activity": "unknown",
                        "requires_individual_limits": False,
                    }
                )
            if purpose in {"nutrition_plan", "nutrition_replacements", "nutrition_goal_check"}:
                targets = payload["nutrition_targets"]
                assert targets["estimated_activity"] == "unknown"
                assert targets["estimates"][1]["minimum"] == 118
            if purpose == "nutrition_plan":
                return json.dumps({"plan": DIET})
            if purpose == "nutrition_replacements":
                assert payload["catalog"][0]["nutrition_per_100"]["г"]["kcal"] == [350, 350]
                amount = next(corrected)
                if amount == 0:
                    return json.dumps({"queries": [], "meals": [], "nutrition_summary": ""})
                if amount == 500:
                    assert "исходного черновика можно заменить" in payload["shopping_result"]
                if amount == 700:
                    assert "1750.0" in payload["goal_feedback"]
                return json.dumps(
                    {
                        "queries": [],
                        "meals": [
                            {
                                "title": "Рацион",
                                "portions": [{"id": 17681, "amount": amount, "measure": "г"}],
                            }
                        ],
                        "nutrition_summary": "НЕВЕРНЫЙ ИТОГ 9999 ккал",
                    }
                )
            if purpose == "nutrition_goal_check":
                assert payload["catalog_totals"]["kcal"] == [2450, 2450]
                assert payload["catalog_totals"]["protein_g"] == [154, 154]
            return fallback(messages, schema, purpose)

        client.generate_structured.side_effect = structured
        result = tool_call(
            service, {"requirements": "На завтра, завтрак, обед и ужин. Готовлю сам."}
        )
        assert not result["isError"], result
        menu = result["content"][0]["text"]
        assert "2450.0" in menu and "9999" not in menu and "Активность не указана" in menu
        catalog.cart_link.assert_called_once_with([{"id": 17681, "q": 2}])


def test_estimates_fill_only_missing_targets_and_respect_individual_limits():
    from fitagent.nutrition_targets import estimate_missing_targets

    energy = {"metric": "kcal", "scope": "daily", "minimum": 2300, "maximum": 2500, "target": None}
    targets = {"days": 1, "limits": [energy]}
    payload = {"profile": profile(weight=74, sex="муж."), "goal": {"text": "Набрать мышцы"}}
    nutrition = Mock()
    nutrition.structured.return_value = {
        "muscle_gain": True,
        "activity": "moderate",
        "requires_individual_limits": False,
    }
    result = estimate_missing_targets(nutrition, payload, targets)
    assert result["limits"][0] == energy and len(targets["limits"]) == 1
    assert [item["metric"] for item in result["estimates"]] == ["protein_g"]
    nutrition.structured.return_value["requires_individual_limits"] = True
    assert estimate_missing_targets(nutrition, payload, targets) == targets


def test_estimated_portion_adjustment_uses_catalog_bounds_without_changing_ingredients():
    from fitagent.menu_replacements import priced_menu
    from fitagent.nutrition_targets import check_targets
    from fitagent.shopping import product_data

    goals = [
        {"metric": "kcal", "scope": "daily", "minimum": 2350, "maximum": 2700, "target": None},
        {"metric": "protein_g", "scope": "daily", "minimum": 105, "maximum": None, "target": None},
    ]
    targets = {"days": 1, "limits": goals, "estimates": goals}
    raw = raw_product(
        properties=[
            {"name": "Состав", "value": "Продукт для теста"},
            {"name": "Пищевая ценность в 100 г", "value": "350–360 ккал, белки 18–20 г"},
        ]
    )
    card = product_data(raw)
    meal = [
        {
            "title": "Исходный рацион",
            "portions": [{"id": card["id"], "amount": 550, "measure": "г"}],
        }
    ]
    with tempfile.TemporaryDirectory() as directory:
        _, client, service = fixture(directory)
        attach(service, client)
        text, proposal = priced_menu(
            service.nutrition, meal, "", {card["id"]: card}, None, targets=targets
        )
        portions = json.loads(proposal["evidence"])
        assert [item["id"] for item in portions] == [card["id"]]
        assert 550 < portions[0]["amount"] <= 550 * 1.35
        assert check_targets(targets, portions) == ""
        assert "По выбранным порциям" in text


def load_tests(loader, tests, pattern):
    return unittest.TestSuite(
        unittest.FunctionTestCase(fn) for name, fn in globals().items() if name.startswith("test_")
    )
