"""Explicit, revision-bound chat confirmations and memory lifecycle transitions."""

import json

from fitagent.domain import validate_profile
from fitagent.goal_state import EXECUTION, finish_goal, invalidate_goal, read_state
from fitagent.goal_workflow import completion_matches, fingerprint
from fitagent.memory import project
from fitagent.memory_layers import memory_layer
from fitagent.state_storage import journal


def write_actions(db, snapshot, now):
    for proposal in snapshot.get("proposals", []):
        action_id = db.execute(
            "INSERT INTO pending_actions(kind,payload,created_at,revision,session) VALUES (?,?,?,?,?)",
            (
                proposal["kind"],
                json.dumps(proposal["payload"], ensure_ascii=False),
                now.isoformat(),
                snapshot["revision"] + 1,
                snapshot["session"],
            ),
        ).lastrowid
        snapshot.setdefault("action_ids", []).append(action_id)


def close_task(db, topic_id, now):
    stamp = now.isoformat()
    db.execute("UPDATE topics SET closed_at=? WHERE id=?", (stamp, topic_id))
    db.execute("DELETE FROM checkpoints WHERE topic_id=?", (topic_id,))
    db.execute(
        "DELETE FROM memory_issues WHERE (applicability='topic' AND topic_id=?) OR (source_topic_id=? AND category IN ('symptoms','recovery'))",
        (topic_id, topic_id),
    )
    db.execute("DELETE FROM summaries")
    rows = db.execute(
        "SELECT * FROM sticky_facts WHERE status='active' AND source_topic_id=?", (topic_id,)
    ).fetchall()
    for row in rows:
        if memory_layer(row["category"]) == "short":
            db.execute(
                "UPDATE sticky_facts SET status='archived',superseded_at=? WHERE lineage_id=?",
                (stamp, row["lineage_id"]),
            )
            journal(
                db,
                row["lineage_id"],
                stamp,
                stamp,
                "task_completed",
                json.loads(row["value"]),
                None,
            )


def archive_goal(db, snapshot, now):
    stamp = now.isoformat()
    goal = db.execute(
        "SELECT * FROM goals WHERE completed_at IS NULL ORDER BY id DESC LIMIT 1"
    ).fetchone()
    if goal is None:
        raise ValueError("Активной цели нет. Опишите новую цель в чате.")
    finish_goal(db, goal["id"])
    facts = [fact for fact in snapshot["facts"] if memory_layer(fact["category"]) == "medium"]
    db.execute(
        "INSERT INTO memory_archives(goal_id,created_at,facts) VALUES (?,?,?)",
        (goal["id"], stamp, json.dumps({"goal": dict(goal), "facts": facts}, ensure_ascii=False)),
    )
    for fact in facts:
        if fact["category"] == "measurements":
            continue
        # Current global routines and regimens remain valid; goal-specific plans do not.
        if fact["applicability"] == "global" and fact["category"] != "agreements":
            continue
        db.execute(
            "UPDATE sticky_facts SET status='archived',superseded_at=? WHERE lineage_id=?",
            (stamp, fact["lineage_id"]),
        )
        journal(db, fact["lineage_id"], stamp, stamp, "goal_completed", fact["value"], None)
        db.execute(
            "DELETE FROM memory_issues WHERE category=? AND fact_key=? AND scope=? AND applicability=? AND topic_id IS ?",
            (fact["category"], fact["key"], fact["scope"], fact["applicability"], fact["topic_id"]),
        )
    db.execute("UPDATE goals SET completed_at=? WHERE completed_at IS NULL", (stamp,))
    topics = [r[0] for r in db.execute("SELECT id FROM topics WHERE closed_at IS NULL")]
    for topic_id in topics:
        close_task(db, topic_id, now)
    db.execute("INSERT OR REPLACE INTO chat_state VALUES ('memory_epoch',?)", (stamp,))
    cutoff = db.execute("SELECT COALESCE(MAX(id),0) FROM changes").fetchone()[0]
    db.execute("INSERT OR REPLACE INTO chat_state VALUES ('memory_change_id',?)", (str(cutoff),))
    db.execute("DELETE FROM pending_actions")


