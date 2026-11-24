"""Nutrition, packaging and service integration tests; all model/MCP calls are fakes."""

import copy
import json
import os
import tempfile
import unittest
from decimal import Decimal
from unittest.mock import Mock, patch

from support import EMPTY_TURN, safe_response, seed_accepted_goal
from test_fitagent import NOW, fixture, profile
from test_goal_workflow import CONTINUE, prepared

from fitagent.nutrition import NUTRITION_ERROR, NutritionShopping
from fitagent.nutrition_schema import ROUTE_PROMPT, SELECTION_PROMPT
from fitagent.service import FitnessService
from fitagent.shopping import product_data, purchase

LINK = "https://vkusvill.ru/mobile?basket&share=123"
DIET = "Рекомендуемый рацион на одного человека на день: гречка, 100 г сухой крупы. КБЖУ ориентировочные."
ROUTE = {
    "action": "shop",
    "question": "",
    "period": "один день",
    "food_format": "cook",
    "cheaper": False,
    "goal_related": False,
    "budget_rub": None,
}
NONE = dict(ROUTE, action="none", period="", food_format="unknown")
CHOICE = {
    "query_index": 0,
    "id": 17681,
    "amount": 100,
    "measure": "г",
    "usage": "На день, сухая крупа",
}


def raw_product(**changes):
    return {
        "id": 17681,
        "name": "Крупа гречневая",
        "price": {"current": 148, "currency": "RUB"},
        "unit": "шт",
        "weight": {"value": 0.5, "unit": "кг"},
        "url": "https://vkusvill.ru/goods/grechka-17681/",
        "properties": [
            {"name": "Состав", "value": "Гречневая крупа"},
            {
                "name": "Пищевая и энергетическая ценность в 100 г",
                "value": "Белки 13 г, жиры 3 г, углеводы 68 г; 346 ккал",
            },
        ],
        **changes,
    }


def attach(service, client, route=None, queries=None, choices=None, plan=None):
    catalog = Mock()
    catalog.search.return_value = [raw_product()]
    catalog.cart_link.return_value = LINK

    def structured(messages, schema, purpose="memory_extraction"):
        value = {
            "nutrition_route": route if route is not None else ROUTE,
            "nutrition_queries": {"queries": queries if queries is not None else ["гречка"]},
            "nutrition_selection": {"items": choices if choices is not None else [CHOICE]},
            "goal_planning": plan or CONTINUE,
        }.get(purpose, json.loads(EMPTY_TURN))
        return json.dumps(value, ensure_ascii=False)

    client.generate_structured.side_effect = structured
    client.generate.side_effect = lambda messages, limit, purpose: (
        '{"warning":""}' if purpose == "goal_assessment" else DIET
    )
    service.nutrition = NutritionShopping(client, catalog, audit=service.audit)
    return catalog


def fails(callback, fragment=None):
    try:
        callback()
    except ValueError as exc:
        if fragment:
            assert fragment in str(exc), str(exc)
        assert "PRIVATE" not in str(exc)
    else:
        raise AssertionError("Invalid nutrition operation accepted")


def test_package_rounding_weight_volume_and_purchase_cost_are_deterministic():
    product = product_data(raw_product())
    result = purchase(product, 600, "г")
    assert result["quantity"] == 2 and result["total"] == Decimal("296.00")
    assert purchase(product, 100, "г")["quantity"] == 1
    assert purchase(product, 1.1, "шт")["quantity"] == 2
    weighted = product_data(raw_product(unit="кг"))
    result = purchase(weighted, 255, "г")
    assert result["quantity"] == 0.26 and result["total"] == Decimal("38.48")
    liquid = product_data(raw_product(weight={"value": 1, "unit": "л"}))
    assert purchase(liquid, 1500, "мл")["quantity"] == 2
    for amount in (0, -1, float("nan"), float("inf"), True):
        fails(lambda: purchase(product, amount, "г"))
    fails(lambda: purchase(product, 21000, "г"))
    fails(lambda: purchase(product, 100, "мл"))
    fails(lambda: purchase(weighted, 1, "шт"))


