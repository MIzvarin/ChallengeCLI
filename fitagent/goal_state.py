"""Persisted goal pipeline and transactional compare-and-swap primitives."""

import hashlib
import json

from fitagent.memory_schema import object_schema, string_schema, unique_object, validate_shape

PLANNING = "planning"
VALIDATION = "validation"
EXECUTION = "execution"
DONE = "done"
TRANSITIONS = {
    PLANNING: {VALIDATION},
    VALIDATION: {EXECUTION},
    EXECUTION: {VALIDATION, DONE},
    DONE: set(),
}
LABELS = {
    PLANNING: "1/4 · Планирование",
    VALIDATION: "2/4 · Валидация",
    EXECUTION: "3/4 · Выполнение",
    DONE: "4/4 · Готово",
}
ASSESSMENT_SCHEMA = object_schema(
    {
        "status": {"type": "string", "enum": ["allow", "warning", "block"]},
        "description": {**string_schema(2000), "minLength": 1},
    }
)
STATE_SCHEMA = object_schema(
    {
        "goal_id": {"type": "integer", "minimum": 1},
        "stage": {"type": "string", "enum": list(TRANSITIONS)},
        "version": {"type": "integer", "minimum": 0},
        "plan": string_schema(8000),
        "criterion": string_schema(1000),
        "assessment": dict(ASSESSMENT_SCHEMA, type=["object", "null"]),
        "fingerprint": {"type": ["string", "null"], "pattern": "^[0-9a-f]{64}$"},
        "question": string_schema(1000),
    }
)


def initial_state(goal_id):
    return {
        "goal_id": goal_id,
        "stage": PLANNING,
        "version": 0,
        "plan": "",
        "criterion": "",
        "assessment": None,
        "fingerprint": None,
        "question": "",
    }


def plan_key(state):
    """Identify plan contents and their assessment, independently of read-only openings."""
    payload = {
        key: state[key] for key in ("goal_id", "plan", "criterion", "assessment", "fingerprint")
    }
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, ensure_ascii=False).encode()
    ).hexdigest()


def check_execution(state):
    if (
        not state["plan"]
        or not state["criterion"]
        or not state["fingerprint"]
        or not state["assessment"]
        or state["assessment"]["status"] not in {"allow", "warning"}
    ):
        raise ValueError("Сначала проверьте план и устраните опасность. Этап не изменён.")


def transition(state, destination):
    if destination not in TRANSITIONS[state["stage"]]:
        raise ValueError("Этот переход этапов цели запрещён. Продолжите текущий этап.")
    if destination == EXECUTION:
        check_execution(state)
    state["stage"] = destination


def reset_assessment(state):
    """Discard cached validation, moving execution back to validation if needed."""
    if state["stage"] == EXECUTION:
        transition(state, VALIDATION)
    state.update(assessment=None, fingerprint=None, question="")


def read_state(db, goal_id):
    row = db.execute(
        "SELECT value FROM chat_state WHERE key=?", (f"goal_flow:{goal_id}",)
    ).fetchone()
    if row is None:
        return initial_state(goal_id)
    try:
        state = json.loads(row[0], object_pairs_hook=unique_object)
        validate_shape(state, STATE_SCHEMA)
        if state["goal_id"] != goal_id:
            raise ValueError
        if state["stage"] == EXECUTION:
            check_execution(state)
        return state
    except (ValueError, KeyError, TypeError, OverflowError, RecursionError):
        raise ValueError(
            "Не удалось прочитать состояние цели. Перезапустите приложение; если ошибка повторяется, восстановите базу из резервной копии."
        ) from None


def write_state(db, state, expected_version):
    try:
        validate_shape(state, STATE_SCHEMA)
    except (ValueError, TypeError, KeyError):
        raise ValueError(
            "Не удалось сохранить этап цели. Данные не изменены; повторите сообщение."
        ) from None
    current = read_state(db, state["goal_id"])
    if current["version"] != expected_version:
        raise ValueError("План изменился другим запросом. Повторите сообщение для новой проверки.")
    if state["stage"] != current["stage"]:
        # A single turn can plan and validate, but cannot skip a pipeline stage.
        if state["stage"] not in TRANSITIONS[current["stage"]]:
            raise ValueError("Этот переход этапов цели запрещён. Продолжите текущий этап.")
    db.execute(
        "INSERT OR REPLACE INTO chat_state VALUES (?,?)",
        (
            f"goal_flow:{state['goal_id']}",
            json.dumps(dict(state, version=expected_version + 1), ensure_ascii=False),
        ),
    )


def write_goal_turn(db, snapshot):
    pending = snapshot.get("goal_flow")
    if pending is None:
        return
    state, version = pending
    active = db.execute(
        "SELECT id FROM goals WHERE completed_at IS NULL ORDER BY id DESC LIMIT 1"
    ).fetchone()
    if not active or active[0] != state["goal_id"]:
        raise ValueError("Цель изменилась. Повторите сообщение для актуального плана.")
    write_state(db, state, version)
    if state["stage"] == EXECUTION:
        db.execute(
            "UPDATE goals SET warning=?,risk_confirmed=? WHERE id=?",
            (
                state["assessment"]["description"]
                if state["assessment"]["status"] == "warning"
                else "",
                int(state["assessment"]["status"] == "warning"),
                state["goal_id"],
            ),
        )


def invalidate_goal(db, goal_id):
    state = read_state(db, goal_id)
    if state["stage"] == DONE:
        return
    version = state["version"]
    reset_assessment(state)
    write_state(db, state, version)


def finish_goal(db, goal_id):
    state = read_state(db, goal_id)
    version = state["version"]
    transition(state, DONE)
    write_state(db, state, version)
