from __future__ import annotations

from datetime import UTC, datetime

import pytest

from smartdata.common.errors import GraphUnavailableError
from smartdata.contracts import SemanticAssetType
from smartdata.contracts.semantic import BusinessQuery
from smartdata.graph import (
    GraphBindingReference,
    GraphDataObject,
    GraphDatasource,
    GraphField,
    GraphRelationship,
    GraphStructure,
    GraphStructureRequest,
    NullGraphReader,
)
from smartdata.semantic import (
    GraphSemanticRetriever,
    SemanticAsset,
    SemanticRetrievalPath,
    SemanticRetrievalResult,
    SQLiteSemanticAssetRegistry,
)


class InMemoryGraphReader:
    """Reader double mirroring how ``Neo4jGraphReader`` scopes and resolves a read.

    Like the real reader it fetches a superset and leaves precise identity matching to
    ``GraphStructure``: references are resolved by graph id or by the readable membership lists.
    """

    def __init__(self, structure: GraphStructure):
        self.structure = structure
        self.requests: list[GraphStructureRequest] = []

    def read_structure(self, request: GraphStructureRequest) -> GraphStructure:
        self.requests.append(request)
        scope = request.datasource_id
        datasources = [
            item
            for item in self.structure.datasources
            if scope is None or item.datasource_id == scope
        ]
        if not datasources:
            return GraphStructure()
        datasource_ids = {item.datasource_id for item in datasources}
        data_objects = [
            item for item in self.structure.data_objects if item.datasource_id in datasource_ids
        ]
        object_ids = {item.node_id for item in data_objects}
        bounded = GraphStructure(
            datasources=datasources,
            data_objects=data_objects,
            fields=[item for item in self.structure.fields if item.object_id in object_ids],
            relationships=[
                item
                for item in self.structure.relationships
                if item.from_datasource_id in datasource_ids
                and item.to_datasource_id in datasource_ids
            ],
        ).sorted()
        return bounded.merged(self._resolve(request.references)).sorted()

    def _resolve(self, references: list[GraphBindingReference]) -> GraphStructure:
        object_ids = {item.graph_data_object_id for item in references if item.graph_data_object_id}
        field_ids = {item.graph_field_id for item in references if item.graph_field_id}
        datasource_ids = {item.datasource_id for item in references if item.datasource_id}
        names = {item.data_object_name for item in references if item.data_object_name}
        qualified_names = {item.qualified_name for item in references if item.qualified_name}
        field_paths = {item.field_path for item in references if item.field_path}
        data_objects = [
            item
            for item in self.structure.data_objects
            if item.node_id in object_ids
            or (
                item.datasource_id in datasource_ids
                and (item.name in names or item.qualified_name in qualified_names)
            )
        ]
        resolved_object_ids = {item.node_id for item in data_objects}
        fields = [
            item
            for item in self.structure.fields
            if item.node_id in field_ids
            or (
                item.object_id in resolved_object_ids
                and item.path in field_paths
                and item.datasource_id in datasource_ids
            )
        ]
        return GraphStructure(data_objects=data_objects, fields=fields)


