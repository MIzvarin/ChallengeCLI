"""Safe, concise public activity messages; the full audit stays separate."""

import json
from datetime import datetime

ACTIVITY_KINDS = ("checkpoint_used", "topic_routed", "facts_read", "history_compacted")


def integer(value, minimum=0):
    return type(value) is int and minimum <= value <= 2**63 - 1


def activity_message(kind, details):
    if not isinstance(details, dict):
        return None
    if kind == "topic_routed":
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
