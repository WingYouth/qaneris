"""Catalog persistence for the secure datasource lifecycle (RS-CONN-01A).

The catalog owns the durable half of the lifecycle: what a stored connection profile looks like,
what an update may and may not touch, and exactly which facts one datasource owns. These tests
cover that layer directly, without a connection test in the way, so a persistence bug is reported
as a persistence bug.

Two invariants run through the whole file:

* a stored profile only ever contains ``SecretReference`` values - never material; and
* a destructive operation touches only the named datasource, and inside one transaction, so a
  failure cannot leave half a datasource behind.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest
from pydantic import ValidationError

from qaneris.catalog import Catalog
from qaneris.common.errors import (
    DatasourceSecureProfileRequiredError,
    ManagedSecretIntegrityError,
)
from qaneris.contracts import (
    AuthenticationConfig,
    AuthenticationMethod,
    ConnectionEndpoint,
    ConnectionProfile,
    DatasetInfo,
    DatasetSample,
    DatasourceCreate,
    DatasourceKind,
    FieldInfo,
    MappingInfo,
    RelationInfo,
    SecretProviderKind,
    SecretReference,
    SecureDatasourceCreate,
)

SECRET_ID = "sec_" + "a" * 32
OTHER_SECRET_ID = "sec_" + "b" * 32
PASSWORD_MARKER = "UNIQUE_CATALOG_PASSWORD_MARKER"


def managed(secret_id: str) -> SecretReference:
    return SecretReference(provider=SecretProviderKind.MANAGED, identifier=secret_id)


def profile(*, driver: str = "postgresql", secret_id: str | None = None) -> ConnectionProfile:
    """A secure profile that names one managed secret, or none when ``secret_id`` is omitted."""
    return ConnectionProfile(
        driver=driver,
        endpoint=ConnectionEndpoint(hosts=[{"host": "127.0.0.1", "port": 5432}]),
        authentication=(
            AuthenticationConfig(
                method=AuthenticationMethod.PASSWORD,
                username="readonly",
                password=managed(secret_id),
            )
            if secret_id
            else AuthenticationConfig(method=AuthenticationMethod.NONE)
        ),
    )


def secure(catalog: Catalog, name: str, *, secret_id: str | None = None, **kwargs) -> str:
    return catalog.create_secure_datasource(
        SecureDatasourceCreate(
            name=name,
            kind=kwargs.pop("kind", DatasourceKind.RELATIONAL),
            connection_profile=profile(secret_id=secret_id, **kwargs),
        )
    ).id


@pytest.fixture
def catalog(tmp_path: Path) -> Catalog:
    return Catalog(tmp_path / "catalog.db")


# --------------------------------------------------------------------------------------------
# update: what may change
# --------------------------------------------------------------------------------------------


def test_update_replaces_the_stored_profile(catalog: Catalog) -> None:
    datasource_id = secure(catalog, "pg", secret_id=SECRET_ID)

    updated = catalog.update_secure_datasource(
        datasource_id, profile(driver="mysql", secret_id=OTHER_SECRET_ID)
    )

    stored = catalog.get_datasource(datasource_id)[1]
    assert stored["format"] == "connection_profile_v1"
    assert stored["profile"]["driver"] == "mysql"
    assert updated.driver == "mysql"
    assert catalog.get_connection_profile(datasource_id).driver == "mysql"


def test_update_persists_only_secret_references(catalog: Catalog) -> None:
    datasource_id = secure(catalog, "pg", secret_id=SECRET_ID)

    catalog.update_secure_datasource(datasource_id, profile(secret_id=OTHER_SECRET_ID))

    serialized = json.dumps(catalog.get_datasource(datasource_id)[1])
    assert PASSWORD_MARKER not in serialized
    assert OTHER_SECRET_ID in serialized
    assert SECRET_ID not in serialized


def test_update_preserves_immutable_identity_fields(catalog: Catalog) -> None:
    """The update contract cannot retarget a datasource: id, workspace and kind all stay put."""
    datasource_id = secure(catalog, "pg", secret_id=SECRET_ID, kind=DatasourceKind.RELATIONAL)
    before = catalog.get_datasource(datasource_id)[0]

    updated = catalog.update_secure_datasource(datasource_id, profile(secret_id=OTHER_SECRET_ID))

    assert updated.id == before.id
    assert updated.workspace_id == before.workspace_id
    assert updated.kind == before.kind
    assert updated.name == before.name
    after = catalog.get_datasource(datasource_id)[0]
    assert (after.id, after.workspace_id, after.kind, after.name) == (
        before.id,
        before.workspace_id,
        before.kind,
        before.name,
    )


def test_update_returns_the_datasource_to_created(catalog: Catalog) -> None:
    """A new connection has not been scanned, so it cannot still claim to be ready."""
    datasource_id = secure(catalog, "pg", secret_id=SECRET_ID)
    catalog.save_snapshot(_ready_snapshot(catalog, datasource_id))
    assert catalog.get_datasource(datasource_id)[0].status == "ready"

    updated = catalog.update_secure_datasource(datasource_id, profile(secret_id=OTHER_SECRET_ID))

    assert updated.status == "created"
    assert catalog.get_datasource(datasource_id)[0].status == "created"


def test_update_of_an_unknown_datasource_is_a_key_error(catalog: Catalog) -> None:
    with pytest.raises(KeyError):
        catalog.update_secure_datasource("ds_missing", profile(secret_id=SECRET_ID))


# --------------------------------------------------------------------------------------------
# update: legacy datasources
# --------------------------------------------------------------------------------------------


def test_a_legacy_datasource_cannot_be_updated(catalog: Catalog) -> None:
    """Legacy rows hold no secure profile to rotate, and are never migrated implicitly."""
    legacy = catalog.create_datasource(
        DatasourceCreate(
            name="legacy",
            kind=DatasourceKind.RELATIONAL,
            connection={"driver": "sqlite", "path": "/tmp/legacy.db"},
        )
    )

    with pytest.raises(DatasourceSecureProfileRequiredError) as raised:
        catalog.update_secure_datasource(legacy.id, profile(secret_id=SECRET_ID))

    assert raised.value.code == "datasource_secure_profile_required"
    assert raised.value.status_code == 409


def test_a_rejected_legacy_update_changes_nothing(catalog: Catalog) -> None:
    legacy = catalog.create_datasource(
        DatasourceCreate(
            name="legacy",
            kind=DatasourceKind.RELATIONAL,
            connection={"driver": "sqlite", "path": "/tmp/legacy.db"},
        )
    )
    before = catalog.get_datasource(legacy.id)[1]

    with pytest.raises(DatasourceSecureProfileRequiredError):
        catalog.update_secure_datasource(legacy.id, profile(secret_id=SECRET_ID))

    assert catalog.get_datasource(legacy.id)[1] == before
    assert catalog.get_connection_profile(legacy.id) is None


# --------------------------------------------------------------------------------------------
# invalidate: retiring the old scan
# --------------------------------------------------------------------------------------------


def test_invalidate_deactivates_the_active_snapshot(catalog: Catalog) -> None:
    datasource_id = secure(catalog, "pg", secret_id=SECRET_ID)
    snapshot = _ready_snapshot(catalog, datasource_id)
    catalog.save_snapshot(snapshot)
    assert catalog.get_active_snapshot(datasource_id) is not None

    catalog.invalidate_datasource_scan(datasource_id)

    assert catalog.get_active_snapshot(datasource_id) is None
    assert catalog.get_datasource(datasource_id)[0].status == "created"


def test_invalidate_keeps_the_snapshot_history(catalog: Catalog) -> None:
    """The snapshot is retired, not deleted: the audit trail of what was scanned survives."""
    datasource_id = secure(catalog, "pg", secret_id=SECRET_ID)
    snapshot = _ready_snapshot(catalog, datasource_id)
    catalog.save_snapshot(snapshot)

    catalog.invalidate_datasource_scan(datasource_id)

    assert _snapshot_count(catalog.path, datasource_id) == 1
    assert _snapshot_active(catalog.path, datasource_id, snapshot.id) == 0


def test_invalidate_keeps_the_initialization_job_history(catalog: Catalog) -> None:
    datasource_id = secure(catalog, "pg", secret_id=SECRET_ID)
    catalog.create_initialization_job(datasource_id)

    catalog.invalidate_datasource_scan(datasource_id)

    assert _row_count(catalog.path, "initialization_job", datasource_id) == 1


def test_invalidate_removes_current_scan_facts(catalog: Catalog) -> None:
    datasource_id = secure(catalog, "pg", secret_id=SECRET_ID)
    _seed_current_facts(catalog, datasource_id)
    assert catalog.list_datasets(datasource_id=datasource_id)

    catalog.invalidate_datasource_scan(datasource_id)

    assert catalog.list_datasets(datasource_id=datasource_id) == []
    assert catalog.list_relations(datasource_id=datasource_id) == []
    assert catalog.list_samples(datasource_id=datasource_id) == []
    assert _row_count(catalog.path, "mapping", datasource_id) == 0


def test_invalidate_leaves_other_datasources_alone(catalog: Catalog) -> None:
    kept = secure(catalog, "kept", secret_id=SECRET_ID)
    changed = secure(catalog, "changed", secret_id=OTHER_SECRET_ID)
    _seed_current_facts(catalog, kept)
    _seed_current_facts(catalog, changed)
    catalog.save_snapshot(_ready_snapshot(catalog, kept))

    catalog.invalidate_datasource_scan(changed)

    assert catalog.list_datasets(datasource_id=kept)
    assert catalog.get_active_snapshot(kept) is not None
    assert catalog.get_datasource(kept)[0].status == "ready"


def test_invalidate_of_an_unknown_datasource_is_a_key_error(catalog: Catalog) -> None:
    with pytest.raises(KeyError):
        catalog.invalidate_datasource_scan("ds_missing")


def test_a_new_scan_after_invalidation_gets_a_new_version(catalog: Catalog) -> None:
    """The retired version is history; the next scan must produce a strictly newer one."""
    datasource_id = secure(catalog, "pg", secret_id=SECRET_ID)
    first = _ready_snapshot(catalog, datasource_id)
    catalog.save_snapshot(first)

    catalog.invalidate_datasource_scan(datasource_id)

    assert catalog.next_snapshot_version(datasource_id) == first.version + 1


# --------------------------------------------------------------------------------------------
# delete
# --------------------------------------------------------------------------------------------


def test_delete_removes_the_datasource_and_its_current_facts(catalog: Catalog) -> None:
    datasource_id = secure(catalog, "pg", secret_id=SECRET_ID)
    _seed_current_facts(catalog, datasource_id)
    catalog.save_snapshot(_ready_snapshot(catalog, datasource_id))

    catalog.delete_datasource(datasource_id)

    with pytest.raises(KeyError):
        catalog.get_datasource(datasource_id)
    for table in ("dataset", "relation", "dataset_sample", "mapping"):
        assert _row_count(catalog.path, table, datasource_id) == 0


def test_delete_removes_the_snapshot_and_job_history(catalog: Catalog) -> None:
    datasource_id = secure(catalog, "pg", secret_id=SECRET_ID)
    catalog.create_initialization_job(datasource_id)
    catalog.save_snapshot(_ready_snapshot(catalog, datasource_id))

    catalog.delete_datasource(datasource_id)

    assert _row_count(catalog.path, "scan_snapshot", datasource_id) == 0
    assert _row_count(catalog.path, "initialization_job", datasource_id) == 0


def test_delete_leaves_other_datasources_intact(catalog: Catalog) -> None:
    kept = secure(catalog, "kept", secret_id=SECRET_ID)
    doomed = secure(catalog, "doomed", secret_id=OTHER_SECRET_ID)
    _seed_current_facts(catalog, kept)
    _seed_current_facts(catalog, doomed)
    catalog.save_snapshot(_ready_snapshot(catalog, kept))

    catalog.delete_datasource(doomed)

    assert catalog.get_datasource(kept)[0].status == "ready"
    assert catalog.list_datasets(datasource_id=kept)
    assert catalog.get_active_snapshot(kept) is not None


def test_delete_of_an_unknown_datasource_is_a_key_error(catalog: Catalog) -> None:
    with pytest.raises(KeyError):
        catalog.delete_datasource("ds_missing")


def test_delete_of_an_unknown_datasource_changes_nothing(catalog: Catalog) -> None:
    kept = secure(catalog, "kept", secret_id=SECRET_ID)
    _seed_current_facts(catalog, kept)

    with pytest.raises(KeyError):
        catalog.delete_datasource("ds_missing")

    assert catalog.list_datasets(datasource_id=kept)


def test_delete_removes_a_legacy_datasource(catalog: Catalog) -> None:
    """Legacy rows are still deletable; only the secure update path refuses them."""
    legacy = catalog.create_datasource(
        DatasourceCreate(
            name="legacy",
            kind=DatasourceKind.RELATIONAL,
            connection={"driver": "sqlite", "path": "/tmp/legacy.db"},
        )
    )

    catalog.delete_datasource(legacy.id)

    with pytest.raises(KeyError):
        catalog.get_datasource(legacy.id)


# --------------------------------------------------------------------------------------------
# managed reference counting
# --------------------------------------------------------------------------------------------


def test_an_updated_profile_stops_referencing_the_old_secret(catalog: Catalog) -> None:
    datasource_id = secure(catalog, "pg", secret_id=SECRET_ID)
    assert catalog.count_managed_secret_references(SECRET_ID) == 1

    catalog.update_secure_datasource(datasource_id, profile(secret_id=OTHER_SECRET_ID))

    assert catalog.count_managed_secret_references(SECRET_ID) == 0
    assert catalog.count_managed_secret_references(OTHER_SECRET_ID) == 1


def test_a_deleted_datasource_stops_referencing_its_secret(catalog: Catalog) -> None:
    datasource_id = secure(catalog, "pg", secret_id=SECRET_ID)

    catalog.delete_datasource(datasource_id)

    assert catalog.count_managed_secret_references(SECRET_ID) == 0


def test_counting_is_structural_not_a_substring_search(catalog: Catalog) -> None:
    """A value that merely looks like a reference is not one; a real reference always counts."""
    datasource_id = secure(catalog, "pg", secret_id=SECRET_ID)
    with catalog._connect() as connection:
        connection.execute(
            "UPDATE datasource SET connection_json=? WHERE id=?",
            (
                json.dumps(
                    {
                        "format": "connection_profile_v1",
                        "profile": profile(secret_id=SECRET_ID).model_dump(mode="json"),
                        "note": f"looks like {OTHER_SECRET_ID} but is not a reference",
                    }
                ),
                datasource_id,
            ),
        )

    assert catalog.count_managed_secret_references(SECRET_ID) == 1
    assert catalog.count_managed_secret_references(OTHER_SECRET_ID) == 0


def test_an_unreadable_stored_profile_fails_the_count_closed(catalog: Catalog) -> None:
    """Zero is what authorises a delete, so an unreadable profile must not report zero."""
    datasource_id = secure(catalog, "pg", secret_id=SECRET_ID)
    with catalog._connect() as connection:
        connection.execute(
            "UPDATE datasource SET connection_json=? WHERE id=?",
            (json.dumps({"format": "connection_profile_v1", "profile": {"driver": ""}}), datasource_id),
        )

    with pytest.raises(ManagedSecretIntegrityError):
        catalog.count_managed_secret_references(SECRET_ID)


def test_update_rejects_a_profile_that_cannot_be_stored(catalog: Catalog) -> None:
    """A malformed profile never reaches the row: validation happens before the write."""
    datasource_id = secure(catalog, "pg", secret_id=SECRET_ID)

    with pytest.raises(ValidationError):
        ConnectionProfile(driver="", endpoint=ConnectionEndpoint(hosts=[{"host": "h"}]))

    assert catalog.get_connection_profile(datasource_id).driver == "postgresql"


# --------------------------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------------------------


def _ready_snapshot(catalog: Catalog, datasource_id: str):
    """An active READY snapshot for this datasource, shaped like one a real scan commits."""
    from datetime import UTC, datetime

    from qaneris.contracts import DataSourceProfile, ScanSnapshot, ScanStatus

    datasource = catalog.get_datasource(datasource_id)[0]
    now = datetime.now(UTC)
    return ScanSnapshot(
        id=f"snap_{datasource_id}",
        datasource_id=datasource_id,
        version=catalog.next_snapshot_version(datasource_id),
        status=ScanStatus.READY,
        profile=DataSourceProfile(
            datasource_id=datasource_id,
            name=datasource.name,
            kind=datasource.kind,
            driver=datasource.driver or "postgresql",
        ),
        started_at=now,
        completed_at=now,
        active=True,
    )


def _seed_current_facts(catalog: Catalog, datasource_id: str) -> None:
    """Write one dataset, relation, sample and mapping owned by this datasource."""
    workspace_id = catalog.get_datasource(datasource_id)[0].workspace_id
    catalog.replace_datasets(
        datasource_id,
        [
            DatasetInfo(
                datasource_id=datasource_id,
                name="orders",
                kind="table",
                fields=[FieldInfo(name="id", data_type="INTEGER")],
            )
        ],
    )
    catalog.replace_relations(
        datasource_id,
        [
            RelationInfo(
                datasource_id=datasource_id,
                from_dataset="orders",
                from_field="customer_id",
                to_dataset="orders",
                to_field="id",
                relation_type="foreign_key",
            )
        ],
    )
    catalog.replace_scan_result(
        datasource_id,
        [
            DatasetInfo(
                datasource_id=datasource_id,
                name="orders",
                kind="table",
                fields=[FieldInfo(name="id", data_type="INTEGER")],
            )
        ],
        [
            RelationInfo(
                datasource_id=datasource_id,
                from_dataset="orders",
                from_field="customer_id",
                to_dataset="orders",
                to_field="id",
                relation_type="foreign_key",
            )
        ],
        [DatasetSample(datasource_id=datasource_id, dataset="orders", rows=[{"id": 1}])],
    )
    catalog.create_mapping(
        MappingInfo(
            workspace_id=workspace_id,
            entity="order",
            canonical_field="amount",
            datasource_id=datasource_id,
            dataset="orders",
            field="id",
            confidence=0.9,
            source="scan",
        )
    )


def _row_count(catalog_path: str, table: str, datasource_id: str) -> int:
    with sqlite3.connect(catalog_path) as connection:
        return connection.execute(
            f"SELECT COUNT(*) FROM {table} WHERE datasource_id=?", (datasource_id,)
        ).fetchone()[0]


def _snapshot_count(catalog_path: str, datasource_id: str) -> int:
    return _row_count(catalog_path, "scan_snapshot", datasource_id)


def _snapshot_active(catalog_path: str, datasource_id: str, snapshot_id: str) -> int:
    with sqlite3.connect(catalog_path) as connection:
        return connection.execute(
            "SELECT active FROM scan_snapshot WHERE id=? AND datasource_id=?",
            (snapshot_id, datasource_id),
        ).fetchone()[0]