def graph_structure() -> GraphStructure:
    return GraphStructure(
        datasources=[
            GraphDatasource(
                node_id="db_sales",
                datasource_id="ds_sales",
                workspace_id="default",
                name="sales",
                kind="relational",
                driver="postgresql",
            ),
            GraphDatasource(
                node_id="db_other",
                datasource_id="ds_other",
                workspace_id="default",
                name="other",
                kind="relational",
                driver="mysql",
            ),
        ],
        data_objects=[
            GraphDataObject(
                node_id="obj_orders",
                datasource_id="ds_sales",
                namespace="public",
                name="orders",
                qualified_name="public.orders",
                object_kind="table",
            ),
            GraphDataObject(
                node_id="obj_customers",
                datasource_id="ds_sales",
                namespace="public",
                name="customers",
                qualified_name="public.customers",
                object_kind="table",
            ),
            GraphDataObject(
                node_id="obj_other_orders",
                datasource_id="ds_other",
                name="other_orders",
                object_kind="table",
            ),
        ],
        fields=[
            GraphField(
                node_id="f_pay",
                object_id="obj_orders",
                datasource_id="ds_sales",
                object_name="orders",
                name="pay_amount",
                path="pay_amount",
                data_type="decimal",
            ),
            GraphField(
                node_id="f_gross",
                object_id="obj_orders",
                datasource_id="ds_sales",
                object_name="orders",
                name="gross_amount",
                path="gross_amount",
                data_type="decimal",
            ),
            GraphField(
                node_id="f_region",
                object_id="obj_orders",
                datasource_id="ds_sales",
                object_name="orders",
                name="region",
                path="region",
                data_type="text",
            ),
            GraphField(
                node_id="f_customer_id",
                object_id="obj_orders",
                datasource_id="ds_sales",
                object_name="orders",
                name="customer_id",
                path="customer_id",
                data_type="integer",
            ),
            GraphField(
                node_id="f_other_amount",
                object_id="obj_other_orders",
                datasource_id="ds_other",
                object_name="other_orders",
                name="pay_amount",
                path="pay_amount",
                data_type="decimal",
            ),
        ],
        relationships=[
            GraphRelationship(
                relationship_id="edge_orders_customers",
                relationship_type="foreign_key",
                from_object_id="obj_orders",
                from_datasource_id="ds_sales",
                from_object_name="orders",
                from_field_path="customer_id",
                to_object_id="obj_customers",
                to_datasource_id="ds_sales",
                to_object_name="customers",
                to_field_path="id",
                source="database",
            )
        ],
    )


def semantic_asset(
    asset_id: str,
    kind: str,
    name: str,
    *,
    aliases: tuple[str, ...] = (),
    datasource_id: str | None = None,
    datasource_name: str | None = None,
    graph_data_object_id: str | None = None,
    graph_field_id: str | None = None,
    namespace: str | None = None,
    data_object_id: str | None = None,
    data_object_name: str | None = None,
    qualified_name: str | None = None,
    field_path: str | None = None,
    source: str = "administrator",
    status: str = "published",
    confidence: float = 1.0,
    target_asset_id: str | None = None,
    description: str | None = None,
) -> SemanticAsset:
    return SemanticAsset(
        asset_id=asset_id,
        workspace_id="default",
        kind=kind,
        name=name,
        aliases=list(aliases),
        description=description,
        source=source,
        source_ref=f"seed://retail/{asset_id}",
        status=status,
        confidence=confidence,
        datasource_id=datasource_id,
        datasource_name=datasource_name,
        graph_data_object_id=graph_data_object_id,
        graph_field_id=graph_field_id,
        namespace=namespace,
        data_object_id=data_object_id,
        data_object_name=data_object_name,
        qualified_name=qualified_name,
        field_path=field_path,
        target_asset_id=target_asset_id,
        updated_at=datetime.now(UTC),
    )


