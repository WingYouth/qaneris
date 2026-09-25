from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

from smartdata.adapters import create_adapter
from smartdata.catalog import Catalog
from smartdata.common.redaction import safe_error, sensitive_values
from smartdata.connections.ports import ConnectionProvider
from smartdata.connections.provider import DatasourceConnectionProvider
from smartdata.contracts.initialization import (
    InitializationExecutor,
    InitializationJob,
    InlineInitializationExecutor,
)
from smartdata.contracts.profile import DocumentStatus, ScanPolicy, ScanSnapshot, ScanStatus
from smartdata.graph import GraphStore, graph_store_from_environment
from smartdata.profiling.builder import ProfileBuilder
from smartdata.profiling.documents import (
    FileSystemProfileDocumentWriter,
    ProfileDocumentWriter,
    default_profile_document_root,
)
from smartdata.scan import ScanGraphBuilder, ScanGraphValidator


class DatabaseInitializer:
    def __init__(
        self,
        catalog: Catalog,
        connection_provider: ConnectionProvider | None = None,
        executor: InitializationExecutor | None = None,
        profile_builder: ProfileBuilder | None = None,
        document_writer: ProfileDocumentWriter | None = None,
        graph_store: GraphStore | None = None,
        generate_profile_documents: bool = True,
    ):
        self.catalog = catalog
        self.connection_provider = connection_provider or DatasourceConnectionProvider(catalog)
        self.executor = executor or InlineInitializationExecutor()
        self.profile_builder = profile_builder or ProfileBuilder()
        self.graph_store = graph_store or graph_store_from_environment()
        self.graph_builder = ScanGraphBuilder()
        self.graph_validator = ScanGraphValidator()
        self.generate_profile_documents = generate_profile_documents or document_writer is not None
        self.document_writer = document_writer or FileSystemProfileDocumentWriter(
            default_profile_document_root(catalog.path)
        )

    def initialize(self, datasource_id: str, policy: ScanPolicy | None = None) -> InitializationJob:
        job = self.catalog.create_initialization_job(datasource_id, policy)
        return self.executor.execute(lambda: self._run(job))

    def _run(self, job: InitializationJob) -> InitializationJob:
        datasource, _ = self.catalog.get_datasource(job.datasource_id)
        connection: dict[str, Any] | None = None
        # The connection is opened once and held for the whole job, so a TLS-enabled datasource
        # keeps its temporary certificate material alive across test_connection and every scan call
        # and has it removed when the job ends - including when a scan raises.
        with self.connection_provider.open(datasource.id) as opened:
            connection = opened
            adapter = create_adapter(datasource.id, datasource.kind, connection)
            try:
                self.catalog.update_initialization_job(job.id, ScanStatus.VERIFYING)
                adapter.test_connection()
                self.catalog.update_initialization_job(job.id, ScanStatus.CONNECTION_VERIFIED)
            except Exception as error:  # noqa: BLE001 - persist adapter failure as job state
                return self.catalog.update_initialization_job(
                    job.id,
                    ScanStatus.CONNECTION_FAILED,
                    error_code=type(error).__name__,
                    error_message=safe_error(error, secrets=sensitive_values(connection)),
                )

            try:
                self.catalog.update_initialization_job(job.id, ScanStatus.SCANNING)
                datasets = adapter.scan_metadata()
                relations = adapter.scan_relations()
                previews = adapter.scan_samples(datasets, limit=job.policy.preview_size)
                profile_records = adapter.scan_profile_records(
                    datasets, limit=job.policy.profile_size
                )
            except Exception as error:  # noqa: BLE001 - persist scan failure as job state
                return self.catalog.update_initialization_job(
                    job.id,
                    ScanStatus.SCAN_FAILED,
                    error_code=type(error).__name__,
                    error_message=safe_error(error, secrets=sensitive_values(connection)),
                )

            try:
                self.catalog.update_initialization_job(job.id, ScanStatus.PROFILING)
                profile, profile_relations = self.profile_builder.build(
                    datasource,
                    datasets,
                    relations,
                    previews,
                    profile_records,
                    job.policy,
                )
                now = datetime.now(UTC)
                snapshot = ScanSnapshot(
                    id=f"snap_{uuid.uuid4().hex[:12]}",
                    datasource_id=datasource.id,
                    version=self.catalog.next_snapshot_version(datasource.id),
                    status=ScanStatus.READY,
                    policy=job.policy,
                    profile=profile,
                    relationships=profile_relations,
                    started_at=job.created_at,
                    completed_at=now,
                    active=True,
                )
                graph = self.graph_builder.build(
                    datasource, datasets, relations, version=snapshot.version, scanned_at=now
                )
                self.graph_validator.validate(graph)
                self.graph_store.ensure_schema()
                self.graph_store.replace_datasource_graph(graph)
                if self.generate_profile_documents:
                    self._write_profile_documents(snapshot, datasource.workspace_id)
                self.catalog.commit_scan_snapshot(snapshot, datasets, relations, previews)
                return self.catalog.update_initialization_job(
                    job.id, ScanStatus.READY, snapshot_id=snapshot.id
                )
            except Exception as error:  # noqa: BLE001 - persist profile failure as job state
                return self.catalog.update_initialization_job(
                    job.id,
                    ScanStatus.PROFILE_FAILED,
                    error_code=type(error).__name__,
                    error_message=safe_error(error, secrets=sensitive_values(connection)),
                )

    def regenerate_profile_documents(self, datasource_id: str) -> ScanSnapshot:
        datasource, _ = self.catalog.get_datasource(datasource_id)
        snapshot = self.catalog.get_active_snapshot(datasource_id)
        if snapshot is None:
            raise KeyError(f"Active scan snapshot not found: {datasource_id}")
        self._write_profile_documents(snapshot, datasource.workspace_id)
        self.catalog.update_snapshot_document_state(snapshot)
        return snapshot

    def _write_profile_documents(self, snapshot: ScanSnapshot, workspace_id: str) -> None:
        failure_warning = "画像文档生成失败，可在扫描快照上单独重试"
        try:
            snapshot.document_path = str(self.document_writer.write(snapshot, workspace_id))
            snapshot.document_status = DocumentStatus.READY
            snapshot.document_error = None
            snapshot.warnings = [
                warning for warning in snapshot.warnings if warning != failure_warning
            ]
        except Exception as error:  # noqa: BLE001 - document generation is independently retryable
            snapshot.document_status = DocumentStatus.FAILED
            snapshot.document_path = None
            snapshot.document_error = f"{type(error).__name__}: {error}"
            if failure_warning not in snapshot.warnings:
                snapshot.warnings.append(failure_warning)
