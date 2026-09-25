"""Confirm cross-source keys against published structure and active scan revisions."""

from uuid import uuid4

from qaneris.conversation.models import now
from qaneris.federation.models import JoinMapping, JoinMappingStatus
from qaneris.federation.repository import SQLiteFederationRepository
from qaneris.graph.reading import GraphStructureRequest


class JoinMappingGovernance:
    def __init__(self, repository: SQLiteFederationRepository, service):
        self.repository = repository
        self.service = service

    def create(self, **fields) -> JoinMapping:
        fields.pop("status", None)
        fields.pop("confirmed_by", None)
        fields.pop("confirmed_at", None)
        fields.pop("left_scan_version", None)
        fields.pop("right_scan_version", None)
        mapping = JoinMapping(mapping_id=str(uuid4()), status=JoinMappingStatus.CANDIDATE, **fields)
        if mapping.left_datasource_id == mapping.right_datasource_id:
            raise ValueError("join mapping requires distinct datasources")
        return self.repository.add_mapping(mapping)

    def _version_and_field(self, mapping: JoinMapping, side: str) -> int:
        datasource_id = getattr(mapping, f"{side}_datasource_id")
        objects_id = getattr(mapping, f"{side}_data_object_id")
        path = getattr(mapping, f"{side}_field_path")
        source = next(
            (item for item in self.service.list_datasources(mapping.workspace_id)
             if item.id == datasource_id and item.status == "ready"),
            None,
        )
        if source is None:
            raise ValueError("join mapping datasource is not ready in workspace")
        snapshot = self.service.catalog.get_active_snapshot(datasource_id)
        if snapshot is None:
            raise ValueError("join mapping datasource has no active scan")
        structure = self.service.graph_reader.read_structure(GraphStructureRequest(
            workspace_id=mapping.workspace_id,
            datasource_id=datasource_id,
            max_data_objects=2000,
            max_fields=20000,
        ))
        if not any(item.datasource_id == datasource_id and item.scan_version == snapshot.version
                   for item in structure.datasources):
            raise ValueError("join mapping scan is not published")
        if not any(item.node_id == objects_id and item.datasource_id == datasource_id
                   for item in structure.data_objects):
            raise ValueError("join mapping data object does not exist")
        if not any(item.object_id == objects_id and item.datasource_id == datasource_id
                   and item.path == path for item in structure.fields):
            raise ValueError("join mapping field does not exist")
        return snapshot.version

    def confirm(self, mapping_id: str, confirmed_by: str) -> JoinMapping:
        mapping = self.repository.mapping(mapping_id)
        if mapping.status not in {JoinMappingStatus.CANDIDATE, JoinMappingStatus.STALE}:
            raise ValueError("join mapping is not confirmable")
        if not confirmed_by.strip():
            raise ValueError("confirmed_by audit label is required")
        left = self._version_and_field(mapping, "left")
        right = self._version_and_field(mapping, "right")
        updated = mapping.model_copy(update={
            "left_scan_version": left,
            "right_scan_version": right,
            "status": JoinMappingStatus.CONFIRMED,
            "confirmed_by": confirmed_by.strip(),
            "confirmed_at": now(),
        })
        return self.repository.save_mapping(updated, mapping.status)

    def reject(self, mapping_id: str) -> JoinMapping:
        mapping = self.repository.mapping(mapping_id)
        return self.repository.save_mapping(
            mapping.model_copy(update={"status": JoinMappingStatus.REJECTED}), mapping.status
        )

    def executable(self, mapping_id: str, workspace_id: str, sources: list[str]) -> JoinMapping:
        try:
            mapping = self.repository.mapping(mapping_id)
        except KeyError as error:
            raise FederationFailure("unconfirmed_mapping", blocked=True) from error
        if mapping.workspace_id != workspace_id or mapping.status != JoinMappingStatus.CONFIRMED:
            raise FederationFailure("unconfirmed_mapping", blocked=True)
        if {mapping.left_datasource_id, mapping.right_datasource_id} != set(sources):
            raise FederationFailure("join_mapping_source_mismatch", blocked=True)
        for side in ("left", "right"):
            datasource_id = getattr(mapping, f"{side}_datasource_id")
            snapshot = self.service.catalog.get_active_snapshot(datasource_id)
            if snapshot is None or snapshot.version != getattr(mapping, f"{side}_scan_version"):
                self.repository.save_mapping(
                    mapping.model_copy(update={"status": JoinMappingStatus.STALE}),
                    JoinMappingStatus.CONFIRMED,
                )
                raise FederationFailure("stale_join_mapping", blocked=True)
        return mapping


class FederationFailure(Exception):
    def __init__(self, code: str, *, blocked: bool = False, retryable: bool = False):
        super().__init__(code)
        self.code = code
        self.blocked = blocked
        self.retryable = retryable
