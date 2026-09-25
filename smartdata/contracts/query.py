from __future__ import annotations

from datetime import date, datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, model_validator

from smartdata.contracts.datasource import DatasourceKind
from smartdata.contracts.profile import DataObjectProfile, RelationshipProfile
from smartdata.contracts.semantic import (
    AggregateFunction,
    BusinessObjective,
    BusinessQuery,
    DerivationSpec,
    GroundedQuery,
    RankingSpec,
    RequestedOutput,
    SemanticAssetType,
)


class FilterOperator(StrEnum):
    EQUALS = "equals"
    NOT_EQUALS = "not_equals"
    GREATER_THAN = "greater_than"
    GREATER_OR_EQUAL = "greater_or_equal"
    LESS_THAN = "less_than"
    LESS_OR_EQUAL = "less_or_equal"
    IN = "in"
    BETWEEN = "between"
    CONTAINS = "contains"


class SortDirection(StrEnum):
    ASCENDING = "ascending"
    DESCENDING = "descending"


#: Structured derivation operations Phase 1 can express. ``DerivationSpec.operation`` is a structured
#: field produced by intent understanding, not free user text, so reading it is not re-interpreting
#: the question. Anything outside this table is refused instead of guessed.
_AGGREGATE_OPERATIONS: dict[str, AggregateFunction] = {
    "count": AggregateFunction.COUNT,
    "计数": AggregateFunction.COUNT,
    "countrows": AggregateFunction.COUNT,
    "sum": AggregateFunction.SUM,
    "total": AggregateFunction.SUM,
    "求和": AggregateFunction.SUM,
    "合计": AggregateFunction.SUM,
    "总和": AggregateFunction.SUM,
    "average": AggregateFunction.AVERAGE,
    "avg": AggregateFunction.AVERAGE,
    "mean": AggregateFunction.AVERAGE,
    "平均": AggregateFunction.AVERAGE,
    "minimum": AggregateFunction.MINIMUM,
    "min": AggregateFunction.MINIMUM,
    "最小值": AggregateFunction.MINIMUM,
    "最小": AggregateFunction.MINIMUM,
    "maximum": AggregateFunction.MAXIMUM,
    "max": AggregateFunction.MAXIMUM,
    "最大值": AggregateFunction.MAXIMUM,
    "最大": AggregateFunction.MAXIMUM,
}


def aggregate_function_for(operation: str) -> AggregateFunction | None:
    """Map a structured derivation operation to a Phase 1 aggregate, or ``None`` if unsupported."""
    key = "".join(character for character in operation.casefold() if character.isalnum())
    return _AGGREGATE_OPERATIONS.get(key)


class QueryLanguage(StrEnum):
    SQL = "sql"
    CQL = "cql"
    CYPHER = "cypher"
    FLUX = "flux"
    REDIS_JSON = "redis_json"
    MONGODB_JSON = "mongodb_json"
    COUCHDB_MANGO = "couchdb_mango"
    HBASE_JSON = "hbase_json"
    SEARCH_JSON = "search_json"
    VECTOR_FILTER_JSON = "vector_filter_json"
    GRAPHQL = "graphql"


class QueryResultType(StrEnum):
    SCALAR = "scalar"
    TABULAR = "tabular"
    DOCUMENT = "document"
    KEY_VALUE = "key_value"
    WIDE_COLUMN = "wide_column"
    GRAPH = "graph"
    TIME_SERIES = "time_series"
    SEARCH = "search"
    VECTOR = "vector"


class TimeRange(BaseModel):
    start: date | datetime | None = None
    end: date | datetime | None = None

    @model_validator(mode="after")
    def validate_order(self) -> TimeRange:
        if self.start is not None and self.end is not None:
            start = self.start.date() if isinstance(self.start, datetime) else self.start
            end = self.end.date() if isinstance(self.end, datetime) else self.end
            if start > end:
                raise ValueError("time range start cannot be after end")
        return self


class FilterSpec(BaseModel):
    field: str
    operator: FilterOperator
    value: Any