def governed_assets() -> list[SemanticAsset]:
    """Registry contents. Graph identity and migration-era profile ids are deliberately different."""
    return [
        semantic_asset(
            "metric_paid_sales",
            "metric",
            "实收销售额",
            aliases=("销售额", "实收金额"),
            description="完成支付的订单金额",
            datasource_id="ds_sales",
            datasource_name="sales",
            graph_data_object_id="obj_orders",
            graph_field_id="f_pay",
            namespace="public",
            data_object_id="object_orders",
            data_object_name="orders",
            qualified_name="public.orders",
            field_path="pay_amount",
        ),
        semantic_asset(
            "metric_gross_sales",
            "metric",
            "成交总额",
            aliases=("销售额", "GMV"),
            datasource_id="ds_sales",
            datasource_name="sales",
            graph_data_object_id="obj_orders",
            graph_field_id="f_gross",
            namespace="public",
            data_object_id="object_orders",
            data_object_name="orders",
            qualified_name="public.orders",
            field_path="gross_amount",
        ),
        semantic_asset(
            "dimension_region",
            "dimension",
            "地区",
            aliases=("区域",),
            datasource_id="ds_sales",
            datasource_name="sales",
            graph_data_object_id="obj_orders",
            graph_field_id="f_region",
            namespace="public",
            data_object_id="object_orders",
            data_object_name="orders",
            qualified_name="public.orders",
            field_path="region",
        ),
        semantic_asset(
            "metric_other_sales",
            "metric",
            "外部销售额",
            aliases=("销售额",),
            datasource_id="ds_other",
            datasource_name="other",
            graph_data_object_id="obj_other_orders",
            graph_field_id="f_other_amount",
            data_object_id="object_other_orders",
            data_object_name="other_orders",
            field_path="pay_amount",
        ),
        semantic_asset(
            "alias_order_revenue",
            "alias",
            "订单收入",
            source="enterprise_definition",
            status="approved",
            target_asset_id="metric_paid_sales",
        ),
        semantic_asset(
            "business_term_customer",
            "business_term",
            "客户",
            datasource_id="ds_sales",
            datasource_name="sales",
            graph_data_object_id="obj_customers",
            namespace="public",
            data_object_id="object_customers",
            data_object_name="customers",
            qualified_name="public.customers",
        ),
        semantic_asset(
            "metric_stale_identity",
            "metric",
            "历史销售额",
            aliases=("陈旧指标",),
            datasource_id="ds_sales",
            datasource_name="sales",
            graph_data_object_id="obj_orders",
            graph_field_id="f_missing",
            data_object_id="object_orders",
            data_object_name="orders",
            field_path="missing_amount",
        ),
        semantic_asset(
            "metric_missing_object",
            "metric",
            "不存在对象指标",
            aliases=("幽灵指标",),
            datasource_id="ds_sales",
            datasource_name="sales",
            graph_data_object_id="obj_ghost",
            data_object_id="object_ghost",
            data_object_name="ghost_table",
            field_path="amount",
        ),
        semantic_asset(
            "metric_legacy_sales",
            "metric",
            "遗留毛利额",
            aliases=("遗留口径",),
            datasource_id="ds_sales",
            datasource_name="sales",
            data_object_id="object_orders",
            data_object_name="orders",
            field_path="gross_amount",
        ),
        semantic_asset(
            "term_unbound",
            "business_term",
            "无绑定术语",
            aliases=("无绑定",),
        ),
        semantic_asset(
            "dimension_region_suggestion",
            "dimension",
            "地区建议",
            aliases=("地区猜测",),
            datasource_id="ds_sales",
            datasource_name="sales",
            graph_data_object_id="obj_orders",
            graph_field_id="f_region",
            namespace="public",
            data_object_id="object_orders",
            data_object_name="orders",
            qualified_name="public.orders",
            field_path="region",
            source="model_suggestion",
            status="suggested",
            confidence=0.7,
        ),
    ]


def governed_registry(tmp_path) -> SQLiteSemanticAssetRegistry:
    registry = SQLiteSemanticAssetRegistry(tmp_path / "catalog.db")
    for asset in governed_assets():
        registry.save(asset)
    return registry


def retriever(tmp_path, *, include_untrusted: bool = False) -> GraphSemanticRetriever:
    return GraphSemanticRetriever(
        InMemoryGraphReader(graph_structure()),
        governed_registry(tmp_path),
        "default",
        include_untrusted=include_untrusted,
    )


def query(
    question: str = "按地区查看销售额",
    *,
    metrics: list[str] | None = None,
    dimensions: list[str] | None = None,
) -> BusinessQuery:
    return BusinessQuery(
        question=question,
        objective="lookup",
        metrics=["销售额"] if metrics is None else metrics,
        dimensions=["地区"] if dimensions is None else dimensions,
    )


def test_scenario_1_metric_and_dimension_are_recalled_with_physical_bindings(tmp_path) -> None:
    result = retriever(tmp_path).retrieve(query(), requested_datasource_id="ds_sales")

    metrics = result.candidates_for(SemanticAssetType.METRIC)
    dimensions = result.candidates_for(SemanticAssetType.DIMENSION)
    assert {item.name for item in metrics} == {"实收销售额", "成交总额"}
    # Physical bindings come from the graph node ids, not from the registry's profile-era ids.
    assert {item.data_object_id for item in metrics} == {"obj_orders"}
    assert {item.field_path for item in metrics} == {"pay_amount", "gross_amount"}
    assert all(item.datasource_id == "ds_sales" for item in metrics)
    assert [item.name for item in dimensions] == ["地区"]
    assert dimensions[0].field_path == "region"
    assert dimensions[0].data_object_id == "obj_orders"
    assert any("企业数据图确认物理绑定：orders.pay_amount" in why for item in metrics for why in item.evidence)


