from __future__ import annotations

from datetime import datetime
from typing import Annotated, Any, Literal

from pydantic import BaseModel, Field


class ResultMetadata(BaseModel):
    datasource_id: str
    data_object_ids: list[str] = Field(min_length=1)
    row_count: int = Field(ge=0)
    truncated: bool = False
    duration_ms: float | None = Field(default=None, ge=0)


class ScalarResult(ResultMetadata):
    result_type: Literal["scalar"] = "scalar"
    value: Any
    value_type: str


class TabularResult(ResultMetadata):
    result_type: Literal["tabular"] = "tabular"
    columns: list[str]
    rows: list[dict[str, Any]]


class DocumentResult(ResultMetadata):
    result_type: Literal["document"] = "document"
    documents: list[dict[str, Any]]


class KeyValueEntry(BaseModel):
    key: str
    value_type: str
    value: Any
    ttl_seconds: int | None = None


class KeyValueResult(ResultMetadata):
    result_type: Literal["key_value"] = "key_value"
    entries: list[KeyValueEntry]


class WideColumnRow(BaseModel):
    row_key: str
    cells: dict[str, Any]


class WideColumnResult(ResultMetadata):
    result_type: Literal["wide_column"] = "wide_column"
    rows: list[WideColumnRow]


class GraphNode(BaseModel):
    id: str
    labels: list[str] = Field(default_factory=list)
    properties: dict[str, Any] = Field(default_factory=dict)


class GraphEdge(BaseModel):
    id: str
    edge_type: str
    from_node_id: str
    to_node_id: str
    properties: dict[str, Any] = Field(default_factory=dict)


class GraphResult(ResultMetadata):
    result_type: Literal["graph"] = "graph"
    nodes: list[GraphNode] = Field(default_factory=list)
    edges: list[GraphEdge] = Field(default_factory=list)
    paths: list[list[str]] = Field(default_factory=list)


class TimeSeriesPoint(BaseModel):
    timestamp: datetime
    value: Any
    fields: dict[str, Any] = Field(default_factory=dict)


class TimeSeries(BaseModel):
    name: str
    tags: dict[str, str] = Field(default_factory=dict)
    points: list[TimeSeriesPoint] = Field(default_factory=list)


class TimeSeriesResult(ResultMetadata):
    result_type: Literal["time_series"] = "time_series"
    series: list[TimeSeries] = Field(default_factory=list)


class SearchHit(BaseModel):
    id: str
    score: float | None = None
    document: dict[str, Any]
    highlights: dict[str, list[str]] = Field(default_factory=dict)


class SearchResult(ResultMetadata):
    result_type: Literal["search"] = "search"
    hits: list[SearchHit] = Field(default_factory=list)
    total_hits: int | None = Field(default=None, ge=0)


class VectorMatch(BaseModel):
    id: str
    score: float
    payload: dict[str, Any] = Field(default_factory=dict)


class VectorResult(ResultMetadata):
    result_type: Literal["vector"] = "vector"
    matches: list[VectorMatch] = Field(default_factory=list)


TypedQueryResult = Annotated[
    ScalarResult
    | TabularResult
    | DocumentResult
    | KeyValueResult
    | WideColumnResult
    | GraphResult
    | TimeSeriesResult
    | SearchResult
    | VectorResult,
    Field(discriminator="result_type"),
]

# Compatibility name retained for the current execution and API layers.
QueryResult = TypedQueryResult
