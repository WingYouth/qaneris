"""Secure datasource lifecycle (RS-CONN-01A).

This is the service-level contract for ``test / create / update / delete``. The tests are written
around two rules that the implementation exists to guarantee:

* **Candidate first.** A candidate connection is proven before any catalog, graph or credential
  mutation. When it fails, the old profile, the old active scan, the old graph and the old secrets
  are all exactly as they were.
* **Fail closed.** When a later step fails, the system aborts rather than proceeding into a state
  that mixes a new connection with an old scan, or a deleted catalog row with a published graph.

Most tests therefore assert *ordering and absence* as much as outcomes: a spy records what was
called and in what order, and the failure tests assert that the steps after the failure never ran.

Connections are real: a workspace SQLite file is the datasource, and the real SQLite adapter is what
tests the connection, so an unreachable path fails for the real reason.
"""

from __future__ import annotations

import json
import logging
import sqlite3
from pathlib import Path
from typing import Any, ClassVar

import pytest

from qaneris.adapters.base import DataSourceAdapter
from qaneris.adapters.registry import register_adapter
from qaneris.application.service import QanerisService
from qaneris.catalog import Catalog
from qaneris.common.errors import (
    DatasourceConnectionTestError,
    DatasourceDeleteError,
    DatasourceNotFoundError,
    DatasourceSecureProfileRequiredError,
    DatasourceUpdateError,
)
from qaneris.connections.credential_service import CredentialService
from qaneris.connections.managed_store import ManagedCredentialStore
from qaneris.connections.secrets import ManagedSecretProvider, SecretResolver
from qaneris.contracts import (
    AuthenticationConfig,
    AuthenticationMethod,
    ConnectionEndpoint,
    ConnectionProfile,
    DatasourceCreate,
    DatasourceKind,
    SecretProviderKind,
    SecretReference,
    SecureDatasourceCreate,
    SecureDatasourceTest,
    SecureDatasourceUpdate,
)
from qaneris.contracts.credentials import ManagedSecretKind

PASSWORD_A = "UNIQUE_CANDIDATE_PASSWORD_A"
PASSWORD_B = "UNIQUE_CANDIDATE_PASSWORD_B"


# --------------------------------------------------------------------------------------------
# wiring
# --------------------------------------------------------------------------------------------


def source_database(path: Path, table: str = "orders") -> Path:
    with sqlite3.connect(path) as connection:
        connection.execute(f'CREATE TABLE "{table}" (id INTEGER PRIMARY KEY, amount REAL)')
        connection.execute(f'INSERT INTO "{table}" VALUES (1, 10.0)')
    return path


def sqlite_profile(path: Path) -> ConnectionProfile:
    return ConnectionProfile(
        driver="sqlite",
        endpoint=ConnectionEndpoint(path=str(path)),
    )


PROBE_DRIVER = "probe"


class ProbeAdapter(DataSourceAdapter):
    """A registered stand-in for a driver whose client library is not installed here.

    The first round targets SQLite, so ``psycopg`` and friends are not dependencies of this repo.
    A probe keeps these tests about the service contract - resolve, translate, test, then mutate -
    rather than about which drivers happen to import. It does two things a real driver does: it
    records the resolved connection it was handed (so a test can assert what crossed the adapter
    boundary) and, when that connection points at a path, it fails if the path is not there.
    """

    records: ClassVar[list[dict[str, Any]]] = []

    def test_connection(self) -> None:
        ProbeAdapter.records.append(dict(self.connection))
        # A host-locator probe is reachable; a path locator is only reachable if the file is there,
        # which is what lets a test aim a probe at an unreachable database on purpose.
        path = self.connection.get("path")
        if path is not None and not Path(str(path)).is_file():
            raise RuntimeError(f"probe cannot reach {self.connection}")
        if path is None and not self.connection.get("hosts"):
            raise RuntimeError(f"probe cannot reach {self.connection}")

    def scan_metadata(self):
        return []

    def execute(self, query: str, max_rows: int = 200):
        raise NotImplementedError("the probe adapter only proves a connection")


def probe_profile(secret_id: str, path: Path | None = None) -> ConnectionProfile:
    """A profile that carries a managed secret, tested through the probe driver."""
    return ConnectionProfile(
        driver=PROBE_DRIVER,
        endpoint=ConnectionEndpoint(
            hosts=[{"host": "127.0.0.1", "port": 5432}],
            path=None if path is None else str(path),
        ),
        authentication=AuthenticationConfig(
            method=AuthenticationMethod.PASSWORD,
            username="readonly",
            password=managed(secret_id),
        ),
    )


def managed(secret_id: str) -> SecretReference:
    return SecretReference(provider=SecretProviderKind.MANAGED, identifier=secret_id)


@pytest.fixture(autouse=True)
def probe_adapter():
    """Publish the probe driver for one test and take it back out of the shared registry after.

    The registry is process-wide, so leaving a test-only driver registered would be a side effect
    on every other test in the suite.
    """
    register_adapter(DatasourceKind.RELATIONAL, PROBE_DRIVER, ProbeAdapter)
    ProbeAdapter.records.clear()
    yield
    from qaneris.adapters.registry import _ADAPTERS

    _ADAPTERS.pop((DatasourceKind.RELATIONAL, PROBE_DRIVER), None)


