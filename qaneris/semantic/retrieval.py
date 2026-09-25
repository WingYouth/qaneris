from __future__ import annotations

import re
from dataclasses import dataclass, replace
from typing import Protocol

from qaneris.contracts.profile import (
    CompanyDataProfile,
    DataObjectProfile,
    FieldProfile,
    SemanticMetadata,
)
from qaneris.contracts.query import AggregateFunction
from qaneris.contracts.semantic import (
    BusinessQuery,
    SemanticAssetType,
    SemanticCandidate,
)
from qaneris.graph.ports import GraphReader
from qaneris.graph.reading import (
    GraphBindingReference,
    GraphDataObject,
    GraphStructure,
    GraphStructureRequest,
)
from qaneris.semantic.assets import (
    SemanticAsset,
    SemanticAssetKind,
    SemanticAssetStatus,
    SemanticAssetStore,
)
from qaneris.semantic.models import SemanticRetrievalPath, SemanticRetrievalResult
from qaneris.semantic.normalization import normalize_term as _normalize

_METRIC_ROLES = {"metric", "measure", "指标", "度量"}
_DIMENSION_ROLES = {"dimension", "attribute", "维度", "属性"}
_LEXICAL_STOP_WORDS = {"no"}
_STRUCTURAL_RECALL_SCORE = 0.05
_TRUSTED_STATUSES = {SemanticAssetStatus.APPROVED, SemanticAssetStatus.PUBLISHED}


class SemanticRetriever(Protocol):
    """Compatibility contract for the profile-backed retriever."""

    def retrieve(
        self,
        profile: CompanyDataProfile,
        query: BusinessQuery,
        requested_datasource_id: str | None = None,
        limit: int = 20,
    ) -> SemanticRetrievalResult: ...


@dataclass(frozen=True)
class _Asset:
    """Uniform retrieval asset, sourced either from the graph or from the asset registry."""

    asset_type: SemanticAssetType
    asset_id: str
    name: str
    datasource_id: str | None = None
    data_object_id: str | None = None
    field_id: str | None = None
    field_path: str | None = None
    relationship_id: str | None = None
    from_data_object_id: str | None = None
    to_data_object_id: str | None = None
    from_field_path: str | None = None
    to_field_path: str | None = None
    aliases: tuple[str, ...] = ()
    descriptions: tuple[str, ...] = ()
    evidence: tuple[str, ...] = ()
    confidence: float = 1.0
    trusted: bool = True
    migration_lookup: bool = False
    #: Recalled from the structural scope even without a lexical hit. A confirmed relationship is a
    #: physical fact of the published scope, and a business question that needs a join never repeats
    #: the foreign key in words.
    structural_recall: bool = False
    #: True when the governed asset declares itself the business time axis of its data source.
    time_axis: bool = False
    #: Graph-declared column metadata and the governed aggregation semantics: planning and the
    #: future query generator consume them, no extra round trip to the graph is required.
    data_type: str | None = None
    native_type: str | None = None
    default_aggregation: AggregateFunction | None = None
    namespace: str | None = None
    qualified_name: str | None = None
    object_kind: str | None = None
    #: Native data object name from ``GraphDataObject.name``. Populated for DATA_OBJECT and FIELD
    #: candidates by looking up the owning object in the same structure read; never the
    #: business term the user said.
    data_object_name: str | None = None


