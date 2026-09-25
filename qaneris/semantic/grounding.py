"""Semantic grounding: bind business expressions to already confirmed physical assets.

Grounding consumes a ``BusinessQuery`` and a ``SemanticRetrievalResult``. It never reads the
enterprise database, never re-scans, never calls a model to pick a field and never invents a
datasource, object, field or relationship: every physical fact in a binding comes from a candidate
that semantic retrieval already confirmed against the published graph.

Selection is structural and deterministic. Two candidates that resolve to the same physical asset
are a single binding; two candidates that resolve to different physical assets are an ambiguity that
is reported instead of resolved. Scores are used for stable ordering and evidence only.
"""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, Field

from qaneris.contracts.semantic import (
    BusinessQuery,
    GroundedQuery,
    GroundingBinding,
    SemanticAssetType,
    SemanticCandidate,
)
from qaneris.semantic.models import (
    ClarificationOption,
    ClarificationRequest,
    SemanticRetrievalPath,
    SemanticRetrievalResult,
)

# Tokenisation must stay identical to retrieval without creating an import cycle.
from qaneris.semantic.normalization import normalize_term as _normalize


class GroundingSlot(StrEnum):
    """A business slot of the query that has to be bound to real assets."""

    METRIC = "metric"
    DIMENSION = "dimension"
    ENTITY = "entity"
    FILTER = "filter"
    RANKING_METRIC = "ranking_metric"
    DERIVATION_METRIC = "derivation_metric"
    #: The business time axis. Unlike the other slots it is not addressed by a business
    #: expression: a question carrying 近30天 rarely names the date column, so the axis is the
    #: governed DIMENSION that declares ``time_axis`` for the scope.
    TIME_AXIS = "time_axis"


#: Asset types a slot accepts. A metric slot never binds a dimension, and vice versa.
_SLOT_ASSET_TYPES: dict[GroundingSlot, tuple[SemanticAssetType, ...]] = {
    GroundingSlot.METRIC: (SemanticAssetType.METRIC,),
    GroundingSlot.RANKING_METRIC: (SemanticAssetType.METRIC,),
    GroundingSlot.DERIVATION_METRIC: (SemanticAssetType.METRIC,),
    GroundingSlot.DIMENSION: (SemanticAssetType.DIMENSION,),
    GroundingSlot.ENTITY: (SemanticAssetType.BUSINESS_TERM, SemanticAssetType.DATA_OBJECT),
    GroundingSlot.FILTER: (SemanticAssetType.DIMENSION, SemanticAssetType.FIELD),
    GroundingSlot.TIME_AXIS: (SemanticAssetType.DIMENSION,),
}

_SLOT_LABELS: dict[GroundingSlot, str] = {
    GroundingSlot.METRIC: "指标",
    GroundingSlot.DIMENSION: "维度",
    GroundingSlot.ENTITY: "业务实体",
    GroundingSlot.FILTER: "过滤维度",
    GroundingSlot.RANKING_METRIC: "排名指标",
    GroundingSlot.DERIVATION_METRIC: "衍生指标",
    GroundingSlot.TIME_AXIS: "业务时间轴",
}

#: Governed semantic assets win over raw physical assets resolving to the same target.
_TYPE_PRIORITY = {
    SemanticAssetType.METRIC: 0,
    SemanticAssetType.DIMENSION: 0,
    SemanticAssetType.BUSINESS_TERM: 1,
    SemanticAssetType.DATA_OBJECT: 2,
    SemanticAssetType.FIELD: 2,
    SemanticAssetType.RELATIONSHIP: 3,
}

_PHYSICAL_BINDING_EVIDENCE = "企业数据图确认物理绑定："


class GroundingResult(BaseModel):
    """Grounding outcome: the grounded query plus any clarification the caller must ask for."""

    grounded_query: GroundedQuery
    clarifications: list[ClarificationRequest] = Field(default_factory=list)

    @property
    def needs_clarification(self) -> bool:
        return bool(self.clarifications)

    @property
    def is_executable(self) -> bool:
        """Only a fully resolved grounding may feed planning."""
        return not self.grounded_query.unresolved_ambiguities


