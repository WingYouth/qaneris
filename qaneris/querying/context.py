"""Query context construction: ``GroundedQuery`` -> planner-facing ``GroundedQueryContext``.

The context is the boundary where the pipeline stops looking at meaning and starts looking at
columns. Building it is pure and fail-closed: it reads no enterprise database, no published graph,
no ``CompanyDataProfile``, no model, and it never runs retrieval again. Every physical asset it
carries — including the allowlists — comes from a binding that grounding already confirmed, and a
grounding that is not fully resolved cannot become an executable context at all.
"""

from __future__ import annotations

import re

from qaneris.common.errors import QueryContextBuildError
from qaneris.contracts.query import (
    FilterOperator,
    GroundedDataObjectRef,
    GroundedFieldRef,
    GroundedQueryContext,
    QueryContextBinding,
    QueryContextFilter,
    QueryContextRelationship,
    TimeRange,
    TimeSpec,
    aggregate_function_for,
)
from qaneris.contracts.semantic import SemanticAssetType, SemanticCandidate
from qaneris.semantic.grounding import GroundingResult

#: Asset types a filter subject may have been bound to. Mirrors the grounding filter slot rule: a
#: context is only built from a binding the corresponding slot could legally produce.
_FILTER_ASSET_TYPES = frozenset({SemanticAssetType.DIMENSION, SemanticAssetType.FIELD})

_TYPE_PRIORITY = {
    SemanticAssetType.METRIC: 0,
    SemanticAssetType.DIMENSION: 0,
    SemanticAssetType.BUSINESS_TERM: 1,
    SemanticAssetType.DATA_OBJECT: 2,
    SemanticAssetType.FIELD: 2,
    SemanticAssetType.RELATIONSHIP: 3,
}

#: Structured business filter operators -> the Phase 1 filter contract. ``not_in`` has no counterpart
#: in the existing contract, so it is refused rather than silently weakened to another operator.
_FILTER_OPERATORS: dict[str, FilterOperator] = {
    "=": FilterOperator.EQUALS,
    "==": FilterOperator.EQUALS,
    "eq": FilterOperator.EQUALS,
    "equal": FilterOperator.EQUALS,
    "equals": FilterOperator.EQUALS,
    "等于": FilterOperator.EQUALS,
    "是": FilterOperator.EQUALS,
    "!=": FilterOperator.NOT_EQUALS,
    "<>": FilterOperator.NOT_EQUALS,
    "ne": FilterOperator.NOT_EQUALS,
    "notequal": FilterOperator.NOT_EQUALS,
    "notequals": FilterOperator.NOT_EQUALS,
    "不等于": FilterOperator.NOT_EQUALS,
    ">": FilterOperator.GREATER_THAN,
    "gt": FilterOperator.GREATER_THAN,
    "greaterthan": FilterOperator.GREATER_THAN,
    "大于": FilterOperator.GREATER_THAN,
    ">=": FilterOperator.GREATER_OR_EQUAL,
    "gte": FilterOperator.GREATER_OR_EQUAL,
    "greaterorequal": FilterOperator.GREATER_OR_EQUAL,
    "大于等于": FilterOperator.GREATER_OR_EQUAL,
    "不小于": FilterOperator.GREATER_OR_EQUAL,
    "<": FilterOperator.LESS_THAN,
    "lt": FilterOperator.LESS_THAN,
    "lessthan": FilterOperator.LESS_THAN,
    "小于": FilterOperator.LESS_THAN,
    "<=": FilterOperator.LESS_OR_EQUAL,
    "lte": FilterOperator.LESS_OR_EQUAL,
    "lessorequal": FilterOperator.LESS_OR_EQUAL,
    "小于等于": FilterOperator.LESS_OR_EQUAL,
    "不大于": FilterOperator.LESS_OR_EQUAL,
    "in": FilterOperator.IN,
    "属于": FilterOperator.IN,
    "between": FilterOperator.BETWEEN,
    "range": FilterOperator.BETWEEN,
    "区间": FilterOperator.BETWEEN,
    "之间": FilterOperator.BETWEEN,
    "contains": FilterOperator.CONTAINS,
    "like": FilterOperator.CONTAINS,
    "包含": FilterOperator.CONTAINS,
}

_WORD_SEPARATORS = re.compile(r"[\s_]+")


def _operator_key(value: str) -> str:
    return _WORD_SEPARATORS.sub("", value).casefold()


