from __future__ import annotations

import json
from collections.abc import Iterable
from typing import Any

from qaneris.common.errors import GraphUnavailableError
from qaneris.graph.reading import (
    GraphDataObject,
    GraphDatasource,
    GraphField,
    GraphRelationship,
    GraphStructure,
    GraphStructureRequest,
)
from qaneris.graph.schema import GRAPH_INDEXES, NODE_CONSTRAINTS
from qaneris.scan.contracts import ScanGraph

_MAX_DATASOURCES = 500

#: The one ownership traversal this store deletes a datasource's graph with. Publication uses it to
#: clear a stale graph before writing a new revision and deletion uses it to publish a removal, so
#: the two can never disagree about what one datasource owns. The datasource id is only ever bound
#: as a parameter; it is never interpolated into the statement.
_DELETE_DATASOURCE_GRAPH = (
    "MATCH (d:Database {datasource_id: $datasource_id}) "
    "OPTIONAL MATCH (d)-[*0..]->(owned) WITH DISTINCT owned DETACH DELETE owned"
)


def _neo4j_properties(properties: dict[str, Any]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in properties.items():
        if value is None:
            continue
        if (
            isinstance(value, (str, int, float, bool))
            or isinstance(value, list)
            and all(isinstance(item, (str, int, float, bool)) for item in value)
        ):
            result[key] = value
        else:
            result[key] = json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)
    return result


def _graph_driver(uri: str, username: str, password: str) -> Any:
    try:
        from neo4j import GraphDatabase
    except ImportError as error:
        raise RuntimeError("Neo4j graph access requires the 'neo4j' project extra") from error
    return GraphDatabase.driver(uri, auth=(username, password))


class Neo4jGraphStore:
    """Neo4j graph store with one-transaction validation, replacement and publication."""

    def __init__(self, uri: str, username: str, password: str, database: str = "neo4j"):
        self._driver = _graph_driver(uri, username, password)
        self._database = database

    def close(self) -> None:
        self._driver.close()

    def ensure_schema(self) -> None:
        with self._driver.session(database=self._database) as session:
            for statement in (*NODE_CONSTRAINTS, *GRAPH_INDEXES):
                session.run(statement).consume()

    def replace_datasource_graph(self, graph: ScanGraph) -> None:
        nodes = [
            {
                "id": node.id,
                "label": node.kind.value,
                "properties": _neo4j_properties(node.properties),
            }
            for node in graph.nodes
        ]
        edges = [
            {
                "id": edge.id,
                "type": edge.type.value,
                "from": edge.from_node_id,
                "to": edge.to_node_id,
                "properties": _neo4j_properties(edge.properties),
            }
            for edge in graph.edges
        ]
        with self._driver.session(database=self._database) as session:
            session.execute_write(self._replace, graph.datasource_id, nodes, edges)

    def delete_datasource_graph(self, datasource_id: str) -> None:
        """Remove one datasource's published graph in a single transaction.

        A store failure raises as itself rather than being converted here: the caller decides what a
        failed deletion means, and for datasource deletion it means the whole operation aborts
        before the catalog is touched.
        """
        with self._driver.session(database=self._database) as session:
            session.execute_write(self._delete, datasource_id)

    @staticmethod
    def _delete(tx: Any, datasource_id: str) -> None:
        tx.run(_DELETE_DATASOURCE_GRAPH, datasource_id=datasource_id).consume()

    @staticmethod
    def _replace(
        tx: Any, datasource_id: str, nodes: list[dict[str, Any]], edges: list[dict[str, Any]]
    ) -> None:
        # All mutations occur in one transaction: a failed scan/write leaves the prior graph intact.
        tx.run(_DELETE_DATASOURCE_GRAPH, datasource_id=datasource_id).consume()
        for node in nodes:
            # Labels and relationship types come only from closed enums in ScanGraph.
            tx.run(
                f"CREATE (n:{node['label']} {{id: $id}}) SET n += $properties",
                id=node["id"],
                properties=node["properties"],
            ).consume()
        for edge in edges:
            tx.run(
                f"MATCH (a {{id: $from_id}}), (b {{id: $to_id}}) "
                f"CREATE (a)-[r:{edge['type']} {{id: $id}}]->(b) SET r += $properties",
                from_id=edge["from"],
                to_id=edge["to"],
                id=edge["id"],
                properties=edge["properties"],
            ).consume()


_DATASOURCE_QUERY = """
MATCH (d:Database)
WHERE d.workspace_id = $workspace_id
  AND ($datasource_id IS NULL OR d.datasource_id = $datasource_id)
RETURN d.id AS node_id, d.datasource_id AS datasource_id, d.workspace_id AS workspace_id,
       d.name AS name, d.kind AS kind, d.driver AS driver,
       d.database_name AS database_name, d.scan_version AS scan_version
ORDER BY datasource_id, node_id
LIMIT $limit
"""