class SemanticGrounder:
    """Deterministic slot-by-slot grounding over confirmed retrieval candidates."""

    def __init__(self, *, enforce_trusted_path: bool = True):
        self.enforce_trusted_path = enforce_trusted_path

    def ground(
        self, query: BusinessQuery, retrieval: SemanticRetrievalResult
    ) -> GroundingResult:
        bindings: list[GroundingBinding] = []
        unresolved: list[str] = []
        clarifications: list[ClarificationRequest] = []
        handled: set[str] = set()

        for slot, term in _slot_terms(query):
            key = _normalize(term)
            if key in handled:
                # The same expression always resolves to the same binding, so a term repeated in
                # another slot (for example ranking.metric) can never bind a different definition.
                continue
            handled.add(key)
            binding, problem, clarification = self._bind_term(slot, term, retrieval)
            if binding is not None:
                bindings.append(binding)
                continue
            if problem:
                unresolved.append(problem)
            if clarification is not None:
                clarifications.append(clarification)

        relationship_binding, relationship_problem, relationship_clarification = (
            self._relationship_binding(bindings, retrieval)
        )
        if relationship_binding is not None:
            bindings.append(relationship_binding)
        if relationship_problem:
            unresolved.append(relationship_problem)
        if relationship_clarification is not None:
            clarifications.append(relationship_clarification)

        time_axis_binding, time_axis_problem, time_axis_clarification = self._time_axis_binding(
            query, retrieval
        )
        if time_axis_binding is not None:
            bindings.append(time_axis_binding)
        if time_axis_problem:
            unresolved.append(time_axis_problem)
        if time_axis_clarification is not None:
            clarifications.append(time_axis_clarification)

        cross_source = _cross_source_problem(bindings)
        if cross_source:
            unresolved.append(cross_source)
            clarifications.append(
                ClarificationRequest(
                    clarification_id="grounding_cross_source",
                    field="datasource",
                    question=(
                        "当前问题需要同时使用多个数据源，但单次问数尚不支持跨数据源关联。"
                        "请分别查询相应数据源。"
                    ),
                )
            )
        if not bindings and not unresolved:
            unresolved.append("当前问题没有可确认的业务绑定")
            clarifications.append(
                ClarificationRequest(
                    clarification_id="grounding_no_binding",
                    field="grounding",
                    question=(
                        "当前已发布数据中没有找到足够的受治理结构来回答这个问题。"
                        "请检查相应数据源是否已经扫描并发布所需业务字段。"
                    ),
                )
            )

        return GroundingResult(
            grounded_query=GroundedQuery(
                business_query=query,
                candidates=list(retrieval.candidates),
                bindings=bindings,
                workspace_id=retrieval.workspace_id,
                requested_datasource_id=retrieval.requested_datasource_id,
                scan_version=retrieval.scan_version,
                allowed_datasource_ids=_distinct(binding.datasource_id for binding in bindings),
                allowed_data_object_ids=_distinct(
                    binding.data_object_id for binding in bindings
                ),
                allowed_field_paths=_distinct(binding.field_path for binding in bindings),
                allowed_relationship_ids=_distinct(
                    binding.relationship_id for binding in bindings
                ),
                unresolved_ambiguities=_unique(unresolved),
                # Evidence only: this is the weakest binding score, never a decision threshold.
                confidence=min((binding.score for binding in bindings), default=0.0),
            ),
            clarifications=_unique_requests(clarifications),
        )

    def _bind_term(
        self, slot: GroundingSlot, term: str, retrieval: SemanticRetrievalResult
    ) -> tuple[GroundingBinding | None, str | None, ClarificationRequest | None]:
        label = _SLOT_LABELS[slot]
        addressed = [
            (candidate, strength)
            for candidate in retrieval.candidates
            if candidate.asset_type in _SLOT_ASSET_TYPES[slot]
            and (strength := _address_strength(candidate, term)) > 0
        ]
        if not addressed:
            return (
                None,
                f"未找到与{label}“{term}”匹配的语义候选",
                _missing_clarification(slot, term),
            )
        # An expression that exactly matches a governed name or alias outranks one that only
        # contains it, so 实收销售额 is not ambiguous with a metric that merely aliases 销售额.
        best = max(strength for _, strength in addressed)
        matched = [candidate for candidate, strength in addressed if strength == best]
        if not matched:
            return (
                None,
                f"未找到与{label}“{term}”匹配的语义候选",
                _missing_clarification(slot, term),
            )
        return self._governed_binding(slot, term, matched, retrieval)

    def _time_axis_binding(
        self, query: BusinessQuery, retrieval: SemanticRetrievalResult
    ) -> tuple[GroundingBinding | None, str | None, ClarificationRequest | None]:
        """Bind the governed business time axis behind a natural-language time expression.

        The axis is never chosen by data type or by field name: only an asset that declares itself
        the time axis, and whose physical binding the graph confirms, is eligible. Zero eligible
        axes and more than one physical axis both fail closed, so the pipeline never silently picks
        a date column on the user's behalf.
        """
        if not query.time_expression:
            return None, None, None
        matched = [
            candidate
            for candidate in retrieval.candidates
            if candidate.time_axis
            and candidate.asset_type in _SLOT_ASSET_TYPES[GroundingSlot.TIME_AXIS]
        ]
        if not matched:
            message = (
                f"问题包含时间表达“{query.time_expression}”，"
                "但当前数据源没有受治理的业务时间轴资产"
            )
            return (
                None,
                message,
                ClarificationRequest(
                    clarification_id="grounding_time_axis_missing",
                    field="time_axis",
                    question=(
                        f"问题包含“{query.time_expression}”，但当前数据源没有可确认的业务时间轴。"
                        "已扫描的日期字段不能自动作为业务时间口径，请先发布相应时间轴定义。"
                    ),
                ),
            )
        return self._governed_binding(
            GroundingSlot.TIME_AXIS,
            query.time_expression,
            matched,
            retrieval,
            # The binding names the axis itself (下单日期), not the expression that needed it:
            # ``TimeSpec.dimension`` is a business time dimension, and that is what the context
            # builder resolves against the grounded bindings.
            use_candidate_name=True,
        )

    def _governed_binding(
        self,
        slot: GroundingSlot,
        term: str,
        matched: list[SemanticCandidate],
        retrieval: SemanticRetrievalResult,
        *,
        use_candidate_name: bool = False,
    ) -> tuple[GroundingBinding | None, str | None, ClarificationRequest | None]:
        """Apply the governance and uniqueness rules every slot shares.

        Scoped to the requested datasource, restricted to trusted assets on the trusted path,
        requiring a complete graph physical identity, refusing name-only migration lookups and
        refusing to choose between two different physical targets.
        """
        label = _SLOT_LABELS[slot]
        scoped = [
            candidate
            for candidate in matched
            if not retrieval.requested_datasource_id
            or candidate.datasource_id == retrieval.requested_datasource_id
        ]
        if not scoped:
            return (
                None,
                f"{label}“{term}”的候选不在指定数据源 {retrieval.requested_datasource_id} 内",
                _unavailable_clarification(slot, term, "当前选定数据源中没有对应定义"),
            )

        if self.enforce_trusted_path and retrieval.retrieval_path is SemanticRetrievalPath.TRUSTED:
            trusted = [candidate for candidate in scoped if candidate.trusted]
            if not trusted:
                return (
                    None,
                    f"{label}“{term}”的候选未受治理确认，不能进入可信语义落地",
                    _unavailable_clarification(slot, term, "候选定义尚未经过治理确认"),
                )
        else:
            trusted = scoped

        physical = [candidate for candidate in trusted if _has_physical_identity(candidate)]
        if not physical:
            return (
                None,
                f"{label}“{term}”的候选缺少完整物理身份",
                _unavailable_clarification(slot, term, "候选定义尚未绑定到已发布数据"),
            )

        stable = [candidate for candidate in physical if not candidate.migration_lookup]
        if not stable:
            return (
                None,
                f"{label}“{term}”的候选仅依赖名称迁移解析，不能作为正式物理绑定",
                _unavailable_clarification(slot, term, "候选定义需要重新确认数据绑定"),
            )

        groups = _physical_groups(slot, stable)
        if len(groups) == 1:
            chosen = _preferred(next(iter(groups.values())))
            return (
                _binding(chosen.name if use_candidate_name else term, chosen),
                None,
                None,
            )
        return (
            None,
            f"{label}“{term}”对应 {len(groups)} 个不同的物理绑定，未解决歧义",
            _ambiguity_clarification(slot, term, groups),
        )

    def _relationship_binding(
        self, bindings: list[GroundingBinding], retrieval: SemanticRetrievalResult
    ) -> tuple[GroundingBinding | None, str | None, ClarificationRequest | None]:
        object_ids = sorted(
            {binding.data_object_id for binding in bindings if binding.data_object_id}
        )
        if len(object_ids) <= 1:
            return None, None, None
        if len(object_ids) > 2:
            message = (
                f"绑定涉及 {len(object_ids)} 个数据对象，当前阶段只支持同一数据对象"
                "或一个已确认的直接关系，不得进入查询计划"
            )
            return (
                None,
                message,
                ClarificationRequest(
                    clarification_id="grounding_relationship_unsupported",
                    field="relationships",
                    question="当前问题需要连接多个数据对象，现有查询能力只支持一条已确认的直接关系。",
                ),
            )
        pair = set(object_ids)
        datasource_ids = {binding.datasource_id for binding in bindings if binding.datasource_id}
        matches = sorted(
            [
                candidate
                for candidate in retrieval.candidates
                if candidate.asset_type is SemanticAssetType.RELATIONSHIP
                and candidate.relationship_id
                and {candidate.from_data_object_id, candidate.to_data_object_id} == pair
                and (not datasource_ids or candidate.datasource_id in datasource_ids)
                and (not self.enforce_trusted_path or candidate.trusted)
            ],
            key=lambda candidate: (-candidate.score, candidate.relationship_id or ""),
        )
        if not matches:
            names = sorted(
                {
                    binding.data_object_name or binding.business_term
                    for binding in bindings
                    if binding.data_object_id in pair
                }
            )
            label = "和".join(names[:2]) if len(names) >= 2 else "这两个数据对象"
            return (
                None,
                (
                    "指标与维度位于不同数据对象，但企业数据图中没有已确认的 RELATES_TO 关系，"
                    "禁止按同名字段或 *_id 推测连接"
                ),
                ClarificationRequest(
                    clarification_id="grounding_relationship_missing",
                    field="relationships",
                    question=(
                        f"当前问题需要同时使用{label}，但企业数据图中没有它们之间已确认的关系。"
                        "Qaneris 不会按同名字段或 *_id 自动连接。"
                    ),
                ),
            )
        relationship_ids = {candidate.relationship_id for candidate in matches}
        if len(relationship_ids) > 1:
            return (
                None,
                "两个数据对象之间存在多个已确认关系，未解决歧义",
                ClarificationRequest(
                    clarification_id=f"grounding_relationship_{'-'.join(object_ids)}",
                    field="relationships",
                    question="这两个数据对象之间存在多个已确认关系，请确认使用哪一个：",
                    options=[
                        ClarificationOption(
                            option_id=candidate.relationship_id or "",
                            label=candidate.name,
                            description=_evidence_binding(candidate),
                        )
                        for candidate in matches
                    ],
                ),
            )
        chosen = matches[0]
        return (
            GroundingBinding(
                business_term=chosen.name,
                asset_type=SemanticAssetType.RELATIONSHIP,
                asset_id=chosen.asset_id,
                datasource_id=chosen.datasource_id,
                relationship_id=chosen.relationship_id,
                score=chosen.score,
                evidence=_binding_evidence(chosen),
                default_aggregation=chosen.default_aggregation,
                data_type=chosen.data_type,
                native_type=chosen.native_type,
                namespace=chosen.namespace,
                qualified_name=chosen.qualified_name,
                object_kind=chosen.object_kind,
                data_object_name=chosen.data_object_name,
            ),
            None,
            None,
        )


