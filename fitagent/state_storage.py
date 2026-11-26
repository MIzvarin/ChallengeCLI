"""Shared journal, measurement and chat-state persistence primitives."""

import hashlib
import json
import math

from fitagent.memory_schema import SAFETY_CATEGORIES, parse_stamp, reported_order, version_covers

DAILY_SAFETY_CATEGORIES = sorted(
    SAFETY_CATEGORIES | {"medications", "supplements", "symptoms", "recovery"}
)


def initialize_state(db):
    db.executescript("""
        CREATE TABLE IF NOT EXISTS chat_state (key TEXT PRIMARY KEY, value TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS daily_plans (
            day TEXT PRIMARY KEY, attempt TEXT NOT NULL, expires_at REAL NOT NULL,
            message_id INTEGER REFERENCES messages(id), input_key TEXT
        );
        CREATE TABLE IF NOT EXISTS pending_actions (
            id INTEGER PRIMARY KEY AUTOINCREMENT, kind TEXT NOT NULL, payload TEXT NOT NULL,
            created_at TEXT NOT NULL, revision INTEGER NOT NULL, session TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS memory_journal (
            id INTEGER PRIMARY KEY AUTOINCREMENT, lineage_id INTEGER NOT NULL,
            recorded_at TEXT NOT NULL, effective_at TEXT NOT NULL, action TEXT NOT NULL,
            old_value TEXT, new_value TEXT, source_message_id INTEGER
        );
        CREATE TABLE IF NOT EXISTS memory_archives (
            id INTEGER PRIMARY KEY AUTOINCREMENT, goal_id INTEGER, created_at TEXT NOT NULL,
            facts TEXT NOT NULL
        );
    """)
    for table, name, definition in (
        ("profiles", "birth_date", "TEXT"),
        ("goals", "completed_at", "TEXT"),
        ("topics", "closed_at", "TEXT"),
        ("daily_plans", "input_key", "TEXT"),
    ):
        if name not in {r["name"] for r in db.execute(f"PRAGMA table_info({table})")}:
            db.execute(f"ALTER TABLE {table} ADD COLUMN {name} {definition}")
    # Old databases may predate per-fact journals. Record the state observed now,
    # not an invented sequence of historical changes, and never rewrite an existing journal.
    db.execute(
        "INSERT INTO memory_journal(lineage_id,recorded_at,effective_at,action,new_value,source_message_id) "
        "SELECT f.lineage_id,strftime('%Y-%m-%dT%H:%M:%f+00:00','now'),f.valid_from,'snapshot',f.value,f.source_message_id "
        "FROM sticky_facts f WHERE f.id=(SELECT MAX(v.id) FROM sticky_facts v WHERE v.lineage_id=f.lineage_id) "
        "AND NOT EXISTS(SELECT 1 FROM memory_journal j WHERE j.lineage_id=f.lineage_id)"
    )


def daily_plan_input_key(db, now):
    """Identify the current safety inputs and active execution plan within a transaction."""
    from fitagent.goal_state import EXECUTION, plan_key, read_state

    marks = ",".join("?" for _ in DAILY_SAFETY_CATEGORIES)
    rules = [
        (row["id"], row["category"], row["value"])
        for row in db.execute(
            "SELECT id,category,value,valid_from,valid_until,superseded_at FROM sticky_facts "
            f"WHERE category IN ({marks}) AND "
            "(status='active' OR (status='replaced' AND superseded_at IS NOT NULL)) ORDER BY id",
            DAILY_SAFETY_CATEGORIES,
        )
        if version_covers(dict(row), now)
    ]
    profile_id = db.execute("SELECT MAX(id) FROM profiles").fetchone()[0]
    weight = current_weight(db)
    goal_id = db.execute("SELECT MAX(id) FROM goals WHERE completed_at IS NULL").fetchone()[0]
    execution = None
    if goal_id is not None:
        state = read_state(db, goal_id)
        if state["stage"] == EXECUTION:
            execution = plan_key(state)
            for row in db.execute(
                "SELECT payload FROM pending_actions WHERE kind='goal' "
                "AND revision=(SELECT CAST(value AS INTEGER) FROM settings WHERE key='memory_revision') "
                "AND session=(SELECT value FROM settings WHERE key='session')"
            ):
                try:
                    payload = json.loads(row["payload"])
                except (ValueError, RecursionError):
                    continue
                if (
                    isinstance(payload, dict)
                    and payload.get("goal_id") == goal_id
                    and payload.get("plan_key") == execution
                ):
                    execution = "paused"
                    break
    inputs = (profile_id, weight["weight"] if weight else None, rules, goal_id, execution)
    return hashlib.sha256(json.dumps(inputs, ensure_ascii=False).encode()).hexdigest()