_DATA_OBJECT_QUERY = """
MATCH (o:DataObject)
WHERE o.datasource_id IN $datasource_ids
RETURN o.id AS node_id, o.datasource_id AS datasource_id, o.namespace AS namespace,
       o.name AS name, o.qualified_name AS qualified_name, o.object_kind AS object_kind,
       o.comment AS comment, o.description AS description,
       o.estimated_record_count AS estimated_record_count
ORDER BY datasource_id, name, node_id
LIMIT $limit
"""

_FIELD_QUERY = """
MATCH (o:DataObject)-[:HAS_FIELD]->(f:Field)
WHERE o.id IN $object_ids
RETURN o.id AS object_id, o.datasource_id AS datasource_id, o.name AS object_name,
       f.id AS node_id, f.name AS name, f.path AS path, f.data_type AS data_type,
       f.native_type AS native_type, f.nullable AS nullable, f.primary_key AS primary_key,
       f.unique AS unique, f.indexed AS indexed, f.comment AS comment,
       coalesce(f.description, '') AS description
ORDER BY object_id, path, node_id
LIMIT $limit
"""

_RELATIONSHIP_QUERY = """
MATCH (a:DataObject)-[r:RELATES_TO]->(b:DataObject)
WHERE a.datasource_id IN $datasource_ids AND b.datasource_id IN $datasource_ids
RETURN r.id AS relationship_id, r.relationship_type AS relationship_type,
       r.source AS source, r.directed AS directed, r.confirmed AS confirmed,
       r.from_field_path AS from_field_path, r.to_field_path AS to_field_path,
       a.id AS from_object_id, a.datasource_id AS from_datasource_id,
       a.name AS from_object_name,
       b.id AS to_object_id, b.datasource_id AS to_datasource_id,
       b.name AS to_object_name
ORDER BY from_object_id, relationship_id
LIMIT $limit
"""

_REFERENCE_OBJECT_QUERY = """
MATCH (d:Database)-[:CONTAINS|HAS_NAMESPACE*1..2]->(o:DataObject)
WHERE d.workspace_id = $workspace_id
  AND ($scoped_datasource_ids = [] OR d.datasource_id IN $scoped_datasource_ids)
  AND (o.id IN $object_ids
       OR (o.datasource_id IN $reference_datasource_ids
           AND (o.name IN $object_names OR o.qualified_name IN $qualified_names)))
RETURN DISTINCT o.id AS node_id, o.datasource_id AS datasource_id, o.namespace AS namespace,
       o.name AS name, o.qualified_name AS qualified_name, o.object_kind AS object_kind,
       o.comment AS comment, o.description AS description,
       o.estimated_record_count AS estimated_record_count
ORDER BY node_id
"""

_REFERENCE_FIELD_QUERY = """
MATCH (d:Database)-[:CONTAINS|HAS_NAMESPACE*1..2]->(o:DataObject)-[:HAS_FIELD]->(f:Field)
WHERE d.workspace_id = $workspace_id
  AND ($scoped_datasource_ids = [] OR d.datasource_id IN $scoped_datasource_ids)
  AND (f.id IN $field_ids
       OR (o.datasource_id IN $reference_datasource_ids
           AND f.path IN $field_paths
           AND (o.name IN $object_names OR o.qualified_name IN $qualified_names)))
RETURN DISTINCT o.id AS object_id, o.datasource_id AS datasource_id, o.name AS object_name,
       f.id AS node_id, f.name AS name, f.path AS path, f.data_type AS data_type,
       f.native_type AS native_type, f.nullable AS nullable, f.primary_key AS primary_key,
       f.unique AS unique, f.indexed AS indexed, f.comment AS comment,
       coalesce(f.description, '') AS description
ORDER BY object_id, path, node_id
"""

_UNAVAILABLE_DRIVER_EXCEPTIONS = (
    # The service could not be reached, or the session was lost.
    "ServiceUnavailable",
    "SessionExpired",
    # The driver could not obtain a usable connection.
    "ConnectionPoolError",
    "ConnectionAcquisitionTimeoutError",
    # The driver configuration itself cannot reach a backend.
    "ConfigurationError",
    # Authentication / authorization failure.
    "AuthError",
    # The target database is not available for reads.
    "DatabaseUnavailable",
)