def test_unit_conversion_keeps_rounding_boundaries_and_rejects_mass_volume_mixing():
    for unit, measure, amount, quantity in (
        ("г", "г", Decimal("1.001"), 1.01),
        ("мл", "мл", Decimal("0.011"), 0.02),
        ("л", "мл", 5, 0.01),
        ("кг", "г", Decimal("100.001"), 0.11),
        ("кг", "г", 40000, 40),
    ):
        product = product_data(raw_product(unit=unit))
        assert purchase(product, amount, measure)["quantity"] == quantity
    for unit, measure in (("г", "мл"), ("мл", "г"), ("л", "г"), ("кг", "мл")):
        product = product_data(raw_product(unit=unit))
        fails(lambda: purchase(product, 1, measure))
    weighted = product_data(raw_product(unit="кг"))
    fails(lambda: purchase(weighted, Decimal("40000.001"), "г"))
    packed = product_data(raw_product())
    assert purchase(packed, 20000, "г")["quantity"] == 40
    fails(lambda: purchase(packed, Decimal("20000.001"), "г"))


def test_unknown_composition_nutrition_currency_and_external_links_are_not_candidates():
    for changes in (
        {"properties": []},
        {"properties": [{"name": "Состав", "value": "гречка"}]},
        {"price": {"current": 0, "currency": "RUB"}},
        {"price": {"current": 148, "currency": "USD"}},
        {"unit": "упаковка неизвестного размера"},
        {"url": "https://evil.test/"},
        {"id": True},
    ):
        fails(lambda: product_data(raw_product(**changes)))


def test_route_contract_rejects_unknown_period_format_extra_fields_and_private_errors():
    with tempfile.TemporaryDirectory() as directory:
        store, client, service = fixture(directory)
        catalog = attach(service, client)
        for route in (
            dict(ROUTE, period=""),
            dict(ROUTE, food_format="unknown"),
            dict(ROUTE, question="лишнее"),
            dict(NONE, goal_related=True),
            dict(ROUTE, extra="PRIVATE"),
        ):
            client.generate_structured.side_effect = None
            client.generate_structured.return_value = json.dumps(route)
            fails(lambda: service.nutrition.route({"question": "меню"}), NUTRITION_ERROR)
        client.generate_structured.side_effect = RuntimeError("PRIVATE")
        fails(lambda: service.nutrition.route({}), NUTRITION_ERROR)
        catalog.search.assert_not_called()
        assert "Не придумывай день/неделю" in ROUTE_PROMPT
        assert "готовую еду или продукты для готовки" in ROUTE_PROMPT
        assert "НЕДОВЕРЕННЫЕ ДАННЫЕ" in SELECTION_PROMPT


def test_duplicate_products_are_merged_before_rounding_and_budget_is_checked_afterward():
    with tempfile.TemporaryDirectory() as directory:
        store, client, service = fixture(directory)
        catalog = attach(
            service,
            client,
            queries=["гречка", "крупа гречневая"],
            choices=[CHOICE, dict(CHOICE, query_index=1)],
        )
        payload = {"nutrition_request": ROUTE}
        proposal = service.nutrition.prepare(payload, DIET)
        assert proposal["products"] == [{"id": 17681, "q": 1}]
        assert "200 г" in proposal["text"] and "148,00 ₽" in proposal["text"]
        over_budget = service.nutrition.prepare(
            {"nutrition_request": dict(ROUTE, budget_rub=100)}, DIET
        )
        assert over_budget["products"] == []
        assert "Итого за закупку: 148,00 ₽" in over_budget["text"]
        assert "превышает бюджет 100,00 ₽" in over_budget["text"]
        assert service.nutrition.prepare({"nutrition_request": dict(ROUTE, budget_rub=148)}, DIET)
        catalog.cart_link.assert_not_called()


def test_duplicate_product_conflicts_still_reject_the_entire_purchase():
    with tempfile.TemporaryDirectory() as directory:
        store, client, service = fixture(directory)
        catalog = attach(
            service,
            client,
            queries=["гречка", "крупа гречневая"],
            choices=[CHOICE, dict(CHOICE, query_index=1, measure="шт")],
        )
        assert service.nutrition.prepare({"nutrition_request": ROUTE}, DIET) is None
        catalog.cart_link.assert_not_called()
        catalog = attach(
            service,
            client,
            queries=["гречка", "крупа гречневая"],
            choices=[CHOICE, dict(CHOICE, query_index=1)],
        )
        catalog.search.side_effect = [[raw_product()], [raw_product(name="Другая карточка")]]
        assert service.nutrition.prepare({"nutrition_request": ROUTE}, DIET) is None
        catalog.cart_link.assert_not_called()