def bump_revision(db):
    db.execute(
        "INSERT INTO settings VALUES ('memory_revision','1') ON CONFLICT(key) "
        "DO UPDATE SET value=CAST(value AS INTEGER)+1"
    )


def journal(db, lineage_id, now, effective_at, action, old, new, message_id=None):
    db.execute(
        "INSERT INTO memory_journal(lineage_id,recorded_at,effective_at,action,old_value,"
        "new_value,source_message_id) VALUES (?,?,?,?,?,?,?)",
        (
            lineage_id,
            now,
            effective_at,
            action,
            json.dumps(old, ensure_ascii=False) if old is not None else None,
            json.dumps(new, ensure_ascii=False) if new is not None else None,
            message_id,
        ),
    )


def record_activity(db, kind, details, stamp):
    db.execute(
        "INSERT INTO events(created_at,kind,details) VALUES (?,?,?)",
        (stamp, kind, json.dumps(details, ensure_ascii=False)),
    )


def profile_weight(db, weight, stamp, profile_id, source_id=None):
    """A profile confirmation records a measurement, never overwrites previous measurements."""
    value = {"text": f"Вес {weight:g} кг", "quantity": weight, "unit": "кг", "qualifier": None}
    source = (
        db.execute(
            "SELECT content,created_at FROM messages WHERE id=? AND role='user'", (source_id,)
        ).fetchone()
        if source_id is not None
        else None
    )
    measured_at = source["created_at"] if source else stamp
    fact_id = db.execute(
        "INSERT INTO sticky_facts(category,fact_key,scope,value,valid_from,created_at,updated_at,"
        "origin,applicability,source_message_id) VALUES ('measurements',?,'usual',?,?,?,?,'profile','global',?)",
        (
            f"weight_profile_{profile_id}",
            json.dumps(value, ensure_ascii=False),
            measured_at,
            stamp,
            stamp,
            source_id if source else None,
        ),
    ).lastrowid
    db.execute("UPDATE sticky_facts SET lineage_id=? WHERE id=?", (fact_id, fact_id))
    if source:
        db.execute(
            "INSERT INTO fact_sources(fact_id,message_id,quote) VALUES (?,?,?)",
            (fact_id, source_id, source["content"]),
        )
    journal(
        db, fact_id, stamp, measured_at, "measurement", None, value, source_id if source else None
    )
    record_activity(db, "fact_changed", {"action": "added", "fact_id": fact_id}, stamp)
    bump_revision(db)


def current_weight(db):
    rows = weight_rows(db, 1, by_report=True)
    if rows:
        return {"weight": rows[0]["weight"], "measured_at": rows[0]["confirmed_at"]}
    return None


def weight_rows(db, limit=30, by_report=False):
    try:
        records = []
        for row in db.execute(
            "SELECT id,value,valid_from,updated_at,source_message_id FROM sticky_facts WHERE category='measurements' AND fact_key LIKE 'weight_%' AND status='active'"
        ):
            value = json.loads(row["value"])["quantity"]
            if (
                type(value) not in (int, float)
                or not math.isfinite(value)
                or not 25 <= value <= 400
            ):
                raise ValueError
            stamp = parse_stamp(row["valid_from"])
            order = reported_order(dict(row)) if by_report else (stamp, row["id"])
            records.append((order, {"confirmed_at": row["valid_from"], "weight": value}))
        return [item[1] for item in sorted(records, key=lambda item: item[0])[-limit:]]
    except (ValueError, TypeError, KeyError, OverflowError, RecursionError):
        raise RuntimeError(
            "Не удалось прочитать измерения веса. Проверьте данные или восстановите базу из резервной копии."
        ) from None
