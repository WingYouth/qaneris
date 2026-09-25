"""EXCEL-01C acceptance: browser upload -> datasource READY -> grounded Ask -> typed result.

Run it from the project root with a real graph backend and a real model endpoint configured
(``SMARTDATA_GRAPH_STORE=neo4j`` with the ``SMARTDATA_NEO4J_*`` credentials, and
``SMARTDATA_MODEL_BASE_URL`` / ``SMARTDATA_MODEL_API_KEY`` / ``SMARTDATA_MODEL_NAME``)::

    set -a && . ./.env && set +a
    python3 -m smartdata.scripts.acceptance.excel_web_ask

Unlike the EXCEL-01A acceptance, which drives ``SmartDataService`` in-process, and the EXCEL-01B one,
which drives the console script, this one drives the **HTTP product boundary**. It starts the real
API as a subprocess and speaks to it only over HTTP:

    multipart upload -> POST /api/datasources/import-excel
    datasource read  -> GET  /api/datasources
    ask              -> POST /api/ask

It proves, on real infrastructure:

1. a browser-shaped multipart upload of the fixed ``orders`` workbook returns HTTP 201 with
   ``status = READY`` and ``neo4j_publication_verified = true``, and the response names no internal
   file location;
2. the datasource list confirms that datasource as ``ready``;
3. ``POST /api/ask`` scoped to the returned ``datasource_id`` walks the existing trusted chain -
   intent, retrieval, grounding, plan, validation, read-only execution - and returns a typed result
   whose per-region sums equal the values the fixture fixes;
4. the executed query is the backend's own ``evidence.display_command``, and it is a read-only
   projection of the imported table at the reported scan revision;
5. re-uploading the same bytes is idempotent: same import, datasource and snapshot, no second
   datasource, artifact untouched;
6. a workbook containing a formula is refused with ``EXCEL_FORMULA_UNSUPPORTED``, its sheet and cell
   coordinate, and no datasource is created;
7. the real frontend modules (``web/frontend/src/api/*``) drive the same two calls against the same
   server, through Node, so the browser request contract itself is exercised end to end.

Business governance: Excel ingestion publishes *physical* structure. A governed metric or dimension
is what a question can bind to, and an administrator states those explicitly - the same rule that
applies to every other datasource. This acceptance therefore registers the fixture's governed
semantic assets through the existing ``SemanticAssetBootstrap``, whose physical identity is resolved
against the freshly published graph rather than asserted by hand.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import os
import re
import socket
import subprocess
import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
from openpyxl import Workbook

from smartdata.catalog import Catalog
from smartdata.common.artifacts import artifact_directory, project_root
from smartdata.contracts.semantic import AggregateFunction
from smartdata.graph import Neo4jConfig, graph_reader_from_environment
from smartdata.ingestion.excel import DEFAULT_EXCEL_POLICY, artifact_key
from smartdata.llm.config import resolve_model_profile
from smartdata.scripts.acceptance.summary import write_summary
from smartdata.semantic import (
    SemanticAssetBootstrap,
    SemanticAssetKind,
    SemanticAssetSeed,
    SemanticAssetSource,
    SemanticAssetStatus,
    SQLiteSemanticAssetRegistry,
)

WORKSPACE = "default"
DATASOURCE_NAME = "Roadshow Excel Orders"
QUESTION = "对 orders 表，按 region 分组汇总 amount 的总和。"
UNBOUND_QUESTION = "对 orders 表，按 region 分组汇总 profit 的总和。"

# The fixed Roadshow workbook. No formula, no merged range, two regions, and amounts that really
# carry decimals so the column is materialized as REAL rather than INTEGER.
ORDERS: list[list[object]] = [
    ["order_id", "order_date", "region", "amount", "status"],
    [1, dt.date(2026, 1, 5), "East", 100.25, "completed"],
    [2, dt.date(2026, 1, 6), "West", 120.50, "completed"],
    [3, dt.date(2026, 1, 7), "East", 150.25, "pending"],
    [4, dt.date(2026, 1, 8), "East", 100.00, "completed"],
    [5, dt.date(2026, 2, 2), "West", 99.75, "completed"],
]

EXPECTED_ROW_COUNT = len(ORDERS) - 1

#: The aggregate the fixture is fixed to. Recomputed from ``ORDERS`` below and locked to these exact
#: values, so a changed fixture cannot silently keep the old expectation.
EXPECTED_SUMS = {"East": 350.50, "West": 220.25}

FORBIDDEN_IN_RESPONSES = (
    "data.sqlite3",
    "manifest.json",
    "source.xlsx",
    "sqlite_path",
    "artifact_directory",
    "manifest_path",
    "/uploads/",
)

WRITE_KEYWORDS = re.compile(
    r"\b(INSERT|UPDATE|DELETE|DROP|ALTER|CREATE|REPLACE|ATTACH|DETACH|VACUUM|PRAGMA)\b", re.IGNORECASE
)

READY_TIMEOUT_SECONDS = 90.0

#: A single HTTP call may contain a whole model round trip. The Roadshow endpoint answers in tens of
#: seconds and occasionally slower, so the ceiling is set well above that: a slow provider must be
#: reported as slow, and only a real hang should end the run.
REQUEST_TIMEOUT_SECONDS = 600.0

#: How many times an ask is repeated when the *only* thing that failed is the model call. A provider
#: that answers with nothing usable is not a product verdict, and nothing about the trusted chain
#: was decided; a clarification, a completed answer or a failed status all arrive as HTTP 200 and
#: are never retried.
ASK_ATTEMPTS = 4


def fixture_sums() -> dict[str, float]:
    totals: dict[str, float] = {}
    for row in ORDERS[1:]:
        totals[row[2]] = round(totals.get(row[2], 0.0) + float(row[3]), 2)
    return totals


def build_orders_workbook(path: Path) -> Path:
    workbook = Workbook()
    worksheet = workbook.active
    worksheet.title = "orders"
    for row in ORDERS:
        worksheet.append(row)
    for row_index in range(2, len(ORDERS) + 1):
        worksheet.cell(row=row_index, column=2).number_format = "yyyy-mm-dd"
    workbook.save(path)
    return path


def build_formula_workbook(path: Path) -> Path:
    workbook = Workbook()
    worksheet = workbook.active
    worksheet.title = "orders"
    worksheet.append(["order_id", "amount"])
    worksheet.append([1, 100.0])
    worksheet.append([2, "=SUM(B2:B2)"])
    workbook.save(path)
    return path


def free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


def start_api(port: int, environment: dict[str, str], cwd: Path, log_path: Path) -> subprocess.Popen[str]:
    """Start the real API entry point as its own process, configured by environment only.

    The process writes to a log file rather than to a pipe. An undrained pipe is a real hazard here:
    the Neo4j driver emits a notification warning per query, and a full pipe would block the server
    mid-run - which looks exactly like the API being broken. The log is also the evidence for what
    the server did.
    """
    log = log_path.open("wb")
    process = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "uvicorn",
            "smartdata.interfaces.api.app:app",
            "--host",
            "127.0.0.1",
            "--port",
            str(port),
            "--log-level",
            "warning",
        ],
        cwd=cwd,
        env=environment,
        stdout=log,
        stderr=subprocess.STDOUT,
        text=False,
    )
    process.api_log = log  # type: ignore[attr-defined]
    deadline = time.monotonic() + 60.0
    while time.monotonic() < deadline:
        if process.poll() is not None:
            log.close()
            raise RuntimeError(f"API process exited early:\n{tail_of(log_path)}")
        try:
            response = httpx.get(f"http://127.0.0.1:{port}/health", timeout=2.0)
            if response.status_code == 200:
                return process
        except httpx.HTTPError:
            time.sleep(0.25)
    log.close()
    raise RuntimeError(f"API did not become healthy in time:\n{tail_of(log_path)}")


def governed_seeds(datasource_id: str) -> list[SemanticAssetSeed]:
    """The fixture's business vocabulary, stated explicitly by an administrator.

    Only a governed metric or dimension can be bound to a metric or dimension slot, and nothing
    downstream invents one from a column name. These seeds keep the Roadshow question answerable
    while leaving every physical fact - datasource, object, field - to be resolved against the
    published graph.
    """
    common = {
        "source": SemanticAssetSource.ADMINISTRATOR,
        "status": SemanticAssetStatus.PUBLISHED,
    }
    return [
        SemanticAssetSeed(
            asset_id="roadshow.excel.metric.amount",
            kind=SemanticAssetKind.METRIC,
            name="amount",
            aliases=["销售额", "金额", "销售金额", "order amount"],
            description="订单金额",
            source_ref="roadshow://excel/orders/amount",
            datasource_id=datasource_id,
            data_object_name="orders",
            field_path="amount",
            default_aggregation=AggregateFunction.SUM,
            **common,
        ),
        SemanticAssetSeed(
            asset_id="roadshow.excel.dimension.region",
            kind=SemanticAssetKind.DIMENSION,
            name="region",
            aliases=["地区", "区域", "销售区域"],
            description="订单所属地区",
            source_ref="roadshow://excel/orders/region",
            datasource_id=datasource_id,
            data_object_name="orders",
            field_path="region",
            **common,
        ),
        SemanticAssetSeed(
            asset_id="roadshow.excel.dimension.order_date",
            kind=SemanticAssetKind.DIMENSION,
            name="order_date",
            aliases=["下单日期", "订单日期"],
            description="下单日期",
            source_ref="roadshow://excel/orders/order_date",
            datasource_id=datasource_id,
            data_object_name="orders",
            field_path="order_date",
            time_axis=True,
            **common,
        ),
    ]


def register_governance(catalog_path: Path, datasource_id: str, workspace_id: str) -> list[str]:
    """Resolve the fixture's governed assets against the published graph and persist them."""
    registry = SQLiteSemanticAssetRegistry(catalog_path)
    profile = Catalog(catalog_path).get_company_data_profile(workspace_id)
    reader = graph_reader_from_environment()
    assets = SemanticAssetBootstrap(registry=registry, graph_reader=reader).load(
        profile, governed_seeds(datasource_id)
    )
    return [
        f"{asset.asset_id} -> {asset.graph_data_object_id}/{asset.graph_field_id}" for asset in assets
    ]


