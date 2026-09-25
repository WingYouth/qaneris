"""Reusable Phase 1 natural-language query acceptance runner."""

from __future__ import annotations

import json
import os
from collections.abc import Callable, Mapping
from contextlib import suppress
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import quote
from uuid import uuid4

from qaneris.application.service import QanerisService
from qaneris.catalog import Catalog
from qaneris.common.artifacts import artifact_directory
from qaneris.common.errors import ModelInvocationError
from qaneris.contracts import AskRequest, AskStatus
from qaneris.contracts.query import TimeSpec
from qaneris.contracts.semantic import SemanticAssetType
from qaneris.graph import Neo4jConfig
from qaneris.llm import resolve_model_profile
from qaneris.scripts.acceptance.phase1_e2e_scope import (
    WORKSPACE_ID,
    build_source,
    ensure_datasource,
)
from qaneris.semantic import (
    SemanticAssetBootstrap,
    SemanticAssetStatus,
    SQLiteSemanticAssetRegistry,
    normalize_time_range,
)
from qaneris.semantic.intent import RuleExtractor

DATE_TYPES = {"date", "datetime", "timestamp", "timestamptz"}
FIXTURE_ASSET_PREFIX = "phase1."


@dataclass(frozen=True)
class Phase1Scenario:
    code: str
    title: str
    question: str
    expected_ask_status: AskStatus


PHASE1_SCENARIOS: tuple[Phase1Scenario, ...] = (
    Phase1Scenario("A", "Single Metric", "总实收销售额是多少？", AskStatus.COMPLETED),
    Phase1Scenario(
        "B", "Metric + Dimension", "按下单地区看实收销售额", AskStatus.COMPLETED
    ),
    Phase1Scenario(
        "C", "Filter", "下单地区为上海的实收销售额是多少？", AskStatus.COMPLETED
    ),
    Phase1Scenario(
        "D", "Time + Ranking", "近30天按下单地区看实收销售额前10名", AskStatus.COMPLETED
    ),
    Phase1Scenario(
        "E",
        "Ambiguity / Clarification",
        "所有订单的营业额合计是多少？",
        AskStatus.CLARIFICATION_REQUIRED,
    ),
    Phase1Scenario(
        "F", "Relationship Join", "按客户地区看实收销售额", AskStatus.COMPLETED
    ),
)


@dataclass
class ScenarioResult:
    code: str
    title: str
    question: str
    status: str = "failed"
    ask_status: str | None = None
    repeat: int = 1
    business_query: dict[str, Any] | None = None
    rule_facts: dict[str, Any] | None = None
    retrieval_candidates: list[str] = field(default_factory=list)
    grounding: dict[str, Any] | None = None
    plan_id: str | None = None
    datasource_id: str | None = None
    scan_version: int | None = None
    display_command: str | None = None
    result_summary: dict[str, Any] | None = None
    clarification: list[dict[str, Any]] = field(default_factory=list)
    evidence: dict[str, Any] | None = None
    error: dict[str, Any] | None = None
    notes: list[str] = field(default_factory=list)
    # Extra evidence retained from the original Phase 1 harness.
    command: str | None = None
    parameters: list[Any] = field(default_factory=list)
    joins: list[dict[str, Any]] = field(default_factory=list)


@dataclass
class Phase1RunReport:
    run_id: str
    started_at: str
    completed_at: str | None
    status: str
    environment_summary: dict[str, Any]
    suite: str
    results: list[ScenarioResult]
    summary: dict[str, int]
    artifact_paths: dict[str, str]

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class AcceptanceConfigurationError(ValueError):
    """Raised for invalid runner input rather than an environment outage."""


class EnvironmentBlockedError(RuntimeError):
    """Raised when a real dependency prevents acceptance from running."""


class _Redactor:
    def __init__(self, environment: Mapping[str, str]):
        markers = ("PASSWORD", "SECRET", "TOKEN", "API_KEY", "CREDENTIAL")
        self._values = {
            value
            for key, value in environment.items()
            if any(marker in key.upper() for marker in markers) and len(value) >= 4
        }

    def text(self, value: object) -> str:
        safe = str(value)
        for secret in sorted(self._values, key=len, reverse=True):
            safe = safe.replace(secret, "<redacted>")
            safe = safe.replace(quote(secret, safe=""), "<redacted>")
        return safe


