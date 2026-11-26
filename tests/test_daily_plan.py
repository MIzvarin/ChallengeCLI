"""Daily MCP and TUI scenarios with local data and a mocked catalog."""

import asyncio
import io
import json
import tempfile
import unittest
from datetime import timedelta
from unittest.mock import Mock

from support import safety_json
from test_fitagent import NOW, fixture, profile
from test_goal_review_fixes import pending_goal
from test_layered_memory import ANSWERS
from test_memory import extract, operation
from test_nutrition import DIET, NONE, attach
from textual.widgets import Input, RichLog

from fitagent.daily_plan import ERROR, NAME, PENDING, PROFILE_ERROR
from fitagent.nutrition import NUTRITION_ERROR
from fitagent.plan_mcp import request_daily_plan, request_plan, serve, tool_call
from fitagent.storage import Storage
from fitagent.ui import FitAgentApp


def mock_plan(service, client, schedule=None, *, route=None, assessment=None, diet=DIET):
    catalog = attach(service, client, route=route)
    fallback = client.generate_structured.side_effect

    def structured(messages, schema, purpose):
        if purpose == "daily_schedule":
            assert schedule is not None
            return json.dumps(schedule, ensure_ascii=False)
        if purpose == "nutrition_plan":
            return json.dumps({"plan": diet}, ensure_ascii=False)
        if purpose == "nutrition_goal_check" and assessment is not None:
            return json.dumps(assessment, ensure_ascii=False)
        if purpose == "nutrition_estimate_context":
            return json.dumps(
                {"muscle_gain": False, "activity": "unknown", "requires_individual_limits": False}
            )
        return fallback(messages, schema, purpose)

    client.generate_structured.side_effect = structured
    return catalog


def call(service, arguments=None):
    return tool_call(service, {} if arguments is None else arguments, name=NAME)


def text(result):
    return result["content"][0]["text"]


def test_daily_mcp_discovery_validation_and_real_stdio_client():
    with tempfile.TemporaryDirectory() as directory:
        _, _, service = fixture(directory)
        packets = [
            ("initialize", {}),
            ("tools/list", {}),
            ("tools/call", {"name": NAME, "arguments": {"profile": "PRIVATE"}}),
        ]
        source = io.StringIO(
            "\n".join(
                json.dumps({"jsonrpc": "2.0", "id": number, "method": method, "params": params})
                for number, (method, params) in enumerate(packets)
            )
        )
        sink = io.StringIO()
        serve(service, source, sink)
        _, discovery, invalid = [
            json.loads(line)["result"] for line in sink.getvalue().splitlines()
        ]
        tool = next(item for item in discovery["tools"] if item["name"] == NAME)
        assert not tool["inputSchema"]["additionalProperties"]
        assert invalid["isError"] and "без аргументов" in text(invalid)
        assert "PRIVATE" not in service.store.activity_logs()
        try:
            asyncio.run(request_daily_plan(directory))
        except ValueError as exc:
            assert str(exc) == PROFILE_ERROR
        else:
            raise AssertionError("Accepted an empty profile")


def test_daily_plan_uses_reminders_and_preferences_and_reuses_cart_same_day():
    with tempfile.TemporaryDirectory() as directory:
        store, client, service = fixture(directory)
        now = NOW.replace(hour=6, minute=30)
        service.clock = lambda: now - timedelta(days=1)
        for answer in (
            *ANSWERS[:9],
            "Добавка А по понедельникам",
            ANSWERS[10],
            "Понедельник: приседания 3×8",
            "08:00",
        ):
            service.chat(answer)
        service.clock = lambda: now
        schedule = {
            "training": "Приседания, 3 подхода по 8 повторений.",
            "supplements": "Сегодня по вашему режиму — Добавка А.",
            "missing": ["Уточните дозу добавки, если она у вас указана."],
        }
        catalog = mock_plan(service, client, schedule)
        first = call(service)
        assert not first["isError"], first
        for fragment in (
            "09.11.2026",
            "понедельник",
            "08:00",
            "Приседания",
            "Добавка А",
            "Уточните дозу",
            DIET,
            "Корзина ВкусВилла",
        ):
            assert fragment in text(first)

        payloads = {
            item.kwargs["purpose"]: json.loads(item.args[0][-1]["content"])
            for item in client.generate_structured.call_args_list
        }
        schedule_data = payloads["daily_schedule"]
        assert schedule_data["weekday"] == "понедельник" and schedule_data["wake_time"] == "08:00"
        assert "Понедельник: приседания 3×8" in schedule_data["training_facts"][0]["value"]["text"]
        assert "Добавка А" in schedule_data["supplement_facts"][0]["value"]["text"]
        menu = payloads["nutrition_plan"]
        assert menu["daily_plan"]["training"] == schedule["training"]
        assert menu["nutrition_targets"]["days"] == 1
        assert menu["nutrition_request"]["food_format"] == "cook"
        assert text(call(service)) == text(first)
        assert len(store.saved_baskets(latest=True)) == 1
        assert store.chat_messages()[-1]["status"] == "daily_plan"
        catalog.search.assert_called_once_with("гречка", cheaper=False)
        catalog.cart_link.assert_called_once_with([{"id": 17681, "q": 1}])
        response_check = json.loads(client.generate_safety.call_args.args[0][-1]["content"])
        assert "Приседания" in response_check["draft"] and "Добавка А" in response_check["draft"]


