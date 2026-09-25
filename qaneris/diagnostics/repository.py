"""SQLite checkpoints for the bounded diagnostic loop."""

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager

from qaneris.conversation.models import now
from qaneris.diagnostics.models import DiagnosticCheckpoint, DiagnosticTask, TaskStatus


class SQLiteDiagnosticRepository:
    def __init__(self, path: str):
        self.path = path
        with self._connect() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS diagnostic_checkpoint (
                    run_id TEXT PRIMARY KEY, state_json TEXT NOT NULL, revision INTEGER NOT NULL);
                CREATE TABLE IF NOT EXISTS diagnostic_task (
                    id TEXT PRIMARY KEY, run_id TEXT NOT NULL, round INTEGER NOT NULL,
                    ordinal INTEGER NOT NULL, state_json TEXT NOT NULL, revision INTEGER NOT NULL,
                    UNIQUE(run_id, round, ordinal));
                CREATE INDEX IF NOT EXISTS diagnostic_task_run ON diagnostic_task(run_id, round, ordinal);
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

    def checkpoint(self, run_id: str) -> DiagnosticCheckpoint:
        with self._connect() as db:
            db.execute(
                "INSERT OR IGNORE INTO diagnostic_checkpoint VALUES (?,?,0)",
                (run_id, DiagnosticCheckpoint(run_id=run_id).model_dump_json()),
            )
            row = db.execute(
                "SELECT state_json FROM diagnostic_checkpoint WHERE run_id=?", (run_id,)
            ).fetchone()
        return DiagnosticCheckpoint.model_validate_json(row[0])

    def save_checkpoint(self, state: DiagnosticCheckpoint) -> DiagnosticCheckpoint:
        updated = state.model_copy(update={"revision": state.revision + 1})
        with self._connect() as db:
            cursor = db.execute(
                "UPDATE diagnostic_checkpoint SET state_json=?, revision=? "
                "WHERE run_id=? AND revision=?",
                (updated.model_dump_json(), updated.revision, state.run_id, state.revision),
            )
            if cursor.rowcount != 1:
                raise ValueError("diagnostic checkpoint revision conflict")
        return updated

    def tasks(self, run_id: str) -> list[DiagnosticTask]:
        with self._connect() as db:
            rows = db.execute(
                "SELECT state_json FROM diagnostic_task WHERE run_id=? ORDER BY round, ordinal",
                (run_id,),
            ).fetchall()
        return [DiagnosticTask.model_validate_json(row[0]) for row in rows]

    def add_task(self, task: DiagnosticTask) -> DiagnosticTask:
        with self._connect() as db:
            db.execute(
                "INSERT INTO diagnostic_task VALUES (?,?,?,?,?,?)",
                (task.id, task.run_id, task.round, task.ordinal, task.model_dump_json(), 0),
            )
        return task

    def plan_round(
        self,
        checkpoint: DiagnosticCheckpoint,
        tasks: list[DiagnosticTask],
    ) -> DiagnosticCheckpoint:
        """Commit a planned batch and its budget as one SQLite transaction."""
        planned = sum(task.status == TaskStatus.PLANNED for task in tasks)
        updated = checkpoint.model_copy(
            update={
                "revision": checkpoint.revision + 1,
                "current_round": checkpoint.current_round + 1,
                "questions_planned": checkpoint.questions_planned + planned,
                "remaining_budget": checkpoint.remaining_budget - planned,
            }
        )
        with self._connect() as db:
            cursor = db.execute(
                "UPDATE diagnostic_checkpoint SET state_json=?, revision=? "
                "WHERE run_id=? AND revision=?",
                (
                    updated.model_dump_json(),
                    updated.revision,
                    checkpoint.run_id,
                    checkpoint.revision,
                ),
            )
            if cursor.rowcount != 1:
                raise ValueError("diagnostic checkpoint revision conflict")
            for task in tasks:
                db.execute(
                    "INSERT INTO diagnostic_task VALUES (?,?,?,?,?,?)",
                    (task.id, task.run_id, task.round, task.ordinal, task.model_dump_json(), 0),
                )
        return updated

    def save_task(self, task: DiagnosticTask) -> DiagnosticTask:
        updated = task.model_copy(update={"revision": task.revision + 1, "updated_at": now()})
        with self._connect() as db:
            cursor = db.execute(
                "UPDATE diagnostic_task SET state_json=?, revision=? WHERE id=? AND revision=?",
                (updated.model_dump_json(), updated.revision, task.id, task.revision),
            )
            if cursor.rowcount != 1:
                raise ValueError("diagnostic task revision conflict")
        return updated

    def recover_running(self) -> None:
        with self._connect() as db:
            rows = db.execute("SELECT state_json FROM diagnostic_task").fetchall()
        for row in rows:
            task = DiagnosticTask.model_validate_json(row[0])
            if task.status == TaskStatus.RUNNING:
                self.save_task(
                    task.model_copy(
                        update={
                            "status": TaskStatus.FAILED,
                            "failure_code": "task_interrupted",
                            "failure_message": "证据查询被进程重启中断",
                            "retryable": True,
                        }
                    )
                )
