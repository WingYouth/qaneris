"""RS-VIZ-02: real Neo4j/SQLite/HTTP/SSE/chart acceptance with a fixed local intent model.

This isolates the deterministic product chain from an external model account's quota. The same
fixed synthetic Excel workbook used by ``excel_web_ask`` is uploaded over HTTP and published to
real Neo4j. The local model returns only business intent; retrieval, grounding, planning, read-only
SQL, SSE, browser projections and chart policy/spec are production code. It is not a claim that an
external model provider passed acceptance; that is reported separately by ``web_workspace``.
"""

from __future__ import annotations

import base64
import json
import shutil
import subprocess
import threading
import time
from datetime import UTC, datetime
from pathlib import Path
from urllib.request import urlopen

import httpx
import uvicorn
from websockets.sync.client import connect

from qaneris.application.service import QanerisService
from qaneris.common.artifacts import artifact_directory, project_root
from qaneris.contracts.semantic import BusinessFilter, BusinessObjective, BusinessQuery
from qaneris.graph import Neo4jConfig
from qaneris.interfaces.api.app import create_app
from qaneris.scripts.acceptance.excel_web_ask import (
    QUESTION,
    UNBOUND_QUESTION,
    build_formula_workbook,
    build_orders_workbook,
    checks_for,
    free_port,
    session,
)
from qaneris.semantic import IntentUnderstandingPipeline, LLMBusinessParser

LINE_QUESTION = "2026-01-05 至 2026-02-02，按 order_date 汇总 amount 的趋势。"
SCALAR_QUESTION = "orders 表的 amount 总额是多少？"
EMPTY_QUESTION = "按 region 汇总 amount，筛选 region = North。"


class FixedIntentModel:
    """Only maps four fixture questions to business intent; it never plans or answers."""

    def parse_business_query(self, question: str, rule_facts: dict) -> BusinessQuery:
        if question == LINE_QUESTION:
            return BusinessQuery(
                question=question,
                objective=BusinessObjective.TREND,
                metrics=["amount"],
                dimensions=["order_date"],
                time_expression=rule_facts.get("time_expression"),
            )
        if question == SCALAR_QUESTION:
            return BusinessQuery(
                question=question, objective=BusinessObjective.LOOKUP, metrics=["amount"]
            )
        if question == EMPTY_QUESTION:
            return BusinessQuery(
                question=question,
                objective=BusinessObjective.LOOKUP,
                metrics=["amount"],
                dimensions=["region"],
                filters=[BusinessFilter(subject="region", operator="=", value="North")],
            )
        if question in (QUESTION, UNBOUND_QUESTION):
            return BusinessQuery(
                question=question,
                objective=BusinessObjective.LOOKUP,
                metrics=["profit" if question == UNBOUND_QUESTION else "amount"],
                dimensions=["region"],
            )
        raise AssertionError(f"unexpected fixture question: {question}")


def _wait_for_api(base_url: str, thread: threading.Thread) -> None:
    for _ in range(120):
        if not thread.is_alive():
            raise RuntimeError("acceptance API stopped before becoming healthy")
        try:
            if httpx.get(f"{base_url}/health", timeout=1).status_code == 200:
                return
        except httpx.HTTPError:
            pass
        time.sleep(0.25)
    raise RuntimeError("acceptance API did not become healthy")


def _visualization_cases(base_url: str, datasource_id: str, run_root: Path) -> dict:
    command = [
        "node", str(project_root() / "web/frontend/scripts/acceptance_visualization_flow.mjs"),
        "--base-url", base_url, "--datasource-id", datasource_id,
    ]
    result = subprocess.run(command, cwd=project_root(), capture_output=True, text=True, check=False)
    (run_root / "visualization-node.log").write_text(result.stderr, encoding="utf-8")
    if result.returncode:
        raise RuntimeError(f"frontend visualization flow failed: {result.stderr[-1000:]}")
    return json.loads(result.stdout.splitlines()[-1])


