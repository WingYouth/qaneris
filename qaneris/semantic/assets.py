from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Protocol

from pydantic import BaseModel, ConfigDict, Field, computed_field, model_validator

from qaneris.contracts.profile import CompanyDataProfile, MetadataOrigin, SemanticMetadata
from qaneris.contracts.query import AggregateFunction
from qaneris.graph.ports import GraphReader
from qaneris.graph.reading import GraphBindingReference, GraphStructureRequest


class SemanticAssetKind(StrEnum):
    METRIC = "metric"
    DIMENSION = "dimension"
    BUSINESS_TERM = "business_term"
    ALIAS = "alias"


class SemanticAssetSource(StrEnum):
    DATABASE_METADATA = "database_metadata"
    ENTERPRISE_DEFINITION = "enterprise_definition"
    ADMINISTRATOR = "administrator"
    MODEL_SUGGESTION = "model_suggestion"


class SemanticAssetStatus(StrEnum):
    SUGGESTED = "suggested"
    PENDING = "pending"
    APPROVED = "approved"
    PUBLISHED = "published"
    DEPRECATED = "deprecated"


_TRUSTED_STATUSES = {SemanticAssetStatus.APPROVED, SemanticAssetStatus.PUBLISHED}
_UNCONFIRMED_SOURCES = {
    SemanticAssetSource.DATABASE_METADATA,
    SemanticAssetSource.MODEL_SUGGESTION,
}


def _validate_trusted_governance(
    *,
    asset_id: str,
    source: SemanticAssetSource,
    status: SemanticAssetStatus,
    confirmed_by: str | None,
) -> None:
    """A database-derived or model-suggested asset cannot be trusted without human confirmation.

    Shared by the seed validator, the asset validator and the registry write boundary so the
    invariant holds no matter how the asset was built.
    """
    if source in _UNCONFIRMED_SOURCES and status in _TRUSTED_STATUSES and not confirmed_by:
        raise ValueError(
            f"database/model assets require confirmed_by before becoming trusted: {asset_id}"
        )


class SemanticAssetSeed(BaseModel):
    """A governed asset as authored or migrated.

    Physical binding may be expressed with human-readable lookup conditions only. The graph's stable
    physical identity is resolved and stored when the seed is loaded.
    """

    model_config = ConfigDict(extra="forbid")

    asset_id: str = Field(min_length=1)
    workspace_id: str = Field(default="default", min_length=1)
    kind: SemanticAssetKind
    name: str = Field(min_length=1)
    aliases: list[str] = Field(default_factory=list)
    description: str | None = None
    source: SemanticAssetSource
    source_ref: str = Field(min_length=1)
    status: SemanticAssetStatus
    confidence: float = Field(default=1.0, ge=0.0, le=1.0)
    evidence: list[str] = Field(default_factory=list)
    confirmed_by: str | None = None
    datasource_id: str | None = None
    datasource_name: str | None = None
    namespace: str | None = None
    data_object_id: str | None = None
    data_object_name: str | None = None
    qualified_name: str | None = None
    field_path: str | None = None
    target_asset_id: str | None = None
    #: Governed aggregation semantics for the metric. ``None`` means the binding does not declare
    #: an aggregation rule; the planner fails closed on metric bindings with no default and no
    #: explicit derivation instead of guessing ``SUM``.
    default_aggregation: AggregateFunction | None = None
    #: Designates a DIMENSION as the business time axis of its data source. An administrator states
    #: this explicitly; nothing downstream infers a time column from its name or data type.
    time_axis: bool = False

    @model_validator(mode="after")
    def validate_governance(self) -> SemanticAssetSeed:
        if self.kind == SemanticAssetKind.ALIAS:
            if not self.target_asset_id:
                raise ValueError("alias asset requires target_asset_id")
            inherited = (
                self.datasource_id,
                self.datasource_name,
                self.namespace,
                self.data_object_id,
                self.data_object_name,
                self.qualified_name,
                self.field_path,
            )
            if any(inherited):
                raise ValueError("alias asset inherits the target physical binding")
        elif self.target_asset_id:
            raise ValueError("target_asset_id is only valid for alias assets")
        if (
            self.kind is SemanticAssetKind.METRIC
            and self.default_aggregation is AggregateFunction.COUNT
            and not self.field_path
        ):
            # COUNT is the only aggregate that does not need a physical column; declaring COUNT on
            # a metric that has no field is allowed.
            pass
        _validate_trusted_governance(
            asset_id=self.asset_id,
            source=self.source,
            status=self.status,
            confirmed_by=self.confirmed_by,
        )
        return self


