"""Shared builders for the grounded query engine unit tests (P1-04C)."""

from __future__ import annotations

from qaneris.contracts.query import GroundedQueryContext
from qaneris.contracts.semantic import (
    AggregateFunction,
    BusinessQuery,
    GroundedQuery,
    GroundingBinding,
    SemanticAssetType,
    SemanticCandidate,
)
from qaneris.querying import QueryContextBuilder
from qaneris.semantic.grounding import GroundingResult

DS = "ds_sales"
OTHER_DS = "ds_crm"
ORDERS = "obj_orders"
CUSTOMERS = "obj_customers"
ORDERS_REGION = "field_region"
ORDERS_PAY = "field_pay_amount"
ORDERS_CUSTOMER_ID = "field_customer_id"
CUSTOMERS_ID = "field_customer_id_pk"
EDGE = "edge_orders_customers"


def candidate(
    asset_type: SemanticAssetType,
    asset_id: str,
    name: str,
    *,
    datasource_id: str | None = DS,
    data_object_id: str | None = ORDERS,
    field_id: str | None = None,
    field_path: str | None = None,
    relationship_id: str | None = None,
    from_data_object_id: str | None = None,
    to_data_object_id: str | None = None,
    from_field_path: str | None = None,
    to_field_path: str | None = None,
    score: float = 0.9,
    default_aggregation: AggregateFunction | None = None,
    data_type: str | None = None,
    namespace: str | None = None,
    qualified_name: str | None = None,
    object_kind: str | None = None,
    data_object_name: str | None = None,
) -> SemanticCandidate:
    return SemanticCandidate(
        asset_type=asset_type,
        asset_id=asset_id,
        name=name,
        score=score,
        datasource_id=datasource_id,
        data_object_id=data_object_id,
        field_id=field_id,
        field_path=field_path,
        relationship_id=relationship_id,
        from_data_object_id=from_data_object_id,
        to_data_object_id=to_data_object_id,
        from_field_path=from_field_path,
        to_field_path=to_field_path,
        evidence=[f"语义资产：{asset_id}"],
        default_aggregation=default_aggregation,
        data_type=data_type,
        namespace=namespace,
        qualified_name=qualified_name,
        object_kind=object_kind,
        data_object_name=data_object_name,
    )


def metric(**overrides) -> SemanticCandidate:
    values = {
        "asset_type": SemanticAssetType.METRIC,
        "asset_id": "metric_paid_sales",
        "name": "实收销售额",
        "field_id": ORDERS_PAY,
        "field_path": "pay_amount",
        "default_aggregation": AggregateFunction.SUM,
    }
    return candidate(**{**values, **overrides})


def graph_backed_locator(**overrides) -> dict[str, str | None]:
    """The four fields a graph-backed object locator carries for ``public.orders``."""
    values = {
        "data_object_name": "orders",
        "namespace": "public",
        "qualified_name": "public.orders",
        "object_kind": "table",
    }
    return {**values, **overrides}


def dimension(**overrides) -> SemanticCandidate:
    values = {
        "asset_type": SemanticAssetType.DIMENSION,
        "asset_id": "dimension_region",
        "name": "地区",
        "field_id": ORDERS_REGION,
        "field_path": "region",
    }
    return candidate(**{**values, **overrides})


def relationship(**overrides) -> SemanticCandidate:
    values = {
        "asset_type": SemanticAssetType.RELATIONSHIP,
        "asset_id": EDGE,
        "name": "foreign_key",
        "data_object_id": None,
        "relationship_id": EDGE,
        "from_data_object_id": ORDERS,
        "from_field_path": "customer_id",
        "to_data_object_id": CUSTOMERS,
        "to_field_path": "id",
    }
    return candidate(**{**values, **overrides})


def binding_for(term: str, asset: SemanticCandidate) -> GroundingBinding:
    return GroundingBinding(
        business_term=term,
        asset_type=asset.asset_type,
        asset_id=asset.asset_id,
        datasource_id=asset.datasource_id,
        data_object_id=asset.data_object_id,
        field_path=asset.field_path,
        relationship_id=asset.relationship_id,
        score=asset.score,
        evidence=list(asset.evidence),
        default_aggregation=asset.default_aggregation,
        data_type=asset.data_type,
        namespace=asset.namespace,
        qualified_name=asset.qualified_name,
        object_kind=asset.object_kind,
        data_object_name=asset.data_object_name,
    )


def grounded_query(
    query: BusinessQuery,
    pairs: list[tuple[str, SemanticCandidate]],
    *,
    candidates: list[SemanticCandidate] | None = None,
    workspace_id: str = "default",
    requested_datasource_id: str | None = None,
    unresolved: list[str] | None = None,
) -> GroundedQuery:
    bindings = [binding_for(term, asset) for term, asset in pairs]
    return GroundedQuery(
        business_query=query,
        candidates=[asset for _, asset in pairs] if candidates is None else candidates,
        bindings=bindings,
        workspace_id=workspace_id,
        requested_datasource_id=requested_datasource_id,
        allowed_datasource_ids=sorted(
            {item.datasource_id for item in bindings if item.datasource_id}
        ),
        allowed_data_object_ids=sorted(
            {item.data_object_id for item in bindings if item.data_object_id}
        ),
        allowed_field_paths=sorted({item.field_path for item in bindings if item.field_path}),
        allowed_relationship_ids=sorted(
            {item.relationship_id for item in bindings if item.relationship_id}
        ),
        unresolved_ambiguities=unresolved or [],
    )


def region_query(**overrides) -> BusinessQuery:
    values = {
        "question": "按地区看销售额",
        "objective": "lookup",
        "metrics": ["销售额"],
        "dimensions": ["地区"],
    }
    return BusinessQuery(**{**values, **overrides})


def sales_context(*, query: BusinessQuery | None = None, **kwargs) -> GroundedQueryContext:
    query = query or region_query()
    sales = metric()
    region = dimension()
    return QueryContextBuilder().build(
        _result(grounded_query(query, [("销售额", sales), ("地区", region)], **kwargs))
    )


def _result(grounded: GroundedQuery, clarifications: list | None = None) -> GroundingResult:
    return GroundingResult(grounded_query=grounded, clarifications=clarifications or [])