class QueryContextBuilder:
    """Deterministic builder of the formal query context.

    ``time_range`` is a normalized fact handed in by the caller (rule facts); it is never parsed
    from the question here. ``time_spec`` names a business time axis the user picked (one of
    ``expression`` or ``dimension``); the builder resolves the dimension to a bound physical
    field and refuses the context when the bound field is not a date/datetime column.
    """

    def build(
        self,
        grounding: GroundingResult,
        *,
        workspace_id: str | None = None,
        requested_datasource_id: str | None = None,
        time_range: TimeRange | None = None,
        time_spec: TimeSpec | None = None,
    ) -> GroundedQueryContext:
        grounded = grounding.grounded_query
        query = grounded.business_query
        self._require_executable(grounding)

        bindings = _context_bindings(grounded.bindings, grounded.candidates)
        workspace = _resolve_workspace(workspace_id, grounded.workspace_id)
        requested = _resolve_requested_datasource(requested_datasource_id, grounded.requested_datasource_id)

        if query.comparison is not None:
            raise QueryContextBuildError(
                "第一阶段不支持比较分析（comparison），不能创建可执行查询上下文"
            )
        _derivations(query.derivations, bindings)
        ranking = _ranking(query.ranking, bindings)
        metrics = _bound_terms(bindings, query.metrics, SemanticAssetType.METRIC, "指标")
        dimensions = _bound_terms(bindings, query.dimensions, SemanticAssetType.DIMENSION, "维度")
        entities = _bound_terms(
            bindings,
            query.entities,
            SemanticAssetType.BUSINESS_TERM,
            "业务实体",
            extra={SemanticAssetType.DATA_OBJECT},
        )
        filters = [_filter(item.subject, item.operator, item.value, bindings) for item in query.filters]
        relationships = _relationships(bindings, grounded.candidates)
        time_field = _time_field(time_spec, bindings, time_range)
        data_objects = _data_objects(bindings)

        allowed_datasource_ids = _distinct(binding.datasource_id for binding in bindings)
        if requested and requested not in allowed_datasource_ids:
            raise QueryContextBuildError(
                f"指定数据源 {requested} 没有已落地的物理绑定，不能创建查询上下文"
            )
        if len(allowed_datasource_ids) > 1:
            raise QueryContextBuildError(
                f"绑定跨越多个数据源 {allowed_datasource_ids}，"
                "第一阶段不支持跨数据源查询，不能创建可执行查询上下文"
            )
        _validate_aggregation_semantics(bindings, query.derivations)

        return GroundedQueryContext(
            workspace_id=workspace,
            requested_datasource_id=requested,
            objective=query.objective,
            business_query=query,
            bindings=bindings,
            metrics=metrics,
            dimensions=dimensions,
            entities=entities,
            filters=filters,
            ranking=ranking,
            derivations=list(query.derivations),
            time_expression=query.time_expression,
            time_range=time_range,
            time_spec=time_spec,
            time_field=time_field,
            requested_output=list(query.requested_output),
            relationships=relationships,
            data_objects=data_objects,
            scan_version=grounded.scan_version,
            allowed_datasource_ids=allowed_datasource_ids,
            allowed_data_object_ids=_distinct(
                binding.data_object_id for binding in bindings
            ),
            allowed_field_paths=_distinct(binding.field_path for binding in bindings),
            allowed_field_ids=_distinct(binding.field_id for binding in bindings),
            allowed_relationship_ids=_distinct(
                binding.relationship_id for binding in bindings
            ),
            unresolved_ambiguities=[],
            confidence=min((binding.score for binding in bindings), default=0.0),
        )

    @staticmethod
    def _require_executable(grounding: GroundingResult) -> None:
        grounded = grounding.grounded_query
        if grounded.unresolved_ambiguities:
            raise QueryContextBuildError(
                "语义落地存在未解决歧义，不能创建可执行查询上下文："
                + "；".join(grounded.unresolved_ambiguities)
            )
        if grounding.clarifications:
            raise QueryContextBuildError(
                "语义落地需要用户澄清后才能继续，不能创建可执行查询上下文："
                + "；".join(item.question for item in grounding.clarifications)
            )
        if not grounded.bindings:
            raise QueryContextBuildError("语义落地没有产生任何物理绑定，不能创建查询上下文")