class RecordingGraphStore:
    """A store that records every publication and deletion, and can be made to fail."""

    def __init__(self, *, fail_delete: bool = False) -> None:
        self.published: list[str] = []
        self.deleted: list[str] = []
        self.calls: list[str] = []
        self.fail_delete = fail_delete

    def ensure_schema(self) -> None:
        return None

    def replace_datasource_graph(self, graph) -> None:
        self.calls.append(f"publish:{graph.datasource_id}")
        self.published.append(graph.datasource_id)

    def delete_datasource_graph(self, datasource_id: str) -> None:
        self.calls.append(f"delete_graph:{datasource_id}")
        if self.fail_delete:
            raise RuntimeError("simulated Neo4j delete failure")
        self.deleted.append(datasource_id)


class RecordingInitializer:
    """Wraps the real initializer and records when a scan was requested."""

    def __init__(self, inner) -> None:
        self.inner = inner
        self.calls: list[str] = []

    def initialize(self, datasource_id: str, policy=None):
        self.calls.append(datasource_id)
        return self.inner.initialize(datasource_id, policy)

    def __getattr__(self, name):
        return getattr(self.inner, name)


@pytest.fixture
def store(tmp_path: Path) -> ManagedCredentialStore:
    return ManagedCredentialStore.from_environment(
        {
            "QANERIS_SECRET_STORE_DIR": str(tmp_path / "secrets"),
            "QANERIS_MASTER_KEY": ManagedCredentialStore.generate_master_key(),
        }
    )


@pytest.fixture
def service(tmp_path: Path, store: ManagedCredentialStore) -> QanerisService:
    catalog = Catalog(tmp_path / "catalog.db")
    graph = RecordingGraphStore()
    reader = RecordingGraphReader()
    reader.store = graph
    instance = QanerisService(
        catalog,
        # The resolver must read the same store the test writes to, or a rotation test would be
        # exercising a second, empty store.
        secret_resolver=SecretResolver(
            {SecretProviderKind.MANAGED: ManagedSecretProvider(store)}
        ),
        graph_store=graph,
        graph_reader=reader,
        credential_service=CredentialService(store, catalog),
    )
    instance.initializer = RecordingInitializer(instance.initializer)
    instance.test_graph = graph
    return instance


class RecordingGraphReader:
    """Answers publication reads from the fake store's own record of what it published."""

    def __init__(self) -> None:
        self.store: RecordingGraphStore | None = None

    def read_structure(self, request):
        from qaneris.graph import GraphDatasource, GraphStructure

        published = self.store.published if self.store else []
        return GraphStructure(
            datasources=[
                GraphDatasource(
                    node_id=f"node::{datasource_id}",
                    datasource_id=datasource_id,
                    workspace_id=request.workspace_id,
                    name=datasource_id,
                    scan_version=1,
                )
                for datasource_id in published
                if request.datasource_id is None or datasource_id == request.datasource_id
            ]
        )


def managed_source(service: QanerisService, tmp_path: Path, name: str = "sales") -> str:
    """Create a scanned, READY secure datasource over a real SQLite file."""
    path = source_database(tmp_path / f"{name}.db")
    datasource = service.create_secure_datasource(
        SecureDatasourceCreate(
            name=name, kind=DatasourceKind.RELATIONAL, connection_profile=sqlite_profile(path)
        )
    )
    service.scan_datasource(datasource.id)
    return datasource.id


# --------------------------------------------------------------------------------------------
# test_secure_datasource: the pure connection-test boundary
# --------------------------------------------------------------------------------------------


def test_a_reachable_candidate_reports_a_safe_result(
    service: QanerisService, tmp_path: Path
) -> None:
    path = source_database(tmp_path / "candidate.db")

    result = service.test_secure_datasource(
        SecureDatasourceTest(
            kind=DatasourceKind.RELATIONAL, connection_profile=sqlite_profile(path)
        )
    )

    assert result.ok is True
    assert result.driver == "sqlite"
    assert result.tls_enabled is False


def test_an_unreachable_candidate_fails_with_the_stable_error(
    service: QanerisService, tmp_path: Path
) -> None:
    with pytest.raises(DatasourceConnectionTestError) as raised:
        service.test_secure_datasource(
            SecureDatasourceTest(
                kind=DatasourceKind.RELATIONAL,
                connection_profile=sqlite_profile(tmp_path / "missing.db"),
            )
        )

    assert raised.value.code == "datasource_connection_test_failed"


def test_a_connection_test_writes_nothing(service: QanerisService, tmp_path: Path) -> None:
    """The test boundary must not create a datasource, a job, a snapshot or a graph node."""
    path = source_database(tmp_path / "candidate.db")

    service.test_secure_datasource(
        SecureDatasourceTest(
            kind=DatasourceKind.RELATIONAL, connection_profile=sqlite_profile(path)
        )
    )

    assert service.list_datasources() == []
    assert service.initializer.calls == []
    assert service.test_graph.calls == []
    assert service.catalog.get_company_data_profile().data_sources == []


def test_a_failed_connection_test_also_writes_nothing(
    service: QanerisService, tmp_path: Path
) -> None:
    with pytest.raises(DatasourceConnectionTestError):
        service.test_secure_datasource(
            SecureDatasourceTest(
                kind=DatasourceKind.RELATIONAL,
                connection_profile=sqlite_profile(tmp_path / "missing.db"),
            )
        )

    assert service.list_datasources() == []
    assert service.initializer.calls == []