def test_invented_or_invalid_product_selections_do_not_create_partial_baskets():
    with tempfile.TemporaryDirectory() as directory:
        store, client, service = fixture(directory)
        for choices in (
            [dict(CHOICE, id=999)],
            [dict(CHOICE, amount=-1)],
            [dict(CHOICE, amount=float("nan"))],
            [dict(CHOICE, query_index=7)],
            [CHOICE, CHOICE],
        ):
            catalog = attach(service, client, choices=choices)
            assert service.nutrition.prepare({"nutrition_request": ROUTE}, DIET) is None
            catalog.cart_link.assert_not_called()


def test_partial_selection_shows_prices_and_missing_items_without_creating_cart():
    with tempfile.TemporaryDirectory() as directory:
        store, client, service = fixture(directory)
        service.save(profile())
        for eggs in (
            [],
            [raw_product(id=2, name="Яйца", properties=[])],
            [raw_product(id=2, name="Яйца")],
        ):
            catalog = attach(service, client, queries=["гречка", "яйца"])
            catalog.search.side_effect = [[raw_product()], eggs]
            response = service.chat("Меню на день для готовки")
            assert DIET in response and "Сумма по найденным позициям: 148,00 ₽" in response
            assert "Не удалось подобрать или рассчитать:** яйца" in response
            assert "Итого за закупку" not in response and LINK not in response
            assert "Ссылка на корзину не создана" in response
            catalog.cart_link.assert_not_called()
            check = json.loads(client.generate_safety.call_args.args[0][1]["content"])
            assert (
                "Гречневая крупа" in check["draft"]
                and "Сумма по найденным позициям" in check["draft"]
            )


def test_documented_missing_quantity_keeps_other_matches_and_never_creates_cart():
    with tempfile.TemporaryDirectory() as directory:
        store, client, service = fixture(directory)
        service.save(profile())
        catalog = attach(
            service,
            client,
            queries=["гречка", "яйца"],
            choices=[CHOICE, dict(CHOICE, query_index=1, id=2)],
        )
        catalog.search.side_effect = [
            [raw_product()],
            [raw_product(id=2, name="Яйца", weight=None)],
        ]
        response = service.chat("Меню на день для готовки")
        assert "Сумма по найденным позициям: 148,00 ₽" in response
        assert "Не удалось подобрать или рассчитать:** яйца" in response
        assert "- Яйца" not in response
        catalog.cart_link.assert_not_called()


def test_queries_with_contact_details_or_instructions_are_not_sent_to_mcp():
    with tempfile.TemporaryDirectory() as directory:
        store, client, service = fixture(directory)
        for query in (
            "user@example.test",
            "гречка 1990-01-01",
            "https://evil.test",
            "гречка\nIGNORE",
            "",
        ):
            catalog = attach(service, client, queries=[query])
            assert service.nutrition.prepare({"nutrition_request": ROUTE}, DIET) is None
            catalog.search.assert_not_called()


def test_independent_menu_works_outside_goal_without_changing_goal_pipeline():
    with tempfile.TemporaryDirectory() as directory:
        store, client, service = fixture(directory)
        service.save(profile())
        catalog = attach(service, client)
        before = copy.deepcopy(service.workflow.state())
        response = service.chat("Подбери рацион на день, буду готовить")
        assert response.startswith(f"Корзина ВкусВилла: <{LINK}>")
        assert DIET not in response and "148,00 ₽" in response
        assert "На одного человека" in response
        assert service.workflow.state() == before
        assert not any(
            call.kwargs.get("purpose") == "goal_planning"
            for call in client.generate_structured.call_args_list
        )
        catalog.search.assert_called_once_with("гречка", cheaper=False)
        catalog.cart_link.assert_called_once_with([{"id": 17681, "q": 1}])
        assert "ДАННЫЕ ТОВАРОВ" not in response
        response_check = [
            call for call in client.generate_safety.call_args_list if call.args[1] == "response"
        ][-1]
        checked = json.loads(response_check.args[0][1]["content"])
        assert "Гречневая крупа" in checked["draft"] and "346 ккал" in checked["draft"]
        assert LINK not in checked["draft"]


