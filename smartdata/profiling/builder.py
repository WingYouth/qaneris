from __future__ import annotations

import hashlib
from collections import defaultdict
from typing import Any

from smartdata.contracts import DatasetInfo, DatasetSample, Datasource, RelationInfo
from smartdata.contracts.profile import (
    DataObjectKind,
    DataObjectProfile,
    DataSourceProfile,
    FieldProfile,
    MetadataOrigin,
    NamespaceProfile,
    RelationshipProfile,
    SamplePreview,
    ScanPolicy,
)
from smartdata.profiling.sample_extractor import extract_field_samples

_OBJECT_KINDS = {item.value: item for item in DataObjectKind}


def _stable_id(prefix: str, *parts: str) -> str:
    value = "\x1f".join(parts).encode()
    return f"{prefix}_{hashlib.sha256(value).hexdigest()[:16]}"


def _value_type(value: Any) -> tuple[str, bool]:
    if value is None:
        return "null", False
    if isinstance(value, bool):
        return "boolean", False
    if isinstance(value, int):
        return "integer", False
    if isinstance(value, float):
        return "number", False
    if isinstance(value, dict):
        return "object", False
    if isinstance(value, list):
        return "array", True
    return "string", False


def _flatten(value: Any, prefix: str = "") -> list[tuple[str, Any, bool]]:
    fields: list[tuple[str, Any, bool]] = []
    if isinstance(value, dict):
        for key, child in value.items():
            path = f"{prefix}.{key}" if prefix else str(key)
            fields.append((path, child, isinstance(child, list)))
            if isinstance(child, dict):
                fields.extend(_flatten(child, path))
            elif isinstance(child, list):
                for item in child:
                    if isinstance(item, dict):
                        fields.extend(_flatten(item, f"{path}[]"))
    return fields


class ProfileBuilder:
    def build(
        self,
        datasource: Datasource,
        datasets: list[DatasetInfo],
        relations: list[RelationInfo],
        previews: list[DatasetSample],
        profile_records: dict[str, list[dict[str, Any]]],
        policy: ScanPolicy,
    ) -> tuple[DataSourceProfile, list[RelationshipProfile]]:
        preview_by_name = {item.dataset: item.rows[: policy.preview_size] for item in previews}
        objects = [
            self._build_object(
                datasource,
                dataset,
                preview_by_name.get(dataset.name, []),
                profile_records.get(dataset.name, []),
                policy.field_sample_size,
            )
            for dataset in datasets[: policy.max_data_objects]
        ]
        object_ids = {item.name: item.id for item in objects}
        relationships = [
            RelationshipProfile(
                id=_stable_id(
                    "rel",
                    datasource.id,
                    relation.from_dataset,
                    relation.from_field or "",
                    relation.to_dataset,
                    relation.to_field or "",
                    relation.relation_type,
                ),
                from_datasource_id=datasource.id,
                from_object_id=object_ids[relation.from_dataset],
                from_field_path=relation.from_field,
                to_datasource_id=datasource.id,
                to_object_id=object_ids[relation.to_dataset],
                to_field_path=relation.to_field,
                relationship_type=relation.relation_type,
                origin=MetadataOrigin.DATABASE,
                confidence=1.0,
                confirmed=True,
            )
            for relation in relations
            if relation.from_dataset in object_ids and relation.to_dataset in object_ids
        ]
        if datasource.kind.value == "graph":
            objects.extend(self._graph_edge_objects(datasource, relationships))
        namespaces: dict[str, list[DataObjectProfile]] = defaultdict(list)
        for item in objects:
            namespaces[item.namespace or "default"].append(item)
        profile = DataSourceProfile(
            datasource_id=datasource.id,
            name=datasource.name,
            kind=datasource.kind,
            driver=datasource.driver or "unknown",
            namespaces=[
                NamespaceProfile(name=name, data_objects=data_objects)
                for name, data_objects in sorted(namespaces.items())
            ],
        )
        return profile, relationships

    def _build_object(
        self,
        datasource: Datasource,
        dataset: DatasetInfo,
        previews: list[dict[str, Any]],
        profile_records: list[dict[str, Any]],
        field_sample_size: int,
    ) -> DataObjectProfile:
        namespace, name = self._split_name(dataset.name)
        records = profile_records or previews
        observed: dict[str, list[Any]] = defaultdict(list)
        arrays: set[str] = set()
        for record in records:
            payload = record.get("value", record) if isinstance(record, dict) else {}
            record_values: dict[str, Any] = {}
            for path, value, is_array in _flatten(payload):
                record_values.setdefault(path, value)
                if is_array:
                    arrays.add(path)
            for path, value in record_values.items():
                observed[path].append(value)
        fields = []
        metadata_fields = {field.name: field for field in dataset.fields}
        for path in sorted(set(metadata_fields) | set(observed)):
            metadata = metadata_fields.get(path)
            values = observed.get(path, [])
            inferred = next((_value_type(value)[0] for value in values if value is not None), None)
            fields.append(
                FieldProfile(
                    name=path.rsplit(".", 1)[-1],
                    path=path,
                    data_type=metadata.data_type if metadata else inferred or "unknown",
                    native_type=metadata.data_type if metadata else None,
                    nullable=metadata.nullable if metadata else len(values) < len(records),
                    primary_key=metadata.primary_key if metadata else path == "_id",
                    array=path in arrays,
                    observed_count=len(values),
                    sample_count=len(records),
                    sample_values=extract_field_samples(path, values, limit=field_sample_size),
                )
            )
        family_kinds = {
            "key_value": DataObjectKind.KEY_GROUP,
            "document": DataObjectKind.COLLECTION,
            "wide_column": DataObjectKind.WIDE_COLUMN_TABLE,
            "vector": DataObjectKind.VECTOR_COLLECTION,
        }
        kind = family_kinds.get(
            datasource.kind.value, _OBJECT_KINDS.get(dataset.kind, DataObjectKind.TABLE)
        )
        return DataObjectProfile(
            id=_stable_id("obj", datasource.id, dataset.name),
            datasource_id=datasource.id,
            namespace=namespace,
            name=name,
            object_kind=kind,
            fields=fields,
            profiled_record_count=len(records),
            previews=[
                SamplePreview(values=row, redacted_fields=self._redacted_paths(row))
                for row in previews[:3]
            ],
        )

    @staticmethod
    def _redacted_paths(value: Any, prefix: str = "") -> list[str]:
        paths = []
        if isinstance(value, dict):
            for key, child in value.items():
                path = f"{prefix}.{key}" if prefix else str(key)
                if child == "<redacted>":
                    paths.append(path)
                else:
                    paths.extend(ProfileBuilder._redacted_paths(child, path))
        elif isinstance(value, list):
            for child in value:
                paths.extend(ProfileBuilder._redacted_paths(child, f"{prefix}[]"))
        return sorted(set(paths))

    @staticmethod
    def _split_name(name: str) -> tuple[str | None, str]:
        if "." not in name:
            return None, name
        return tuple(name.split(".", 1))  # type: ignore[return-value]

    @staticmethod
    def _graph_edge_objects(
        datasource: Datasource, relationships: list[RelationshipProfile]
    ) -> list[DataObjectProfile]:
        return [
            DataObjectProfile(
                id=_stable_id("edge", datasource.id, relation.relationship_type),
                datasource_id=datasource.id,
                name=relation.relationship_type,
                object_kind=DataObjectKind.EDGE_TYPE,
            )
            for relation in {item.relationship_type: item for item in relationships}.values()
        ]