def test_the_connection_test_result_carries_no_secret(service: QanerisService, tmp_path: Path) -> None:
    """The result is a three-field summary; a resolved connection never reaches the caller."""
    path = source_database(tmp_path / "candidate.db")

    result = service.test_secure_datasource(
        SecureDatasourceTest(
            kind=DatasourceKind.RELATIONAL, connection_profile=sqlite_profile(path)
        )
    )

    assert set(result.model_dump()) == {"ok", "driver", "tls_enabled"}
    assert str(path) not in result.model_dump_json()


def test_a_driver_failure_does_not_leak_the_resolved_connection(
    service: QanerisService, store: ManagedCredentialStore, tmp_path: Path
) -> None:
    """A driver exception that quotes its input is sanitized before it becomes a product error.

    The probe adapter raises an error that renders the whole resolved connection, which is the
    worst case a real driver can produce. The secret must be gone by the time the product error is
    built - even though the driver quoted it back.
    """
    secret = store.create(ManagedSecretKind.PASSWORD, PASSWORD_A)

    with pytest.raises(DatasourceConnectionTestError) as raised:
        service.test_secure_datasource(
            SecureDatasourceTest(
                kind=DatasourceKind.RELATIONAL,
                connection_profile=probe_profile(secret.id, tmp_path / "missing.db"),
            )
        )

    assert PASSWORD_A not in str(raised.value)


# --------------------------------------------------------------------------------------------
# create: test then save, never scan
# --------------------------------------------------------------------------------------------


def test_create_tests_before_it_persists(service: QanerisService, tmp_path: Path) -> None:
    path = source_database(tmp_path / "created.db")

    datasource = service.create_secure_datasource(
        SecureDatasourceCreate(
            name="created", kind=DatasourceKind.RELATIONAL, connection_profile=sqlite_profile(path)
        )
    )

    assert datasource.status == "created"
    assert [item.id for item in service.list_datasources()] == [datasource.id]
    assert service.catalog.get_connection_profile(datasource.id) is not None


def test_a_failed_create_writes_no_datasource_row(
    service: QanerisService, tmp_path: Path
) -> None:
    with pytest.raises(DatasourceConnectionTestError):
        service.create_secure_datasource(
            SecureDatasourceCreate(
                name="ghost",
                kind=DatasourceKind.RELATIONAL,
                connection_profile=sqlite_profile(tmp_path / "missing.db"),
            )
        )

    assert service.list_datasources() == []


def test_create_does_not_scan(service: QanerisService, tmp_path: Path) -> None:
    """``Test → Save → Scan``: create saves, and only ``scan_datasource`` scans."""
    path = source_database(tmp_path / "created.db")

    datasource = service.create_secure_datasource(
        SecureDatasourceCreate(
            name="created", kind=DatasourceKind.RELATIONAL, connection_profile=sqlite_profile(path)
        )
    )

    assert service.initializer.calls == []
    assert service.catalog.get_active_snapshot(datasource.id) is None
    assert service.catalog.list_datasets(datasource_id=datasource.id) == []
    assert service.test_graph.published == []


def test_create_creates_no_initialization_job(service: QanerisService, tmp_path: Path) -> None:
    path = source_database(tmp_path / "created.db")

    service.create_secure_datasource(
        SecureDatasourceCreate(
            name="created", kind=DatasourceKind.RELATIONAL, connection_profile=sqlite_profile(path)
        )
    )

    assert _job_count(service.catalog.path) == 0


def test_create_reports_no_scan_facts(service: QanerisService, tmp_path: Path) -> None:
    """A saved-but-unscanned datasource must not advertise a scan it never had."""
    path = source_database(tmp_path / "created.db")
    datasource = service.create_secure_datasource(
        SecureDatasourceCreate(
            name="created", kind=DatasourceKind.RELATIONAL, connection_profile=sqlite_profile(path)
        )
    )

    detail = service.inspect_datasource(datasource.id)

    assert detail.status == "created"
    assert detail.scan_version is None
    assert detail.last_scan_status is None
    assert detail.dataset_count == 0


def test_explicit_scan_after_create_reaches_ready(
    service: QanerisService, tmp_path: Path
) -> None:
    path = source_database(tmp_path / "created.db")
    datasource = service.create_secure_datasource(
        SecureDatasourceCreate(
            name="created", kind=DatasourceKind.RELATIONAL, connection_profile=sqlite_profile(path)
        )
    )

    datasets = service.scan_datasource(datasource.id)

    assert [item.name for item in datasets] == ["orders"]
    detail = service.inspect_datasource(datasource.id)
    assert detail.status == "ready"
    assert detail.scan_version == 1
    assert detail.dataset_count == 1
    assert service.test_graph.published == [datasource.id]


def test_a_stored_profile_contains_only_references(
    service: QanerisService, store: ManagedCredentialStore, tmp_path: Path
) -> None:
    secret = store.create(ManagedSecretKind.PASSWORD, PASSWORD_A)

    datasource = service.create_secure_datasource(
        SecureDatasourceCreate(
            name="pg",
            kind=DatasourceKind.RELATIONAL,
            connection_profile=probe_profile(secret.id),
        )
    )

    stored = json.dumps(service.catalog.get_datasource(datasource.id)[1])
    assert secret.id in stored
    assert PASSWORD_A not in stored