def _slot_terms(query: BusinessQuery) -> list[tuple[GroundingSlot, str]]:
    """Business expressions to bind, in a fixed slot order."""
    terms: list[tuple[GroundingSlot, str]] = []
    terms.extend((GroundingSlot.METRIC, term) for term in query.metrics)
    terms.extend((GroundingSlot.DIMENSION, term) for term in query.dimensions)
    terms.extend((GroundingSlot.ENTITY, term) for term in query.entities)
    terms.extend((GroundingSlot.FILTER, item.subject) for item in query.filters)
    if query.ranking and query.ranking.metric:
        terms.append((GroundingSlot.RANKING_METRIC, query.ranking.metric))
    terms.extend(
        (GroundingSlot.DERIVATION_METRIC, item.metric)
        for item in query.derivations
        if item.metric
    )
    return [(slot, term) for slot, term in terms if term and term.strip()]


def _missing_clarification(slot: GroundingSlot, term: str) -> ClarificationRequest:
    label = _SLOT_LABELS[slot]
    return ClarificationRequest(
        clarification_id=f"grounding_missing_{slot.value}_{_slug(term)}",
        field=f"{slot.value}:{term}",
        question=(
            f"当前已发布数据中没有找到可确认的{label}“{term}”。"
            "如果该数据存在，请补充对应数据源或发布相应业务定义。"
        ),
    )


