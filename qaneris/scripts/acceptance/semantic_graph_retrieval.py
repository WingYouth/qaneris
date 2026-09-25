"""P1-04A / P1-04B / P1-04C1 acceptance: recall, ground and plan over a real Neo4j data graph.

Run against a Catalog whose datasources have already been scanned and published:

    python -m qaneris.scripts.acceptance.semantic_graph_retrieval --catalog <path> \
        [--datasource <datasource_id>] [--limit 20]

The CompanyDataProfile(企业数据画像) is used only as the reference for the sample-leakage check.
Physical retrieval reads the published graph through ``Neo4jGraphReader`` and never touches the
profile or any database adapter. Grounding binds the query's business expressions to those confirmed
candidates only, and must stop at a clarification when a business term is ambiguous. Planning then
consumes the grounded context alone: it reads no database, graph, profile or model, and every
physical reference of the plan must come from the grounding allowlist.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from qaneris.catalog import Catalog
from qaneris.common.errors import QueryContextBuildError, QueryPlanningError
from qaneris.contracts import (
    BusinessFilter,
    BusinessQuery,
    RankingDirection,
    RankingSpec,
    SemanticAssetType,
)
from qaneris.contracts.query import (
    GroundedQueryContext,
    GroundedQueryPlan,
)
from qaneris.graph import Neo4jConfig, Neo4jGraphReader
from qaneris.graph.reading import GraphStructure, GraphStructureRequest
from qaneris.querying import GroundedPlanValidator, GroundedQueryPlanner, QueryContextBuilder
from qaneris.semantic import (
    GraphSemanticRetriever,
    GroundingResult,
    SemanticAssetStatus,
    SemanticGrounder,
    SemanticRetrievalResult,
    SQLiteSemanticAssetRegistry,
)


def business_questions() -> list[tuple[str, BusinessQuery, str, dict[str, object]]]:
    """Fixed questions with the grounding outcome each one must reach.

    Each tuple carries a label, a :class:`BusinessQuery`, the expected grounding outcome
    (``"bound"`` or ``"clarification"``) and a planning directive for the new P1-04C1 stage.
    The directive tells the acceptance script what to do after grounding succeeds:

    * ``"plan"`` — build the executable context, plan it and run the validator.
    * ``"context_error"`` — the context builder must refuse with ``QueryContextBuildError``
      because the grounding left an ambiguity that blocks execution.
    """
    return [
        (
            "按客户看销售额（指标+维度）",
            BusinessQuery(
                question="按客户看实收销售额",
                objective="lookup",
                metrics=["实收销售额"],
                dimensions=["客户名称"],
            ),
            "bound",
            {"mode": "plan", "scenario": "metric+dimension+join"},
        ),
        (
            "销售额排名（ranking）",
            BusinessQuery(
                question="销售额排名",
                objective="ranking",
                metrics=["实收销售额"],
                ranking=RankingSpec(direction=RankingDirection.TOP, limit=10, metric="实收销售额"),
            ),
            "bound",
            {"mode": "plan", "scenario": "ranking"},
        ),
        (
            "对客户 A 的销售额（过滤）",
            BusinessQuery(
                question="客户A的销售额",
                objective="lookup",
                metrics=["实收销售额"],
                filters=[
                    BusinessFilter(subject="客户名称", operator="equals", value="客户A")
                ],
            ),
            "bound",
            {"mode": "plan", "scenario": "filter"},
        ),
        (
            "销售额（无可执行落地）",
            BusinessQuery(
                question="非受治理指标：未来预测销售",
                objective="lookup",
                metrics=["未来预测销售"],
            ),
            "clarification",
            {"mode": "context_error"},
        ),
    ]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--catalog", required=True, type=Path, help="Acceptance Catalog path")
    parser.add_argument("--datasource", default=None, help="Restrict retrieval to one datasource")
    parser.add_argument("--limit", type=int, default=20, help="Candidate limit per question")
    return parser.parse_args()


def print_candidates(result: SemanticRetrievalResult) -> None:
    if not result.candidates:
        print("  (no candidate)")
    for candidate in result.candidates:
        print(
            f"  - {candidate.asset_type.value}: {candidate.name} "
            f"score={candidate.score:.6f} datasource={candidate.datasource_id} "
            f"object={candidate.data_object_id} field_id={candidate.field_id} "
            f"field={candidate.field_path} relationship={candidate.relationship_id} "
            f"trusted={candidate.trusted} ambiguous={candidate.ambiguous} "
            f"migration_lookup={candidate.migration_lookup}"
        )
    for warning in result.warnings:
        print(f"  warning: {warning}")


def physical_correctness(structure: GraphStructure, result: SemanticRetrievalResult) -> list[str]:
    """Every returned physical reference must exist in the graph read for the same candidate."""
    object_ids = {item.node_id for item in structure.data_objects}
    field_ids = {item.node_id for item in structure.fields}
    relationship_ids = {item.relationship_id for item in structure.relationships}
    failures: list[str] = []
    for candidate in result.candidates:
        if candidate.data_object_id and candidate.data_object_id not in object_ids:
            failures.append(f"{candidate.asset_id}: data object absent from graph")
        if candidate.field_id and candidate.field_id not in field_ids:
            failures.append(f"{candidate.asset_id}: graph field id absent from graph")
        if candidate.relationship_id and candidate.relationship_id not in relationship_ids:
            failures.append(f"{candidate.asset_id}: relationship absent from graph")
        if candidate.field_path and candidate.data_object_id:
            exists = any(
                item.object_id == candidate.data_object_id and item.path == candidate.field_path
                for item in structure.fields
            )
            if not exists:
                failures.append(f"{candidate.asset_id}: field absent from graph")
    return failures


def identity_failures(results: list[SemanticRetrievalResult]) -> list[str]:
    """Semantic candidates that still rely on a readable-name lookup as their physical identity."""
    offenders = sorted(
        {
            candidate.asset_id
            for result in results
            for candidate in result.candidates
            if candidate.asset_type
            in {
                SemanticAssetType.METRIC,
                SemanticAssetType.DIMENSION,
                SemanticAssetType.BUSINESS_TERM,
            }
            and candidate.migration_lookup
        }
    )
    return [
        f"{asset_id}: still resolved by name, no graph physical identity" for asset_id in offenders
    ]


def sample_fragments(catalog: Catalog) -> list[str]:
    profile = catalog.get_company_data_profile()
    fragments: list[str] = []
    for source in profile.data_sources:
        for namespace in source.namespaces:
            for data_object in namespace.data_objects:
                for preview in data_object.previews:
                    fragments.append(json.dumps(preview.values, ensure_ascii=False, sort_keys=True))
                for field in data_object.fields:
                    if field.sample_values:
                        fragments.append(json.dumps(field.sample_values, ensure_ascii=False))
    return [item for item in fragments if item not in {"[]", "{}"}]


def print_grounding(grounding: GroundingResult) -> None:
    query = grounding.grounded_query
    print(
        f"  resolved: {len(query.bindings)} binding(s), "
        f"{len(grounding.clarifications)} clarification(s)"
    )
    for binding in query.bindings:
        print(
            f"  - binding: {binding.business_term} -> {binding.asset_type.value} "
            f"{binding.asset_id} object={binding.data_object_id} field={binding.field_path} "
            f"relationship={binding.relationship_id} score={binding.score:.6f}"
        )
    print(
        "  allowlist: "
        f"datasources={query.allowed_datasource_ids} objects={query.allowed_data_object_ids} "
        f"fields={query.allowed_field_paths} relationships={query.allowed_relationship_ids}"
    )
    for clarification in grounding.clarifications:
        options = ", ".join(option.label for option in clarification.options)
        print(f"  clarification: {clarification.question} [{options}]")
    for problem in query.unresolved_ambiguities:
        print(f"  unresolved: {problem}")


def grounding_outcome(
    grounding: GroundingResult, expectation: str, label: str
) -> tuple[bool, list[str]]:
    query = grounding.grounded_query
    failures: list[str] = []
    if expectation == "bound":
        if not grounding.is_executable:
            failures.append(f"{label}: grounding is not executable")
        bound_fields = {
            binding.field_path for binding in query.bindings if binding.field_path
        }
        bound_relationships = {
            binding.relationship_id
            for binding in query.bindings
            if binding.relationship_id
        }
        if not bound_fields:
            failures.append(f"{label}: expected a metric and/or a dimension binding")
        if set(query.allowed_field_paths) != bound_fields:
            failures.append(f"{label}: allowlist does not match the bound fields")
        if "gross_amount" in query.allowed_field_paths:
            failures.append(f"{label}: an unbound retrieval candidate reached the allowlist")
        if not bound_fields and not bound_relationships:
            failures.append(f"{label}: grounding did not bind any physical asset")
        if grounding.needs_clarification:
            failures.append(f"{label}: unexpected clarification for a unique binding")
    elif expectation == "clarification":
        if grounding.is_executable:
            failures.append(f"{label}: an ambiguous grounding must not be executable")
        if not grounding.grounded_query.unresolved_ambiguities and not grounding.clarifications:
            failures.append(
                f"{label}: grounding left no executable answer and reported no reason"
            )
    return not failures, failures


def print_plan(plan: GroundedQueryPlan) -> None:
    print(
        f"  plan {plan.plan_id}: datasource={plan.datasource_id} objects={plan.data_object_ids} "
        f"result={plan.expected_result_type.value} limit={plan.limit}"
    )
    for aggregate in plan.aggregates:
        field = aggregate.field.field_path if aggregate.field else "(no field)"
        print(f"  - aggregate: {aggregate.function.value}({field}) as {aggregate.alias}")
    for item in plan.group_by:
        print(f"  - group by: {item.field_path} field_id={item.field_id}")
    for item in plan.filters:
        print(f"  - filter: {item.field.field_path} {item.operator.value} <business value>")
    for item in plan.sorts:
        target = item.aggregate_alias or (item.field.field_path if item.field else "?")
        print(f"  - sort: {target} {item.direction.value}")
    for join in plan.joins:
        print(f"  - join: {join.relationship_id} {join.from_field_path} -> {join.to_field_path}")
    if plan.time_range is not None:
        print(f"  - time range: {plan.time_range.start} .. {plan.time_range.end}")
    for line in plan.rationale:
        print(f"  rationale: {line}")


def plan_physical_correctness(
    context: GroundedQueryContext, plan: GroundedQueryPlan, label: str
) -> list[str]:
    """Every physical reference of the plan must come from the grounding allowlist."""
    failures: list[str] = []
    if not set(plan.data_object_ids) <= set(context.allowed_data_object_ids):
        failures.append(f"{label}: plan names a data object outside the grounding allowlist")
    if plan.datasource_id not in context.allowed_datasource_ids:
        failures.append(f"{label}: plan names a datasource outside the grounding allowlist")
    used = {item.field_path for item in plan.selected_fields}
    used |= {item.field_path for item in plan.group_by}
    used |= {item.field.field_path for item in plan.filters}
    used |= {item.field.field_path for item in plan.aggregates if item.field}
    if plan.time_field is not None:
        used.add(plan.time_field.field_path)
    unauthorized = sorted(used - set(context.allowed_field_paths))
    if unauthorized:
        failures.append(f"{label}: plan names fields outside the grounding allowlist: {unauthorized}")
    joins = {join.relationship_id for join in plan.joins}
    if not joins <= set(context.allowed_relationship_ids):
        failures.append(f"{label}: plan names a relationship outside the grounding allowlist")
    if "gross_amount" in used:
        failures.append(f"{label}: an unbound retrieval candidate reached the plan")
    return failures


def planning_outcome(
    directive: dict[str, object],
    grounding: GroundingResult,
    context: GroundedQueryContext,
    plan: GroundedQueryPlan,
    label: str,
) -> list[str]:
    """Every plan that the context allowed for must stay inside the grounding allowlist.

    The scenarios mirror the test cases in ``tests/unit/querying/test_grounded_plan.py``: a
    metric+dimension+join plan must carry exactly one ``sum`` aggregate, the dimension grouping and
    one confirmed relationship join; the ranking plan must sort by an aggregate alias that is in
    the plan; the filter plan must carry a single predicate on the bound dimension.
    """
    failures: list[str] = []
    scenario = directive.get("scenario")
    aggregates = [item for item in plan.aggregates if item.field is not None]
    if not aggregates:
        failures.append(f"{label}: plan has no metric aggregate")
    for aggregate in aggregates:
        if aggregate.function.value not in {"sum", "count", "average", "minimum", "maximum"}:
            failures.append(f"{label}: plan aggregate function is out of Phase 1: {aggregate.function}")
        if aggregate.field.field_path not in context.allowed_field_paths:
            failures.append(f"{label}: aggregate field outside allowlist: {aggregate.field.field_path}")
    if scenario == "metric+dimension+join":
        if plan.expected_result_type.value != "tabular":
            failures.append(f"{label}: metric+dimension plan should be tabular")
        if not plan.group_by:
            failures.append(f"{label}: metric+dimension plan must group by a dimension")
        if len(plan.joins) != 1:
            failures.append(f"{label}: metric+dimension+join plan must declare exactly one confirmed join")
        elif plan.joins[0].relationship_id not in context.allowed_relationship_ids:
            failures.append(f"{label}: join uses a relationship outside the allowlist")
        if any(aggregate.function.value != "sum" for aggregate in aggregates):
            failures.append(f"{label}: default metric aggregation must be sum")
    elif scenario == "ranking":
        aggregate_sorts = [
            item for item in plan.sorts if item.target.value == "aggregate"
        ]
        if len(aggregate_sorts) != 1:
            failures.append(f"{label}: ranking plan must sort by exactly one aggregate")
        elif aggregate_sorts[0].direction.value != "descending":
            failures.append(f"{label}: ranking TOP must sort descending")
        if plan.limit != 10:
            failures.append(f"{label}: ranking limit should come from ranking spec verbatim")
        aggregate_aliases = {aggregate.alias for aggregate in aggregates}
        if aggregate_sorts and aggregate_sorts[0].aggregate_alias not in aggregate_aliases:
            failures.append(
                f"{label}: sort alias does not match any aggregate alias"
            )
    elif scenario == "filter":
        if not plan.filters:
            failures.append(f"{label}: filter plan must carry at least one predicate")
        else:
            for item in plan.filters:
                if item.field.field_path not in context.allowed_field_paths:
                    failures.append(
                        f"{label}: filter field outside allowlist: {item.field.field_path}"
                    )
    return failures


def planning_block(
    directive: dict[str, object],
    grounding: GroundingResult,
    context_builder: QueryContextBuilder,
    planner: GroundedQueryPlanner,
    validator: GroundedPlanValidator,
    label: str,
) -> tuple[GroundedQueryContext | None, GroundedQueryPlan | None, list[str]]:
    """Build the executable context, plan and validate. Returns ``(context, plan, failures)``.

    A directive with ``mode == "context_error"`` exercises the fail-closed path of the context
    builder: an ambiguous grounding must surface a :class:`QueryContextBuildError` instead of a plan.
    """
    failures: list[str] = []
    mode = directive.get("mode")
    if mode == "context_error":
        try:
            context_builder.build(grounding)
        except QueryContextBuildError as error:
            print(f"  context error (expected): {error}")
        else:
            failures.append(f"{label}: ambiguous grounding produced an executable context")
        return None, None, failures

    context = context_builder.build(grounding)
    print(
        f"  context: workspace={context.workspace_id} datasource={context.allowed_datasource_ids} "
        f"objects={context.allowed_data_object_ids} fields={context.allowed_field_paths}"
    )
    plan = planner.plan(context)
    validator.validate(plan, context)
    repeated = planner.plan(context)
    repeated_validation = repeated.model_dump_json() == plan.model_dump_json()
    if not repeated_validation:
        failures.append(f"{label}: planner is not deterministic across identical contexts")
    print_plan(plan)
    failures.extend(planning_outcome(directive, grounding, context, plan, label))
    failures.extend(plan_physical_correctness(context, plan, label))
    return context, plan, failures


def main() -> int:
    args = parse_args()
    catalog_path = args.catalog.expanduser().resolve()
    if not catalog_path.is_file():
        print(f"[FAIL] Catalog file does not exist: {catalog_path}")
        return 2

    config = Neo4jConfig.from_environment()
    reader = Neo4jGraphReader(
        config.uri, config.username, config.password, database=config.database
    )
    catalog = Catalog(str(catalog_path))
    registry = SQLiteSemanticAssetRegistry(catalog_path)
    retriever = GraphSemanticRetriever(reader, registry)
    grounder = SemanticGrounder()
    context_builder = QueryContextBuilder()
    planner = GroundedQueryPlanner()
    validator = GroundedPlanValidator()

    try:
        structure = reader.read_structure(
            GraphStructureRequest(datasource_id=args.datasource)
        )
        trusted = [
            asset
            for asset in registry.list()
            if asset.status in {SemanticAssetStatus.APPROVED, SemanticAssetStatus.PUBLISHED}
        ]
        print(f"Catalog: {catalog_path}")
        print(f"Neo4j database: {config.database}")
        print(f"Datasources in graph: {structure.datasource_ids()}")
        print(f"Data objects: {len(structure.data_objects)}")
        print(f"Fields: {len(structure.fields)}")
        print(f"RELATES_TO relationships: {len(structure.relationships)}")
        print(f"Trusted semantic assets: {len(trusted)}")

        results: list[SemanticRetrievalResult] = []
        grounds: list[GroundingResult] = []
        failures: list[str] = []
        grounding_failures: list[str] = []
        planning_failures: list[str] = []
        for label, business_query, expectation, directive in business_questions():
            print()
            print(f"Question [{expectation}]: {label}")
            result = retriever.retrieve(
                business_query,
                requested_datasource_id=args.datasource,
                limit=args.limit,
            )
            repeat = retriever.retrieve(
                business_query,
                requested_datasource_id=args.datasource,
                limit=args.limit,
            )
            print_candidates(result)
            results.append(result)
            if result.model_dump_json() != repeat.model_dump_json():
                failures.append(f"non-deterministic retrieval for question: {label}")
            failures.extend(physical_correctness(structure, result))
            if args.datasource:
                out_of_scope = {
                    item.datasource_id
                    for item in result.candidates
                    if item.datasource_id != args.datasource
                }
                if out_of_scope:
                    failures.append(f"datasource isolation broken: {sorted(out_of_scope)}")

            grounding = grounder.ground(business_query, result)
            repeated_grounding = grounder.ground(business_query, repeat)
            print_grounding(grounding)
            grounds.append(grounding)
            if grounding.model_dump_json() != repeated_grounding.model_dump_json():
                grounding_failures.append(f"non-deterministic grounding for question: {label}")
            _, problems = grounding_outcome(grounding, expectation, label)
            grounding_failures.extend(problems)

            if expectation == "bound":
                try:
                    _, _, plan_problems = planning_block(
                        directive, grounding, context_builder, planner, validator, label
                    )
                    planning_failures.extend(plan_problems)
                except QueryContextBuildError as error:
                    grounding_failures.append(f"{label}: bound grounding refused context: {error}")
                except QueryPlanningError as error:
                    planning_failures.append(f"{label}: planner rejected the context: {error}")
            else:
                try:
                    planning_block(
                        directive, grounding, context_builder, planner, validator, label
                    )
                except QueryPlanningError as error:
                    planning_failures.append(
                        f"{label}: planner must not run on ambiguous grounding: {error}"
                    )

        serialized = "\n".join(
            [
                *(item.model_dump_json() for item in results),
                *(item.model_dump_json() for item in grounds),
            ]
        )
        leaked = [fragment for fragment in sample_fragments(catalog) if fragment in serialized]
        if leaked:
            failures.append(f"sample values leaked into retrieval results: {len(leaked)} fragment(s)")
        unnamed = identity_failures(results)

        print()
        print("=" * 80)
        print("P1-04A / P1-04B / P1-04C1 Acceptance Summary")
        print("=" * 80)
        print(f"Graph readable: {'PASS' if structure.datasources else 'FAIL'}")
        print(f"Deterministic retrieval: {'PASS' if not failures else 'FAIL'}")
        print(f"Physical correctness: {'PASS' if not failures else 'FAIL'}")
        print(f"Graph physical identity: {'PASS' if not unnamed else 'FAIL'}")
        print(f"Sample isolation: {'PASS' if not leaked else 'FAIL'}")
        print(f"Grounding: {'PASS' if not grounding_failures else 'FAIL'}")
        print(
            "Planning: "
            f"{'PASS' if not planning_failures else 'FAIL'}"
        )
        for failure in [*failures, *unnamed, *grounding_failures, *planning_failures]:
            print(f"[FAIL] {failure}")
        return (
            0
            if not failures
            and not unnamed
            and not grounding_failures
            and not planning_failures
            and structure.datasources
            else 1
        )
    finally:
        reader.close()


if __name__ == "__main__":
    raise SystemExit(main())
