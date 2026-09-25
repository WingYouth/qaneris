from __future__ import annotations

from smartdata.contracts.semantic import (
    BusinessFilter,
    BusinessQuery,
    RankingSpec,
    SemanticAssetType,
    SemanticCandidate,
)
from smartdata.semantic import (
    GroundingResult,
    SemanticGrounder,
    SemanticRetrievalPath,
    SemanticRetrievalResult,
)

DS = "ds_sales"


def candidate(
    asset_type: SemanticAssetType,
    asset_id: str,
    name: str,
    *,
    score: float = 0.8,
    datasource_id: str | None = DS,
    data_object_id: str | None = "obj_orders",
    field_id: str | None = None,
    field_path: str | None = None,
    relationship_id: str | None = None,
    from_data_object_id: str | None = None,
    to_data_object_id: str | None = None,
    matched_phrases: tuple[str, ...] = (),
    matched_terms: tuple[str, ...] = (),
    labels: tuple[str, ...] = (),
    trusted: bool = True,
    migration_lookup: bool = False,
    physical_binding: str | None = None,
    time_axis: bool = False,
) -> SemanticCandidate:
    evidence = [f"语义资产：{asset_id}"]
    if physical_binding:
        evidence.append(f"企业数据图确认物理绑定：{physical_binding}")
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
        evidence=evidence,
        reasons=[f"业务表达精确匹配名称：{name}"],
        matched_phrases=list(matched_phrases),
        matched_terms=list(matched_terms),
        labels=list(labels) or [name],
        trusted=trusted,
        migration_lookup=migration_lookup,
        time_axis=time_axis,
    )


def metric(
    asset_id: str = "metric_paid_sales",
    name: str = "实收销售额",
    field_path: str = "pay_amount",
    *,
    phrases: tuple[str, ...] = ("销售额",),
    object_id: str = "obj_orders",
    datasource_id: str = DS,
    score: float = 0.8,
    **kwargs,
) -> SemanticCandidate:
    return candidate(
        SemanticAssetType.METRIC,
        asset_id,
        name,
        score=score,
        datasource_id=datasource_id,
        data_object_id=object_id,
        field_path=field_path,
        matched_phrases=phrases,
        physical_binding=f"orders.{field_path}",
        **kwargs,
    )


def dimension(
    asset_id: str = "dimension_region",
    name: str = "地区",
    field_path: str = "region",
    *,
    phrases: tuple[str, ...] = ("地区",),
    object_id: str = "obj_orders",
    datasource_id: str = DS,
    score: float = 0.8,
    **kwargs,
) -> SemanticCandidate:
    return candidate(
        SemanticAssetType.DIMENSION,
        asset_id,
        name,
        score=score,
        datasource_id=datasource_id,
        data_object_id=object_id,
        field_path=field_path,
        matched_phrases=phrases,
        physical_binding=f"orders.{field_path}",
        **kwargs,
    )


def field_candidate(
    name: str = "region", *, object_id: str = "obj_orders", phrases: tuple[str, ...] = ("地区",)
) -> SemanticCandidate:
    return candidate(
        SemanticAssetType.FIELD,
        f"field_{name}",
        name,
        data_object_id=object_id,
        field_path=name,
        matched_phrases=phrases,
        physical_binding=f"orders.{name}",
    )


def relationship(
    relationship_id: str = "edge_orders_customers",
    *,
    name: str = "foreign_key",
    from_object_id: str = "obj_orders",
    to_object_id: str = "obj_customers",
    phrases: tuple[str, ...] = ("foreign_key",),
) -> SemanticCandidate:
    return candidate(
        SemanticAssetType.RELATIONSHIP,
        relationship_id,
        name,
        score=0.4,
        data_object_id=None,
        relationship_id=relationship_id,
        from_data_object_id=from_object_id,
        to_data_object_id=to_object_id,
        matched_phrases=phrases,
        physical_binding="orders→customers",
    )


def retrieval(
    candidates: list[SemanticCandidate],
    query: BusinessQuery,
    *,
    path: SemanticRetrievalPath = SemanticRetrievalPath.TRUSTED,
    requested_datasource_id: str | None = None,
) -> SemanticRetrievalResult:
    return SemanticRetrievalResult(
        business_query=query,
        candidates=candidates,
        retrieval_path=path,
        requested_datasource_id=requested_datasource_id,
    )