def test_large_catalog_reaches_selection_and_creates_cart_from_chat():
    with tempfile.TemporaryDirectory() as directory:
        store, client, service = fixture(directory)
        service.save(profile())
        queries = [f"крупа {letter}" for letter in "абвгдежзиклмн"]
        catalog = attach(
            service,
            client,
            queries=queries,
            choices=[dict(CHOICE, query_index=index) for index in range(len(queries))],
        )
        product = raw_product()
        product["properties"].append({"name": "Описание", "value": "а" * 4000})
        catalog.search.return_value = [product]

        response = service.chat("Подбери рацион на день, буду готовить")

        selection = next(
            call
            for call in client.generate_structured.call_args_list
            if call.kwargs.get("purpose") == "nutrition_selection"
        )
        groups = json.loads(selection.args[0][1]["content"])["catalog"]
        assert [group["query"] for group in groups] == queries
        assert len(json.dumps(groups, ensure_ascii=False).encode("utf-8")) > 100_000
        assert LINK in response and DIET not in response
        catalog.cart_link.assert_called_once_with([{"id": 17681, "q": 3}])


def test_unknown_format_or_period_is_asked_in_chat_without_mcp_or_goal_changes():
    with tempfile.TemporaryDirectory() as directory:
        store, client, service = fixture(directory)
        service.save(profile())
        for question in (
            "На какой период подобрать рацион?",
            "Подобрать готовую еду или продукты для готовки?",
        ):
            route = dict(
                ROUTE, action="clarify", question=question, food_format="unknown", period=""
            )
            catalog = attach(service, client, route=route)
            before = service.workflow.state()
            assert service.chat("Подбери рацион") == question
            assert service.workflow.state() == before
            catalog.search.assert_not_called()
            catalog.cart_link.assert_not_called()


def test_general_nutrition_question_does_not_trigger_catalog_or_cart():
    with tempfile.TemporaryDirectory() as directory:
        store, client, service = fixture(directory)
        service.save(profile())
        seed_accepted_goal(service)
        catalog = attach(service, client, route=NONE)
        service.chat("Что такое белок?")
        catalog.search.assert_not_called()
        catalog.cart_link.assert_not_called()


def test_empty_catalog_marks_products_missing_and_only_search_failure_hides_prices():
    with tempfile.TemporaryDirectory() as directory:
        store, client, service = fixture(directory)
        service.save(profile())
        for products in ([], [{"id": "PRIVATE"}], [raw_product(properties=[])]):
            catalog = attach(service, client)
            catalog.search.return_value = products
            response = service.chat("Меню на день для готовки")
            assert DIET in response and "Не удалось подобрать или рассчитать:** гречка" in response
            assert "₽" not in response and "PRIVATE" not in response
            catalog.cart_link.assert_not_called()
        catalog = attach(service, client)
        catalog.search.side_effect = ValueError("PRIVATE network error")
        assert service.chat("Меню на день для готовки") == DIET
        catalog.cart_link.assert_not_called()
        catalog = attach(service, client, queries=["гречка", "яйца"])
        catalog.search.side_effect = [[raw_product()], ValueError("PRIVATE network error")]
        assert service.chat("Меню на день для готовки") == DIET
        catalog.cart_link.assert_not_called()
        catalog = attach(service, client)
        catalog.cart_link.side_effect = ValueError("PRIVATE link error")
        response = service.chat("Меню на день для готовки")
        assert DIET in response and "Итого за закупку: 148,00 ₽" in response
        assert "Ссылка на корзину недоступна" in response and LINK not in response
        assert "PRIVATE" not in response


def test_cheaper_followup_uses_previous_menu_and_price_sort_without_personal_mcp_payload():
    with tempfile.TemporaryDirectory() as directory:
        store, client, service = fixture(directory)
        service.save(profile())
        catalog = attach(service, client)
        service.chat("Рацион на день, буду готовить")
        catalog = attach(service, client, route=dict(ROUTE, cheaper=True, budget_rub=100))
        catalog.search.return_value = [raw_product(price={"current": 90, "currency": "RUB"})]
        response = service.chat("Дорого, нужно дешевле, до 100 рублей")
        assert "90,00 ₽" in response and LINK in response
        catalog.search.assert_called_once_with("гречка", cheaper=True)
        routing = [
            call
            for call in client.generate_structured.call_args_list
            if call.kwargs.get("purpose") == "nutrition_route"
        ][-1]
        context = json.loads(routing.args[0][1]["content"])
        assert "148,00" in context["dialogue"] and "Крупа гречневая" in context["dialogue"]
        assert list(catalog.cart_link.call_args.args[0][0]) == ["id", "q"]