def upload_workbook(client: httpx.Client, path: Path, **form: str) -> httpx.Response:
    """One browser-shaped multipart upload. Only bytes, a name and a workspace are submitted."""
    return client.post(
        "/api/datasources/import-excel",
        files={
            "file": (
                path.name,
                path.read_bytes(),
                "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            )
        },
        data=form,
    )


def ask(client: httpx.Client, question: str, datasource_id: str) -> tuple[httpx.Response, int]:
    """Ask once, retrying only a model-level failure.

    A provider that answers with nothing usable is not a product verdict: ``ask`` reports it as a
    4xx and the only thing that failed is the model call. That case - and only that case - is
    retried, because nothing about the trusted chain was decided. A clarification, a completed
    answer or a ``failed`` status all arrive as HTTP 200 and are never retried.
    """
    attempts = 0
    response = httpx.Response(0)
    while attempts < ASK_ATTEMPTS:
        attempts += 1
        response = client.post(
            "/api/ask",
            json={"question": question, "workspace_id": WORKSPACE, "datasource_id": datasource_id},
        )
        if response.status_code == 200:
            break
        print(
            f"[retry] ask attempt {attempts}/{ASK_ATTEMPTS} returned "
            f"HTTP {response.status_code} (model-level failure, not a chain verdict)"
        )
    return response, attempts


def fingerprint(directory: Path) -> dict[str, tuple[int, int, str]]:
    return {
        name: (
            (directory / name).stat().st_ino,
            (directory / name).stat().st_mtime_ns,
            hashlib.sha256((directory / name).read_bytes()).hexdigest(),
        )
        for name in ("source.xlsx", "data.sqlite3", "manifest.json")
        if (directory / name).is_file()
    }


def tail_of(path: Path, limit: int = 4000) -> str:
    if not path.is_file():
        return ""
    text = path.read_text("utf-8", errors="replace")
    return text[-limit:]


def artifact_directory_of(path: Path, workspace_id: str) -> Path | None:
    """Locate the managed artifact from the artifact root, without trusting the HTTP response.

    The HTTP response deliberately hides the location, so the idempotency evidence is read from the
    artifact root directly - the same content-addressed path the Core derives.
    """
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    workspace = hashlib.sha256(workspace_id.encode("utf-8")).hexdigest()[:12]
    candidate = (
        artifact_directory("ingestion")
        / "excel"
        / workspace
        / artifact_key(DEFAULT_EXCEL_POLICY)
        / digest
    )
    return candidate if candidate.is_dir() else None


def run_browser_flow(
    base_url: str, workbook: Path, formula: Path, cwd: Path
) -> dict[str, Any]:
    """Drive the same two calls through the real frontend modules, from Node."""
    script = cwd / "web" / "frontend" / "scripts" / "acceptance_workspace_flow.mjs"
    completed = subprocess.run(
        [
            "node",
            str(script),
            "--base-url",
            base_url,
            "--workbook",
            str(workbook),
            "--formula-workbook",
            str(formula),
        ],
        cwd=cwd,
        capture_output=True,
        text=True,
        check=False,
    )
    payload: dict[str, Any] = {}
    for line in reversed(completed.stdout.splitlines()):
        if line.strip().startswith("{"):
            try:
                payload = json.loads(line)
            except json.JSONDecodeError:
                payload = {}
            break
    return {
        "returncode": completed.returncode,
        "payload": payload,
        "output": completed.stdout,
        "errors": completed.stderr,
    }


def session(
    client: httpx.Client, workbook: Path, formula: Path, catalog_path: Path, cwd: Path
) -> dict[str, Any]:
    """Run the whole product conversation and return the observations the checks are built from.

    Everything that can fail on infrastructure - a slow model, a dropped connection - is kept out of
    this function's control flow on purpose: it records what it saw and lets the caller decide. A
    transport failure must be reported as a transport failure, never as a product verdict.
    """
    observed: dict[str, Any] = {}

    # ---- 1. the browser upload ------------------------------------------------------------
    first = upload_workbook(client, workbook, name=DATASOURCE_NAME, workspace_id=WORKSPACE)
    payload = json_of(first)
    datasource_id = str(payload.get("datasource_id", ""))
    observed.update(
        first=first,
        payload=payload,
        datasource_id=datasource_id,
        directory=artifact_directory_of(workbook, WORKSPACE),
    )

    # ---- 2. the datasource list agrees ----------------------------------------------------
    observed["listed"] = client.get("/api/datasources", params={"workspace_id": WORKSPACE}).json()

    # ---- 3. the fixture's governed business vocabulary ------------------------------------
    observed["assets"] = (
        register_governance(catalog_path, datasource_id, WORKSPACE) if datasource_id else []
    )

    # ---- 4. the real Ask ------------------------------------------------------------------
    ask_response, ask_attempts = ask(client, QUESTION, datasource_id)
    answer = json_of(ask_response)
    observed.update(
        ask_response=ask_response,
        ask_attempts=ask_attempts,
        answer=answer,
        result=answer.get("result") or {},
        evidence=answer.get("evidence") or {},
        plan=answer.get("plan") or {},
    )
    observed["rows"] = {
        row.get("region"): row.get("sum_amount") for row in observed["result"].get("rows", [])
    }

    # ---- 5. an ungoverned metric must never produce a number ------------------------------
    unbound_response, _ = ask(client, UNBOUND_QUESTION, datasource_id)
    observed["unbound"] = json_of(unbound_response)

    # ---- 6. idempotent replay -------------------------------------------------------------
    directory = observed["directory"]
    observed["before"] = fingerprint(directory) if directory else {}
    replay = upload_workbook(client, workbook, name=DATASOURCE_NAME, workspace_id=WORKSPACE)
    observed["replay"] = replay
    observed["replay_payload"] = json_of(replay)
    observed["after"] = fingerprint(directory) if directory else {}
    observed["listed_after"] = client.get(
        "/api/datasources", params={"workspace_id": WORKSPACE}
    ).json()

    # ---- 7. the refusal, through the same boundary ---------------------------------------
    observed["datasources_before"] = len(observed["listed_after"])
    refused = upload_workbook(client, formula, workspace_id=WORKSPACE)
    observed["refused"] = refused
    observed["refused_payload"] = json_of(refused)
    observed["datasources_after"] = len(
        client.get("/api/datasources", params={"workspace_id": WORKSPACE}).json()
    )
    observed["refused_streams"] = f"{refused.text} {replay.text} {first.text}"

    # ---- 8. the browser request contract itself, through Node ----------------------------
    observed["browser"] = run_browser_flow(
        str(client.base_url), workbook, formula, cwd
    )
    return observed


def checks_for(observed: dict[str, Any]) -> dict[str, bool]:
    """Every verdict, expressed only from what was observed.

    Nothing here reaches out: a missing observation makes its checks fail rather than raising, so a
    session that broke halfway still produces a full, readable verdict list.
    """
    payload = observed.get("payload", {})
    first = observed.get("first")
    datasource_id = observed.get("datasource_id", "")
    listed = observed.get("listed", [])
    plan = observed.get("plan", {})
    result = observed.get("result", {})
    evidence = observed.get("evidence", {})
    answer = observed.get("answer", {})
    rows = observed.get("rows", {})
    expected = fixture_sums()
    replay_payload = observed.get("replay_payload", {})
    refused_payload = observed.get("refused_payload", {})
    browser_payload = observed.get("browser", {}).get("payload", {})
    chart_spec = browser_payload.get("chart_spec") or {}
    display_command = evidence.get("display_command", "")

    return {
        # 1. the upload boundary
        "Upload HTTP 201": getattr(first, "status_code", None) == 201,
        "Upload status READY": payload.get("status") == "READY",
        "Upload publication verified": payload.get("neo4j_publication_verified") is True,
        "Upload identity": bool(datasource_id)
        and bool(payload.get("snapshot_id"))
        and payload.get("scan_version") == 1,
        "Upload names the workbook": payload.get("original_filename") == "orders.xlsx",
        "Upload sheet shape": payload.get("sheets", [{}])[0].get("row_count") == EXPECTED_ROW_COUNT
        and payload.get("sheets", [{}])[0].get("column_count") == len(ORDERS[0]),
        "Upload hides server locations": bool(getattr(first, "text", ""))
        and not any(token in first.text for token in FORBIDDEN_IN_RESPONSES),
        # 2. the datasource is really there
        "Datasource listed ready": [
            item for item in listed if item.get("id") == datasource_id and item.get("status") == "ready"
        ]
        != [],
        # 3. the fixture really has governed semantics
        "Governance resolved against the graph": len(observed.get("assets", [])) == 3,
        # 4. the grounded answer
        "Ask HTTP 200": getattr(observed.get("ask_response"), "status_code", None) == 200,
        "Ask completed without clarification": answer.get("status") == "completed"
        and answer.get("clarification", []) == [],
        "Grounding produced a plan": bool(plan.get("plan_id")) and plan.get("scan_version") == 1,
        "Plan is the imported datasource": plan.get("datasource_id") == datasource_id,
        "Plan aggregates the imported field": [
            item
            for item in plan.get("aggregates", [])
            if item.get("field", {}).get("field_path") == "amount"
            and item.get("function") == "sum"
        ]
        != [],
        "Result is typed with both regions": sorted(result.get("columns", []))
        == ["region", "sum_amount"]
        and result.get("row_count") == len(expected),
        "Expected aggregate matches exactly": rows == expected,
        "Fixture still fixes the Roadshow numbers": expected == EXPECTED_SUMS,
        "Evidence points at the imported datasource": evidence.get("datasource_id") == datasource_id
        and evidence.get("scan_version") == 1,
        "Executed query is read-only SQL": display_command.lstrip().upper().startswith("SELECT")
        and not WRITE_KEYWORDS.search(display_command),
        "Executed query reads the imported table": '"orders"' in display_command,
        # 5. an ungoverned metric never yields a number
        "Ungoverned metric is not silently answered": not (
            observed.get("unbound", {}).get("status") == "completed"
            and (observed.get("unbound", {}).get("result") or {}).get("rows")
        ),
        # 6. idempotency, verified from the artifact rather than from the response
        "Replay HTTP 201": getattr(observed.get("replay"), "status_code", None) == 201,
        "Replay same import": replay_payload.get("import_id") == payload.get("import_id"),
        "Replay same datasource": replay_payload.get("datasource_id") == datasource_id,
        "Replay same snapshot": replay_payload.get("snapshot_id") == payload.get("snapshot_id"),
        "Replay leaves the artifact untouched": bool(observed.get("before"))
        and observed.get("before") == observed.get("after"),
        "Replay creates no second datasource": len(observed.get("listed_after", [])) == 1,
        # 7. the refusal boundary
        "Formula refused HTTP 400": getattr(observed.get("refused"), "status_code", None) == 400,
        "Formula keeps its code": refused_payload.get("error", {}).get("code")
        == "EXCEL_FORMULA_UNSUPPORTED",
        "Formula reports the location": refused_payload.get("error", {}).get("sheet") == "orders"
        and refused_payload.get("error", {}).get("coordinate") == "B3",
        "Formula creates no datasource": observed.get("datasources_after")
        == observed.get("datasources_before")
        and observed.get("datasources_after") == 1,
        "No formula body or internal path leaked": bool(observed.get("refused_streams"))
        and not any(
            token in observed["refused_streams"]
            for token in (*FORBIDDEN_IN_RESPONSES, "SUM", "B2:B2")
        ),
        # 8. the browser modules
        "Frontend flow succeeded": observed.get("browser", {}).get("returncode") == 0,
        "Frontend upload reached READY": browser_payload.get("status") == "READY",
        "Frontend auto-selected the imported datasource": browser_payload.get(
            "used_returned_datasource_id"
        )
        is True,
        "Frontend asked with the returned datasource": browser_payload.get("asked_datasource_id")
        == datasource_id,
        "Frontend rendered the result table": browser_payload.get("table_rows") == expected
        and browser_payload.get("table_columns") == ["region", "sum_amount"]
        and browser_payload.get("row_count") == len(expected),
        "Frontend rendered evidence": bool(browser_payload.get("display_command"))
        and browser_payload.get("ask_status") == "completed",
        "Frontend generated the expected controlled chart": browser_payload.get("chart_auto_kind") == "bar"
        and "pie" in browser_payload.get("chart_eligible_kinds", [])
        and chart_spec.get("kind") == "bar"
        and chart_spec.get("category", {}).get("field") == "region"
        and (chart_spec.get("series") or [{}])[0].get("field") == "sum_amount"
        and chart_spec.get("points") == [
            {"category": "East", "values": {"sum_amount": 350.5}},
            {"category": "West", "values": {"sum_amount": 220.25}},
        ],
        "Frontend upload reported no fabricated progress": browser_payload.get("progress_percent")
        is None,
        "Frontend SSE event chain is ordered": browser_payload.get("trace_events", [])
        == ["accepted", "intent_ready", "retrieval_ready", "grounding_ready", "plan_ready", "query_ready", "execution_started", "result_ready", "done"],
        "Frontend SSE used the same datasource and scan revision": browser_payload.get("asked_datasource_id") == datasource_id
        and browser_payload.get("asked_scan_version") == 1,
        "Frontend trace displays public SQL evidence": browser_payload.get("trace_display_command") == browser_payload.get("display_command"),
        "Frontend clarification stream is a normal result": browser_payload.get("clarification_stream_status") == "clarification_required"
        and browser_payload.get("clarification_stream_events", [])[-2:] == ["clarification_required", "done"],
        "Frontend error view keeps the stable code": browser_payload.get("error_code")
        == "EXCEL_FORMULA_UNSUPPORTED"
        and browser_payload.get("error_state") == "EXCEL_FORMULA_UNSUPPORTED",
    }


def json_of(response: httpx.Response) -> dict[str, Any]:
    """The JSON object of a response, or an empty one when the body is not a JSON object."""
    if not response.headers.get("content-type", "").startswith("application/json"):
        return {}
    try:
        parsed = response.json()
    except json.JSONDecodeError:  # pragma: no cover - a broken body is not a product verdict
        return {}
    return parsed if isinstance(parsed, dict) else {}


def main() -> int:
    run_id = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    run_root = artifact_directory("acceptance") / f"excel-web-ask-{run_id}"
    run_root.mkdir(parents=True, exist_ok=False)

    if os.getenv("SMARTDATA_GRAPH_STORE", "neo4j").strip().lower() == "null":
        print("[FAIL] SMARTDATA_GRAPH_STORE=null: READY requires a verified publication")
        return 1
    try:
        Neo4jConfig.from_environment()
    except ValueError as error:
        print(f"[FAIL] Neo4j is not configured: {error}")
        return 1
    if resolve_model_profile() is None:
        print("[BLOCKED] no model endpoint configured: a real acceptance cannot be faked")
        return 1

    workbook = build_orders_workbook(run_root / "orders.xlsx")
    formula = build_formula_workbook(run_root / "formula.xlsx")
    catalog_path = run_root / "smartdata.db"
    environment = {**os.environ, "SMARTDATA_CATALOG": str(catalog_path)}
    cwd = project_root()

    port = free_port()
    base_url = f"http://127.0.0.1:{port}"
    api_log = run_root / "api.log"
    process = start_api(port, environment, cwd, api_log)

    observed: dict[str, Any] = {}
    session_error = ""
    try:
        # The client timeout is deliberately generous. A model call on a busy endpoint can take a
        # minute, and an ask retries a provider flake, so a slow provider must surface as "slow"
        # rather than as a broken product chain.
        with httpx.Client(base_url=base_url, timeout=REQUEST_TIMEOUT_SECONDS) as client:
            try:
                observed = session(client, workbook, formula, catalog_path, cwd)
            except Exception as error:  # noqa: BLE001 - the acceptance reports, it does not crash
                session_error = f"{type(error).__name__}: {error}"
    finally:
        process.terminate()
        try:
            process.wait(timeout=15)
        except subprocess.TimeoutExpired:  # pragma: no cover - a stuck uvicorn
            process.kill()
            process.wait(timeout=15)
        handle = getattr(process, "api_log", None)
        if handle is not None:
            handle.close()

    checks = checks_for(observed)
    checks["Session completed without a transport failure"] = not session_error

    for name, passed in checks.items():
        print(f"[{'PASS' if passed else 'FAIL'}] {name}")

    write_summary(
        checks,
        stage="http_web",
        datasource_id=observed.get("datasource_id"),
        scan_version=(observed.get("payload") or {}).get("scan_version"),
        run_root=str(run_root),
    )

    browser = observed.get("browser") or {"returncode": None, "payload": {}, "output": "", "errors": ""}
    if session_error or not checks["Frontend flow succeeded"]:
        print()
        print("--- diagnostics ---")
        if session_error:
            print(f"session error: {session_error}")
        print(f"browser returncode: {browser.get('returncode')}")
        print(str(browser.get("output", "")).strip())
        if str(browser.get("errors", "")).strip():
            print(str(browser["errors"]).strip())
        print(f"--- api log tail ({api_log}) ---")
        print(tail_of(api_log).strip())
        print("--- end diagnostics ---")

    payload = observed.get("payload", {})
    answer = observed.get("answer", {})
    print()
    print(f"Datasource      : {payload.get('datasource_id')}")
    print(
        f"Snapshot        : {payload.get('snapshot_id')} "
        f"(scan_version={payload.get('scan_version')})"
    )
    print(f"Neo4j verified  : {payload.get('neo4j_publication_verified')}")
    print(f"Question        : {QUESTION}")
    print(f"Ask status      : {answer.get('status')} (attempts={observed.get('ask_attempts')})")
    if answer.get("error"):
        print(f"Ask error       : {answer['error']}")
    print(f"Generated query : {(observed.get('evidence') or {}).get('display_command')}")
    print(
        f"Browser ask     : {browser.get('payload', {}).get('ask_status')} "
        f"(attempts={browser.get('payload', {}).get('ask_attempts')})"
    )
    print(f"Expected rows   : {fixture_sums()}")
    print(f"Actual rows     : {observed.get('rows')}")
    print(f"API log         : {api_log}")
    print(f"Evidence        : {run_root}")
    failed = [name for name, passed in checks.items() if not passed]
    print(
        f"Result          : {'PASS' if not failed else 'FAIL'} "
        f"({len(checks) - len(failed)}/{len(checks)})"
    )
    return 0 if not failed else 1


if __name__ == "__main__":
    raise SystemExit(main())