class SemanticAsset(BaseModel):
    """A governed semantic asset with its confirmed physical identity.

    ``graph_data_object_id`` / ``graph_field_id`` are the authoritative physical identity and come
    from the published enterprise data graph. ``datasource_name`` / ``namespace`` /
    ``qualified_name`` / ``data_object_name`` are display and migration metadata: they must never be
    the only physical identity of an asset. ``data_object_id`` is the migration-era
    ``DataObjectProfile`` id and is kept only for compatibility with profile projection.
    """

    model_config = ConfigDict(extra="forbid")

    asset_id: str
    workspace_id: str
    kind: SemanticAssetKind
    name: str
    aliases: list[str] = Field(default_factory=list)
    description: str | None = None
    source: SemanticAssetSource
    source_ref: str
    status: SemanticAssetStatus
    confidence: float = Field(ge=0.0, le=1.0)
    evidence: list[str] = Field(default_factory=list)
    confirmed_by: str | None = None
    datasource_id: str | None = None
    datasource_name: str | None = None
    graph_data_object_id: str | None = None
    graph_field_id: str | None = None
    namespace: str | None = None
    qualified_name: str | None = None
    data_object_id: str | None = None
    data_object_name: str | None = None
    field_path: str | None = None
    target_asset_id: str | None = None
    #: Governed aggregation semantics for the metric. ``None`` for non-metric assets; ``None`` for
    #: a metric binding is what triggers the planner's fail-closed rule.
    default_aggregation: AggregateFunction | None = None
    #: True when this DIMENSION is the governed business time axis of its data source.
    time_axis: bool = False
    updated_at: datetime

    @model_validator(mode="after")
    def validate_governance(self) -> SemanticAsset:
        _validate_trusted_governance(
            asset_id=self.asset_id,
            source=self.source,
            status=self.status,
            confirmed_by=self.confirmed_by,
        )
        return self

    @computed_field
    @property
    def trusted(self) -> bool:
        return self.status in _TRUSTED_STATUSES

    @property
    def has_graph_identity(self) -> bool:
        """True when the asset carries the graph's stable physical identity."""
        return bool(self.datasource_id and self.graph_data_object_id)

    @property
    def has_physical_binding(self) -> bool:
        """True when the asset claims any physical binding, including a migration-era lookup."""
        return bool(self.datasource_id and (self.graph_data_object_id or self.data_object_name))

    @property
    def physical_identity(self) -> tuple[str, str, str | None] | None:
        """``(datasource_id, graph_data_object_id, field_path)``, the stable physical identity."""
        if not self.has_graph_identity:
            return None
        return (self.datasource_id, self.graph_data_object_id, self.field_path)


class SemanticAssetStore(Protocol):
    """Read access to governed semantic assets for one workspace."""

    def get(self, workspace_id: str, asset_id: str) -> SemanticAsset | None: ...

    def list(
        self,
        workspace_id: str = "default",
        statuses: set[SemanticAssetStatus] | None = None,
    ) -> list[SemanticAsset]: ...


_SCHEMA = """
CREATE TABLE IF NOT EXISTS semantic_asset (
    asset_id TEXT NOT NULL,
    workspace_id TEXT NOT NULL,
    kind TEXT NOT NULL,
    name TEXT NOT NULL,
    status TEXT NOT NULL,
    asset_json TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (workspace_id, asset_id)
);
CREATE INDEX IF NOT EXISTS semantic_asset_workspace_status
    ON semantic_asset(workspace_id, status);
"""

_LEGACY_TABLE = "semantic_asset_legacy"


