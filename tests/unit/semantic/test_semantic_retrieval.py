from datetime import UTC, datetime

import pytest

from qaneris.contracts import (
    BusinessQuery,
    CompanyDataProfile,
    DataObjectProfile,
    DataSourceProfile,
    FieldProfile,
    MetadataOrigin,
    NamespaceProfile,
    RelationshipProfile,
    SamplePreview,
    SemanticAssetType,
    SemanticMetadata,
)
from qaneris.semantic import MetadataLexicalSemanticRetriever, SemanticRetriever


def semantic(
    name: str,
    aliases: list[str],
    roles: list[str],
    description: str = "",
    confidence: float = 1.0,
) -> SemanticMetadata:
    return SemanticMetadata(
        business_name=name,
        description=description,
        roles=roles,
        aliases=aliases,
        origin=MetadataOrigin.MANUAL,
        confidence=confidence,
    )


def company_profile() -> CompanyDataProfile:
    orders = DataObjectProfile(
        id="object_orders",
        datasource_id="ds_sales",
        name="orders",
        object_kind="table",
        semantic_metadata=[semantic("订单", ["销售订单"], ["entity"])],
        fields=[
            FieldProfile(
                name="pay_amount",
                path="pay_amount",
                data_type="decimal",
                sample_values=["sensitive-value-must-not-leak"],
                observed_count=1,
                sample_count=1,
                semantic_metadata=[
                    semantic("实收销售额", ["销售额", "实收金额"], ["metric", "指标"])
                ],
            ),
            FieldProfile(
                name="gross_amount",
                path="gross_amount",
                data_type="decimal",
                semantic_metadata=[
                    semantic("成交总额", ["销售额", "GMV"], ["metric", "指标"])
                ],
            ),
            FieldProfile(
                name="region",
                path="region",
                data_type="text",
                semantic_metadata=[semantic("地区", ["区域"], ["dimension", "维度"])],
            ),
        ],
        previews=[SamplePreview(values={"pay_amount": "sensitive-value-must-not-leak"})],
    )
    customers = DataObjectProfile(
        id="object_customers",
        datasource_id="ds_sales",
        name="customers",
        object_kind="table",
        semantic_metadata=[semantic("客户", ["消费者"], ["entity"])],
    )
    other_orders = DataObjectProfile(
        id="other_orders",
        datasource_id="ds_other",
        name="other_orders",
        object_kind="table",
        fields=[
            FieldProfile(
                name="amount",
                path="amount",
                data_type="decimal",
                semantic_metadata=[semantic("外部销售额", ["销售额"], ["metric"])],
            )
        ],
    )
    return CompanyDataProfile(
        workspace_id="default",
        data_sources=[
            DataSourceProfile(
                datasource_id="ds_sales",
                name="sales",
                kind="relational",
                driver="postgresql",
                semantic_metadata=[semantic("销售数据库", ["销售库"], ["datasource"])],
                namespaces=[
                    NamespaceProfile(name="public", data_objects=[orders, customers])
                ],
            ),
            DataSourceProfile(
                datasource_id="ds_other",
                name="other",
                kind="relational",
                driver="mysql",
                namespaces=[NamespaceProfile(name="default", data_objects=[other_orders])],
            ),
        ],
        relationships=[
            RelationshipProfile(
                id="orders_customer",
                from_datasource_id="ds_sales",
                from_object_id="object_orders",
                from_field_path="customer_id",
                to_datasource_id="ds_sales",
                to_object_id="object_customers",
                to_field_path="id",
                relationship_type="foreign_key",
                origin=MetadataOrigin.DATABASE,
                confirmed=True,
            )
        ],
        generated_at=datetime.now(UTC),
    )


def test_retriever_returns_metric_dimension_and_physical_references() -> None:
    retriever: SemanticRetriever = MetadataLexicalSemanticRetriever()
    query = BusinessQuery(
        question="按地区查看销售额",
        objective="lookup",
        metrics=["销售额"],
        dimensions=["地区"],
    )

    result = retriever.retrieve(company_profile(), query, requested_datasource_id="ds_sales")

    metrics = result.candidates_for(SemanticAssetType.METRIC)
    dimensions = result.candidates_for(SemanticAssetType.DIMENSION)
    assert {item.name for item in metrics} == {"实收销售额", "成交总额"}
    assert {item.field_path for item in metrics} == {"pay_amount", "gross_amount"}
    assert dimensions[0].name == "地区"
    assert dimensions[0].data_object_id == "object_orders"
    assert all(item.datasource_id == "ds_sales" for item in result.candidates)