def unavailable_driver_exceptions() -> tuple[type[BaseException], ...]:
    """Driver exceptions that mean "the graph backend cannot be read".

    Deliberately narrow. ``DriverError`` is *not* caught as a whole because it also covers code-usage
    and result-handling errors such as ``ResultError``, ``TransactionError`` and
    ``BrokenRecordError``. Server-returned query errors (for example ``CypherSyntaxError``) are our own
    contract bugs and must surface as themselves.

    Names are resolved dynamically so a driver version that lacks one of them simply contributes
    nothing (the project supports ``neo4j>=5,<7``).
    """
    try:
        from neo4j import exceptions
    except ImportError:  # pragma: no cover - the extra is optional at runtime
        return ()
    return tuple(
        item
        for name in _UNAVAILABLE_DRIVER_EXCEPTIONS
        if (item := getattr(exceptions, name, None)) is not None
    )


class Neo4jGraphReader:
    """Read side of the Neo4j enterprise data graph.

    The reader only projects published structural metadata. It never returns observed business
    values, and it never derives a relationship that the graph does not already contain.
    """

    def __init__(self, uri: str, username: str, password: str, database: str = "neo4j"):
        self._driver = _graph_driver(uri, username, password)
        self._database = database

    def close(self) -> None:
        self._driver.close()

    def read_structure(self, request: GraphStructureRequest) -> GraphStructure:
        try:
            return self._read(request)
        except unavailable_driver_exceptions() as error:
            raise GraphUnavailableError(
                "图后端不可用：无法从 Neo4j 企业数据图读取结构"
                f"（{type(error).__name__}）"
            ) from error

    def _read(self, request: GraphStructureRequest) -> GraphStructure:
        with self._driver.session(database=self._database) as session:
            datasources = self._datasources(session, request)
            if not datasources:
                return GraphStructure()
            datasource_ids = sorted({item.datasource_id for item in datasources})
            objects, objects_truncated = self._data_objects(
                session, datasource_ids, request.max_data_objects
            )
            fields, fields_truncated = self._fields(
                session, [item.node_id for item in objects], request.max_fields
            )
            relationships, relationships_truncated = self._relationships(
                session, datasource_ids, request.max_relationships
            )
            referenced = self._resolve_references(session, request)
        bounded = GraphStructure(
            datasources=datasources,
            data_objects=objects,
            fields=fields,
            relationships=relationships,
            truncated=objects_truncated or fields_truncated or relationships_truncated,
        )
        return bounded.merged(referenced)

    def _datasources(
        self, session: Any, request: GraphStructureRequest
    ) -> list[GraphDatasource]:
        records = session.run(
            _DATASOURCE_QUERY,
            workspace_id=request.workspace_id,
            datasource_id=request.datasource_id,
            limit=_MAX_DATASOURCES,
        )
        return [
            GraphDatasource(
                node_id=_text(record, "node_id"),
                datasource_id=_text(record, "datasource_id"),
                workspace_id=_text(record, "workspace_id"),
                name=_text(record, "name"),
                kind=_optional_text(record, "kind"),
                driver=_optional_text(record, "driver"),
                database_name=_optional_text(record, "database_name"),
                scan_version=_optional_int(record, "scan_version"),
            )
            for record in records
        ]

    @staticmethod
    def _data_objects(
        session: Any, datasource_ids: list[str], limit: int
    ) -> tuple[list[GraphDataObject], bool]:
        records = list(
            session.run(_DATA_OBJECT_QUERY, datasource_ids=datasource_ids, limit=limit + 1)
        )
        truncated = len(records) > limit
        return (
            [
                GraphDataObject(
                    node_id=_text(record, "node_id"),
                    datasource_id=_text(record, "datasource_id"),
                    namespace=_optional_text(record, "namespace"),
                    name=_text(record, "name"),
                    qualified_name=_optional_text(record, "qualified_name"),
                    object_kind=_optional_text(record, "object_kind"),
                    comment=_optional_text(record, "comment"),
                    description=_optional_text(record, "description"),
                    estimated_record_count=_optional_int(record, "estimated_record_count"),
                )
                for record in records[:limit]
            ],
            truncated,
        )

    @staticmethod
    def _fields(
        session: Any, object_ids: list[str], limit: int
    ) -> tuple[list[GraphField], bool]:
        """Read the field window of the already selected data object window.

        The bound is applied in the query itself, so unselected objects can never consume the field
        window.
        """
        if not object_ids:
            return [], False
        records = list(session.run(_FIELD_QUERY, object_ids=object_ids, limit=limit + 1))
        truncated = len(records) > limit
        return ([_field(item) for item in records[:limit]], truncated)

    @staticmethod
    def _relationships(
        session: Any, datasource_ids: list[str], limit: int
    ) -> tuple[list[GraphRelationship], bool]:
        records = list(
            session.run(_RELATIONSHIP_QUERY, datasource_ids=datasource_ids, limit=limit + 1)
        )
        truncated = len(records) > limit
        return (
            [
                GraphRelationship(
                    relationship_id=_text(record, "relationship_id"),
                    relationship_type=_text(record, "relationship_type"),
                    from_object_id=_text(record, "from_object_id"),
                    from_datasource_id=_text(record, "from_datasource_id"),
                    from_object_name=_text(record, "from_object_name"),
                    from_field_path=_optional_text(record, "from_field_path"),
                    to_object_id=_text(record, "to_object_id"),
                    to_datasource_id=_text(record, "to_datasource_id"),
                    to_object_name=_text(record, "to_object_name"),
                    to_field_path=_optional_text(record, "to_field_path"),
                    source=_optional_text(record, "source"),
                    directed=_bool(record, "directed", True),
                    confirmed=_bool(record, "confirmed", True),
                )
                for record in records[:limit]
            ],
            truncated,
        )

    @staticmethod
    def _resolve_references(session: Any, request: GraphStructureRequest) -> GraphStructure:
        """Resolve requested bindings exactly, inside the workspace (and datasource) scope.

        Reference resolution deliberately carries no ``LIMIT``: exact physical binding resolution must
        never be dropped because of the bounded-context ``max_*`` settings. It does carry the same
        workspace boundary as every other read: a graph id from another workspace must not resolve,
        and the traversal goes through the workspace's own published ``Database`` nodes rather than
        trusting object names.
        """
        if not request.references:
            return GraphStructure()
        parameters = _reference_parameters(request)
        object_records = list(session.run(_REFERENCE_OBJECT_QUERY, **parameters))
        field_records = list(session.run(_REFERENCE_FIELD_QUERY, **parameters))
        return GraphStructure(
            data_objects=[
                GraphDataObject(
                    node_id=_text(record, "node_id"),
                    datasource_id=_text(record, "datasource_id"),
                    namespace=_optional_text(record, "namespace"),
                    name=_text(record, "name"),
                    qualified_name=_optional_text(record, "qualified_name"),
                    object_kind=_optional_text(record, "object_kind"),
                    comment=_optional_text(record, "comment"),
                    description=_optional_text(record, "description"),
                    estimated_record_count=_optional_int(record, "estimated_record_count"),
                )
                for record in object_records
            ],
            fields=[_field(record) for record in field_records],
        )