def _unavailable_clarification(
    slot: GroundingSlot, term: str, reason: str
) -> ClarificationRequest:
    return ClarificationRequest(
        clarification_id=f"grounding_unavailable_{slot.value}_{_slug(term)}",
        field=f"{slot.value}:{term}",
        question=(
            f"当前无法确认{_SLOT_LABELS[slot]}“{term}”：{reason}。"
            "请先完成该业务定义的发布。"
        ),
    )


def _address_strength(candidate: SemanticCandidate, term: str) -> int:
    """How strongly one business expression addresses this candidate.

    ``2`` means the expression is exactly one of the candidate's governed labels (its name or an
    alias), ``1`` means the expression or a label contains the other, ``0`` means no address. Sharing
    a mere 2-gram is never an address. A candidate that declares no address is never bound: grounding
    fails closed rather than guessing which definition the user meant.
    """
    target = _normalize(term)
    if not target:
        return 0
    labels = [
        normalized
        for value in [candidate.name, *candidate.labels]
        if (normalized := _normalize(value))
    ]
    if target in labels:
        return 2
    if any(len(value) >= 2 and value in target for value in labels):
        return 1
    if any(
        target == normalized or target in normalized
        for phrase in candidate.matched_phrases
        if (normalized := _normalize(phrase))
    ):
        return 1
    return 0