class SQLiteSemanticAssetRegistry:
    """Minimal governed semantic asset registry stored beside the Catalog.

    Asset identity is workspace-scoped: the physical unique key is ``(workspace_id, asset_id)``.
    Two workspaces may therefore own an asset with the same ``asset_id`` without overwriting each
    other, and reads are always bound to one workspace.
    """

    def __init__(self, catalog_path: str | Path):
        self.path = str(catalog_path)
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path)
        connection.row_factory = sqlite3.Row
        return connection

    def _initialize(self) -> None:
        with self._connect() as connection:
            self._migrate(connection)
            connection.executescript(_SCHEMA)

    @staticmethod
    def _migrate(connection: sqlite3.Connection) -> None:
        """Move a legacy ``asset_id``-keyed table to the workspace-scoped key.

        Existing databases are keyed globally by ``asset_id``. Migration keeps the table name and
        copies every row; a global key cannot contain two rows that the workspace-scoped key would
        consider distinct, so nothing is lost or silently merged.
        """
        primary_key = _primary_key_columns(connection, "semantic_asset")
        if not primary_key or primary_key == ["workspace_id", "asset_id"]:
            return
        connection.executescript(
            f"""
            DROP TABLE IF EXISTS {_LEGACY_TABLE};
            ALTER TABLE semantic_asset RENAME TO {_LEGACY_TABLE};
            DROP INDEX IF EXISTS semantic_asset_workspace_status;
            """
        )
        connection.executescript(_SCHEMA)
        connection.execute(
            f"""
            INSERT OR IGNORE INTO semantic_asset(
                asset_id, workspace_id, kind, name, status, asset_json, updated_at
            )
            SELECT asset_id, workspace_id, kind, name, status, asset_json, updated_at
            FROM {_LEGACY_TABLE}
            """
        )
        connection.execute(f"DROP TABLE {_LEGACY_TABLE}")

    def save(self, asset: SemanticAsset) -> None:
        # The registry is the single write boundary: re-check the invariant so no caller can reach
        # persisted trusted state by bypassing model validation (``model_copy`` does not validate).
        _validate_trusted_governance(
            asset_id=asset.asset_id,
            source=asset.source,
            status=asset.status,
            confirmed_by=asset.confirmed_by,
        )
        payload = asset.model_dump_json(exclude={"trusted"})
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO semantic_asset(
                    asset_id, workspace_id, kind, name, status, asset_json, updated_at
                ) VALUES(?,?,?,?,?,?,?)
                ON CONFLICT(workspace_id, asset_id) DO UPDATE SET
                    kind=excluded.kind,
                    name=excluded.name,
                    status=excluded.status,
                    asset_json=excluded.asset_json,
                    updated_at=excluded.updated_at
                """,
                (
                    asset.asset_id,
                    asset.workspace_id,
                    asset.kind.value,
                    asset.name,
                    asset.status.value,
                    payload,
                    asset.updated_at.isoformat(),
                ),
            )

    def get(self, workspace_id: str, asset_id: str) -> SemanticAsset | None:
        """Read one asset inside its workspace; asset ids are never looked up across workspaces."""
        with self._connect() as connection:
            row = connection.execute(
                "SELECT asset_json FROM semantic_asset WHERE workspace_id=? AND asset_id=?",
                (workspace_id, asset_id),
            ).fetchone()
        return SemanticAsset.model_validate_json(row["asset_json"]) if row else None

    def list(
        self,
        workspace_id: str = "default",
        statuses: set[SemanticAssetStatus] | None = None,
    ) -> list[SemanticAsset]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT asset_json FROM semantic_asset WHERE workspace_id=? ORDER BY asset_id",
                (workspace_id,),
            ).fetchall()
        assets = [SemanticAsset.model_validate_json(row["asset_json"]) for row in rows]
        return [asset for asset in assets if statuses is None or asset.status in statuses]

    def project_profile(
        self,
        profile: CompanyDataProfile,
        *,
        include_untrusted: bool = False,
    ) -> CompanyDataProfile:
        projected = profile.model_copy(deep=True)
        assets = self.list(profile.workspace_id)
        selected = [
            asset
            for asset in assets
            if asset.status != SemanticAssetStatus.DEPRECATED
            and (include_untrusted or asset.trusted)
        ]
        aliases: dict[str, list[str]] = {}
        for asset in selected:
            if asset.kind == SemanticAssetKind.ALIAS and asset.target_asset_id:
                aliases.setdefault(asset.target_asset_id, []).extend([asset.name, *asset.aliases])
        for asset in selected:
            if asset.kind == SemanticAssetKind.ALIAS:
                continue
            metadata = SemanticMetadata(
                business_name=asset.name,
                description=asset.description,
                roles=[asset.kind.value],
                aliases=list(dict.fromkeys([*asset.aliases, *aliases.get(asset.asset_id, [])])),
                origin=_metadata_origin(asset.source),
                confidence=asset.confidence,
            )
            _attach_metadata(projected, asset, metadata)
        return projected


class SemanticAssetBootstrap:
    """Load governed semantic assets, resolving their physical identity against the published graph.

    Assets are authored with human-readable lookup conditions. When a ``GraphReader`` is supplied, the
    loader confirms each binding against the enterprise data graph and persists the graph's stable
    physical identity (``datasource_id`` / ``graph_data_object_id`` / ``graph_field_id`` / ``field_path``).
    Without a reader the loader keeps the migration-era behaviour and stores names only.
    """

    def __init__(self, registry: SQLiteSemanticAssetRegistry, graph_reader: GraphReader | None = None):
        self.registry = registry
        self.graph_reader = graph_reader

    def load_file(
        self, profile: CompanyDataProfile, path: str | Path
    ) -> list[SemanticAsset]:
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
        raw_assets = payload.get("assets", payload) if isinstance(payload, dict) else payload
        if not isinstance(raw_assets, list):
            raise TypeError("semantic asset seed must be a list or contain an assets list")
        return self.load(profile, [SemanticAssetSeed.model_validate(item) for item in raw_assets])

    def load(
        self, profile: CompanyDataProfile, seeds: list[SemanticAssetSeed]
    ) -> list[SemanticAsset]:
        resolved = [self._resolve(profile, seed) for seed in seeds]
        # Alias targets resolve inside the loaded workspace only.
        known_ids = {asset.asset_id for asset in self.registry.list(profile.workspace_id)} | {
            asset.asset_id for asset in resolved
        }
        for asset in resolved:
            if asset.target_asset_id and asset.target_asset_id not in known_ids:
                raise ValueError(f"semantic alias target does not exist: {asset.target_asset_id}")
        for asset in resolved:
            self.registry.save(asset)
        return resolved

    def _resolve(self, profile: CompanyDataProfile, seed: SemanticAssetSeed) -> SemanticAsset:
        if seed.workspace_id != profile.workspace_id:
            raise ValueError(f"semantic asset workspace does not match profile: {seed.asset_id}")
        datasource_id = seed.datasource_id
        datasource_name = seed.datasource_name
        data_object_id = seed.data_object_id
        data_object_name = seed.data_object_name
        namespace = seed.namespace
        qualified_name = seed.qualified_name
        graph_data_object_id: str | None = None
        graph_field_id: str | None = None
        if seed.kind != SemanticAssetKind.ALIAS:
            datasource = _find_datasource(profile, seed.datasource_id, seed.datasource_name)
            datasource_id = datasource.datasource_id if datasource else None
            datasource_name = datasource.name if datasource else datasource_name
            data_object = _find_data_object(
                datasource, seed.data_object_id, seed.data_object_name, seed.namespace
            )
            data_object_id = data_object.id if data_object else None
            data_object_name = data_object.name if data_object else data_object_name
            namespace = data_object.namespace if data_object else namespace
            if seed.field_path:
                if data_object is None:
                    raise ValueError(f"field binding requires a data object: {seed.asset_id}")
                if seed.field_path not in {field.path for field in data_object.fields}:
                    raise ValueError(
                        f"semantic asset field does not exist: {data_object.name}.{seed.field_path}"
                    )
            if seed.kind in {SemanticAssetKind.METRIC, SemanticAssetKind.DIMENSION} and (
                not datasource_id or not data_object_id or not seed.field_path
            ):
                raise ValueError(
                    f"metric/dimension requires a physical field binding: {seed.asset_id}"
                )
            graph_data_object_id, graph_field_id = self._graph_identity(seed, datasource_id)
        return SemanticAsset(
            asset_id=seed.asset_id,
            workspace_id=seed.workspace_id,
            kind=seed.kind,
            name=seed.name,
            aliases=seed.aliases,
            description=seed.description,
            source=seed.source,
            source_ref=seed.source_ref,
            status=seed.status,
            confidence=seed.confidence,
            evidence=seed.evidence,
            confirmed_by=seed.confirmed_by,
            datasource_id=datasource_id,
            datasource_name=datasource_name,
            graph_data_object_id=graph_data_object_id,
            graph_field_id=graph_field_id,
            namespace=namespace,
            qualified_name=qualified_name,
            data_object_id=data_object_id,
            data_object_name=data_object_name,
            field_path=seed.field_path,
            target_asset_id=seed.target_asset_id,
            default_aggregation=seed.default_aggregation,
            time_axis=seed.time_axis,
            updated_at=datetime.now(UTC),
        )

    def _graph_identity(
        self, seed: SemanticAssetSeed, datasource_id: str | None
    ) -> tuple[str | None, str | None]:
        """Confirm the seed's lookup against the published graph and return its physical identity."""
        if self.graph_reader is None:
            return None, None
        if not (seed.data_object_name or seed.qualified_name):
            # No physical claim: unbound business vocabulary keeps no graph identity.
            return None, None
        reference = GraphBindingReference(
            datasource_id=datasource_id or seed.datasource_id,
            datasource_name=seed.datasource_name,
            namespace=seed.namespace,
            qualified_name=seed.qualified_name,
            data_object_name=seed.data_object_name,
            field_path=seed.field_path,
        )
        structure = self.graph_reader.read_structure(
            GraphStructureRequest(
                workspace_id=seed.workspace_id,
                datasource_id=datasource_id,
                references=[reference],
            )
        )
        objects = structure.match_object(reference, datasource_id)
        if not objects:
            raise ValueError(
                "semantic asset binding does not exist in the enterprise data graph: "
                f"{seed.asset_id} → {_lookup_label(seed)}"
            )
        if len(objects) > 1:
            raise ValueError(
                "semantic asset binding is not unique in the enterprise data graph: "
                f"{seed.asset_id} → {_lookup_label(seed)}"
            )
        graph_data_object_id = objects[0].node_id
        if not seed.field_path:
            return graph_data_object_id, None
        fields = structure.match_field(reference, objects[0])
        if not fields:
            raise ValueError(
                "semantic asset field does not exist in the enterprise data graph: "
                f"{seed.asset_id} → {_lookup_label(seed)}"
            )
        if len(fields) > 1:
            raise ValueError(
                "semantic asset field is not unique in the enterprise data graph: "
                f"{seed.asset_id} → {_lookup_label(seed)}"
            )
        return graph_data_object_id, fields[0].node_id