def _now() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def _new_run_id() -> str:
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S.%fZ")
    return f"{stamp}-{uuid4().hex[:8]}"


def _business_query(query: Any) -> dict[str, Any] | None:
    if query is None:
        return None
    return {
        "question": query.question,
        "objective": query.objective,
        "metrics": list(query.metrics),
        "dimensions": list(query.dimensions),
        "entities": list(query.entities),
        "filters": [item.model_dump(mode="json") for item in query.filters],
        "time_expression": query.time_expression,
        "ranking": query.ranking.model_dump(mode="json") if query.ranking else None,
        "requested_output": [item.value for item in query.requested_output],
        "ambiguities": list(query.ambiguities),
        "confidence": query.confidence,
    }


def _declared_asset_ids(seed_path: Path) -> set[str]:
    payload = json.loads(seed_path.read_text(encoding="utf-8"))
    raw = payload.get("assets", payload)
    return {item["asset_id"] for item in raw}


def _seed_assets(catalog_path: Path, graph_reader: Any) -> tuple[list[str], list[str]]:
    registry = SQLiteSemanticAssetRegistry(catalog_path)
    seed_path = (
        Path(__file__).resolve().parents[1]
        / "scripts"
        / "acceptance"
        / "phase1_e2e_assets.json"
    )
    declared = _declared_asset_ids(seed_path)
    retired: list[str] = []
    for asset in registry.list(WORKSPACE_ID):
        if (
            asset.asset_id.startswith(FIXTURE_ASSET_PREFIX)
            and asset.asset_id not in declared
            and asset.status is not SemanticAssetStatus.DEPRECATED
        ):
            registry.save(asset.model_copy(update={"status": SemanticAssetStatus.DEPRECATED}))
            retired.append(asset.asset_id)
    catalog = Catalog(catalog_path)
    profile = catalog.get_company_data_profile(WORKSPACE_ID)
    assets = SemanticAssetBootstrap(registry=registry, graph_reader=graph_reader).load_file(
        profile, seed_path
    )
    return [asset.asset_id for asset in assets], retired


