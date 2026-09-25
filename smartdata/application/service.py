"""Application facade for datasource and ask-data use cases."""

from __future__ import annotations

import logging
import os
import re
from collections.abc import Iterator
from dataclasses import dataclass
from statistics import mean
from typing import Any
from uuid import uuid4

from smartdata.adapters import create_adapter
from smartdata.adapters.registry import list_registered_adapters
from smartdata.answering import AnswerComposer
from smartdata.catalog import Catalog
from smartdata.common.errors import (
    DatasourceConnectionTestError,
    DatasourceDeleteError,
    DatasourceNotFoundError,
    DatasourceNotReadyError,
    DatasourceSecureProfileRequiredError,
    DatasourceUnavailableError,
    DatasourceUpdateError,
    GraphUnavailableError,
    IntentParsingError,
    ModelInvocationError,
    QueryPlanningError,
    SmartDataError,
)
from smartdata.common.numbers import normalize_number, normalize_rows
from smartdata.common.redaction import SecretRedactor, safe_error, sensitive_values
from smartdata.connections.credential_service import CredentialService
from smartdata.connections.managed_store import ManagedCredentialStore
from smartdata.connections.provider import DatasourceConnectionProvider
from smartdata.connections.references import managed_secret_ids
from smartdata.connections.secrets import SecretResolver
from smartdata.connections.tls_matrix import TLS_DRIVER_MATRIX
from smartdata.contracts import (
    AskClarification,
    AskEvent,
    AskEventType,
    AskRequest,
    AskResponse,
    AskStatus,
    DatasetInfo,
    Datasource,
    DatasourceCreate,
    DatasourceDetail,
    DatasourceKind,
    ErrorDetail,
    GovernanceSuggestion,
    MappingInfo,
    NormalizedResult,
    QueryPlanStep,
    RelationInfo,
)
from smartdata.contracts.connection import (
    ConnectionProfile,
    SecureDatasourceCreate,
    SecureDatasourceTest,
    SecureDatasourceTestResult,
    SecureDatasourceUpdate,
    contains_inline_secret,
)
from smartdata.contracts.credentials import ManagedSecretInfo, ManagedSecretKind
from smartdata.contracts.initialization import InitializationJob
from smartdata.contracts.profile import ScanStatus
from smartdata.contracts.query import (
    GroundedExecution,
    GroundedQueryContext,
    GroundedQueryPlan,
    NativeQuery,
    PreparedQuery,
    TimeRange,
    TimeSpec,
)
from smartdata.contracts.semantic import BusinessQuery
from smartdata.graph import (
    GraphReader,
    GraphStore,
    graph_reader_from_environment,
    graph_store_from_environment,
)
from smartdata.graph.reading import GraphStructureRequest
from smartdata.ingestion.excel import (
    ExcelImportRequest,
    ExcelImportResult,
    ExcelIngestionService,
)
from smartdata.initialization import DatabaseInitializer
from smartdata.llm.gateway import AiyallmSchemaModel, summarize_without_model
from smartdata.llm.ports import SchemaModel
from smartdata.querying import (
    ExecutionRevisionValidator,
    GroundedNativeQueryValidator,
    GroundedPlanValidator,
    GroundedQueryPlanner,
    QueryContextBuilder,
    QueryPreparationPipeline,
)
from smartdata.querying.execution import GroundedQueryExecutor
from smartdata.querying.generation.native import GroundedNativeCompiler
from smartdata.querying.retrieval.schema_context import build_schema_context
from smartdata.security import is_sensitive_field
from smartdata.semantic import (
    GraphSemanticRetriever,
    GroundingResult,
    IntentUnderstandingPipeline,
    LLMBusinessParser,
    SemanticGrounder,
    SemanticRetrievalResult,
    SQLiteSemanticAssetRegistry,
    normalize_time_range,
)
from smartdata.semantic.models import ClarificationRequest, IntentUnderstandingResult

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class _AskEventRecord:
    """Internal envelope pairing a public event with non-public control state."""

    event: AskEvent
    response: AskResponse | None = None
    exception: Exception | None = None