# --------------------------------------------------------------------------------------------
# update: candidate first
# --------------------------------------------------------------------------------------------


def test_a_failed_candidate_leaves_the_old_profile_in_place(
    service: QanerisService, tmp_path: Path
) -> None:
    datasource_id = managed_source(service, tmp_path)
    before = service.catalog.get_connection_profile(datasource_id)

    with pytest.raises(DatasourceConnectionTestError):
        service.update_secure_datasource(
            datasource_id,
            SecureDatasourceUpdate(
                connection_profile=sqlite_profile(tmp_path / "missing.db")
            ),
        )

    assert service.catalog.get_connection_profile(datasource_id) == before


def test_a_failed_candidate_leaves_the_old_scan_active(
    service: QanerisService, tmp_path: Path
) -> None:
    datasource_id = managed_source(service, tmp_path)
    before = service.catalog.get_active_snapshot(datasource_id)
    assert before is not None

    with pytest.raises(DatasourceConnectionTestError):
        service.update_secure_datasource(
            datasource_id,
            SecureDatasourceUpdate(
                connection_profile=sqlite_profile(tmp_path / "missing.db")
            ),
        )

    assert service.catalog.get_active_snapshot(datasource_id) == before
    assert service.catalog.get_datasource(datasource_id)[0].status == "ready"
    assert service.catalog.list_datasets(datasource_id=datasource_id)


def test_a_failed_candidate_does_not_touch_the_graph(
    service: QanerisService, tmp_path: Path
) -> None:
    """The most important ordering rule: no graph deletion before the connection is proven."""
    datasource_id = managed_source(service, tmp_path)
    before = list(service.test_graph.calls)

    with pytest.raises(DatasourceConnectionTestError):
        service.update_secure_datasource(
            datasource_id,
            SecureDatasourceUpdate(
                connection_profile=sqlite_profile(tmp_path / "missing.db")
            ),
        )

    assert service.test_graph.calls == before
    assert service.test_graph.deleted == []


def test_a_failed_candidate_does_not_run_a_scan(
    service: QanerisService, tmp_path: Path
) -> None:
    datasource_id = managed_source(service, tmp_path)
    before = list(service.initializer.calls)

    with pytest.raises(DatasourceConnectionTestError):
        service.update_secure_datasource(
            datasource_id,
            SecureDatasourceUpdate(
                connection_profile=sqlite_profile(tmp_path / "missing.db")
            ),
        )

    assert service.initializer.calls == before


def test_update_of_an_unknown_datasource_reports_not_found(
    service: QanerisService, tmp_path: Path
) -> None:
    with pytest.raises(DatasourceNotFoundError) as raised:
        service.update_secure_datasource(
            "ds_missing",
            SecureDatasourceUpdate(connection_profile=sqlite_profile(tmp_path / "x.db")),
        )

    assert raised.value.code == "datasource_not_found"


def test_update_of_a_legacy_datasource_is_rejected(
    service: QanerisService, tmp_path: Path
) -> None:
    legacy = service.create_datasource(
        DatasourceCreate(
            name="legacy",
            kind=DatasourceKind.RELATIONAL,
            connection={"driver": "sqlite", "path": str(source_database(tmp_path / "l.db"))},
        )
    )

    with pytest.raises(DatasourceSecureProfileRequiredError) as raised:
        service.update_secure_datasource(
            legacy.id,
            SecureDatasourceUpdate(connection_profile=sqlite_profile(tmp_path / "l.db")),
        )

    assert raised.value.code == "datasource_secure_profile_required"


def test_a_successful_update_switches_the_profile_and_invalidates_the_scan(
    service: QanerisService, tmp_path: Path
) -> None:
    datasource_id = managed_source(service, tmp_path, "first")
    second = source_database(tmp_path / "second.db", table="invoices")

    updated = service.update_secure_datasource(
        datasource_id,
        SecureDatasourceUpdate(connection_profile=sqlite_profile(second)),
    )

    assert updated.status == "created"
    assert service.catalog.get_connection_profile(datasource_id).endpoint.path == str(second)
    assert service.catalog.get_active_snapshot(datasource_id) is None
    assert service.catalog.list_datasets(datasource_id=datasource_id) == []
    assert service.catalog.get_datasource(datasource_id)[0].status == "created"


def test_update_invalidates_before_it_returns(
    service: QanerisService, tmp_path: Path
) -> None:
    """After a successful update, inspect must not still show the old active snapshot."""
    datasource_id = managed_source(service, tmp_path, "first")
    second = source_database(tmp_path / "second.db", table="invoices")

    service.update_secure_datasource(
        datasource_id, SecureDatasourceUpdate(connection_profile=sqlite_profile(second))
    )

    detail = service.inspect_datasource(datasource_id)
    assert detail.status == "created"
    assert detail.scan_version is None
    assert detail.last_scan_status is None
    assert detail.dataset_count == 0


def test_update_deletes_the_old_graph(service: QanerisService, tmp_path: Path) -> None:
    datasource_id = managed_source(service, tmp_path, "first")
    second = source_database(tmp_path / "second.db", table="invoices")

    service.update_secure_datasource(
        datasource_id, SecureDatasourceUpdate(connection_profile=sqlite_profile(second))
    )

    assert service.test_graph.deleted == [datasource_id]


