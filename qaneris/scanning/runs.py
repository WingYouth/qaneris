"""Application-level orchestration for multi-datasource connection tests and scans."""

from __future__ import annotations

import json
from collections.abc import Callable, Sequence
from contextlib import suppress
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

from qaneris.adapters import create_adapter
from qaneris.application.service import QanerisService
from qaneris.catalog import Catalog
from qaneris.common.artifacts import artifact_directory
from qaneris.common.redaction import SecretRedactor, safe_error
from qaneris.connections.provider import DatasourceConnectionProvider
from qaneris.connections.secrets import SecretResolver
from qaneris.contracts import (
    ConnectionTestReport,
    ConnectionTestResult,
    Neo4jValidation,
    ScanRunInfo,
    ScanRunMember,
    ScanRunReport,
    ScanRunSummary,
    ScanStatus,
    SecureDatasourceCreate,
)
from qaneris.contracts.datasource import DatasetInfo, RelationInfo
from qaneris.contracts.profile import ScanSnapshot
from qaneris.graph.reading import GraphStructureRequest
from qaneris.profiling.documents import FileSystemProfileDocumentWriter


class ScanRunConfigurationError(ValueError):
    """Raised when a batch cannot form one unambiguous ScanRun."""


def _now() -> datetime:
    return datetime.now(UTC)


def _new_run_id() -> str:
    return f"{_now().strftime('%Y%m%dT%H%M%S.%fZ')}-{uuid4().hex[:8]}"


def _profile_secrets(
    resolver: SecretResolver, request: SecureDatasourceCreate
) -> list[str]:
    """Collect the candidate's secret values for redaction only.

    The values are resolved independently of the connection attempt because the failure path no
    longer holds a resolved connection: the context manager has already closed it. A resolution
    failure returns nothing to redact rather than masking the original error.
    """
    try:
        resolved = resolver.materialize(request.connection_profile)
    except Exception:  # noqa: BLE001 - redaction is best-effort and must not replace the error
        return []
    return [*resolved.secrets.values(), *resolved.tls_material.values()]


class SnapshotGraphValidator:
    """Read back one published graph and compare it with one active snapshot."""

    def validate(
        self,
        service: QanerisService,
        snapshot: ScanSnapshot,
        datasets: list[DatasetInfo],
        relations: list[RelationInfo],
    ) -> Neo4jValidation:
        expected = {
            "datasource_id": snapshot.datasource_id,
            "scan_version": snapshot.version,
            "objects": len(datasets),
            "fields": sum(len(item.fields) for item in datasets),
            "relationships": len(relations),
        }
        try:
            structure = service.graph_reader.read_structure(
                GraphStructureRequest(
                    workspace_id=self._workspace_id(service, snapshot.datasource_id),
                    datasource_id=snapshot.datasource_id,
                    max_data_objects=max(1, min(expected["objects"] + 1, 2_000)),
                    max_fields=max(1, min(expected["fields"] + 1, 20_000)),
                    max_relationships=max(1, min(expected["relationships"] + 1, 2_000)),
                )
            )
            nodes = [
                item
                for item in structure.datasources
                if item.datasource_id == snapshot.datasource_id
            ]
            actual = {
                "datasource_id": nodes[0].datasource_id if len(nodes) == 1 else None,
                "scan_version": nodes[0].scan_version if len(nodes) == 1 else None,
                "objects": len(structure.data_objects),
                "fields": len(structure.fields),
                "relationships": len(structure.relationships),
            }
            checks = {
                "database_node_exists": len(nodes) == 1,
                "datasource_id_matches": actual["datasource_id"] == expected["datasource_id"],
                "scan_version_matches": actual["scan_version"] == expected["scan_version"],
                "data_object_count_matches": actual["objects"] == expected["objects"],
                "field_count_matches": actual["fields"] == expected["fields"],
                "relationship_count_matches": actual["relationships"]
                == expected["relationships"],
                "active_snapshot_matches": snapshot.active
                and snapshot.status is ScanStatus.READY
                and actual["scan_version"] == snapshot.version,
                "graph_read_not_truncated": not structure.truncated,
            }
            return Neo4jValidation(
                status="passed" if all(checks.values()) else "failed",
                checks=checks,
                expected=expected,
                actual=actual,
            )
        except Exception as error:  # noqa: BLE001 - validation failure is report evidence
            return Neo4jValidation(
                status="failed",
                expected=expected,
                error=safe_error(error),
            )

    @staticmethod
    def _workspace_id(service: QanerisService, datasource_id: str) -> str:
        return service.catalog.get_datasource(datasource_id)[0].workspace_id


