"""Workspace-scoped asset identity for the SQLite semantic asset registry."""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime

import pytest

from qaneris.contracts import (
    CompanyDataProfile,
    DataObjectProfile,
    DataSourceProfile,
    FieldProfile,
    NamespaceProfile,
)
from qaneris.semantic import (
    SemanticAsset,
    SemanticAssetBootstrap,
    SemanticAssetSeed,
    SQLiteSemanticAssetRegistry,
)


def asset(
    asset_id: str,
    workspace_id: str,
    name: str,
    *,
    graph_object_id: str,
    graph_field_id: str,
    aliases: tuple[str, ...] = (),
    source: str = "administrator",
    status: str = "published",
    target_asset_id: str | None = None,
) -> SemanticAsset:
    return SemanticAsset(
        asset_id=asset_id,
        workspace_id=workspace_id,
        kind="alias" if target_asset_id else "metric",
        name=name,
        aliases=list(aliases),
        source=source,
        source_ref=f"seed://{workspace_id}/{asset_id}",
        status=status,
        confidence=1.0,
        datasource_id=None if target_asset_id else f"ds_{workspace_id}",
        datasource_name=None if target_asset_id else workspace_id,
        graph_data_object_id=None if target_asset_id else graph_object_id,
        graph_field_id=None if target_asset_id else graph_field_id,
        data_object_name=None if target_asset_id else "orders",
        field_path=None if target_asset_id else "amount",
        target_asset_id=target_asset_id,
        updated_at=datetime.now(UTC),
    )


def test_two_workspaces_can_own_the_same_asset_id(tmp_path) -> None:
    registry = SQLiteSemanticAssetRegistry(tmp_path / "catalog.db")
    registry.save(
        asset(
            "metric_sales",
            "workspace_a",
            "A 的销售额",
            graph_object_id="obj_a",
            graph_field_id="field_a",
            aliases=("A 口径",),
        )
    )
    registry.save(
        asset(
            "metric_sales",
            "workspace_b",
            "B 的销售额",
            graph_object_id="obj_b",
            graph_field_id="field_b",
            aliases=("B 口径",),
        )
    )

    # Both rows coexist instead of one overwriting the other.
    listed_a = [item.asset_id for item in registry.list("workspace_a")]
    listed_b = [item.asset_id for item in registry.list("workspace_b")]
    assert listed_a == ["metric_sales"]
    assert listed_b == ["metric_sales"]

    stored_a = registry.get("workspace_a", "metric_sales")
    stored_b = registry.get("workspace_b", "metric_sales")
    assert stored_a is not None and stored_b is not None
    assert stored_a.name == "A 的销售额"
    assert stored_b.name == "B 的销售额"
    assert stored_a.graph_field_id == "field_a"
    assert stored_b.graph_field_id == "field_b"
    assert stored_a.aliases == ["A 口径"]
    assert registry.get("workspace_c", "metric_sales") is None


def test_updating_one_workspace_does_not_touch_the_other(tmp_path) -> None:
    registry = SQLiteSemanticAssetRegistry(tmp_path / "catalog.db")
    registry.save(
        asset(
            "metric_sales",
            "workspace_a",
            "A 的销售额",
            graph_object_id="obj_a",
            graph_field_id="field_a",
        )
    )
    registry.save(
        asset(
            "metric_sales",
            "workspace_b",
            "B 的销售额",
            graph_object_id="obj_b",
            graph_field_id="field_b",
        )
    )

    registry.save(
        asset(
            "metric_sales",
            "workspace_b",
            "B 的销售额（更新）",
            graph_object_id="obj_b2",
            graph_field_id="field_b2",
        )
    )

    stored_a = registry.get("workspace_a", "metric_sales")
    stored_b = registry.get("workspace_b", "metric_sales")
    assert stored_a.name == "A 的销售额"
    assert stored_a.graph_field_id == "field_a"
    assert stored_b.name == "B 的销售额（更新）"
    assert stored_b.graph_field_id == "field_b2"


def workspace_profile(workspace_id: str) -> CompanyDataProfile:
    return CompanyDataProfile(
        workspace_id=workspace_id,
        data_sources=[
            DataSourceProfile(
                datasource_id=f"ds_{workspace_id}",
                name=workspace_id,
                kind="relational",
                driver="sqlite",
                namespaces=[
                    NamespaceProfile(
                        name="public",
                        data_objects=[
                            DataObjectProfile(
                                id=f"profile_{workspace_id}",
                                datasource_id=f"ds_{workspace_id}",
                                namespace="public",
                                name="orders",
                                object_kind="table",
                                fields=[
                                    FieldProfile(name="amount", path="amount", data_type="decimal")
                                ],
                            )
                        ],
                    )
                ],
            )
        ],
        generated_at=datetime.now(UTC),
    )


def metric_seed(workspace_id: str, seed_id: str) -> SemanticAssetSeed:
    return SemanticAssetSeed(
        asset_id=seed_id,
        workspace_id=workspace_id,
        kind="metric",
        name="销售额",
        source="administrator",
        source_ref=f"seed://{workspace_id}/{seed_id}",
        status="published",
        datasource_name=workspace_id,
        namespace="public",
        data_object_name="orders",
        field_path="amount",
    )