class ChatActions:
    def __init__(self, service):
        self.service, self.store = service, service.store

    def profile_from_patch(self, patch, memory):
        profile = self.service.profile_for_memory(memory)
        goal = self.store.latest("goals") or {}
        values = {key: profile[key] for key in ("height", "weight", "sex", "birth_date")}
        values.update(goal=goal.get("text", ""), target=goal.get("target"))
        for key in ("birth_date", "height", "sex", "goal", "target"):
            if patch[key] is not None:
                values[key] = patch[key]
        if patch["clear_target"]:
            values["target"] = None
        return validate_profile(**values, now=self.service.clock())

    def prepare(self, snapshot, question, dialogue=None):
        proposals = []
        patch = snapshot.get("profile_change")
        if patch is not None:
            if patch["source_quote"] not in question or not patch["source_quote"].strip():
                raise ValueError(
                    "Изменение профиля не подтверждено вашим сообщением. Уточните изменения."
                )
            if (
                not any(
                    patch[key] is not None
                    for key in ("birth_date", "height", "sex", "goal", "target")
                )
                and not patch["clear_target"]
            ):
                raise ValueError(
                    "Не указано, какое поле профиля изменить. Опишите изменение в чате."
                )
            if patch["target"] is not None and patch["clear_target"]:
                raise ValueError(
                    "Целевой вес: укажите новое значение или отмену, но не оба действия сразу."
                )
            profile = self.profile_from_patch(patch, snapshot)
            managed = snapshot.get("goal_managed", False)
            result = self.service.safety.check(
                "goal_proposal" if managed else "changes",
                question,
                snapshot,
                profile,
                changes=patch,
                dialogue=dialogue,
                report_only=managed,
            )
            if managed:
                warning = "Цель и план потребуют валидации до выполнения. "
                if result["status"] != "allow":
                    warning += result["description"]
            else:
                warning = self.service.assess_goal(
                    profile, safety_result=result, memory=snapshot, dialogue=dialogue
                )
            proposals.append(
                {
                    "kind": "profile",
                    "payload": {
                        "profile": profile,
                        "warning": warning,
                        "question": question,
                        "dialogue": dialogue,
                        "goal_managed": managed,
                    },
                }
            )
        if snapshot.get("pending_safety"):
            proposals.append(
                {
                    "kind": "safety",
                    "payload": {
                        "operations": snapshot["pending_safety"],
                        "question": question,
                        "dialogue": dialogue,
                    },
                }
            )
        completion = snapshot.get("completion", "none")
        if snapshot.get("goal_managed") and completion == "goal":
            flow = snapshot.get("goal_flow")
            state = flow[0] if flow is not None else self.service.workflow.state()
            if state is None or state["stage"] != EXECUTION:
                completion = "none"
        if completion != "none":
            payload = {"topic_id": snapshot["dialogue"]["topic_id"]}
            if completion == "goal":
                goal = self.store.latest("goals")
                if goal:
                    flow = snapshot.get("goal_flow")
                    state = flow[0] if flow is not None else self.service.workflow.state(goal)
                    payload.update(self.service.workflow.completion_payload(goal, state, snapshot))
            proposals.append({"kind": completion, "payload": payload})
        snapshot["proposals"] = proposals

    @staticmethod
    def describe_proposals(snapshot):
        lines = []
        for proposal, action_id in zip(
            snapshot.get("proposals", []), snapshot.get("action_ids", [])
        ):
            kind, payload = proposal["kind"], proposal["payload"]
            if kind == "profile":
                description = "Изменить профиль/цель: " + json.dumps(
                    payload["profile"], ensure_ascii=False
                )
                if payload["warning"]:
                    description += "\nПредупреждение: " + payload["warning"]
            elif kind == "safety":
                description = "Изменить ограничения безопасности: " + "; ".join(
                    op["value"]["text"] for op in payload["operations"]
                )
                description += ". Внимание: исправляйте только ошибочные/изменившиеся сведения, не для обхода безопасности."
            elif kind == "task":
                description = "Завершить задачу и очистить её краткосрочную память? Переписка останется в истории."
            else:
                description = "Подтвердить достижение цели и архивировать среднесрочную память? История веса, ограничения и текущий режим сохранятся; новую цель задайте в чате."
            lines.append(
                f"{description}\nДля подтверждения: /confirm {action_id}; для отмены: /cancel {action_id}."
            )
        return "\n\n".join(lines)

    def command(self, text):
        parts = text.split()
        if (
            len(parts) != 2
            or parts[0] not in {"/confirm", "/cancel"}
            or not parts[1].isascii()
            or not parts[1].isdigit()
            or len(parts[1]) > 18
        ):
            raise ValueError("Введите /confirm НОМЕР или /cancel НОМЕР из предложения в чате.")
        with self.store.connect() as db:
            row = db.execute(
                "SELECT * FROM pending_actions WHERE id=?", (int(parts[1]),)
            ).fetchone()
        if row is None:
            raise ValueError(
                "Предложение не найдено или уже обработано. Сформулируйте изменение заново."
            )
        if parts[0] == "/cancel":
            with self.store.connect() as db:
                db.execute("BEGIN IMMEDIATE")
                db.execute("DELETE FROM pending_actions WHERE id=?", (row["id"],))
                if row["kind"] == "goal":
                    payload = json.loads(row["payload"])
                    goal = db.execute(
                        "SELECT id FROM goals WHERE completed_at IS NULL ORDER BY id DESC LIMIT 1"
                    ).fetchone()
                    if goal and goal[0] == payload.get("goal_id"):
                        state = read_state(db, goal[0])
                        if state["fingerprint"] != payload.get("data_fingerprint"):
                            invalidate_goal(db, goal[0])
            return "Предложение отменено. Данные не изменены."
        snapshot = self.service.memory.repository.load(self.service.clock())
        if row["revision"] != snapshot["revision"] or row["session"] != snapshot["session"]:
            raise ValueError(
                "После предложения данные изменились. Повторите запрос изменения для новой проверки."
            )
        payload = json.loads(row["payload"])
        if row["kind"] == "goal":
            goal = self.store.latest("goals")
            state = self.service.workflow.state(goal) if goal else None
            now = self.service.clock()
            profile = self.service.current_profile()
            if state is None or state["stage"] != EXECUTION:
                raise ValueError(
                    "Завершение доступно только при выполнении актуального проверенного плана. Сначала пройдите валидацию."
                )
            if not completion_matches(state, payload, fingerprint(goal, profile, snapshot, now)):
                raise ValueError(
                    "Предложение о завершении устарело: план или данные изменились либо предложение создано старой версией приложения. Сообщите о достижении цели ещё раз, чтобы получить новое подтверждение."
                )
        candidate = project(
            snapshot, payload.get("operations", []), None, self.service.clock(), manual=True
        )
        candidate["consume_action"] = row["id"]
        if row["kind"] in {"profile", "safety"}:
            self.service.memory.clock = self.service.clock
            self.service.memory.context(candidate)
            self.service.memory.check_inventory(candidate)
            profile = payload.get("profile") or self.service.current_profile()
            managed = payload.get("goal_managed", False)
            result = self.service.safety.check(
                "goal_proposal" if managed else "changes",
                payload["question"],
                candidate,
                profile,
                changes=payload,
                confirmed=True,
                previous_snapshot=snapshot,
                dialogue=payload.get("dialogue"),
                report_only=managed,
            )
            if row["kind"] == "profile":
                warning = payload["warning"]
                if not managed:
                    warning = self.service.checked_goal_warning(
                        profile, warning, memory=candidate, dialogue=payload.get("dialogue")
                    )
                self.store.save_profile(
                    profile,
                    self.service.clock(),
                    self.service.next_check(),
                    warning,
                    bool(warning) and not managed,
                    memory=candidate,
                    record_weight=False,
                )
            else:
                self.service.memory.repository.save_manual(candidate)
            notice = (
                "\nПредупреждение: " + result["description"]
                if result["status"] == "warning"
                else ""
            )
            if row["kind"] == "profile" and warning != payload["warning"]:
                notice += "\nПроверка цели: " + warning
            return "Память обновлена. Подтверждённое изменение сохранено в журнале." + notice
        with self.store.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            self.service.memory.repository._write(db, candidate)
            if row["kind"] == "task":
                close_task(db, payload["topic_id"], self.service.clock())
            else:
                archive_goal(db, snapshot, self.service.clock())
        return (
            "Задача завершена. Краткосрочная память очищена."
            if row["kind"] == "task"
            else "4/4 · Готово\nЦель завершена. Среднесрочная память архивирована, текущие данные сохранены. Опишите новую цель в чате."
        )
