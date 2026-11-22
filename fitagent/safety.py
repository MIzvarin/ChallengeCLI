"""Independent Luna checks; no raw safety reasoning is forwarded to the chat model."""

import json

from fitagent.memory_layers import inventory_facts
from fitagent.memory_schema import SAFETY_CATEGORIES, version_covers
from fitagent.safety_schema import SAFETY_ERROR, SAFETY_PROMPT, SafetyValidationError, parse_safety


def safety_rules(snapshot, now):
    return [
        {"rule_id": f"fact:{fact['id']}", "category": fact["category"], "value": fact["value"]}
        for fact in snapshot["facts"] + snapshot["previous_versions"]
        if fact["category"] in SAFETY_CATEGORIES | {"medications", "supplements"}
        and version_covers(fact, now)
    ]


class SafetyGuard:
    """Fail closed on transport/protocol errors; warnings alone do not block requests."""

    def __init__(self, store, client, clock):
        self.store, self.client, self.clock = store, client, clock

    def check(
        self,
        stage,
        question,
        snapshot,
        profile,
        draft=None,
        changes=None,
        confirmed=False,
        previous_snapshot=None,
        goal=None,
        dialogue=None,
        report_only=False,
    ):
        if report_only and stage not in {"goal_plan", "onboarding", "goal_proposal"}:
            raise ValueError("Нельзя отключить блокировку для этой проверки безопасности.")
        rules = safety_rules(snapshot, self.clock())
        previous_rules = safety_rules(previous_snapshot, self.clock()) if previous_snapshot else []
        current_rules = {rule["rule_id"]: rule for rule in rules}
        for rule in previous_rules:
            current = current_rules.get(rule["rule_id"])
            if current is not None and current["value"] != rule["value"]:
                # One stable fact number must not collapse old/new safety checks into one.
                rule["rule_id"] = "previous:" + rule["rule_id"].split(":", 1)[1]
        proposed_rules = [
            {
                "rule_id": f"proposed:{index}",
                "category": operation["category"],
                "value": operation["value"],
            }
            for index, operation in enumerate(changes if isinstance(changes, list) else [])
            if operation["category"] in SAFETY_CATEGORIES | {"medications", "supplements"}
            and operation["action"] == "upsert"
        ]
        required_rules = list(
            dict.fromkeys(
                ["system", *(rule["rule_id"] for rule in rules + previous_rules + proposed_rules)]
            )
        )
        payload = {
            "stage": stage,
            "question": question,
            "profile": profile,
            "goal": goal
            if goal is not None
            else (
                {"text": profile["goal"], "target": profile.get("target")}
                if "goal" in profile
                else self.store.latest("goals")
            ),
            "dialogue": dialogue,
            "rules": rules,
            "draft": draft,
            "changes": changes,
            "confirmed": confirmed,
            "previous_rules": previous_rules,
            "proposed_rules": proposed_rules,
            "context": inventory_facts(snapshot["facts"]),
            "required_rule_ids": required_rules,
        }
        phase = "service_response"
        try:
            raw = self.client.generate_safety(
                [
                    {"role": "system", "content": SAFETY_PROMPT},
                    {"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
                ],
                stage,
            )
            phase = "contract"
            result = parse_safety(
                raw,
                required_rules,
                exact=True,
            )
        except Exception as exc:
            details = {
                "stage": stage,
                "type": type(exc).__name__,
                "phase": phase,
                "reason": "service_error" if phase == "service_response" else "validation_error",
            }
            if isinstance(exc, SafetyValidationError):
                details.update(exc.diagnostics)
            self.store.event("safety_failed", details, self.clock())
            raise ValueError(SAFETY_ERROR) from None
        self.store.event(
            "safety_checked", {"stage": stage, "status": result["status"]}, self.clock()
        )
        public = {key: result[key] for key in ("status", "description")}
        if public["status"] == "block" and not report_only:
            action = {
                "onboarding": "Не могу сохранить профиль с такими данными.",
                "changes": "Не могу сохранить это изменение.",
                "response": "Не могу показать подготовленный ответ.",
                "goal_response": "Не могу показать эту оценку цели.",
            }.get(stage, "Не могу выполнить эту просьбу.")
            raise ValueError(
                action
                + "\nПричина: "
                + public["description"].strip()
                + "\nМожно попросить безопасный вариант или задать другой вопрос. "
                "Если сведения об ограничениях ошибочны, опишите исправление в чате — "
                "его нужно будет подтвердить. Сохранённые данные не изменены."
            )
        return public
