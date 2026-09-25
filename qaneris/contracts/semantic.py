from __future__ import annotations

from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, model_validator


class AggregateFunction(StrEnum):
    """Phase 1 aggregation functions a governed metric may declare as its default.

    Lives in the semantic contracts because it is a property of a governed metric asset, not a
    property of the plan query model. The query planning contracts re-export it for callers.
    """

    COUNT = "count"
    SUM = "sum"
    AVERAGE = "average"
    MINIMUM = "minimum"
    MAXIMUM = "maximum"


class BusinessObjective(StrEnum):
    LOOKUP = "lookup"
    COMPARISON = "comparison"
    TREND = "trend"
    RANKING = "ranking"
    DIAGNOSIS = "diagnosis"


class RequestedOutput(StrEnum):
    SCALAR = "scalar"
    TABLE = "table"
    TREND = "trend"
    EXPLANATION = "explanation"


class ComparisonType(StrEnum):
    YEAR_OVER_YEAR = "year_over_year"
    PERIOD_OVER_PERIOD = "period_over_period"
    GROUP = "group"
    BASELINE = "baseline"


class RankingDirection(StrEnum):
    TOP = "top"
    BOTTOM = "bottom"


class SemanticAssetType(StrEnum):
    BUSINESS_TERM = "business_term"
    METRIC = "metric"
    DIMENSION = "dimension"
    DATASOURCE = "datasource"
    DATA_OBJECT = "data_object"
    FIELD = "field"
    RELATIONSHIP = "relationship"
    HISTORICAL_QUERY = "historical_query"


class BusinessFilter(BaseModel):
    model_config = ConfigDict(extra="forbid")

    subject: str = Field(min_length=1)
    operator: str = Field(min_length=1)
    value: Any


class ComparisonSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")

    comparison_type: ComparisonType
    baseline: str | None = None


class DerivationSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")

    operation: str = Field(min_length=1)
    metric: str | None = None
    parameters: dict[str, Any] = Field(default_factory=dict)


class RankingSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")

    direction: RankingDirection
    limit: int = Field(ge=1, le=1_000)
    metric: str | None = None


class BusinessQuery(BaseModel):
    model_config = ConfigDict(extra="forbid")

    question: str = Field(min_length=1)
    objective: BusinessObjective
    entities: list[str] = Field(default_factory=list)
    metrics: list[str] = Field(default_factory=list)
    dimensions: list[str] = Field(default_factory=list)
    filters: list[BusinessFilter] = Field(default_factory=list)
    time_expression: str | None = None
    comparison: ComparisonSpec | None = None
    derivations: list[DerivationSpec] = Field(default_factory=list)
    ranking: RankingSpec | None = None
    requested_output: list[RequestedOutput] = Field(default_factory=list)
    ambiguities: list[str] = Field(default_factory=list)
    confidence: float = Field(default=1.0, ge=0.0, le=1.0)

    @model_validator(mode="after")
    def validate_objective_details(self) -> BusinessQuery:
        if self.objective == BusinessObjective.RANKING and self.ranking is None:
            raise ValueError("ranking objective requires ranking details")
        if self.objective == BusinessObjective.COMPARISON and self.comparison is None:
            raise ValueError("comparison objective requires comparison details")
        return self