def test_missing_data_keeps_meal_and_cart_and_the_next_date_gets_a_new_plan():
    with tempfile.TemporaryDirectory() as directory:
        store, client, service = fixture(directory)
        service.save(profile())
        catalog = mock_plan(service, client, route=NONE)
        first = call(service)
        assert not first["isError"], first
        assert "Укажите дни и упражнения" in text(first)
        assert "какие добавки вы принимаете" in text(first)
        assert "/wake ЧЧ:ММ" in text(first)
        assert DIET in text(first) and "Корзина ВкусВилла" in text(first)
        nutrition_payload = next(
            json.loads(item.args[0][-1]["content"])
            for item in client.generate_structured.call_args_list
            if item.kwargs["purpose"] == "nutrition_plan"
        )
        assert nutrition_payload["nutrition_request"]["food_format"] == "mixed"
        assert all(
            item.kwargs["purpose"] != "daily_schedule"
            for item in client.generate_structured.call_args_list
        )
        store.set_wake_time(None)
        service.clock = lambda: NOW + timedelta(days=1)
        second = call(service)
        assert not second["isError"], second
        assert "10.11.2026" in text(second) and "/wake" not in text(second)
        assert catalog.cart_link.call_count == 2
        assert len([row for row in store.chat_messages() if row["status"] == "daily_plan"]) == 2


def test_failed_plan_is_retryable_and_reservation_is_shared_and_cleared():
    with tempfile.TemporaryDirectory() as directory:
        store, client, service = fixture(directory)
        service.save(profile())
        day = NOW.date().isoformat()
        attempt, saved = Storage(directory).reserve_daily_plan(NOW)
        assert attempt and saved is None
        busy = call(service)
        assert busy["isError"] and text(busy) == PENDING
        assert store.daily_plan_status(NOW) == "pending"
        store.release_daily_plan(day, attempt)
        mock_plan(service, client)
        client.generate_structured.side_effect = OSError("PRIVATE")
        error = call(service)
        assert error["isError"] and "PRIVATE" not in text(error)
        assert store.daily_plan_status(NOW) == "missing"
        catalog = mock_plan(service, client)
        catalog.search.side_effect = OSError("PRIVATE")
        retried = call(service)
        assert not retried["isError"], retried
        assert DIET in text(retried) and "Подбор товаров недоступен" in text(retried)
        assert "PRIVATE" not in text(retried)
        assert store.daily_plan_status(NOW) == "saved"
        assert len(store.saved_baskets(include_daily=True)) == 1
        assert not store.saved_baskets()
        store.set_wake_time("08:00")
        service.clear()
        assert store.daily_plan_status(NOW) == "missing" and store.wake_time() is None


def test_first_tui_open_before_wake_time_shows_today_without_repeating_on_reopen():
    async def scenario(directory):
        store, client, service = fixture(directory)
        service.save(profile())
        service.clock = lambda: NOW.replace(hour=6)
        store.set_wake_time("08:00")
        mock_plan(service, client)
        calls = []

        async def request(data_directory):
            calls.append(data_directory)
            result = call(service)
            assert not result["isError"], result
            return text(result)

        for _ in range(2):
            app = FitAgentApp(service, daily_plan_request=request)
            async with app.run_test(size=(110, 50)) as pilot:
                await app.workers.wait_for_complete()
                await pilot.pause()
                log = "\n".join(line.text for line in app.query_one("#chat", RichLog).lines)
                assert log.count("План на 09.11.2026") == 1
                assert "08:00" in log and "Корзина ВкусВилла" in log
        assert calls == [store.directory]

    with tempfile.TemporaryDirectory() as directory:
        asyncio.run(scenario(directory))


