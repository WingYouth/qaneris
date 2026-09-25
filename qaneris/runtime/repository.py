"""SQLite run checkpoints and ordered public event log."""

import json
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager

from qaneris.conversation.models import now
from qaneris.runtime.models import Run, RunEvent, RunStatus
from qaneris.runtime.state_machine import transition


class SQLiteRunRepository:
    def __init__(self, path: str):
        self.path = path
        with self._connect() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS run (
                    id TEXT PRIMARY KEY, conversation_id TEXT NOT NULL,
                    user_message_id TEXT NOT NULL, workspace_id TEXT NOT NULL,
                    status TEXT NOT NULL, current_stage TEXT NOT NULL,
                    revision INTEGER NOT NULL, attempt INTEGER NOT NULL,
                    cancel_requested INTEGER NOT NULL, resolved_question TEXT,
                    response_json TEXT, failure_code TEXT, failure_message TEXT,
                    retryable INTEGER NOT NULL, max_rows INTEGER NOT NULL,
                    client_request_id TEXT, created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL, completed_at TEXT,
                    UNIQUE(conversation_id, client_request_id));
                CREATE TABLE IF NOT EXISTS run_event (
                    run_id TEXT NOT NULL, sequence INTEGER NOT NULL,
                    event_type TEXT NOT NULL, occurred_at TEXT NOT NULL,
                    public_payload_json TEXT NOT NULL,
                    PRIMARY KEY(run_id, sequence));
            """)
            columns = {row[1] for row in db.execute("PRAGMA table_info(run)")}
            if "run_kind" not in columns:
                db.execute("ALTER TABLE run ADD COLUMN run_kind TEXT NOT NULL DEFAULT 'normal'")

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
    def _run(row: sqlite3.Row) -> Run:
        values = dict(row)
        values["run_id"] = values.pop("id")
        values["response_json"] = (
            json.loads(values["response_json"]) if values["response_json"] else None
        )
        return Run.model_validate(values)

    def create(self, run: Run) -> Run:
        with self._connect() as db:
            if run.client_request_id:
                existing = db.execute(
                    "SELECT * FROM run WHERE conversation_id=? AND client_request_id=?",
                    (run.conversation_id, run.client_request_id),
                ).fetchone()
                if existing:
                    return self._run(existing)
            try:
                db.execute(
                    "INSERT INTO run (id, conversation_id, user_message_id, workspace_id, "
                    "status, current_stage, revision, attempt, cancel_requested, resolved_question, "
                    "response_json, failure_code, failure_message, retryable, max_rows, "
                    "client_request_id, created_at, updated_at, completed_at, run_kind) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        run.run_id,
                        run.conversation_id,
                        run.user_message_id,
                        run.workspace_id,
                        run.status,
                        run.current_stage,
                        run.revision,
                        run.attempt,
                        int(run.cancel_requested),
                        run.resolved_question,
                        json.dumps(run.response_json) if run.response_json else None,
                        run.failure_code,
                        run.failure_message,
                        int(run.retryable),
                        run.max_rows,
                        run.client_request_id,
                        run.created_at.isoformat(),
                        run.updated_at.isoformat(),
                        run.completed_at.isoformat() if run.completed_at else None,
                        run.run_kind,
                    ),
                )
            except sqlite3.IntegrityError:
                if not run.client_request_id:
                    raise
                existing = db.execute(
                    "SELECT * FROM run WHERE conversation_id=? AND client_request_id=?",
                    (run.conversation_id, run.client_request_id),
                ).fetchone()
                if existing is None:
                    raise
                return self._run(existing)
        return run

    def get(self, run_id: str) -> Run:
        with self._connect() as db:
            row = db.execute("SELECT * FROM run WHERE id=?", (run_id,)).fetchone()
        if row is None:
            raise KeyError(run_id)
        return self._run(row)

    def save(self, run: Run, expected_revision: int) -> Run:
        updated = run.model_copy(update={"revision": expected_revision + 1, "updated_at": now()})
        with self._connect() as db:
            cursor = db.execute(
                """UPDATE run SET status=?, current_stage=?, revision=?, attempt=?,
                cancel_requested=?, resolved_question=?, response_json=?, failure_code=?,
                failure_message=?, retryable=?, updated_at=?, completed_at=?, run_kind=?
                WHERE id=? AND revision=?""",
                (
                    updated.status,
                    updated.current_stage,
                    updated.revision,
                    updated.attempt,
                    int(updated.cancel_requested),
                    updated.resolved_question,
                    json.dumps(updated.response_json) if updated.response_json else None,
                    updated.failure_code,
                    updated.failure_message,
                    int(updated.retryable),
                    updated.updated_at.isoformat(),
                    updated.completed_at.isoformat() if updated.completed_at else None,
                    updated.run_kind,
                    updated.run_id,
                    expected_revision,
                ),
            )
            if cursor.rowcount != 1:
                raise ValueError("run revision conflict")
        return updated

    def append_event(self, run_id: str, event_type: str, payload: dict) -> RunEvent:
        event = RunEvent(run_id=run_id, sequence=1, event_type=event_type, public_payload=payload)
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            sequence = db.execute(
                "SELECT COALESCE(MAX(sequence),0)+1 FROM run_event WHERE run_id=?", (run_id,)
            ).fetchone()[0]
            event = event.model_copy(update={"sequence": sequence})
            db.execute(
                "INSERT INTO run_event VALUES (?,?,?,?,?)",
                (
                    run_id,
                    sequence,
                    event_type,
                    event.occurred_at.isoformat(),
                    json.dumps(event.public_payload, ensure_ascii=False),
                ),
            )
        return event

    def events(self, run_id: str, after_sequence: int = 0) -> list[RunEvent]:
        with self._connect() as db:
            rows = db.execute(
                "SELECT * FROM run_event WHERE run_id=? AND sequence>? ORDER BY sequence",
                (run_id, after_sequence),
            ).fetchall()
        return [
            RunEvent(
                run_id=row["run_id"],
                sequence=row["sequence"],
                event_type=row["event_type"],
                occurred_at=row["occurred_at"],
                public_payload=json.loads(row["public_payload_json"]),
            )
            for row in rows
        ]

    def last_event(self, run_id: str) -> RunEvent | None:
        with self._connect() as db:
            row = db.execute(
                "SELECT * FROM run_event WHERE run_id=? ORDER BY sequence DESC LIMIT 1", (run_id,)
            ).fetchone()
        if row is None:
            return None
        return RunEvent(
            run_id=row["run_id"],
            sequence=row["sequence"],
            event_type=row["event_type"],
            occurred_at=row["occurred_at"],
            public_payload=json.loads(row["public_payload_json"]),
        )

    def recover_interrupted(self) -> None:
        active = (
            RunStatus.CONTEXTUALIZING,
            RunStatus.DISCOVERING,
            RunStatus.PLANNING,
            RunStatus.EXECUTING,
            RunStatus.ANSWERING,
        )
        with self._connect() as db:
            rows = db.execute("SELECT * FROM run WHERE status IN (?,?,?,?,?)", active).fetchall()
        for row in rows:
            current = self.get(row["id"])
            try:
                self.save(
                    transition(
                        current,
                        RunStatus.FAILED,
                        failure_code="run_interrupted",
                        failure_message="运行被进程重启中断，可重试",
                        retryable=True,
                    ),
                    current.revision,
                )
                self.append_event(
                    current.run_id,
                    "RUN_FAILED",
                    {"failure_code": "run_interrupted", "retryable": True},
                )
            except ValueError:
                pass

    def queued_run_ids(self) -> list[str]:
        with self._connect() as db:
            rows = db.execute(
                "SELECT id FROM run WHERE "
                "(status=? AND current_stage=?) OR "
                "(status=? AND current_stage=?) ORDER BY created_at, id",
                (RunStatus.WAITING_USER, "resume_queued", RunStatus.FAILED, "retry_queued"),
            ).fetchall()
        return [row[0] for row in rows]

    def incomplete_completions(self) -> list[Run]:
        with self._connect() as db:
            rows = db.execute(
                "SELECT r.* FROM run AS r WHERE r.status=? AND NOT EXISTS ("
                "SELECT 1 FROM run_event AS e WHERE e.run_id=r.id AND e.event_type='RUN_COMPLETED') "
                "ORDER BY r.completed_at, r.id",
                (RunStatus.COMPLETED,),
            ).fetchall()
        return [self._run(row) for row in rows]

    def incomplete_clarifications(self) -> list[Run]:
        with self._connect() as db:
            rows = db.execute(
                "SELECT r.* FROM run AS r WHERE r.status=? AND r.current_stage=? "
                "AND (SELECT event_type FROM run_event WHERE run_id=r.id "
                "ORDER BY sequence DESC LIMIT 1) != 'CLARIFICATION_REQUIRED' "
                "ORDER BY r.updated_at, r.id",
                (RunStatus.WAITING_USER, "waiting_user"),
            ).fetchall()
        return [self._run(row) for row in rows]