def test_update_is_not_scan(service: QanerisService, tmp_path: Path) -> None:
    """An update leaves the datasource saved; reaching READY again requires an explicit scan."""
    datasource_id = managed_source(service, tmp_path, "first")
    second = source_database(tmp_path / "second.db", table="invoices")
    scan_calls = list(service.initializer.calls)

    service.update_secure_datasource(
        datasource_id, SecureDatasourceUpdate(connection_profile=sqlite_profile(second))
    )

    assert service.initializer.calls == scan_calls


def test_scanning_after_update_publishes_a_new_version(
    service: QanerisService, tmp_path: Path
) -> None:
    datasource_id = managed_source(service, tmp_path, "first")
    first_version = service.catalog.get_active_snapshot(datasource_id).version
    second = source_database(tmp_path / "second.db", table="invoices")
    service.update_secure_datasource(
        datasource_id, SecureDatasourceUpdate(connection_profile=sqlite_profile(second))
    )

    datasets = service.scan_datasource(datasource_id)

    snapshot = service.catalog.get_active_snapshot(datasource_id)
    assert snapshot.version == first_version + 1
    assert [item.name for item in datasets] == ["invoices"]
    assert service.inspect_datasource(datasource_id).status == "ready"


def test_update_keeps_the_old_snapshot_as_history(
    service: QanerisService, tmp_path: Path
) -> None:
    datasource_id = managed_source(service, tmp_path, "first")
    first_snapshot = service.catalog.get_active_snapshot(datasource_id)
    second = source_database(tmp_path / "second.db", table="invoices")

    service.update_secure_datasource(
        datasource_id, SecureDatasourceUpdate(connection_profile=sqlite_profile(second))
    )
    service.scan_datasource(datasource_id)

    assert _snapshot_count(service.catalog.path, datasource_id) == 2
    assert _snapshot_active(service.catalog.path, first_snapshot.id) == 0
    assert service.catalog.get_active_snapshot(datasource_id).id != first_snapshot.id


# --------------------------------------------------------------------------------------------
# update: graph deletion failure aborts
# --------------------------------------------------------------------------------------------


def test_a_graph_delete_failure_aborts_the_update(
    service: QanerisService, tmp_path: Path
) -> None:
    datasource_id = managed_source(service, tmp_path, "first")
    before = service.catalog.get_connection_profile(datasource_id)
    before_snapshot = service.catalog.get_active_snapshot(datasource_id)
    second = source_database(tmp_path / "second.db", table="invoices")
    service.test_graph.fail_delete = True

    with pytest.raises(DatasourceUpdateError) as raised:
        service.update_secure_datasource(
            datasource_id, SecureDatasourceUpdate(connection_profile=sqlite_profile(second))
        )

    assert raised.value.code == "datasource_update_failed"
    assert service.catalog.get_connection_profile(datasource_id) == before
    assert service.catalog.get_active_snapshot(datasource_id) == before_snapshot
    assert service.catalog.get_datasource(datasource_id)[0].status == "ready"


def test_a_graph_delete_failure_does_not_write_the_candidate(
    service: QanerisService, tmp_path: Path
) -> None:
    datasource_id = managed_source(service, tmp_path, "first")
    second = source_database(tmp_path / "second.db", table="invoices")
    service.test_graph.fail_delete = True

    with pytest.raises(DatasourceUpdateError):
        service.update_secure_datasource(
            datasource_id, SecureDatasourceUpdate(connection_profile=sqlite_profile(second))
        )

    assert service.catalog.get_connection_profile(datasource_id).endpoint.path != str(second)


# --------------------------------------------------------------------------------------------
# update: secret rotation
# --------------------------------------------------------------------------------------------


def test_rotation_deletes_the_now_unreferenced_old_secret(
    service: QanerisService, store: ManagedCredentialStore, tmp_path: Path
) -> None:
    old = store.create(ManagedSecretKind.PASSWORD, PASSWORD_A)
    new = store.create(ManagedSecretKind.PASSWORD, PASSWORD_B)
    datasource = service.create_secure_datasource(
        SecureDatasourceCreate(
            name="pg", kind=DatasourceKind.RELATIONAL, connection_profile=probe_profile(old.id)
        )
    )
    # The candidate must connect for the update to proceed; SQLite is the reachable candidate.
    reachable = source_database(tmp_path / "candidate.db")
    candidate = probe_profile(new.id).model_copy(
        update={"endpoint": ConnectionEndpoint(path=str(reachable)), "driver": "sqlite"}
    )
    candidate.authentication = AuthenticationConfig(method=AuthenticationMethod.NONE)

    service.update_secure_datasource(
        datasource.id, SecureDatasourceUpdate(connection_profile=candidate)
    )

    assert store.exists(old.id) is False
    assert store.exists(new.id) is True


def test_a_shared_old_secret_is_retained(
    service: QanerisService, store: ManagedCredentialStore, tmp_path: Path
) -> None:
    """Another datasource still names the secret, so deleting it would break that datasource."""
    shared = store.create(ManagedSecretKind.PASSWORD, PASSWORD_A)
    first = service.catalog.create_secure_datasource(
        SecureDatasourceCreate(
            name="pg-one", kind=DatasourceKind.RELATIONAL, connection_profile=probe_profile(shared.id)
        )
    )
    service.catalog.create_secure_datasource(
        SecureDatasourceCreate(
            name="pg-two", kind=DatasourceKind.RELATIONAL, connection_profile=probe_profile(shared.id)
        )
    )
    reachable = source_database(tmp_path / "candidate.db")

    service.update_secure_datasource(
        first.id,
        SecureDatasourceUpdate(connection_profile=sqlite_profile(reachable)),
    )

    assert store.exists(shared.id) is True
    assert service.catalog.count_managed_secret_references(shared.id) == 1