def sales_query() -> BusinessQuery:
    return BusinessQuery(
        question="按地区看销售额",
        objective="lookup",
        metrics=["销售额"],
        dimensions=["地区"],
    )


def ground(candidates, query=None, **kwargs) -> GroundingResult:
    query = query or sales_query()
    return SemanticGrounder().ground(query, retrieval(candidates, query, **kwargs))


def bindings_for(result: GroundingResult) -> dict[str, str | None]:
    physical_types = {
        SemanticAssetType.METRIC,
        SemanticAssetType.DIMENSION,
        SemanticAssetType.FIELD,
    }
    return {
        binding.business_term: binding.field_path
        for binding in result.grounded_query.bindings
        if binding.asset_type in physical_types
    }


def test_scenario_a_unique_metric_binds_with_its_physical_identity() -> None:
    query = BusinessQuery(question="销售额", objective="lookup", metrics=["销售额"])
    other_metric = metric(
        asset_id="metric_gross_sales",
        name="成交总额",
        field_path="gross_amount",
        phrases=("GMV",),
    )

    result = SemanticGrounder().ground(
        query, retrieval([metric(), other_metric], query)
    )

    binding = result.grounded_query.bindings[0]
    assert binding.business_term == "销售额"
    assert binding.asset_type is SemanticAssetType.METRIC
    assert binding.asset_id == "metric_paid_sales"
    assert binding.datasource_id == DS
    assert binding.data_object_id == "obj_orders"
    assert binding.field_path == "pay_amount"
    assert binding.evidence
    assert result.grounded_query.allowed_datasource_ids == [DS]
    assert result.grounded_query.allowed_data_object_ids == ["obj_orders"]
    # A retrieval candidate that was not bound must not reach the allowlist.
    assert result.grounded_query.allowed_field_paths == ["pay_amount"]
    assert result.grounded_query.unresolved_ambiguities == []
    assert result.is_executable is True
    assert result.needs_clarification is False


def test_scenario_b_metric_and_dimension_on_the_same_object() -> None:
    result = ground([metric(), dimension()])

    assert bindings_for(result) == {"销售额": "pay_amount", "地区": "region"}
    assert result.grounded_query.allowed_field_paths == ["pay_amount", "region"]
    assert result.grounded_query.allowed_relationship_ids == []
    assert result.is_executable is True


def test_scenario_c_ambiguous_metric_is_not_guessed() -> None:
    result = ground(
        [
            metric(score=0.9),
            metric(
                asset_id="metric_gross_sales",
                name="成交总额",
                field_path="gross_amount",
                phrases=("销售额",),
                score=0.85,
            ),
        ]
    )

    assert result.grounded_query.bindings == []
    assert result.grounded_query.unresolved_ambiguities
    assert "销售额" in result.grounded_query.unresolved_ambiguities[0]
    assert result.is_executable is False
    assert result.needs_clarification is True
    clarification = result.clarifications[0]
    assert clarification.field == "metric:销售额"
    assert [option.label for option in clarification.options] == ["实收销售额", "成交总额"]
    assert "销售额" in clarification.question
    rendered = clarification.model_dump_json()
    assert "obj_" not in rendered and "field_" not in rendered
    assert result.grounded_query.allowed_field_paths == []


def test_scenario_c_ambiguity_does_not_block_the_unambiguous_slot() -> None:
    result = ground(
        [
            metric(score=0.9),
            metric(
                asset_id="metric_gross_sales",
                name="成交总额",
                field_path="gross_amount",
                phrases=("销售额",),
                score=0.85,
            ),
            dimension(),
        ]
    )

    assert bindings_for(result) == {"地区": "region"}
    assert result.grounded_query.allowed_field_paths == ["region"]
    assert result.grounded_query.unresolved_ambiguities
    assert result.is_executable is False


