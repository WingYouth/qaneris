"""In-memory Neo4j stand-in for graph publish/read integration tests.

It stores the same node and edge shape that ``Neo4jGraphStore`` writes, and answers exactly the
queries that ``Neo4jGraphReader`` issues. Any unsupported statement fails loudly instead of silently
returning nothing, so a reader query change cannot pass unnoticed.
"""

from __future__ import annotations

from copy import deepcopy
from typing import Any, Self

from smartdata.graph.neo4j import Neo4jGraphReader, Neo4jGraphStore


class _Result:
    def __init__(self, records: list[dict[str, Any]] | None = None):
        self.records = records or []

    def __iter__(self):
        return iter(self.records)

    def __len__(self) -> int:
        return len(self.records)

    def consume(self) -> _Result:
        return self


class _Transaction:
    """Applies the write statements issued by ``Neo4jGraphStore._replace``."""

    def __init__(
        self,
        state: dict[str, Any],
        fail_after: int | None = None,
        writes: list[tuple[str, dict[str, Any]]] | None = None,
    ):
        self.state = state
        self.fail_after = fail_after
        self.operations = 0
        self.writes = writes if writes is not None else []

    def run(self, query: str, **parameters: Any) -> _Result:
        self.operations += 1
        self.writes.append((query, dict(parameters)))
        if self.fail_after is not None and self.operations > self.fail_after:
            raise RuntimeError("simulated Neo4j transaction failure")
        if query.startswith("MATCH (d:Database {"):
            self._delete_datasource(parameters["datasource_id"])
        elif query.startswith("CREATE (n:"):
            label = query.split("CREATE (n:", 1)[1].split(" ", 1)[0]
            self.state["nodes"][parameters["id"]] = {
                "label": label,
                "id": parameters["id"],
                **parameters["properties"],
            }
        elif query.startswith("MATCH (a"):
            edge_type = query.split("CREATE (a)-[r:", 1)[1].split(" ", 1)[0]
            self.state["edges"][parameters["id"]] = {
                "type": edge_type,
                "id": parameters["id"],
                "from": parameters["from_id"],
                "to": parameters["to_id"],
                **parameters["properties"],
            }
        else:
            raise AssertionError(f"unsupported write statement: {query}")
        return _Result()

    def _delete_datasource(self, datasource_id: str) -> None:
        owned = {
            identifier
            for identifier, node in self.state["nodes"].items()
            if node.get("label") == "Database" and node.get("datasource_id") == datasource_id
        }
        while True:
            discovered = {
                edge["to"] for edge in self.state["edges"].values() if edge["from"] in owned
            }
            if discovered <= owned:
                break
            owned.update(discovered)
        self.state["nodes"] = {
            identifier: node
            for identifier, node in self.state["nodes"].items()
            if identifier not in owned
        }
        self.state["edges"] = {
            identifier: edge
            for identifier, edge in self.state["edges"].items()
            if edge["from"] not in owned and edge["to"] not in owned
        }


class _Session:
    def __init__(self, driver: FakeNeo4jDriver):
        self.driver = driver

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *_: object) -> None:
        return None

    def close(self) -> None:
        return None

    def run(self, query: str, **parameters: Any) -> _Result:
        if self.driver.fail_read_with is not None:
            raise self.driver.fail_read_with
        return self.driver.read(query, parameters)

    def execute_write(self, callback, *args: Any) -> None:
        candidate = deepcopy(self.driver.state)
        writes: list[tuple[str, dict[str, Any]]] = []
        callback(_Transaction(candidate, self.driver.fail_after, writes), *args)
        self.driver.state = candidate
        self.driver.writes.extend(writes)