class Phase1AcceptanceRunner:
    """Own the one canonical Phase 1 suite and its durable evidence."""

    def __init__(
        self,
        *,
        service_factory: Callable[[Catalog], QanerisService] = QanerisService,
        artifact_base: Path | None = None,
        environment: Mapping[str, str] | None = None,
    ):
        self._service_factory = service_factory
        self._artifact_base = artifact_base
        self._environment = environment if environment is not None else os.environ
        self._redactor = _Redactor(self._environment)

    def run(
        self,
        *,
        question_class: str | None = None,
        repeat: int = 1,
        trace: bool = True,
    ) -> Phase1RunReport:
        if repeat < 1:
            raise AcceptanceConfigurationError("repeat must be at least 1")
        selected = self._select(question_class)
        # Refresh at run time while never retaining or emitting a secret itself. CLI
        # entry points own the shared environment bootstrap.
        self._redactor = _Redactor(self._environment)

        run_id = _new_run_id()
        run_directory = (self._artifact_base or artifact_directory("acceptance") / "phase1") / run_id
        try:
            run_directory.mkdir(parents=True, exist_ok=False)
        except OSError as error:
            raise EnvironmentBlockedError(
                f"cannot create acceptance artifact directory: {self._redactor.text(error)}"
            ) from error

        json_path = run_directory / "report.json"
        markdown_path = run_directory / "report.md"
        report = Phase1RunReport(
            run_id=run_id,
            started_at=_now(),
            completed_at=None,
            status="running",
            environment_summary={
                "artifact_root": {"status": "available", "path": str(run_directory)},
                "model_configuration": {"status": "missing"},
                "neo4j_configuration": {"status": "missing"},
                "neo4j_connectivity": {"status": "unavailable"},
                "catalog_configuration": {
                    "status": "available",
                    "path": str(run_directory / "catalog.db"),
                },
            },
            suite="phase1",
            results=[],
            summary={"total": len(selected) * repeat, "passed": 0, "failed": 0, "blocked": 0},
            artifact_paths={"report_json": str(json_path), "report_markdown": str(markdown_path)},
        )
        self._write(report, json_path, markdown_path)

        service: QanerisService | None = None
        try:
            service, datasource_id = self._prepare(run_directory, report)
            environment_blocked = False
            for attempt in range(1, repeat + 1):
                for scenario in selected:
                    result = self._run_scenario(
                        service,
                        datasource_id,
                        scenario,
                        repeat=attempt,
                        trace=trace,
                    )
                    report.results.append(result)
                    self._update_summary(report)
                    self._write(report, json_path, markdown_path)
                    if result.status == "environment_blocked":
                        self._fill_environment_blocked(
                            report, selected, repeat, result.error or {"type": "EnvironmentError"}
                        )
                        environment_blocked = True
                        break
                if environment_blocked:
                    break
        except Exception as error:  # noqa: BLE001 - setup outages become durable evidence
            self._mark_environment_blocked(report, selected, repeat, error)
        finally:
            self._close_service(service)

        self._update_summary(report)
        report.completed_at = _now()
        if report.summary["blocked"]:
            report.status = "environment_blocked"
        elif report.summary["failed"]:
            report.status = "failed"
        elif report.summary["passed"] == report.summary["total"]:
            report.status = "passed"
        else:
            report.status = "aborted"
        self._write(report, json_path, markdown_path)
        return report

    @staticmethod
    def _select(question_class: str | None) -> list[Phase1Scenario]:
        if question_class is None:
            return list(PHASE1_SCENARIOS)
        normalized = question_class.upper()
        selected = [item for item in PHASE1_SCENARIOS if item.code == normalized]
        if not selected:
            raise AcceptanceConfigurationError(
                f"unknown question class {question_class!r}; expected A, B, C, D, E, or F"
            )
        return selected

    def _prepare(
        self, run_directory: Path, report: Phase1RunReport
    ) -> tuple[QanerisService, str]:
        source_path = run_directory / "source.db"
        catalog_path = run_directory / "catalog.db"
        build_source(source_path, today=datetime.now(UTC).date())
        if resolve_model_profile() is None:
            raise EnvironmentBlockedError("model configuration is missing")
        report.environment_summary["model_configuration"]["status"] = "configured"
        Neo4jConfig.from_environment()
        report.environment_summary["neo4j_configuration"]["status"] = "configured"
        service = self._service_factory(Catalog(catalog_path))
        try:
            if service.model is None:
                raise EnvironmentBlockedError("model configuration is missing")
            datasource, _ = ensure_datasource(service, source_path)
            service.scan_datasource(datasource.id)
            _seed_assets(catalog_path, service.graph_reader)
            report.environment_summary["neo4j_connectivity"]["status"] = "available"
            return service, datasource.id
        except Exception:
            self._close_service(service)
            raise

    def _run_scenario(
        self,
        service: QanerisService,
        datasource_id: str,
        scenario: Phase1Scenario,
        *,
        repeat: int,
        trace: bool,
    ) -> ScenarioResult:
        result = ScenarioResult(
            code=scenario.code,
            title=scenario.title,
            question=scenario.question,
            repeat=repeat,
        )
        try:
            rules = RuleExtractor().extract(scenario.question, datasource_id)
            result.rule_facts = rules.model_dump(mode="json")
            response = service.ask(
                AskRequest(
                    question=scenario.question,
                    workspace_id=WORKSPACE_ID,
                    datasource_id=datasource_id,
                )
            )
            result.ask_status = response.status.value
            result.business_query = _business_query(response.business_query)
            result.clarification = [
                {"question": item.question, "options": list(item.options)}
                for item in response.clarification
            ]
            result.evidence = (
                response.evidence.model_dump(mode="json") if response.evidence else None
            )
            if response.error:
                result.error = {
                    "code": response.error.code,
                    "message": self._redactor.text(response.error.message),
                }

            plan = response.plan
            if plan is not None and not isinstance(plan, list):
                result.plan_id = plan.plan_id
                result.datasource_id = plan.datasource_id
                result.scan_version = plan.scan_version
                result.joins = [
                    {
                        "from": item.from_data_object_id,
                        "to": item.to_data_object_id,
                        "from_field": item.from_field_path,
                        "to_field": item.to_field_path,
                        "relationship_id": getattr(item, "relationship_id", None),
                    }
                    for item in plan.joins
                ]
                native = service.generate_grounded_query(plan)
                result.command = native.command
                result.display_command = native.display_command
                result.parameters = list(native.parameters)

            query_result = response.result
            if query_result is not None and hasattr(query_result, "rows"):
                result.result_summary = {
                    "row_count": query_result.row_count,
                    "truncated": query_result.truncated,
                    "rows": list(query_result.rows[:5]),
                }

            if trace:
                self._trace(service, result, datasource_id, response.business_query, rules)
            result.status = "passed" if self._passes(scenario, result) else "failed"
        except Exception as error:  # noqa: BLE001 - preserve all other scenario evidence
            result.error = self._safe_error(error)
            if self._is_environment_block(error):
                result.status = "environment_blocked"
            else:
                result.status = "failed"
        return result

    def _trace(
        self,
        service: QanerisService,
        result: ScenarioResult,
        datasource_id: str,
        query: Any,
        rules: Any,
    ) -> None:
        if query is None:
            result.notes.append("ask() produced no business query to trace")
            return
        retrieval = service.retrieve_semantics(
            query,
            workspace_id=WORKSPACE_ID,
            requested_datasource_id=datasource_id,
        )
        result.retrieval_candidates = [
            f"{item.asset_type.value}:{item.name}@{item.field_path or '-'}"
            + (" [migration_lookup]" if getattr(item, "migration_lookup", False) else "")
            for item in retrieval.candidates
        ]
        grounding = service.ground_semantics(query, retrieval)
        result.grounding = {
            "is_executable": grounding.is_executable,
            "needs_clarification": grounding.needs_clarification,
            "unresolved_ambiguities": list(grounding.grounded_query.unresolved_ambiguities),
            "bindings": [
                {
                    "term": binding.business_term,
                    "asset_type": binding.asset_type.value,
                    "field_path": binding.field_path,
                    "data_type": binding.data_type,
                }
                for binding in grounding.grounded_query.bindings
            ],
        }
        if not grounding.is_executable:
            return
        time_range = normalize_time_range(rules)
        time_dimensions = [
            binding.business_term
            for binding in grounding.grounded_query.bindings
            if binding.asset_type is SemanticAssetType.DIMENSION
            and binding.data_type
            and binding.data_type.strip().casefold() in DATE_TYPES
        ]
        time_spec = (
            TimeSpec(dimension=time_dimensions[0])
            if query.time_expression and time_range is not None and len(time_dimensions) == 1
            else None
        )
        result.notes.append(f"normalized time range = {time_range}")
        try:
            service.build_query_context(
                grounding,
                workspace_id=WORKSPACE_ID,
                requested_datasource_id=datasource_id,
                time_range=time_range,
                time_spec=time_spec,
            )
        except Exception as error:  # noqa: BLE001 - trace cannot alter the ask() verdict
            result.notes.append(f"context trace: {self._redactor.text(error)}")

    @staticmethod
    def _passes(scenario: Phase1Scenario, result: ScenarioResult) -> bool:
        if result.ask_status != scenario.expected_ask_status.value:
            result.notes.append(
                f"expected ask status {scenario.expected_ask_status.value}, got {result.ask_status}"
            )
            return False
        if scenario.expected_ask_status is AskStatus.CLARIFICATION_REQUIRED:
            if not result.clarification:
                result.notes.append("expected a user-facing clarification payload")
                return False
            blob = json.dumps(result.clarification, ensure_ascii=False)
            leaked = [token for token in ("obj_", "field_", "graph_", "edge_", "ds_") if token in blob]
            if leaked:
                result.notes.append(f"internal identifier leaked into clarification: {leaked}")
                return False
            return True
        if result.plan_id is None or result.result_summary is None or result.evidence is None:
            result.notes.append("completed response is missing plan, typed result, or evidence")
            return False
        if scenario.code == "C" and (
            "上海" in (result.command or "") or not result.parameters
        ):
            result.notes.append("filter value was not safely parameterized")
            return False
        if scenario.code == "D":
            query = result.business_query or {}
            if not query.get("time_expression") or not query.get("ranking"):
                result.notes.append("time/ranking intent was not preserved")
                return False
        if scenario.code == "F" and not result.joins:
            result.notes.append("relationship question produced no governed join")
            return False
        return True

    def _mark_environment_blocked(
        self,
        report: Phase1RunReport,
        selected: list[Phase1Scenario],
        repeat: int,
        error: Exception,
    ) -> None:
        self._fill_environment_blocked(report, selected, repeat, self._safe_error(error))

    @staticmethod
    def _fill_environment_blocked(
        report: Phase1RunReport,
        selected: list[Phase1Scenario],
        repeat: int,
        safe_error: dict[str, Any],
    ) -> None:
        already = {(item.code, item.repeat) for item in report.results}
        for attempt in range(1, repeat + 1):
            for scenario in selected:
                if (scenario.code, attempt) in already:
                    continue
                report.results.append(
                    ScenarioResult(
                        code=scenario.code,
                        title=scenario.title,
                        question=scenario.question,
                        repeat=attempt,
                        status="environment_blocked",
                        error=safe_error,
                    )
                )

    def _safe_error(self, error: Exception) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "type": type(error).__name__,
            "message": self._redactor.text(error)[:800],
        }
        response = getattr(error, "response", None)
        if response is not None:
            payload["status_code"] = getattr(response, "status_code", None)
            payload["body"] = self._redactor.text(getattr(response, "text", ""))[:800]
        return payload

    @staticmethod
    def _is_environment_block(error: Exception) -> bool:
        if isinstance(error, (EnvironmentBlockedError, ModelInvocationError)):
            return True
        response = getattr(error, "response", None)
        return getattr(response, "status_code", None) in {401, 402, 403, 408, 429, 500, 502, 503, 504}

    @staticmethod
    def _update_summary(report: Phase1RunReport) -> None:
        report.summary = {
            "total": report.summary.get("total", len(report.results)),
            "passed": sum(item.status == "passed" for item in report.results),
            "failed": sum(item.status == "failed" for item in report.results),
            "blocked": sum(item.status == "environment_blocked" for item in report.results),
        }

    @staticmethod
    def _close_service(service: QanerisService | None) -> None:
        if service is None:
            return
        seen: set[int] = set()
        for resource in (service.graph_reader, service.graph_store):
            if id(resource) in seen:
                continue
            seen.add(id(resource))
            close = getattr(resource, "close", None)
            if callable(close):
                with suppress(Exception):
                    close()

    @staticmethod
    def _write(report: Phase1RunReport, json_path: Path, markdown_path: Path) -> None:
        payload = report.to_dict()
        json_path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2, default=str) + "\n",
            encoding="utf-8",
        )
        markdown_path.write_text(_render_markdown(payload), encoding="utf-8")


