"""Explicit fake structured responses; no network or implicit safety bypasses in application code."""

import json

EMPTY_TURN = json.dumps(
    {
        "operations": [],
        "topic": {
            "action": "continue",
            "topic_id": None,
            "title": "",
            "related_topic_ids": [],
            "clarification": None,
        },
        "profile_change": None,
        "completion": "none",
    }
)


def safe_response(messages, stage):
    payload = json.loads(messages[1]["content"])
    return safety_json(
        list(
            dict.fromkeys(
                [
                    "system",
                    *(
                        rule["rule_id"]
                        for rule in payload["rules"]
                        + payload.get("previous_rules", [])
                        + payload.get("proposed_rules", [])
                    ),
                ]
            )
        )
    )


def safety_json(
    rules=("system",), status="allow", description="Проверка пройдена.", strictness="strict"
):
    return json.dumps(
        {
            "status": status,
            "description": description,
            "checks": [
                {
                    "rule_id": rule,
                    "strictness": strictness,
                    "status": status,
                    "description": "Краткое основание.",
                }
                for rule in rules
            ],
        },
        ensure_ascii=False,
    )


def seed_accepted_goal(service):
    """Explicit pre-existing, confirmed plan fixture for tests unrelated to planning."""
    from fitagent.goal_state import (
        EXECUTION,
        PLANNING,
        VALIDATION,
        read_state,
        transition,
        write_state,
    )
    from fitagent.goal_workflow import REQUIRED_CATEGORIES, fingerprint
    from fitagent.memory import project

    now = service.clock()
    snapshot = service.memory.repository.load(now)
    present = {fact["category"] for fact in snapshot["facts"]}
    operations = [
        {
            "action": "upsert",
            "update_mode": "replace",
            "fact_id": None,
            "category": category,
            "key": "fixture_report",
            "scope": "usual",
            "applicability": "global",
            "topic_id": None,
            "value": {
                "text": "Сообщено отсутствие ограничений/режима",
                "quantity": None,
                "unit": None,
                "qualifier": None,
            },
            "valid_from": None,
            "valid_until": None,
            "valid_until_action": "keep",
            "source_quote": "нет",
            "clarification": None,
        }
        for category in REQUIRED_CATEGORIES
        if category not in present
    ]
    service.memory.repository.save_manual(project(snapshot, operations, None, now, manual=True))
    goal = service.store.latest("goals")
    snapshot = service.memory.repository.load(now)
    with service.store.connect() as db:
        db.execute("BEGIN IMMEDIATE")
        state = read_state(db, goal["id"])
        if state["stage"] in {PLANNING, EXECUTION}:
            transition(state, VALIDATION)
        state.update(
            plan="Умеренные тренировки с отдыхом и контролем самочувствия.",
            criterion="Выполнить согласованный объём без ухудшения самочувствия.",
            assessment={"status": "allow", "description": "Проверка пройдена."},
            fingerprint=fingerprint(goal, service.current_profile(), snapshot, now),
            question="",
        )
        write_state(db, state, state["version"])
        state = read_state(db, goal["id"])
        transition(state, EXECUTION)
        write_state(db, state, state["version"])


def seed_legacy_fact_versions(store, versions):
    """Recreate actual old multi-row facts; current updates intentionally keep one row."""
    from fitagent.fact_storage import FACT_COLUMNS

    columns = (*FACT_COLUMNS[1:], "status", "superseded_at")
    root_id = versions[0]["id"]
    with store.connect() as db:
        for index, fact in enumerate(versions):
            following = versions[index + 1] if index + 1 < len(versions) else None
            values = dict(
                fact,
                fact_key=fact["key"],
                value=json.dumps(fact["value"], ensure_ascii=False),
                lineage_id=versions[0]["lineage_id"],
                status="replaced" if following else "active",
                superseded_at=following["valid_from"] if following else None,
            )
            if index == 0:
                db.execute(
                    f"UPDATE sticky_facts SET {','.join(column + '=?' for column in columns)} WHERE id=?",
                    [*[values[column] for column in columns], root_id],
                )
            else:
                new_id = db.execute(
                    f"INSERT INTO sticky_facts({','.join(columns)}) VALUES ({','.join('?' for _ in columns)})",
                    [values[column] for column in columns],
                ).lastrowid
                db.execute(
                    "UPDATE fact_sources SET fact_id=? WHERE fact_id=? AND message_id=?",
                    (new_id, root_id, fact["source_message_id"]),
                )


def allow_goal_continue(client):
    """Extend a fake extraction client with an explicit structured 'no plan changes' reply."""
    generator = client.generate_structured
    ordinary = generator.side_effect

    def structured(messages, schema, purpose="memory_extraction"):
        if purpose == "goal_planning":
            return json.dumps({"action": "continue", "question": "", "plan": "", "criterion": ""})
        if ordinary is not None:
            return ordinary(messages, schema)
        return generator.return_value

    generator.side_effect = structured