def test_scenario_d_untrusted_candidate_is_never_bound() -> None:
    suggestion = metric(
        asset_id="metric_model_suggestion",
        name="模型销售额",
        field_path="pay_amount",
        phrases=("销售额",),
        trusted=False,
    )

    trusted_path = ground([suggestion])

    assert trusted_path.grounded_query.bindings == []
    assert trusted_path.grounded_query.allowed_field_paths == []
    assert trusted_path.grounded_query.unresolved_ambiguities
    assert "未受治理确认" in trusted_path.grounded_query.unresolved_ambiguities[0]
    assert trusted_path.is_executable is False


def test_exploratory_retrieval_never_produces_an_executable_grounding() -> None:
    suggestion = metric(
        asset_id="metric_model_suggestion",
        name="模型销售额",
        phrases=("销售额",),
        trusted=False,
    )

    result = ground([suggestion], path=SemanticRetrievalPath.EXPLORATORY)

    assert result.grounded_query.unresolved_ambiguities
    assert result.is_executable is False


def test_scenario_e_migration_lookup_candidate_is_not_silently_bound() -> None:
    legacy = metric(
        asset_id="metric_legacy_sales",
        name="遗留销售额",
        phrases=("销售额",),
        migration_lookup=True,
    )

    result = ground([legacy])

    assert result.grounded_query.bindings == []
    assert result.grounded_query.allowed_field_paths == []
    assert "迁移" in result.grounded_query.unresolved_ambiguities[0]
    assert result.is_executable is False


def test_scenario_f_grounding_never_invents_a_field_removed_by_rescan() -> None:
    query = BusinessQuery(question="销售额", objective="lookup", metrics=["销售额"])
    # After the rescan the graph no longer holds pay_amount, so retrieval returns nothing useful.
    survivors = [dimension()]

    result = SemanticGrounder().ground(query, retrieval(survivors, query))

    assert result.grounded_query.bindings == []
    assert result.grounded_query.allowed_field_paths == []
    assert "pay_amount" not in result.grounded_query.model_dump_json()
    assert result.grounded_query.unresolved_ambiguities


def test_scenario_g_relationship_between_two_objects_is_bound() -> None:
    result = ground(
        [
            metric(),
            dimension(name="地区", field_path="region", object_id="obj_customers"),
            relationship(),
        ]
    )

    relationship_binding = next(
        binding
        for binding in result.grounded_query.bindings
        if binding.asset_type is SemanticAssetType.RELATIONSHIP
    )
    assert relationship_binding.relationship_id == "edge_orders_customers"
    assert relationship_binding.asset_id == "edge_orders_customers"
    assert relationship_binding.business_term == "foreign_key"
    assert result.grounded_query.allowed_relationship_ids == ["edge_orders_customers"]
    assert result.grounded_query.unresolved_ambiguities == []
    assert result.is_executable is True


def test_scenario_h_two_objects_without_a_relationship_must_stop() -> None:
    result = ground(
        [metric(), dimension(field_path="region", object_id="obj_customers")]
    )

    assert all(
        binding.asset_type is not SemanticAssetType.RELATIONSHIP
        for binding in result.grounded_query.bindings
    )
    assert result.grounded_query.allowed_relationship_ids == []
    assert any(
        "没有已确认的 RELATES_TO" in problem
        for problem in result.grounded_query.unresolved_ambiguities
    )
    assert result.is_executable is False
    assert any(
        "没有它们之间已确认的关系" in item.question
        and "不会按同名字段" in item.question
        for item in result.clarifications
    )


def test_more_than_two_objects_is_reported_as_unsupported() -> None:
    query = BusinessQuery(
        question="按地区看销售额",
        objective="lookup",
        metrics=["销售额"],
        dimensions=["地区"],
        entities=["客户", "商品"],
    )
    result = SemanticGrounder().ground(
        query,
        retrieval(
            [
                metric(),
                dimension(),
                candidate(
                    SemanticAssetType.BUSINESS_TERM,
                    "term_customer",
                    "客户",
                    data_object_id="obj_customers",
                    matched_phrases=("客户",),
                    physical_binding="customers",
                ),
                candidate(
                    SemanticAssetType.BUSINESS_TERM,
                    "term_product",
                    "商品",
                    data_object_id="obj_products",
                    matched_phrases=("商品",),
                    physical_binding="products",
                ),
            ],
            query,
        ),
    )

    assert any(
        "个数据对象" in problem for problem in result.grounded_query.unresolved_ambiguities
    )
    assert result.grounded_query.allowed_relationship_ids == []
    assert result.is_executable is False