def test_scenario_1_physical_assets_are_also_retrievable(tmp_path) -> None:
    result = retriever(tmp_path).retrieve(
        query("orders region", metrics=[], dimensions=[]),
        requested_datasource_id="ds_sales",
    )

    assert any(
        item.asset_type == SemanticAssetType.DATA_OBJECT and item.name == "orders"
        for item in result.candidates
    )
    assert any(
        item.asset_type == SemanticAssetType.FIELD and item.field_path == "region"
        for item in result.candidates
    )


def test_scenario_2_registry_binding_absent_from_the_graph_is_rejected(tmp_path) -> None:
    result = retriever(tmp_path).retrieve(
        query("历史销售额", metrics=["历史销售额"], dimensions=[]),
        requested_datasource_id="ds_sales",
        limit=100,
    )

    assert all(item.asset_id != "metric_stale_identity" for item in result.candidates)
    assert all(item.field_path != "missing_amount" for item in result.candidates)
    assert any("metric_stale_identity" in warning for warning in result.warnings)
    assert any("不存在于企业数据图" in warning for warning in result.warnings)


def test_scenario_2_registry_object_absent_from_the_graph_is_rejected(tmp_path) -> None:
    result = retriever(tmp_path).retrieve(
        query("幽灵指标", metrics=["幽灵指标"], dimensions=[]),
        requested_datasource_id="ds_sales",
        limit=100,
    )

    assert all(item.name != "不存在对象指标" for item in result.candidates)
    assert any("metric_missing_object" in warning for warning in result.warnings)


def test_scenario_3_shared_alias_preserves_ambiguity_without_choosing(tmp_path) -> None:
    result = retriever(tmp_path).retrieve(query("销售额"), requested_datasource_id="ds_sales")

    metrics = result.candidates_for(SemanticAssetType.METRIC)
    assert {item.name for item in metrics} == {"实收销售额", "成交总额"}
    assert len(metrics) == 2
    assert all(item.ambiguous for item in metrics)
    assert all("多个 metric 候选" in (item.ambiguity or "") for item in metrics)


def test_scenario_4_requested_datasource_bounds_every_candidate(tmp_path) -> None:
    result = retriever(tmp_path).retrieve(query(), requested_datasource_id="ds_other", limit=100)

    assert result.candidates
    assert {item.datasource_id for item in result.candidates} == {"ds_other"}
    assert {item.name for item in result.candidates_for(SemanticAssetType.METRIC)} == {"外部销售额"}


def test_scenario_5_unknown_requested_datasource_does_not_fall_back(tmp_path) -> None:
    result = retriever(tmp_path).retrieve(query(), requested_datasource_id="ds_missing")

    assert result.candidates == []
    assert result.requested_datasource_id == "ds_missing"
    assert len(result.warnings) == 1
    assert "ds_missing" in result.warnings[0]
    assert "不存在于工作区企业数据图" in result.warnings[0]


def test_scenario_6_relationships_come_only_from_published_edges(tmp_path) -> None:
    reader = InMemoryGraphReader(graph_structure())
    retriever_instance = GraphSemanticRetriever(reader, governed_registry(tmp_path))

    matched = retriever_instance.retrieve(
        query("foreign_key customer_id", metrics=[], dimensions=[]), limit=100
    )
    unmatched = retriever_instance.retrieve(
        query("pay_amount", metrics=[], dimensions=[]), limit=100
    )

    relationships = matched.candidates_for(SemanticAssetType.RELATIONSHIP)
    assert [item.relationship_id for item in relationships] == ["edge_orders_customers"]
    # A confirmed relationship is also offered structurally for joining, but it is never inferred:
    # the only relationship candidate is the real published edge.
    assert {
        item.relationship_id for item in unmatched.candidates_for(SemanticAssetType.RELATIONSHIP)
    } <= {"edge_orders_customers"}
    assert all(
        item.asset_type is not SemanticAssetType.RELATIONSHIP
        or item.relationship_id == "edge_orders_customers"
        for item in unmatched.candidates
    )


