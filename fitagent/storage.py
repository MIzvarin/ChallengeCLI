"""Private SQLite persistence and structured audit events."""

import json
import os
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from uuid import uuid4


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
            row = db.execute(f"SELECT * FROM {table} ORDER BY id DESC LIMIT 1").fetchone()
        return dict(row) if row else None

    def save_profile(self, profile, now, next_date, warning="", confirmed=False):
        stamp = now.isoformat()
        with self.connect() as db:
            db.execute(
                "INSERT INTO profiles(height,weight,sex,age,confirmed_at,next_check) "
                "VALUES (?,?,?,?,?,?)",
                (*[profile[k] for k in ("height", "weight", "sex", "age")], stamp, next_date),
            )
            old = db.execute("SELECT * FROM goals ORDER BY id DESC LIMIT 1").fetchone()
            if not old or (old["text"], old["target"]) != (profile["goal"], profile["target"]):
                db.execute(
                    "INSERT INTO goals(text,target,created_at,warning,risk_confirmed) "
                    "VALUES (?,?,?,?,?)",
                    (profile["goal"], profile["target"], stamp, warning, int(confirmed)),
                )
            db.execute(
                "INSERT INTO changes(day,created_at,kind,data) VALUES (?,?,?,?)",
                (now.date().isoformat(), stamp, "profile_confirmed", json.dumps(profile)),
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
                "messages",
                "changes",
                "summaries",
                "events",
                "settings",
            ):
                db.execute(f"DELETE FROM {table}")
            db.execute("INSERT INTO settings VALUES ('session', ?)", (session_id,))
        return session_id

    def weights(self):
        with self.connect() as db:
            return [
                dict(r)
                for r in db.execute(
                    "SELECT confirmed_at,weight FROM profiles ORDER BY id DESC LIMIT 30"
                ).fetchall()
            ][::-1]
