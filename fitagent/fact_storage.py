"""Persistence of fact revisions and atomic completion of memory-enabled turns."""

import json
from datetime import datetime
from itertools import groupby

from fitagent.memory_schema import parse_stamp
from fitagent.state_storage import journal
from fitagent.topic_storage import write_topic_turn

FACT_COLUMNS = (
    "id",
    "category",
    "fact_key",
    "scope",
    "value",
    "valid_from",
    "valid_until",
    "review_at",
    "created_at",
    "updated_at",
    "source_message_id",
    "origin",
    "applicability",
    "topic_id",
    "source_topic_id",
    "lineage_id",
)


def migrate_scheduled_versions(db):
    """Recover transition boundaries from recorded revision dates, without model calls."""
    rows = db.execute("SELECT * FROM sticky_facts ORDER BY lineage_id,id").fetchall()
    for _, versions in groupby(rows, key=lambda row: row["lineage_id"]):
        versions = list(versions)
        if versions[-1]["status"] != "active":
            continue
        effective = [
            row
            for index, row in enumerate(versions)
            if index == len(versions) - 1
            or parse_stamp(row["valid_from"]) <= parse_stamp(versions[index + 1]["created_at"])
        ]
        for previous, following in zip(effective, effective[1:]):
            if previous["status"] == "replaced" and parse_stamp(
                following["valid_from"]
            ) > parse_stamp(following["created_at"]):
                db.execute(
                    "UPDATE sticky_facts SET superseded_at=? WHERE id=?",
                    (following["valid_from"], previous["id"]),
                )


def initialize_memory(db):
    db.executescript("""
        CREATE TABLE IF NOT EXISTS sticky_facts (
            id INTEGER PRIMARY KEY AUTOINCREMENT, category TEXT NOT NULL, fact_key TEXT NOT NULL,
            scope TEXT NOT NULL, value TEXT NOT NULL, valid_from TEXT NOT NULL,
            valid_until TEXT, review_at TEXT, created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL, source_message_id INTEGER, origin TEXT NOT NULL,
            applicability TEXT NOT NULL DEFAULT 'global', topic_id INTEGER,
            source_topic_id INTEGER, lineage_id INTEGER,
            status TEXT NOT NULL DEFAULT 'active', superseded_at TEXT
        );
        CREATE TABLE IF NOT EXISTS fact_sources (
            fact_id INTEGER NOT NULL, message_id INTEGER NOT NULL, quote TEXT NOT NULL,
            source_topic_id INTEGER,
            PRIMARY KEY (fact_id,message_id)
        );
        CREATE TABLE IF NOT EXISTS memory_issues (
            category TEXT NOT NULL, fact_key TEXT NOT NULL, scope TEXT NOT NULL,
            question TEXT NOT NULL, source_message_id INTEGER NOT NULL, created_at TEXT NOT NULL,
            applicability TEXT NOT NULL DEFAULT 'global', topic_id INTEGER, source_topic_id INTEGER
        );
        CREATE TABLE IF NOT EXISTS memory_turns (message_id INTEGER PRIMARY KEY);
    """)
    # Migrate the earlier global-only memory without guessing historical semantic topics.
    columns = {row["name"] for row in db.execute("PRAGMA table_info(sticky_facts)")}
    if not db.in_transaction:
        db.execute("BEGIN")
    for name, definition in (
        ("applicability", "TEXT NOT NULL DEFAULT 'global'"),
        ("topic_id", "INTEGER"),
        ("source_topic_id", "INTEGER"),
        ("lineage_id", "INTEGER"),
        ("superseded_at", "TEXT"),
    ):
        if name not in columns:
            db.execute(f"ALTER TABLE sticky_facts ADD COLUMN {name} {definition}")
    db.execute(
        "UPDATE sticky_facts SET lineage_id=(SELECT MIN(f.id) FROM sticky_facts f "
        "WHERE f.category=sticky_facts.category AND f.fact_key=sticky_facts.fact_key "
        "AND f.scope=sticky_facts.scope) WHERE lineage_id IS NULL"
    )
    if "superseded_at" not in columns:
        migrate_scheduled_versions(db)
    db.execute("DROP INDEX IF EXISTS sticky_fact_identity")
    db.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS sticky_fact_scope_identity ON sticky_facts"
        "(category,fact_key,scope,applicability,COALESCE(topic_id,0)) WHERE status='active'"
    )
    if "source_topic_id" not in {
        row["name"] for row in db.execute("PRAGMA table_info(fact_sources)")
    }:
        db.execute("ALTER TABLE fact_sources ADD COLUMN source_topic_id INTEGER")
    if "applicability" not in {
        row["name"] for row in db.execute("PRAGMA table_info(memory_issues)")
    }:
        db.execute("ALTER TABLE memory_issues RENAME TO memory_issues_v1")
        db.execute("""CREATE TABLE memory_issues (
            category TEXT NOT NULL,fact_key TEXT NOT NULL,scope TEXT NOT NULL,question TEXT NOT NULL,
            source_message_id INTEGER NOT NULL,created_at TEXT NOT NULL,
            applicability TEXT NOT NULL DEFAULT 'global',topic_id INTEGER,source_topic_id INTEGER
        )""")
        db.execute(
            "INSERT INTO memory_issues(category,fact_key,scope,question,source_message_id,created_at) "
            "SELECT category,fact_key,scope,question,source_message_id,created_at FROM memory_issues_v1"
        )
        db.execute("DROP TABLE memory_issues_v1")
    db.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS memory_issue_scope ON memory_issues"
        "(category,fact_key,scope,applicability,COALESCE(topic_id,0))"
    )