class AggregateSpec(BaseModel):
    function: AggregateFunction
    field: str | None = None
    alias: str | None = None


class SortSpec(BaseModel):
    field: str
    direction: SortDirection = SortDirection.ASCENDING


class QueryIntent(BaseModel):
    question: str
    entities: list[str] = Field(default_factory=list)
    metrics: list[str] = Field(default_factory=list)
    dimensions: list[str] = Field(default_factory=list)
    time_range: TimeRange | None = None
    filters: list[FilterSpec] = Field(default_factory=list)
    aggregates: list[AggregateSpec] = Field(default_factory=list)
    sorts: list[SortSpec] = Field(default_factory=list)
    limit: int | None = Field(default=None, ge=1, le=1_000)
    requested_datasource_id: str | None = None
    ambiguities: list[str] = Field(default_factory=list)
    confidence: float = Field(default=1.0, ge=0.0, le=1.0)


class RetrievedDataObject(BaseModel):
    profile: DataObjectProfile
    score: float = Field(ge=0.0)
    reasons: list[str] = Field(default_factory=list)


class RetrievedDataSource(BaseModel):
    datasource_id: str
    name: str
    kind: DatasourceKind
    driver: str
    data_objects: list[RetrievedDataObject] = Field(default_factory=list, max_length=5)


class QueryContext(BaseModel):
    """Profile-era compatibility context (``CompanyDataProfile`` physical structure).

    The semantic query engine main chain uses :class:`GroundedQueryContext` instead: it is built from
    a ``GroundedQuery`` only, and it can only contain assets the grounding already confirmed. This
    class stays for the legacy pipeline (``QueryPreparationPipeline`` / ``RuleBasedQueryPlanner``)
    until the unified ask pipeline replaces it; new capabilities must not depend on it.
    """

    workspace_id: str
    intent: QueryIntent
    data_sources: list[RetrievedDataSource] = Field(default_factory=list, max_length=3)
    relationships: list[RelationshipProfile] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_field_budget(self) -> QueryContext:
        field_count = sum(
            len(data_object.profile.fields)
            for datasource in self.data_sources
            for data_object in datasource.data_objects
        )
        if field_count > 30:
            raise ValueError("query context cannot contain more than 30 fields")
        return self


class QueryPlan(BaseModel):
    """Profile-era compatibility plan (see :class:`QueryContext`).

    The main chain uses :class:`GroundedQueryPlan`, whose every physical reference is a graph
    identity taken from the grounding allowlist.
    """

    id: str
    datasource_id: str
    query_language: QueryLanguage
    data_object_ids: list[str] = Field(min_length=1)
    output_fields: list[str] = Field(default_factory=list)
    metrics: list[str] = Field(default_factory=list)
    dimensions: list[str] = Field(default_factory=list)
    relationship_ids: list[str] = Field(default_factory=list)
    filters: list[FilterSpec] = Field(default_factory=list)
    time_range: TimeRange | None = None
    time_field: str | None = None
    aggregates: list[AggregateSpec] = Field(default_factory=list)
    group_by: list[str] = Field(default_factory=list)
    sorts: list[SortSpec] = Field(default_factory=list)
    limit: int = Field(default=200, ge=1, le=1_000)
    expected_result_type: QueryResultType
    rationale: list[str] = Field(default_factory=list)
    unresolved_ambiguities: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_time_constraint(self) -> QueryPlan:
        if (self.time_range is None) != (self.time_field is None):
            raise ValueError("time_range and time_field must be provided together")
        return self


class GeneratedQuery(BaseModel):
    datasource_id: str
    language: QueryLanguage
    command: str | dict[str, Any]
    display_command: str | dict[str, Any]


class PreparedQuery(BaseModel):
    intent: QueryIntent
    context: QueryContext
    plan: QueryPlan
    generated_query: GeneratedQuery


