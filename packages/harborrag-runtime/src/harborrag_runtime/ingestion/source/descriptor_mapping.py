"""Map connector descriptors into durable source registrations and releases."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any

from harborrag_adapters.connectors.descriptors import ConnectorDocumentDescriptor
from harborrag_core.chunking import RelationType
from harborrag_core.domain.source import SourceRecord
from harborrag_core.ingestion import (
    AdmissionSnapshot,
    BindingKind,
    ChangeFingerprintBuilder,
    DiscoveredSourceItem,
    DocumentIdentityBuilder,
    SourceBinding,
    SourceIdentity,
    SourceRelationDescriptor,
)
from harborrag_runtime.serialization import to_json_value

from ..document.models import DocumentReleaseRequest
from .models import PlannedDocumentRelease, SourceIngestionRequest


@dataclass(frozen=True, slots=True)
class PendingSourceRelease:
    item: DiscoveredSourceItem
    release: PlannedDocumentRelease


class SourceDescriptorMapper:
    """Apply stable identity and admission rules to connector descriptors."""

    def __init__(self) -> None:
        self._identities = DocumentIdentityBuilder()
        self._fingerprints = ChangeFingerprintBuilder()

    def releases(
        self,
        request: SourceIngestionRequest,
        descriptor: ConnectorDocumentDescriptor,
    ) -> tuple[PendingSourceRelease, ...]:
        sources = (
            (descriptor.source, descriptor.admission),
            *((bound, self._bound_admission(bound)) for bound in descriptor.bound_records),
        )
        pending: list[PendingSourceRelease] = []
        for source, admission in sources:
            identity = self._source_identity(request, source)
            document_id = self._identities.document_id(
                tenant_id=identity.tenant_id,
                connector_type=identity.connector_type,
                connection_id=identity.connection_id,
                source_item_id=identity.source_item_id,
            )
            pending.append(
                PendingSourceRelease(
                    item=DiscoveredSourceItem(
                        source_identity=identity,
                        document_id=document_id,
                        source_version=admission.source_version,
                        admission_change_key=(
                            self._fingerprints.admission_change_key(snapshot=admission)
                        ),
                        descriptor=self._source_payload(source),
                    ),
                    release=PlannedDocumentRelease(
                        request=DocumentReleaseRequest(
                            tenant_id=request.tenant_id,
                            connector_name=request.connector_name,
                            source=source,
                            source_identity=identity,
                            admission=admission,
                            processing=request.processing,
                            configuration_fingerprint=request.configuration_fingerprint,
                            force_reprocess=request.force_reprocess,
                        ),
                        document_id=document_id,
                    ),
                )
            )
        return tuple(pending)

    @staticmethod
    def _source_identity(
        request: SourceIngestionRequest,
        source: SourceRecord,
    ) -> SourceIdentity:
        raw_binding = str(source.metadata.get("binding_kind") or "ROOT")
        parent = source.metadata.get("parent_source_item_id")
        return SourceIdentity(
            tenant_id=request.tenant_id,
            connector_type=request.connector_type,
            connection_id=request.connection_id,
            source_item_id=SourceDescriptorMapper._source_item_id(request, source),
            source_scope_id=request.source_scope_id,
            binding=SourceBinding(
                kind=BindingKind(raw_binding),
                parent_source_item_id=(str(parent) if parent is not None else None),
            ),
        )

    @staticmethod
    def _source_item_id(
        request: SourceIngestionRequest,
        source: SourceRecord,
    ) -> str:
        del request
        relative_path = source.metadata.get("relative_path")
        if relative_path is None:
            return source.id
        if not str(relative_path).strip():
            raise ValueError("source relative_path must be non-empty when provided")
        value = str(relative_path).strip().replace("\\", "/")
        if value.startswith(("/", "../")) or "/../" in f"/{value}/":
            raise ValueError("source relative_path must stay inside its source scope")
        return value

    @staticmethod
    def _bound_admission(source: SourceRecord) -> AdmissionSnapshot:
        parent = str(source.metadata["parent_source_item_id"])
        version = str(source.metadata["source_version"])
        return AdmissionSnapshot(
            source_version=version,
            relations=(
                SourceRelationDescriptor(
                    relation_type=RelationType.ATTACHED_TO,
                    target_source_item_id=parent,
                    source_relation_version=version,
                ),
            ),
        )

    @staticmethod
    def _source_payload(source: SourceRecord) -> dict[str, Any]:
        payload = to_json_value(asdict(source))
        if not isinstance(payload, dict):
            raise TypeError("serialized source descriptor is not an object")
        return {str(key): value for key, value in payload.items()}