class FactRepository:
    def __init__(self, store):
        self.store = store

    def load(self, now=None):
        self.store.session_id()
        now = now or datetime.now().astimezone()
        with self.store.connect() as db:
            # sqlite3's connection context alone does not start a read transaction.
            db.execute("BEGIN")
            session = db.execute("SELECT value FROM settings WHERE key='session'").fetchone()[0]
            rows = db.execute(
                "SELECT * FROM sticky_facts WHERE status='active' OR "
                "(status='replaced' AND superseded_at IS NOT NULL) ORDER BY id"
            )
            facts, previous = [], []
            for row in rows:
                if row["status"] == "replaced" and parse_stamp(row["superseded_at"]) <= now:
                    continue
                fact = dict(row)
                fact["key"] = fact.pop("fact_key")
                try:
                    fact["value"] = json.loads(fact["value"])
                except (ValueError, TypeError, RecursionError):
                    raise RuntimeError("Не удалось прочитать значение памяти.") from None
                (facts if fact["status"] == "active" else previous).append(fact)
            issues = []
            for row in db.execute("SELECT * FROM memory_issues ORDER BY category,fact_key,scope"):
                issue = dict(row)
                issue["key"] = issue.pop("fact_key")
                issues.append(issue)
            next_id = db.execute(
                "SELECT COALESCE((SELECT seq FROM sqlite_sequence WHERE name='sticky_facts'),0)+1"
            ).fetchone()[0]
            revision = db.execute(
                "SELECT value FROM settings WHERE key='memory_revision'"
            ).fetchone()
        return {
            "facts": facts,
            "previous_versions": previous,
            "issues": issues,
            "next_id": next_id,
            "session": session,
            "revision": int(revision[0]) if revision else 0,
        }

    def previous_exchange(self, before_id):
        """Only the immediately preceding completed turn, never a bulk archive export."""
        with self.store.connect() as db:
            rows = db.execute(
                "SELECT m.id,m.role,m.content,m.created_at FROM messages m "
                "LEFT JOIN topic_messages tm ON tm.message_id=m.id LEFT JOIN topics t ON t.id=tm.topic_id "
                "WHERE m.id<? AND m.status='complete' AND (tm.topic_id IS NULL OR t.closed_at IS NULL) "
                "ORDER BY m.id DESC LIMIT 2",
                (before_id,),
            ).fetchall()
        # Do not send an unexpectedly large old message for a short contextual reference.
        result = [dict(row) for row in reversed(rows)]
        if [row["role"] for row in result] != ["user", "assistant"] or sum(
            len(row["content"].encode("utf-8")) for row in result
        ) > 12_000:
            return []
        return result

    def pending_source(self, message_id, question):
        with self.store.connect() as db:
            row = db.execute(
                "SELECT created_at,content,role,status FROM messages WHERE id=?", (message_id,)
            ).fetchone()
        if (
            not row
            or row["role"] != "user"
            or row["status"] != "pending"
            or row["content"] != question
        ):
            raise ValueError("Вопрос уже обработан или удалён. Отправьте новый вопрос.")
        return row["created_at"]

    def _write(self, db, snapshot):
        session = db.execute("SELECT value FROM settings WHERE key='session'").fetchone()
        revision = db.execute("SELECT value FROM settings WHERE key='memory_revision'").fetchone()
        if (
            not session
            or session[0] != snapshot["session"]
            or (int(revision[0]) if revision else 0) != snapshot["revision"]
        ):
            raise ValueError("Память изменена другим запросом. Повторите вопрос.")
        if snapshot.get("consume_action"):
            if not db.execute(
                "DELETE FROM pending_actions WHERE id=?", (snapshot["consume_action"],)
            ).rowcount:
                raise ValueError("Предложение уже обработано. Сформулируйте изменение заново.")
        for fact_id, status, superseded_at in snapshot["retired"]:
            db.execute(
                "UPDATE sticky_facts SET status=?,superseded_at=? WHERE id=?",
                (status, superseded_at, fact_id),
            )
        for fact in snapshot["previous_versions"]:
            db.execute(
                "UPDATE sticky_facts SET superseded_at=?,updated_at=?,review_at=?,"
                "source_message_id=?,origin=?,source_topic_id=? WHERE id=? AND status='replaced'",
                tuple(
                    fact[key]
                    for key in (
                        "superseded_at",
                        "updated_at",
                        "review_at",
                        "source_message_id",
                        "origin",
                        "source_topic_id",
                        "id",
                    )
                ),
            )
        for fact in snapshot["facts"]:
            values = dict(fact, fact_key=fact["key"])
            values["value"] = json.dumps(fact["value"], ensure_ascii=False, allow_nan=False)
            db.execute(
                f"INSERT INTO sticky_facts({','.join(FACT_COLUMNS)}) "
                f"VALUES ({','.join('?' for _ in FACT_COLUMNS)}) ON CONFLICT(id) DO UPDATE SET "
                "updated_at=excluded.updated_at,review_at=excluded.review_at,"
                "source_message_id=excluded.source_message_id,origin=excluded.origin,"
                "source_topic_id=excluded.source_topic_id",
                [values[column] for column in FACT_COLUMNS],
            )
        for fact_id, message_id, quote, source_topic_id in snapshot["sources"]:
            db.execute(
                "INSERT OR IGNORE INTO fact_sources VALUES (?,?,?,?)",
                (fact_id, message_id, quote, source_topic_id),
            )
        db.execute("DELETE FROM memory_issues")
        for issue in snapshot["issues"]:
            db.execute(
                "INSERT INTO memory_issues VALUES (?,?,?,?,?,?,?,?,?)",
                tuple(
                    issue[key]
                    for key in (
                        "category",
                        "key",
                        "scope",
                        "question",
                        "source_message_id",
                        "created_at",
                        "applicability",
                        "topic_id",
                        "source_topic_id",
                    )
                ),
            )
        db.execute(
            "INSERT OR REPLACE INTO settings VALUES ('memory_revision',?)",
            (str(snapshot["revision"] + 1),),
        )
        for lineage, effective, action, old, new, message in snapshot.get("journal", []):
            journal(
                db,
                lineage,
                snapshot.get("recorded_at", effective),
                effective,
                action,
                old,
                new,
                message,
            )
        from fitagent.goal_state import write_goal_turn

        write_goal_turn(db, snapshot)

    def complete(self, snapshot, message_id, response, now):
        with self.store.connect() as db:
            # Take the writer lock before checking revision/source, including across processes.
            db.execute("BEGIN IMMEDIATE")
            source = db.execute(
                "SELECT role,status FROM messages WHERE id=?", (message_id,)
            ).fetchone()
            if not source or source["role"] != "user" or source["status"] != "pending":
                raise ValueError("Вопрос уже обработан или удалён. Отправьте новый вопрос.")
            self._write(db, snapshot)
            from fitagent.chat_actions import ChatActions, write_actions

            write_actions(db, snapshot, now)
            proposals = ChatActions.describe_proposals(snapshot)
            if proposals:
                response += "\n\n" + proposals
            db.execute("INSERT INTO memory_turns VALUES (?)", (message_id,))
            assistant = db.execute(
                "INSERT INTO messages(day,created_at,role,content,status) VALUES (?,?,'assistant',?,'complete')",
                (now.date().isoformat(), now.isoformat(), response),
            )
            if "dialogue" in snapshot:
                write_topic_turn(db, snapshot["dialogue"], message_id, assistant.lastrowid, now)
            db.execute("UPDATE messages SET status='complete' WHERE id=?", (message_id,))
            db.execute(
                "INSERT INTO events(created_at,kind,details) VALUES (?,'memory_finished',?)",
                (
                    now.isoformat(),
                    json.dumps(
                        {
                            "facts": len(snapshot["facts"]),
                            "retired": len(snapshot["retired"]),
                            "clarifications": len(snapshot["issues"]),
                        }
                    ),
                ),
            )
        return response

    def delete(self, fact_id):
        with self.store.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                "SELECT * FROM sticky_facts WHERE id=? AND status='active'", (fact_id,)
            ).fetchone()
            if row is None:
                raise ValueError("Факт не найден. Посмотрите доступные номера через /memory.")
            identity = (
                row["category"],
                row["fact_key"],
                row["scope"],
                row["applicability"],
                row["topic_id"],
            )
            db.execute(
                "DELETE FROM fact_sources WHERE fact_id IN "
                "(SELECT id FROM sticky_facts WHERE lineage_id=?)",
                (row["lineage_id"],),
            )
            db.execute(
                "DELETE FROM sticky_facts WHERE lineage_id=?",
                (row["lineage_id"],),
            )
            db.execute("DELETE FROM memory_journal WHERE lineage_id=?", (row["lineage_id"],))
            db.execute(
                "DELETE FROM memory_issues WHERE category=? AND fact_key=? AND scope=? "
                "AND applicability=? AND topic_id IS ?",
                identity,
            )
            db.execute(
                "INSERT INTO settings VALUES ('memory_revision','1') ON CONFLICT(key) "
                "DO UPDATE SET value=CAST(value AS INTEGER)+1"
            )

    def save_manual(self, snapshot):
        with self.store.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            self._write(db, snapshot)

    def history(self, fact_id):
        with self.store.connect() as db:
            row = db.execute(
                "SELECT lineage_id FROM sticky_facts WHERE id=?", (fact_id,)
            ).fetchone()
            if row is None:
                raise ValueError("Факт не найден. Посмотрите номера через /memory.")
            rows = db.execute(
                "SELECT * FROM memory_journal WHERE lineage_id=? ORDER BY id", (row[0],)
            ).fetchall()
        return "Журнал изменений:\n" + "\n".join(
            f"{r['recorded_at']} · действует с {r['effective_at']} · {r['action']} · "
            f"{r['old_value'] or 'не было'} → {r['new_value'] or 'отозвано'}"
            for r in rows
        )

    def archives(self):
        with self.store.connect() as db:
            rows = db.execute("SELECT * FROM memory_archives ORDER BY id").fetchall()
        return "Архив среднесрочной памяти:\n" + (
            "\n".join(
                f"№{r['id']} · цель №{r['goal_id']} · {r['created_at']}\n{r['facts']}" for r in rows
            )
            or "Архив пока пуст."
        )