def test_allergy_invalidates_today_plan_and_cart_but_keeps_chat_history():
    with tempfile.TemporaryDirectory() as directory:
        store, client, service = fixture(directory)
        service.save(profile())
        mock_plan(service, client)
        first = call(service)
        assert not first["isError"] and "гречка" in text(first)
        service.nutrition = None
        client.generate_structured.side_effect = None
        allergy = "Аллергия на гречку"
        extract(client, operation(allergy, category="food_restrictions", key="buckwheat_allergy"))
        service.answer(allergy)
        assert store.daily_plan_status(NOW) == "missing"
        assert not store.saved_baskets(latest=True, now=NOW)
        assert store.chat_messages(now=NOW)[0]["status"] == "outdated_daily_plan"

        replacement = mock_plan(service, client, diet="Сегодня на обед — рис и овощи.")
        replacement.search.side_effect = OSError("Catalog unavailable")
        second = call(service)
        assert not second["isError"] and "рис и овощи" in text(second)
        assert "гречка" not in text(second)
        checked = json.loads(client.generate_safety.call_args.args[0][-1]["content"])
        assert any(rule["value"]["text"] == allergy for rule in checked["rules"])
        old, new = [
            item["content"] for item in store.chat_messages() if item["status"] == "daily_plan"
        ]
        assert old == text(first) and new == text(second)


def test_plan_in_progress_cannot_be_saved_after_safety_inputs_change():
    with tempfile.TemporaryDirectory() as directory:
        store, client, service = fixture(directory)
        service.save(profile())
        first, _ = store.reserve_daily_plan(NOW)
        service.nutrition = None
        allergy = "Аллергия на арахис"
        extract(client, operation(allergy, category="food_restrictions", key="peanut_allergy"))
        service.answer(allergy)
        assert not store.finish_daily_plan(NOW.date().isoformat(), first, "Старый план", NOW)
        assert all(msg["status"] != "daily_plan" for msg in store.chat_messages())


def test_recall_keeps_daily_plan_warning_through_stdio_and_chat_renderer():
    async def scenario(directory):
        _, client, service = fixture(directory)
        service.save(profile())
        mock_plan(service, client)
        warning = "Учитывайте сообщённое ограничение."
        client.generate_safety.side_effect = lambda messages, stage: safety_json(
            status="warning", description=warning, strictness="advisory"
        )
        planned = call(service)
        assert not planned["isError"] and warning in text(planned)
        recalled = await request_plan(directory, "последняя корзина")
        assert warning in recalled and "Корзина ВкусВилла" in recalled
        app = FitAgentApp(service)
        async with app.run_test(size=(110, 50)) as pilot:
            app.query_one("#question", Input).value = "/plan последняя корзина"
            await pilot.press("enter")
            await app.workers.wait_for_complete()
            log = "\n".join(line.text for line in app.query_one("#chat", RichLog).lines)
            assert log.count(warning) == 2

    with tempfile.TemporaryDirectory() as directory:
        asyncio.run(scenario(directory))


def test_daily_failure_while_token_screen_is_open_reaches_the_chat():
    async def scenario(directory):
        _, _, service = fixture(directory)
        service.save(profile())
        started, fail = asyncio.Event(), asyncio.Event()

        async def request(_):
            started.set()
            await fail.wait()
            raise ValueError(ERROR)

        app = FitAgentApp(service, daily_plan_request=request)
        async with app.run_test(size=(110, 45)) as pilot:
            await asyncio.wait_for(started.wait(), 2)
            app.action_tokens()
            await pilot.pause()
            assert len(app.screen_stack) == 2
            fail.set()
            await app.workers.wait_for_complete()
            await pilot.press("escape")
            log = "\n".join(line.text for line in app.query_one("#chat", RichLog).lines)
            assert "Не удалось подготовить план на сегодня" in log

    with tempfile.TemporaryDirectory() as directory:
        asyncio.run(scenario(directory))


def test_paused_goal_is_excluded_from_daily_schedule_and_menu():
    with tempfile.TemporaryDirectory() as directory:
        _, client, service = pending_goal(directory)
        assert "Выполнение плана пока не продолжаем" in service.start_chat()
        schedule = {"training": "Прогулка", "supplements": "Нет", "missing": []}
        mock_plan(service, client, schedule)
        result = call(service)
        assert not result["isError"], result
        payloads = {
            item.kwargs["purpose"]: json.loads(item.args[0][-1]["content"])
            for item in client.generate_structured.call_args_list
            if "purpose" in item.kwargs
        }
        assert payloads["daily_schedule"]["accepted_goal_plan"] == ""
        assert payloads["nutrition_plan"]["goal_state"] is None
        assert payloads["nutrition_plan"]["goal"] is None
        assert DIET in text(result)


def test_unsuitable_menu_keeps_reminders_and_reports_assessment_once():
    schedule = {"training": "Приседания", "supplements": "Добавка А", "missing": []}
    for status in ("clarify", "revise"):
        with tempfile.TemporaryDirectory() as directory:
            _, client, service = fixture(directory)
            for answer in ANSWERS:
                service.chat(answer)
            reason = "Уточните фактический объём порций."
            catalog = mock_plan(
                service, client, schedule, assessment={"status": status, "description": reason}
            )
            result = call(service)
            assert not result["isError"], result
            for fragment in (
                "План на 09.11.2026",
                "### Тренировка",
                "Приседания",
                "### Добавки",
                "Добавка А",
                "### Питание",
            ):
                assert fragment in text(result)
            assert text(result).count(reason) == 1
            catalog.cart_link.assert_not_called()


