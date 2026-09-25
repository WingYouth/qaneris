from __future__ import annotations

import re

from qaneris.contracts.semantic import BusinessFilter, BusinessQuery, RequestedOutput
from qaneris.semantic.models import IntentConflict, RuleExtraction

_QUALIFIED_IDENTIFIER = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*\.[A-Za-z_][A-Za-z0-9_]*$")


class BusinessQueryMergePolicy:
    """Merge deterministic facts over model-inferred business semantics."""

    def merge(
        self, question: str, rules: RuleExtraction, model_query: BusinessQuery
    ) -> tuple[BusinessQuery, list[IntentConflict]]:
        values = model_query.model_dump()
        values["question"] = question
        conflicts: list[IntentConflict] = []

        self._override(values, conflicts, "objective", rules.objective_hint)
        self._override(values, conflicts, "time_expression", rules.time_expression)
        comparison = rules.comparison
        if comparison and model_query.comparison and comparison.baseline is None:
            comparison = comparison.model_copy(update={"baseline": model_query.comparison.baseline})
        ranking = rules.ranking
        if ranking and model_query.ranking and ranking.metric is None:
            ranking = ranking.model_copy(update={"metric": model_query.ranking.metric})
        self._override(values, conflicts, "comparison", comparison)
        self._override(values, conflicts, "ranking", ranking)

        values["filters"] = self._merge_filters(rules.filters, model_query.filters, conflicts)
        rule_ambiguities = self.rule_ambiguities(rules)
        if rule_ambiguities:
            ambiguities = list(values["ambiguities"])
            ambiguities.extend(rule_ambiguities)
            values["ambiguities"] = list(dict.fromkeys(ambiguities))
        if rules.ranking and not values["requested_output"]:
            values["requested_output"] = [RequestedOutput.TABLE]
        if conflicts:
            values["confidence"] = min(float(values["confidence"]), 0.8)
        return BusinessQuery.model_validate(values), conflicts

    @staticmethod
    def rule_ambiguities(rules: RuleExtraction) -> list[str]:
        """Ambiguities the *rule layer* derived, as opposed to ones the model reported.

        Only these block execution: they mean a deterministic input could not be expressed as a
        business filter, so answering without asking would silently drop part of the question.
        The model's own ``ambiguities`` stay advisory.
        """
        return [
            f"物理字段表达 {item.subject} 需转换为业务过滤条件"
            for item in rules.filters
            if _QUALIFIED_IDENTIFIER.fullmatch(item.subject)
        ]

    @staticmethod
    def _override(
        values: dict[str, object],
        conflicts: list[IntentConflict],
        field: str,
        deterministic_value: object | None,
    ) -> None:
        if deterministic_value is None:
            return
        model_value = values.get(field)
        dumped_value = (
            deterministic_value.model_dump()
            if hasattr(deterministic_value, "model_dump")
            else deterministic_value
        )
        if model_value is not None and model_value != dumped_value:
            conflicts.append(
                IntentConflict(
                    field=field,
                    deterministic_value=dumped_value,
                    model_value=model_value,
                )
            )
        values[field] = dumped_value

    @staticmethod
    def _merge_filters(
        deterministic: list[BusinessFilter],
        inferred: list[BusinessFilter],
        conflicts: list[IntentConflict],
    ) -> list[BusinessFilter]:
        safe_deterministic = [
            item for item in deterministic if not _QUALIFIED_IDENTIFIER.fullmatch(item.subject)
        ]
        merged = list(safe_deterministic)
        deterministic_by_subject = {item.subject.casefold(): item for item in safe_deterministic}
        for model_filter in inferred:
            rule_filter = deterministic_by_subject.get(model_filter.subject.casefold())
            if rule_filter is None:
                merged.append(model_filter)
                continue
            if rule_filter != model_filter:
                conflicts.append(
                    IntentConflict(
                        field=f"filters.{model_filter.subject}",
                        deterministic_value=rule_filter.model_dump(),
                        model_value=model_filter.model_dump(),
                    )
                )
        return merged