def test_shared_alias_marks_metric_candidates_as_ambiguous() -> None:
    query = BusinessQuery(
        question="销售额",
        objective="lookup",
        metrics=["销售额"],
    )

    result = MetadataLexicalSemanticRetriever().retrieve(
        company_profile(), query, requested_datasource_id="ds_sales"
    )
    metrics = result.candidates_for("metric")

    assert len(metrics) == 2
    assert all(item.ambiguous for item in metrics)
    assert all("多个 metric 候选" in (item.ambiguity or "") for item in metrics)


def test_requested_datasource_bounds_candidates() -> None:
    query = BusinessQuery(question="销售额", objective="lookup", metrics=["销售额"])

    result = MetadataLexicalSemanticRetriever().retrieve(
        company_profile(), query, requested_datasource_id="ds_other"
    )

    assert result.candidates
    assert {item.datasource_id for item in result.candidates} == {"ds_other"}


def test_missing_requested_datasource_returns_warning_without_fallback() -> None:
    query = BusinessQuery(question="销售额", objective="lookup", metrics=["销售额"])

    result = MetadataLexicalSemanticRetriever().retrieve(
        company_profile(), query, requested_datasource_id="ds_missing"
    )

    assert result.candidates == []
    assert "ds_missing" in result.warnings[0]


def test_retrieval_evidence_never_contains_sample_values() -> None:
    query = BusinessQuery(question="实收销售额", objective="lookup", metrics=["实收销售额"])

    result = MetadataLexicalSemanticRetriever().retrieve(company_profile(), query)
    serialized = result.model_dump_json()

    assert "sensitive-value-must-not-leak" not in serialized


def test_physical_metadata_and_relationships_remain_retrievable() -> None:
    query = BusinessQuery(
        question="orders foreign key",
        objective="lookup",
        entities=["orders", "foreign key"],
    )

    result = MetadataLexicalSemanticRetriever().retrieve(company_profile(), query)

    assert any(item.asset_type == "data_object" and item.name == "orders" for item in result.candidates)
    relationship = next(item for item in result.candidates if item.asset_type == "relationship")
    assert relationship.relationship_id == "orders_customer"


def test_retrieval_is_bounded_and_deterministic() -> None:
    query = BusinessQuery(question="销售额", objective="lookup", metrics=["销售额"])
    retriever = MetadataLexicalSemanticRetriever()

    first = retriever.retrieve(company_profile(), query, limit=2)
    second = retriever.retrieve(company_profile(), query, limit=2)

    assert len(first.candidates) == 2
    assert first == second
    with pytest.raises(ValueError, match="between 1 and 100"):
        retriever.retrieve(company_profile(), query, limit=0)


def test_semantic_metadata_confidence_affects_candidate_score() -> None:
    query = BusinessQuery(question="实收销售额", objective="lookup", metrics=["实收销售额"])
    normal_profile = company_profile()
    low_profile = company_profile()
    low_profile.data_sources[0].namespaces[0].data_objects[0].fields[0].semantic_metadata[
        0
    ].confidence = 0.2
    retriever = MetadataLexicalSemanticRetriever()

    normal = next(
        item
        for item in retriever.retrieve(normal_profile, query).candidates
        if item.name == "实收销售额"
    )
    low = next(
        item
        for item in retriever.retrieve(low_profile, query).candidates
        if item.name == "实收销售额"
    )

    assert low.score < normal.score


def test_no_match_returns_empty_candidates_instead_of_arbitrary_asset() -> None:
    query = BusinessQuery(question="完全不存在的业务术语", objective="lookup")

    result = MetadataLexicalSemanticRetriever().retrieve(company_profile(), query)

    assert result.candidates == []
    assert result.warnings == ["未在当前语义画像中检索到匹配资产"]


def test_no_match_token_does_not_match_number_suffix_fields() -> None:
    profile = company_profile()
    fields = profile.data_sources[0].namespaces[0].data_objects[0].fields
    fields.extend(
        [
            FieldProfile(name="order_no", path="order_no", data_type="text"),
            FieldProfile(name="tracking_no", path="tracking_no", data_type="text"),
        ]
    )
    retriever = MetadataLexicalSemanticRetriever()

    no_match = retriever.retrieve(
        profile,
        BusinessQuery(
            question="**semantic_retrieval_no_match_987654**",
            objective="lookup",
        ),
    )
    exact_field = retriever.retrieve(
        profile,
        BusinessQuery(question="order_no", objective="lookup"),
    )

    assert no_match.candidates == []
    assert no_match.warnings == ["未在当前语义画像中检索到匹配资产"]
    assert any(
        item.asset_type == "field" and item.name == "order_no"
        for item in exact_field.candidates
    )