def test_scenario_7_retrieval_evidence_never_carries_business_values(tmp_path) -> None:
    result = retriever(tmp_path).retrieve(query(), requested_datasource_id="ds_sales", limit=100)
    serialized = result.model_dump_json()

    for marker in ("sample_values", "previews", "sensitive-value", "observed-value"):
        assert marker not in serialized
    assert all(
        any(why.startswith(prefix) for prefix in ("企业数据图", "语义资产", "图物理身份", "来源"))
        for item in result.candidates
        for why in item.evidence
    )


def test_scenario_8_retrieval_is_deterministic(tmp_path) -> None:
    instance = retriever(tmp_path)
    first: SemanticRetrievalResult = instance.retrieve(query(), requested_datasource_id="ds_sales", limit=100)
    second = instance.retrieve(query(), requested_datasource_id="ds_sales", limit=100)

    assert first == second
    assert first.model_dump_json() == second.model_dump_json()
    ordered = sorted(
        first.candidates, key=lambda item: (-item.score, item.asset_type.value, item.name, item.asset_id)
    )
    assert first.candidates == ordered


def test_alias_asset_names_reach_their_target_candidate(tmp_path) -> None:
    result = retriever(tmp_path).retrieve(
        query("订单收入", metrics=["订单收入"], dimensions=[]), requested_datasource_id="ds_sales"
    )

    metric = next(
        item for item in result.candidates if item.asset_type == SemanticAssetType.METRIC
    )
    assert metric.name == "实收销售额"
    assert metric.field_path == "pay_amount"


def test_business_term_is_retrieved_with_its_confirmed_object_binding(tmp_path) -> None:
    result = retriever(tmp_path).retrieve(
        query("客户", metrics=[], dimensions=[]), requested_datasource_id="ds_sales", limit=100
    )

    term = next(
        item for item in result.candidates if item.asset_type == SemanticAssetType.BUSINESS_TERM
    )
    assert term.name == "客户"
    assert term.data_object_id == "obj_customers"
    assert term.field_path is None


def test_unbound_business_vocabulary_never_becomes_a_physical_candidate(tmp_path) -> None:
    result = retriever(tmp_path).retrieve(
        query("无绑定术语", metrics=[], dimensions=[]), requested_datasource_id="ds_sales", limit=100
    )

    assert all(item.asset_id != "term_unbound" for item in result.candidates)
    assert all(item.name != "无绑定术语" for item in result.candidates)


def test_model_suggestion_stays_out_of_the_trusted_path(tmp_path) -> None:
    trusted = retriever(tmp_path).retrieve(
        query("地区建议", metrics=[], dimensions=["地区建议"]), requested_datasource_id="ds_sales"
    )
    exploratory = retriever(tmp_path, include_untrusted=True).retrieve(
        query("地区建议", metrics=[], dimensions=["地区建议"]), requested_datasource_id="ds_sales"
    )

    assert all(item.asset_id != "dimension_region_suggestion" for item in trusted.candidates)
    assert trusted.retrieval_path is SemanticRetrievalPath.TRUSTED
    suggestion = next(
        item
        for item in exploratory.candidates
        if item.asset_id == "dimension_region_suggestion"
    )
    assert suggestion.trusted is False
    assert suggestion.field_path == "region"
    assert exploratory.retrieval_path is SemanticRetrievalPath.EXPLORATORY


def test_no_match_reports_empty_candidates_and_a_warning(tmp_path) -> None:
    result = retriever(tmp_path).retrieve(
        query("**semantic_retrieval_no_match_987654**", metrics=[], dimensions=[])
    )

    assert result.candidates == []
    assert result.warnings == ["未在当前企业数据图与语义资产注册表中检索到匹配资产"]


def test_retrieval_limit_is_bounded_and_applied(tmp_path) -> None:
    instance = retriever(tmp_path)

    assert len(instance.retrieve(query(), limit=2).candidates) == 2
    with pytest.raises(ValueError, match="between 1 and 100"):
        instance.retrieve(query(), limit=0)