def test_a_failed_rotation_keeps_both_secrets(
    service: QanerisService, store: ManagedCredentialStore, tmp_path: Path
) -> None:
    """A failed update keeps the old secret and never deletes the caller's candidate secret."""
    old = store.create(ManagedSecretKind.PASSWORD, PASSWORD_A)
    new = store.create(ManagedSecretKind.PASSWORD, PASSWORD_B)
    datasource = service.create_secure_datasource(
        SecureDatasourceCreate(
            name="pg", kind=DatasourceKind.RELATIONAL, connection_profile=probe_profile(old.id)
        )
    )
    unreachable = probe_profile(new.id).model_copy(
        update={
            "endpoint": ConnectionEndpoint(path=str(tmp_path / "missing.db")),
            "driver": "sqlite",
        }
    )
    unreachable.authentication = AuthenticationConfig(method=AuthenticationMethod.NONE)

    with pytest.raises(DatasourceConnectionTestError):
        service.update_secure_datasource(
            datasource.id, SecureDatasourceUpdate(connection_profile=unreachable)
        )

    assert store.exists(old.id) is True
    assert store.exists(new.id) is True
    assert service.catalog.get_connection_profile(datasource.id).authentication.password == managed(
        old.id
    )


def test_a_secret_cleanup_failure_does_not_fail_the_update(
    service: QanerisService, store: ManagedCredentialStore, tmp_path: Path, caplog
) -> None:
    """Cleanup is post-mutation garbage collection: it must not roll back a durable update."""
    old = store.create(ManagedSecretKind.PASSWORD, PASSWORD_A)
    datasource = service.create_secure_datasource(
        SecureDatasourceCreate(
            name="pg", kind=DatasourceKind.RELATIONAL, connection_profile=probe_profile(old.id)
        )
    )
    reachable = source_database(tmp_path / "candidate.db")

    def explode(secret_id: str) -> None:
        raise RuntimeError("simulated credential store outage")

    service._credentials().delete_secret = explode

    with caplog.at_level(logging.WARNING):
        updated = service.update_secure_datasource(
            datasource.id,
            SecureDatasourceUpdate(connection_profile=sqlite_profile(reachable)),
        )

    assert updated.status == "created"
    assert service.catalog.get_connection_profile(datasource.id).driver == "sqlite"
    assert old.id in next(record.getMessage() for record in caplog.records)


def test_a_cleanup_warning_names_the_secret_but_not_its_value(
    service: QanerisService, store: ManagedCredentialStore, tmp_path: Path, caplog
) -> None:
    old = store.create(ManagedSecretKind.PASSWORD, PASSWORD_A)
    datasource = service.create_secure_datasource(
        SecureDatasourceCreate(
            name="pg", kind=DatasourceKind.RELATIONAL, connection_profile=probe_profile(old.id)
        )
    )
    reachable = source_database(tmp_path / "candidate.db")

    def explode(secret_id: str) -> None:
        raise RuntimeError(f"simulated outage for {PASSWORD_A}")

    service._credentials().delete_secret = explode

    with caplog.at_level(logging.WARNING):
        service.update_secure_datasource(
            datasource.id,
            SecureDatasourceUpdate(connection_profile=sqlite_profile(reachable)),
        )

    messages = [record.getMessage() for record in caplog.records]
    assert old.id in messages[0]
    assert PASSWORD_A not in messages[0]


def test_an_unchanged_secret_is_not_deleted(service: QanerisService, store: ManagedCredentialStore) -> None:
    """A secret the candidate still names is in use by definition, not obsolete."""
    shared = store.create(ManagedSecretKind.PASSWORD, PASSWORD_A)
    datasource = service.create_secure_datasource(
        SecureDatasourceCreate(
            name="pg", kind=DatasourceKind.RELATIONAL, connection_profile=probe_profile(shared.id)
        )
    )

    # The candidate keeps the same secret, so the diff is empty and nothing is collected.
    service._cleanup_obsolete_secrets(probe_profile(shared.id), probe_profile(shared.id))

    assert store.exists(shared.id) is True
    assert datasource.id


# --------------------------------------------------------------------------------------------
# delete
# --------------------------------------------------------------------------------------------


def test_delete_removes_the_graph_before_the_catalog_row(
    service: QanerisService, tmp_path: Path
) -> None:
    """Graph first: catalog-first would leave a ghost datasource the graph still publishes."""
    datasource_id = managed_source(service, tmp_path)

    service.delete_datasource(datasource_id)

    assert service.test_graph.deleted == [datasource_id]
    with pytest.raises(DatasourceNotFoundError):
        service.inspect_datasource(datasource_id)


def test_delete_removes_the_owned_catalog_state(service: QanerisService, tmp_path: Path) -> None:
    datasource_id = managed_source(service, tmp_path)

    service.delete_datasource(datasource_id)

    assert service.list_datasources() == []
    assert _row_count(service.catalog.path, "dataset", datasource_id) == 0
    assert _row_count(service.catalog.path, "relation", datasource_id) == 0
    assert _row_count(service.catalog.path, "dataset_sample", datasource_id) == 0
    assert _row_count(service.catalog.path, "mapping", datasource_id) == 0
    assert _snapshot_count(service.catalog.path, datasource_id) == 0
    assert _job_count(service.catalog.path, datasource_id) == 0