class GraphSemanticRetriever:
    """Primary semantic retrieval path.

    Physical truth comes from the published enterprise data graph; business semantics come from the
    governed semantic asset registry. An asset in the registry never produces a candidate unless the
    graph confirms its physical binding, and the graph's physical identity
    (``graph_data_object_id`` / ``graph_field_id`` / ``field_path``) is what the candidate carries.

    Retrieval stays at candidate level. Choosing between competing candidates, and turning them into
    a physical binding, belongs to grounding, not here.

    Phase 1 limitation: term-level recall reads a bounded structural context (the first ``max_*``
    objects, fields and relationships of the scope, in a deterministic order) and scores it in
    memory. Lexical matching is therefore not pushed into the graph, and there is no vector or
    full-text retrieval yet. Exact physical binding resolution is not subject to those bounds.
    Fails closed with :class:`GraphUnavailableError` when no graph backend is configured, so an
    unreachable graph never looks like "no candidates found".
    """

    def __init__(
        self,
        reader: GraphReader,
        registry: SemanticAssetStore,
        workspace_id: str = "default",
        *,
        include_untrusted: bool = False,
    ):
        self.reader = reader
        self.registry = registry
        self.workspace_id = workspace_id
        self.include_untrusted = include_untrusted
        self.retrieval_path = (
            SemanticRetrievalPath.EXPLORATORY
            if include_untrusted
            else SemanticRetrievalPath.TRUSTED
        )

    def retrieve(
        self,
        query: BusinessQuery,
        *,
        requested_datasource_id: str | None = None,
        limit: int = 20,
    ) -> SemanticRetrievalResult:
        if not 1 <= limit <= 100:
            raise ValueError("semantic retrieval limit must be between 1 and 100")
        query_phrases = _query_phrases(query)
        materials = self._materials(requested_datasource_id)
        structure = self.reader.read_structure(
            GraphStructureRequest(
                workspace_id=self.workspace_id,
                datasource_id=requested_datasource_id,
                references=_binding_references(materials),
            )
        )
        if requested_datasource_id and not structure.has_datasource(requested_datasource_id):
            return self._result(
                query,
                [],
                requested_datasource_id,
                [f"指定数据源不存在于工作区企业数据图：{requested_datasource_id}"],
                structure=structure,
            )

        assets, warnings = self._assets(structure, materials, query, query_phrases)
        candidates = [
            candidate
            for asset in assets
            if (candidate := _candidate_for(asset, query, query_phrases)) is not None
        ]
        candidates.extend(_structural_candidates(assets, candidates, query, query_phrases))
        candidates.extend(_time_axis_candidates(assets, candidates, query))
        candidates.sort(
            key=lambda item: (-item.score, item.asset_type.value, item.name, item.asset_id)
        )
        _mark_ambiguities(candidates)
        if not candidates:
            warnings.append("未在当前企业数据图与语义资产注册表中检索到匹配资产")
        return self._result(
            query, candidates[:limit], requested_datasource_id, warnings, structure=structure
        )

    def _result(
        self,
        query: BusinessQuery,
        candidates: list[SemanticCandidate],
        requested_datasource_id: str | None,
        warnings: list[str],
        structure: GraphStructure | None = None,
    ) -> SemanticRetrievalResult:
        scan_version = _structure_scan_version(structure, requested_datasource_id)
        return SemanticRetrievalResult(
            business_query=query,
            candidates=candidates,
            workspace_id=self.workspace_id,
            requested_datasource_id=requested_datasource_id,
            warnings=warnings,
            retrieval_path=self.retrieval_path,
            scan_version=scan_version,
        )

    def _materials(self, requested_datasource_id: str | None) -> list[SemanticAsset]:
        statuses = None if self.include_untrusted else set(_TRUSTED_STATUSES)
        return [
            asset
            for asset in self.registry.list(self.workspace_id, statuses)
            if asset.status is not SemanticAssetStatus.DEPRECATED
            and (
                asset.kind is SemanticAssetKind.ALIAS
                or requested_datasource_id is None
                or asset.datasource_id == requested_datasource_id
            )
        ]

    def _assets(
        self,
        structure: GraphStructure,
        materials: list[SemanticAsset],
        query: BusinessQuery,
        query_phrases: list[str],
    ) -> tuple[list[_Asset], list[str]]:
        warnings: list[str] = []
        aliases: dict[str, list[str]] = {}
        semantic: list[_Asset] = []
        for material in materials:
            if material.kind is SemanticAssetKind.ALIAS:
                if material.target_asset_id:
                    aliases.setdefault(material.target_asset_id, []).extend(
                        [material.name, *material.aliases]
                    )
                continue
            asset = _material_asset(structure, material, query, query_phrases, warnings)
            if asset is not None:
                semantic.append(asset)
        resolved = [
            replace(asset, aliases=tuple(dict.fromkeys([*asset.aliases, *aliases[asset.asset_id]])))
            if aliases.get(asset.asset_id)
            else asset
            for asset in semantic
        ]
        return [*_physical_assets(structure), *resolved], warnings


