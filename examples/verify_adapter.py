from __future__ import annotations

import argparse
import json
import os
from typing import Any

from smartdata.adapters.registry import create_adapter
from smartdata.contracts import DatasourceKind

CONNECTION_ENV = "SMARTDATA_TEST_CONNECTION"


def verify_adapter(kind: str, driver: str, connection: dict[str, Any]) -> dict[str, Any]:
    safe_connection = dict(connection)
    safe_connection["driver"] = driver
    adapter = create_adapter("ds_verification", DatasourceKind(kind), safe_connection)
    adapter.test_connection()
    datasets = adapter.scan_metadata()
    relations = adapter.scan_relations()
    samples = adapter.scan_samples(datasets, limit=3)
    if any(len(sample.rows) > 3 for sample in samples):
        raise RuntimeError("Adapter returned more than three sample rows for a dataset")
    return {
        "kind": kind,
        "driver": driver,
        "connection": "ok",
        "dataset_count": len(datasets),
        "relation_count": len(relations),
        "sampled_dataset_count": len(samples),
        "sample_row_count": sum(len(sample.rows) for sample in samples),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Verify one SmartData database adapter")
    parser.add_argument("--kind", required=True)
    parser.add_argument("--driver", required=True)
    args = parser.parse_args()
    raw_connection = os.getenv(CONNECTION_ENV)
    if not raw_connection:
        parser.error(f"Set {CONNECTION_ENV} to the database connection JSON")
    connection = json.loads(raw_connection)
    if not isinstance(connection, dict):
        parser.error(f"{CONNECTION_ENV} must contain a JSON object")
    print(
        json.dumps(verify_adapter(args.kind, args.driver, connection), ensure_ascii=False, indent=2)
    )


if __name__ == "__main__":
    main()
