"""SQLite federation checkpoints and governed join mappings."""

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager

from qaneris.conversation.models import now
from qaneris.federation.models import (
    FederatedPlan,
    JoinMapping,
    JoinMappingStatus,
    SourceTask,
    SourceTaskStatus,
)


class SQLiteFederationRepository:
    def __init__(self, path: str):
        self.path = path
        with self._connect() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS federated_plan (
                    run_id TEXT PRIMARY KEY, plan_json TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS federated_source_task (
                    task_id TEXT PRIMARY KEY, run_id TEXT NOT NULL, ordinal INTEGER NOT NULL,
                    state_json TEXT NOT NULL, revision INTEGER NOT NULL,
                    UNIQUE(run_id, ordinal));
                CREATE INDEX IF NOT EXISTS federated_source_task_run
                    ON federated_source_task(run_id, ordinal);
                CREATE TABLE IF NOT EXISTS join_mapping (
                    mapping_id TEXT PRIMARY KEY, workspace_id TEXT NOT NULL,
                    state_json TEXT NOT NULL, revision INTEGER NOT NULL DEFAULT 0);
                CREATE INDEX IF NOT EXISTS join_mapping_workspace ON join_mapping(workspace_id);
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

    def plan(self, run_id: str) -> FederatedPlan | None:
        with self._connect() as db:
            row = db.execute(
                "SELECT plan_json FROM federated_plan WHERE run_id=?", (run_id,)
            ).fetchone()
        return FederatedPlan.model_validate_json(row[0]) if row else None

    def create_plan(self, plan: FederatedPlan) -> FederatedPlan:
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            existing = db.execute(
                "SELECT plan_json FROM federated_plan WHERE run_id=?", (plan.run_id,)
            ).fetchone()
            if existing:
                return FederatedPlan.model_validate_json(existing[0])
            db.execute("INSERT INTO federated_plan VALUES (?,?)", (plan.run_id, plan.model_dump_json()))
            for task in plan.source_tasks:
                db.execute(
                    "INSERT INTO federated_source_task VALUES (?,?,?,?,?)",
                    (task.task_id, task.run_id, task.ordinal, task.model_dump_json(), task.revision),
                )
        return plan

    def tasks(self, run_id: str) -> list[SourceTask]:
        with self._connect() as db:
            rows = db.execute(
                "SELECT state_json FROM federated_source_task WHERE run_id=? ORDER BY ordinal",
                (run_id,),
            ).fetchall()
        return [SourceTask.model_validate_json(row[0]) for row in rows]

    def save_task(self, task: SourceTask) -> SourceTask:
        updated = task.model_copy(update={"revision": task.revision + 1})
        with self._connect() as db:
            cursor = db.execute(
                "UPDATE federated_source_task SET state_json=?, revision=? "
                "WHERE task_id=? AND revision=?",
                (updated.model_dump_json(), updated.revision, task.task_id, task.revision),
            )
            if cursor.rowcount != 1:
                raise ValueError("source task revision conflict")
        return updated

    def recover_running(self) -> None:
        with self._connect() as db:
            rows = db.execute(
                "SELECT state_json FROM federated_source_task"
            ).fetchall()
        for row in rows:
            task = SourceTask.model_validate_json(row[0])
            if task.status == SourceTaskStatus.RUNNING:
                self.save_task(task.model_copy(update={
                    "status": SourceTaskStatus.FAILED,
                    "failure_code": "task_interrupted",
                    "retryable": True,
                }))

    def add_mapping(self, mapping: JoinMapping) -> JoinMapping:
        if mapping.status != JoinMappingStatus.CANDIDATE:
            raise ValueError("new join mapping must be a candidate")
        with self._connect() as db:
            db.execute(
                "INSERT INTO join_mapping(mapping_id,workspace_id,state_json) VALUES (?,?,?)",
                (mapping.mapping_id, mapping.workspace_id, mapping.model_dump_json()),
            )
        return mapping

    def mapping(self, mapping_id: str) -> JoinMapping:
        with self._connect() as db:
            row = db.execute(
                "SELECT state_json FROM join_mapping WHERE mapping_id=?", (mapping_id,)
            ).fetchone()
        if row is None:
            raise KeyError(mapping_id)
        return JoinMapping.model_validate_json(row[0])

    def mappings(self, workspace_id: str) -> list[JoinMapping]:
        with self._connect() as db:
            rows = db.execute(
                "SELECT state_json FROM join_mapping WHERE workspace_id=? ORDER BY mapping_id",
                (workspace_id,),
            ).fetchall()
        return [JoinMapping.model_validate_json(row[0]) for row in rows]

    def save_mapping(self, mapping: JoinMapping, expected_status: JoinMappingStatus) -> JoinMapping:
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                "SELECT state_json,revision FROM join_mapping WHERE mapping_id=?",
                (mapping.mapping_id,),
            ).fetchone()
            if row is None:
                raise KeyError(mapping.mapping_id)
            current = JoinMapping.model_validate_json(row[0])
            if current.status != expected_status:
                raise ValueError("join mapping state conflict")
            db.execute(
                "UPDATE join_mapping SET state_json=?,revision=? WHERE mapping_id=?",
                (mapping.model_copy(update={"updated_at": now()}).model_dump_json(),
                 row[1] + 1, mapping.mapping_id),
            )
        return self.mapping(mapping.mapping_id)