def test_relevant_followup_carries_daily_reply_to_memory_chat_and_safety():
    with tempfile.TemporaryDirectory() as directory:
        _, client, service = fixture(directory)
        service.save(profile())
        diet = "Обед в 13:00 — гречка с овощами."
        mock_plan(service, client, diet=diet)
        plan = call(service)
        assert not plan["isError"], plan
        service.nutrition = None
        client.generate_structured.side_effect = None
        service.answer("Чем заменить обед в сегодняшнем плане?")
        extracted = json.loads(client.generate_structured.call_args.args[0][-1]["content"])
        assert diet in extracted["daily_plan_reference"]
        assert all(diet not in fact["value"]["text"] for fact in extracted["existing_facts"])
        assert diet in client.generate.call_args.args[0][-1]["content"]
        checked = json.loads(client.generate_safety.call_args.args[0][-1]["content"])
        assert diet in checked["dialogue"]
        assert text(call(service)) == text(plan)
        service.answer("Как часто измерять вес?")
        extracted = json.loads(client.generate_structured.call_args.args[0][-1]["content"])
        assert "daily_plan_reference" not in extracted


def test_tui_recovers_when_another_window_releases_daily_reservation():
    async def scenario(directory):
        store, client, service = fixture(directory)
        service.save(profile())
        mock_plan(service, client)
        attempt, _ = store.reserve_daily_plan(NOW)
        request = Mock(side_effect=lambda _: call(service))

        async def generate(data_directory):
            result = request(data_directory)
            assert not result["isError"], result
            return text(result)

        app = FitAgentApp(service, daily_plan_request=generate)
        async with app.run_test(size=(110, 45)) as pilot:
            await pilot.pause()
            request.assert_not_called()
            store.release_daily_plan(NOW.date().isoformat(), attempt)
            await asyncio.wait_for(app.workers.wait_for_complete(), 4)
            request.assert_called_once()
            log = "\n".join(line.text for line in app.query_one("#chat", RichLog).lines)
            assert "План на 09.11.2026" in log

    with tempfile.TemporaryDirectory() as directory:
        asyncio.run(scenario(directory))


def test_daily_route_failure_has_daily_retry_instructions():
    with tempfile.TemporaryDirectory() as directory:
        store, client, service = fixture(directory)
        service.save(profile())
        mock_plan(service, client)
        service.nutrition.plan_route = Mock(side_effect=ValueError(NUTRITION_ERROR))
        result = call(service)
        assert result["isError"] and text(result) == ERROR
        assert store.daily_plan_status(NOW) == "missing"


def test_empty_daily_catalog_stops_before_selection_and_cart():
    with tempfile.TemporaryDirectory() as directory:
        store, client, service = fixture(directory)
        service.save(profile())
        catalog = mock_plan(service, client)
        catalog.search.return_value = []
        result = call(service)
        assert not result["isError"], result
        response = text(result)
        assert DIET in response and "Поиск товаров во ВкусВилле не нашёл" in response
        assert "Ссылка на корзину не создана" in response
        assert "Корзина ВкусВилла: <" not in response and "₽" not in response
        purposes = {
            call.kwargs.get("purpose") for call in client.generate_structured.call_args_list
        }
        assert not purposes & {"nutrition_selection", "nutrition_replacements"}
        catalog.search.assert_called_once_with("гречка", cheaper=False)
        catalog.cart_link.assert_not_called()
        assert "найдено товаров: 0" in store.activity_logs()
        assert "подходящие товары не найдены" in store.activity_logs()


def test_daily_cart_failure_names_stage_and_keeps_verified_prices():
    with tempfile.TemporaryDirectory() as directory:
        store, client, service = fixture(directory)
        service.save(profile())
        catalog = mock_plan(service, client)
        catalog.cart_link.side_effect = ValueError("PRIVATE backend error")
        result = call(service)
        assert not result["isError"], result
        response = text(result)
        assert DIET in response and "148,00 ₽" in response
        assert "Сбой на шаге создания ссылки во ВкусВилле" in response
        assert "Корзина ВкусВилла: <" not in response and "PRIVATE" not in response
        catalog.cart_link.assert_called_once_with([{"id": 17681, "q": 1}])
        assert not store.saved_baskets()
        assert "Ошибка MCP ВкусВилла  создание ссылки на корзину" in store.activity_logs()


def load_tests(loader, tests, pattern):
    return unittest.TestSuite(
        unittest.FunctionTestCase(fn) for name, fn in globals().items() if name.startswith("test_")
    )