def _context_bindings(
    bindings: list, candidates: list[SemanticCandidate]
) -> list[QueryContextBinding]:
    """Carry each grounding binding into the context with its confirmed graph identity."""
    field_ids = _candidate_field_ids(candidates)
    data_type_map = _candidate_field_type(candidates)
    object_locator_map = _candidate_object_locator(candidates)
    context: list[QueryContextBinding] = []
    for binding in bindings:
        if not binding.datasource_id:
            raise QueryContextBuildError(
                f"绑定“{binding.business_term}”缺少数据源身份，不能创建查询上下文"
            )
        if not binding.data_object_id and binding.asset_type is not SemanticAssetType.RELATIONSHIP:
            raise QueryContextBuildError(
                f"绑定“{binding.business_term}”缺少数据对象身份，不能创建查询上下文"
            )
        field_id = None
        if binding.field_path and binding.data_object_id:
            field_id = field_ids.get(
                (binding.datasource_id, binding.data_object_id, binding.field_path)
            )
        data_type, native_type = (None, None)
        if binding.field_path and binding.data_object_id:
            data_type, native_type = data_type_map.get(
                (binding.datasource_id, binding.data_object_id, binding.field_path),
                (None, None),
            )
        data_object_name = namespace = qualified_name = object_kind = None
        if binding.data_object_id:
            (
                data_object_name,
                namespace,
                qualified_name,
                object_kind,
            ) = object_locator_map.get(
                (binding.datasource_id, binding.data_object_id),
                (None, None, None, None),
            )
        context.append(
            QueryContextBinding(
                business_term=binding.business_term,
                asset_type=binding.asset_type,
                asset_id=binding.asset_id,
                datasource_id=binding.datasource_id,
                data_object_id=binding.data_object_id,
                field_id=field_id,
                field_path=binding.field_path,
                relationship_id=binding.relationship_id,
                score=binding.score,
                evidence=list(binding.evidence),
                default_aggregation=getattr(binding, "default_aggregation", None),
                data_type=data_type,
                native_type=native_type,
                namespace=namespace,
                qualified_name=qualified_name,
                object_kind=object_kind,
                data_object_name=data_object_name,
            )
        )
    return context


def _candidate_field_ids(
    candidates: list[SemanticCandidate],
) -> dict[tuple[str, str, str], str | None]:
    """Graph field identity per physical field, taken from the retrieval candidates.

    Grounding deliberately does not carry a field id, so the context resolves it from the candidate
    the binding came from. Two different graph identities for one physical field would mean the graph
    itself is contradictory, which fails closed instead of picking one.
    """
    identities: dict[tuple[str, str, str], set[str]] = {}
    for candidate in candidates:
        if not (candidate.datasource_id and candidate.data_object_id and candidate.field_path):
            continue
        key = (candidate.datasource_id, candidate.data_object_id, candidate.field_path)
        identities.setdefault(key, set())
        if candidate.field_id:
            identities[key].add(candidate.field_id)
    resolved: dict[tuple[str, str, str], str | None] = {}
    for key in sorted(identities):
        found = sorted(identities[key])
        if len(found) > 1:
            raise QueryContextBuildError(
                "同一物理字段在企业数据图中存在多个字段标识，不能创建查询上下文："
                f"{key} → {found}"
            )
        resolved[key] = found[0] if found else None
    return resolved


def _candidate_field_type(
    candidates: list[SemanticCandidate],
) -> dict[tuple[str, str, str], tuple[str | None, str | None]]:
    """Field-level ``(data_type, native_type)`` per physical field, captured from the graph.

    The data type is only meaningful when every candidate that mentions the same physical field
    agrees on it. Two disagreeing type declarations would mean the graph is contradictory and the
    context fails closed.
    """
    aggregated: dict[tuple[str, str, str], set[tuple[str | None, str | None]]] = {}
    for candidate in candidates:
        if not (candidate.datasource_id and candidate.data_object_id and candidate.field_path):
            continue
        key = (candidate.datasource_id, candidate.data_object_id, candidate.field_path)
        aggregated.setdefault(key, set()).add((candidate.data_type, candidate.native_type))
    resolved: dict[tuple[str, str, str], tuple[str | None, str | None]] = {}
    for key in sorted(aggregated):
        options = sorted(aggregated[key])
        if len(options) > 1:
            raise QueryContextBuildError(
                "同一物理字段在企业数据图中存在多种类型声明，不能创建查询上下文："
                f"{key} → {options}"
            )
        item = options[0]
        resolved[key] = item
    return resolved