def _browser_checks(base_url: str, run_root: Path) -> dict[str, bool]:
    chrome = Path("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome")
    if not chrome.is_file():
        raise RuntimeError("Google Chrome is required for browser SVG acceptance")
    profile = run_root / "chrome-profile"
    log = (run_root / "chrome.log").open("w", encoding="utf-8")
    process = subprocess.Popen(
        [str(chrome), "--headless=new", "--no-first-run", "--no-default-browser-check",
         "--remote-debugging-port=0", "--remote-allow-origins=http://localhost",
         f"--user-data-dir={profile}", base_url + "/"],
        stdout=log, stderr=subprocess.STDOUT,
    )
    try:
        active = profile / "DevToolsActivePort"
        for _ in range(120):
            if active.is_file():
                break
            if process.poll() is not None:
                raise RuntimeError("Chrome exited before DevTools started")
            time.sleep(0.25)
        else:
            raise RuntimeError("Chrome DevTools did not start")
        port = active.read_text(encoding="utf-8").splitlines()[0]
        with urlopen(f"http://127.0.0.1:{port}/json/list", timeout=5) as response:
            pages = json.load(response)
        url = next(page["webSocketDebuggerUrl"] for page in pages if page["type"] == "page")
        with connect(url, origin="http://localhost") as socket:
            counter = 0

            def command(method: str, params: dict | None = None) -> dict:
                nonlocal counter
                counter += 1
                socket.send(json.dumps({"id": counter, "method": method, "params": params or {}}))
                while True:
                    message = json.loads(socket.recv(timeout=30))
                    if message.get("id") == counter:
                        if "error" in message:
                            raise RuntimeError(str(message["error"]))
                        return message.get("result", {})

            def evaluate(expression: str):
                result = command("Runtime.evaluate", {"expression": expression,
                    "returnByValue": True, "awaitPromise": True})
                if "exceptionDetails" in result:
                    raise RuntimeError(str(result["exceptionDetails"]))
                return result.get("result", {}).get("value")

            command("Emulation.setDeviceMetricsOverride", {
                "width": 1280, "height": 900, "deviceScaleFactor": 1, "mobile": False,
            })

            def until(expression: str, seconds: float = 30):
                deadline = time.monotonic() + seconds
                while time.monotonic() < deadline:
                    value = evaluate(expression)
                    if value:
                        return value
                    time.sleep(0.2)
                raise RuntimeError(f"browser condition timed out: {expression}")

            until("!!document.querySelector('textarea') && !!document.querySelector('.backend-status--available')")

            def ask(question: str) -> None:
                evaluate("(() => { const e = document.querySelector('textarea'); "
                    "Object.getOwnPropertyDescriptor(HTMLTextAreaElement.prototype, 'value').set.call(e, "
                    + json.dumps(question, ensure_ascii=False) + "); "
                    "e.dispatchEvent(new Event('input', { bubbles: true })); return true; })()")
                until("Array.from(document.querySelectorAll('button')).some(b => b.textContent === '开始问数' && !b.disabled)")
                evaluate("Array.from(document.querySelectorAll('button')).find(b => b.textContent === '开始问数').click()")
                until("!!document.querySelector('.status-pill--ask-completed') || !!document.querySelector('.status-pill--ask-failed')")

            ask(QUESTION)
            bar = evaluate("document.querySelector('svg[role=img]')?.getAttribute('aria-label') || ''")
            bar_evidence = evaluate("!!document.querySelector('.evidence-panel')")
            command("Page.captureScreenshot")
            evaluate("Array.from(document.querySelectorAll('.chart-selector button')).find(b => b.textContent === '饼图').click()")
            pie = until("document.querySelector('svg[role=img]')?.getAttribute('aria-label')?.startsWith('饼图')")
            pie_paths = evaluate("document.querySelectorAll('.chart--pie path').length")
            evaluate("document.querySelector('.chart').scrollIntoView({block: 'center', behavior: 'instant'})")
            screenshot = command("Page.captureScreenshot")
            (run_root / "pie-browser.png").write_bytes(base64.b64decode(screenshot["data"]))

            ask(LINE_QUESTION)
            line = evaluate("document.querySelector('svg[role=img]')?.getAttribute('aria-label') || ''")
            line_segments = evaluate("document.querySelectorAll('.chart polyline').length")
            evaluate("document.querySelector('.chart').scrollIntoView({block: 'center', behavior: 'instant'})")
            screenshot = command("Page.captureScreenshot")
            (run_root / "line-browser.png").write_bytes(base64.b64decode(screenshot["data"]))

            ask(SCALAR_QUESTION)
            scalar_table = evaluate("!!document.querySelector('.result-table table') && !document.querySelector('svg[role=img]')")
            ask(EMPTY_QUESTION)
            empty_message = evaluate("document.querySelector('.chart-empty')?.textContent || ''")
            return {
                "Chrome rendered bar SVG and evidence": bar.startswith("柱状图") and bar_evidence,
                "Chrome rendered selectable pie SVG": pie and pie_paths == 2,
                "Chrome rendered line SVG": line.startswith("折线图") and line_segments >= 1,
                "Chrome rendered scalar table": scalar_table,
                "Chrome rendered empty result message": empty_message == "当前查询没有可视化数据",
            }
    finally:
        process.terminate()
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=10)
        log.close()
        shutil.rmtree(profile, ignore_errors=True)