class QueryTrace(BaseModel):
    trace_id: str | None = None
    intent: QueryIntent | None = None
    business_query: BusinessQuery | None = None
    grounded_query: GroundedQuery | None = None
    retrieved_datasource_ids: list[str] = Field(default_factory=list)
    retrieved_data_object_ids: list[str] = Field(default_factory=list)
    retrieved_field_paths: list[str] = Field(default_factory=list)
    retrieved_relationship_ids: list[str] = Field(default_factory=list)
    plan: QueryPlan
    executed_query: str | dict[str, Any]
    display_query: str | dict[str, Any]
    duration_ms: float = Field(ge=0)
    truncated: bool = False
    warnings: list[str] = Field(default_factory=list)
    result_summary: dict[str, Any] | None = None

    @model_validator(mode="after")
    def validate_query_source(self) -> QueryTrace:
        if self.intent is None and self.business_query is None:
            raise ValueError("query trace requires intent or business_query")
        if (
            self.grounded_query
            and self.business_query
            and self.grounded_query.business_query != self.business_query
        ):
            raise ValueError("grounded query must reference the traced business query")
        return self


# --------------------------------------------------------------------------------------------------
# Grounded query engine contracts (P1-04C).
#
# ``GroundedQueryContext`` is the formal QueryContext of the semantic query engine main chain and
# ``GroundedQueryPlan`` is its formal QueryPlan. Both are downstream of Grounding(语义落地): every
# physical reference they carry comes from the grounding allowlist, and nothing in them is a business
# expression waiting to be resolved. Planning therefore never reads the enterprise database, the
# graph, ``CompanyDataProfile`` or a model.
# --------------------------------------------------------------------------------------------------


class GroundedFieldRef(BaseModel):
    """One physical column, identified by the graph's identity instead of by a readable name.

    ``datasource_id`` + ``data_object_id`` + ``field_path`` are the identity. ``field_id`` is the
    graph's stable field identity and is carried as evidence when the grounding resolved one.
    ``business_term`` is the business expression the column was bound from, and it is kept for logs
    and debugging only: no planner or generator may ever use it to look the column up again.

    ``data_type`` / ``native_type`` carry the column's declared data type from the published graph;
    the query generator uses them to compile read-only native queries against the actual database.
    """

    model_config = ConfigDict(extra="forbid")

    datasource_id: str = Field(min_length=1)
    data_object_id: str = Field(min_length=1)
    field_path: str = Field(min_length=1)
    field_id: str | None = None
    business_term: str | None = None
    data_type: str | None = None
    native_type: str | None = None

    def identity(self) -> tuple[str, str, str]:
        return (self.datasource_id, self.data_object_id, self.field_path)

    def is_date_type(self) -> bool:
        """Treat a column as a date/time axis only when its declared graph type matches.

        Phase 1's ``TimeRange`` carries date / datetime boundaries, so the time axis only accepts
        ``date`` / ``datetime`` / ``timestamp`` / ``timestamptz``. A bare ``time`` column (a
        within-day time of day, no date component) cannot be used as a time range axis — reading
        must include ``is_date_type() is True`` and the builder must fail closed otherwise.
        ``None`` and any other declared type are also rejected.
        """
        if self.data_type is None:
            return False
        normalized = self.data_type.strip().casefold()
        return normalized in {"date", "datetime", "timestamp", "timestamptz"}