class MetadataLexicalSemanticRetriever:
    """Compatibility path: bounded metadata and alias retrieval over ``CompanyDataProfile``.

    The profile is a migration-era structure. New retrieval and grounding work must use the
    graph-backed retriever above; this class stays only so existing profile consumers keep working.
    """

    def retrieve(
        self,
        profile: CompanyDataProfile,
        query: BusinessQuery,
        requested_datasource_id: str | None = None,
        limit: int = 20,
    ) -> SemanticRetrievalResult:
        if not 1 <= limit <= 100:
            raise ValueError("semantic retrieval limit must be between 1 and 100")
        assets = self._assets(profile, requested_datasource_id)
        query_phrases = _query_phrases(query)
        candidates = [
            candidate
            for asset in assets
            if (candidate := _candidate_for(asset, query, query_phrases)) is not None
        ]
        candidates.sort(
            key=lambda item: (-item.score, item.asset_type.value, item.name, item.asset_id)
        )
        _mark_ambiguities(candidates)
        warnings: list[str] = []
        if requested_datasource_id and not any(
            source.datasource_id == requested_datasource_id for source in profile.data_sources
        ):
            warnings.append(f"指定数据源不存在于工作区画像：{requested_datasource_id}")
        elif not candidates:
            warnings.append("未在当前语义画像中检索到匹配资产")
        return SemanticRetrievalResult(
            business_query=query,
            candidates=candidates[:limit],
            workspace_id=profile.workspace_id,
            requested_datasource_id=requested_datasource_id,
            warnings=warnings,
        )

    def _assets(
        self, profile: CompanyDataProfile, requested_datasource_id: str | None
    ) -> list[_Asset]:
        assets: list[_Asset] = []
        for datasource in profile.data_sources:
            if requested_datasource_id and datasource.datasource_id != requested_datasource_id:
                continue
            assets.append(
                _Asset(
                    asset_type=SemanticAssetType.DATASOURCE,
                    asset_id=datasource.datasource_id,
                    name=datasource.name,
                    datasource_id=datasource.datasource_id,
                    descriptions=(datasource.driver, datasource.kind.value),
                    evidence=(f"数据源画像：{datasource.datasource_id}",),
                )
            )
            assets.extend(
                self._semantic_assets(
                    owner_id=datasource.datasource_id,
                    owner_name=datasource.name,
                    metadata=datasource.semantic_metadata,
                    datasource_id=datasource.datasource_id,
                )
            )
            for namespace in datasource.namespaces:
                for data_object in namespace.data_objects:
                    assets.extend(self._object_assets(data_object))
        allowed_source_ids = {
            item.datasource_id for item in profile.data_sources
            if not requested_datasource_id or item.datasource_id == requested_datasource_id
        }
        for relationship in profile.relationships:
            if relationship.from_datasource_id not in allowed_source_ids:
                continue
            if relationship.to_datasource_id not in allowed_source_ids:
                continue
            assets.append(
                _Asset(
                    asset_type=SemanticAssetType.RELATIONSHIP,
                    asset_id=relationship.id,
                    name=relationship.relationship_type,
                    datasource_id=relationship.from_datasource_id,
                    relationship_id=relationship.id,
                    from_data_object_id=relationship.from_object_id,
                    to_data_object_id=relationship.to_object_id,
                    from_field_path=relationship.from_field_path,
                    to_field_path=relationship.to_field_path,
                    descriptions=(
                        relationship.from_object_id,
                        relationship.from_field_path or "",
                        relationship.to_object_id,
                        relationship.to_field_path or "",
                    ),
                    evidence=(
                        f"关系画像：{relationship.from_object_id}→{relationship.to_object_id}",
                        *relationship.evidence,
                    ),
                )
            )
        return assets

    def _object_assets(self, data_object: DataObjectProfile) -> list[_Asset]:
        assets = [
            _Asset(
                asset_type=SemanticAssetType.DATA_OBJECT,
                asset_id=data_object.id,
                name=data_object.name,
                datasource_id=data_object.datasource_id,
                data_object_id=data_object.id,
                descriptions=(data_object.comment or "", data_object.object_kind.value),
                evidence=(f"数据对象画像：{data_object.id}",),
            )
        ]
        assets.extend(
            self._semantic_assets(
                owner_id=data_object.id,
                owner_name=data_object.name,
                metadata=data_object.semantic_metadata,
                datasource_id=data_object.datasource_id,
                data_object_id=data_object.id,
            )
        )
        for field_profile in data_object.fields:
            assets.extend(self._field_assets(data_object, field_profile))
        return assets

    def _field_assets(
        self, data_object: DataObjectProfile, field_profile: FieldProfile
    ) -> list[_Asset]:
        field_id = f"{data_object.id}:{field_profile.path}"
        assets = [
            _Asset(
                asset_type=SemanticAssetType.FIELD,
                asset_id=field_id,
                name=field_profile.name,
                datasource_id=data_object.datasource_id,
                data_object_id=data_object.id,
                field_path=field_profile.path,
                descriptions=(field_profile.path, field_profile.comment or ""),
                evidence=(f"字段画像：{data_object.id}.{field_profile.path}",),
            )
        ]
        assets.extend(
            self._semantic_assets(
                owner_id=field_id,
                owner_name=field_profile.name,
                metadata=field_profile.semantic_metadata,
                datasource_id=data_object.datasource_id,
                data_object_id=data_object.id,
                field_path=field_profile.path,
            )
        )
        return assets

    @staticmethod
    def _semantic_assets(
        owner_id: str,
        owner_name: str,
        metadata: list[SemanticMetadata],
        datasource_id: str,
        data_object_id: str | None = None,
        field_path: str | None = None,
    ) -> list[_Asset]:
        assets: list[_Asset] = []
        for index, item in enumerate(metadata):
            roles = frozenset(role.casefold() for role in item.roles)
            if roles & _METRIC_ROLES:
                asset_type = SemanticAssetType.METRIC
            elif roles & _DIMENSION_ROLES:
                asset_type = SemanticAssetType.DIMENSION
            else:
                asset_type = SemanticAssetType.BUSINESS_TERM
            name = item.business_name or item.business_entity or next(
                iter(item.aliases), owner_name
            )
            assets.append(
                _Asset(
                    asset_type=asset_type,
                    asset_id=f"{owner_id}:semantic:{index}:{asset_type.value}",
                    name=name,
                    datasource_id=datasource_id,
                    data_object_id=data_object_id,
                    field_path=field_path,
                    aliases=tuple(item.aliases),
                    descriptions=tuple(
                        value
                        for value in (item.description, item.business_entity, *item.roles)
                        if value
                    ),
                    evidence=(
                        f"语义元数据：{owner_id}",
                        f"来源：{item.origin.value}",
                    ),
                    confidence=item.confidence,
                )
            )
        return assets


