"""Topic persistence; writes participate in the existing answer/facts transaction."""

import json


def initialize_topics(db):
    db.executescript("""
        CREATE TABLE IF NOT EXISTS topics (
            id INTEGER PRIMARY KEY AUTOINCREMENT, title TEXT NOT NULL,
            parent_topic_id INTEGER, created_at TEXT NOT NULL, updated_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS topic_messages (
            message_id INTEGER PRIMARY KEY, topic_id INTEGER NOT NULL
        );
        CREATE INDEX IF NOT EXISTS topic_message_lookup ON topic_messages(topic_id,message_id);
        CREATE TABLE IF NOT EXISTS topic_transitions (
            message_id INTEGER PRIMARY KEY, topic_id INTEGER NOT NULL, previous_topic_id INTEGER,
            action TEXT NOT NULL, related_topic_ids TEXT NOT NULL, created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS checkpoints (
            id INTEGER PRIMARY KEY AUTOINCREMENT, topic_id INTEGER NOT NULL,
            first_message_id INTEGER NOT NULL, covered_message_id INTEGER NOT NULL,
            message_count INTEGER NOT NULL, content TEXT NOT NULL, profile_key TEXT NOT NULL,
            created_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS checkpoint_topic ON checkpoints(topic_id,id);
    """)
    if not db.execute("SELECT 1 FROM topics LIMIT 1").fetchone():
        old = db.execute(
            "SELECT MIN(created_at),MAX(created_at),COUNT(*) FROM messages WHERE status='complete'"
        ).fetchone()
        if old[2]:
            legacy = db.execute(
                "INSERT INTO topics(title,created_at,updated_at) VALUES ('Общий диалог',?,?)",
                (old[0], old[1]),
            ).lastrowid
            db.execute(
                "INSERT INTO topic_messages SELECT id,? FROM messages WHERE status='complete'",
                (legacy,),
            )


def write_topic_turn(db, plan, user_id, assistant_id, now):
    last = db.execute("SELECT COALESCE(MAX(message_id),0) FROM topic_transitions").fetchone()[0]
    if last != plan["revision"]:
        raise ValueError("Тема изменена другим запросом. Повторите вопрос.")
    topic_id = plan["topic_id"]
    if plan["new_topic"]:
        db.execute(
            "INSERT INTO topics(id,title,parent_topic_id,created_at,updated_at) VALUES (?,?,?,?,?)",
            (topic_id, plan["title"], plan["parent_topic_id"], now.isoformat(), now.isoformat()),
        )
    if plan["adopt_legacy"]:
        db.execute(
            "INSERT INTO topic_messages(message_id,topic_id) SELECT id,? FROM messages "
            "WHERE status='complete' AND id<? AND id NOT IN (SELECT message_id FROM topic_messages)",
            (topic_id, user_id),
        )
    for checkpoint in plan["checkpoints"]:
        db.execute(
            "INSERT INTO checkpoints(topic_id,first_message_id,covered_message_id,message_count,"
            "content,profile_key,created_at) VALUES (?,?,?,?,?,?,?)",
            tuple(
                checkpoint[key]
                for key in (
                    "topic_id",
                    "first_message_id",
                    "covered_message_id",
                    "message_count",
                    "content",
                    "profile_key",
                    "created_at",
                )
            ),
        )
    db.executemany(
        "INSERT INTO topic_messages(message_id,topic_id) VALUES (?,?)",
        [(user_id, topic_id), (assistant_id, topic_id)],
    )
    db.execute("UPDATE topics SET updated_at=? WHERE id=?", (now.isoformat(), topic_id))
    db.execute(
        "INSERT INTO topic_transitions VALUES (?,?,?,?,?,?)",
        (
            user_id,
            topic_id,
            plan["previous_topic_id"],
            plan["action"],
            json.dumps(plan["related_topic_ids"]),
            now.isoformat(),
        ),
    )
    db.execute(
        "INSERT INTO events(created_at,kind,details) VALUES (?,'topic_routed',?)",
        (
            now.isoformat(),
            json.dumps(
                {
                    "action": plan["action"],
                    "topic_id": topic_id,
                    "previous_topic_id": plan["previous_topic_id"],
                    "checkpoints": len(plan["checkpoints"]),
                }
            ),
        ),
    )


class TopicRepository:
    def __init__(self, store):
        self.store = store

    def snapshot(self):
        with self.store.connect() as db:
            topics = [
                dict(row)
                for row in db.execute("SELECT * FROM topics WHERE closed_at IS NULL ORDER BY id")
            ]
            last = db.execute(
                "SELECT * FROM topic_transitions ORDER BY message_id DESC LIMIT 1"
            ).fetchone()
            next_id = db.execute(
                "SELECT COALESCE((SELECT seq FROM sqlite_sequence WHERE name='topics'),0)+1"
            ).fetchone()[0]
        open_ids = {topic["id"] for topic in topics}
        active = (
            last["topic_id"]
            if last and last["topic_id"] in open_ids
            else topics[-1]["id"]
            if topics
            else None
        )
        return {
            "topics": topics,
            "active_topic_id": active,
            "revision": last["message_id"] if last else 0,
            "next_id": next_id,
        }

    def checkpoint(self, topic_id):
        with self.store.connect() as db:
            row = db.execute(
                "SELECT * FROM checkpoints WHERE topic_id=? ORDER BY id DESC LIMIT 1",
                (topic_id,),
            ).fetchone()
        return dict(row) if row else None

    def messages(self, topic_id, since=None, after=0, legacy=False):
        where = "tm.topic_id=?" if not legacy else "tm.topic_id IS NULL"
        parameters = [] if legacy else [topic_id]
        where += " AND m.id>? AND m.status='complete'"
        if not legacy:
            where += " AND EXISTS (SELECT 1 FROM topics t WHERE t.id=tm.topic_id AND t.closed_at IS NULL)"
        parameters.append(after)
        if since:
            where += " AND m.created_at>=?"
            parameters.append(since)
        with self.store.connect() as db:
            rows = db.execute(
                "SELECT m.id,m.created_at,m.role,m.content FROM messages m "
                "LEFT JOIN topic_messages tm ON tm.message_id=m.id WHERE "
                + where
                + " ORDER BY m.id",
                parameters,
            ).fetchall()
        return [dict(row) for row in rows]

    def notice(self, message_id=None):
        with self.store.connect() as db:
            suffix = (
                "WHERE tr.message_id=?"
                if message_id is not None
                else "ORDER BY tr.message_id DESC LIMIT 1"
            )
            rows = db.execute(
                "SELECT tr.*,t.title FROM topic_transitions tr JOIN topics t ON t.id=tr.topic_id "
                + suffix,
                (message_id,) if message_id is not None else (),
            ).fetchall()
        if not rows or rows[0]["action"] == "continue":
            return None
        row = dict(rows[0])
        return {"action": row["action"], "title": row["title"]}