class GroundedDataObjectRef(BaseModel):
    """One physical data object with everything the query generator needs to compile a native query.

    ``data_object_id`` is the graph's stable identity and remains the authoritative reference;
    ``namespace`` / ``qualified_name`` / ``object_kind`` are what the generator hands to the
    underlying database driver. ``public.orders`` and ``archive.orders`` therefore carry
    different ``qualified_name`` values, so the generator cannot accidentally target the wrong
    table.

    ``name`` is the data object's **native** name, taken directly from the published graph's
    ``GraphDataObject.name`` (for example ``orders`` — never the business term ``销售额`` and
    never ``qualified_name``). The query generator compiles against ``qualified_name`` and
    ``namespace``; ``name`` is the human-readable identifier the database driver reports for the
    resolved table or collection. It is ``None`` only on the legacy profile path; a formal
    graph-backed plan that resolves to ``name=None`` is refused by the P1-04C2 execution
    validator rather than guessed.
    """

    model_config = ConfigDict(extra="forbid")

    datasource_id: str = Field(min_length=1)
    data_object_id: str = Field(min_length=1)
    #: Native object name (``GraphDataObject.name``). ``None`` only on the legacy compatibility
    #: path; the formal graph-backed path always populates it from the published graph.
    name: str | None = Field(default=None, min_length=1)
    namespace: str | None = None
    qualified_name: str | None = None
    object_kind: str | None = None

    def identity(self) -> tuple[str, str]:
        return (self.datasource_id, self.data_object_id)

    def full_identity(self) -> tuple[str, str, str | None, str | None, str | None, str | None]:
        """The complete native locator the executor compiles against.

        Includes the native ``name`` so the validator can refuse a plan whose locator disagrees
        with the grounded context on anything other than the graph's stable identity.
        """
        return (
            self.datasource_id,
            self.data_object_id,
            self.name,
            self.namespace,
            self.qualified_name,
            self.object_kind,
        )


class TimeSpec(BaseModel):
    """The structured business time axis a query cares about.

    ``TimeSpec`` carries exactly one piece of information: which already-grounded business
    dimension is the time axis. The natural-language time phrase is owned by
    ``BusinessQuery.time_expression`` (the intent layer), and the normalised date boundaries are
    owned by ``TimeRange``. Having two natural-language sources for the same time expression
    created ambiguous inputs, so the contract is converged to ``dimension`` only and the
    builder refuses the context unless the bound field is a date/datetime column.

    The builder resolves ``dimension`` to a bound physical field by walking the grounding
    bindings: a caller that hands the builder a literal field name instead of a business
    dimension is rejected at the contract boundary.
    """

    model_config = ConfigDict(extra="forbid")

    dimension: str = Field(min_length=1)


class QueryContextBinding(BaseModel):
    """One grounding binding as the query context carries it, with its confirmed physical identity.

    A relationship binding carries no single data object: its endpoints live in the context's
    confirmed relationships. ``default_aggregation`` is the governed aggregation semantics of a
    metric asset (``None`` for non-metric bindings). The planner uses it as the binding's
    authoritative aggregation rule unless a structured ``DerivationSpec`` overrides it.

    ``namespace`` / ``qualified_name`` / ``object_kind`` describe the bound data object's native
    database identity. They are copied from retrieval so the generator can target the correct
    table — for example ``public.orders`` vs ``archive.orders`` — without going back to the graph.
    ``data_object_name`` is the data object's native name from the published graph and is what
    the executor compiles against (``orders`` for the database driver), distinct from
    ``business_term`` (the user's word for it).
    """

    model_config = ConfigDict(extra="forbid")

    business_term: str = Field(min_length=1)
    asset_type: SemanticAssetType
    asset_id: str = Field(min_length=1)
    datasource_id: str = Field(min_length=1)
    data_object_id: str | None = None
    field_id: str | None = None
    field_path: str | None = None
    relationship_id: str | None = None
    score: float = Field(ge=0.0, le=1.0)
    evidence: list[str] = Field(default_factory=list)
    default_aggregation: AggregateFunction | None = None
    data_type: str | None = None
    native_type: str | None = None
    namespace: str | None = None
    qualified_name: str | None = None
    object_kind: str | None = None
    data_object_name: str | None = None

    @model_validator(mode="after")
    def validate_physical_identity(self) -> QueryContextBinding:
        if self.asset_type is SemanticAssetType.RELATIONSHIP:
            if not self.relationship_id:
                raise ValueError("relationship binding requires a relationship_id")
            return self
        if not self.data_object_id:
            raise ValueError("binding requires a physical data object")
        if self.asset_type in {
            SemanticAssetType.METRIC,
            SemanticAssetType.DIMENSION,
            SemanticAssetType.FIELD,
        } and not self.field_path:
            raise ValueError("metric/dimension/field binding requires a physical field path")
        return self

    def field_ref(self) -> GroundedFieldRef | None:
        if not self.field_path:
            return None
        return GroundedFieldRef(
            datasource_id=self.datasource_id,
            data_object_id=self.data_object_id,
            field_path=self.field_path,
            field_id=self.field_id,
            business_term=self.business_term,
            data_type=self.data_type,
            native_type=self.native_type,
        )