def _lookup_label(seed: SemanticAssetSeed) -> str:
    parts = [seed.qualified_name or seed.data_object_name or "?", seed.field_path or ""]
    if seed.namespace:
        parts.insert(0, seed.namespace)
    return ".".join(item for item in parts if item)


def _primary_key_columns(connection: sqlite3.Connection, table: str) -> list[str]:
    """Primary key columns of one table, in key order. Empty when the table does not exist."""
    rows = connection.execute(f"PRAGMA table_info({table})").fetchall()
    return [
        row["name"]
        for row in sorted(rows, key=lambda item: item["pk"])
        if row["pk"]
    ]


def _find_datasource(profile, datasource_id: str | None, datasource_name: str | None):
    if not datasource_id and not datasource_name:
        return None
    matches = [
        item
        for item in profile.data_sources
        if (not datasource_id or item.datasource_id == datasource_id)
        and (not datasource_name or item.name == datasource_name)
    ]
    if len(matches) != 1:
        raise ValueError(f"semantic asset datasource binding is not unique: {datasource_id or datasource_name}")
    return matches[0]


def _find_data_object(datasource, data_object_id: str | None, data_object_name: str | None, namespace: str | None = None):
    if not data_object_id and not data_object_name:
        return None
    if datasource is None:
        raise ValueError("data object binding requires a datasource")
    objects = [item for namespace_profile in datasource.namespaces for item in namespace_profile.data_objects]
    matches = [
        item
        for item in objects
        if (not data_object_id or item.id == data_object_id)
        and (not data_object_name or item.name == data_object_name)
        and (not namespace or item.namespace == namespace)
    ]
    if len(matches) != 1:
        label = ".".join(
            item for item in (namespace, data_object_id or data_object_name) if item
        )
        raise ValueError(f"semantic asset object binding is not unique: {label}")
    return matches[0]


def _attach_metadata(
    profile: CompanyDataProfile, asset: SemanticAsset, metadata: SemanticMetadata
) -> None:
    for datasource in profile.data_sources:
        if datasource.datasource_id != asset.datasource_id:
            continue
        if not asset.data_object_id:
            datasource.semantic_metadata.append(metadata)
            return
        for namespace in datasource.namespaces:
            for data_object in namespace.data_objects:
                if data_object.id != asset.data_object_id:
                    continue
                if not asset.field_path:
                    data_object.semantic_metadata.append(metadata)
                    return
                for field in data_object.fields:
                    if field.path == asset.field_path:
                        field.semantic_metadata.append(metadata)
                        return
    raise ValueError(f"semantic asset physical binding is not present in profile: {asset.asset_id}")


def _metadata_origin(source: SemanticAssetSource) -> MetadataOrigin:
    return {
        SemanticAssetSource.DATABASE_METADATA: MetadataOrigin.DATABASE,
        SemanticAssetSource.ENTERPRISE_DEFINITION: MetadataOrigin.MANUAL,
        SemanticAssetSource.ADMINISTRATOR: MetadataOrigin.MANUAL,
        SemanticAssetSource.MODEL_SUGGESTION: MetadataOrigin.MODEL,
    }[source]