def _binding_references(materials: list[SemanticAsset]) -> list[GraphBindingReference]:
    """Physical bindings the registry claims, so the graph can confirm or reject each one.

    A binding is sent with the graph's stable physical identity when the asset has one. Otherwise the
    readable lookup conditions are sent as a migration-era fallback.
    """
    references: list[GraphBindingReference] = []
    for material in materials:
        if material.kind is SemanticAssetKind.ALIAS:
            continue
        if not material.has_physical_binding and not material.has_graph_identity:
            continue
        references.append(
            GraphBindingReference(
                datasource_id=material.datasource_id,
                graph_data_object_id=material.graph_data_object_id,
                namespace=material.namespace,
                qualified_name=material.qualified_name,
                data_object_name=material.data_object_name,
                graph_field_id=material.graph_field_id,
                field_path=material.field_path,
            )
        )
    return references


def _structure_scan_version(
    structure: GraphStructure | None, requested_datasource_id: str | None
) -> int | None:
    """Resolve the scan_version reported by the read.

    A scoped read (``requested_datasource_id`` set) returns the version of that datasource; an
    unscoped read with a single datasource returns its version; otherwise ``None`` because the
    read crossed datasources and no single revision is meaningful.
    """
    if structure is None or not structure.datasources:
        return None
    if requested_datasource_id:
        matches = [
            item.scan_version
            for item in structure.datasources
            if item.datasource_id == requested_datasource_id and item.scan_version is not None
        ]
        if len(matches) == 1:
            return matches[0]
        return None
    versions = {
        item.scan_version
        for item in structure.datasources
        if item.scan_version is not None
    }
    if len(structure.datasources) == 1 and len(versions) == 1:
        return next(iter(versions))
    return None


