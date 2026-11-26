"""Safe, concise public activity messages; the full audit stays separate."""

import json
from datetime import datetime

from fitagent.safety_schema import SAFETY_FAILURE_REASONS

ACTIVITY_KINDS = (
    "checkpoint_used",
    "topic_routed",
    "facts_read",
    "history_compacted",
    "safety_checked",
    "safety_failed",
    "memory_failed",
    "fact_changed",
    "fact_change_pending",
    "profile_updated",
    "goal_plan_updated",
    "mcp_started",
    "mcp_finished",
    "mcp_failed",
    "mcp_search_retry",
    "nutrition_selection_failed",
    "nutrition_cart_skipped",
)
MCP_OPERATIONS = {"search": "поиск товаров", "cart_link": "создание ссылки на корзину"}
MCP_RETRY_REASONS = {
    "timeout": "истекло время ожидания",
    "network": "сбой соединения",
    "rate_limit": "слишком много запросов",
    "server": "временная ошибка сервера",
}
CART_SKIP_REASONS = {
    "invalid_queries": "не удалось подготовить безопасные поисковые названия",
    "no_products": "подходящие товары не найдены",
    "product_data": "недостаточно данных о составе, КБЖУ или единицах продажи",
    "catalog_limit": "каталог превысил допустимый размер",
    "selection_error": "не удалось проверить подбор товаров",
    "incomplete_selection": "полный подходящий набор не найден",
    "quantity_error": "не удалось рассчитать количества упаковок",
    "product_conflict": "карточки или единицы одного товара не совпадают",
    "budget": "закупка превышает указанный бюджет",
    "goal_mismatch": "рацион не подходит для активной цели",
    "goal_data_missing": "нужны сведения для подбора рациона под цель",
    "mcp_error": "не удалось получить товары из MCP",
    "link_error": "не удалось получить ссылку на корзину",
}
SELECTION_FAILURE_REASONS = {
    "provider_response": "Модель не вернула результат подбора",
    "invalid_json": "ответ подбора не удалось прочитать",
    "invalid_schema": "структура ответа подбора не прошла проверку",
    "duplicate_group": "в ответе повторилась группа товаров",
    "unknown_group": "в ответе указана отсутствующая группа товаров",
    "unknown_product": "выбранного товара нет в указанной группе",
}
SAFETY_STAGES = {
    "onboarding": "первичное заполнение профиля",
    "request": "запрос",
    "changes": "изменение данных",
    "response": "ответ",
    "goal_response": "оценка цели",
    "goal_plan": "валидация плана цели",
    "goal_proposal": "изменение желаемой цели",
}


def integer(value, minimum=0):
    return type(value) is int and minimum <= value <= 2**63 - 1