def _candidate_object_locator(
    candidates: list[SemanticCandidate],
) -> dict[tuple[str, str], tuple[str | None, str | None, str | None, str | None]]:
    """Object-level locator per data object, from the graph.

    Returns the four-tuple ``(data_object_name, namespace, qualified_name, object_kind)``.
    Order independence is structural: candidates are aggregated into a set, so a reversal or
    interleaving of empty and populated candidates never changes the verdict.

    Rules:
    * Empty locators (every component ``None``) never disqualify a richer locator for the same
      data object. They are also never recorded as the answer.
    * Two or more *different populated* locators for one data object fail closed: the graph
      itself would be contradictory.
    * When every candidate is empty for a data object the result is the empty / compatibility
      locator, so downstream callers can distinguish a graph-backed binding from a legacy one.
    """
    EMPTY = (None, None, None, None)

    def _is_empty(option: tuple[str | None, str | None, str | None, str | None]) -> bool:
        return all(part is None for part in option)

    aggregated: dict[tuple[str, str], set[tuple[str | None, str | None, str | None, str | None]]] = {}
    for candidate in candidates:
        if not (candidate.datasource_id and candidate.data_object_id):
            continue
        key = (candidate.datasource_id, candidate.data_object_id)
        option = (
            candidate.data_object_name,
            candidate.namespace,
            candidate.qualified_name,
            candidate.object_kind,
        )
        if _is_empty(option):
            # An empty candidate never disqualifies the data object a richer candidate already
            # described. Empty candidates also never become the recorded locator on their own.
            aggregated.setdefault(key, set())
            continue
        aggregated.setdefault(key, set()).add(option)
    resolved: dict[tuple[str, str], tuple[str | None, str | None, str | None, str | None]] = {}
    for key in sorted(aggregated):
        options = sorted(aggregated[key])
        if len(options) > 1:
            raise QueryContextBuildError(
                "同一数据对象在企业数据图中存在多种定位，不能创建查询上下文："
                f"{key} → {options}"
            )
        resolved[key] = options[0] if options else EMPTY
    return resolved


def _resolve_workspace(requested: str | None, grounded: str) -> str:
    if requested is not None and requested != grounded:
        raise QueryContextBuildError(
            f"工作区 {requested} 与语义落地工作区 {grounded} 不一致，不能创建查询上下文"
        )
    return grounded


def _resolve_requested_datasource(requested: str | None, grounded: str | None) -> str | None:
    if requested is None:
        return grounded
    if grounded is not None and requested != grounded:
        raise QueryContextBuildError(
            f"指定数据源 {requested} 与语义落地数据源 {grounded} 不一致，不能创建查询上下文"
        )
    return requested


def _bound_terms(
    bindings: list[QueryContextBinding],
    terms: list[str],
    asset_type: SemanticAssetType,
    label: str,
    *,
    extra: set[SemanticAssetType] | None = None,
) -> list[QueryContextBinding]:
    """Bind each business expression of one slot to its already grounded physical binding."""
    accepted = {asset_type, *(extra or set())}
    chosen: list[QueryContextBinding] = []
    for term in terms:
        matches = [
            binding
            for binding in bindings
            if binding.business_term == term and binding.asset_type in accepted
        ]
        if not matches:
            raise QueryContextBuildError(
                f"{label}“{term}”没有已落地的物理绑定，不能创建查询上下文"
            )
        chosen.append(_preferred(matches))
    return _dedupe(chosen)


def _filter(
    subject: str, operator: str, value: object, bindings: list[QueryContextBinding]
) -> QueryContextFilter:
    matches = [
        binding
        for binding in bindings
        if binding.business_term == subject and binding.asset_type in _FILTER_ASSET_TYPES
    ]
    if not matches:
        raise QueryContextBuildError(f"过滤主题“{subject}”没有已落地的物理绑定，不能创建查询上下文")
    reference = _preferred(matches).field_ref()
    if reference is None:
        raise QueryContextBuildError(f"过滤主题“{subject}”没有物理字段绑定，不能创建查询上下文")
    mapped = _FILTER_OPERATORS.get(_operator_key(operator))
    if mapped is None:
        raise QueryContextBuildError(f"过滤运算符第一阶段不支持：{operator}（{subject}）")
    _validate_filter_value(mapped, value, subject)
    return QueryContextFilter(subject=subject, field=reference, operator=mapped, value=value)