def _physical_assets(structure: GraphStructure) -> list[_Asset]:
    """Physical assets of the published graph, with no inferred relationship of any kind.

    Object candidates carry the native locator (namespace / qualified_name / object_kind) so the
    generator can target ``public.orders`` and ``archive.orders`` correctly; field candidates
    carry the column data type **and the owning data object's native locator** so a raw FIELD
    that grounding binds via a filter slot still knows its parent table's
    ``namespace.qualified_name`` (``public.orders``) and native ``name`` (``orders``) — without
    re-reading the graph at context time. No lookup table is consulted for these values: they
    come from the published graph row we already read.
    """
    assets: list[_Asset] = []
    objects_by_id: dict[str, GraphDataObject] = {
        item.node_id: item for item in structure.data_objects
    }
    for source in structure.datasources:
        assets.append(
            _Asset(
                asset_type=SemanticAssetType.DATASOURCE,
                asset_id=source.datasource_id,
                name=source.name,
                datasource_id=source.datasource_id,
                descriptions=tuple(
                    value
                    for value in (source.kind, source.driver, source.database_name)
                    if value
                ),
                evidence=(f"企业数据图：数据源 {source.name}",),
            )
        )
    for data_object in structure.data_objects:
        assets.append(
            _Asset(
                asset_type=SemanticAssetType.DATA_OBJECT,
                asset_id=data_object.node_id,
                name=data_object.name,
                datasource_id=data_object.datasource_id,
                data_object_id=data_object.node_id,
                namespace=data_object.namespace,
                qualified_name=data_object.qualified_name,
                object_kind=data_object.object_kind,
                data_object_name=data_object.name,
                descriptions=tuple(
                    value
                    for value in (
                        data_object.object_kind,
                        data_object.comment,
                        data_object.namespace,
                        data_object.qualified_name,
                    )
                    if value
                ),
                evidence=(f"企业数据图：数据对象 {data_object.name}",),
            )
        )
    for field in structure.fields:
        owner = objects_by_id.get(field.object_id)
        assets.append(
            _Asset(
                asset_type=SemanticAssetType.FIELD,
                asset_id=field.node_id,
                name=field.name,
                datasource_id=field.datasource_id,
                data_object_id=field.object_id,
                field_id=field.node_id,
                field_path=field.path,
                data_type=field.data_type,
                native_type=field.native_type,
                namespace=owner.namespace if owner else None,
                qualified_name=owner.qualified_name if owner else None,
                object_kind=owner.object_kind if owner else None,
                data_object_name=owner.name if owner else None,
                descriptions=tuple(
                    value for value in (field.path, field.data_type, field.comment) if value
                ),
                evidence=(f"企业数据图：字段 {field.object_name}.{field.path}",),
            )
        )
    for relationship in structure.relationships:
        assets.append(
            _Asset(
                asset_type=SemanticAssetType.RELATIONSHIP,
                asset_id=relationship.relationship_id,
                name=relationship.relationship_type,
                datasource_id=relationship.from_datasource_id,
                relationship_id=relationship.relationship_id,
                from_data_object_id=relationship.from_object_id,
                to_data_object_id=relationship.to_object_id,
                from_field_path=relationship.from_field_path,
                to_field_path=relationship.to_field_path,
                descriptions=tuple(
                    value
                    for value in (
                        relationship.from_object_name,
                        relationship.from_field_path,
                        relationship.to_object_name,
                        relationship.to_field_path,
                    )
                    if value
                ),
                evidence=(
                    f"企业数据图：关系 {relationship.from_object_name}→{relationship.to_object_name}",
                ),
                structural_recall=True,
            )
        )
    return assets


def _asset_type(kind: SemanticAssetKind) -> SemanticAssetType:
    return {
        SemanticAssetKind.METRIC: SemanticAssetType.METRIC,
        SemanticAssetKind.DIMENSION: SemanticAssetType.DIMENSION,
    }.get(kind, SemanticAssetType.BUSINESS_TERM)


