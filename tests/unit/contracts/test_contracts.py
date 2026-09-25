from datetime import UTC, datetime

import pytest
from pydantic import TypeAdapter, ValidationError

from smartdata.contracts import (
    DataObjectKind,
    DataObjectProfile,
    DataSourceProfile,
    DocumentResult,
    FieldProfile,
    GraphEdge,
    GraphNode,
    GraphResult,
    GroundedFieldRef,
    QueryContext,
    QueryIntent,
    QueryResult,
    RetrievedDataObject,
    RetrievedDataSource,
    SamplePreview,
    ScanPolicy,
    ScanSnapshot,
    ScanStatus,
)


def test_scan_policy_freezes_profile_and_preview_limits() -> None:
    assert ScanPolicy().profile_size == 20
    assert ScanPolicy().preview_size == 3
    with pytest.raises(ValidationError):
        ScanPolicy(preview_size=4)


def test_data_object_rejects_more_than_three_previews() -> None:
    with pytest.raises(ValidationError):
        DataObjectProfile(
            id="object_orders",
            datasource_id="ds_sales",
            name="orders",
            object_kind=DataObjectKind.COLLECTION,
            previews=[SamplePreview(values={"id": index}) for index in range(4)],
        )


def test_field_observation_count_cannot_exceed_profile_sample() -> None:
    with pytest.raises(ValidationError):
        FieldProfile(
            name="customer_id",
            path="customer_id",
            data_type="integer",
            observed_count=21,
            sample_count=20,
        )


def test_only_ready_completed_snapshot_can_be_active() -> None:
    profile = DataSourceProfile(
        datasource_id="ds_sales",
        name="sales",
        kind="document",
        driver="mongodb",
    )
    with pytest.raises(ValidationError):
        ScanSnapshot(
            id="scan_1",
            datasource_id="ds_sales",
            version=1,
            status=ScanStatus.SCANNING,
            profile=profile,
            started_at=datetime.now(UTC),
            active=True,
        )


def test_query_result_uses_discriminated_native_shapes() -> None:
    adapter = TypeAdapter(QueryResult)
    document = adapter.validate_python(
        {
            "result_type": "document",
            "datasource_id": "ds_mongo",
            "data_object_ids": ["object_orders"],
            "row_count": 1,
            "documents": [{"customer": {"id": 7}}],
        }
    )
    graph = adapter.validate_python(
        GraphResult(
            datasource_id="ds_graph",
            data_object_ids=["label_customer", "label_product", "edge_bought"],
            row_count=2,
            nodes=[GraphNode(id="1"), GraphNode(id="2")],
            edges=[GraphEdge(id="e1", edge_type="BOUGHT", from_node_id="1", to_node_id="2")],
        ).model_dump()
    )

    assert isinstance(document, DocumentResult)
    assert document.documents[0]["customer"]["id"] == 7
    assert isinstance(graph, GraphResult)
    assert graph.edges[0].edge_type == "BOUGHT"


def test_query_context_enforces_retrieval_field_budget() -> None:
    data_object = DataObjectProfile(
        id="object_orders",
        datasource_id="ds_sales",
        name="orders",
        object_kind=DataObjectKind.TABLE,
        fields=[
            FieldProfile(name=f"field_{index}", path=f"field_{index}", data_type="text")
            for index in range(31)
        ],
    )

    with pytest.raises(ValidationError, match="30 fields"):
        QueryContext(
            workspace_id="default",
            intent=QueryIntent(question="查询订单"),
            data_sources=[
                RetrievedDataSource(
                    datasource_id="ds_sales",
                    name="sales",
                    kind="relational",
                    driver="postgresql",
                    data_objects=[RetrievedDataObject(profile=data_object, score=1.0)],
                )
            ],
        )


@pytest.mark.parametrize(
    "data_type",
    ["date", "datetime", "timestamp", "timestamptz", "DATE", "DateTime", "TimeStamp", "TIMESTAMPTZ"],
)
def test_grounded_field_ref_accepts_phase1_time_range_types(data_type: str) -> None:
    """Phase 1 ``TimeRange`` carries date / datetime boundaries; only those columns are time axes."""
    ref = GroundedFieldRef(
        datasource_id="ds_sales",
        data_object_id="obj_orders",
        field_path="order_date",
        data_type=data_type,
    )
    assert ref.is_date_type() is True


@pytest.mark.parametrize("data_type", ["time", "TIME", "Time", " text ", "integer", "numeric", "json"])
def test_grounded_field_ref_rejects_non_phase1_time_axis_types(data_type: str) -> None:
    """Bare ``time`` (within-day time of day) is not a Phase 1 ``TimeRange`` axis."""
    ref = GroundedFieldRef(
        datasource_id="ds_sales",
        data_object_id="obj_orders",
        field_path="order_time",
        data_type=data_type,
    )
    assert ref.is_date_type() is False


def test_grounded_field_ref_without_data_type_is_not_a_time_axis() -> None:
    ref = GroundedFieldRef(
        datasource_id="ds_sales",
        data_object_id="obj_orders",
        field_path="order_date",
    )
    assert ref.is_date_type() is False