class FakeNeo4jDriver:
    def __init__(self) -> None:
        self.state: dict[str, Any] = {"nodes": {}, "edges": {}}
        self.schema_statements: list[str] = []
        self.reads: list[tuple[str, dict[str, Any]]] = []
        #: Every write statement this driver was asked to run, in order, so a test can assert on
        #: the exact query text and bound parameters.
        self.writes: list[tuple[str, dict[str, Any]]] = []
        self.fail_after: int | None = None
        self.fail_read_with: BaseException | None = None

    @property
    def nodes(self) -> dict[str, dict[str, Any]]:
        return self.state["nodes"]

    @property
    def edges(self) -> dict[str, dict[str, Any]]:
        return self.state["edges"]

    def session(self, **_: Any) -> _Session:
        return _Session(self)

    def close(self) -> None:
        return None

    def read_parameters(self, marker: str) -> dict[str, Any]:
        """Parameters the reader passed for the read statement containing ``marker``."""
        for statement, parameters in self.reads:
            if marker in statement:
                return parameters
        raise AssertionError(f"no read statement contained: {marker}")

    def inject_field_property(self, field_name: str, key: str, value: Any) -> None:
        """Simulate a Field node carrying extra properties the read model must never project."""
        for node in self.nodes.values():
            if node.get("label") == "Field" and node.get("name") == field_name:
                node[key] = value

    def read(self, query: str, parameters: dict[str, Any]) -> _Result:
        statement = query.strip()
        self.reads.append((statement, parameters))
        if "CREATE CONSTRAINT" in statement or "CREATE INDEX" in statement:
            self.schema_statements.append(statement)
            return _Result()
        if "*1..2]->(o:DataObject)-[:HAS_FIELD]" in statement:
            return _Result(self._reference_fields(parameters))
        if "*1..2]->(o:DataObject)" in statement:
            return _Result(self._reference_objects(parameters))
        if statement.startswith("MATCH (d:Database)"):
            return _Result(self._datasources(parameters))
        if "-[r:RELATES_TO]->" in statement:
            return _Result(self._relationships(parameters))
        if "-[:HAS_FIELD]->" in statement:
            return _Result(self._fields(parameters))
        if statement.startswith("MATCH (o:DataObject)"):
            return _Result(self._data_objects(parameters))
        raise AssertionError(f"unsupported read statement: {statement}")

    def _workspace_datasource_ids(
        self, workspace_id: str, scoped_datasource_ids: list[str]
    ) -> set[str]:
        """Datasources reachable from the workspace's own published Database nodes."""
        found = {
            node.get("datasource_id")
            for node in self.nodes.values()
            if node.get("label") == "Database" and node.get("workspace_id") == workspace_id
        }
        return found & set(scoped_datasource_ids) if scoped_datasource_ids else found

    def _datasources(self, parameters: dict[str, Any]) -> list[dict[str, Any]]:
        requested = parameters["datasource_id"]
        records = [
            {
                "node_id": node["id"],
                "datasource_id": node.get("datasource_id"),
                "workspace_id": node.get("workspace_id"),
                "name": node.get("name"),
                "kind": node.get("kind"),
                "driver": node.get("driver"),
                "database_name": node.get("database_name"),
                "scan_version": node.get("scan_version"),
            }
            for node in self.nodes.values()
            if node.get("label") == "Database"
            and node.get("workspace_id") == parameters["workspace_id"]
            and (requested is None or node.get("datasource_id") == requested)
        ]
        records.sort(key=lambda item: (item["datasource_id"], item["node_id"]))
        return records[: parameters["limit"]]

    def _data_objects(self, parameters: dict[str, Any]) -> list[dict[str, Any]]:
        datasource_ids = set(parameters["datasource_ids"])
        records = [
            _object_record(node)
            for node in self.nodes.values()
            if node.get("label") == "DataObject" and node.get("datasource_id") in datasource_ids
        ]
        records.sort(key=lambda item: (item["datasource_id"], item["name"], item["node_id"]))
        return records[: parameters["limit"]]

    def _fields(self, parameters: dict[str, Any]) -> list[dict[str, Any]]:
        """Fields of the selected data object window, bounded by the query limit."""
        object_ids = set(parameters["object_ids"])
        records = [
            _field_record(object_node, field_node)
            for object_node, field_node in self._field_pairs()
            if object_node.get("id") in object_ids
        ]
        records.sort(key=lambda item: (item["object_id"], item["path"], item["node_id"]))
        return records[: parameters["limit"]]

    def _relationships(self, parameters: dict[str, Any]) -> list[dict[str, Any]]:
        datasource_ids = set(parameters["datasource_ids"])
        records = []
        for edge in self.edges.values():
            if edge.get("type") != "RELATES_TO":
                continue
            source = self.nodes.get(edge["from"], {})
            target = self.nodes.get(edge["to"], {})
            if source.get("datasource_id") not in datasource_ids:
                continue
            if target.get("datasource_id") not in datasource_ids:
                continue
            records.append(
                {
                    "relationship_id": edge["id"],
                    "relationship_type": edge.get("relationship_type"),
                    "source": edge.get("source"),
                    "directed": edge.get("directed", True),
                    "confirmed": edge.get("confirmed", True),
                    "from_field_path": edge.get("from_field_path"),
                    "to_field_path": edge.get("to_field_path"),
                    "from_object_id": source["id"],
                    "from_datasource_id": source.get("datasource_id"),
                    "from_object_name": source.get("name"),
                    "to_object_id": target["id"],
                    "to_datasource_id": target.get("datasource_id"),
                    "to_object_name": target.get("name"),
                }
            )
        records.sort(key=lambda item: (item["from_object_id"], item["relationship_id"]))
        return records[: parameters["limit"]]

    def _reference_objects(self, parameters: dict[str, Any]) -> list[dict[str, Any]]:
        """Mirrors the reader's workspace-scoped OR of exact ids and readable lookup conditions."""
        workspace_ids = self._workspace_datasource_ids(
            parameters["workspace_id"], parameters["scoped_datasource_ids"]
        )
        reference_ids = set(parameters["reference_datasource_ids"])
        object_ids = set(parameters["object_ids"])
        object_names = set(parameters["object_names"])
        qualified_names = set(parameters["qualified_names"])
        records = []
        for node in self.nodes.values():
            if node.get("label") != "DataObject":
                continue
            # The workspace boundary comes first: a graph id cannot escape its workspace.
            if node.get("datasource_id") not in workspace_ids:
                continue
            if node.get("id") in object_ids:
                records.append(_object_record(node))
                continue
            if node.get("datasource_id") not in reference_ids:
                continue
            if node.get("name") in object_names or node.get("qualified_name") in qualified_names:
                records.append(_object_record(node))
        return _deduplicate(records, "node_id")

    def _reference_fields(self, parameters: dict[str, Any]) -> list[dict[str, Any]]:
        workspace_ids = self._workspace_datasource_ids(
            parameters["workspace_id"], parameters["scoped_datasource_ids"]
        )
        reference_ids = set(parameters["reference_datasource_ids"])
        field_ids = set(parameters["field_ids"])
        field_paths = set(parameters["field_paths"])
        object_names = set(parameters["object_names"])
        qualified_names = set(parameters["qualified_names"])
        records = []
        for object_node, field_node in self._field_pairs():
            if object_node.get("datasource_id") not in workspace_ids:
                continue
            if field_node.get("id") in field_ids:
                records.append(_field_record(object_node, field_node))
                continue
            if object_node.get("datasource_id") not in reference_ids:
                continue
            if field_node.get("path") not in field_paths:
                continue
            if object_node.get("name") in object_names or object_node.get(
                "qualified_name"
            ) in qualified_names:
                records.append(_field_record(object_node, field_node))
        return _deduplicate(records, "node_id")

    def _field_pairs(self) -> list[tuple[dict[str, Any], dict[str, Any]]]:
        pairs = []
        for edge in self.edges.values():
            if edge.get("type") != "HAS_FIELD":
                continue
            object_node = self.nodes.get(edge["from"])
            field_node = self.nodes.get(edge["to"])
            if object_node is None or field_node is None:
                continue
            pairs.append((object_node, field_node))
        return pairs