def alias_seed(workspace_id: str, seed_id: str) -> SemanticAssetSeed:
    return SemanticAssetSeed(
        asset_id=seed_id,
        workspace_id=workspace_id,
        kind="alias",
        name="订单收入",
        source="enterprise_definition",
        source_ref=f"seed://{workspace_id}/{seed_id}",
        status="approved",
        target_asset_id="metric_sales",
    )


def test_alias_target_resolution_does_not_cross_workspaces(tmp_path) -> None:
    registry = SQLiteSemanticAssetRegistry(tmp_path / "catalog.db")
    # Workspace A owns metric_sales; workspace B must not be able to alias it.
    registry.save(
        asset(
            "metric_sales",
            "workspace_a",
            "A 的销售额",
            graph_object_id="obj_a",
            graph_field_id="field_a",
        )
    )
    bootstrap = SemanticAssetBootstrap(registry)

    with pytest.raises(ValueError, match="alias target does not exist"):
        bootstrap.load(workspace_profile("workspace_b"), [alias_seed("workspace_b", "alias_b")])

    bootstrap.load(
        workspace_profile("workspace_b"),
        [metric_seed("workspace_b", "metric_sales"), alias_seed("workspace_b", "alias_b")],
    )

    assert [item.asset_id for item in registry.list("workspace_a")] == ["metric_sales"]
    assert [item.asset_id for item in registry.list("workspace_b")] == [
        "alias_b",
        "metric_sales",
    ]
    # Workspace A's asset is untouched by the workspace B load.
    assert registry.get("workspace_a", "metric_sales").name == "A 的销售额"
    assert registry.get("workspace_b", "metric_sales").name == "销售额"


def primary_key_columns(path) -> list[str]:
    with sqlite3.connect(path) as connection:
        connection.row_factory = sqlite3.Row
        rows = connection.execute("PRAGMA table_info(semantic_asset)").fetchall()
        tables = {
            item["name"]
            for item in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
    assert "semantic_asset_legacy" not in tables
    return [row["name"] for row in sorted(rows, key=lambda item: item["pk"]) if row["pk"]]


def test_legacy_asset_id_keyed_table_is_migrated_without_data_loss(tmp_path) -> None:
    path = tmp_path / "catalog.db"
    old_style = asset(
        "metric_legacy",
        "workspace_a",
        "旧版销售额",
        graph_object_id="obj_old",
        graph_field_id="field_old",
    )
    # A row written before P1-04A.1 has no graph identity or readable binding metadata at all.
    restored = json.loads(old_style.model_dump_json(exclude={"trusted"}))
    for key in ("graph_data_object_id", "graph_field_id", "namespace", "qualified_name"):
        restored.pop(key, None)
    with sqlite3.connect(path) as connection:
        connection.executescript(
            """
            CREATE TABLE semantic_asset (
                asset_id TEXT PRIMARY KEY,
                workspace_id TEXT NOT NULL,
                kind TEXT NOT NULL,
                name TEXT NOT NULL,
                status TEXT NOT NULL,
                asset_json TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE INDEX semantic_asset_workspace_status
                ON semantic_asset(workspace_id, status);
            """
        )
        connection.execute(
            "INSERT INTO semantic_asset VALUES(?,?,?,?,?,?,?)",
            (
                old_style.asset_id,
                old_style.workspace_id,
                old_style.kind.value,
                old_style.name,
                old_style.status.value,
                json.dumps(restored),
                old_style.updated_at.isoformat(),
            ),
        )
        connection.execute(
            "INSERT INTO semantic_asset VALUES(?,?,?,?,?,?,?)",
            (
                "metric_other",
                "workspace_b",
                "metric",
                "另一个指标",
                "published",
                asset(
                    "metric_other",
                    "workspace_b",
                    "另一个指标",
                    graph_object_id="obj_b",
                    graph_field_id="field_b",
                ).model_dump_json(exclude={"trusted"}),
                old_style.updated_at.isoformat(),
            ),
        )

    registry = SQLiteSemanticAssetRegistry(path)

    assert primary_key_columns(path) == ["workspace_id", "asset_id"]
    assert registry.get("workspace_a", "metric_legacy").name == "旧版销售额"
    assert registry.get("workspace_b", "metric_other").name == "另一个指标"

    # The workspace-scoped key is in force after migration.
    registry.save(
        asset(
            "metric_legacy",
            "workspace_b",
            "B 的旧版销售额",
            graph_object_id="obj_b",
            graph_field_id="field_b",
        )
    )
    assert registry.get("workspace_a", "metric_legacy").name == "旧版销售额"
    assert registry.get("workspace_b", "metric_legacy").name == "B 的旧版销售额"


def test_migration_is_idempotent_and_keeps_an_already_scoped_table(tmp_path) -> None:
    path = tmp_path / "catalog.db"
    first = SQLiteSemanticAssetRegistry(path)
    first.save(
        asset(
            "metric_sales",
            "workspace_a",
            "A 的销售额",
            graph_object_id="obj_a",
            graph_field_id="field_a",
        )
    )

    second = SQLiteSemanticAssetRegistry(path)

    assert primary_key_columns(path) == ["workspace_id", "asset_id"]
    assert second.get("workspace_a", "metric_sales").name == "A 的销售额"
    assert second.list("workspace_b") == []
