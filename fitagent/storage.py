"""Private SQLite persistence and structured audit events."""

import json
import os
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from uuid import uuid4

from fitagent.activity import ACTIVITY_KINDS, format_activity
from fitagent.fact_storage import initialize_memory
from fitagent.state_storage import (
    current_weight,
    daily_plan_input_key,
    initialize_state,
    profile_weight,
    record_activity,
    weight_rows,
)
from fitagent.tokens import collect_usage
from fitagent.topic_storage import initialize_topics


class Storage:
    """Single-user storage; each operation owns its connection for worker safety."""

    def __init__(self, directory):
        self.directory = Path(directory).expanduser()
        self.directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.path = self.directory / "fitagent.sqlite3"
        with self.connect() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS profiles (
                    id INTEGER PRIMARY KEY, height REAL NOT NULL, weight REAL NOT NULL,
                    sex TEXT NOT NULL, age INTEGER NOT NULL, confirmed_at TEXT NOT NULL,
                    next_check TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS goals (
                    id INTEGER PRIMARY KEY, text TEXT NOT NULL, target REAL,
                    created_at TEXT NOT NULL, warning TEXT NOT NULL,
                    risk_confirmed INTEGER NOT NULL
                );
                CREATE TABLE IF NOT EXISTS messages (
                    id INTEGER PRIMARY KEY, day TEXT NOT NULL, created_at TEXT NOT NULL,
                    role TEXT NOT NULL, content TEXT NOT NULL, status TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS changes (
                    id INTEGER PRIMARY KEY, day TEXT NOT NULL, created_at TEXT NOT NULL,
                    kind TEXT NOT NULL, data TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS summaries (
                    cache_key TEXT PRIMARY KEY, content TEXT NOT NULL, created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS events (
                    id INTEGER PRIMARY KEY, created_at TEXT NOT NULL,
                    kind TEXT NOT NULL, details TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
                CREATE INDEX IF NOT EXISTS messages_time ON messages(created_at);
                CREATE INDEX IF NOT EXISTS changes_time ON changes(created_at);
            """)
            initialize_memory(db)
            initialize_topics(db)
            initialize_state(db)
        os.chmod(self.path, 0o600)

    @contextmanager
    def connect(self):
        connection = sqlite3.connect(self.path, timeout=10)
        connection.row_factory = sqlite3.Row
        try:
            with connection:
                yield connection
        finally:
            connection.close()

    def latest(self, table):
        if table not in {"profiles", "goals"}:
            raise ValueError("Unknown table")
        with self.connect() as db:
            where = " WHERE completed_at IS NULL" if table == "goals" else ""
            row = db.execute(f"SELECT * FROM {table}{where} ORDER BY id DESC LIMIT 1").fetchone()
            result = dict(row) if row else None
            if result and table == "profiles":
                weight = current_weight(db)
                if weight:
                    result.update(weight)
                elif (
                    isinstance(result["weight"], (int, float))
                    and 25 <= result["weight"] <= 400
                    and not db.execute(
                        "SELECT 1 FROM sticky_facts WHERE category='measurements' "
                        "AND fact_key LIKE 'weight_%' LIMIT 1"
                    ).fetchone()
                ):
                    result.update(measured_at=None, weight_recorded_at=result["confirmed_at"])
                else:
                    result.update(weight=None, measured_at=None)
        return result

    def save_profile(
        self,
        profile,
        now,
        next_date,
        warning="",
        confirmed=False,
        memory=None,
        setup_done=False,
        record_weight=True,
        wake_time=None,
    ):
        stamp = now.isoformat()
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            if memory is not None:
                from fitagent.fact_storage import FactRepository

                FactRepository(self)._write(db, memory)
            profile_id = db.execute(
                "INSERT INTO profiles(height,weight,sex,age,confirmed_at,next_check,birth_date) "
                "VALUES (?,?,?,?,?,?,?)",
                (
                    *[profile[k] for k in ("height", "weight", "sex", "age")],
                    stamp,
                    next_date,
                    profile["birth_date"],
                ),
            ).lastrowid
            if record_weight:
                profile_weight(
                    db,
                    profile["weight"],
                    stamp,
                    profile_id,
                    memory.get("weight_source_message_id") if memory else None,
                )
            old = db.execute(
                "SELECT * FROM goals WHERE completed_at IS NULL ORDER BY id DESC LIMIT 1"
            ).fetchone()
            if not old or (old["text"], old["target"]) != (profile["goal"], profile["target"]):
                db.execute("UPDATE goals SET completed_at=? WHERE completed_at IS NULL", (stamp,))
                db.execute(
                    "INSERT INTO goals(text,target,created_at,warning,risk_confirmed) "
                    "VALUES (?,?,?,?,?)",
                    (profile["goal"], profile["target"], stamp, warning, int(confirmed)),
                )
            from fitagent.goal_state import invalidate_goal

            active = db.execute(
                "SELECT id FROM goals WHERE completed_at IS NULL ORDER BY id DESC LIMIT 1"
            ).fetchone()
            invalidate_goal(db, active[0])
            record_activity(
                db,
                "profile_updated",
                {"profile_id": profile_id, "goal_id": active[0]},
                stamp,
            )
            db.execute(
                "INSERT INTO changes(day,created_at,kind,data) VALUES (?,?,?,?)",
                (now.date().isoformat(), stamp, "profile_confirmed", json.dumps(profile)),
            )
            if setup_done:
                self._set_wake_time(db, wake_time)
                db.execute("DELETE FROM chat_state WHERE key='onboarding'")
                db.execute(
                    "INSERT OR REPLACE INTO chat_state VALUES ('onboarding_finished','true')"
                )

    def defer(self, next_date):
        with self.connect() as db:
            db.execute(
                "UPDATE profiles SET next_check=? WHERE id=(SELECT MAX(id) FROM profiles)",
                (next_date,),
            )

    def message(self, role, content, now, status="complete"):
        with self.connect() as db:
            cursor = db.execute(
                "INSERT INTO messages(day,created_at,role,content,status) VALUES (?,?,?,?,?)",
                (now.date().isoformat(), now.isoformat(), role, content, status),
            )
            return cursor.lastrowid

    def set_status(self, message_id, status):
        with self.connect() as db:
            db.execute("UPDATE messages SET status=? WHERE id=?", (status, message_id))

    def chat_messages(self, *, now=None):
        """Return every saved chat message, independently of the LLM context window."""
        with self.connect() as db:
            db.execute("BEGIN")
            current = None
            day = now.date().isoformat() if now is not None else None
            if day:
                current = db.execute(
                    "SELECT message_id FROM daily_plans WHERE day=? AND input_key=?",
                    (day, daily_plan_input_key(db, now)),
                ).fetchone()
            rows = db.execute(
                "SELECT m.id,m.created_at,m.role,m.content,"
                "CASE WHEN m.status='daily_plan' AND m.day=? AND m.id IS NOT ? "
                "THEN 'outdated_daily_plan' ELSE m.status END AS status,"
                "tr.action AS topic_action,t.title AS topic_title "
                "FROM messages m LEFT JOIN topic_transitions tr ON tr.message_id=m.id "
                "LEFT JOIN topics t ON t.id=tr.topic_id ORDER BY m.id",
                (day, current["message_id"] if current else None),
            ).fetchall()
        return [dict(row) for row in rows]

    def saved_baskets(self, after_id=0, *, latest=False, include_daily=False, now=None):
        """Read completed baskets and, for chat synchronization, daily plans."""
        order = "DESC LIMIT 1" if latest else "ASC"
        daily = " OR status='daily_plan'" if include_daily else ""
        with self.connect() as db:
            db.execute("BEGIN")
            current = ""
            params = [after_id, "Корзина ВкусВилла: <https://vkusvill.ru/"]
            if now is not None:
                current = (
                    " AND (status!='daily_plan' OR day!=? OR id IN "
                    "(SELECT message_id FROM daily_plans WHERE day=? AND input_key=?))"
                )
                day = now.date().isoformat()
                params.extend((day, day, daily_plan_input_key(db, now)))
            rows = db.execute(
                "SELECT id,content,status FROM messages WHERE id>? AND role='assistant' "
                "AND ((status IN ('complete','daily_plan') AND instr(content, ?) > 0)"
                + daily
                + ")"
                + current
                + " ORDER BY id "
                + order,
                params,
            ).fetchall()
        return [dict(row) for row in rows]

    def wake_time(self):
        with self.connect() as db:
            row = db.execute("SELECT value FROM settings WHERE key='wake_time'").fetchone()
        return row[0] if row else None

    @staticmethod
    def _set_wake_time(db, value):
        db.execute("INSERT OR REPLACE INTO settings VALUES ('wake_time',?)", (value or "",))

    def set_wake_time(self, value):
        with self.connect() as db:
            self._set_wake_time(db, value)

    def daily_plan_status(self, now):
        day = now.date().isoformat()
        with self.connect() as db:
            db.execute("BEGIN")
            row = db.execute("SELECT * FROM daily_plans WHERE day=?", (day,)).fetchone()
            if row and row["message_id"] is None and row["expires_at"] > now.timestamp():
                return "pending"
            if (
                row
                and row["message_id"] is not None
                and row["input_key"] == daily_plan_input_key(db, now)
            ):
                return "saved"
            return "missing"

    def current_daily_plan(self, now, *, before_id):
        with self.connect() as db:
            db.execute("BEGIN")
            row = db.execute(
                "SELECT m.content FROM daily_plans d JOIN messages m ON m.id=d.message_id "
                "WHERE d.day=? AND d.input_key=? AND m.id<?",
                (now.date().isoformat(), daily_plan_input_key(db, now), before_id),
            ).fetchone()
        return row["content"] if row else ""

    def reserve_daily_plan(self, now):
        day = now.date().isoformat()
        attempt = str(uuid4())
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            input_key = daily_plan_input_key(db, now)
            row = db.execute(
                "SELECT d.expires_at,d.input_key,m.content FROM daily_plans d "
                "LEFT JOIN messages m ON m.id=d.message_id WHERE d.day=?",
                (day,),
            ).fetchone()
            if row:
                if row["content"] is not None and row["input_key"] == input_key:
                    return None, row["content"]
                if row["content"] is None and row["expires_at"] > now.timestamp():
                    return None, None
            db.execute(
                "INSERT OR REPLACE INTO daily_plans(day,attempt,expires_at,input_key) VALUES (?,?,?,?)",
                (day, attempt, now.timestamp() + 30 * 60, input_key),
            )
        return attempt, None

    def finish_daily_plan(self, day, attempt, content, now):
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            if not db.execute(
                "SELECT 1 FROM daily_plans WHERE day=? AND attempt=? AND message_id IS NULL AND input_key=?",
                (day, attempt, daily_plan_input_key(db, now)),
            ).fetchone():
                return False
            message = db.execute(
                "INSERT INTO messages(day,created_at,role,content,status) "
                "VALUES (?,?,'assistant',?,'daily_plan')",
                (day, now.isoformat(), content),
            ).lastrowid
            db.execute("UPDATE daily_plans SET message_id=? WHERE day=?", (message, day))
            return True

    def release_daily_plan(self, day, attempt):
        with self.connect() as db:
            db.execute(
                "DELETE FROM daily_plans WHERE day=? AND attempt=? AND message_id IS NULL",
                (day, attempt),
            )

    def history(self, since):
        with self.connect() as db:
            messages = db.execute(
                "SELECT created_at,role,content FROM messages "
                "WHERE created_at>=? AND status='complete' ORDER BY id",
                (since,),
            ).fetchall()
            changes = db.execute(
                "SELECT created_at,kind,data FROM changes WHERE created_at>=? ORDER BY id", (since,)
            ).fetchall()
        items = [(r["created_at"], f"{r['role']}: {r['content']}") for r in messages]
        items += [(r["created_at"], f"{r['kind']}: {r['data']}") for r in changes]
        return [f"{stamp} {text}" for stamp, text in sorted(items, key=lambda item: item[0])]

    def cache_get(self, key):
        with self.connect() as db:
            row = db.execute("SELECT content FROM summaries WHERE cache_key=?", (key,)).fetchone()
        return row[0] if row else None

    def profile_history(self, since):
        """Confirmed profile changes are global data, not a different conversation topic."""
        with self.connect() as db:
            epoch = db.execute("SELECT value FROM chat_state WHERE key='memory_epoch'").fetchone()
            if epoch and epoch[0] > since:
                since = epoch[0]
            cutoff = db.execute(
                "SELECT value FROM chat_state WHERE key='memory_change_id'"
            ).fetchone()
            rows = db.execute(
                "SELECT created_at,kind,data FROM changes WHERE created_at>=? AND id>? ORDER BY id",
                (since, int(cutoff[0]) if cutoff else 0),
            ).fetchall()
        return [f"{row['created_at']} {row['kind']}: {row['data']}" for row in rows]

    def cache_put(self, key, content, now):
        with self.connect() as db:
            db.execute(
                "INSERT OR REPLACE INTO summaries VALUES (?,?,?)", (key, content, now.isoformat())
            )

    def event(self, kind, details, now):
        with self.connect() as db:
            db.execute(
                "INSERT INTO events(created_at,kind,details) VALUES (?,?,?)",
                (now.isoformat(), kind, json.dumps(details, ensure_ascii=False)),
            )

    def logs(self, limit=200):
        with self.connect() as db:
            rows = db.execute("SELECT * FROM events ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
        return "\n".join(f"{r['created_at']}  {r['kind']}  {r['details']}" for r in reversed(rows))

    def activity_logs(self, limit=200):
        """Select the latest meaningful activity, not the latest raw audit rows."""
        if type(limit) is not int or limit <= 0:
            raise ValueError("Количество событий должно быть положительным целым числом.")
        lines = []
        with self.connect() as db:
            rows = db.execute(
                "SELECT created_at,kind,details FROM events "
                f"WHERE kind IN ({','.join('?' for _ in ACTIVITY_KINDS)}) ORDER BY id DESC",
                ACTIVITY_KINDS,
            )
            for row in rows:
                line = format_activity(row)
                if line is not None:
                    lines.append(line)
                    if len(lines) == limit:
                        break
        return "\n".join(reversed(lines))

    def token_usage(self, include_history=False):
        """Read the entire conversation's API accounting, not just the visible logs."""
        with self.connect() as db:
            rows = db.execute(
                "SELECT kind,details FROM events "
                "WHERE kind IN ('api_request','api_usage') ORDER BY id"
            )
            return collect_usage(
                ((row["kind"], json.loads(row["details"])) for row in rows), include_history
            )

    def session_id(self):
        with self.connect() as db:
            db.execute("INSERT OR IGNORE INTO settings VALUES ('session', ?)", (str(uuid4()),))
            return db.execute("SELECT value FROM settings WHERE key='session'").fetchone()[0]

    def clear(self):
        """Atomically remove application records and create a fresh session identity."""
        session_id = str(uuid4())
        with self.connect() as db:
            for table in (
                "profiles",
                "goals",
                "daily_plans",
                "messages",
                "changes",
                "summaries",
                "fact_sources",
                "memory_issues",
                "memory_turns",
                "sticky_facts",
                "topic_messages",
                "topic_transitions",
                "checkpoints",
                "topics",
                "events",
                "settings",
                "chat_state",
                "pending_actions",
                "memory_journal",
                "memory_archives",
            ):
                db.execute(f"DELETE FROM {table}")
            db.execute("INSERT INTO settings VALUES ('session', ?)", (session_id,))
        return session_id

    def weights(self):
        with self.connect() as db:
            return weight_rows(db)