def _object_record(node: dict[str, Any]) -> dict[str, Any]:
    return {
        "node_id": node["id"],
        "datasource_id": node.get("datasource_id"),
        "namespace": node.get("namespace"),
        "name": node.get("name"),
        "qualified_name": node.get("qualified_name"),
        "object_kind": node.get("object_kind"),
        "comment": node.get("comment"),
        "description": node.get("description"),
        "estimated_record_count": node.get("estimated_record_count"),
    }


def _field_record(object_node: dict[str, Any], field_node: dict[str, Any]) -> dict[str, Any]:
    return {
        "object_id": object_node["id"],
        "datasource_id": object_node.get("datasource_id"),
        "object_name": object_node.get("name"),
        "node_id": field_node["id"],
        "name": field_node.get("name"),
        "path": field_node.get("path"),
        "data_type": field_node.get("data_type"),
        "native_type": field_node.get("native_type"),
        "nullable": field_node.get("nullable"),
        "primary_key": field_node.get("primary_key", False),
        "unique": field_node.get("unique", False),
        "indexed": field_node.get("indexed", False),
        "comment": field_node.get("comment"),
        "description": field_node.get("description"),
    }


def _deduplicate(records: list[dict[str, Any]], key: str) -> list[dict[str, Any]]:
    unique: dict[Any, dict[str, Any]] = {}
    for record in records:
        unique.setdefault(record[key], record)
    return sorted(unique.values(), key=lambda item: item[key])


def graph_store(driver: FakeNeo4jDriver) -> Neo4jGraphStore:
    store = object.__new__(Neo4jGraphStore)
    store._driver = driver
    store._database = "neo4j"
    return store


def graph_reader(driver: FakeNeo4jDriver) -> Neo4jGraphReader:
    reader = object.__new__(Neo4jGraphReader)
    reader._driver = driver
    reader._database = "neo4j"
    return reader