def test_scenario_i_cross_datasource_is_not_executable() -> None:
    result = ground(
        [
            metric(datasource_id="ds_sales"),
            dimension(datasource_id="ds_other", object_id="obj_other_orders"),
        ]
    )

    assert any(
        "跨数据源" in problem for problem in result.grounded_query.unresolved_ambiguities
    )
    assert result.is_executable is False
    assert sorted(result.grounded_query.allowed_datasource_ids) == ["ds_other", "ds_sales"]


def test_scenario_j_filter_binds_the_subject_and_keeps_the_value_as_business_data() -> None:
    query = BusinessQuery(
        question="地区=华东的销售额",
        objective="lookup",
        metrics=["销售额"],
        filters=[BusinessFilter(subject="地区", operator="=", value="华东")],
    )

    result = SemanticGrounder().ground(query, retrieval([metric(), dimension()], query))

    filters = [
        binding
        for binding in result.grounded_query.bindings
        if binding.asset_type is SemanticAssetType.DIMENSION
    ]
    assert [binding.business_term for binding in filters] == ["地区"]
    assert filters[0].field_path == "region"
    assert result.grounded_query.allowed_field_paths == ["pay_amount", "region"]
    # The filter value is business data, never structure: it stays out of bindings and allowlists.
    structural = [
        binding.model_dump() for binding in result.grounded_query.bindings
    ] + [result.grounded_query.allowed_field_paths]
    assert "华东" not in str(structural)
    assert result.is_executable is True


def test_filter_subject_can_bind_a_physical_field_when_no_governed_dimension_exists() -> None:
    query = BusinessQuery(
        question="地区=华东的销售额",
        objective="lookup",
        metrics=["销售额"],
        filters=[BusinessFilter(subject="地区", operator="=", value="华东")],
    )

    result = SemanticGrounder().ground(query, retrieval([metric(), field_candidate()], query))

    assert bindings_for(result) == {"销售额": "pay_amount", "地区": "region"}


def test_scenario_k_ranking_metric_reuses_the_metric_binding() -> None:
    query = BusinessQuery(
        question="销售额最高的前 10 个地区",
        objective="ranking",
        metrics=["销售额"],
        dimensions=["地区"],
        ranking=RankingSpec(direction="top", limit=10, metric="销售额"),
    )
    candidates = [
        metric(),
        metric(
            asset_id="metric_gross_sales",
            name="成交总额",
            field_path="gross_amount",
            phrases=("GMV",),
        ),
        dimension(),
    ]

    result = SemanticGrounder().ground(query, retrieval(candidates, query))

    metric_bindings = [
        binding
        for binding in result.grounded_query.bindings
        if binding.asset_type is SemanticAssetType.METRIC
    ]
    assert len(metric_bindings) == 1
    assert metric_bindings[0].business_term == "销售额"
    assert metric_bindings[0].field_path == "pay_amount"
    assert result.grounded_query.allowed_field_paths == ["pay_amount", "region"]
    assert result.is_executable is True


def test_ranking_metric_ambiguity_is_reported_once() -> None:
    query = BusinessQuery(
        question="销售额最高的前 10 个地区",
        objective="ranking",
        metrics=["销售额"],
        ranking=RankingSpec(direction="top", limit=10, metric="销售额"),
    )
    candidates = [
        metric(score=0.9),
        metric(
            asset_id="metric_gross_sales",
            name="成交总额",
            field_path="gross_amount",
            phrases=("销售额",),
            score=0.85,
        ),
    ]

    result = SemanticGrounder().ground(query, retrieval(candidates, query))

    assert len(result.grounded_query.unresolved_ambiguities) == 1
    assert len(result.clarifications) == 1
    assert result.is_executable is False