def test_delete_cleans_up_an_exclusive_managed_secret(
    service: QanerisService, store: ManagedCredentialStore, tmp_path: Path
) -> None:
    secret = store.create(ManagedSecretKind.PASSWORD, PASSWORD_A)
    datasource = service.create_secure_datasource(
        SecureDatasourceCreate(
            name="pg", kind=DatasourceKind.RELATIONAL, connection_profile=probe_profile(secret.id)
        )
    )

    service.delete_datasource(datasource.id)

    assert store.exists(secret.id) is False


def test_delete_retains_a_shared_managed_secret(
    service: QanerisService, store: ManagedCredentialStore
) -> None:
    shared = store.create(ManagedSecretKind.PASSWORD, PASSWORD_A)
    first = service.create_secure_datasource(
        SecureDatasourceCreate(
            name="pg-one", kind=DatasourceKind.RELATIONAL, connection_profile=probe_profile(shared.id)
        )
    )
    service.create_secure_datasource(
        SecureDatasourceCreate(
            name="pg-two", kind=DatasourceKind.RELATIONAL, connection_profile=probe_profile(shared.id)
        )
    )

    service.delete_datasource(first.id)

    assert store.exists(shared.id) is True
    assert service.catalog.count_managed_secret_references(shared.id) == 1


def test_delete_of_an_unknown_datasource_reports_not_found(service: QanerisService) -> None:
    with pytest.raises(DatasourceNotFoundError) as raised:
        service.delete_datasource("ds_missing")

    assert raised.value.code == "datasource_not_found"


def test_a_graph_delete_failure_aborts_the_datasource_delete(
    service: QanerisService, store: ManagedCredentialStore, tmp_path: Path
) -> None:
    """If the graph cannot be removed, nothing else is: no catalog delete and no secret cleanup."""
    secret = store.create(ManagedSecretKind.PASSWORD, PASSWORD_A)
    datasource = service.create_secure_datasource(
        SecureDatasourceCreate(
            name="pg", kind=DatasourceKind.RELATIONAL, connection_profile=probe_profile(secret.id)
        )
    )
    service.test_graph.fail_delete = True

    with pytest.raises(DatasourceDeleteError) as raised:
        service.delete_datasource(datasource.id)

    assert raised.value.code == "datasource_delete_failed"
    assert [item.id for item in service.list_datasources()] == [datasource.id]
    assert store.exists(secret.id) is True


def test_delete_removes_a_legacy_datasource_without_secret_cleanup(
    service: QanerisService, tmp_path: Path
) -> None:
    """A legacy connection document names no managed secret, so there is nothing to collect."""
    legacy = service.create_datasource(
        DatasourceCreate(
            name="legacy",
            kind=DatasourceKind.RELATIONAL,
            connection={"driver": "sqlite", "path": str(source_database(tmp_path / "l.db"))},
        )
    )

    service.delete_datasource(legacy.id)

    assert service.list_datasources() == []
    assert service.test_graph.deleted == [legacy.id]


def test_delete_leaves_other_datasources_published(
    service: QanerisService, tmp_path: Path
) -> None:
    kept = managed_source(service, tmp_path, "kept")
    doomed = managed_source(service, tmp_path, "doomed")

    service.delete_datasource(doomed)

    assert service.catalog.get_active_snapshot(kept) is not None
    assert service.catalog.list_datasets(datasource_id=kept)
    assert service.test_graph.deleted == [doomed]


def test_a_deleted_datasource_cannot_be_deleted_again(service: QanerisService, tmp_path: Path) -> None:
    datasource_id = managed_source(service, tmp_path)

    service.delete_datasource(datasource_id)

    with pytest.raises(DatasourceNotFoundError):
        service.delete_datasource(datasource_id)


# --------------------------------------------------------------------------------------------
# ordering: the whole update sequence
# --------------------------------------------------------------------------------------------


def test_update_runs_test_then_graph_then_catalog_then_invalidate(
    service: QanerisService, store: ManagedCredentialStore, tmp_path: Path
) -> None:
    """The whole documented order is the contract; a reordering must not pass silently.

    Every step is recorded into one list - including the candidate connection test, which is the
    step that has to be first - so a change that moved the test after the graph deletion, or the
    cleanup before the profile switch, would fail here rather than in production.
    """
    old = store.create(ManagedSecretKind.PASSWORD, PASSWORD_A)
    datasource = service.create_secure_datasource(
        SecureDatasourceCreate(
            name="pg", kind=DatasourceKind.RELATIONAL, connection_profile=probe_profile(old.id)
        )
    )
    order: list[str] = []
    reachable = source_database(tmp_path / "candidate.db")

    original_test = service._test_secure_connection
    original_graph_delete = service.test_graph.delete_datasource_graph
    original_catalog_update = service.catalog.update_secure_datasource
    original_invalidate = service.catalog.invalidate_datasource_scan

    def tracked_test(*args, **kwargs):
        order.append("connection_test")
        return original_test(*args, **kwargs)

    def tracked_graph_delete(datasource_id: str) -> None:
        order.append("graph_delete")
        return original_graph_delete(datasource_id)

    def tracked_update(*args, **kwargs):
        order.append("catalog_update")
        return original_catalog_update(*args, **kwargs)

    def tracked_invalidate(*args, **kwargs):
        order.append("invalidate")
        return original_invalidate(*args, **kwargs)

    def tracked_delete(secret_id: str) -> None:
        order.append("secret_cleanup")

    service._test_secure_connection = tracked_test
    service.test_graph.delete_datasource_graph = tracked_graph_delete
    service.catalog.update_secure_datasource = tracked_update
    service.catalog.invalidate_datasource_scan = tracked_invalidate
    service._credentials().delete_secret = tracked_delete

    service.update_secure_datasource(
        datasource.id, SecureDatasourceUpdate(connection_profile=sqlite_profile(reachable))
    )

    assert order == [
        "connection_test",
        "graph_delete",
        "catalog_update",
        "invalidate",
        "secret_cleanup",
    ]