def _field(record: Any) -> GraphField:
    return GraphField(
        node_id=_text(record, "node_id"),
        object_id=_text(record, "object_id"),
        datasource_id=_text(record, "datasource_id"),
        object_name=_text(record, "object_name"),
        name=_text(record, "name"),
        path=_text(record, "path"),
        data_type=_optional_text(record, "data_type"),
        native_type=_optional_text(record, "native_type"),
        nullable=_optional_bool(record, "nullable"),
        primary_key=_bool(record, "primary_key", False),
        unique=_bool(record, "unique", False),
        indexed=_bool(record, "indexed", False),
        comment=_optional_text(record, "comment"),
        description=_optional_text(record, "description"),
    )


def _reference_parameters(request: GraphStructureRequest) -> dict[str, Any]:
    """Group reference identities into the parameter lists the resolution queries expect."""
    references = request.references
    return {
        "workspace_id": request.workspace_id,
        "scoped_datasource_ids": [request.datasource_id] if request.datasource_id else [],
        "reference_datasource_ids": _values(
            reference.datasource_id for reference in references
        ),
        "object_ids": _values(reference.graph_data_object_id for reference in references),
        "field_ids": _values(reference.graph_field_id for reference in references),
        "object_names": _values(reference.data_object_name for reference in references),
        "qualified_names": _values(reference.qualified_name for reference in references),
        "field_paths": _values(reference.field_path for reference in references),
    }


def _values(values: Iterable[str | None]) -> list[str]:
    return sorted({value for value in values if value})


def _text(record: Any, name: str) -> str:
    value = record.get(name)
    if value is None:
        raise ValueError(f"graph record is missing required property: {name}")
    return str(value)


def _optional_text(record: Any, name: str) -> str | None:
    value = record.get(name)
    return None if value is None else str(value)


def _optional_int(record: Any, name: str) -> int | None:
    value = record.get(name)
    return None if value is None else int(value)


def _bool(record: Any, name: str, default: bool) -> bool:
    value = record.get(name)
    return default if value is None else bool(value)


def _optional_bool(record: Any, name: str) -> bool | None:
    value = record.get(name)
    return None if value is None else bool(value)