def _has_physical_identity(candidate: SemanticCandidate) -> bool:
    if candidate.asset_type in {
        SemanticAssetType.METRIC,
        SemanticAssetType.DIMENSION,
        SemanticAssetType.FIELD,
    }:
        return bool(candidate.datasource_id and candidate.data_object_id and candidate.field_path)
    if candidate.asset_type is SemanticAssetType.RELATIONSHIP:
        return bool(candidate.relationship_id)
    return bool(candidate.datasource_id and candidate.data_object_id)


def _physical_target(
    slot: GroundingSlot, candidate: SemanticCandidate
) -> tuple[str, str | None, str | None]:
    if slot is GroundingSlot.ENTITY:
        # A business term and the data object it is bound to are the same physical target.
        return (candidate.datasource_id or "", candidate.data_object_id, None)
    return (candidate.datasource_id or "", candidate.data_object_id, candidate.field_path)


def _physical_groups(
    slot: GroundingSlot, candidates: list[SemanticCandidate]
) -> dict[tuple[str, str | None, str | None], list[SemanticCandidate]]:
    groups: dict[tuple[str, str | None, str | None], list[SemanticCandidate]] = {}
    for candidate in sorted(candidates, key=_candidate_order):
        groups.setdefault(_physical_target(slot, candidate), []).append(candidate)
    return groups


