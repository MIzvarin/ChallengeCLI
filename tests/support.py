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