def test_exact_label_outranks_a_suffix_alias() -> None:
    """An expression naming one metric exactly is not ambiguous with a metric that aliases a part."""
    query = BusinessQuery(question="实收销售额", objective="lookup", metrics=["实收销售额"])
    exact = metric(
        asset_id="metric_paid_sales",
        name="实收销售额",
        phrases=("实收销售额",),
        labels=("实收销售额", "销售额", "实收金额"),
    )
    suffix = metric(
        asset_id="metric_gross_sales",
        name="成交总额",
        field_path="gross_amount",
        phrases=("实收销售额",),
        labels=("成交总额", "销售额"),
    )

    result = SemanticGrounder().ground(query, retrieval([exact, suffix], query))

    assert bindings_for(result) == {"实收销售额": "pay_amount"}
    assert result.grounded_query.allowed_field_paths == ["pay_amount"]
    assert result.is_executable is True


def test_metric_slot_never_binds_a_dimension_candidate() -> None:
    query = BusinessQuery(question="按地区看销售额", objective="lookup", metrics=["地区"])

    result = SemanticGrounder().ground(query, retrieval([dimension()], query))

    assert result.grounded_query.bindings == []
    assert "未找到与指标" in result.grounded_query.unresolved_ambiguities[0]


def test_missing_metric_clarification_names_the_missing_business_definition() -> None:
    query = BusinessQuery(question="汇总发货金额", objective="lookup", metrics=["发货金额"])

    result = ground([], query)

    assert not result.is_executable
    assert "发货金额" in result.clarifications[0].question
    assert "指标" in result.clarifications[0].question
    assert "发布" in result.clarifications[0].question
    assert result.clarifications[0].options == []


def test_missing_dimension_clarification_names_the_missing_dimension() -> None:
    query = BusinessQuery(
        question="按发货地区看销售额", objective="lookup",
        metrics=["销售额"], dimensions=["发货地区"],
    )

    result = ground([metric()], query)

    assert not result.is_executable
    assert any(
        "维度“发货地区”" in item.question for item in result.clarifications
    )


def test_empty_business_query_clarifies_missing_published_structure() -> None:
    query = BusinessQuery(question="汇总发货数据", objective="lookup", confidence=0.2)

    result = ground([], query)

    assert not result.is_executable
    assert result.clarifications[0].question.startswith("当前已发布数据中")


def test_entity_slot_prefers_the_governed_term_over_the_raw_object() -> None:
    query = BusinessQuery(
        question="客户的销售额", objective="lookup", metrics=["销售额"], entities=["客户"]
    )
    term = candidate(
        SemanticAssetType.BUSINESS_TERM,
        "term_customer",
        "客户",
        data_object_id="obj_customers",
        matched_phrases=("客户",),
        physical_binding="customers",
    )
    data_object = candidate(
        SemanticAssetType.DATA_OBJECT,
        "obj_customers",
        "客户",
        data_object_id="obj_customers",
        matched_phrases=("客户",),
        physical_binding="customers",
    )

    result = SemanticGrounder().ground(
        query, retrieval([metric(), term, data_object], query)
    )

    entity_bindings = [
        binding
        for binding in result.grounded_query.bindings
        if binding.asset_type in {SemanticAssetType.BUSINESS_TERM, SemanticAssetType.DATA_OBJECT}
    ]
    assert len(entity_bindings) == 1
    assert entity_bindings[0].asset_type is SemanticAssetType.BUSINESS_TERM
    assert entity_bindings[0].data_object_id == "obj_customers"


def test_requested_datasource_scope_excludes_other_sources() -> None:
    result = ground(
        [metric(datasource_id="ds_sales"), dimension(datasource_id="ds_other")],
        requested_datasource_id="ds_sales",
    )

    assert bindings_for(result) == {"销售额": "pay_amount"}
    assert result.grounded_query.allowed_datasource_ids == ["ds_sales"]
    assert any(
        "不在指定数据源" in problem for problem in result.grounded_query.unresolved_ambiguities
    )