def _preferred(candidates: list[SemanticCandidate]) -> SemanticCandidate:
    return min(candidates, key=_candidate_order)


def _candidate_order(candidate: SemanticCandidate) -> tuple[int, float, str, str]:
    return (
        _TYPE_PRIORITY.get(candidate.asset_type, 9),
        -candidate.score,
        candidate.name,
        candidate.asset_id,
    )


def _binding(term: str, candidate: SemanticCandidate) -> GroundingBinding:
    return GroundingBinding(
        business_term=term,
        asset_type=candidate.asset_type,
        asset_id=candidate.asset_id,
        datasource_id=candidate.datasource_id,
        data_object_id=candidate.data_object_id,
        field_path=candidate.field_path,
        relationship_id=candidate.relationship_id,
        score=candidate.score,
        evidence=_binding_evidence(candidate),
        default_aggregation=candidate.default_aggregation,
        time_axis=candidate.time_axis,
        data_type=candidate.data_type,
        native_type=candidate.native_type,
        namespace=candidate.namespace,
        qualified_name=candidate.qualified_name,
        object_kind=candidate.object_kind,
        data_object_name=candidate.data_object_name,
    )


def _binding_evidence(candidate: SemanticCandidate) -> list[str]:
    return [*candidate.evidence, *candidate.reasons]


def _ambiguity_clarification(
    slot: GroundingSlot,
    term: str,
    groups: dict[tuple[str, str | None, str | None], list[SemanticCandidate]],
) -> ClarificationRequest:
    options = [
        ClarificationOption(
            option_id=preferred.asset_id,
            label=preferred.name,
            description=_option_description(preferred),
        )
        for preferred in sorted(
            (_preferred(group) for group in groups.values()), key=_candidate_order
        )
    ]
    labels = "、".join(option.label for option in options)
    question = (
        f"当前数据源存在多个可确认的业务时间轴，请确认“{term}”应按哪个日期口径：{labels}"
        if slot is GroundingSlot.TIME_AXIS
        else f"“{term}”有多个企业定义，请确认你指的是：{labels}"
    )
    return ClarificationRequest(
        clarification_id=f"grounding_{slot.value}_{_slug(term)}",
        field=f"{slot.value}:{term}",
        question=question,
        options=options,
    )


def _option_description(candidate: SemanticCandidate) -> str | None:
    """Business-facing description: no internal graph or asset identifiers."""
    if candidate.field_path:
        return f"物理字段：{candidate.field_path}"
    return None


def _evidence_binding(candidate: SemanticCandidate) -> str | None:
    for line in candidate.evidence:
        if line.startswith(_PHYSICAL_BINDING_EVIDENCE):
            return line[len(_PHYSICAL_BINDING_EVIDENCE) :]
    return None


def _cross_source_problem(bindings: list[GroundingBinding]) -> str | None:
    datasource_ids = sorted(
        {binding.datasource_id for binding in bindings if binding.datasource_id}
    )
    if len(datasource_ids) > 1:
        return (
            f"绑定跨越多个数据源 {datasource_ids}，当前阶段不支持跨数据源查询，"
            "不得进入查询计划"
        )
    return None


def _distinct(values) -> list[str]:
    return sorted({value for value in values if value})


def _unique(messages: list[str]) -> list[str]:
    return list(dict.fromkeys(messages))


def _unique_requests(requests: list[ClarificationRequest]) -> list[ClarificationRequest]:
    unique: dict[str, ClarificationRequest] = {}
    for request in requests:
        unique.setdefault(request.clarification_id, request)
    return [unique[key] for key in sorted(unique)]


def _slug(value: str) -> str:
    return _normalize(value) or "term"