def test_requested_datasource_is_forwarded_to_the_graph_reader(tmp_path) -> None:
    reader = InMemoryGraphReader(graph_structure())
    GraphSemanticRetriever(reader, governed_registry(tmp_path)).retrieve(
        query(), requested_datasource_id="ds_sales"
    )

    assert len(reader.requests) == 1
    assert reader.requests[0].workspace_id == "default"
    assert reader.requests[0].datasource_id == "ds_sales"
    assert {item.datasource_id for item in reader.requests[0].references} == {"ds_sales"}
    # Registry claims are sent to the graph, including the ones the graph is expected to reject.
    assert {item.data_object_name for item in reader.requests[0].references} == {
        "orders",
        "customers",
        "ghost_table",
    }


def test_candidates_expose_the_graph_physical_identity(tmp_path) -> None:
    result = retriever(tmp_path).retrieve(query(), requested_datasource_id="ds_sales", limit=100)

    metric = next(
        item for item in result.candidates if item.asset_id == "metric_paid_sales"
    )
    assert metric.datasource_id == "ds_sales"
    assert metric.data_object_id == "obj_orders"
    assert metric.field_id == "f_pay"
    assert metric.field_path == "pay_amount"
    assert metric.migration_lookup is False
    assert "obj_orders/f_pay" in " ".join(metric.evidence)


def test_graph_identity_wins_over_a_conflicting_readable_name(tmp_path) -> None:
    """A stale or wrong display name must never redirect an asset to another physical object."""
    assets = [
        semantic_asset(
            "metric_identity_first",
            "metric",
            "身份优先销售额",
            aliases=("身份优先",),
            datasource_id="ds_sales",
            graph_data_object_id="obj_orders",
            graph_field_id="f_pay",
            data_object_name="ghost_table",
            qualified_name="ghost.ghost_table",
            field_path="ghost_amount",
        )
    ]
    retriever_instance = GraphSemanticRetriever(
        InMemoryGraphReader(graph_structure()), registry_with(assets, tmp_path)
    )

    result = retriever_instance.retrieve(
        query("身份优先", metrics=["身份优先"], dimensions=[]),
        requested_datasource_id="ds_sales",
        limit=100,
    )

    candidate = next(
        item for item in result.candidates if item.asset_id == "metric_identity_first"
    )
    assert candidate.data_object_id == "obj_orders"
    assert candidate.field_id == "f_pay"
    assert candidate.field_path == "pay_amount"


def test_namespace_collision_binds_two_assets_to_different_graph_objects(tmp_path) -> None:
    """``public.orders.amount`` and ``archive.orders.amount`` must never collapse into one object."""
    assets = [
        semantic_asset(
            "metric_public_amount",
            "metric",
            "对公销售额",
            aliases=("对公口径",),
            datasource_id="ds_sales",
            graph_data_object_id="obj_public_orders",
            graph_field_id="field_public_amount",
            namespace="public",
            qualified_name="public.orders",
            data_object_name="orders",
            field_path="amount",
        ),
        semantic_asset(
            "metric_archive_amount",
            "metric",
            "归档销售额",
            aliases=("归档口径",),
            datasource_id="ds_sales",
            graph_data_object_id="obj_archive_orders",
            graph_field_id="field_archive_amount",
            namespace="archive",
            qualified_name="archive.orders",
            data_object_name="orders",
            field_path="amount",
        ),
    ]
    retriever_instance = GraphSemanticRetriever(
        InMemoryGraphReader(colliding_structure()), registry_with(assets, tmp_path)
    )

    public = retriever_instance.retrieve(
        query("对公销售额", metrics=["对公销售额"], dimensions=[]),
        requested_datasource_id="ds_sales",
        limit=100,
    )
    archive = retriever_instance.retrieve(
        query("归档销售额", metrics=["归档销售额"], dimensions=[]),
        requested_datasource_id="ds_sales",
        limit=100,
    )

    public_metric = next(
        item for item in public.candidates if item.asset_id == "metric_public_amount"
    )
    archive_metric = next(
        item for item in archive.candidates if item.asset_id == "metric_archive_amount"
    )
    assert public_metric.data_object_id == "obj_public_orders"
    assert public_metric.field_id == "field_public_amount"
    assert archive_metric.data_object_id == "obj_archive_orders"
    assert archive_metric.field_id == "field_archive_amount"
    assert public_metric.data_object_id != archive_metric.data_object_id
    assert public_metric.field_id != archive_metric.field_id


