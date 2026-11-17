"""Shared journal, measurement and chat-state persistence primitives."""

import json
import math

from fitagent.memory_schema import parse_stamp


def initialize_state(db):
    db.executescript("""
        CREATE TABLE IF NOT EXISTS chat_state (key TEXT PRIMARY KEY, value TEXT NOT NULL);
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
    ):
        if name not in {r["name"] for r in db.execute(f"PRAGMA table_info({table})")}:
            db.execute(f"ALTER TABLE {table} ADD COLUMN {name} {definition}")


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
    bump_revision(db)


def current_weight(db):
    rows = weight_rows(db, 1)
    if rows:
        return {"weight": rows[0]["weight"], "measured_at": rows[0]["confirmed_at"]}
    return None


def weight_rows(db, limit=30):
    try:
        records = []
        for row in db.execute(
            "SELECT id,value,valid_from FROM sticky_facts WHERE category='measurements' AND fact_key LIKE 'weight_%' AND status='active'"
        ):
            value = json.loads(row["value"])["quantity"]
            if (
                type(value) not in (int, float)
                or not math.isfinite(value)
                or not 25 <= value <= 400
            ):
                raise ValueError
            stamp = parse_stamp(row["valid_from"])
            records.append((stamp, row["id"], {"confirmed_at": row["valid_from"], "weight": value}))
        return [item[2] for item in sorted(records, key=lambda item: item[:2])[-limit:]]
    except (ValueError, TypeError, KeyError, OverflowError, RecursionError):
        raise RuntimeError(
            "Не удалось прочитать измерения веса. Проверьте данные или восстановите базу из резервной копии."
        ) from None
