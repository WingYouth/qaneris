"""Local browser acceptance server with real FastAPI, runtime, and SQLite sources.

The source metadata and deterministic planning model come from the existing IQ-05
integration fixture; all source queries execute against three real SQLite files.
Run from the repository root with `.venv/bin/python tests/e2e/iq06_browser_fixture.py`.
"""

import os
import sqlite3
import sys
import tempfile
import time
from pathlib import Path

import uvicorn

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "tests" / "integration"))
sys.path.insert(0, str(ROOT / "tests"))
DIRECTORY = Path(tempfile.mkdtemp(prefix="smartdata-iq06-browser-"))
os.environ.setdefault("SMARTDATA_GRAPH_STORE", "null")
os.environ["SMARTDATA_CATALOG"] = str(DIRECTORY / "bootstrap.db")

from test_diagnostic_sqlite_e2e import Model as DiagnosticModel
from test_federation_sqlite_e2e import _setup
from test_grounded_sqlite_pipeline_execution import governed_assets, registry_with, scanned

import smartdata.interfaces.api.app as api_module
from smartdata.application.service import SmartDataService
from smartdata.catalog import Catalog
from smartdata.graph.reading import GraphStructureRequest


def fixture_app():
    if os.getenv("IQ06_DIAGNOSTIC") == "1":
        _, reader, catalog_path, _, database = scanned(DIRECTORY)
        with sqlite3.connect(database) as db:
            db.execute("DELETE FROM orders")
            db.executemany("INSERT INTO orders VALUES (?, ?, ?, ?, ?)", [
                (1, 1, 1000.0, "华东", "2025-08-12"),
                (2, 2, 800.0, "华南", "2025-08-12"),
                (3, 1, 700.0, "华东", "2026-08-12"),
                (4, 2, 790.0, "华南", "2026-08-12"),
            ])
        with Catalog(catalog_path)._connect() as db:
            db.execute("UPDATE datasource SET status='ready'")
        assets = governed_assets(reader)[:3]
        assets[2] = assets[2].model_copy(update={"time_axis": True})
        registry_with(assets, catalog_path)
        model = DiagnosticModel()
        if os.getenv("IQ06_SLOW") == "1":
            class SlowModel(DiagnosticModel):
                def parse_business_query(self, question, rule_facts):
                    time.sleep(float(os.getenv("IQ06_SLOW_SECONDS", "5")))
                    return super().parse_business_query(question, rule_facts)

            model = SlowModel()
        service = SmartDataService(Catalog(catalog_path), model=model, graph_reader=reader)
    else:
        runtime, _, _, reader = _setup(DIRECTORY)
        service = runtime.federation_service
        catalog_path = DIRECTORY / "catalog.db"
    if os.getenv("IQ06_CONFIRM_JOIN") == "1" and os.getenv("IQ06_DIAGNOSTIC") != "1":
        structure = reader.read_structure(GraphStructureRequest(workspace_id="default"))
        objects = {item.datasource_id: item.node_id for item in structure.data_objects if item.name == "items"}
        mapping = runtime.federation.governance.create(
            workspace_id="default", business_key="订单号",
            left_datasource_id="payments", left_data_object_id=objects["payments"],
            left_field_path="order_id", left_grain="one order",
            right_datasource_id="shipments", right_data_object_id=objects["shipments"],
            right_field_path="order_id", right_grain="one order",
            cardinality="ONE_TO_ONE", null_policy="REJECT",
        )
        runtime.federation.governance.confirm(mapping.mapping_id, "iq06-fixture")
    api_module.SmartDataService = lambda _catalog: service
    app = api_module.create_app(database_path=str(catalog_path))
    if os.getenv("IQ06_FAIL_ONCE") == "1":
        class TemporaryFailure(RuntimeError):
            code = "temporary_unavailable"

        class FailOnceAsk:
            def __init__(self, delegate):
                self.delegate = delegate
                self.failed = False

            def execute(self, request):
                if not self.failed:
                    self.failed = True
                    raise TemporaryFailure("temporary_unavailable")
                return self.delegate.execute(request)

        runtime = app.state.run_orchestrator
        runtime.ask = FailOnceAsk(runtime.ask)
    print(f"IQ-06 SQLite fixture: {DIRECTORY}", flush=True)
    return app


if __name__ == "__main__":
    uvicorn.run(fixture_app(), host="127.0.0.1", port=8000)
