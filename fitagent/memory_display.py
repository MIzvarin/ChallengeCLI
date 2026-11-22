"""Human-readable memory views; stored JSON and model payloads stay unchanged."""

import json

from fitagent.memory_schema import (
    CATEGORIES,
    VALUE_SCHEMA,
    parse_stamp,
    unique_object,
    validate_shape,
)

HISTORY_ERROR = (
    "Не удалось прочитать журнал факта. Проверьте данные или восстановите базу из резервной копии."
)
VALUE_ERROR = "Не удалось прочитать значение факта. Проверьте данные или восстановите базу из резервной копии."
ARCHIVE_ERROR = (
    "Не удалось прочитать архив памяти. Проверьте данные или восстановите базу из резервной копии."
)


def display_stamp(value):
    stamp = parse_stamp(value)
    if stamp is None:
        raise ValueError
    return stamp.strftime("%d.%m.%Y %H:%M:%S %z")


def display_value(value):
    try:
        validate_shape(value, VALUE_SCHEMA)
        text = value["text"].strip()
        if not text:
            raise ValueError
        details = []
        if value["quantity"] is not None:
            number = format(value["quantity"], "g").replace(".", ",")
            measure = " ".join(part for part in (number, value["unit"]) if part)
            if measure.casefold() not in text.casefold():
                details.append(measure)
        elif value["unit"] and value["unit"].casefold() not in text.casefold():
            details.append("единица измерения: " + value["unit"])
        if value["qualifier"] and value["qualifier"].casefold() not in text.casefold():
            details.append(value["qualifier"])
        return text + (" (" + "; ".join(details) + ")" if details else "")
    except (ValueError, TypeError, KeyError, OverflowError, RecursionError):
        raise ValueError(VALUE_ERROR) from None


def stored_value(raw):
    return display_value(json.loads(raw, object_pairs_hook=unique_object))


def display_history(fact_id, records):
    actions = {
        "update": "значение обновлено",
        "confirm": "актуальность подтверждена",
        "measurement": "измерение сохранено",
        "revoke": "факт отозван",
        "task_completed": "задача завершена",
        "goal_completed": "цель завершена",
        "snapshot": "снимок сохранённых данных при обновлении приложения",
    }
    lines = [f"Журнал изменений факта №{fact_id}"]
    try:
        for record in records:
            action = record["action"]
            label = actions.get(action, "изменение")
            if action == "update" and record["old_value"] is None:
                label = "факт добавлен"
            lines.extend(["", f"{display_stamp(record['recorded_at'])} · {label}"])
            if parse_stamp(record["effective_at"]) != parse_stamp(record["recorded_at"]):
                lines.append("Действует с: " + display_stamp(record["effective_at"]))
            if action == "confirm":
                lines.append("Значение: " + stored_value(record["new_value"]))
            else:
                if record["old_value"] is not None:
                    lines.append("Было: " + stored_value(record["old_value"]))
                if record["new_value"] is not None:
                    lines.append("Стало: " + stored_value(record["new_value"]))
                elif action in {"task_completed", "goal_completed"}:
                    lines.append("Стало: в архиве")
                else:
                    lines.append("Стало: факт отозван")
            if record["source_message_id"] is not None:
                lines.append(f"Источник: сообщение №{record['source_message_id']}")
    except (ValueError, TypeError, KeyError, IndexError, OverflowError, RecursionError):
        raise ValueError(HISTORY_ERROR) from None
    if len(lines) == 1:
        lines.extend(["", "Записанных изменений пока нет."])
    return "\n".join(lines)


def display_archives(records):
    lines = ["Архив среднесрочной памяти"]
    try:
        for record in records:
            data = json.loads(record["facts"], object_pairs_hook=unique_object)
            goal = data["goal"]["text"]
            if not isinstance(goal, str) or not goal.strip() or not isinstance(data["facts"], list):
                raise ValueError
            lines.extend(
                [
                    "",
                    f"Архив №{record['id']} · {display_stamp(record['created_at'])}",
                    "Цель: " + goal,
                ]
            )
            for fact in data["facts"]:
                category = CATEGORIES[fact["category"]]
                lines.append(f"  №{fact['id']} · {category}: {display_value(fact['value'])}")
            if not data["facts"]:
                lines.append("Закреплённых фактов в этом архиве нет.")
    except (ValueError, TypeError, KeyError, IndexError, OverflowError, RecursionError):
        raise ValueError(ARCHIVE_ERROR) from None
    if len(lines) == 1:
        lines.extend(["", "Архив пока пуст."])
    return "\n".join(lines)
