import sqlite3
from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from smartdata.application.service import SmartDataService
from smartdata.catalog import Catalog
from smartdata.contracts import BusinessQuery, DatasourceCreate, SemanticAssetType
from smartdata.semantic import (
    MetadataLexicalSemanticRetriever,
    SemanticAsset,
    SemanticAssetBootstrap,
    SemanticAssetSeed,
    SemanticAssetStatus,
    SQLiteSemanticAssetRegistry,
)


def scanned_profile(tmp_path):
    source = tmp_path / "sales.db"
    with sqlite3.connect(source) as connection:
        connection.executescript(
            """
            CREATE TABLE orders (
                id INTEGER PRIMARY KEY,
                pay_amount REAL NOT NULL,
                region TEXT NOT NULL
            );
            INSERT INTO orders VALUES (1, 120.5, '华东');
            """
        )
    catalog_path = tmp_path / "catalog.db"
    service = SmartDataService(Catalog(catalog_path))
    datasource = service.create_datasource(
        DatasourceCreate(
            name="sales",
            kind="relational",
            connection={"driver": "sqlite", "path": str(source)},
        )
    )
    service.scan_datasource(datasource.id)
    return catalog_path, service.catalog.get_company_data_profile()


def test_real_scan_bootstrap_registry_and_retrieval_closed_loop(tmp_path) -> None:
    catalog_path, profile = scanned_profile(tmp_path)
    registry = SQLiteSemanticAssetRegistry(catalog_path)
    bootstrap = SemanticAssetBootstrap(registry)
    assets = bootstrap.load(
        profile,
        [
            SemanticAssetSeed(
                asset_id="metric_paid_sales",
                kind="metric",
                name="实收销售额",
                aliases=["销售额"],
                description="完成支付的订单金额",
                source="administrator",
                source_ref="seed://retail/paid-sales",
                status="published",
                datasource_name="sales",
                data_object_name="orders",
                field_path="pay_amount",
            ),
            SemanticAssetSeed(
                asset_id="alias_order_revenue",
                kind="alias",
                name="订单收入",
                source="enterprise_definition",
                source_ref="dictionary://retail/order-revenue",
                status="approved",
                target_asset_id="metric_paid_sales",
            ),
            SemanticAssetSeed(
                asset_id="dimension_region_suggestion",
                kind="dimension",
                name="地区建议",
                source="model_suggestion",
                source_ref="model://bootstrap/run-1",
                status="suggested",
                confidence=0.7,
                datasource_name="sales",
                data_object_name="orders",
                field_path="region",
            ),
        ],
    )

    assert len(assets) == 3
    assert SQLiteSemanticAssetRegistry(catalog_path).get("default", "metric_paid_sales") is not None

    retriever = MetadataLexicalSemanticRetriever()
    trusted_profile = registry.project_profile(profile)
    trusted_result = retriever.retrieve(
        trusted_profile,
        BusinessQuery(question="订单收入", objective="lookup", metrics=["订单收入"]),
    )
    suggestions_hidden = retriever.retrieve(
        trusted_profile,
        BusinessQuery(question="地区建议", objective="lookup", dimensions=["地区建议"]),
    )
    exploratory_result = retriever.retrieve(
        registry.project_profile(profile, include_untrusted=True),
        BusinessQuery(question="地区建议", objective="lookup", dimensions=["地区建议"]),
    )

    metric = next(
        item
        for item in trusted_result.candidates
        if item.asset_type == SemanticAssetType.METRIC
    )
    assert metric.name == "实收销售额"
    assert metric.field_path == "pay_amount"
    assert suggestions_hidden.candidates == []
    assert any(
        item.asset_type == SemanticAssetType.DIMENSION and item.field_path == "region"
        for item in exploratory_result.candidates
    )


def test_bootstrap_rejects_binding_outside_real_profile(tmp_path) -> None:
    catalog_path, profile = scanned_profile(tmp_path)
    bootstrap = SemanticAssetBootstrap(SQLiteSemanticAssetRegistry(catalog_path))

    with pytest.raises(ValueError, match="field does not exist"):
        bootstrap.load(
            profile,
            [
                SemanticAssetSeed(
                    asset_id="metric_missing",
                    kind="metric",
                    name="不存在指标",
                    source="administrator",
                    source_ref="seed://invalid",
                    status="published",
                    datasource_name="sales",
                    data_object_name="orders",
                    field_path="missing_amount",
                )
            ],
        )


def test_model_suggestion_cannot_be_trusted_without_confirmation() -> None:
    with pytest.raises(ValidationError, match="confirmed_by"):
        SemanticAssetSeed(
            asset_id="unsafe_suggestion",
            kind="metric",
            name="模型猜测指标",
            source="model_suggestion",
            source_ref="model://bootstrap/run-2",
            status="published",
            datasource_name="sales",
            data_object_name="orders",
            field_path="pay_amount",
        )


def candidate_asset(
    asset_id: str,
    *,
    source: str,
    status: str,
    confirmed_by: str | None,
) -> SemanticAsset:
    return SemanticAsset(
        asset_id=asset_id,
        workspace_id="default",
        kind="metric",
        name="模型销售额",
        aliases=["模型口径"],
        source=source,
        source_ref=f"model://bootstrap/{asset_id}",
        status=status,
        confidence=0.7,
        confirmed_by=confirmed_by,
        datasource_id="ds_sales",
        graph_data_object_id="obj_orders",
        graph_field_id="field_pay_amount",
        data_object_name="orders",
        field_path="pay_amount",
        updated_at=datetime.now(UTC),
    )


def test_trusted_governance_invariant_holds_on_direct_construction(tmp_path) -> None:
    registry = SQLiteSemanticAssetRegistry(tmp_path / "catalog.db")

    for asset_id, source, status in (
        ("smuggled_model", "model_suggestion", "published"),
        ("smuggled_model_approved", "model_suggestion", "approved"),
        ("smuggled_database", "database_metadata", "published"),
    ):
        with pytest.raises(ValidationError, match="confirmed_by"):
            candidate_asset(asset_id, source=source, status=status, confirmed_by=None)
        assert registry.get("default", asset_id) is None


def test_registry_write_boundary_refuses_a_validation_bypass(tmp_path) -> None:
    """``model_copy`` skips validation, so the registry must re-check the invariant itself."""
    registry = SQLiteSemanticAssetRegistry(tmp_path / "catalog.db")
    suggestion = candidate_asset(
        "metric_suggested", source="model_suggestion", status="suggested", confirmed_by=None
    )
    registry.save(suggestion)
    assert registry.get("default", "metric_suggested").status == "suggested"

    smuggled = suggestion.model_copy(update={"status": "published", "confirmed_by": None})

    with pytest.raises(ValueError, match="confirmed_by"):
        registry.save(smuggled)

    assert registry.get("default", "metric_suggested").status == "suggested"
    assert registry.get("default", "metric_suggested").trusted is False


def test_model_suggestion_becomes_trusted_only_after_human_confirmation(tmp_path) -> None:
    registry = SQLiteSemanticAssetRegistry(tmp_path / "catalog.db")

    confirmed = candidate_asset(
        "metric_confirmed",
        source="model_suggestion",
        status="published",
        confirmed_by="alice",
    )
    registry.save(confirmed)

    stored = registry.get("default", "metric_confirmed")
    assert stored.trusted is True
    assert stored.confirmed_by == "alice"
    assert [item.asset_id for item in registry.list("default", {SemanticAssetStatus.PUBLISHED})] == [
        "metric_confirmed"
    ]