class QueryContextFilter(BaseModel):
    """A filter whose subject is already bound, carrying its business value verbatim.

    The value is business data, not a physical asset: it never enters an allowlist and is never
    checked against the database during planning.
    """

    model_config = ConfigDict(extra="forbid")

    subject: str = Field(min_length=1)
    field: GroundedFieldRef
    operator: FilterOperator
    value: Any


class QueryContextRelationship(BaseModel):
    """A confirmed, graph-published relationship available to the plan.

    Both join keys are required: a relationship whose columns are unknown cannot be joined
    deterministically, so it fails closed instead of guessing.
    """

    model_config = ConfigDict(extra="forbid")

    relationship_id: str = Field(min_length=1)
    relationship_type: str | None = None
    datasource_id: str = Field(min_length=1)
    from_data_object_id: str = Field(min_length=1)
    from_field_path: str = Field(min_length=1)
    to_data_object_id: str = Field(min_length=1)
    to_field_path: str = Field(min_length=1)
    evidence: list[str] = Field(default_factory=list)


class GroundedQueryContext(BaseModel):
    """Formal QueryContext: what planning is allowed to see, and nothing else.

    Built only from a ``GroundedQuery``. It cannot carry unresolved ambiguity, and every physical
    asset in it — including the allowlists — comes from a successful grounding binding.

    ``data_objects`` carries the full physical locator of every data object the plan may touch;
    ``scan_version`` records the version of the published enterprise data graph that produced
    these bindings; ``time_spec`` records the user-facing time phrase and business dimension the
    ``time_field`` was bound from, so the planner and a future execution validator can confirm that
    the time axis is grounded, not caller-named.
    """

    model_config = ConfigDict(extra="forbid")

    workspace_id: str = Field(min_length=1)
    requested_datasource_id: str | None = None
    objective: BusinessObjective
    business_query: BusinessQuery
    bindings: list[QueryContextBinding] = Field(min_length=1)
    metrics: list[QueryContextBinding] = Field(default_factory=list)
    dimensions: list[QueryContextBinding] = Field(default_factory=list)
    entities: list[QueryContextBinding] = Field(default_factory=list)
    filters: list[QueryContextFilter] = Field(default_factory=list)
    ranking: RankingSpec | None = None
    derivations: list[DerivationSpec] = Field(default_factory=list)
    time_expression: str | None = None
    time_range: TimeRange | None = None
    time_spec: TimeSpec | None = None
    time_field: GroundedFieldRef | None = None
    requested_output: list[RequestedOutput] = Field(default_factory=list)
    relationships: list[QueryContextRelationship] = Field(default_factory=list)
    data_objects: dict[str, GroundedDataObjectRef] = Field(default_factory=dict)
    scan_version: int | None = None
    allowed_datasource_ids: list[str] = Field(min_length=1)
    allowed_data_object_ids: list[str] = Field(min_length=1)
    allowed_field_paths: list[str] = Field(default_factory=list)
    allowed_field_ids: list[str] = Field(default_factory=list)
    allowed_relationship_ids: list[str] = Field(default_factory=list)
    unresolved_ambiguities: list[str] = Field(default_factory=list)
    confidence: float = Field(default=0.0, ge=0.0, le=1.0)

    @model_validator(mode="after")
    def validate_grounded_only(self) -> GroundedQueryContext:
        """The context is an executable one: no ambiguity, no unbound physical asset."""
        if self.unresolved_ambiguities:
            raise ValueError(
                "an executable query context cannot carry unresolved ambiguities: "
                f"{self.unresolved_ambiguities}"
            )
        if (self.time_range is None) != (self.time_field is None):
            raise ValueError("time_range and time_field must be provided together")
        if self.time_spec is not None and self.time_field is None:
            raise ValueError("a time_spec is only valid when time_field is grounded")
        if self.time_field is not None and not self.time_field.is_date_type():
            raise ValueError(
                f"time field is not a date/datetime column: {self.time_field.identity()}"
            )
        datasource_ids = set(self.allowed_datasource_ids)
        object_ids = set(self.allowed_data_object_ids)
        field_paths = set(self.allowed_field_paths)
        field_ids = set(self.allowed_field_ids)
        relationship_ids = set(self.allowed_relationship_ids)
        for binding in self.bindings:
            if binding.datasource_id not in datasource_ids:
                raise ValueError("binding datasource is not in the context allowlist")
            if binding.data_object_id and binding.data_object_id not in object_ids:
                raise ValueError("binding data object is not in the context allowlist")
            if binding.field_path and binding.field_path not in field_paths:
                raise ValueError("binding field is not in the context allowlist")
            if binding.field_id and binding.field_id not in field_ids:
                raise ValueError("binding field identity is not in the context allowlist")
            if binding.relationship_id and binding.relationship_id not in relationship_ids:
                raise ValueError("binding relationship is not in the context allowlist")
        if self.requested_datasource_id and (
            set(self.allowed_datasource_ids) != {self.requested_datasource_id}
        ):
            raise ValueError("a scoped query context may only contain the requested datasource")
        for object_id, locator in self.data_objects.items():
            if locator.data_object_id != object_id:
                raise ValueError("data_objects key must equal the locator's data_object_id")
            if locator.datasource_id not in datasource_ids:
                raise ValueError("data_objects carries a datasource outside the allowlist")
        return self

    def field_identity(self) -> dict[tuple[str, str, str], str | None]:
        """Allowed physical field identities, keyed by (datasource, data object, field path)."""
        return {
            binding.field_ref().identity(): binding.field_id
            for binding in self.bindings
            if binding.field_ref() is not None
        }