def test_ambiguous_name_binding_is_rejected_instead_of_guessing_a_namespace(tmp_path) -> None:
    assets = [
        semantic_asset(
            "metric_ambiguous_name",
            "metric",
            "歧义销售额",
            aliases=("歧义口径",),
            datasource_id="ds_sales",
            data_object_name="orders",
            field_path="amount",
        )
    ]
    retriever_instance = GraphSemanticRetriever(
        InMemoryGraphReader(colliding_structure()), registry_with(assets, tmp_path)
    )

    result = retriever_instance.retrieve(
        query("歧义销售额", metrics=["歧义销售额"], dimensions=[]),
        requested_datasource_id="ds_sales",
        limit=100,
    )

    assert all(item.asset_id != "metric_ambiguous_name" for item in result.candidates)
    assert any("不唯一" in warning for warning in result.warnings)


def test_legacy_name_only_binding_is_flagged_as_migration_lookup(tmp_path) -> None:
    result = retriever(tmp_path).retrieve(
        query("遗留毛利额", metrics=["遗留毛利额"], dimensions=[]),
        requested_datasource_id="ds_sales",
        limit=100,
    )

    legacy = next(
        item for item in result.candidates if item.asset_id == "metric_legacy_sales"
    )
    assert legacy.migration_lookup is True
    assert legacy.field_path == "gross_amount"
    assert any("按名称迁移解析" in why for why in legacy.evidence)
    assert all(
        item.migration_lookup is False
        for item in result.candidates
        if item.asset_id != "metric_legacy_sales"
    )


def test_available_graph_without_matches_is_not_graph_unavailability(tmp_path) -> None:
    """Case A: the graph was read and simply holds nothing matching."""
    empty = GraphStructure(
        datasources=[
            GraphDatasource(
                node_id="db_sales",
                datasource_id="ds_sales",
                workspace_id="default",
                name="sales",
                kind="relational",
            )
        ]
    )
    empty_registry = SQLiteSemanticAssetRegistry(tmp_path / "empty_catalog.db")
    retriever_instance = GraphSemanticRetriever(InMemoryGraphReader(empty), empty_registry)

    result = retriever_instance.retrieve(query(), requested_datasource_id="ds_sales")

    assert result.candidates == []
    assert result.warnings == ["未在当前企业数据图与语义资产注册表中检索到匹配资产"]


def test_unconfigured_graph_backend_fails_closed(tmp_path) -> None:
    """Case B: no backend configured must never look like "no candidates found"."""
    retriever_instance = GraphSemanticRetriever(NullGraphReader(), governed_registry(tmp_path))

    with pytest.raises(GraphUnavailableError) as error:
        retriever_instance.retrieve(query(), requested_datasource_id="ds_sales")

    assert error.value.code == "graph_unavailable"
    assert "图后端未配置" in error.value.message


def colliding_structure() -> GraphStructure:
    return GraphStructure(
        datasources=[
            GraphDatasource(
                node_id="db_sales",
                datasource_id="ds_sales",
                workspace_id="default",
                name="sales",
                kind="relational",
            )
        ],
        data_objects=[
            GraphDataObject(
                node_id="obj_public_orders",
                datasource_id="ds_sales",
                namespace="public",
                name="orders",
                qualified_name="public.orders",
                object_kind="table",
            ),
            GraphDataObject(
                node_id="obj_archive_orders",
                datasource_id="ds_sales",
                namespace="archive",
                name="orders",
                qualified_name="archive.orders",
                object_kind="table",
            ),
        ],
        fields=[
            GraphField(
                node_id="field_public_amount",
                object_id="obj_public_orders",
                datasource_id="ds_sales",
                object_name="orders",
                name="amount",
                path="amount",
                data_type="decimal",
            ),
            GraphField(
                node_id="field_archive_amount",
                object_id="obj_archive_orders",
                datasource_id="ds_sales",
                object_name="orders",
                name="amount",
                path="amount",
                data_type="decimal",
            ),
        ],
    )


def registry_with(assets: list[SemanticAsset], tmp_path) -> SQLiteSemanticAssetRegistry:
    registry = SQLiteSemanticAssetRegistry(tmp_path / "collision_catalog.db")
    for asset in assets:
        registry.save(asset)
    return registry