class SemanticCandidate(BaseModel):
    asset_type: SemanticAssetType
    asset_id: str = Field(min_length=1)
    name: str = Field(min_length=1)
    score: float = Field(ge=0.0, le=1.0)
    datasource_id: str | None = None
    data_object_id: str | None = None
    field_id: str | None = None
    field_path: str | None = None
    relationship_id: str | None = None
    from_data_object_id: str | None = None
    to_data_object_id: str | None = None
    from_field_path: str | None = None
    to_field_path: str | None = None
    #: Native column data type for field candidates, taken from the published graph. The query
    #: generator uses it to compile a read-only query without going back to the graph.
    data_type: str | None = None
    native_type: str | None = None
    #: Stable object locator for DATA_OBJECT candidates: namespace, qualified_name and
    #: object_kind are the values the database driver must compile against. Two objects sharing
    #: ``name=orders`` (public.orders, archive.orders) never collapse because qualified_name and
    #: namespace are carried separately.
    namespace: str | None = None
    qualified_name: str | None = None
    object_kind: str | None = None
    #: Native data object name from the published graph (``GraphDataObject.name``). It is also
    #: populated on FIELD candidates by looking up the owning ``GraphDataObject`` at retrieval
    #: time, so a raw FIELD bound by a filter slot keeps the parent table's native locator.
    data_object_name: str | None = None
    #: Governed aggregation semantics for METRIC candidates. ``None`` means the asset does not
    #: declare one; the planner must then fail closed unless a structured derivation overrides it.
    default_aggregation: AggregateFunction | None = None
    #: Set when the governed DIMENSION asset is designated as the business time axis. The axis is
    #: chosen by this explicit declaration — never by sniffing the data type of whichever
    #: dimensions happen to be grounded — so a question that carries a time expression but does
    #: not spell out the date dimension still resolves deterministically, and two governed time
    #: axes stay ambiguous instead of silently picking one.
    time_axis: bool = False
    reason: str | None = None
    reasons: list[str] = Field(default_factory=list)
    evidence: list[str] = Field(default_factory=list)
    matched_terms: list[str] = Field(default_factory=list)
    matched_phrases: list[str] = Field(default_factory=list)
    labels: list[str] = Field(default_factory=list)
    ambiguity: str | None = None
    ambiguous: bool = False
    trusted: bool = True
    migration_lookup: bool = False


class GroundingBinding(BaseModel):
    business_term: str = Field(min_length=1)
    asset_type: SemanticAssetType
    asset_id: str = Field(min_length=1)
    datasource_id: str | None = None
    data_object_id: str | None = None
    field_path: str | None = None
    relationship_id: str | None = None
    score: float = Field(ge=0.0, le=1.0)
    evidence: list[str] = Field(default_factory=list)
    ambiguous: bool = False
    #: Carried over from the asset: the governed aggregation semantics for METRIC bindings.
    default_aggregation: AggregateFunction | None = None
    #: Carried over from the asset: this DIMENSION binding is the governed business time axis.
    time_axis: bool = False
    data_type: str | None = None
    native_type: str | None = None
    namespace: str | None = None
    qualified_name: str | None = None
    object_kind: str | None = None
    #: Native data object name from the published graph; never ``business_term``.
    data_object_name: str | None = None


class GroundedQuery(BaseModel):
    """The business query bound to confirmed physical assets.

    Every physical reference in it — bindings and allowlists alike — was confirmed against the
    published enterprise data graph. Downstream planning and generation may only use the allowlist.
    ``scan_version`` records the revision of the published graph retrieval ran against; the
    planner carries it into the plan and a future execution validator may use it to refuse running
    a plan against a rescanned graph.
    """

    business_query: BusinessQuery
    candidates: list[SemanticCandidate] = Field(default_factory=list)
    bindings: list[GroundingBinding] = Field(default_factory=list)
    workspace_id: str = Field(default="default", min_length=1)
    requested_datasource_id: str | None = None
    scan_version: int | None = None
    allowed_datasource_ids: list[str] = Field(default_factory=list)
    allowed_data_object_ids: list[str] = Field(default_factory=list)
    allowed_field_paths: list[str] = Field(default_factory=list)
    allowed_relationship_ids: list[str] = Field(default_factory=list)
    unresolved_ambiguities: list[str] = Field(default_factory=list)
    confidence: float = Field(default=1.0, ge=0.0, le=1.0)

    @model_validator(mode="after")
    def validate_binding_allowlist(self) -> GroundedQuery:
        datasource_ids = set(self.allowed_datasource_ids)
        object_ids = set(self.allowed_data_object_ids)
        field_paths = set(self.allowed_field_paths)
        relationship_ids = set(self.allowed_relationship_ids)
        for binding in self.bindings:
            if binding.datasource_id and binding.datasource_id not in datasource_ids:
                raise ValueError("grounding binding datasource is not in the allowlist")
            if binding.data_object_id and binding.data_object_id not in object_ids:
                raise ValueError("grounding binding data object is not in the allowlist")
            if binding.field_path and binding.field_path not in field_paths:
                raise ValueError("grounding binding field is not in the allowlist")
            if binding.relationship_id and binding.relationship_id not in relationship_ids:
                raise ValueError("grounding binding relationship is not in the allowlist")
            if (
                self.requested_datasource_id
                and binding.datasource_id != self.requested_datasource_id
            ):
                raise ValueError("a datasource-scoped grounding may only bind that datasource")
        return self
