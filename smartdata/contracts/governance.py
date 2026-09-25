from pydantic import BaseModel, Field


class MappingInfo(BaseModel):
    id: str | None = None
    workspace_id: str = "default"
    entity: str
    canonical_field: str
    datasource_id: str
    dataset: str
    field: str
    confidence: float = Field(default=1.0, ge=0.0, le=1.0)
    source: str = "manual"


class GovernanceSuggestion(BaseModel):
    id: str
    type: str
    entity: str
    confidence: float
    reason: str
    status: str = "pending"
