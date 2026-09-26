"""SQLite conversation store, independent of Catalog's SQL implementation."""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager

from qaneris.conversation.models import Conversation, Message, SemanticMemory, now


class SQLiteConversationRepository:
    def __init__(self, path: str):
        self.path = path
        with self._connect() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS conversation (
                    id TEXT PRIMARY KEY, workspace_id TEXT NOT NULL, title TEXT NOT NULL,
                    datasource_scope_json TEXT NOT NULL, status TEXT NOT NULL,
                    revision INTEGER NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS conversation_message (
                    id TEXT PRIMARY KEY, conversation_id TEXT NOT NULL, run_id TEXT,
                    role TEXT NOT NULL, message_kind TEXT NOT NULL, content TEXT NOT NULL,
                    created_at TEXT NOT NULL);
                CREATE INDEX IF NOT EXISTS conversation_message_order
                    ON conversation_message(conversation_id, created_at, id);
                CREATE TABLE IF NOT EXISTS conversation_memory (
                    conversation_id TEXT PRIMARY KEY, memory_json TEXT NOT NULL,
                    revision INTEGER NOT NULL, updated_at TEXT NOT NULL);
            """)

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        db = sqlite3.connect(self.path, timeout=30)
        db.row_factory = sqlite3.Row
        try:
            yield db
            db.commit()
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()

    @staticmethod
    def _conversation(row: sqlite3.Row) -> Conversation:
        return Conversation(
            conversation_id=row["id"],
            workspace_id=row["workspace_id"],
            title=row["title"],
            datasource_scope=json.loads(row["datasource_scope_json"]),
            status=row["status"],
            revision=row["revision"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )

    def create(self, conversation: Conversation) -> Conversation:
        with self._connect() as db:
            db.execute(
                "INSERT INTO conversation VALUES (?,?,?,?,?,?,?,?)",
                (
                    conversation.conversation_id,
                    conversation.workspace_id,
                    conversation.title,
                    json.dumps(conversation.datasource_scope),
                    conversation.status,
                    conversation.revision,
                    conversation.created_at.isoformat(),
                    conversation.updated_at.isoformat(),
                ),
            )
            db.execute(
                "INSERT INTO conversation_memory VALUES (?,?,?,?)",
                (
                    conversation.conversation_id,
                    SemanticMemory().model_dump_json(),
                    0,
                    now().isoformat(),
                ),
            )
        return conversation

    def get(self, conversation_id: str) -> Conversation:
        with self._connect() as db:
            row = db.execute("SELECT * FROM conversation WHERE id=?", (conversation_id,)).fetchone()
        if row is None:
            raise KeyError(conversation_id)
        return self._conversation(row)

    def list(self, workspace_id: str) -> list[Conversation]:
        with self._connect() as db:
            rows = db.execute(
                "SELECT * FROM conversation WHERE workspace_id=? ORDER BY created_at DESC",
                (workspace_id,),
            ).fetchall()
        return [self._conversation(row) for row in rows]

    def delete(self, conversation_id: str) -> None:
        """Remove one settled conversation and its durable run state atomically."""
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            if db.execute("SELECT 1 FROM conversation WHERE id=?", (conversation_id,)).fetchone() is None:
                raise KeyError(conversation_id)
            tables = {row[0] for row in db.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )}
            run_ids = []
            if "run" in tables:
                active = db.execute(
                    "SELECT 1 FROM run WHERE conversation_id=? AND status NOT IN "
                    "('COMPLETED','FAILED','CANCELLED','BLOCKED') LIMIT 1",
                    (conversation_id,),
                ).fetchone()
                if active is not None:
                    raise ValueError("conversation_has_active_runs")
                run_ids = [row[0] for row in db.execute(
                    "SELECT id FROM run WHERE conversation_id=?", (conversation_id,)
                )]
            for run_id in run_ids:
                for table in (
                    "diagnostic_task", "diagnostic_checkpoint", "federated_source_task",
                    "federated_plan", "run_event",
                ):
                    if table in tables:
                        db.execute(f"DELETE FROM {table} WHERE run_id=?", (run_id,))
            if "run" in tables:
                db.execute("DELETE FROM run WHERE conversation_id=?", (conversation_id,))
            db.execute("DELETE FROM conversation_message WHERE conversation_id=?", (conversation_id,))
            db.execute("DELETE FROM conversation_memory WHERE conversation_id=?", (conversation_id,))
            db.execute("DELETE FROM conversation WHERE id=?", (conversation_id,))

    def add_message(self, message: Message) -> None:
        with self._connect() as db:
            db.execute(
                "INSERT OR IGNORE INTO conversation_message VALUES (?,?,?,?,?,?,?)",
                (
                    message.message_id,
                    message.conversation_id,
                    message.run_id,
                    message.role,
                    message.message_kind,
                    message.content,
                    message.created_at.isoformat(),
                ),
            )
            if message.role == "user" and message.message_kind == "normal":
                db.execute(
                    "UPDATE conversation SET title=?, revision=revision+1, updated_at=? "
                    "WHERE id=? AND title=''",
                    (message.content[:60], now().isoformat(), message.conversation_id),
                )

    def messages(self, conversation_id: str, limit: int | None = None) -> list[Message]:
        with self._connect() as db:
            if limit is None:
                rows = db.execute(
                    "SELECT * FROM conversation_message WHERE conversation_id=? "
                    "ORDER BY created_at, id",
                    (conversation_id,),
                ).fetchall()
            else:
                rows = db.execute(
                    "SELECT * FROM conversation_message WHERE conversation_id=? "
                    "ORDER BY created_at DESC, id DESC LIMIT ?",
                    (conversation_id, limit),
                ).fetchall()[::-1]
        return [
            Message(
                message_id=row["id"],
                conversation_id=row["conversation_id"],
                run_id=row["run_id"],
                role=row["role"],
                message_kind=row["message_kind"],
                content=row["content"],
                created_at=row["created_at"],
            )
            for row in rows
        ]

    def memory(self, conversation_id: str) -> SemanticMemory:
        with self._connect() as db:
            row = db.execute(
                "SELECT memory_json FROM conversation_memory WHERE conversation_id=?",
                (conversation_id,),
            ).fetchone()
        if row is None:
            raise KeyError(conversation_id)
        return SemanticMemory.model_validate_json(row[0])

    def memory_revision(self, conversation_id: str) -> int:
        with self._connect() as db:
            row = db.execute(
                "SELECT revision FROM conversation_memory WHERE conversation_id=?",
                (conversation_id,),
            ).fetchone()
        if row is None:
            raise KeyError(conversation_id)
        return row[0]

    def put_memory(
        self, conversation_id: str, memory: SemanticMemory, expected_revision: int
    ) -> None:
        with self._connect() as db:
            cursor = db.execute(
                "UPDATE conversation_memory SET memory_json=?, revision=revision+1, "
                "updated_at=? WHERE conversation_id=? AND revision=?",
                (memory.model_dump_json(), now().isoformat(), conversation_id, expected_revision),
            )
            if cursor.rowcount != 1:
                raise ValueError("semantic memory revision conflict")
