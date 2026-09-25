"""Relevant profile retrieval."""

from __future__ import annotations

import re

from qaneris.contracts.profile import CompanyDataProfile, DataObjectProfile
from qaneris.contracts.query import (
    QueryContext,
    QueryIntent,
    RetrievedDataObject,
    RetrievedDataSource,
)


def _terms(value: str) -> set[str]:
    lowered = value.lower()
    words = set(re.findall(r"[a-z0-9]+|[\u4e00-\u9fff]+", lowered))
    chinese = "".join(re.findall(r"[\u4e00-\u9fff]", lowered))
    words.update(chinese[index : index + 2] for index in range(max(0, len(chinese) - 1)))
    return {item for item in words if item}


class ProfileRetriever:
    def retrieve(self, profile: CompanyDataProfile, intent: QueryIntent) -> QueryContext:
        query_terms = _terms(" ".join([intent.question, *intent.entities, *intent.metrics]))
        candidates: list[tuple[float, object, DataObjectProfile, list[str]]] = []
        for datasource in profile.data_sources:
            if (
                intent.requested_datasource_id
                and datasource.datasource_id != intent.requested_datasource_id
            ):
                continue
            for namespace in datasource.namespaces:
                for data_object in namespace.data_objects:
                    score, reasons = self._score(query_terms, data_object)
                    candidates.append((score, datasource, data_object, reasons))
        candidates.sort(key=lambda item: (-item[0], item[2].name, item[1].datasource_id))
        selected = self._select(candidates)
        selected = self._expand_one_hop(selected, candidates, profile)
        selected_ids = {item[2].id for item in selected}
        relationships = [
            relation
            for relation in profile.relationships
            if relation.from_object_id in selected_ids and relation.to_object_id in selected_ids
        ]
        by_source: dict[str, RetrievedDataSource] = {}
        field_budget = 30
        for score, datasource, data_object, reasons in selected:
            trimmed = self._trim_fields(data_object, query_terms, field_budget)
            field_budget -= len(trimmed.fields)
            target = by_source.setdefault(
                datasource.datasource_id,
                RetrievedDataSource(
                    datasource_id=datasource.datasource_id,
                    name=datasource.name,
                    kind=datasource.kind,
                    driver=datasource.driver,
                ),
            )
            target.data_objects.append(
                RetrievedDataObject(profile=trimmed, score=score, reasons=reasons)
            )
        return QueryContext(
            workspace_id=profile.workspace_id,
            intent=intent,
            data_sources=list(by_source.values()),
            relationships=relationships,
        )

    @staticmethod
    def _score(query_terms: set[str], data_object: DataObjectProfile) -> tuple[float, list[str]]:
        object_terms = _terms(data_object.name)
        field_terms = {term for field in data_object.fields for term in _terms(field.path)}
        semantic_values = [data_object.comment or ""]
        for metadata in data_object.semantic_metadata:
            semantic_values.extend(
                [
                    metadata.business_name or "",
                    metadata.description or "",
                    metadata.business_entity or "",
                    *metadata.aliases,
                    *metadata.roles,
                ]
            )
        semantic_terms = _terms(" ".join(semantic_values))
        object_hits = query_terms & object_terms
        field_hits = query_terms & field_terms
        semantic_hits = query_terms & semantic_terms
        score = 5.0 * len(object_hits) + 2.0 * len(field_hits) + 3.0 * len(semantic_hits)
        reasons = []
        if object_hits:
            reasons.append(f"数据对象名称匹配：{', '.join(sorted(object_hits))}")
        if field_hits:
            reasons.append(f"字段匹配：{', '.join(sorted(field_hits))}")
        if semantic_hits:
            reasons.append(f"语义元数据匹配：{', '.join(sorted(semantic_hits))}")
        return score, reasons

    @staticmethod
    def _select(candidates: list[tuple]) -> list[tuple]:
        if not candidates:
            return []
        positive = [item for item in candidates if item[0] > 0]
        pool = positive or candidates[:1]
        selected: list[tuple] = []
        source_counts: dict[str, int] = {}
        for item in pool:
            source_id = item[1].datasource_id
            if source_id not in source_counts and len(source_counts) >= 3:
                continue
            if source_counts.get(source_id, 0) >= 5:
                continue
            selected.append(item)
            source_counts[source_id] = source_counts.get(source_id, 0) + 1
        return selected

    @staticmethod
    def _expand_one_hop(
        selected: list[tuple], candidates: list[tuple], profile: CompanyDataProfile
    ) -> list[tuple]:
        selected_ids = {item[2].id for item in selected}
        related_ids = {
            endpoint
            for relation in profile.relationships
            if relation.from_object_id in selected_ids or relation.to_object_id in selected_ids
            for endpoint in (relation.from_object_id, relation.to_object_id)
            if endpoint not in selected_ids
        }
        counts: dict[str, int] = {}
        for item in selected:
            counts[item[1].datasource_id] = counts.get(item[1].datasource_id, 0) + 1
        for score, datasource, data_object, reasons in candidates:
            if data_object.id not in related_ids or data_object.id in selected_ids:
                continue
            if datasource.datasource_id not in counts and len(counts) >= 3:
                continue
            if counts.get(datasource.datasource_id, 0) >= 5:
                continue
            selected.append(
                (
                    max(score, 0.1),
                    datasource,
                    data_object,
                    [*reasons, "与已召回数据对象存在一跳关系"],
                )
            )
            selected_ids.add(data_object.id)
            counts[datasource.datasource_id] = counts.get(datasource.datasource_id, 0) + 1
        return selected

    @staticmethod
    def _trim_fields(
        data_object: DataObjectProfile, query_terms: set[str], budget: int
    ) -> DataObjectProfile:
        ranked = sorted(
            data_object.fields,
            key=lambda field: (not bool(_terms(field.path) & query_terms), field.path),
        )
        return data_object.model_copy(
            update={"fields": ranked[: max(0, budget)], "previews": data_object.previews[:3]}
        )
