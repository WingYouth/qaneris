"""Read model for the published enterprise data graph.

The write side publishes a ``ScanGraph``. This module describes the read side contract used by
semantic retrieval: a bounded, structural view of what is currently published.

Only structural metadata is part of the read model. Sample values, previews and any other observed
business data are deliberately absent, so no consumer of this model can forward them into model
context or retrieval evidence.
"""

from __future__ import annotations

from typing import TypeVar

from pydantic import BaseModel, ConfigDict, Field, model_validator


class GraphDatasource(BaseModel):
    """A published ``Database`` node within one workspace."""

    model_config = ConfigDict(extra="forbid")

    node_id: str = Field(min_length=1)
    datasource_id: str = Field(min_length=1)
    workspace_id: str = Field(min_length=1)
    name: str = Field(min_length=1)
    kind: str | None = None
    driver: str | None = None
    database_name: str | None = None
    scan_version: int | None = None


class GraphDataObject(BaseModel):
    """A published ``DataObject`` node together with its structural metadata."""

    model_config = ConfigDict(extra="forbid")

    node_id: str = Field(min_length=1)
    datasource_id: str = Field(min_length=1)
    namespace: str | None = None
    name: str = Field(min_length=1)
    qualified_name: str | None = None
    object_kind: str | None = None
    comment: str | None = None
    description: str | None = None
    estimated_record_count: int | None = None


class GraphField(BaseModel):
    """A published ``Field`` node of one data object."""

    model_config = ConfigDict(extra="forbid")

    node_id: str = Field(min_length=1)
    object_id: str = Field(min_length=1)
    datasource_id: str = Field(min_length=1)
    object_name: str = Field(min_length=1)
    name: str = Field(min_length=1)
    path: str = Field(min_length=1)
    data_type: str | None = None
    native_type: str | None = None
    nullable: bool | None = None
    primary_key: bool = False
    unique: bool = False
    indexed: bool = False
    comment: str | None = None
    description: str | None = None


class GraphRelationship(BaseModel):
    """A published ``RELATES_TO`` edge.

    Relationships only exist when the graph contains a real ``RELATES_TO`` edge. This model carries
    no inference or suggestion semantics: an object without a published edge never becomes a
    relationship asset.
    """

    model_config = ConfigDict(extra="forbid")

    relationship_id: str = Field(min_length=1)
    relationship_type: str = Field(min_length=1)
    from_object_id: str = Field(min_length=1)
    from_datasource_id: str = Field(min_length=1)
    from_object_name: str = Field(min_length=1)
    from_field_path: str | None = None
    to_object_id: str = Field(min_length=1)
    to_datasource_id: str = Field(min_length=1)
    to_object_name: str = Field(min_length=1)
    to_field_path: str | None = None
    source: str | None = None
    directed: bool = True
    confirmed: bool = True


class GraphBindingReference(BaseModel):
    """A physical binding a caller needs resolved against the published graph.

    A caller may identify the binding by the graph's stable physical identity
    (``graph_data_object_id`` / ``graph_field_id``) or, during migration, by human-readable lookup
    (``datasource_name``, ``namespace``, ``qualified_name``, ``data_object_name``, ``field_path``).
    The physical identity is authoritative; readable names never override it.
    """

    model_config = ConfigDict(extra="forbid")

    datasource_id: str | None = Field(default=None, min_length=1)
    datasource_name: str | None = Field(default=None, min_length=1)
    graph_data_object_id: str | None = Field(default=None, min_length=1)
    namespace: str | None = Field(default=None, min_length=1)
    qualified_name: str | None = Field(default=None, min_length=1)
    data_object_name: str | None = Field(default=None, min_length=1)
    graph_field_id: str | None = Field(default=None, min_length=1)
    field_path: str | None = Field(default=None, min_length=1)

    @model_validator(mode="after")
    def validate_identity(self) -> GraphBindingReference:
        if not (self.datasource_id or self.datasource_name or self.graph_data_object_id):
            raise ValueError("binding reference requires a datasource or a graph data object id")
        if not (self.graph_data_object_id or self.qualified_name or self.data_object_name):
            raise ValueError("binding reference requires a graph data object id or an object name")
        return self


class GraphStructureRequest(BaseModel):
    """Bounded read request for the current enterprise data graph.

    ``datasource_id`` scopes the read inside the database query itself, not after reading everything.
    ``references`` asks the reader to additionally resolve exact physical bindings, which keeps
    semantic asset verification exact even when the bounded context is truncated by the ``max_*``
    limits: reference resolution is never truncated.

    Phase 1 limitation: ``max_*`` bound the structural context used for term-level recall, so a
    workspace with more objects than ``max_data_objects`` is only partially visible to term scoring.
    Readers must order their queries deterministically so the same published graph always yields the
    same bounded window.
    """

    model_config = ConfigDict(extra="forbid")

    workspace_id: str = Field(default="default", min_length=1)
    datasource_id: str | None = None
    references: list[GraphBindingReference] = Field(default_factory=list)
    max_data_objects: int = Field(default=200, ge=1, le=2_000)
    max_fields: int = Field(default=2_000, ge=1, le=20_000)
    max_relationships: int = Field(default=200, ge=1, le=2_000)