def _material_asset(
    structure: GraphStructure,
    material: SemanticAsset,
    query: BusinessQuery,
    query_phrases: list[str],
    warnings: list[str],
) -> _Asset | None:
    """Bind one governed asset to its confirmed physical asset, or skip it.

    The asset's graph physical identity is authoritative when present. Readable names are only used
    to resolve assets that predate the identity fields, and such candidates are flagged as migration
    lookups so nothing downstream treats a name as the physical identity.
    """
    if not material.has_physical_binding:
        # Pure business vocabulary without a physical claim; it must not become a physical asset.
        return None
    reference = _material_reference(material)
    label = _lookup_label(material)
    data_object, reason = _resolve_object(structure, material, reference)
    if data_object is None:
        _warn_broken_binding(material, label, reason, query, query_phrases, warnings)
        return None
    field = None
    if material.field_path or material.graph_field_id:
        fields = structure.match_field(reference, data_object)
        if not fields:
            _warn_broken_binding(
                material,
                f"{label}.{material.field_path or material.graph_field_id}",
                "不存在于企业数据图",
                query,
                query_phrases,
                warnings,
            )
            return None
        if len(fields) > 1:
            _warn_broken_binding(
                material,
                f"{label}.{material.field_path or material.graph_field_id}",
                "在企业数据图中不唯一",
                query,
                query_phrases,
                warnings,
            )
            return None
        field = fields[0]
    if material.kind in {SemanticAssetKind.METRIC, SemanticAssetKind.DIMENSION} and field is None:
        warnings.append(f"语义资产缺少物理字段绑定，已跳过：{material.asset_id}")
        return None
    binding = f"{data_object.name}.{field.path}" if field else data_object.name
    identity = data_object.node_id if field is None else f"{data_object.node_id}/{field.node_id}"
    return _Asset(
        asset_type=_asset_type(material.kind),
        asset_id=material.asset_id,
        name=material.name,
        datasource_id=data_object.datasource_id,
        data_object_id=data_object.node_id,
        field_id=field.node_id if field else None,
        field_path=field.path if field else None,
        aliases=tuple(material.aliases),
        descriptions=tuple(
            value for value in (material.description, material.kind.value) if value
        ),
        evidence=(
            f"语义资产：{material.asset_id}",
            f"企业数据图确认物理绑定：{binding}",
            f"图物理身份：{identity}{'' if material.has_graph_identity else '（按名称迁移解析）'}",
            f"来源：{material.source.value}",
        ),
        confidence=material.confidence,
        trusted=material.trusted,
        migration_lookup=not material.has_graph_identity,
        data_type=field.data_type if field else None,
        native_type=field.native_type if field else None,
        default_aggregation=material.default_aggregation,
        time_axis=material.time_axis,
        namespace=data_object.namespace,
        qualified_name=data_object.qualified_name,
        object_kind=data_object.object_kind,
        data_object_name=data_object.name,
    )


def _resolve_object(
    structure: GraphStructure, material: SemanticAsset, reference: GraphBindingReference
) -> tuple[GraphDataObject | None, str]:
    """Resolve the asset's data object, and say why when it cannot be resolved."""
    if not structure.match_datasource(datasource_id=material.datasource_id):
        return None, "的数据源不在当前读取范围"
    objects = structure.match_object(reference, material.datasource_id)
    if not objects:
        return None, "不存在于企业数据图"
    if len(objects) > 1:
        return None, "在企业数据图中不唯一（需要限定名称或命名空间）"
    if objects[0].datasource_id != material.datasource_id:
        return None, "的物理身份与登记的数据源不一致"
    return objects[0], ""


def _material_reference(material: SemanticAsset) -> GraphBindingReference:
    return GraphBindingReference(
        datasource_id=material.datasource_id,
        graph_data_object_id=material.graph_data_object_id,
        namespace=material.namespace,
        qualified_name=material.qualified_name,
        data_object_name=material.data_object_name,
        graph_field_id=material.graph_field_id,
        field_path=material.field_path,
    )


def _lookup_label(material: SemanticAsset) -> str:
    return material.qualified_name or material.data_object_name or material.graph_data_object_id or "?"