def test_delete_runs_graph_then_catalog_then_cleanup(
    service: QanerisService, store: ManagedCredentialStore
) -> None:
    secret = store.create(ManagedSecretKind.PASSWORD, PASSWORD_A)
    datasource = service.create_secure_datasource(
        SecureDatasourceCreate(
            name="pg", kind=DatasourceKind.RELATIONAL, connection_profile=probe_profile(secret.id)
        )
    )
    order: list[str] = []
    original_catalog_delete = service.catalog.delete_datasource
    original_graph_delete = service.test_graph.delete_datasource_graph

    def tracked_catalog_delete(datasource_id: str) -> None:
        order.append("catalog_delete")
        return original_catalog_delete(datasource_id)

    def tracked_graph_delete(datasource_id: str) -> None:
        order.append("graph_delete")
        return original_graph_delete(datasource_id)

    def tracked_delete(secret_id: str) -> None:
        order.append("secret_cleanup")

    service.catalog.delete_datasource = tracked_catalog_delete
    service.test_graph.delete_datasource_graph = tracked_graph_delete
    service._credentials().delete_secret = tracked_delete

    service.delete_datasource(datasource.id)

    assert order == ["graph_delete", "catalog_delete", "secret_cleanup"]


def test_a_candidate_test_always_precedes_any_graph_mutation(
    service: QanerisService, tmp_path: Path
) -> None:
    datasource_id = managed_source(service, tmp_path)
    service.test_graph.calls.clear()

    with pytest.raises(DatasourceConnectionTestError):
        service.update_secure_datasource(
            datasource_id,
            SecureDatasourceUpdate(
                connection_profile=sqlite_profile(tmp_path / "missing.db")
            ),
        )

    assert not any(call.startswith("delete_graph") for call in service.test_graph.calls)


# --------------------------------------------------------------------------------------------
# no raw secret anywhere
# --------------------------------------------------------------------------------------------


def test_no_secret_value_reaches_the_catalog_graph_or_logs(
    service: QanerisService, store: ManagedCredentialStore, tmp_path: Path, caplog
) -> None:
    secret = store.create(ManagedSecretKind.PASSWORD, PASSWORD_A)
    reachable = source_database(tmp_path / "candidate.db")
    candidate = sqlite_profile(reachable)

    with caplog.at_level(logging.DEBUG):
        datasource = service.create_secure_datasource(
            SecureDatasourceCreate(
                name="pg", kind=DatasourceKind.RELATIONAL, connection_profile=probe_profile(secret.id)
            )
        )
        # A failing candidate is where a driver message could most easily escape.
        try:
            service.update_secure_datasource(
                datasource.id,
                SecureDatasourceUpdate(
                    connection_profile=probe_profile(secret.id).model_copy(
                        update={
                            "endpoint": ConnectionEndpoint(path=str(tmp_path / PASSWORD_A)),
                            "driver": "sqlite",
                        }
                    )
                ),
            )
        except DatasourceConnectionTestError as error:
            assert PASSWORD_A not in str(error)

    catalog_bytes = (tmp_path / "catalog.db").read_bytes()
    assert datasource.id.encode() in catalog_bytes
    assert PASSWORD_A.encode() not in catalog_bytes
    assert all(PASSWORD_A not in record.getMessage() for record in caplog.records)
    assert candidate.driver == "sqlite"


# --------------------------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------------------------


def _row_count(catalog_path: str, table: str, datasource_id: str) -> int:
    with sqlite3.connect(catalog_path) as connection:
        return connection.execute(
            f"SELECT COUNT(*) FROM {table} WHERE datasource_id=?", (datasource_id,)
        ).fetchone()[0]


def _snapshot_count(catalog_path: str, datasource_id: str) -> int:
    return _row_count(catalog_path, "scan_snapshot", datasource_id)


def _snapshot_active(catalog_path: str, snapshot_id: str) -> int:
    with sqlite3.connect(catalog_path) as connection:
        return connection.execute(
            "SELECT active FROM scan_snapshot WHERE id=?", (snapshot_id,)
        ).fetchone()[0]


def _job_count(catalog_path: str, datasource_id: str | None = None) -> int:
    with sqlite3.connect(catalog_path) as connection:
        if datasource_id is None:
            return connection.execute("SELECT COUNT(*) FROM initialization_job").fetchone()[0]
        return connection.execute(
            "SELECT COUNT(*) FROM initialization_job WHERE datasource_id=?", (datasource_id,)
        ).fetchone()[0]
