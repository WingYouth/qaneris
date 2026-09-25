from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field, model_validator


class ScanNodeKind(StrEnum):
    DATABASE = "Database"
    NAMESPACE = "Namespace"
    DATA_OBJECT = "DataObject"
    FIELD = "Field"
    INDEX = "Index"
    CONSTRAINT = "Constraint"


class ScanEdgeType(StrEnum):
    HAS_NAMESPACE = "HAS_NAMESPACE"
    CONTAINS = "CONTAINS"
    HAS_FIELD = "HAS_FIELD"
    HAS_INDEX = "HAS_INDEX"
    HAS_CONSTRAINT = "HAS_CONSTRAINT"
    RELATES_TO = "RELATES_TO"


class ScanGraphNode(BaseModel):
    id: str
    kind: ScanNodeKind
    properties: dict[str, Any] = Field(default_factory=dict)


class ScanGraphEdge(BaseModel):
    id: str
    type: ScanEdgeType
    from_node_id: str
    to_node_id: str
    properties: dict[str, Any] = Field(default_factory=dict)


class ScanGraph(BaseModel):
    workspace_id: str
    datasource_id: str
    scan_version: int = Field(ge=1)
    scanned_at: datetime
    nodes: list[ScanGraphNode] = Field(default_factory=list)
    edges: list[ScanGraphEdge] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_integrity(self) -> ScanGraph:
        node_ids = [node.id for node in self.nodes]
        edge_ids = [edge.id for edge in self.edges]
        if len(node_ids) != len(set(node_ids)):
            raise ValueError("scan graph contains duplicate node ids")
        if len(edge_ids) != len(set(edge_ids)):
            raise ValueError("scan graph contains duplicate edge ids")
        known = set(node_ids)
        dangling = [
            edge.id
            for edge in self.edges
            if edge.from_node_id not in known or edge.to_node_id not in known
        ]
        if dangling:
            raise ValueError(f"scan graph contains dangling edges: {', '.join(dangling)}")
        return self