def _warn_broken_binding(
    material: SemanticAsset,
    binding: str,
    reason: str,
    query: BusinessQuery,
    query_phrases: list[str],
    warnings: list[str],
) -> None:
    """Report an unusable binding only when the current question would have used that asset."""
    provisional = _Asset(
        asset_type=_asset_type(material.kind),
        asset_id=material.asset_id,
        name=material.name,
        datasource_id=material.datasource_id,
        field_path=material.field_path,
        aliases=tuple(material.aliases),
        descriptions=tuple(
            value for value in (material.description, material.kind.value) if value
        ),
        confidence=material.confidence,
        trusted=material.trusted,
    )
    if _candidate_for(provisional, query, query_phrases) is None:
        return
    warnings.append(f"语义资产物理绑定{reason}，已跳过：{material.asset_id} → {binding}")


def _query_phrases(query: BusinessQuery) -> list[str]:
    phrases = [query.question, *query.entities, *query.metrics, *query.dimensions]
    phrases.extend(item.subject for item in query.filters)
    phrases.extend(str(item.value) for item in query.filters)
    if query.time_expression:
        phrases.append(query.time_expression)
    return [item for item in phrases if item]


def _candidate_for(
    asset: _Asset,
    query: BusinessQuery,
    query_phrases: list[str],
    *,
    structural: bool = False,
) -> SemanticCandidate | None:
    query_terms = _terms(" ".join(query_phrases))
    name_terms = _terms(asset.name)
    alias_terms = _terms(" ".join(asset.aliases))
    description_terms = _terms(" ".join(asset.descriptions))
    name_hits = query_terms & name_terms
    alias_hits = query_terms & alias_terms
    description_hits = query_terms & description_terms
    exact_name = any(_normalize(item) == _normalize(asset.name) for item in query_phrases)
    exact_aliases = {
        alias
        for alias in asset.aliases
        if any(_normalize(item) == _normalize(alias) for item in query_phrases)
    }
    score = 0.0
    reasons: list[str] = []
    if exact_name:
        score += 0.65
        reasons.append(f"业务表达精确匹配名称：{asset.name}")
    if exact_aliases:
        score += 0.75
        reasons.append(f"业务表达精确匹配别名：{', '.join(sorted(exact_aliases))}")
    if name_hits:
        score += min(0.35, 0.12 * len(name_hits))
        reasons.append(f"名称文本匹配：{', '.join(sorted(name_hits))}")
    if alias_hits:
        score += min(0.4, 0.14 * len(alias_hits))
        reasons.append(f"别名文本匹配：{', '.join(sorted(alias_hits))}")
    if description_hits:
        score += min(0.2, 0.05 * len(description_hits))
        reasons.append(f"元数据描述匹配：{', '.join(sorted(description_hits))}")
    if score > 0 and asset.asset_type == SemanticAssetType.METRIC and query.metrics:
        score += 0.1
        reasons.append("候选角色与业务指标槽位一致")
    if score > 0 and asset.asset_type == SemanticAssetType.DIMENSION and query.dimensions:
        score += 0.1
        reasons.append("候选角色与业务维度槽位一致")
    score *= asset.confidence
    if score <= 0:
        if not structural:
            return None
        # Structural recall: a confirmed fact of the scoped structure, offered at a baseline score
        # instead of depending on word overlap.
        score = _STRUCTURAL_RECALL_SCORE
        reasons.append("企业数据图已确认的结构事实（不依赖词面匹配）")
    matched_terms = sorted(name_hits | alias_hits | description_hits)
    return SemanticCandidate(
        asset_type=asset.asset_type,
        asset_id=asset.asset_id,
        name=asset.name,
        score=min(round(score, 6), 1.0),
        datasource_id=asset.datasource_id,
        data_object_id=asset.data_object_id,
        field_id=asset.field_id,
        field_path=asset.field_path,
        relationship_id=asset.relationship_id,
        from_data_object_id=asset.from_data_object_id,
        to_data_object_id=asset.to_data_object_id,
        from_field_path=asset.from_field_path,
        to_field_path=asset.to_field_path,
        data_type=asset.data_type,
        native_type=asset.native_type,
        default_aggregation=asset.default_aggregation,
        time_axis=asset.time_axis,
        namespace=asset.namespace,
        qualified_name=asset.qualified_name,
        object_kind=asset.object_kind,
        data_object_name=asset.data_object_name,
        reason=reasons[0],
        reasons=reasons,
        evidence=list(asset.evidence),
        matched_terms=matched_terms,
        matched_phrases=_matched_phrases(asset, query_phrases),
        labels=list(dict.fromkeys(value for value in (asset.name, *asset.aliases) if value)),
        trusted=asset.trusted,
        migration_lookup=asset.migration_lookup,
    )