def test_scenario_l_grounding_is_deterministic_and_order_independent() -> None:
    candidates = [
        metric(score=0.9),
        metric(
            asset_id="metric_gross_sales",
            name="成交总额",
            field_path="gross_amount",
            phrases=("销售额",),
            score=0.85,
        ),
        dimension(),
        relationship(),
    ]
    query = sales_query()
    grounder = SemanticGrounder()

    first = grounder.ground(query, retrieval(candidates, query))
    second = grounder.ground(query, retrieval(candidates, query))
    reversed_order = grounder.ground(query, retrieval(list(reversed(candidates)), query))

    assert first.model_dump_json() == second.model_dump_json()
    assert reversed_order.grounded_query.bindings == first.grounded_query.bindings
    assert (
        reversed_order.grounded_query.allowed_field_paths
        == first.grounded_query.allowed_field_paths
    )
    assert (
        reversed_order.grounded_query.unresolved_ambiguities
        == first.grounded_query.unresolved_ambiguities
    )
    assert reversed_order.clarifications == first.clarifications
    assert (
        reversed_order.grounded_query.candidates != first.grounded_query.candidates
        or len({item.asset_id for item in candidates}) == len(candidates)
    )


# ── Governed business time axis ──────────────────────────────────────────────
def time_axis_dimension(
    asset_id: str = "dimension_order_date",
    name: str = "下单日期",
    field_path: str = "order_date",
    *,
    time_axis: bool = True,
    **kwargs,
) -> SemanticCandidate:
    return dimension(
        asset_id,
        name,
        field_path,
        phrases=(),
        time_axis=time_axis,
        **kwargs,
    )


def time_expression_query() -> BusinessQuery:
    return BusinessQuery(
        question="近30天按地区看销售额",
        objective="lookup",
        metrics=["销售额"],
        dimensions=["地区"],
        time_expression="近30天",
    )


def test_time_expression_binds_a_governed_axis_the_question_never_named() -> None:
    """A question that carries 近30天 rarely names the date column; the axis still resolves.

    This is the regression guard for the unified Ask path: the intent model returns
    ``dimensions=["地区"]`` with ``time_expression="近30天"``, so waiting for the axis to appear
    among the grounded dimensions would always drop the axis.
    """
    query = time_expression_query()
    result = ground(
        [metric(phrases=("销售额",)), dimension(phrases=("地区",)), time_axis_dimension()],
        query,
    )

    assert result.is_executable is True
    axes = [binding for binding in result.grounded_query.bindings if binding.time_axis]
    assert [binding.business_term for binding in axes] == ["下单日期"]
    assert axes[0].field_path == "order_date"


def test_time_expression_without_a_governed_axis_fails_closed() -> None:
    """A date-typed dimension is not an axis until an administrator says so."""
    query = time_expression_query()
    result = ground(
        [
            metric(phrases=("销售额",)),
            dimension(phrases=("地区",)),
            time_axis_dimension(time_axis=False),
        ],
        query,
    )

    assert result.is_executable is False
    assert not [binding for binding in result.grounded_query.bindings if binding.time_axis]
    assert any(
        "时间轴" in item for item in result.grounded_query.unresolved_ambiguities
    )
    assert any(
        "没有可确认的业务时间轴" in item.question for item in result.clarifications
    )


def test_no_time_expression_means_no_axis_is_bound() -> None:
    """Without a time expression the axis stays out of the bindings entirely."""
    query = BusinessQuery(
        question="按地区看销售额", objective="lookup", metrics=["销售额"], dimensions=["地区"]
    )
    result = ground(
        [metric(phrases=("销售额",)), dimension(phrases=("地区",)), time_axis_dimension()],
        query,
    )

    assert result.is_executable is True
    assert not [binding for binding in result.grounded_query.bindings if binding.time_axis]


def test_two_governed_time_axes_stay_ambiguous() -> None:
    """Two governed axes over different physical columns must ask, never pick one."""
    query = time_expression_query()
    result = ground(
        [
            metric(phrases=("销售额",)),
            dimension(phrases=("地区",)),
            time_axis_dimension(),
            time_axis_dimension("dimension_pay_date", "支付日期", "pay_date"),
        ],
        query,
    )

    assert result.is_executable is False
    assert not [binding for binding in result.grounded_query.bindings if binding.time_axis]
    assert any("不同的物理绑定" in item for item in result.grounded_query.unresolved_ambiguities)
    options = sorted(
        option.label for request in result.clarifications for option in request.options
    )
    assert options == ["下单日期", "支付日期"]