def _validate_filter_value(operator: FilterOperator, value: object, subject: str) -> None:
    if operator is FilterOperator.IN and (
        not isinstance(value, (list, tuple)) or not value
    ):
        raise QueryContextBuildError(f"过滤“{subject}”使用 IN 时需要非空值列表")
    if operator is FilterOperator.BETWEEN and (
        not isinstance(value, (list, tuple)) or len(value) != 2
    ):
        raise QueryContextBuildError(f"过滤“{subject}”使用区间时需要两个边界值")


def _ranking(ranking, bindings: list[QueryContextBinding]):
    if ranking is None:
        return None
    if not ranking.metric:
        raise QueryContextBuildError("排名缺少指标业务表达，第一阶段不猜测排名依据")
    if not any(
        binding.business_term == ranking.metric
        and binding.asset_type is SemanticAssetType.METRIC
        for binding in bindings
    ):
        raise QueryContextBuildError(
            f"排名指标“{ranking.metric}”没有已落地的物理绑定，不能创建查询上下文"
        )
    return ranking


def _derivations(derivations: list, bindings: list[QueryContextBinding]) -> list:
    """Phase 1 can only express a derivation as one of the supported aggregate functions."""
    for derivation in derivations:
        function = aggregate_function_for(derivation.operation)
        if function is None:
            raise QueryContextBuildError(
                f"衍生计算“{derivation.operation}”不在第一阶段支持范围，不能创建可执行查询上下文"
            )
        if not derivation.metric:
            raise QueryContextBuildError(
                f"衍生计算“{derivation.operation}”没有指标业务表达，不能创建查询上下文"
            )
        if not any(
            binding.business_term == derivation.metric
            and binding.asset_type is SemanticAssetType.METRIC
            for binding in bindings
        ):
            raise QueryContextBuildError(
                f"衍生指标“{derivation.metric}”没有已落地的物理绑定，不能创建查询上下文"
            )
    return list(derivations)


def _relationships(
    bindings: list[QueryContextBinding], candidates: list[SemanticCandidate]
) -> list[QueryContextRelationship]:
    """Confirmed relationships of the grounding, with the join keys they were confirmed with."""
    by_relationship: dict[str, SemanticCandidate] = {}
    for candidate in candidates:
        if candidate.asset_type is SemanticAssetType.RELATIONSHIP and candidate.relationship_id:
            by_relationship.setdefault(candidate.relationship_id, candidate)
    relationships: list[QueryContextRelationship] = []
    for binding in bindings:
        if binding.asset_type is not SemanticAssetType.RELATIONSHIP:
            continue
        relationship_id = binding.relationship_id or ""
        candidate = by_relationship.get(relationship_id)
        keys = (
            candidate.from_data_object_id,
            candidate.from_field_path,
            candidate.to_data_object_id,
            candidate.to_field_path,
        ) if candidate is not None else (None, None, None, None)
        if not all(keys):
            raise QueryContextBuildError(
                f"已确认关系 {relationship_id} 缺少连接字段，不能创建可执行查询上下文"
            )
        relationships.append(
            QueryContextRelationship(
                relationship_id=relationship_id,
                relationship_type=candidate.name,
                datasource_id=binding.datasource_id,
                from_data_object_id=keys[0],
                from_field_path=keys[1],
                to_data_object_id=keys[2],
                to_field_path=keys[3],
                evidence=list(binding.evidence),
            )
        )
    return relationships


def _time_field(
    spec: TimeSpec | None, bindings: list[QueryContextBinding], time_range: TimeRange | None
) -> GroundedFieldRef | None:
    """Resolve the time axis only via a structured ``TimeSpec`` whose dimension was grounded.

    The dimension is the business term the user picked. ``TimeSpec`` is converged to carry
    exactly that one piece of information: the natural-language phrase lives on
    ``BusinessQuery.time_expression`` and the normalised date boundaries live on ``TimeRange``,
    so the builder cannot mistake one for the other. The bound column must declare a date or
    datetime type (including ``timestamp`` / ``timestamptz``); ``time`` and other plain
    types are refused here, not silently widened to a date axis.
    """
    reference: GroundedFieldRef | None = None
    if spec is None:
        if time_range is not None:
            raise QueryContextBuildError(
                "查询包含时间范围，但调用者没有提供结构化 TimeSpec，不能创建可执行查询上下文"
            )
        return None
    # ``dimension`` is a required field on the converged ``TimeSpec``; reaching this branch with
    # ``None`` means the contract has been bypassed and the builder refuses the context.
    if not spec.dimension:
        raise QueryContextBuildError(
            "TimeSpec 缺少 dimension；执行前必须有一个已落地的时间维度绑定"
        )
    matches = [
        binding
        for binding in bindings
        if binding.business_term == spec.dimension and binding.field_path
    ]
    if not matches:
        raise QueryContextBuildError(
            f"时间维度“{spec.dimension}”没有已落地的物理绑定，不能创建查询上下文"
        )
    candidate = _preferred(matches)
    reference = candidate.field_ref()
    if reference is None:
        raise QueryContextBuildError(
            f"时间维度“{spec.dimension}”没有可用的物理字段绑定，不能创建查询上下文"
        )
    if not reference.is_date_type():
        raise QueryContextBuildError(
            f"时间维度“{spec.dimension}”绑定到非日期字段 {reference.field_path}，"
            "不能作为时间轴使用"
        )
    if time_range is None:
        raise QueryContextBuildError(
            f"指定了时间维度“{spec.dimension}”但没有规范化时间范围，不能创建查询上下文"
        )
    return reference