def _render_markdown(report: dict[str, Any]) -> str:
    """Render human evidence from the exact structure persisted as JSON."""

    summary = report["summary"]
    lines = [
        "# Phase 1 E2E Acceptance",
        "",
        "## Run",
        "",
        f"- Run ID: `{report['run_id']}`",
        f"- Started: {report['started_at']}",
        f"- Completed: {report['completed_at'] or 'in progress'}",
        f"- Status: **{report['status']}**",
        "",
        "## Environment",
        "",
    ]
    for name, detail in report["environment_summary"].items():
        lines.append(f"- {name.replace('_', ' ').title()}: {detail['status']}")
    lines.extend(
        [
            "",
            "## Summary",
            "",
            (
                f"{summary['passed']}/{summary['total']} passed; "
                f"{summary['failed']} failed; {summary['blocked']} blocked."
            ),
            "",
        ]
    )
    for item in report["results"]:
        suffix = f" (repeat {item['repeat']})" if item["repeat"] > 1 else ""
        lines.extend(
            [
                f"## {item['code']} {item['title']}{suffix}",
                "",
                f"- Status: **{item['status']}**",
                f"- Question: {item['question']}",
                f"- Ask status: {item['ask_status'] or '-'}",
                f"- Plan: `{item['plan_id'] or '-'}`",
                f"- Datasource: `{item['datasource_id'] or '-'}`",
                f"- Scan version: {item['scan_version'] if item['scan_version'] is not None else '-'}",
                f"- Result: `{json.dumps(item['result_summary'], ensure_ascii=False, default=str)}`",
                f"- Clarification: `{json.dumps(item['clarification'], ensure_ascii=False)}`",
                "",
            ]
        )
    problems = [item for item in report["results"] if item["status"] != "passed"]
    warnings = [item for item in report["results"] if item["notes"]]
    lines.extend(["## Failures / Warnings", ""])
    if not problems and not warnings:
        lines.append("None.")
    else:
        for item in problems:
            lines.append(
                f"- {item['code']} repeat {item['repeat']}: "
                f"{json.dumps(item['error'], ensure_ascii=False) if item['error'] else item['status']}"
            )
        for item in warnings:
            for note in item["notes"]:
                lines.append(f"- {item['code']} repeat {item['repeat']}: {note}")
    return "\n".join(lines) + "\n"