class MultiDatabaseScanRunner:
    """Coordinate a batch without weakening per-datasource snapshot identity."""

    def __init__(
        self,
        *,
        artifact_base: Path | None = None,
        service_factory: Callable[[Catalog], QanerisService] | None = None,
        secret_resolver: SecretResolver | None = None,
        connection_provider: DatasourceConnectionProvider | None = None,
        graph_validator: SnapshotGraphValidator | None = None,
    ):
        self.secret_resolver = secret_resolver or SecretResolver()
        # The batch connection test uses the same provider boundary a stored datasource uses, so a
        # candidate with TLS gets the same materialization and cleanup as one that was saved. Only
        # ``open_profile`` is reached here, so the runner needs no catalog of its own.
        self.connection_provider = connection_provider or DatasourceConnectionProvider(
            catalog=None, secret_resolver=self.secret_resolver
        )
        self.artifact_base = artifact_base
        self.service_factory = service_factory or (
            lambda catalog: QanerisService(catalog, secret_resolver=self.secret_resolver)
        )
        self.graph_validator = graph_validator or SnapshotGraphValidator()

    def test_connections(
        self, requests: Sequence[SecureDatasourceCreate]
    ) -> ConnectionTestReport:
        self._workspace_id(requests)
        results = [self._test_connection(request) for request in requests]
        passed = sum(item.status == "passed" for item in results)
        return ConnectionTestReport(
            status="passed" if passed == len(results) else "failed",
            configured_datasources=len(results),
            connection_passed=passed,
            connection_failed=len(results) - passed,
            results=results,
        )

    def scan(
        self,
        requests: Sequence[SecureDatasourceCreate],
        *,
        config_source: str,
        catalog_path: Path | None = None,
    ) -> ScanRunReport:
        workspace_id = self._workspace_id(requests)
        run_id = _new_run_id()
        run_directory = (self.artifact_base or artifact_directory("scan-runs")) / run_id
        run_directory.mkdir(parents=True, exist_ok=False)
        catalog = (catalog_path or run_directory / "catalog.db").expanduser().resolve()
        catalog.parent.mkdir(parents=True, exist_ok=True)
        paths = {
            "manifest": str(run_directory / "manifest.json"),
            "scan_report_json": str(run_directory / "scan_report.json"),
            "scan_report_markdown": str(run_directory / "scan_report.md"),
            "catalog": str(catalog),
            "profiles": str(run_directory / "profiles"),
        }
        report = ScanRunReport(
            run=ScanRunInfo(
                run_id=run_id,
                workspace_id=workspace_id,
                started_at=_now(),
                status="running",
                config_source=config_source,
            ),
            summary=self._summary([], len(requests)),
            artifact_paths=paths,
        )
        self._write(report)

        connection_results = [self._test_connection(request) for request in requests]
        service: QanerisService | None = None
        try:
            try:
                service = self.service_factory(Catalog(catalog))
                service.initializer.document_writer = FileSystemProfileDocumentWriter(
                    run_directory / "profiles"
                )
            except Exception as error:  # noqa: BLE001 - all members retain a result
                failure = safe_error(error)
                for request, connection in zip(requests, connection_results, strict=True):
                    report.datasources.append(
                        self._blocked_member(request, connection, failure)
                    )
                return self._complete(report)

            for request, connection in zip(requests, connection_results, strict=True):
                if connection.status != "passed":
                    member = self._blocked_member(request, connection, connection.error)
                else:
                    member = self._scan_one(service, request)
                report.datasources.append(member)
                self._refresh(report)
                self._write(report)
            return self._complete(report)
        finally:
            self._close_service(service)

    def _test_connection(self, request: SecureDatasourceCreate) -> ConnectionTestResult:
        try:
            with self.connection_provider.open_profile(request.connection_profile) as connection:
                create_adapter("connection_test", request.kind, connection).test_connection()
            return ConnectionTestResult(
                name=request.name,
                driver=request.connection_profile.driver,
                workspace_id=request.workspace_id,
                status="passed",
            )
        except Exception as error:  # noqa: BLE001 - continue through the full batch
            return ConnectionTestResult(
                name=request.name,
                driver=request.connection_profile.driver,
                workspace_id=request.workspace_id,
                status="failed",
                error=safe_error(error, secrets=_profile_secrets(self.secret_resolver, request)),
            )

    def _scan_one(
        self, service: QanerisService, request: SecureDatasourceCreate
    ) -> ScanRunMember:
        member = ScanRunMember(
            name=request.name,
            driver=request.connection_profile.driver,
            kind=request.kind.value,
            workspace_id=request.workspace_id,
            status="failed",
            connection_status="passed",
            scan_status="failed",
        )
        try:
            datasource = service.create_secure_datasource(request)
            member.datasource_id = datasource.id
            # Secure datasource creation tests and saves only; the scan is an explicit step, and
            # this runner is that step. ``scan_datasource`` raises when initialization does not
            # reach READY, so the check below is a second, independent confirmation.
            service.scan_datasource(datasource.id)
            if service.catalog.get_datasource(datasource.id)[0].status != ScanStatus.READY.value:
                raise RuntimeError("initialization did not commit a READY datasource")
            snapshot = service.catalog.get_active_snapshot(datasource.id)
            if snapshot is None:
                raise RuntimeError("READY datasource has no active ScanSnapshot")
            datasets = service.catalog.list_datasets(request.workspace_id, datasource.id)
            relations = service.catalog.list_relations(request.workspace_id, datasource.id)
            member.snapshot_id = snapshot.id
            member.scan_version = snapshot.version
            member.scan_status = "passed"
            member.objects = len(datasets)
            member.fields = sum(len(item.fields) for item in datasets)
            member.indexes = sum(len(item.indexes) for item in datasets)
            member.constraints = sum(len(item.constraints) for item in datasets)
            member.relationships = len(relations)
            member.document_status = snapshot.document_status.value
            member.document_path = (
                str(Path(snapshot.document_path) / "database_profile.md")
                if snapshot.document_path
                else None
            )
            member.warnings = list(snapshot.warnings)
            validation = self.graph_validator.validate(service, snapshot, datasets, relations)
            member.neo4j_validation = validation
            member.neo4j_status = validation.status
            document_ready = snapshot.document_status.value == "ready"
            member.status = (
                "passed" if validation.status == "passed" and document_ready else "failed"
            )
            if member.status == "failed":
                failures = []
                if validation.status != "passed":
                    failures.append("Neo4j validation failed")
                if not document_ready:
                    failures.append("profile document is not ready")
                member.error = "; ".join(failures)
        except Exception as error:  # noqa: BLE001 - one datasource cannot stop the batch
            member.error = safe_error(error)
            matches = [
                item
                for item in service.list_datasources(request.workspace_id)
                if item.name == request.name
            ]
            if len(matches) == 1:
                member.datasource_id = matches[0].id
        return member

    @staticmethod
    def _blocked_member(
        request: SecureDatasourceCreate,
        connection: ConnectionTestResult,
        error: str | None,
    ) -> ScanRunMember:
        return ScanRunMember(
            name=request.name,
            driver=request.connection_profile.driver,
            kind=request.kind.value,
            workspace_id=request.workspace_id,
            status="failed",
            connection_status=connection.status,
            scan_status="not_run",
            error=error or "scan could not start",
        )

    def _complete(self, report: ScanRunReport) -> ScanRunReport:
        self._refresh(report)
        report.run.completed_at = _now()
        report.run.status = (
            "passed"
            if report.summary.scan_passed == report.summary.configured_datasources
            else "failed"
        )
        self._write(report)
        return report

    @staticmethod
    def _workspace_id(requests: Sequence[SecureDatasourceCreate]) -> str:
        if not requests:
            raise ScanRunConfigurationError("at least one datasource is required")
        workspaces = {request.workspace_id for request in requests}
        if len(workspaces) != 1:
            raise ScanRunConfigurationError(
                "one ScanRun cannot span multiple workspaces: " + ", ".join(sorted(workspaces))
            )
        return next(iter(workspaces))

    @staticmethod
    def _summary(members: list[ScanRunMember], configured: int) -> ScanRunSummary:
        return ScanRunSummary(
            configured_datasources=configured,
            connection_passed=sum(item.connection_status == "passed" for item in members),
            scan_passed=sum(item.status == "passed" for item in members),
            scan_failed=sum(item.status == "failed" for item in members),
            total_data_objects=sum(item.objects for item in members),
            total_fields=sum(item.fields for item in members),
            total_indexes=sum(item.indexes for item in members),
            total_constraints=sum(item.constraints for item in members),
            total_relationships=sum(item.relationships for item in members),
        )

    def _refresh(self, report: ScanRunReport) -> None:
        report.summary = self._summary(
            report.datasources, report.summary.configured_datasources
        )
        report.neo4j_validation = [
            {
                "name": member.name,
                **member.neo4j_validation.model_dump(mode="json"),
            }
            for member in report.datasources
            if member.neo4j_validation is not None
        ]
        report.warnings = [
            f"{member.name}: {warning}"
            for member in report.datasources
            for warning in member.warnings
        ]
        report.failures = [
            {"name": member.name, "error": member.error or member.status}
            for member in report.datasources
            if member.status != "passed"
        ]

    def _write(self, report: ScanRunReport) -> None:
        redactor = SecretRedactor.from_environment()
        payload = redactor.value(report.model_dump(mode="json"))
        manifest = redactor.value(self._manifest(payload))
        Path(report.artifact_paths["manifest"]).write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        Path(report.artifact_paths["scan_report_json"]).write_text(
            json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        Path(report.artifact_paths["scan_report_markdown"]).write_text(
            self._markdown(payload), encoding="utf-8"
        )

    @staticmethod
    def _manifest(report: dict[str, Any]) -> dict[str, Any]:
        run = report["run"]
        return {
            "run_id": run["run_id"],
            "workspace_id": run["workspace_id"],
            "started_at": run["started_at"],
            "completed_at": run["completed_at"],
            "status": run["status"],
            "datasources": [
                {
                    "name": item["name"],
                    "datasource_id": item["datasource_id"],
                    "driver": item["driver"],
                    "snapshot_id": item["snapshot_id"],
                    "scan_version": item["scan_version"],
                    "status": item["status"],
                    "profile_document": item["document_path"],
                }
                for item in report["datasources"]
            ],
        }

    @staticmethod
    def _markdown(report: dict[str, Any]) -> str:
        run = report["run"]
        summary = report["summary"]
        lines = [
            "# Qaneris Multi-Database Scan Report",
            "",
            "## Run",
            "",
            f"- Run ID: `{run['run_id']}`",
            f"- Workspace: `{run['workspace_id']}`",
            f"- Started: {run['started_at']}",
            f"- Completed: {run['completed_at'] or 'in progress'}",
            f"- Status: **{run['status']}**",
            "",
            "## Summary",
            "",
            f"- Configured: {summary['configured_datasources']}",
            f"- Connection Passed: {summary['connection_passed']}",
            f"- Scan Passed: {summary['scan_passed']}",
            f"- Failed: {summary['scan_failed']}",
            f"- Total Data Objects: {summary['total_data_objects']}",
            f"- Total Fields: {summary['total_fields']}",
            f"- Total Indexes: {summary['total_indexes']}",
            f"- Total Constraints: {summary['total_constraints']}",
            f"- Total Relationships: {summary['total_relationships']}",
            "",
            "## Datasources",
            "",
        ]
        for item in report["datasources"]:
            lines.extend(
                [
                    f"### {item['name']}",
                    "",
                    f"- Driver: `{item['driver']}`",
                    f"- Status: **{item['status']}**",
                    f"- Snapshot: `{item['snapshot_id'] or '-'}`",
                    f"- Scan version: {item['scan_version'] or '-'}",
                    f"- Objects / fields: {item['objects']} / {item['fields']}",
                    (
                        f"- Indexes / constraints / relationships: {item['indexes']} / "
                        f"{item['constraints']} / {item['relationships']}"
                    ),
                    f"- Profile document: `{item['document_path'] or '-'}`",
                    f"- Neo4j: {item['neo4j_status']}",
                    "",
                ]
            )
        lines.extend(["## Neo4j Validation", ""])
        if report["neo4j_validation"]:
            for item in report["neo4j_validation"]:
                lines.append(f"- {item['name']}: **{item['status']}**")
        else:
            lines.append("- Not run.")
        lines.extend(["", "## Failures / Warnings", ""])
        if not report["failures"] and not report["warnings"]:
            lines.append("None.")
        else:
            lines.extend(
                f"- FAILURE {item['name']}: {item['error']}" for item in report["failures"]
            )
            lines.extend(f"- WARNING {item}" for item in report["warnings"])
        return "\n".join(lines) + "\n"

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