def _data_objects(
    bindings: list[QueryContextBinding],
) -> dict[str, GroundedDataObjectRef]:
    """Every data object the bindings touched, with its full physical locator.

    Keys are the graph's stable ``data_object_id``; the values carry ``namespace`` /
    ``qualified_name`` / ``object_kind`` so the generator can target the right database object
    without going back to the graph. ``public.orders`` and ``archive.orders`` therefore never
    share a single locator.
    """
    locators: dict[str, GroundedDataObjectRef] = {}
    for binding in bindings:
        if binding.asset_type is SemanticAssetType.RELATIONSHIP or not binding.data_object_id:
            continue
        if binding.data_object_id in locators:
            continue
        locator = _object_locator(binding)
        if locator is not None:
            locators[binding.data_object_id] = locator
    return locators


def _object_locator(binding: QueryContextBinding) -> GroundedDataObjectRef | None:
    """Build a locator from the binding, with the native object name from the published graph.

    ``name`` is the data object's native name (``GraphDataObject.name``): the identifier the
    database driver reports for the resolved table or collection (e.g. ``orders``). It is **not**
    the business term (e.g. ``销售额``) and it is **not** ``qualified_name`` (e.g. ``public.orders``).

    On the formal graph-backed main path ``name`` is always populated from the graph; a context
    that reaches the planner without it is one that never went through retrieval, and the P1-04C2
    execution validator refuses to compile against a ``name=None`` locator rather than guess.
    """
    if not binding.data_object_id:
        return None
    return GroundedDataObjectRef(
        datasource_id=binding.datasource_id,
        data_object_id=binding.data_object_id,
        name=binding.data_object_name,
        namespace=binding.namespace,
        qualified_name=binding.qualified_name,
        object_kind=binding.object_kind,
    )


def _validate_aggregation_semantics(
    bindings: list[QueryContextBinding], derivations: list[object]
) -> None:
    """A bound metric must either declare an aggregation rule or be targeted by a derivation.

    The planner picks a concrete aggregate function from a structured ``DerivationSpec`` first,
    then from ``binding.default_aggregation``; a metric that has neither fails closed here, with
    a clear error. A non-additive metric (ratio, distinct count, ...) that has no default is
    expected to be expressed through a derivation; the planner never falls back to ``SUM``.
    """
    derived_metrics = {
        item.metric
        for item in derivations
        if getattr(item, "metric", None)
    }
    for binding in bindings:
        if binding.asset_type is not SemanticAssetType.METRIC:
            continue
        if binding.business_term in derived_metrics:
            # The planner will use the derivation's op directly; no default is required.
            continue
        if binding.default_aggregation is None:
            raise QueryContextBuildError(
                f"指标“{binding.business_term}”没有受治理聚合语义，也没有结构化衍生计算覆盖，"
                "不能创建可执行查询上下文"
            )


def _preferred(bindings: list[QueryContextBinding]) -> QueryContextBinding:
    return min(
        bindings,
        key=lambda binding: (
            _TYPE_PRIORITY.get(binding.asset_type, 9),
            -binding.score,
            binding.asset_id,
        ),
    )


def _dedupe(bindings: list[QueryContextBinding]) -> list[QueryContextBinding]:
    seen: dict[tuple[str, str | None, str | None], QueryContextBinding] = {}
    for binding in bindings:
        seen.setdefault(
            (binding.datasource_id, binding.data_object_id, binding.field_path), binding
        )
    return list(seen.values())


def _distinct(values) -> list[str]:
    return sorted({value for value in values if value})
