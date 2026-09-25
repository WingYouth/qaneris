"""Route by governed asset coverage, after the existing business intent parser."""

import re
from dataclasses import dataclass

from smartdata.contracts.semantic import SemanticAssetType
from smartdata.federation.governance import FederationFailure


@dataclass(frozen=True)
class RoutingDecision:
    kind: str
    business_query: dict
    sources: list[dict]
    single_datasource_id: str | None = None


class FederationRouter:
    def __init__(self, service):
        self.service = service

    def route(self, question: str, workspace_id: str, allowed: list[str]) -> RoutingDecision:
        sources = [item for item in self.service.list_datasources(workspace_id)
                   if item.status == "ready" and item.id in allowed]
        if len(sources) < 2:
            return RoutingDecision("single_source", {}, [], sources[0].id if sources else None)
        intent = self.service.understand_intent(question)
        query = intent.business_query
        required = list(dict.fromkeys([*query.metrics, *query.dimensions, *query.entities]))
        coverage: dict[str, set[str]] = {}
        summaries: list[dict] = []
        for source in sources:
            retrieval = self.service.retrieve_semantics(query, workspace_id, source.id, limit=100)
            assets = [item for item in retrieval.candidates
                      if item.datasource_id == source.id and item.asset_type in {
                          SemanticAssetType.BUSINESS_TERM,
                          SemanticAssetType.METRIC,
                          SemanticAssetType.DIMENSION,
                      } and item.trusted]
            names = {name for name in required if any(
                name.casefold() == item.name.casefold()
                or name in item.matched_terms for item in assets
            )}
            coverage[source.id] = names
            summaries.append({
                "datasource_id": source.id,
                "business_name": source.name,
                "kind": str(source.kind),
                "driver": source.driver,
                "assets": sorted({item.name for item in assets})[:60],
                "covered_terms": sorted(names),
            })
        explicit = [source for source in sources if _mentions(question, source.name)
                    or _mentions(question, source.id)]
        if len(explicit) >= 2:
            kind = "federated"
        elif required and any(set(required) <= names for names in coverage.values()):
            kind = "single_source"
        elif required and set(required) <= set().union(*coverage.values()) \
                and sum(bool(names) for names in coverage.values()) >= 2:
            kind = "federated"
        else:
            kind = "single_source"
        full = [source.id for source in sources
                if required and set(required) <= coverage[source.id]]
        selected = full[0] if len(full) == 1 else None
        if kind == "single_source" and selected is None and len(sources) > 1:
            raise FederationFailure("source_selection_ambiguous", blocked=True)
        return RoutingDecision(kind, query.model_dump(mode="json"), summaries, selected)


def ready_scope(service, workspace_id: str, requested: list[str]) -> list[str]:
    ready = {item.id for item in service.list_datasources(workspace_id)
             if item.status == "ready"}
    if requested and not set(requested) <= ready:
        raise FederationFailure("scope_datasource_not_ready", blocked=True)
    return sorted(set(requested) if requested else ready)


def _mentions(question: str, name: str) -> bool:
    if not name.strip():
        return False
    if name.isascii():
        return bool(re.search(
            rf"(?<![A-Za-z0-9_]){re.escape(name)}(?![A-Za-z0-9_])",
            question, re.IGNORECASE,
        ))
    return name.casefold() in question.casefold()