class GraphStructure(BaseModel):
    """Deterministic, bounded view of the published enterprise data graph."""

    model_config = ConfigDict(extra="forbid")

    datasources: list[GraphDatasource] = Field(default_factory=list)
    data_objects: list[GraphDataObject] = Field(default_factory=list)
    fields: list[GraphField] = Field(default_factory=list)
    relationships: list[GraphRelationship] = Field(default_factory=list)
    truncated: bool = False

    def sorted(self) -> GraphStructure:
        """Return the same structure in a deterministic, backend-independent order."""
        return GraphStructure(
            datasources=sorted(
                self.datasources, key=lambda item: (item.datasource_id, item.node_id)
            ),
            data_objects=sorted(
                self.data_objects,
                key=lambda item: (
                    item.datasource_id,
                    item.qualified_name or item.name,
                    item.name,
                    item.node_id,
                ),
            ),
            fields=sorted(
                self.fields,
                key=lambda item: (
                    item.datasource_id,
                    item.object_id,
                    item.path,
                    item.node_id,
                ),
            ),
            relationships=sorted(
                self.relationships,
                key=lambda item: (item.from_object_id, item.relationship_id),
            ),
            truncated=self.truncated,
        )

    def merged(self, other: GraphStructure) -> GraphStructure:
        """Union two reads of the same graph, de-duplicated by physical identity."""
        return GraphStructure(
            datasources=_unique(self.datasources, other.datasources, "node_id"),
            data_objects=_unique(self.data_objects, other.data_objects, "node_id"),
            fields=_unique(self.fields, other.fields, "node_id"),
            relationships=_unique(self.relationships, other.relationships, "relationship_id"),
            truncated=self.truncated or other.truncated,
        ).sorted()

    def datasource_ids(self) -> list[str]:
        return sorted({item.datasource_id for item in self.datasources})

    def has_datasource(self, datasource_id: str) -> bool:
        return any(item.datasource_id == datasource_id for item in self.datasources)

    def match_datasource(
        self, *, datasource_id: str | None = None, datasource_name: str | None = None
    ) -> list[GraphDatasource]:
        """Datasources matching the reference; more than one result means the lookup is ambiguous."""
        if datasource_id:
            return [item for item in self.datasources if item.datasource_id == datasource_id]
        if datasource_name:
            return [item for item in self.datasources if item.name == datasource_name]
        return []

    def match_object(
        self, reference: GraphBindingReference, datasource_id: str | None = None
    ) -> list[GraphDataObject]:
        """Data objects matching the reference.

        Resolution by ``graph_data_object_id`` is exact and never falls back to names. Resolution by
        readable names is intentionally strict: ``namespace`` narrows the match, so ``public.orders``
        and ``archive.orders`` never collapse into each other, and an unterminated lookup returns
        every candidate instead of silently picking one.
        """
        if reference.graph_data_object_id:
            return [item for item in self.data_objects if item.node_id == reference.graph_data_object_id]
        expected_datasource_id = datasource_id or reference.datasource_id
        matches = []
        for item in self.data_objects:
            if expected_datasource_id and item.datasource_id != expected_datasource_id:
                continue
            if reference.datasource_name and item.datasource_id != _datasource_id_by_name(
                self.datasources, reference.datasource_name
            ):
                continue
            if reference.namespace and item.namespace != reference.namespace:
                continue
            if reference.qualified_name and item.qualified_name != reference.qualified_name:
                continue
            if reference.data_object_name and reference.data_object_name not in {
                item.name,
                item.qualified_name,
            }:
                continue
            matches.append(item)
        return matches

    def match_field(
        self, reference: GraphBindingReference, data_object: GraphDataObject
    ) -> list[GraphField]:
        """Fields of one resolved data object matching the reference."""
        if reference.graph_field_id:
            return [
                item
                for item in self.fields
                if item.node_id == reference.graph_field_id and item.object_id == data_object.node_id
            ]
        if not reference.field_path:
            return []
        return [
            item
            for item in self.fields
            if item.object_id == data_object.node_id and item.path == reference.field_path
        ]

    def object_by_id(self, object_id: str) -> GraphDataObject | None:
        return next((item for item in self.data_objects if item.node_id == object_id), None)

    def field_by_id(self, field_id: str) -> GraphField | None:
        return next((item for item in self.fields if item.node_id == field_id), None)

    def object_by_name(self, datasource_id: str, name: str) -> GraphDataObject | None:
        """First object with that name. Display convenience only, never binding resolution."""
        for item in self.data_objects:
            if item.datasource_id != datasource_id:
                continue
            if name in {item.name, item.qualified_name}:
                return item
        return None

    def field_by_path(
        self, datasource_id: str, object_name: str, field_path: str
    ) -> GraphField | None:
        """First field with that path. Display convenience only, never binding resolution."""
        object_ids = {
            item.node_id
            for item in self.data_objects
            if item.datasource_id == datasource_id
            and object_name in {item.name, item.qualified_name}
        }
        for item in self.fields:
            if item.object_id in object_ids and item.path == field_path:
                return item
        return None


def _datasource_id_by_name(datasources: list[GraphDatasource], name: str) -> str | None:
    matches = [item.datasource_id for item in datasources if item.name == name]
    return matches[0] if len(matches) == 1 else None


_T = TypeVar("_T")


def _unique(first: list[_T], second: list[_T], key: str) -> list[_T]:
    seen: dict[str, _T] = {}
    for item in (*first, *second):
        seen.setdefault(getattr(item, key), item)
    return list(seen.values())