def test_accepted_goal_stays_in_context_for_independent_menu_without_reacceptance():
    with tempfile.TemporaryDirectory() as directory:
        store, client, service = fixture(directory)
        service.save(profile())
        seed_accepted_goal(service)
        before = service.workflow.state()
        attach(service, client)
        assert LINK in service.chat("Рацион на день, буду готовить")
        assert service.workflow.state() == before
        assert client.generate.call_args.args[0][0]["content"].count("ПРИНЯТЫЙ ПЛАН ЦЕЛИ") == 1
        attach(service, client, route=NONE)
        service.chat("Как выполнять принятый план?")
        assert client.generate.call_args.args[0][0]["content"].count("ПРИНЯТЫЙ ПЛАН ЦЕЛИ") == 1
        assert service.workflow.state()["stage"] == "execution"
        service.answer("Низкоуровневый ответ без управления целью")
        assert "ПРИНЯТЫЙ ПЛАН ЦЕЛИ" not in client.generate.call_args.args[0][0]["content"]


def test_goal_nutrition_draft_gets_cart_but_still_requires_explicit_acceptance():
    with tempfile.TemporaryDirectory() as directory:
        store, client, service = prepared(directory)
        plan = {
            "action": "propose",
            "question": "",
            "plan": DIET,
            "criterion": "Неделя без ухудшения самочувствия",
        }
        catalog = attach(service, client, route=dict(ROUTE, goal_related=True), plan=plan)
        response = service.chat("Составь план питания для цели на день, буду готовить")
        assert "Черновик плана" in response and LINK in response
        assert service.workflow.state()["stage"] == "validation"
        checks_before = client.generate_safety.call_count
        calls_before = client.generate_structured.call_count
        assert "Начинаем выполнение" in service.chat("подтверждаю план")
        assert service.workflow.state()["stage"] == "execution"
        assert client.generate_safety.call_count == checks_before
        assert client.generate_structured.call_count == calls_before
        catalog.cart_link.assert_called_once()


def test_safety_block_or_protocol_failure_never_creates_link_or_commits_answer():
    with tempfile.TemporaryDirectory() as directory:
        store, client, service = fixture(directory)
        service.save(profile())
        catalog = attach(service, client)

        def block(messages, stage):
            value = json.loads(safe_response(messages, stage))
            if stage == "response":
                assert not catalog.cart_link.called
                for check in value["checks"]:
                    check.update(status="block", strictness="strict")
                value.update(status="block", description="Состав не подходит при аллергии.")
            return json.dumps(value)

        client.generate_safety.side_effect = block
        fails(lambda: service.chat("Меню на день для готовки"), "Состав не подходит")
        catalog.cart_link.assert_not_called()
        assert DIET not in "\n".join(store.history(NOW.isoformat()))
        client.generate_safety.side_effect = lambda messages, stage: (
            "PRIVATE" if stage == "response" else safe_response(messages, stage)
        )
        fails(lambda: service.chat("Меню на день для готовки"), "проверк")
        catalog.cart_link.assert_not_called()


def test_goal_danger_or_missing_required_information_does_not_fetch_products():
    with tempfile.TemporaryDirectory() as directory:
        store, client, service = prepared(directory)
        plan = {
            "action": "propose",
            "question": "",
            "plan": DIET,
            "criterion": "Проверяемый результат",
        }
        catalog = attach(service, client, route=dict(ROUTE, goal_related=True), plan=plan)

        def danger(messages, stage):
            value = json.loads(safe_response(messages, stage))
            if stage == "goal_plan":
                for check in value["checks"]:
                    check.update(status="block", strictness="strict")
                value.update(status="block", description="Опасность, нужна корректировка.")
            return json.dumps(value)

        client.generate_safety.side_effect = danger
        assert "Опасность" in service.chat("План питания для цели на день для готовки")
        catalog.search.assert_not_called()
    with tempfile.TemporaryDirectory() as directory:
        store, client, service = fixture(directory)
        service.save(profile())
        plan = {
            "action": "clarify",
            "question": "Какие у вас аллергии?",
            "plan": "",
            "criterion": "",
        }
        catalog = attach(service, client, route=dict(ROUTE, goal_related=True), plan=plan)
        assert "аллергии" in service.chat("План питания для цели на день для готовки")
        catalog.search.assert_not_called()