def main() -> int:
    Neo4jConfig.from_environment()
    run_root = artifact_directory("acceptance") / f"web-viz-{datetime.now(UTC):%Y%m%dT%H%M%SZ}"
    run_root.mkdir(parents=True, exist_ok=False)
    workbook = build_orders_workbook(run_root / "orders.xlsx")
    formula = build_formula_workbook(run_root / "formula.xlsx")
    catalog_path = run_root / "qaneris.db"
    app = create_app(str(catalog_path))
    service: QanerisService = app.state.service
    fixed_model = FixedIntentModel()
    service.model = fixed_model
    service.intent_understanding = IntentUnderstandingPipeline(LLMBusinessParser(fixed_model))
    port = free_port()
    base_url = f"http://127.0.0.1:{port}"
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="error"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    try:
        _wait_for_api(base_url, thread)
        with httpx.Client(base_url=base_url, timeout=120) as client:
            observed = session(client, workbook, formula, catalog_path, project_root())
        checks = checks_for(observed)
        cases = _visualization_cases(base_url, observed["datasource_id"], run_root)
        bar = cases["bar"]
        line = cases["line"]
        scalar = cases["scalar"]
        empty = cases["empty"]
        checks.update({
            "Bar from real query": bar["status"] == "completed" and bar["auto"] == "bar"
            and bar["autoSpec"]["category"]["field"] == "region"
            and bar["autoSpec"]["points"] == [
                {"category": "East", "values": {"sum_amount": 350.5}},
                {"category": "West", "values": {"sum_amount": 220.25}},
            ],
            "Pie from same real result": "pie" in bar["eligible"]
            and bar["pieSpec"]["kind"] == "pie"
            and bar["pieSpec"]["points"] == bar["autoSpec"]["points"],
            "Line from real query": line["status"] == "completed" and line["auto"] == "line"
            and line["plan"]["timeField"] == "order_date"
            and line["autoSpec"]["category"]["field"] == "order_date"
            and len(line["autoSpec"]["points"]) == 5,
            "Scalar stays table": scalar["status"] == "completed" and scalar["auto"] == "table",
            "Empty result stays table": empty["status"] == "completed" and empty["auto"] == "table"
            and empty["table"]["rowCount"] == 0,
            "Invalid spec rejected": cases["invalidSpecRejected"],
            "Unsupported kind falls back": cases["unsupportedFallsBack"],
        })
        checks.update(_browser_checks(base_url, run_root))
        (run_root / "visualization-checks.json").write_text(
            json.dumps({"checks": checks, "cases": cases}, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        for name, passed in checks.items():
            print(f"[{'PASS' if passed else 'FAIL'}] {name}")
        print(f"Evidence: {run_root}")
        return 0 if all(checks.values()) else 1
    finally:
        server.should_exit = True
        thread.join(timeout=15)


if __name__ == "__main__":
    raise SystemExit(main())
