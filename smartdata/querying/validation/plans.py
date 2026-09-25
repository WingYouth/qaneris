"""Query plan and generated-command validation."""

from __future__ import annotations

import json
import re

from smartdata.common.errors import QueryPlanningError, QuerySafetyError
from smartdata.contracts.query import (
    GeneratedQuery,
    GroundedFieldRef,
    GroundedQueryContext,
    GroundedQueryPlan,
    PlanSortTarget,
    QueryContext,
    QueryLanguage,
    QueryPlan,
    aggregate_function_for,
)
from smartdata.contracts.semantic import SemanticAssetType
from smartdata.querying.validation.read_only import validate_read_only_query

_WRITE_CYPHER = re.compile(
    r"\b(CREATE|DELETE|DETACH|DROP|FOREACH|LOAD\s+CSV|MERGE|REMOVE|SET)\b", re.IGNORECASE
)
_WRITE_GRAPHQL = re.compile(r"\bmutation\b", re.IGNORECASE)
_WRITE_FLUX = re.compile(r"\b(to|experimental\.to)\s*\(", re.IGNORECASE)


class QueryPlanValidator:
    def validate(self, plan: QueryPlan, context: QueryContext) -> None:
        sources = [
            item for item in context.data_sources if item.datasource_id == plan.datasource_id
        ]
        if len(sources) != 1:
            raise QueryPlanningError("查询计划必须使用一个已召回的数据源")
        objects = {item.profile.id: item.profile for item in sources[0].data_objects}
        if not set(plan.data_object_ids).issubset(objects):
            raise QueryPlanningError("查询计划包含未授权的数据对象")
        allowed_fields = {
            f"{object_id}::{field.path}"
            for object_id in plan.data_object_ids
            for field in objects[object_id].fields
        }
        used_fields = set(plan.output_fields + plan.metrics + plan.dimensions + plan.group_by)
        used_fields.update(item.field for item in plan.filters)
        used_fields.update(item.field for item in plan.aggregates if item.field)
        if plan.time_field:
            used_fields.add(plan.time_field)
        if (plan.time_range is None) != (plan.time_field is None):
            raise QueryPlanningError("时间范围和时间字段必须同时存在")
        aliases = {item.alias for item in plan.aggregates if item.alias}
        used_fields.update(item.field for item in plan.sorts if item.field not in aliases)
        if not used_fields.issubset(allowed_fields):
            raise QueryPlanningError("查询计划包含未召回的字段")
        allowed_relationships = {item.id: item for item in context.relationships}
        if not set(plan.relationship_ids).issubset(allowed_relationships):
            raise QueryPlanningError("查询计划包含未召回的关系")
        for relationship_id in plan.relationship_ids:
            relationship = allowed_relationships[relationship_id]
            if (
                relationship.from_datasource_id != plan.datasource_id
                or relationship.to_datasource_id != plan.datasource_id
                or relationship.from_object_id not in plan.data_object_ids
                or relationship.to_object_id not in plan.data_object_ids
            ):
                raise QueryPlanningError("第一版禁止跨数据源关系或计划外对象参与执行")


class NativeQueryValidator:
    def validate(self, generated: GeneratedQuery, plan: QueryPlan) -> None:
        if (
            generated.datasource_id != plan.datasource_id
            or generated.language != plan.query_language
        ):
            raise QuerySafetyError("原生命令与查询计划不一致")
        command = generated.command
        if generated.language in (QueryLanguage.SQL, QueryLanguage.CQL):
            if not isinstance(command, str):
                raise QuerySafetyError("SQL/CQL 命令必须是字符串")
            validate_read_only_query(command)
            return
        if generated.language == QueryLanguage.CYPHER:
            if not isinstance(command, str) or _WRITE_CYPHER.search(command):
                raise QuerySafetyError("Cypher 命令不是只读查询")
            return
        if generated.language == QueryLanguage.GRAPHQL:
            if not isinstance(command, str) or _WRITE_GRAPHQL.search(command):
                raise QuerySafetyError("GraphQL 命令包含写操作")
            return
        if generated.language == QueryLanguage.FLUX:
            if not isinstance(command, str) or _WRITE_FLUX.search(command):
                raise QuerySafetyError("Flux 命令包含写操作")
            return
        try:
            payload = json.loads(command) if isinstance(command, str) else command
        except json.JSONDecodeError as error:
            raise QuerySafetyError("JSON 查询命令格式无效") from error
        if not isinstance(payload, dict):
            raise QuerySafetyError("JSON 查询命令必须是对象")
        if generated.language == QueryLanguage.REDIS_JSON and str(
            payload.get("command", "")
        ).upper() not in {"GET", "MGET", "HGETALL", "SCAN"}:
            raise QuerySafetyError("Redis 命令不在只读白名单中")


