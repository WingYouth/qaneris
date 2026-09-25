"""Schema context construction for query retrieval."""

from __future__ import annotations

import re
from collections import defaultdict
from typing import Any

from qaneris.catalog.ports import SchemaCatalogReader


def _canonical_name(value: str) -> str:
    return re.sub(r"[^a-z0-9\u4e00-\u9fff]+", "_", value.lower()).strip("_")


def build_schema_context(
    catalog: SchemaCatalogReader, workspace_id: str = "default"
) -> dict[str, Any]:
    datasources = catalog.list_datasources(workspace_id)
    datasets = catalog.list_datasets(workspace_id)
    relations = catalog.list_relations(workspace_id)
    samples = catalog.list_samples(workspace_id)
    fields_by_name: dict[str, list[dict[str, str]]] = defaultdict(list)
    for dataset in datasets:
        for field in dataset.fields:
            fields_by_name[_canonical_name(field.name)].append(
                {
                    "datasource_id": dataset.datasource_id,
                    "dataset": dataset.name,
                    "field": field.name,
                    "data_type": field.data_type,
                }
            )
    mapping_candidates = []
    for canonical_field, fields in sorted(fields_by_name.items()):
        if len({field["datasource_id"] for field in fields}) < 2:
            continue
        mapping_candidates.append(
            {
                "canonical_field": canonical_field,
                "confidence": 0.8,
                "source": "exact_field_name",
                "fields": fields,
            }
        )
    return {
        "workspace_id": workspace_id,
        "datasources": [item.model_dump(mode="json") for item in datasources],
        "datasets": [item.model_dump(mode="json") for item in datasets],
        "relations": [item.model_dump(mode="json") for item in relations],
        "samples": [item.model_dump(mode="json") for item in samples],
        "mapping_candidates": mapping_candidates,
    }