def test_no_active_goal_and_restart_keep_menu_available_without_a_new_goal():
    with tempfile.TemporaryDirectory() as directory:
        store, client, service = fixture(directory)
        service.save(profile())
        with store.connect() as db:
            db.execute("UPDATE goals SET completed_at=?", (NOW.isoformat(),))
        attach(service, client)
        assert LINK in service.chat("Меню на день для готовки")
        assert store.latest("goals") is None
        reopened = FitnessService(store, client, clock=lambda: NOW)
        catalog = attach(reopened, client, route=dict(ROUTE, cheaper=True))
        assert LINK in reopened.chat("Нужно дешевле")
        catalog.search.assert_called_once_with("гречка", cheaper=True)
        routing = [
            call
            for call in client.generate_structured.call_args_list
            if call.kwargs.get("purpose") == "nutrition_route"
        ][-1]
        context = json.loads(routing.args[0][1]["content"])
        assert "Крупа гречневая" in context["dialogue"] and context["goal"] is None


def test_short_answers_receive_previous_clarification_and_period_without_catalog_beforehand():
    with tempfile.TemporaryDirectory() as directory:
        store, client, service = fixture(directory)
        service.save(profile())
        question = "Подобрать готовую еду или продукты для готовки?"
        catalog = attach(
            service,
            client,
            route=dict(ROUTE, action="clarify", question=question, food_format="unknown"),
        )
        assert service.chat("Подбери меню на день") == question
        catalog.search.assert_not_called()
        catalog = attach(service, client)
        assert LINK in service.chat("Буду готовить")
        routing = [
            call
            for call in client.generate_structured.call_args_list
            if call.kwargs.get("purpose") == "nutrition_route"
        ][-1]
        context = json.loads(routing.args[0][1]["content"])
        assert question in context["dialogue"] and "меню на день" in context["dialogue"]
        catalog.search.assert_called_once()


def test_request_block_stops_before_catalog_and_pending_critical_change_never_creates_cart():
    from test_memory import operation

    with tempfile.TemporaryDirectory() as directory:
        store, client, service = fixture(directory)
        service.save(profile())
        catalog = attach(service, client)

        def blocked(messages, stage):
            value = json.loads(safe_response(messages, stage))
            for check in value["checks"]:
                check.update(status="block", strictness="strict")
            value.update(status="block", description="Нужен безопасный вариант.")
            return json.dumps(value)

        client.generate_safety.side_effect = blocked
        fails(lambda: service.chat("Меню на день для готовки"), "безопасный вариант")
        catalog.search.assert_not_called()
        catalog.cart_link.assert_not_called()
    with tempfile.TemporaryDirectory() as directory:
        store, client, service = prepared(directory)
        catalog = attach(service, client)
        original = client.generate_structured.side_effect
        restriction = next(
            fact
            for fact in service.memory.repository.load()["facts"]
            if fact["category"] == "food_restrictions"
        )

        def pending(messages, schema, purpose="memory_extraction"):
            if purpose == "memory_extraction":
                turn = json.loads(EMPTY_TURN)
                turn["operations"] = [
                    operation(
                        category="food_restrictions",
                        key=restriction["key"],
                        fact_id=restriction["id"],
                        text="Аллергий больше нет",
                    )
                ]
                return json.dumps(turn)
            return original(messages, schema, purpose)

        client.generate_structured.side_effect = pending
        service.chat("Аллергий больше нет. Меню на день для готовки")
        catalog.search.assert_not_called()
        catalog.cart_link.assert_not_called()


def test_cli_wires_anonymous_nutrition_without_network_on_startup():
    from fitagent.cli import main
    from fitagent.vkusvill import VkusvillClient

    with tempfile.TemporaryDirectory() as directory:
        with (
            patch("sys.argv", ["fitagent"]),
            patch.dict(os.environ, {"FITAGENT_DATA_DIR": directory, "OPENCODE_API_KEY": ""}),
            patch("fitagent.cli.load_dotenv"),
            patch("fitagent.ui.FitAgentApp") as app,
            patch("httpx.Client") as network,
        ):
            main()
            service = app.call_args.args[0]
            app.return_value.run.assert_called_once_with()
            assert isinstance(service.nutrition, NutritionShopping)
            assert isinstance(service.nutrition.catalog, VkusvillClient)
            assert "рождения" in service.start_chat()
            network.assert_not_called()


def load_tests(loader, tests, pattern):
    return unittest.TestSuite(
        unittest.FunctionTestCase(value)
        for name, value in globals().items()
        if name.startswith("test_") and callable(value)
    )