#: A grounded plan expresses *how* to query with graph identities. Native query text or a query
#: language would let a write intent reach a datasource through planning, so their presence in the
#: contract is a boundary failure — the plan reaches a datasource only as a read-only query that the
#: generator compiled and the native validator approved.
_FORBIDDEN_PLAN_FIELDS = (
    "sql",
    "cypher",
    "cql",
    "flux",
    "command",
    "query",
    "query_language",
)


def _plan_field_refs(plan: GroundedQueryPlan) -> list[tuple[GroundedFieldRef, str]]:
    references: list[tuple[GroundedFieldRef, str]] = [
        (item, "选择字段") for item in plan.selected_fields
    ]
    references.extend((item, "分组字段") for item in plan.group_by)
    references.extend((item.field, "过滤字段") for item in plan.filters)
    references.extend(
        (item.field, "聚合字段") for item in plan.aggregates if item.field is not None
    )
    references.extend(
        (item.field, "排序字段") for item in plan.sorts if item.field is not None
    )
    if plan.time_field is not None:
        references.append((plan.time_field, "时间字段"))
    return references


class GroundedPlanValidator:
    """Validates a grounded plan against the context it was planned from.

    Every physical reference must come from the grounding allowlist, every join must be a confirmed
    relationship used with its own keys, and a plan that would cross data sources is refused. The
    validator never repairs a plan: an invalid plan fails.
    """

    def validate(self, plan: GroundedQueryPlan, context: GroundedQueryContext) -> None:
        self._require_no_write_intent(plan)
        self._require_scope(plan, context)
        self._require_objects(plan, context)
        self._require_locator_subset(plan, context)
        self._require_fields(plan, context)
        self._require_ordering(plan)
        self._require_joins(plan, context)
        self._require_unique(plan)
        self._require_aggregation_semantics(plan, context)
        self._require_revision(plan, context)

    @staticmethod
    def _require_no_write_intent(plan: GroundedQueryPlan) -> None:
        declared = set(type(plan).model_fields)
        forbidden = sorted(declared & set(_FORBIDDEN_PLAN_FIELDS))
        if forbidden:
            raise QueryPlanningError(
                f"第一阶段查询计划不得携带原生查询文本或查询语言：{forbidden}"
            )

    @staticmethod
    def _require_scope(plan: GroundedQueryPlan, context: GroundedQueryContext) -> None:
        if len(context.allowed_datasource_ids) != 1:
            raise QueryPlanningError("第一阶段禁止跨数据源查询计划")
        allowed = context.allowed_datasource_ids[0]
        if plan.datasource_id != allowed:
            raise QueryPlanningError(
                f"查询计划的数据源不在接地允许列表中：{plan.datasource_id}"
            )
        if (
            context.requested_datasource_id
            and plan.datasource_id != context.requested_datasource_id
        ):
            raise QueryPlanningError("查询计划的数据源与指定数据源不一致")
        if plan.workspace_id != context.workspace_id:
            raise QueryPlanningError("查询计划的工作区与查询上下文不一致")

    @staticmethod
    def _require_objects(plan: GroundedQueryPlan, context: GroundedQueryContext) -> None:
        unauthorized = sorted(set(plan.data_object_ids) - set(context.allowed_data_object_ids))
        if unauthorized:
            raise QueryPlanningError(f"查询计划包含允许列表之外的数据对象：{unauthorized}")
        if len(plan.data_object_ids) > 1 and not plan.joins:
            raise QueryPlanningError("多数据对象计划必须包含一个已确认的直接连接")

    @staticmethod
    def _require_locator_subset(
        plan: GroundedQueryPlan, context: GroundedQueryContext
    ) -> None:
        """Every plan data object must have a locator, and it must match the grounded locator.

        An object the context never confirmed — or one whose locator disagrees with the
        context's locator — would let the generator target the wrong table. The plan is rejected
        instead of silently re-targeted. Two distinct data objects must not collapse into one
        locator and one data object must not be hidden behind several locators.
        """
        plan_ids = set(plan.data_object_ids)
        locator_ids = set(plan.data_objects)
        orphans = sorted(locator_ids - plan_ids)
        if orphans:
            raise QueryPlanningError(
                f"plan data_objects contains an object not bound to the plan: {orphans}"
            )
        missing = sorted(plan_ids - locator_ids)
        if missing:
            raise QueryPlanningError(
                f"plan is missing a data_objects locator for: {missing}"
            )
        duplicates = [
            object_id
            for object_id, locator in plan.data_objects.items()
            if locator.data_object_id != object_id
        ]
        if duplicates:
            raise QueryPlanningError(
                f"plan data_objects key disagrees with the locator's data_object_id: {duplicates}"
            )
        for object_id, locator in plan.data_objects.items():
            reference = context.data_objects.get(object_id)
            if reference is None:
                raise QueryPlanningError(
                    f"plan data_objects references an object the context never grounded: {object_id}"
                )
            if reference.full_identity() != locator.full_identity():
                raise QueryPlanningError(
                    "plan data_objects locator disagrees with the grounded context for "
                    f"{object_id}: plan={locator.full_identity()}, "
                    f"context={reference.full_identity()}"
                )

    @staticmethod
    def _require_fields(plan: GroundedQueryPlan, context: GroundedQueryContext) -> None:
        identities = context.field_identity()
        for reference, used_as in _plan_field_refs(plan):
            identity = reference.identity()
            if identity not in identities:
                raise QueryPlanningError(
                    f"{used_as}不在允许列表中：{identity[1]}.{identity[2]}"
                )
            if reference.data_object_id not in plan.data_object_ids:
                raise QueryPlanningError(
                    f"{used_as}不属于计划内的数据对象：{reference.data_object_id}"
                )
            expected = identities[identity]
            if reference.field_id is not None and reference.field_id != expected:
                raise QueryPlanningError(
                    f"{used_as}的图字段标识与允许列表不一致："
                    f"{reference.field_path} → {reference.field_id}"
                )

    @staticmethod
    def _require_ordering(plan: GroundedQueryPlan) -> None:
        aliases = {aggregate.alias for aggregate in plan.aggregates}
        for item in plan.sorts:
            if item.target is PlanSortTarget.AGGREGATE and item.aggregate_alias not in aliases:
                raise QueryPlanningError(
                    f"排序引用了计划中不存在的聚合：{item.aggregate_alias}"
                )

    @staticmethod
    def _require_joins(plan: GroundedQueryPlan, context: GroundedQueryContext) -> None:
        confirmed = {item.relationship_id: item for item in context.relationships}
        for join in plan.joins:
            if join.relationship_id not in context.allowed_relationship_ids:
                raise QueryPlanningError(
                    f"连接使用了允许列表之外的关系：{join.relationship_id}"
                )
            relationship = confirmed.get(join.relationship_id)
            if relationship is None:
                raise QueryPlanningError(
                    f"连接必须来自上下文中已确认的关系：{join.relationship_id}"
                )
            if (
                join.datasource_id != plan.datasource_id
                or relationship.datasource_id != plan.datasource_id
            ):
                raise QueryPlanningError("第一阶段禁止跨数据源连接")
            if join.from_data_object_id == join.to_data_object_id:
                raise QueryPlanningError("连接的两个端点不能是同一个数据对象")
            if {join.from_data_object_id, join.to_data_object_id} != {
                relationship.from_data_object_id,
                relationship.to_data_object_id,
            }:
                raise QueryPlanningError("连接的端点必须与已确认关系一致")
            if (join.from_field_path, join.to_field_path) != (
                relationship.from_field_path,
                relationship.to_field_path,
            ):
                raise QueryPlanningError("连接的字段必须与已确认关系一致")
            endpoints = {join.from_data_object_id, join.to_data_object_id}
            if not endpoints.issubset(plan.data_object_ids):
                raise QueryPlanningError("连接的端点必须属于计划内的数据对象")

    @staticmethod
    def _require_unique(plan: GroundedQueryPlan) -> None:
        """A plan is a deterministic document: no duplicated alias, reference or join."""
        aliases = [aggregate.alias for aggregate in plan.aggregates]
        if len(set(aliases)) != len(aliases):
            raise QueryPlanningError("查询计划的聚合别名必须唯一")
        for references, label in ((plan.selected_fields, "选择字段"), (plan.group_by, "分组字段")):
            identities = [item.identity() for item in references]
            if len(set(identities)) != len(identities):
                raise QueryPlanningError(f"查询计划包含重复的{label}引用")
        relationship_ids = [join.relationship_id for join in plan.joins]
        if len(set(relationship_ids)) != len(relationship_ids):
            raise QueryPlanningError("查询计划包含重复的连接")

    @staticmethod
    def _require_aggregation_semantics(
        plan: GroundedQueryPlan, context: GroundedQueryContext
    ) -> None:
        """The aggregate function must come from a governed rule, never be guessed.

        For each metric-bearing aggregate we know which binding it came from: the binding's
        ``default_aggregation`` or a structured ``DerivationSpec`` on the context that explicitly
        targets the metric. Anything else is a "this happens to be a metric, so SUM" guess and is
        refused here as a final fence.
        """
        overrides = {
            derivation.metric: aggregate_function_for(derivation.operation)
            for derivation in context.derivations
            if derivation.metric
        }
        allowed_bindings = {binding.field_ref().identity(): binding for binding in context.bindings if binding.field_ref() is not None}
        for aggregate in plan.aggregates:
            if aggregate.field is None:
                continue
            identity = aggregate.field.identity()
            binding = allowed_bindings.get(identity)
            if binding is None:
                # The aggregate field is allowlisted but not bound to a governed metric binding.
                # The earlier allowlist check would have caught an unrelated column; here we only
                # care about metrics.
                continue
            if binding.asset_type is not SemanticAssetType.METRIC:
                continue
            if overrides.get(binding.business_term) is not None:
                continue
            if binding.default_aggregation is None:
                raise QueryPlanningError(
                    f"指标“{binding.business_term}”缺少受治理聚合语义，计划不得猜测聚合函数"
                )
            if binding.default_aggregation is not aggregate.function:
                raise QueryPlanningError(
                    f"指标“{binding.business_term}”的聚合函数与受治理默认值不一致："
                    f"计划用 {aggregate.function.value}，"
                    f"治理值 {binding.default_aggregation.value}"
                )

    @staticmethod
    def _require_revision(plan: GroundedQueryPlan, context: GroundedQueryContext) -> None:
        """The plan must carry the graph revision the plan was built from, exactly.

        ``context.scan_version`` is the revision the bindings came from; ``plan.scan_version``
        must equal it, including when the context has a version but the plan has ``None`` (a
        sign the planner discarded the version). A compatibility context without a version may
        pair with a version-less plan; both ``None`` is fine. Plans that let the executor run
        against a different graph revision are refused.
        """
        if context.scan_version is None:
            return
        if plan.scan_version != context.scan_version:
            raise QueryPlanningError(
                "查询计划使用了与查询上下文不同的企业数据图扫描版本："
                f"context={context.scan_version}, plan={plan.scan_version}"
            )