def activity_message(kind, details):
    if not isinstance(details, dict):
        return None
    if kind == "mcp_search_retry":
        attempt, code = details.get("attempt"), details.get("reason")
        reason = MCP_RETRY_REASONS.get(code) if isinstance(code, str) else None
        if reason and integer(attempt, 2) and attempt <= 3:
            return f"MCP ВкусВилла  повтор поиска · попытка {attempt} из 3 · {reason}"
        return None
    if kind in {"mcp_started", "mcp_finished", "mcp_failed"}:
        code = details.get("operation")
        if code in ("create_nutrition_plan", "create_daily_plan"):
            prefix = f"MCP FitAgent  {code}"
            if kind == "mcp_started":
                return prefix + " · начато"
            duration = details.get("duration_ms")
            if integer(duration):
                status = "завершено" if kind == "mcp_finished" else "ошибка"
                return f"{prefix} · {status} · {duration} мс"
            return None
        operation = MCP_OPERATIONS.get(code) if isinstance(code, str) else None
        if not operation:
            return None
        if kind == "mcp_started":
            return f"MCP ВкусВилла  {operation} · начато"
        if kind == "mcp_failed":
            return f"Ошибка MCP ВкусВилла  {operation} · запрос не выполнен"
        count = details.get("count")
        if integer(count) and count <= 100:
            label = "найдено товаров" if code == "search" else "позиций в корзине"
            return f"MCP ВкусВилла  {operation} · завершено · {label}: {count}"
        return None
    if kind == "nutrition_cart_skipped":
        code = details.get("reason")
        reason = CART_SKIP_REASONS.get(code) if isinstance(code, str) else None
        return f"Корзина ВкусВилла не собрана  {reason}" if reason else None
    if kind == "nutrition_selection_failed":
        code = details.get("reason")
        reason = SELECTION_FAILURE_REASONS.get(code) if isinstance(code, str) else None
        return f"Подбор товаров  {reason}" if reason else None
    if kind == "fact_changed":
        action = {
            "added": "Факт добавлен",
            "replaced": "Факт обновлён",
            "updated": "Факт обновлён",
            "confirmed": "Актуальность факта подтверждена",
            "revoked": "Факт отозван",
            "deleted": "Факт удалён",
        }.get(details.get("action"))
        fact_id, previous = details.get("fact_id"), details.get("previous_fact_id")
        if not action or not integer(fact_id, 1):
            return None
        if details.get("action") == "replaced":
            if not integer(previous, 1) or previous == fact_id:
                return None
            return f"Факт №{fact_id} обновлён"
        if details.get("action") == "updated":
            return f"Факт №{fact_id} обновлён"
        return f"{action}  №{fact_id}"
    elif kind == "fact_change_pending":
        action_id, ids = details.get("action_id"), details.get("fact_ids")
        if (
            integer(action_id, 1)
            and isinstance(ids, list)
            and ids
            and all(integer(i, 1) for i in ids)
            and len(ids) == len(set(ids))
        ):
            return f"Уточнение замены фактов  номера: {', '.join(str(i) for i in ids)} · предложение №{action_id}"
    elif kind == "profile_updated":
        profile_id, goal_id = details.get("profile_id"), details.get("goal_id")
        if integer(profile_id, 1) and integer(goal_id, 1):
            return f"Профиль обновлён  снимок №{profile_id} · цель №{goal_id}"
    elif kind == "goal_plan_updated" and integer(details.get("goal_id"), 1):
        return f"План цели обновлён  цель №{details['goal_id']}"
    elif kind == "topic_routed":
        previous, current = details.get("previous_topic_id"), details.get("topic_id")
        action = {
            "switch": "новая тема",
            "resume": "возврат",
            "detour": "отступление",
            "mixed": "связанный вопрос",
        }.get(details.get("action"))
        if action and integer(previous, 1) and integer(current, 1) and previous != current:
            return f"Тема изменена  №{previous} → №{current} · {action}"
    elif kind == "checkpoint_used":
        topic, first, last = (
            details.get(key) for key in ("topic_id", "first_message_id", "covered_message_id")
        )
        if all(integer(value, 1) for value in (topic, first, last)) and first <= last:
            related = " · связанная тема" if details.get("related") is True else ""
            return (
                f"Checkpoint подключён  тема №{topic} · границы сообщений №{first}–№{last}{related}"
            )
    elif kind == "facts_read":
        ids = details.get("fact_ids")
        if isinstance(ids, list) and ids and all(integer(value, 1) for value in ids):
            if len(ids) != len(set(ids)):
                return None
            labels = ", ".join(f"№{value}" for value in ids[:12])
            if len(ids) > 12:
                labels += f" и ещё {len(ids) - 12}"
            return f"Факты прочитаны в контекст  {labels} · всего: {len(ids)}"
    elif kind == "history_compacted":
        before, after = details.get("before_bytes"), details.get("after_bytes")
        if integer(before, 1) and integer(after) and after < before:
            return f"История сжата  {before:,} → {after:,} байт UTF-8".replace(",", " ")
    elif kind in {"safety_checked", "safety_failed"}:
        stage = SAFETY_STAGES.get(details.get("stage"))
        if not stage:
            return None
        if kind == "safety_checked":
            status = {"warning": "предупреждение", "block": "заблокировано"}.get(
                details.get("status")
            )
            if status:
                return f"Проверка безопасности  {stage} · {status}"
        elif isinstance(details.get("type"), str) and details["type"]:
            code = details.get("reason")
            reason = SAFETY_FAILURE_REASONS.get(code) if isinstance(code, str) else None
            explanation = f" · причина: {reason}" if reason else ""
            return (
                f"Ошибка проверки безопасности  {stage}{explanation}"
                " · запрос остановлен; повторите попытку"
            )
    elif kind == "memory_failed" and isinstance(details.get("type"), str) and details["type"]:
        return "Ошибка проверки памяти  факты не изменены; повторите запрос"
    return None


def format_activity(row):
    try:
        details = json.loads(row["details"])
        message = activity_message(row["kind"], details)
        if not message:
            return None
        stamp = datetime.fromisoformat(row["created_at"].replace("Z", "+00:00"))
        return f"{stamp.isoformat(timespec='seconds')}  {message}"
    except (ValueError, TypeError, KeyError, RecursionError):
        return None