def _time_axis_candidates(
    assets: list[_Asset],
    matched: list[SemanticCandidate],
    query: BusinessQuery,
) -> list[SemanticCandidate]:
    """Governed time axes offered for a question that carries a time expression.

    The intent model is told to express business meaning only, so a question such as "近30天…"
    fills ``time_expression`` and generally does *not* name the date dimension. Waiting for a
    lexical hit would therefore drop the axis entirely. The axis is recalled structurally — the
    same way a confirmed relationship is — because it is a governed fact of the scope, not a
    phrasing. It is offered even when nothing else matched: a time expression on its own is a real
    business constraint, and grounding still decides whether it can be bound.
    """
    if not query.time_expression:
        return []
    already = {candidate.asset_id for candidate in matched}
    return [
        candidate
        for asset in assets
        if asset.time_axis
        and asset.asset_id not in already
        and (candidate := _candidate_for(asset, query, [], structural=True)) is not None
    ]


def _structural_candidates(
    assets: list[_Asset],
    matched: list[SemanticCandidate],
    query: BusinessQuery,
    query_phrases: list[str],
) -> list[SemanticCandidate]:
    """Confirmed relationships offered for joining, only when the read already matched something.

    A relationship exists to connect objects the question already referred to. Offering one on a read
    that matched nothing would turn "no match" into a false positive, so relationships are added to
    the candidate list only alongside other matches, and they are never inferred.
    """
    if not matched:
        return []
    already = {candidate.asset_id for candidate in matched}
    return [
        candidate
        for asset in assets
        if asset.structural_recall
        and asset.asset_id not in already
        and (candidate := _candidate_for(asset, query, query_phrases, structural=True)) is not None
    ]


def _matched_phrases(asset: _Asset, query_phrases: list[str]) -> list[str]:
    """Query expressions that address this asset by its name or one of its aliases.

    Grounding needs to know which business expression a candidate came from, so that a slot is bound
    from its own expression instead of from whichever candidate happens to score highest. Only exact
    or containment relations count: sharing a 2-gram (金额 in 口岸金额 and 实收金额) is a recall
    signal, not evidence that the user meant this asset, so it is not recorded as an address.
    """
    readable = [
        item
        for item in [_normalize(asset.name), *(_normalize(alias) for alias in asset.aliases)]
        if item
    ]
    addressed = [
        phrase
        for phrase in query_phrases
        if (normalized := _normalize(phrase))
        and any(
            normalized == item or (len(item) >= 2 and item in normalized) for item in readable
        )
    ]
    return sorted(set(addressed))


def _mark_ambiguities(candidates: list[SemanticCandidate]) -> None:
    semantic_types = {
        SemanticAssetType.METRIC,
        SemanticAssetType.DIMENSION,
        SemanticAssetType.BUSINESS_TERM,
    }
    by_term: dict[tuple[SemanticAssetType, str], list[SemanticCandidate]] = {}
    for candidate in candidates:
        if candidate.asset_type not in semantic_types:
            continue
        for term in candidate.matched_terms:
            by_term.setdefault((candidate.asset_type, term), []).append(candidate)
    for (_, term), matches in by_term.items():
        unique = {item.asset_id for item in matches}
        if len(unique) < 2:
            continue
        for candidate in matches:
            candidate.ambiguous = True
            candidate.ambiguity = f"术语 {term} 匹配多个 {candidate.asset_type.value} 候选"


def _terms(value: str) -> set[str]:
    lowered = value.casefold()
    words = set(re.findall(r"[a-z0-9]+|[\u4e00-\u9fff]+", lowered))
    chinese_sequences = re.findall(r"[\u4e00-\u9fff]+", lowered)
    for sequence in chinese_sequences:
        words.update(sequence[index : index + 2] for index in range(len(sequence) - 1))
    return {item for item in words if item and item not in _LEXICAL_STOP_WORDS}
