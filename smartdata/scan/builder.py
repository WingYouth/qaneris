from __future__ import annotations

import hashlib
from datetime import datetime
from typing import Any

from smartdata.contracts.datasource import DatasetInfo, Datasource, RelationInfo
from smartdata.scan.contracts import (
    ScanEdgeType,
    ScanGraph,
    ScanGraphEdge,
    ScanGraphNode,
    ScanNodeKind,
)


def _id(prefix: str, *parts: str) -> str:
    raw = "\x1f".join(parts).encode()
    return f"{prefix}_{hashlib.sha256(raw).hexdigest()[:24]}"


class ScanGraphBuilder:
    """Normalize adapter output into the storage-independent scan graph contract."""

    def build(
        self,
        datasource: Datasource,
        datasets: list[DatasetInfo],
        relations: list[RelationInfo],
        *,
        version: int,
        scanned_at: datetime,
    ) -> ScanGraph:
        database_id = _id("db", datasource.workspace_id, datasource.id)
        nodes = [
            ScanGraphNode(
                id=database_id,
                kind=ScanNodeKind.DATABASE,
                properties={
                    "workspace_id": datasource.workspace_id,
                    "datasource_id": datasource.id,
                    "name": datasource.name,
                    "kind": datasource.kind.value,
                    "driver": datasource.driver,
                    "scan_version": version,
                    "scanned_at": scanned_at.isoformat(),
                },
            )
        ]
        edges: list[ScanGraphEdge] = []
        object_ids: dict[str, str] = {}
        namespace_ids: dict[str, str] = {}

        for dataset in datasets:
            namespace, object_name = self._location(dataset)
            parent_id = database_id
            if namespace:
                namespace_id = namespace_ids.setdefault(
                    namespace, _id("ns", datasource.id, namespace)
                )
                if not any(node.id == namespace_id for node in nodes):
                    nodes.append(
                        ScanGraphNode(
                            id=namespace_id,
                            kind=ScanNodeKind.NAMESPACE,
                            properties={"datasource_id": datasource.id, "name": namespace},
                        )
                    )
                    edges.append(self._edge(ScanEdgeType.HAS_NAMESPACE, database_id, namespace_id))
                parent_id = namespace_id

            object_id = _id("obj", datasource.id, dataset.name)
            object_ids[dataset.name] = object_id
            nodes.append(
                ScanGraphNode(
                    id=object_id,
                    kind=ScanNodeKind.DATA_OBJECT,
                    properties={
                        "datasource_id": datasource.id,
                        "namespace": namespace,
                        "name": object_name,
                        "qualified_name": dataset.name,
                        "object_kind": dataset.kind,
                        "comment": dataset.comment,
                        "estimated_record_count": dataset.estimated_record_count,
                    },
                )
            )
            edges.append(self._edge(ScanEdgeType.CONTAINS, parent_id, object_id))
            for field in dataset.fields:
                field_id = _id("field", object_id, field.name)
                nodes.append(
                    ScanGraphNode(
                        id=field_id,
                        kind=ScanNodeKind.FIELD,
                        properties={
                            "object_id": object_id,
                            "path": field.name,
                            **field.model_dump(),
                        },
                    )
                )
                edges.append(self._edge(ScanEdgeType.HAS_FIELD, object_id, field_id))
            for index in dataset.indexes:
                index_id = _id("idx", object_id, index.name)
                nodes.append(
                    ScanGraphNode(
                        id=index_id,
                        kind=ScanNodeKind.INDEX,
                        properties={"object_id": object_id, **index.model_dump()},
                    )
                )
                edges.append(self._edge(ScanEdgeType.HAS_INDEX, object_id, index_id))
            for constraint in dataset.constraints:
                constraint_id = _id("con", object_id, constraint.name)
                nodes.append(
                    ScanGraphNode(
                        id=constraint_id,
                        kind=ScanNodeKind.CONSTRAINT,
                        properties={"object_id": object_id, **constraint.model_dump()},
                    )
                )
                edges.append(self._edge(ScanEdgeType.HAS_CONSTRAINT, object_id, constraint_id))

        for relation in relations:
            from_id = object_ids.get(relation.from_dataset)
            to_id = object_ids.get(relation.to_dataset)
            if from_id is None or to_id is None:
                raise ValueError(f"relationship references an unknown object: {relation}")
            source = "database" if relation.source in {"scan", "database"} else relation.source
            if source not in {"database", "configuration"}:
                raise ValueError(f"untrusted relationship source: {relation.source}")
            edges.append(
                self._edge(
                    ScanEdgeType.RELATES_TO,
                    from_id,
                    to_id,
                    relationship_type=relation.relation_type,
                    source=source,
                    from_field_path=relation.from_field,
                    to_field_path=relation.to_field,
                    directed=True,
                    confirmed=True,
                )
            )
        return ScanGraph(
            workspace_id=datasource.workspace_id,
            datasource_id=datasource.id,
            scan_version=version,
            scanned_at=scanned_at,
            nodes=nodes,
            edges=edges,
        )

    @staticmethod
    def _location(dataset: DatasetInfo) -> tuple[str | None, str]:
        if dataset.namespace:
            return dataset.namespace, dataset.name.rsplit(".", 1)[-1]
        if "." in dataset.name:
            return tuple(dataset.name.rsplit(".", 1))  # type: ignore[return-value]
        return None, dataset.name

    @staticmethod
    def _edge(
        edge_type: ScanEdgeType, from_id: str, to_id: str, **properties: Any
    ) -> ScanGraphEdge:
        return ScanGraphEdge(
            id=_id("edge", edge_type.value, from_id, to_id, repr(sorted(properties.items()))),
            type=edge_type,
            from_node_id=from_id,
            to_node_id=to_id,
            properties=properties,
        )