class PlanAggregate(BaseModel):
    """One aggregate of the plan. Only ``count`` may aggregate without a physical field."""

    model_config = ConfigDict(extra="forbid")

    function: AggregateFunction
    field: GroundedFieldRef | None = None
    alias: str = Field(min_length=1)

    @model_validator(mode="after")
    def validate_field_presence(self) -> PlanAggregate:
        if self.function is not AggregateFunction.COUNT and self.field is None:
            raise ValueError("only count may aggregate without a physical field")
        return self


class PlanFilter(BaseModel):
    """One predicate of the plan, on a bound field, with its business value verbatim."""

    model_config = ConfigDict(extra="forbid")

    field: GroundedFieldRef
    operator: FilterOperator
    value: Any


class PlanSortTarget(StrEnum):
    FIELD = "field"
    AGGREGATE = "aggregate"


class PlanSort(BaseModel):
    """Sorting by a plan aggregate (ranking) or by a projected field, never by a business word."""

    model_config = ConfigDict(extra="forbid")

    target: PlanSortTarget
    direction: SortDirection = SortDirection.ASCENDING
    aggregate_alias: str | None = None
    field: GroundedFieldRef | None = None

    @model_validator(mode="after")
    def validate_target(self) -> PlanSort:
        if self.target is PlanSortTarget.AGGREGATE:
            if not self.aggregate_alias or self.field is not None:
                raise ValueError("an aggregate sort requires aggregate_alias only")
            return self
        if self.field is None or self.aggregate_alias is not None:
            raise ValueError("a field sort requires field only")
        return self


class PlanJoin(BaseModel):
    """A direct join of two bound objects over one confirmed relationship.

    Multi-hop, cross-datasource and inferred joins are out of Phase 1 scope: the keys must be the
    confirmed relationship's own keys, which is verified against the context.
    """

    model_config = ConfigDict(extra="forbid")

    datasource_id: str = Field(min_length=1)
    relationship_id: str = Field(min_length=1)
    relationship_type: str | None = None
    from_data_object_id: str = Field(min_length=1)
    from_field_path: str = Field(min_length=1)
    to_data_object_id: str = Field(min_length=1)
    to_field_path: str = Field(min_length=1)


