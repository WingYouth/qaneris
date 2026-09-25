"""Model boundary: only safe business context enters federation planning."""

from typing import Any, Protocol

from pydantic import BaseModel, Field

from smartdata.federation.models import ExecutionScope, FederatedPlanDraft


class FederatedPlanningContext(BaseModel):
    question: str
    business_query: dict[str, Any]
    sources: list[dict[str, Any]]
    confirmed_join_mappings: list[dict[str, Any]] = Field(default_factory=list)
    confirmed_semantic_memory: dict[str, Any] = Field(default_factory=dict)
    allowed_merge_operations: list[str]
    execution_scope: ExecutionScope


class FederationPlanningModel(Protocol):
    def plan_federation(self, context: FederatedPlanningContext) -> FederatedPlanDraft: ...


class FederationDiscoveryService(Protocol):
    def discover(self, question: str, workspace_id: str, allowed: list[str], memory: Any): ...
