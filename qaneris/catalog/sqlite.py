"""SQLite-backed catalog repository."""

from __future__ import annotations

import json
import sqlite3
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from qaneris.common.errors import (
    DatasourceSecureProfileRequiredError,
    ManagedSecretIntegrityError,
)
from qaneris.connections.references import managed_secret_references
from qaneris.contracts import (
    DatasetInfo,
    DatasetSample,
    Datasource,
    DatasourceCreate,
    DatasourceKind,
    FieldInfo,
    GovernanceSuggestion,
    MappingInfo,
    RelationInfo,
)
from qaneris.contracts.connection import (
    ConnectionProfile,
    SecureDatasourceCreate,
    contains_inline_secret,
)
from qaneris.contracts.initialization import InitializationJob
from qaneris.contracts.profile import (
    CompanyDataProfile,
    DataSourceProfile,
    ScanPolicy,
    ScanSnapshot,
    ScanStatus,
)


class Catalog:
    def __init__(self, path: str | Path = "qaneris.db"):
        self.path = str(path)
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path)
        connection.row_factory = sqlite3.Row
        return connection

    def _initialize(self) -> None:
        with self._connect() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS datasource (
                    id TEXT PRIMARY KEY,
                    workspace_id TEXT NOT NULL,
                    name TEXT NOT NULL,
                    kind TEXT NOT NULL,
                    connection_json TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'created',
                    UNIQUE(workspace_id, name)
                );
                CREATE TABLE IF NOT EXISTS dataset (
                    id TEXT PRIMARY KEY,
                    datasource_id TEXT NOT NULL REFERENCES datasource(id) ON DELETE CASCADE,
                    name TEXT NOT NULL,
                    kind TEXT NOT NULL,
                    metadata_json TEXT NOT NULL,
                    UNIQUE(datasource_id, name)
                );
                CREATE TABLE IF NOT EXISTS relation (
                    id TEXT PRIMARY KEY,
                    datasource_id TEXT NOT NULL REFERENCES datasource(id) ON DELETE CASCADE,
                    from_dataset TEXT NOT NULL,
                    from_field TEXT,
                    to_dataset TEXT NOT NULL,
                    to_field TEXT,
                    relation_type TEXT NOT NULL,
                    source TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS dataset_sample (
                    id TEXT PRIMARY KEY,
                    datasource_id TEXT NOT NULL REFERENCES datasource(id) ON DELETE CASCADE,
                    dataset TEXT NOT NULL,
                    rows_json TEXT NOT NULL,
                    UNIQUE(datasource_id, dataset)
                );
                CREATE TABLE IF NOT EXISTS mapping (
                    id TEXT PRIMARY KEY,
                    workspace_id TEXT NOT NULL,
                    entity TEXT NOT NULL,
                    canonical_field TEXT NOT NULL,
                    datasource_id TEXT NOT NULL REFERENCES datasource(id) ON DELETE CASCADE,
                    dataset TEXT NOT NULL,
                    field TEXT NOT NULL,
                    confidence REAL NOT NULL,
                    source TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS governance_suggestion (
                    id TEXT PRIMARY KEY,
                    workspace_id TEXT NOT NULL,
                    type TEXT NOT NULL,
                    entity TEXT NOT NULL,
                    confidence REAL NOT NULL,
                    reason TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'pending'
                );
                CREATE TABLE IF NOT EXISTS initialization_job (
                    id TEXT PRIMARY KEY,
                    datasource_id TEXT NOT NULL REFERENCES datasource(id) ON DELETE CASCADE,
                    status TEXT NOT NULL,
                    policy_json TEXT NOT NULL,
                    snapshot_id TEXT,
                    error_code TEXT,
                    error_message TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS scan_snapshot (
                    id TEXT PRIMARY KEY,
                    datasource_id TEXT NOT NULL REFERENCES datasource(id) ON DELETE CASCADE,
                    version INTEGER NOT NULL,
                    status TEXT NOT NULL,
                    snapshot_json TEXT NOT NULL,
                    active INTEGER NOT NULL DEFAULT 0,
                    UNIQUE(datasource_id, version)
                );
                CREATE UNIQUE INDEX IF NOT EXISTS one_active_snapshot_per_datasource
                    ON scan_snapshot(datasource_id) WHERE active = 1;
                """
            )

    def create_datasource(self, data: DatasourceCreate) -> Datasource:
        if contains_inline_secret(data.connection):
            raise ValueError("Catalog cannot persist inline connection secrets")
        return self._insert_datasource(data.name, data.kind, data.workspace_id, data.connection)

    def _insert_datasource(
        self,
        name: str,
        kind: DatasourceKind,
        workspace_id: str,
        connection_data: dict[str, Any],
    ) -> Datasource:
        datasource_id = f"ds_{uuid.uuid4().hex[:12]}"
        with self._connect() as connection:
            connection.execute(
                "INSERT INTO datasource(id, workspace_id, name, kind, connection_json) VALUES(?,?,?,?,?)",
                (
                    datasource_id,
                    workspace_id,
                    name,
                    kind.value,
                    json.dumps(connection_data),
                ),
            )
        return Datasource(
            id=datasource_id,
            name=name,
            kind=kind,
            workspace_id=workspace_id,
            driver=self._stored_driver(connection_data),
        )

    def create_secure_datasource(self, data: SecureDatasourceCreate) -> Datasource:
        stored = {
            "format": "connection_profile_v1",
            "profile": data.connection_profile.model_dump(mode="json"),
        }
        return self._insert_datasource(
            data.name,
            data.kind,
            data.workspace_id,
            stored,
        )

    def get_connection_profile(self, datasource_id: str) -> ConnectionProfile | None:
        _, stored = self.get_datasource(datasource_id)
        if stored.get("format") != "connection_profile_v1":
            return None
        return ConnectionProfile.model_validate(stored["profile"])

    def update_secure_datasource(
        self,
        datasource_id: str,
        connection_profile: ConnectionProfile,
    ) -> Datasource:
        """Replace one secure datasource's stored connection profile in a single transaction.

        Only a datasource that already uses ``connection_profile_v1`` can be updated. A legacy row
        holds a raw connection document with no ``SecretReference`` to rotate, and this method does
        not migrate one: the caller is told ``datasource_secure_profile_required`` instead of
        silently gaining a secure profile it never asked for.

        The datasource is returned to ``created`` as part of the same transaction. The stored
        profile is new, but no scan has run against it yet, so the old active snapshot can no
        longer describe this datasource - leaving ``ready`` here would advertise a scan of a
        connection that is no longer configured.
        """
        stored = {
            "format": "connection_profile_v1",
            "profile": connection_profile.model_dump(mode="json"),
        }
        with self._connect() as connection:
            row = connection.execute(
                "SELECT id, name, kind, workspace_id, connection_json FROM datasource WHERE id=?",
                (datasource_id,),
            ).fetchone()
            if row is None:
                raise KeyError(f"Datasource not found: {datasource_id}")
            current = json.loads(row["connection_json"])
            if current.get("format") != "connection_profile_v1":
                raise DatasourceSecureProfileRequiredError(
                    f"datasource {datasource_id} has no secure connection profile"
                )
            connection.execute(
                "UPDATE datasource SET connection_json=?, status='created' WHERE id=?",
                (json.dumps(stored), datasource_id),
            )
        return Datasource(
            id=row["id"],
            name=row["name"],
            kind=DatasourceKind(row["kind"]),
            workspace_id=row["workspace_id"],
            driver=connection_profile.driver,
            status="created",
        )

    def invalidate_datasource_scan(self, datasource_id: str) -> None:
        """Retire a datasource's current scan facts in one transaction.

        Once the connection profile changes, the active snapshot, the datasets, the relations, the
        samples and the mappings were all observed through a connection that no longer exists.
        Keeping them active would let Ask join a new connection profile to an old scan, so they are
        dropped together here rather than being left for the next successful scan to overwrite.

        Historical ``scan_snapshot`` rows and ``initialization_job`` rows are not touched: the
        snapshot is marked inactive, never deleted, so the audit trail of what was scanned and when
        survives the connection change. The datasource returns to ``created`` in the same
        transaction, which is what ``inspect_datasource`` reports as "no active scan" until the
        caller explicitly scans again.
        """
        with self._connect() as connection:
            cursor = connection.execute(
                "UPDATE datasource SET status='created' WHERE id=?", (datasource_id,)
            )
            if cursor.rowcount != 1:
                raise KeyError(f"Datasource not found: {datasource_id}")
            connection.execute(
                "UPDATE scan_snapshot SET active=0 WHERE datasource_id=?", (datasource_id,)
            )
            for table in ("dataset", "relation", "dataset_sample", "mapping"):
                connection.execute(
                    f"DELETE FROM {table} WHERE datasource_id=?", (datasource_id,)
                )

    def delete_datasource(self, datasource_id: str) -> None:
        """Delete one datasource and every current fact it owns, in one transaction.

        The delete order follows the reference graph - samples, relations, datasets, mappings,
        initialization jobs, snapshots, then the datasource itself - because the schema's
        ``ON DELETE CASCADE`` is not the formal contract here, and a delete that only works when
        the pragma happens to be enabled is not a delete that always works. The parent row is
        removed last, so a failure mid-transaction can never leave a row whose owner is gone.

        Everything the datasource currently owns is removed: its datasets, relations, samples,
        mappings, its initialization job history and its snapshot history. A missing datasource is
        a ``KeyError``, matching ``get_datasource``, so the caller can report it as
        ``datasource_not_found`` and nothing else.
        """
        with self._connect() as connection:
            if (
                connection.execute(
                    "SELECT 1 FROM datasource WHERE id=?", (datasource_id,)
                ).fetchone()
                is None
            ):
                raise KeyError(f"Datasource not found: {datasource_id}")
            for table in (
                "dataset_sample",
                "relation",
                "dataset",
                "mapping",
                "initialization_job",
                "scan_snapshot",
            ):
                connection.execute(
                    f"DELETE FROM {table} WHERE datasource_id=?", (datasource_id,)
                )
            connection.execute("DELETE FROM datasource WHERE id=?", (datasource_id,))

    def count_managed_secret_references(self, secret_id: str) -> int:
        """Count how many stored connection profiles still reference one managed secret.

        Every ``connection_profile_v1`` datasource is parsed back into a ``ConnectionProfile`` and
        walked by the shared reference helper, so a reference is recognised by its meaning rather
        than by a substring of the stored JSON: a re-serialised profile, a differently ordered field
        or an unrelated string that happens to contain the id cannot change the answer. The same
        helper is what the application service uses to compute rotation cleanup, so the count that
        authorises a delete and the set that drives one can never disagree.

        The count is per occurrence, not per distinct secret: a profile that names the same secret
        in several fields really does depend on it several times over.

        Only references are read. A profile never holds secret material, and nothing here decrypts
        or resolves anything. A row that claims to be a stored profile but no longer parses fails
        closed: an unknown reference count must not be reported as zero, because zero is what
        authorises a delete.
        """
        return sum(
            sum(
                1
                for reference in managed_secret_references(profile)
                if reference.identifier == secret_id
            )
            for _, profile in self._stored_profiles()
        )

    def _stored_profiles(self) -> list[tuple[str, ConnectionProfile]]:
        """Read every stored ``connection_profile_v1`` document back into a profile.

        A row that claims to be a stored profile but no longer parses fails closed with
        :class:`ManagedSecretIntegrityError`: an unreadable profile could reference anything, and
        guessing its references - in either direction - is not safe.
        """
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT id, connection_json FROM datasource ORDER BY id"
            ).fetchall()
        profiles: list[tuple[str, ConnectionProfile]] = []
        for row in rows:
            stored = json.loads(row["connection_json"])
            if stored.get("format") != "connection_profile_v1":
                continue
            try:
                profile = ConnectionProfile.model_validate(stored["profile"])
            except ValidationError as error:
                raise ManagedSecretIntegrityError(
                    f"datasource {row['id']} has an unreadable connection profile"
                ) from error
            profiles.append((row["id"], profile))
        return profiles

    def list_datasources(self, workspace_id: str = "default") -> list[Datasource]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT id, name, kind, workspace_id, connection_json, status FROM datasource "
                "WHERE workspace_id=? ORDER BY name",
                (workspace_id,),
            ).fetchall()
        return [
            Datasource(
                id=row["id"],
                name=row["name"],
                kind=row["kind"],
                workspace_id=row["workspace_id"],
                driver=self._stored_driver(json.loads(row["connection_json"])),
                status=row["status"],
            )
            for row in rows
        ]

    @staticmethod
    def _stored_driver(stored: dict[str, Any]) -> str | None:
        if stored.get("format") == "connection_profile_v1":
            return stored.get("profile", {}).get("driver")
        return stored.get("driver")

    def get_datasource(self, datasource_id: str) -> tuple[Datasource, dict[str, Any]]:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM datasource WHERE id=?", (datasource_id,)
            ).fetchone()
        if row is None:
            raise KeyError(f"Datasource not found: {datasource_id}")
        stored = json.loads(row["connection_json"])
        driver = (
            stored.get("profile", {}).get("driver")
            if stored.get("format") == "connection_profile_v1"
            else stored.get("driver")
        )
        datasource = Datasource(
            id=row["id"],
            name=row["name"],
            kind=DatasourceKind(row["kind"]),
            workspace_id=row["workspace_id"],
            driver=driver,
            status=row["status"],
        )
        return datasource, stored

    def create_initialization_job(
        self, datasource_id: str, policy: ScanPolicy | None = None
    ) -> InitializationJob:
        now = datetime.now(UTC)
        job = InitializationJob(
            id=f"job_{uuid.uuid4().hex[:12]}",
            datasource_id=datasource_id,
            policy=policy or ScanPolicy(),
            created_at=now,
            updated_at=now,
        )
        with self._connect() as connection:
            if (
                connection.execute(
                    "SELECT 1 FROM datasource WHERE id=?", (datasource_id,)
                ).fetchone()
                is None
            ):
                raise KeyError(f"Datasource not found: {datasource_id}")
            connection.execute(
                "INSERT INTO initialization_job(id, datasource_id, status, policy_json, "
                "created_at, updated_at) VALUES(?,?,?,?,?,?)",
                (
                    job.id,
                    job.datasource_id,
                    job.status.value,
                    job.policy.model_dump_json(),
                    job.created_at.isoformat(),
                    job.updated_at.isoformat(),
                ),
            )
        return job

    def update_initialization_job(
        self,
        job_id: str,
        status: ScanStatus,
        *,
        snapshot_id: str | None = None,
        error_code: str | None = None,
        error_message: str | None = None,
    ) -> InitializationJob:
        now = datetime.now(UTC)
        with self._connect() as connection:
            current = connection.execute(
                "SELECT status FROM initialization_job WHERE id=?", (job_id,)
            ).fetchone()
            if current is None:
                raise KeyError(f"Initialization job not found: {job_id}")
            transitions = {
                ScanStatus.CREATED: {ScanStatus.VERIFYING},
                ScanStatus.VERIFYING: {
                    ScanStatus.CONNECTION_VERIFIED,
                    ScanStatus.CONNECTION_FAILED,
                },
                ScanStatus.CONNECTION_VERIFIED: {ScanStatus.SCANNING},
                ScanStatus.SCANNING: {ScanStatus.PROFILING, ScanStatus.SCAN_FAILED},
                ScanStatus.PROFILING: {ScanStatus.READY, ScanStatus.PROFILE_FAILED},
            }
            current_status = ScanStatus(current["status"])
            if status not in transitions.get(current_status, set()):
                raise ValueError(
                    f"Invalid initialization transition: {current_status.value} -> {status.value}"
                )
            connection.execute(
                "UPDATE initialization_job SET status=?, snapshot_id=COALESCE(?, snapshot_id), "
                "error_code=?, error_message=?, updated_at=? WHERE id=?",
                (status.value, snapshot_id, error_code, error_message, now.isoformat(), job_id),
            )
        return self.get_initialization_job(job_id)

    def get_initialization_job(self, job_id: str) -> InitializationJob:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM initialization_job WHERE id=?", (job_id,)
            ).fetchone()
        if row is None:
            raise KeyError(f"Initialization job not found: {job_id}")
        return InitializationJob(
            id=row["id"],
            datasource_id=row["datasource_id"],
            status=row["status"],
            policy=ScanPolicy.model_validate_json(row["policy_json"]),
            snapshot_id=row["snapshot_id"],
            error_code=row["error_code"],
            error_message=row["error_message"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )

    def next_snapshot_version(self, datasource_id: str) -> int:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT COALESCE(MAX(version), 0) + 1 AS version FROM scan_snapshot "
                "WHERE datasource_id=?",
                (datasource_id,),
            ).fetchone()
        return int(row["version"])

    def save_snapshot(self, snapshot: ScanSnapshot) -> None:
        """Persist a snapshot and atomically switch the active ready version."""
        with self._connect() as connection:
            if snapshot.active:
                connection.execute(
                    "UPDATE scan_snapshot SET active=0 WHERE datasource_id=?",
                    (snapshot.datasource_id,),
                )
            connection.execute(
                "INSERT INTO scan_snapshot(id, datasource_id, version, status, snapshot_json, active) "
                "VALUES(?,?,?,?,?,?)",
                (
                    snapshot.id,
                    snapshot.datasource_id,
                    snapshot.version,
                    snapshot.status.value,
                    snapshot.model_dump_json(),
                    int(snapshot.active),
                ),
            )
            if snapshot.active:
                connection.execute(
                    "UPDATE datasource SET status='ready' WHERE id=?", (snapshot.datasource_id,)
                )

    def commit_scan_snapshot(
        self,
        snapshot: ScanSnapshot,
        datasets: list[DatasetInfo],
        relations: list[RelationInfo],
        samples: list[DatasetSample],
    ) -> None:
        """Atomically update the compatibility catalog and activate a ready snapshot."""
        if not snapshot.active or snapshot.status != ScanStatus.READY:
            raise ValueError("Only an active ready snapshot can be committed")
        self._validate_scan_result(snapshot.datasource_id, datasets, relations, samples)
        with self._connect() as connection:
            connection.execute(
                "DELETE FROM dataset WHERE datasource_id=?", (snapshot.datasource_id,)
            )
            connection.execute(
                "DELETE FROM relation WHERE datasource_id=?", (snapshot.datasource_id,)
            )
            connection.execute(
                "DELETE FROM dataset_sample WHERE datasource_id=?", (snapshot.datasource_id,)
            )
            connection.executemany(
                "INSERT INTO dataset(id, datasource_id, name, kind, metadata_json) "
                "VALUES(?,?,?,?,?)",
                [
                    (
                        f"set_{uuid.uuid4().hex[:12]}",
                        snapshot.datasource_id,
                        item.name,
                        item.kind,
                        item.model_dump_json(),
                    )
                    for item in datasets
                ],
            )
            self._insert_relations(connection, relations)
            connection.executemany(
                "INSERT INTO dataset_sample(id, datasource_id, dataset, rows_json) VALUES(?,?,?,?)",
                [
                    (
                        f"sample_{uuid.uuid4().hex[:12]}",
                        snapshot.datasource_id,
                        item.dataset,
                        json.dumps(item.rows, ensure_ascii=False, default=str),
                    )
                    for item in samples
                ],
            )
            connection.execute(
                "UPDATE scan_snapshot SET active=0 WHERE datasource_id=?",
                (snapshot.datasource_id,),
            )
            connection.execute(
                "INSERT INTO scan_snapshot(id, datasource_id, version, status, "
                "snapshot_json, active) VALUES(?,?,?,?,?,1)",
                (
                    snapshot.id,
                    snapshot.datasource_id,
                    snapshot.version,
                    snapshot.status.value,
                    snapshot.model_dump_json(),
                ),
            )
            cursor = connection.execute(
                "UPDATE datasource SET status='ready' WHERE id=?",
                (snapshot.datasource_id,),
            )
            if cursor.rowcount != 1:
                raise KeyError(f"Datasource not found: {snapshot.datasource_id}")

    def get_active_snapshot(self, datasource_id: str) -> ScanSnapshot | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT snapshot_json FROM scan_snapshot WHERE datasource_id=? AND active=1",
                (datasource_id,),
            ).fetchone()
        return ScanSnapshot.model_validate_json(row["snapshot_json"]) if row else None

    def update_snapshot_document_state(self, snapshot: ScanSnapshot) -> None:
        with self._connect() as connection:
            cursor = connection.execute(
                "UPDATE scan_snapshot SET snapshot_json=? WHERE id=?",
                (snapshot.model_dump_json(), snapshot.id),
            )
            if cursor.rowcount != 1:
                raise KeyError(f"Scan snapshot not found: {snapshot.id}")

    def get_company_data_profile(self, workspace_id: str = "default") -> CompanyDataProfile:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT s.snapshot_json FROM scan_snapshot s JOIN datasource d "
                "ON d.id=s.datasource_id WHERE d.workspace_id=? AND s.active=1 ORDER BY d.name",
                (workspace_id,),
            ).fetchall()
        snapshots = [ScanSnapshot.model_validate_json(row["snapshot_json"]) for row in rows]
        profiles: list[DataSourceProfile] = [item.profile for item in snapshots if item.profile]
        relationships = [relation for item in snapshots for relation in item.relationships]
        return CompanyDataProfile(
            workspace_id=workspace_id,
            data_sources=profiles,
            relationships=relationships,
            generated_at=datetime.now(UTC),
        )

    def replace_datasets(self, datasource_id: str, datasets: list[DatasetInfo]) -> None:
        with self._connect() as connection:
            connection.execute("DELETE FROM dataset WHERE datasource_id=?", (datasource_id,))
            connection.executemany(
                "INSERT INTO dataset(id, datasource_id, name, kind, metadata_json) VALUES(?,?,?,?,?)",
                [
                    (
                        f"set_{uuid.uuid4().hex[:12]}",
                        datasource_id,
                        item.name,
                        item.kind,
                        item.model_dump_json(),
                    )
                    for item in datasets
                ],
            )
            connection.execute("UPDATE datasource SET status='ready' WHERE id=?", (datasource_id,))

    def replace_scan_result(
        self,
        datasource_id: str,
        datasets: list[DatasetInfo],
        relations: list[RelationInfo],
        samples: list[DatasetSample] | None = None,
    ) -> None:
        samples = samples or []
        self._validate_scan_result(datasource_id, datasets, relations, samples)
        with self._connect() as connection:
            connection.execute("DELETE FROM dataset WHERE datasource_id=?", (datasource_id,))
            connection.execute("DELETE FROM relation WHERE datasource_id=?", (datasource_id,))
            connection.execute("DELETE FROM dataset_sample WHERE datasource_id=?", (datasource_id,))
            connection.executemany(
                "INSERT INTO dataset(id, datasource_id, name, kind, metadata_json) VALUES(?,?,?,?,?)",
                [
                    (
                        f"set_{uuid.uuid4().hex[:12]}",
                        datasource_id,
                        item.name,
                        item.kind,
                        item.model_dump_json(),
                    )
                    for item in datasets
                ],
            )
            self._insert_relations(connection, relations)
            connection.executemany(
                "INSERT INTO dataset_sample(id, datasource_id, dataset, rows_json) VALUES(?,?,?,?)",
                [
                    (
                        f"sample_{uuid.uuid4().hex[:12]}",
                        datasource_id,
                        item.dataset,
                        json.dumps(item.rows, ensure_ascii=False, default=str),
                    )
                    for item in samples
                ],
            )
            cursor = connection.execute(
                "UPDATE datasource SET status='ready' WHERE id=?", (datasource_id,)
            )
            if cursor.rowcount != 1:
                raise KeyError(f"Datasource not found: {datasource_id}")

    def replace_relations(self, datasource_id: str, relations: list[RelationInfo]) -> None:
        self._validate_relations(datasource_id, relations)
        with self._connect() as connection:
            if (
                connection.execute(
                    "SELECT 1 FROM datasource WHERE id=?", (datasource_id,)
                ).fetchone()
                is None
            ):
                raise KeyError(f"Datasource not found: {datasource_id}")
            connection.execute("DELETE FROM relation WHERE datasource_id=?", (datasource_id,))
            self._insert_relations(connection, relations)

    def list_relations(
        self, workspace_id: str = "default", datasource_id: str | None = None
    ) -> list[RelationInfo]:
        query = (
            "SELECT r.datasource_id, r.from_dataset, r.from_field, r.to_dataset, "
            "r.to_field, r.relation_type, r.source FROM relation r "
            "JOIN datasource d ON d.id=r.datasource_id WHERE d.workspace_id=?"
        )
        params: list[Any] = [workspace_id]
        if datasource_id:
            query += " AND r.datasource_id=?"
            params.append(datasource_id)
        query += " ORDER BY r.rowid"
        with self._connect() as connection:
            rows = connection.execute(query, params).fetchall()
        return [RelationInfo(**dict(row)) for row in rows]

    def create_mapping(self, mapping: MappingInfo) -> MappingInfo:
        mapping_id = mapping.id or f"map_{uuid.uuid4().hex[:12]}"
        stored_mapping = mapping.model_copy(update={"id": mapping_id})
        with self._connect() as connection:
            datasource = connection.execute(
                "SELECT workspace_id FROM datasource WHERE id=?", (mapping.datasource_id,)
            ).fetchone()
            if datasource is None:
                raise KeyError(f"Datasource not found: {mapping.datasource_id}")
            if datasource["workspace_id"] != mapping.workspace_id:
                raise ValueError("Mapping workspace must match datasource workspace")
            connection.execute(
                "INSERT INTO mapping(id, workspace_id, entity, canonical_field, "
                "datasource_id, dataset, field, confidence, source) VALUES(?,?,?,?,?,?,?,?,?)",
                (
                    stored_mapping.id,
                    stored_mapping.workspace_id,
                    stored_mapping.entity,
                    stored_mapping.canonical_field,
                    stored_mapping.datasource_id,
                    stored_mapping.dataset,
                    stored_mapping.field,
                    stored_mapping.confidence,
                    stored_mapping.source,
                ),
            )
        return stored_mapping

    def list_mappings(
        self, workspace_id: str = "default", entity: str | None = None
    ) -> list[MappingInfo]:
        query = (
            "SELECT id, workspace_id, entity, canonical_field, datasource_id, dataset, "
            "field, confidence, source FROM mapping WHERE workspace_id=?"
        )
        params: list[Any] = [workspace_id]
        if entity:
            query += " AND entity=?"
            params.append(entity)
        query += " ORDER BY rowid"
        with self._connect() as connection:
            rows = connection.execute(query, params).fetchall()
        return [MappingInfo(**dict(row)) for row in rows]

    @staticmethod
    def _validate_relations(datasource_id: str, relations: list[RelationInfo]) -> None:
        if any(item.datasource_id != datasource_id for item in relations):
            raise ValueError("All relations must belong to the scanned datasource")

    @classmethod
    def _validate_scan_result(
        cls,
        datasource_id: str,
        datasets: list[DatasetInfo],
        relations: list[RelationInfo],
        samples: list[DatasetSample] | None = None,
    ) -> None:
        if any(item.datasource_id != datasource_id for item in datasets):
            raise ValueError("All datasets must belong to the scanned datasource")
        cls._validate_relations(datasource_id, relations)
        if any(item.datasource_id != datasource_id for item in samples or []):
            raise ValueError("All samples must belong to the scanned datasource")
        dataset_names = {item.name for item in datasets}
        if any(item.dataset not in dataset_names for item in samples or []):
            raise ValueError("Every sample must reference a scanned dataset")

    def list_samples(
        self, workspace_id: str = "default", datasource_id: str | None = None
    ) -> list[DatasetSample]:
        query = (
            "SELECT s.datasource_id, s.dataset, s.rows_json FROM dataset_sample s "
            "JOIN datasource d ON d.id=s.datasource_id WHERE d.workspace_id=?"
        )
        params: list[Any] = [workspace_id]
        if datasource_id:
            query += " AND s.datasource_id=?"
            params.append(datasource_id)
        query += " ORDER BY s.dataset"
        with self._connect() as connection:
            rows = connection.execute(query, params).fetchall()
        return [
            DatasetSample(
                datasource_id=row["datasource_id"],
                dataset=row["dataset"],
                rows=json.loads(row["rows_json"]),
            )
            for row in rows
        ]

    @staticmethod
    def _insert_relations(connection: sqlite3.Connection, relations: list[RelationInfo]) -> None:
        connection.executemany(
            "INSERT INTO relation(id, datasource_id, from_dataset, from_field, "
            "to_dataset, to_field, relation_type, source) VALUES(?,?,?,?,?,?,?,?)",
            [
                (
                    f"rel_{uuid.uuid4().hex[:12]}",
                    item.datasource_id,
                    item.from_dataset,
                    item.from_field,
                    item.to_dataset,
                    item.to_field,
                    item.relation_type,
                    item.source,
                )
                for item in relations
            ],
        )

    def list_datasets(
        self, workspace_id: str = "default", datasource_id: str | None = None
    ) -> list[DatasetInfo]:
        query = (
            "SELECT d.datasource_id, d.name, d.kind, d.metadata_json FROM dataset d "
            "JOIN datasource s ON s.id=d.datasource_id WHERE s.workspace_id=?"
        )
        params: list[Any] = [workspace_id]
        if datasource_id:
            query += " AND d.datasource_id=?"
            params.append(datasource_id)
        query += " ORDER BY d.name"
        with self._connect() as connection:
            rows = connection.execute(query, params).fetchall()
        results = []
        for row in rows:
            metadata = json.loads(row["metadata_json"])
            metadata["fields"] = [FieldInfo(**field) for field in metadata.get("fields", [])]
            results.append(DatasetInfo(**metadata))
        return results

    def create_suggestion(
        self, workspace_id: str, kind: str, entity: str, confidence: float, reason: str
    ) -> GovernanceSuggestion:
        suggestion = GovernanceSuggestion(
            id=f"gov_{uuid.uuid4().hex[:12]}",
            type=kind,
            entity=entity,
            confidence=confidence,
            reason=reason,
        )
        with self._connect() as connection:
            connection.execute(
                "INSERT INTO governance_suggestion(id, workspace_id, type, entity, confidence, reason, status) "
                "VALUES(?,?,?,?,?,?,?)",
                (
                    suggestion.id,
                    workspace_id,
                    suggestion.type,
                    suggestion.entity,
                    suggestion.confidence,
                    suggestion.reason,
                    suggestion.status,
                ),
            )
        return suggestion

    def list_suggestions(self, workspace_id: str = "default") -> list[GovernanceSuggestion]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT id, type, entity, confidence, reason, status FROM governance_suggestion "
                "WHERE workspace_id=? ORDER BY rowid DESC",
                (workspace_id,),
            ).fetchall()
        return [GovernanceSuggestion(**dict(row)) for row in rows]