class GroundedQueryPlan(BaseModel):
    """Formal QueryPlan: how to query, expressed with graph identities instead of SQL text.

    The plan carries no native query text and no query language, so a write intent cannot enter it
    structurally; compilation to a read-only native query belongs to the query generator, which must
    not add physical assets either.

    ``data_objects`` carries the full physical locator of every data object the plan may touch so
    the generator does not have to call back into the graph; ``scan_version`` records the graph
    revision the plan was built from and is reserved for the future execution-time consistency
    check.
    """

    model_config = ConfigDict(extra="forbid")

    plan_id: str = Field(min_length=1)
    workspace_id: str = Field(min_length=1)
    datasource_id: str = Field(min_length=1)
    data_object_ids: list[str] = Field(min_length=1)
    data_objects: dict[str, GroundedDataObjectRef] = Field(default_factory=dict)
    scan_version: int | None = None
    aggregates: list[PlanAggregate] = Field(default_factory=list)
    selected_fields: list[GroundedFieldRef] = Field(default_factory=list)
    filters: list[PlanFilter] = Field(default_factory=list)
    group_by: list[GroundedFieldRef] = Field(default_factory=list)
    sorts: list[PlanSort] = Field(default_factory=list)
    limit: int = Field(default=200, ge=1, le=1_000)
    joins: list[PlanJoin] = Field(default_factory=list)
    time_range: TimeRange | None = None
    time_field: GroundedFieldRef | None = None
    expected_result_type: QueryResultType
    rationale: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_plan_shape(self) -> GroundedQueryPlan:
        if (self.time_range is None) != (self.time_field is None):
            raise ValueError("time_range and time_field must be provided together")
        if not self.aggregates and not self.selected_fields:
            raise ValueError("a plan requires at least one aggregate or one selected field")
        if len(set(self.data_object_ids)) != len(self.data_object_ids):
            raise ValueError("plan data object ids must be unique")
        if len(self.data_object_ids) > 1 and not self.joins:
            raise ValueError("a plan spanning several data objects requires a confirmed join")
        selected = {ref.identity() for ref in self.selected_fields}
        for ref in self.group_by:
            if ref.identity() not in selected:
                raise ValueError("group by fields must be part of the selected fields")
        for object_id, locator in self.data_objects.items():
            if locator.data_object_id != object_id:
                raise ValueError("plan data_objects key must equal the locator's data_object_id")
            if locator.datasource_id != self.datasource_id:
                raise ValueError("plan data_objects carries a foreign datasource")
        return self


class NativeQuery(BaseModel):
    """A generated native command whose business values remain driver-bound parameters."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    datasource_id: str = Field(min_length=1)
    plan_id: str = Field(min_length=1)
    scan_version: int | None = None
    query_language: QueryLanguage
    command: str | dict[str, Any]
    parameters: tuple[Any, ...] = ()
    display_command: str = Field(min_length=1)


class GroundedQueryResult(BaseModel):
    """Typed execution result for the post-grounding query path."""

    model_config = ConfigDict(extra="forbid")

    plan_id: str = Field(min_length=1)
    datasource_id: str = Field(min_length=1)
    query_language: QueryLanguage
    columns: list[str]
    rows: list[dict[str, Any]]
    row_count: int = Field(ge=0)
    truncated: bool = False
    scan_version: int | None = None


class ExecutionEvidence(BaseModel):
    """Non-sensitive evidence safe for traces and later answer generation."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    plan_id: str = Field(min_length=1)
    datasource_id: str = Field(min_length=1)
    scan_version: int | None = None
    query_language: QueryLanguage
    display_command: str = Field(min_length=1)
    row_count: int = Field(ge=0)
    truncated: bool = False


class GroundedExecution(BaseModel):
    model_config = ConfigDict(extra="forbid")

    result: GroundedQueryResult
    evidence: ExecutionEvidence