class SmartDataService:
    def __init__(
        self,
        catalog: Catalog | None = None,
        model: SchemaModel | None = None,
        secret_resolver: SecretResolver | None = None,
        graph_store: GraphStore | None = None,
        graph_reader: GraphReader | None = None,
        credential_service: CredentialService | None = None,
    ):
        self.catalog = catalog or Catalog()
        self.model = model or AiyallmSchemaModel.from_environment()
        self.connection_provider = DatasourceConnectionProvider(self.catalog, secret_resolver)
        self._credential_service = credential_service
        self.graph_store = graph_store or graph_store_from_environment()
        self.graph_reader = graph_reader or graph_reader_from_environment()
        self.initializer = DatabaseInitializer(
            self.catalog,
            connection_provider=self.connection_provider,
            graph_store=self.graph_store,
        )
        self.query_preparation = QueryPreparationPipeline(self.catalog)
        self.query_context_builder = QueryContextBuilder()
        self.grounded_planner = GroundedQueryPlanner()
        self.grounded_plan_validator = GroundedPlanValidator()
        self.grounded_native_compiler = GroundedNativeCompiler()
        self.grounded_sql_compiler = self.grounded_native_compiler.sql
        self.grounded_native_validator = GroundedNativeQueryValidator()
        self.execution_revision_validator = ExecutionRevisionValidator()
        self.grounded_executor = GroundedQueryExecutor(self.connection_provider)
        self.answer_composer = AnswerComposer(self.model)
        self.intent_understanding = (
            IntentUnderstandingPipeline(LLMBusinessParser(self.model)) if self.model else None
        )
        self._excel_ingestion: ExcelIngestionService | None = None

    @classmethod
    def from_environment(cls) -> SmartDataService:
        """Build the deployment's service from environment configuration.

        A process entry point (API, CLI, MCP) needs a service bound to the configured catalog, and
        every one of them must resolve that the same way. Keeping the convention here means an
        interface layer never constructs a ``Catalog`` itself - which is what keeps the catalog
        path a deployment decision rather than four independent ones.
        """
        return cls(Catalog(os.getenv("SMARTDATA_CATALOG", "smartdata.db")))

    def create_datasource(self, request: DatasourceCreate) -> Datasource:
        if contains_inline_secret(request.connection):
            raise ValueError("敏感连接信息必须使用 Secret Reference（密钥引用）")
        adapter = create_adapter("connection_test", request.kind, request.connection)
        adapter.test_connection()
        return self.catalog.create_datasource(request)

    # -- managed secrets ---------------------------------------------------------------------

    def create_managed_secret(
        self,
        kind: ManagedSecretKind,
        value: str | bytes,
        *,
        private_key_password: str | None = None,
    ) -> ManagedSecretInfo:
        """Create one managed secret and return its public record.

        The only credential-creation entry point a product interface may call, so every upload
        boundary - HTTP today, CLI and Web later - shares the same validation, normalization and
        storage instead of each inventing its own.
        """
        return self._credentials().create_secret(
            kind, value, private_key_password=private_key_password
        )

    def create_managed_client_identity(
        self,
        certificate: str | bytes,
        private_key: str | bytes,
        *,
        private_key_password: str | None = None,
    ) -> tuple[ManagedSecretInfo, ManagedSecretInfo]:
        """Validate and store one mTLS client identity, returning ``(certificate, key)``.

        The pair is checked and the two secrets are written all-or-nothing by the credential
        service, so a product caller never has to reason about a half-created identity.
        """
        return self._credentials().create_client_identity(
            certificate, private_key, private_key_password=private_key_password
        )

    def inspect_managed_secret(self, secret_id: str) -> ManagedSecretInfo:
        """Return the public record of one managed secret, without decrypting it."""
        return self._credentials().inspect_secret(secret_id)

    def delete_managed_secret(self, secret_id: str) -> None:
        """Delete one managed secret, refusing while a connection profile still references it."""
        self._credentials().delete_secret(secret_id)

    # -- secure datasource lifecycle ---------------------------------------------------------

    def _credentials(self) -> CredentialService:
        """The credential service used for rotation cleanup, built on first use.

        It is only needed when a managed secret actually becomes unreferenced, so a deployment that
        uses environment or file references - which is what round one targets - never has to
        configure the managed store to update or delete a datasource.
        """
        if self._credential_service is None:
            self._credential_service = CredentialService(
                ManagedCredentialStore.from_environment(), self.catalog
            )
        return self._credential_service

    def _test_secure_connection(
        self,
        kind: DatasourceKind,
        profile: ConnectionProfile,
    ) -> None:
        """Resolve a candidate profile and prove the connection really works.

        This is the one place a candidate connection is exercised, and every secure entry point
        goes through it. The profile's ``SecretReference`` values are materialized, translated at
        the adapter boundary and handed to a real connection test; nothing is written to the
        catalog, the graph or the credential store.

        A driver failure is converted to ``datasource_connection_test_failed`` and sanitized on the
        way out. The resolved connection - and therefore every credential value - stays in this
        frame: the raised error is built from the driver's exception only, with the materialized
        secrets passed to the redactor as extra values, and the resolved dict itself is never
        rendered, logged or returned.
        """
        connection: dict[str, Any] | None = None
        try:
            with self.connection_provider.open_profile(profile) as connection:
                create_adapter("connection_test", kind, connection).test_connection()
        except SmartDataError:
            raise
        except Exception as error:
            raise DatasourceConnectionTestError(
                safe_error(error, secrets=sensitive_values(connection))
            ) from error

    def test_secure_datasource(
        self,
        request: SecureDatasourceTest,
    ) -> SecureDatasourceTestResult:
        """Test a candidate connection without saving, scanning or publishing anything.

        This is the ``Test`` step of ``Test → Save → Scan`` and it is deliberately read-only with
        respect to every other component: it writes no datasource, creates no initialization job,
        runs no scan and touches neither the catalog nor the graph. What it returns is a safe
        three-field summary, so a caller can decide whether to save the profile without ever
        handling a resolved secret.
        """
        self._test_secure_connection(request.kind, request.connection_profile)
        return SecureDatasourceTestResult(
            ok=True,
            driver=request.connection_profile.driver,
            tls_enabled=request.connection_profile.tls.enabled,
        )

    def test_saved_datasource(self, datasource_id: str) -> dict[str, bool | str]:
        """Check a saved connection without changing its catalog or scan state."""
        datasource = self._resolve_datasource(datasource_id)
        profile = self.catalog.get_connection_profile(datasource_id)
        if profile is not None:
            self._test_secure_connection(datasource.kind, profile)
        else:
            _, connection = self.catalog.get_datasource(datasource_id)
            try:
                create_adapter("connection_test", datasource.kind, connection).test_connection()
            except SmartDataError:
                raise
            except Exception as error:
                raise DatasourceConnectionTestError(safe_error(error)) from error
        return {"ok": True, "datasource_id": datasource_id}

    def create_secure_datasource(self, request: SecureDatasourceCreate) -> Datasource:
        """Test a candidate connection, then save it as a datasource in ``created`` state.

        The scan is deliberately not run here. The formal product chain is ``Test → Save → Scan``,
        and ``scan_datasource`` is the only scan entry point; a create that also scanned would make
        "saved but not yet published" impossible to express and would hide a scan failure behind a
        create call. The returned datasource therefore reports ``created`` until the caller scans.
        """
        self._test_secure_connection(request.kind, request.connection_profile)
        return self.catalog.create_secure_datasource(request)

    def update_secure_datasource(
        self,
        datasource_id: str,
        request: SecureDatasourceUpdate,
    ) -> Datasource:
        """Switch one secure datasource onto a new candidate connection profile.

        The order below is the contract, not an implementation detail:

        1. resolve the datasource, so an unknown id is ``datasource_not_found``;
        2. require an existing ``connection_profile_v1`` document, so a legacy datasource is
           rejected rather than migrated;
        3. capture the old profile, to diff its managed references for cleanup;
        4. **test the candidate first** - the whole point of candidate-first: on failure the old
           profile, the old active scan, the old graph and the old secrets are all untouched;
        5. delete the old published graph - if this fails the update aborts and the catalog is
           still on the old profile, which is a fail-closed state rather than a mixed one;
        6. persist the candidate profile;
        7. invalidate the old catalog scan state, so no query can pair the new connection with the
           old scan;
        8. garbage-collect managed secrets the switch made unreferenced.

        ``update`` is not ``scan``: a successful update leaves the datasource in ``created`` and
        the caller must call ``scan_datasource`` to reach READY again on the new revision.
        """
        datasource = self._resolve_datasource(datasource_id)
        previous = self.catalog.get_connection_profile(datasource_id)
        if previous is None:
            raise DatasourceSecureProfileRequiredError(
                f"数据源 {datasource_id} 不是受管安全数据源，无法更新连接配置"
            )

        # Candidate first: nothing below this line may run before the connection is proven.
        self._test_secure_connection(datasource.kind, request.connection_profile)

        try:
            self.graph_store.delete_datasource_graph(datasource_id)
        except Exception as error:
            raise DatasourceUpdateError(
                f"数据源 {datasource_id} 的旧图删除失败，更新已中止：{safe_error(error)}"
            ) from error

        try:
            updated = self.catalog.update_secure_datasource(
                datasource_id, request.connection_profile
            )
            self.catalog.invalidate_datasource_scan(datasource_id)
        except SmartDataError:
            raise
        except Exception as error:
            raise DatasourceUpdateError(safe_error(error)) from error

        self._cleanup_obsolete_secrets(previous, request.connection_profile)
        return updated

    def delete_datasource(self, datasource_id: str) -> None:
        """Delete one datasource, its published graph and any secrets it alone referenced.

        The graph is removed before the catalog row. The reverse order would let a failure leave a
        catalog entry with no datasource behind it while the graph still held a published
        datasource - a ghost that retrieval could still return. If the graph deletion fails, the
        catalog and the credential store are left exactly as they were.

        A legacy (non-secure) datasource can be deleted here, but its raw connection document
        holds no managed references, so no secret cleanup is attempted for it.
        """
        self._resolve_datasource(datasource_id)
        profile = self.catalog.get_connection_profile(datasource_id)

        try:
            self.graph_store.delete_datasource_graph(datasource_id)
        except Exception as error:
            raise DatasourceDeleteError(
                f"数据源 {datasource_id} 的图删除失败，删除已中止：{safe_error(error)}"
            ) from error

        try:
            self.catalog.delete_datasource(datasource_id)
        except SmartDataError:
            raise
        except Exception as error:
            raise DatasourceDeleteError(safe_error(error)) from error

        if profile is not None:
            self._cleanup_unreferenced_secrets(managed_secret_ids(profile))

    def _cleanup_obsolete_secrets(
        self,
        previous: ConnectionProfile,
        candidate: ConnectionProfile,
    ) -> None:
        """Garbage-collect managed secrets the profile switch left unreferenced.

        Only secrets present in the old profile and absent from the new one are candidates: a
        secret the candidate still names is in use by definition, and a secret only the candidate
        names belongs to the caller and is never touched here - a failed update must not destroy
        material the caller created for it.
        """
        obsolete = managed_secret_ids(previous) - managed_secret_ids(candidate)
        self._cleanup_unreferenced_secrets(obsolete)

    def _cleanup_unreferenced_secrets(self, secret_ids: set[str]) -> None:
        """Delete managed secrets no stored profile still references, and keep the rest.

        This runs after the profile switch or datasource deletion has already succeeded, so it is
        garbage collection rather than part of the transaction: a credential-store failure here
        must not roll back a datasource operation that is already durable. It is logged as a
        warning naming the secret id and the error type only - never the secret content - and the
        secret is left for a later cleanup. The reference count is re-read per secret, so the
        shared-secret case (another datasource still names it) retains the secret rather than
        breaking that datasource.
        """
        for secret_id in sorted(secret_ids):
            try:
                if self.catalog.count_managed_secret_references(secret_id):
                    continue
                self._credentials().delete_secret(secret_id)
            except Exception as error:  # noqa: BLE001 - cleanup must not fail a durable mutation
                logger.warning(
                    "managed secret cleanup skipped: secret_id=%s error=%s",
                    secret_id,
                    type(error).__name__,
                )

    def list_datasources(self, workspace_id: str = "default") -> list[Datasource]:
        return self.catalog.list_datasources(workspace_id)

    def inspect_datasource(self, datasource_id: str) -> DatasourceDetail:
        """Read one datasource's public projection together with its current scan facts.

        This is a read-only convenience for interface layers that must show a single datasource.
        It runs no scan, opens no connection and reads no graph; it also never returns the stored
        connection document, a secret reference or a credential.

        ``scan_version`` / ``last_scan_status`` describe the active ``ScanSnapshot`` only. The
        active snapshot is the same revision the Ask pipeline fences execution against, so a
        datasource that was never successfully scanned reports no version instead of a fabricated
        one.
        """
        datasource = self._resolve_datasource(datasource_id)
        snapshot = self.catalog.get_active_snapshot(datasource_id)
        datasets = self.catalog.list_datasets(datasource.workspace_id, datasource_id)
        return DatasourceDetail(
            id=datasource.id,
            name=datasource.name,
            kind=datasource.kind,
            driver=datasource.driver,
            status=datasource.status,
            workspace_id=datasource.workspace_id,
            scan_version=snapshot.version if snapshot is not None else None,
            last_scan_status=snapshot.status.value if snapshot is not None else None,
            dataset_count=len(datasets),
        )

    def list_supported_adapters(self) -> list[dict[str, Any]]:
        """List the database kinds and drivers this installation can connect to.

        This is the read side of the adapter registry. Interface layers must not import the
        registry themselves: registry contents describe what the *application* can open, and
        keeping that behind the facade is what lets a future deployment expose a subset without
        every product surface disagreeing about it.
        """
        return list_registered_adapters()

    def list_tls_capabilities(self) -> list[dict[str, Any]]:
        """Return the safe public TLS controls from the canonical driver matrix."""
        return [
            {
                "driver": spec.driver,
                "custom_ca": spec.custom_ca,
                "mtls": spec.mtls,
                "server_name_override": spec.server_name_override,
                "tls13_control": spec.tls13_control,
            }
            for spec in TLS_DRIVER_MATRIX.values()
        ]

    def get_schema_context(self, workspace_id: str = "default") -> dict[str, Any]:
        """Return the normalized workspace structure used for retrieval and schema display.

        Read-only: it reads datasources, datasets, relations and bounded samples from the catalog
        and derives cross-datasource field candidates. It opens no connection and reads no graph, so
        an interface layer can render a schema overview without touching the catalog directly.
        """
        return build_schema_context(self.catalog, workspace_id)

    def summarize_schema(self, workspace_id: str = "default") -> str:
        """Summarize the workspace schema with the configured model, or deterministically without one.

        The model port is owned by the application service, so the decision of whether a model is
        available is made here rather than re-derived by each interface. A deployment with no model
        configured gets the bounded deterministic fallback instead of an error.
        """
        context = self.get_schema_context(workspace_id)
        if self.model is None:
            return summarize_without_model(context)
        return self.model.summarize_schema(context)

    def search_datasets(self, query: str, workspace_id: str = "default") -> list[DatasetInfo]:
        """Search datasets by name, matching a dataset name or any of its field names.

        The match is a case-insensitive substring over dataset and field names only. Sample values
        and metadata are deliberately not searched, so a search cannot become a side channel that
        surfaces business data to an interface caller.
        """
        lowered = query.lower()
        return [
            dataset
            for dataset in self.catalog.list_datasets(workspace_id)
            if lowered in dataset.name.lower()
            or any(lowered in field.name.lower() for field in dataset.fields)
        ]

    def list_relations(
        self, workspace_id: str = "default", datasource_id: str | None = None
    ) -> list[RelationInfo]:
        """List structural relations discovered inside data sources in one workspace."""
        return self.catalog.list_relations(workspace_id, datasource_id)

    def list_mappings(
        self, workspace_id: str = "default", entity: str | None = None
    ) -> list[MappingInfo]:
        """List physical-to-canonical field mappings recorded in one workspace."""
        return self.catalog.list_mappings(workspace_id, entity)

    def _resolve_datasource(self, datasource_id: str) -> Datasource:
        """Read one datasource, reporting an unknown id as the stable product error.

        The catalog signals a miss with ``KeyError``. Every interface layer should see
        ``datasource_not_found`` instead, and the scan path must resolve the datasource before it
        creates an initialization job for an id that does not exist.
        """
        try:
            return self.catalog.get_datasource(datasource_id)[0]
        except KeyError as error:
            raise DatasourceNotFoundError(f"数据源不存在：{datasource_id}") from error

    def scan_datasource(self, datasource_id: str) -> list[DatasetInfo]:
        # Resolve before scanning: an unknown id must be reported as ``datasource_not_found`` and
        # must not leave an initialization job behind for a datasource that does not exist.
        datasource = self._resolve_datasource(datasource_id)
        job = self.initializer.initialize(datasource_id)
        if job.status != ScanStatus.READY:
            raise DatasourceUnavailableError(job.error_message or "数据库初始化失败")
        return self.catalog.list_datasets(datasource.workspace_id, datasource_id)

    def initialize_datasource(self, datasource_id: str) -> InitializationJob:
        return self.initializer.initialize(datasource_id)

    @property
    def excel_ingestion(self) -> ExcelIngestionService:
        if self._excel_ingestion is None:
            self._excel_ingestion = ExcelIngestionService(self)
        return self._excel_ingestion

    def import_excel(self, request: ExcelImportRequest) -> ExcelImportResult:
        """Import a ``.xlsx`` workbook as a first-class SmartData datasource.

        This is the only entry point an interface layer may use. The ingestion service owns
        deterministic validation, the managed artifact, SQLite materialization and idempotency, and
        then reuses the existing secure datasource + ``DatabaseInitializer`` chain so the workbook
        is scanned, snapshotted and published exactly like any other relational source. An import is
        reported READY only when the artifact, the datasource, the active ScanSnapshot and the Neo4j
        publication are all in place.
        """
        return self.excel_ingestion.import_excel(request)

    def prepare_query(
        self,
        question: str,
        workspace_id: str = "default",
        datasource_id: str | None = None,
        max_rows: int = 200,
    ) -> PreparedQuery:
        return self.query_preparation.prepare(question, workspace_id, datasource_id, max_rows)

    def retrieve_semantics(
        self,
        query: BusinessQuery,
        workspace_id: str = "default",
        requested_datasource_id: str | None = None,
        limit: int = 20,
    ) -> SemanticRetrievalResult:
        """Recall candidate physical and semantic assets from the published graph.

        This replaces reading physical structure from ``CompanyDataProfile``. Grounding, planning
        and query generation stay out of this step.
        """
        retriever = GraphSemanticRetriever(
            self.graph_reader,
            SQLiteSemanticAssetRegistry(self.catalog.path),
            workspace_id,
        )
        return retriever.retrieve(
            query,
            requested_datasource_id=requested_datasource_id,
            limit=limit,
        )

    def ground_semantics(
        self, query: BusinessQuery, retrieval: SemanticRetrievalResult
    ) -> GroundingResult:
        """Bind the query's business expressions to the confirmed retrieval candidates.

        The result is not executable when anything is left unresolved; callers must check
        ``GroundingResult.is_executable`` before planning. ``ask()`` keeps its existing behaviour
        and is not rewired to this path yet.
        """
        return SemanticGrounder().ground(query, retrieval)

    def understand_intent(
        self, question: str, requested_datasource_id: str | None = None
    ) -> IntentUnderstandingResult:
        """Parse only business intent from natural language using rules plus the intent model."""
        if self.intent_understanding is None:
            raise IntentParsingError("自然语言问数需要配置 BusinessQuery intent model")
        return self.intent_understanding.understand(question, requested_datasource_id)

    def build_query_context(
        self,
        grounding: GroundingResult,
        *,
        workspace_id: str | None = None,
        requested_datasource_id: str | None = None,
        time_range: TimeRange | None = None,
        time_spec: TimeSpec | None = None,
    ) -> GroundedQueryContext:
        """Turn a grounded query into the planner-facing context.

        ``time_spec`` is the structured business time axis (the converged ``TimeSpec`` carries
        exactly the bound dimension; the natural-language phrase lives on
        ``BusinessQuery.time_expression``). The builder refuses a context whose time dimension
        is not a grounded date/datetime column. This step is pure: it reads no database, no
        graph, no profile and no model. It fails closed with ``QueryContextBuildError`` when the
        grounding is not executable, so an unresolved question never reaches planning.
        """
        return self.query_context_builder.build(
            grounding,
            workspace_id=workspace_id,
            requested_datasource_id=requested_datasource_id,
            time_range=time_range,
            time_spec=time_spec,
        )

    def plan_grounded_query(self, context: GroundedQueryContext) -> GroundedQueryPlan:
        """Plan and validate a grounded context. Only allowlisted physical assets appear in the plan.

        The planner cannot read the enterprise database, the graph, retrieval or a model, and it
        emits no native query text: compilation to a read-only native query is a later step.
        ``ask()`` is not rewired to this path yet.
        """
        plan = self.grounded_planner.plan(context)
        self.grounded_plan_validator.validate(plan, context)
        return plan

    def generate_grounded_query(self, plan: GroundedQueryPlan) -> NativeQuery:
        """Compile and independently validate a locked datasource's native read."""
        datasource, _ = self.catalog.get_datasource(plan.datasource_id)
        native = self.grounded_native_compiler.compile(plan, datasource)
        self.grounded_native_validator.validate(native, plan, expected=native)
        return native

    def execute_grounded_plan(
        self,
        context: GroundedQueryContext,
        plan: GroundedQueryPlan,
        *,
        workspace_id: str,
        max_rows: int = 200,
    ) -> GroundedExecution:
        """The formal grounded execution boundary.

        Walks the seven steps that prove a native query comes from a grounded
        plan and is run only against the published graph revision:

        1. ``GroundedPlanValidator.validate(plan, context)`` — refuses any
           drift between the plan's locator / scan_version and the context.
        2. ``GroundedSQLCompiler.compile(plan)`` — produces the canonical
           ``expected`` ``NativeQuery`` (the authoritative command / parameters).
        3. ``GroundedNativeQueryValidator.validate(native, plan, expected=expected)``
           — refuses forged native queries that share identifiers with the
           plan but carry a different command / parameters.
        4. Read the current graph structure and pick the published
           ``scan_version`` for the datasource.
        5. ``ExecutionRevisionValidator.validate(native, current)`` — refuses
           stale plans before the executor opens a connection.
        6. ``GroundedQueryExecutor.execute(native, datasource, max_rows)`` —
           parameterized, read-only SQL via the adapter.
        7. Return a ``GroundedExecution`` whose ``result`` carries the typed
           rows and whose ``evidence`` is safe for traces (no parameter
           values, no business inputs).

        The returned ``NativeQuery`` produced in step 2 is *not* an
        authorization artefact: callers must not be able to forge one and
        execute it directly. ``execute_grounded_query(native, ...)`` is kept
        only as the internal primitive the orchestration calls into.
        """
        self.grounded_plan_validator.validate(plan, context)
        datasource, _ = self.catalog.get_datasource(plan.datasource_id)
        if datasource.workspace_id != workspace_id or plan.workspace_id != workspace_id:
            raise QueryPlanningError("Native query datasource is outside the requested workspace")
        expected = self.grounded_native_compiler.compile(plan, datasource)
        self.grounded_native_validator.validate(expected, plan)
        # Re-prove provenance by validating ``expected`` against itself — the
        # comparison anchor is the compiled artefact, so a forged NativeQuery
        # cannot pass by sharing identifiers with the plan alone.
        self.grounded_native_validator.validate(expected, plan, expected=expected)
        native = expected
        structure = self.graph_reader.read_structure(
            GraphStructureRequest(
                workspace_id=workspace_id,
                datasource_id=native.datasource_id,
                max_data_objects=1,
                max_fields=1,
                max_relationships=1,
            )
        )
        matches = [
            item for item in structure.datasources if item.datasource_id == native.datasource_id
        ]
        current = matches[0].scan_version if len(matches) == 1 else None
        self.execution_revision_validator.validate(native, current)
        return self.grounded_executor.execute(native, datasource, max_rows)

    def execute_grounded_query(
        self, native: NativeQuery, *, workspace_id: str, max_rows: int = 200
    ) -> GroundedExecution:
        """Internal primitive: run a pre-compiled native query with revision check.

        This is the executor entry point that ``execute_grounded_plan`` calls
        into after every trust boundary has passed. It is **not** a public
        authorization gate: a caller supplying a hand-built ``NativeQuery`` is
        asserting, on trust, that the artefact comes from a grounded plan. The
        formal entry point that proves the artefact *did* come from a grounded
        plan is ``execute_grounded_plan``.
        """
        structure = self.graph_reader.read_structure(
            GraphStructureRequest(
                workspace_id=workspace_id,
                datasource_id=native.datasource_id,
                max_data_objects=1,
                max_fields=1,
                max_relationships=1,
            )
        )
        matches = [
            item for item in structure.datasources if item.datasource_id == native.datasource_id
        ]
        current = matches[0].scan_version if len(matches) == 1 else None
        self.execution_revision_validator.validate(native, current)
        datasource, _ = self.catalog.get_datasource(native.datasource_id)
        if datasource.workspace_id != workspace_id:
            raise QueryPlanningError("Native query datasource is outside the requested workspace")
        return self.grounded_executor.execute(native, datasource, max_rows)

    def ask(self, request: AskRequest) -> AskResponse:
        """Run Ask by consuming the same orchestration records exposed by ``ask_stream``.

        Existing exception behaviour is retained: the public stream represents a failure as
        ``error`` followed by ``done``, while this synchronous compatibility entry point re-raises
        the original exception only after consuming that terminal pair.
        """
        response: AskResponse | None = None
        failure: Exception | None = None
        for record in self._ask_event_records(request):
            if record.response is not None:
                response = record.response
            if record.exception is not None:
                failure = record.exception
        if failure is not None:
            raise failure
        if response is None:  # pragma: no cover - invariant guarded by orchestration tests
            raise RuntimeError("Ask orchestration ended without a response")
        return response

    def ask_stream(self, request: AskRequest) -> Iterator[AskEvent]:
        """Yield the stable public progress events for the single formal Ask pipeline."""
        for record in self._ask_event_records(request):
            yield record.event

    def _ask_event_records(self, request: AskRequest) -> Iterator[_AskEventRecord]:
        correlation_id = str(uuid4())
        sequence = 0

        def record(
            event_type: AskEventType,
            payload: dict[str, Any],
            *,
            response: AskResponse | None = None,
            exception: Exception | None = None,
        ) -> _AskEventRecord:
            nonlocal sequence
            sequence += 1
            return _AskEventRecord(
                event=AskEvent(
                    event_type=event_type,
                    sequence=sequence,
                    correlation_id=correlation_id,
                    payload=payload,
                ),
                response=response,
                exception=exception,
            )

        yield record(
            AskEventType.ACCEPTED,
            {
                "stage": "accepted",
                "workspace_id": request.workspace_id,
                "datasource_id": request.datasource_id,
            },
        )

        try:
            if request.sql is not None:
                datasource, step = self._prepare_explicit_sql(request)
                yield record(AskEventType.PLAN_READY, self._legacy_plan_payload(step))
                yield record(
                    AskEventType.QUERY_READY,
                    {
                        "stage": "query",
                        "datasource_id": datasource.id,
                        "query_language": "sql",
                        "display_command": "<explicit read-only SQL>",
                    },
                )
                yield record(
                    AskEventType.EXECUTION_STARTED,
                    {
                        "stage": "execution",
                        "datasource_id": datasource.id,
                        "plan_id": step.id,
                    },
                )
                response = self._execute_explicit_sql(request, datasource, step)
            else:
                self._require_ready_scope(request.workspace_id, request.datasource_id)
                if self._is_schema_inventory_question(request.question):
                    response = self._schema_inventory_response(request)
                    if response.status == AskStatus.COMPLETED:
                        yield record(
                            AskEventType.RETRIEVAL_READY,
                            {
                                "stage": "retrieval",
                                "datasource_id": request.datasource_id,
                                "scan_version": response.analysis["scan_version"],
                                "candidate_count": response.result.row_count,
                                "retrieval_path": "published_schema",
                            },
                        )
                    event_type = (
                        AskEventType.CLARIFICATION_REQUIRED
                        if response.status == AskStatus.CLARIFICATION_REQUIRED
                        else AskEventType.RESULT_READY
                    )
                    payload = (
                        self._clarification_payload(response)
                        if event_type == AskEventType.CLARIFICATION_REQUIRED
                        else self._result_payload(response)
                    )
                    yield record(event_type, payload, response=response)
                    yield record(AskEventType.DONE, self._done_payload(response), response=response)
                    return
                intent = self.understand_intent(request.question, request.datasource_id)
                yield record(AskEventType.INTENT_READY, self._intent_payload(intent))
                if intent.needs_clarification:
                    response = self._clarification_response(
                        request.question, intent.business_query, intent.clarifications
                    )
                    yield record(
                        AskEventType.CLARIFICATION_REQUIRED,
                        self._clarification_payload(response),
                        response=response,
                    )
                    yield record(
                        AskEventType.DONE,
                        self._done_payload(response),
                        response=response,
                    )
                    return

                query = intent.business_query
                scope = intent.requested_datasource_id
                retrieval = self.retrieve_semantics(
                    query,
                    workspace_id=request.workspace_id,
                    requested_datasource_id=scope,
                )
                yield record(AskEventType.RETRIEVAL_READY, self._retrieval_payload(retrieval))
                grounding = self.ground_semantics(query, retrieval)
                yield record(AskEventType.GROUNDING_READY, self._grounding_payload(grounding))
                if (
                    grounding.needs_clarification
                    or not grounding.is_executable
                    or not grounding.grounded_query.bindings
                ):
                    clarifications = list(grounding.clarifications)
                    if not clarifications:
                        clarifications = [
                            ClarificationRequest(
                                clarification_id="grounding_unavailable",
                                field="grounding",
                                question=(
                                    "当前已发布数据中没有找到足够的受治理结构来回答这个问题。"
                                    "请检查相应数据源是否已经扫描并发布所需业务字段。"
                                ),
                            )
                        ]
                    response = self._clarification_response(
                        request.question, query, clarifications
                    )
                    yield record(
                        AskEventType.CLARIFICATION_REQUIRED,
                        self._clarification_payload(response),
                        response=response,
                    )
                    yield record(
                        AskEventType.DONE,
                        self._done_payload(response),
                        response=response,
                    )
                    return

                time_range = normalize_time_range(intent.rule_extraction)
                time_spec: TimeSpec | None = None
                if query.time_expression:
                    if time_range is None:
                        response = self._plain_clarification(
                            request.question,
                            query,
                            "无法可靠归一化当前时间范围，请提供明确的日期或受支持的相对时间。",
                        )
                        yield record(
                            AskEventType.CLARIFICATION_REQUIRED,
                            self._clarification_payload(response),
                            response=response,
                        )
                        yield record(
                            AskEventType.DONE,
                            self._done_payload(response),
                            response=response,
                        )
                        return
                    # The axis is the governed time-axis binding produced by grounding. Never
                    # infer it from a convenient date-looking dimension.
                    time_axes = list(
                        dict.fromkeys(
                            binding.business_term
                            for binding in grounding.grounded_query.bindings
                            if binding.time_axis
                        )
                    )
                    if len(time_axes) != 1:
                        response = self._plain_clarification(
                            request.question,
                            query,
                            "请明确唯一的业务时间维度（例如下单日期、支付日期或发货日期）。",
                            time_axes,
                        )
                        yield record(
                            AskEventType.CLARIFICATION_REQUIRED,
                            self._clarification_payload(response),
                            response=response,
                        )
                        yield record(
                            AskEventType.DONE,
                            self._done_payload(response),
                            response=response,
                        )
                        return
                    time_spec = TimeSpec(dimension=time_axes[0])

                context = self.build_query_context(
                    grounding,
                    workspace_id=request.workspace_id,
                    requested_datasource_id=scope,
                    time_range=time_range,
                    time_spec=time_spec,
                )
                plan = self.plan_grounded_query(context)
                yield record(AskEventType.PLAN_READY, self._plan_payload(plan))
                native = self.generate_grounded_query(plan)
                yield record(AskEventType.QUERY_READY, self._native_query_payload(native))
                yield record(
                    AskEventType.EXECUTION_STARTED,
                    {
                        "stage": "execution",
                        "datasource_id": plan.datasource_id,
                        "scan_version": plan.scan_version,
                        "plan_id": plan.plan_id,
                    },
                )
                execution = self.execute_grounded_plan(
                    context,
                    plan,
                    workspace_id=request.workspace_id,
                    max_rows=request.max_rows,
                )
                analysis = self._analyze(execution.result.rows)
                # Model-reported ambiguity notes are advisory evidence and cannot block governed
                # grounding or execution.
                if query.ambiguities:
                    analysis["intent_ambiguities"] = list(query.ambiguities)
                answer = self.answer_composer.compose(
                    question=request.question,
                    result=execution.result,
                    analysis=analysis,
                    deterministic_fallback=self._summarize(
                        request.question,
                        execution.result.row_count,
                        analysis,
                        execution.result.truncated,
                    ),
                )
                analysis["answer_source"] = answer.source
                response = AskResponse(
                    question=request.question,
                    status=AskStatus.COMPLETED,
                    answer=answer.text,
                    business_query=query,
                    plan=plan,
                    result=execution.result,
                    evidence=execution.evidence,
                    analysis=analysis,
                )

            yield record(
                AskEventType.RESULT_READY,
                self._result_payload(response),
                response=response,
            )
            yield record(AskEventType.DONE, self._done_payload(response), response=response)
        except Exception as error:  # noqa: BLE001 - streams must terminate as error then done
            detail = self._ask_error_detail(error)
            failed = AskResponse(
                question=request.question,
                status=AskStatus.FAILED,
                answer=detail.message,
                error=detail,
            )
            yield record(
                AskEventType.ERROR,
                {
                    "stage": "error",
                    "error": detail.model_dump(mode="json"),
                    "response": self._public_response(failed),
                },
                response=failed,
                exception=error,
            )
            yield record(AskEventType.DONE, self._done_payload(failed), response=failed)

    def _ask_explicit_sql(self, request: AskRequest) -> AskResponse:
        """Legacy explicit-native-query compatibility path, separate from natural-language Ask."""
        datasource, step = self._prepare_explicit_sql(request)
        return self._execute_explicit_sql(request, datasource, step)

    def _prepare_explicit_sql(
        self, request: AskRequest
    ) -> tuple[Datasource, QueryPlanStep]:
        datasources = self.catalog.list_datasources(request.workspace_id)
        if request.datasource_id:
            datasources = [item for item in datasources if item.id == request.datasource_id]
            if not datasources:
                raise DatasourceNotFoundError(
                    f"工作区 {request.workspace_id} 中不存在数据源 {request.datasource_id}"
                )
        if not datasources:
            raise DatasourceUnavailableError(f"工作区 {request.workspace_id} 中没有可用数据源")

        ready_datasources = [item for item in datasources if item.status == "ready"]
        if not ready_datasources:
            raise DatasourceNotReadyError("数据源尚未扫描，请先执行 scan_datasource")
        datasource = ready_datasources[0]
        step = QueryPlanStep(
            id="legacy-explicit-sql",
            datasource_id=datasource.id,
            dataset="query",
            purpose=f"显式 SQL：{request.question}",
            query=request.sql or "",
            query_language="sql",
        )
        return datasource, step

    def _execute_explicit_sql(
        self, request: AskRequest, datasource: Datasource, step: QueryPlanStep
    ) -> AskResponse:
        # Opened for the duration of the execution: a TLS-enabled datasource needs its certificate
        # material to survive the adapter call and be cleaned up afterwards.
        with self.connection_provider.open(datasource.id) as connection_info:
            adapter = create_adapter(datasource.id, datasource.kind, connection_info)
            result = adapter.execute(step.query, request.max_rows)
        result.dataset = step.dataset
        result.rows = normalize_rows(result.rows)
        analysis = self._analyze(result.rows)
        return AskResponse(
            question=request.question,
            status=AskStatus.COMPLETED,
            answer=self._summarize(
                request.question, result.row_count, analysis, result.truncated
            ),
            plan=[step],
            result=result,
            analysis=analysis,
            recommended_questions=[
                f"{step.dataset} 的总记录数是多少？",
                f"查看 {step.dataset} 的前 10 条数据",
            ],
        )

    @staticmethod
    def _intent_payload(intent: IntentUnderstandingResult) -> dict[str, Any]:
        query = intent.business_query
        return {
            "stage": "intent",
            "datasource_id": intent.requested_datasource_id,
            "intent": {
                "objective": query.objective.value,
                "entities": list(query.entities),
                "metrics": list(query.metrics),
                "dimensions": list(query.dimensions),
                "has_filters": bool(query.filters),
                "time_expression": query.time_expression,
                "requested_output": [item.value for item in query.requested_output],
                "confidence": query.confidence,
            },
        }

    @staticmethod
    def _retrieval_payload(retrieval: Any) -> dict[str, Any]:
        candidates = list(getattr(retrieval, "candidates", []))
        datasource_ids = sorted(
            {
                candidate.datasource_id
                for candidate in candidates
                if getattr(candidate, "datasource_id", None)
            }
        )
        path = getattr(retrieval, "retrieval_path", None)
        return {
            "stage": "retrieval",
            "datasource_id": getattr(retrieval, "requested_datasource_id", None),
            "datasource_ids": datasource_ids,
            "scan_version": getattr(retrieval, "scan_version", None),
            "candidate_count": len(candidates),
            "retrieval_path": getattr(path, "value", path),
        }

    @staticmethod
    def _grounding_payload(grounding: Any) -> dict[str, Any]:
        grounded = grounding.grounded_query
        bindings = list(getattr(grounded, "bindings", []))
        return {
            "stage": "grounding",
            "datasource_id": getattr(grounded, "requested_datasource_id", None),
            "scan_version": getattr(grounded, "scan_version", None),
            "is_executable": bool(grounding.is_executable),
            "bindings": [
                {
                    "business_term": getattr(binding, "business_term", None),
                    "asset_type": getattr(
                        getattr(binding, "asset_type", None),
                        "value",
                        getattr(binding, "asset_type", None),
                    ),
                    "datasource_id": getattr(binding, "datasource_id", None),
                    "data_object_id": getattr(binding, "data_object_id", None),
                    "field_path": getattr(binding, "field_path", None),
                    "relationship_id": getattr(binding, "relationship_id", None),
                }
                for binding in bindings
            ],
        }

    @staticmethod
    def _plan_payload(plan: GroundedQueryPlan) -> dict[str, Any]:
        return {
            "stage": "plan",
            "plan": {
                "plan_id": plan.plan_id,
                "workspace_id": plan.workspace_id,
                "datasource_id": plan.datasource_id,
                "data_object_ids": list(plan.data_object_ids),
                "scan_version": plan.scan_version,
                "aggregate_count": len(plan.aggregates),
                "selected_field_count": len(plan.selected_fields),
                "filter_count": len(plan.filters),
                "join_count": len(plan.joins),
                "limit": plan.limit,
                "expected_result_type": plan.expected_result_type.value,
            },
        }

    @staticmethod
    def _legacy_plan_payload(step: QueryPlanStep) -> dict[str, Any]:
        return {
            "stage": "plan",
            "plan": {
                "plan_id": step.id,
                "datasource_id": step.datasource_id,
                "dataset": step.dataset,
                "query_language": step.query_language,
                "purpose": "explicit_native_query",
            },
        }

    @staticmethod
    def _native_query_payload(native: NativeQuery) -> dict[str, Any]:
        # ``parameters`` is deliberately absent. ``display_command`` is produced by the compiler
        # specifically for public traces and contains placeholders instead of bound values.
        return {
            "stage": "query",
            "datasource_id": native.datasource_id,
            "scan_version": native.scan_version,
            "plan_id": native.plan_id,
            "query_language": native.query_language.value,
            "display_command": native.display_command,
        }

    @classmethod
    def _clarification_payload(cls, response: AskResponse) -> dict[str, Any]:
        return {
            "stage": "clarification",
            "status": response.status.value,
            "clarification": [
                item.model_dump(mode="json") for item in response.clarification
            ],
            "response": cls._public_response(response),
        }

    @classmethod
    def _result_payload(cls, response: AskResponse) -> dict[str, Any]:
        result = response.result
        return {
            "stage": "result",
            "status": response.status.value,
            "datasource_id": getattr(result, "datasource_id", None)
            or getattr(result, "source", None),
            "row_count": getattr(result, "row_count", 0),
            "truncated": bool(getattr(result, "truncated", False)),
            "evidence": (
                response.evidence.model_dump(mode="json") if response.evidence else None
            ),
            "response": cls._public_response(response),
        }

    @staticmethod
    def _done_payload(response: AskResponse) -> dict[str, Any]:
        return {"stage": "done", "status": response.status.value}

    @staticmethod
    def _public_response(response: AskResponse) -> dict[str, Any]:
        payload = response.model_dump(mode="json")
        sensitive_values: list[str] = []
        business_query = payload.get("business_query")
        if isinstance(business_query, dict):
            for item in business_query.get("filters", []):
                if not isinstance(item, dict) or not is_sensitive_field(
                    str(item.get("subject", ""))
                ):
                    continue
                value = item.get("value")
                if isinstance(value, str):
                    sensitive_values.append(value)
                item["value"] = "<redacted>"
        plan = payload.get("plan")
        if isinstance(plan, list):
            for step in plan:
                if isinstance(step, dict) and "query" in step:
                    step["query"] = "<redacted explicit SQL>"
        elif isinstance(plan, dict):
            for item in plan.get("filters", []):
                if not isinstance(item, dict):
                    continue
                field = item.get("field", {})
                names = (
                    str(field.get(name, ""))
                    for name in ("business_term", "field_path")
                    if isinstance(field, dict)
                )
                if not any(is_sensitive_field(name) for name in names):
                    continue
                value = item.get("value")
                if isinstance(value, str):
                    sensitive_values.append(value)
                item["value"] = "<redacted>"
        return SecretRedactor.from_environment(extra_values=sensitive_values).value(payload)

    @staticmethod
    def _ask_error_detail(error: Exception) -> ErrorDetail:
        redactor = SecretRedactor.from_environment()
        if isinstance(error, SmartDataError):
            return ErrorDetail(code=error.code, message=redactor.text(error.message))
        return ErrorDetail(code="internal_error", message="问数执行失败。")

    def _require_ready_scope(self, workspace_id: str, datasource_id: str | None) -> None:
        datasources = self.catalog.list_datasources(workspace_id)
        if datasource_id:
            matches = [item for item in datasources if item.id == datasource_id]
            if not matches:
                raise DatasourceNotFoundError(
                    f"工作区 {workspace_id} 中不存在数据源 {datasource_id}"
                )
            if matches[0].status != "ready":
                raise DatasourceNotReadyError("指定数据源尚未扫描，请先执行 scan_datasource")
            return
        if not datasources:
            raise DatasourceUnavailableError(f"工作区 {workspace_id} 中没有可用数据源")
        if not any(item.status == "ready" for item in datasources):
            raise DatasourceNotReadyError("工作区中没有已完成扫描的数据源")

    @staticmethod
    def _is_schema_inventory_question(question: str) -> bool:
        """Recognize explicit structure discovery without intercepting business queries."""
        compact = re.sub(r"\s+", "", question).casefold()
        terms = (
            "表结构", "数据结构", "数据库结构", "有哪些表", "有什么表",
            "数据集有哪些", "有哪些数据集", "列出数据表", "列出所有表",
        )
        if any(term in compact for term in terms):
            return True
        if ("数据库" in compact or "数据源" in compact) and any(
            term in compact for term in ("有哪些字段", "哪些字段", "有什么字段")
        ):
            return True
        if re.search(
            r"(?:数据库|数据源|库).{0,8}"
            r"(?:有什么|有哪些|是什么|(?:包含|包括)(?:什么|哪些))"
            r"(?:数据|内容|表)",
            compact,
        ):
            return True
        if re.search(
            r"(?:这个|这张|当前)(?:数据)?表.{0,8}"
            r"(?:有什么|有哪些|是什么|(?:包含|包括)(?:什么|哪些))"
            r"(?:数据|内容|字段)",
            compact,
        ):
            return True
        return bool(
            re.search(
                r"\b(?:schema|(?:list|show|what)\s+(?:tables|columns|fields))\b",
                question.casefold(),
            )
        )

    def _schema_inventory_response(self, request: AskRequest) -> AskResponse:
        """Answer source structure from the published graph, without model or row access."""
        if not request.datasource_id:
            message = "请先选择一个已扫描的数据源，再查看它有哪些表和字段。"
            return AskResponse(
                question=request.question,
                status=AskStatus.CLARIFICATION_REQUIRED,
                answer=message,
                clarification=[AskClarification(question=message)],
            )

        datasource = self._resolve_datasource(request.datasource_id)
        question = request.question.casefold()
        mentioned_driver = next(
            (
                driver
                for driver in ("mysql", "postgresql", "postgres", "sqlite", "mongodb")
                if re.search(rf"(?<![a-z]){driver}(?![a-z])", question)
            ),
            None,
        )
        actual_driver = (datasource.driver or "").casefold()
        if mentioned_driver == "postgres":
            mentioned_driver = "postgresql"
        if mentioned_driver and mentioned_driver not in actual_driver:
            message = (
                f"当前选中的是“{datasource.name}”（{datasource.driver or '未知驱动'}），"
                f"问题提到 {mentioned_driver}。请在“选择数据源”中切换到目标数据源后再问。"
            )
            return AskResponse(
                question=request.question,
                status=AskStatus.CLARIFICATION_REQUIRED,
                answer=message,
                clarification=[AskClarification(question=message)],
            )

        snapshot = self.catalog.get_active_snapshot(datasource.id)
        if snapshot is None:
            raise DatasourceNotReadyError("指定数据源没有可用的扫描版本，请重新扫描")
        structure = self.graph_reader.read_structure(
            GraphStructureRequest(
                workspace_id=request.workspace_id,
                datasource_id=datasource.id,
                max_data_objects=2_000,
                max_fields=20_000,
                max_relationships=2_000,
            )
        )
        graph_sources = [
            item for item in structure.datasources if item.datasource_id == datasource.id
        ]
        if len(graph_sources) != 1 or graph_sources[0].scan_version != snapshot.version:
            raise GraphUnavailableError("扫描目录与 Neo4j 已发布结构不一致，请重新扫描数据源")

        fields_by_object: dict[str, list[str]] = {}
        for field in structure.fields:
            fields_by_object.setdefault(field.object_id, []).append(field.path)
        objects = structure.data_objects
        rows = [
            {
                "数据表": item.qualified_name or item.name,
                "类型": item.object_kind or "table",
                "字段": "、".join(fields_by_object.get(item.node_id, [])) or "—",
            }
            for item in objects[: request.max_rows]
        ]
        truncated = structure.truncated or len(objects) > len(rows)
        answer = (
            f"从“{datasource.name}”已发布的扫描结构中读到 {len(objects)} 个数据对象、"
            f"{len(structure.fields)} 个字段。下表列出表和字段；这些是结构信息，不是表内记录。"
            if objects else f"“{datasource.name}”的已发布扫描结构中没有数据对象。"
        )
        if re.search(r"(?:这个|这张|当前)(?:数据)?表", request.question) and len(objects) > 1:
            answer = (
                f"当前选中的是数据源“{datasource.name}”，其中有 {len(objects)} 张表。"
                "问题没有指定某一张表，先展示这个数据源的结构。若要查看某张表的记录，"
                f"请写出表名。\n\n{answer}"
            )
        summary_source = "scan"
        summarize_structure = getattr(self.model, "summarize_schema_inventory", None)
        if objects and callable(summarize_structure):
            context = {
                "datasource": datasource.name,
                "driver": datasource.driver,
                "scan_version": snapshot.version,
                "visible_object_count": len(objects),
                "visible_field_count": len(structure.fields),
                "truncated": truncated,
                "objects": [
                    {
                        "name": item.qualified_name or item.name,
                        "kind": item.object_kind,
                        "fields": fields_by_object.get(item.node_id, [])[:20],
                    }
                    for item in objects[:40]
                ],
            }
            try:
                summary = summarize_structure(request.question, context).strip()
                if summary:
                    answer = f"{summary}\n\n扫描依据：{answer}"
                    summary_source = "model"
                else:
                    summary_source = "model_unavailable"
            except ModelInvocationError as error:
                logger.warning("schema inventory model summary unavailable: %s", type(error).__name__)
                summary_source = "model_unavailable"
        elif self.model is not None and objects:
            summary_source = "model_unavailable"
        if truncated:
            answer += " 当前结果受读取上限限制，可能未列全。"
        return AskResponse(
            question=request.question,
            status=AskStatus.COMPLETED,
            answer=answer,
            result=NormalizedResult(
                source=datasource.id,
                dataset="published_schema",
                columns=["数据表", "类型", "字段"],
                rows=rows,
                row_count=len(rows),
                truncated=truncated,
            ),
            analysis={
                "result_kind": "schema_inventory",
                "scan_version": snapshot.version,
                "summary_source": summary_source,
            },
        )

    @staticmethod
    def _clarification_response(
        question: str,
        query: BusinessQuery,
        clarifications: list[ClarificationRequest],
    ) -> AskResponse:
        public = [
            AskClarification(
                question=item.question,
                options=[option.label for option in item.options],
            )
            for item in clarifications
        ]
        return AskResponse(
            question=question,
            status=AskStatus.CLARIFICATION_REQUIRED,
            answer=public[0].question if public else "需要进一步澄清。",
            business_query=query,
            clarification=public,
        )

    @staticmethod
    def _plain_clarification(
        question: str,
        query: BusinessQuery,
        message: str,
        options: list[str] | None = None,
    ) -> AskResponse:
        return AskResponse(
            question=question,
            status=AskStatus.CLARIFICATION_REQUIRED,
            answer=message,
            business_query=query,
            clarification=[AskClarification(question=message, options=options or [])],
        )

    def list_suggestions(self, workspace_id: str = "default") -> list[GovernanceSuggestion]:
        return self.catalog.list_suggestions(workspace_id)

    @staticmethod
    def _analyze(rows: list[dict[str, Any]]) -> dict[str, Any]:
        if not rows:
            return {"row_count": 0, "numeric_summary": {}}
        summary: dict[str, Any] = {}
        for column in rows[0]:
            values = [row[column] for row in rows if isinstance(row.get(column), (int, float))]
            if values:
                summary[column] = {
                    "min": normalize_number(min(values)),
                    "max": normalize_number(max(values)),
                    "average": normalize_number(mean(values)),
                    "sum": normalize_number(sum(values)),
                }
        return {"row_count": len(rows), "numeric_summary": summary}

    @staticmethod
    def _summarize(question: str, row_count: int, analysis: dict[str, Any], truncated: bool) -> str:
        suffix = "，结果已按上限截断" if truncated else ""
        metrics = analysis.get("numeric_summary", {})
        if row_count == 1 and metrics:
            first = next(iter(metrics.items()))
            return f"已完成“{question}”。{first[0]} 的结果为 {first[1]['sum']}{suffix}。"
        return f"已完成“{question}”，返回 {row_count} 行数据{suffix}。"
