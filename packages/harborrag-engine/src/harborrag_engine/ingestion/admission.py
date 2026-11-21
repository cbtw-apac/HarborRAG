"""Admission decisions, document identities and change fingerprints.

Fingerprints and IDs are pure functions of the document and its source, so the
same input always yields the same document version.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Any

from pydantic_core import to_jsonable_python

from harborrag_core.chunking import ConnectorType
from harborrag_core.domain.document import Document
from harborrag_core.ingestion import (
    AdmissionSnapshot,
    ChangeFingerprintBuilder,
    ChangeFingerprints,
    DocumentIdentityBuilder,
    DocumentVersionCandidate,
    DocumentVersionSnapshot,
    ProcessingProfile,
    SourceAdmissionDecision,
    SourceIdentity,
    VectorPayload,
)
from harborrag_core.schemas.ids import DocumentId, DocumentVersionId

from .chunking.records.sharepoint import sharepoint_chunk_fields

_EVIDENCE_METADATA_FIELDS = frozenset({"comments", "custom_fields"})
_RETRIEVAL_METADATA_FIELDS = frozenset(
    {
        "ancestors",
        "attachment_names",
        "attachments",
        "breadcrumb",
        "children",
        "filename",
        "issue_key",
        "issue_links",
        "issue_type",
        "labels",
        "name",
        "parent",
        "project_key",
        "project_id",
        "relative_path",
        "space_id",
        "space_key",
        "status",
        "subtasks",
    }
)
_PAYLOAD_SOURCE_FIELDS = frozenset(VectorPayload.model_fields) | frozenset(
    {"issue_type", "source_created_at", "source_updated_at"}
)


def _projected_source_fields(
    document: Document,
    source_identity: SourceIdentity,
) -> dict[str, object]:
    """Return the document fields that chunks carry into their vector payloads.

    They are not in the evidence text, so they are hashed here. Otherwise a
    change to only an assignee or an update time would be UNCHANGED, and the
    active payloads would keep the old value.

    For every connector this is each provenance field named like a payload
    field, or like ``issue_type`` or a ``source_*_at`` time the payload falls
    back to, and the document's creation and update times. A strategy that
    copies fewer of them onto its chunks only gets an extra METADATA_CHANGED
    version, whose evidence vectors are reused. SharePoint also hashes the
    fields the chunk factory copies from its document.
    """
    provenance = document.provenance
    fields: dict[str, object] = {
        "payload_fields": to_jsonable_python(
            {key: value for key, value in provenance.extra.items() if key in _PAYLOAD_SOURCE_FIELDS}
        ),
        "created_at": to_jsonable_python(provenance.created_at),
        "updated_at": to_jsonable_python(provenance.updated_at),
    }
    if source_identity.connector_type == ConnectorType.SHAREPOINT:
        fields["sharepoint_chunk_fields"] = sharepoint_chunk_fields(document)
    return fields


class SourceAdmissionPolicy:
    """Make change decisions without coupling correctness to a runtime cache."""

    def before_fetch(
        self,
        *,
        active: DocumentVersionSnapshot | None,
        admission_change_key: str,
        processing_fingerprint: str,
        force_reprocess: bool = False,
    ) -> SourceAdmissionDecision:
        """Decide from source metadata alone whether a source item needs fetching.

        Args:
            active: The active version's snapshot, or ``None`` for a new item.
            admission_change_key: Change key the connector reported for the item.
            processing_fingerprint: Fingerprint of this run's processing profile.
            force_reprocess: Reprocess the item even when nothing changed.

        Returns:
            NEW, UPDATED, FORCE_REPROCESS or UNCHANGED.
        """
        if force_reprocess:
            return SourceAdmissionDecision.FORCE_REPROCESS
        if active is None:
            return SourceAdmissionDecision.NEW
        fingerprints = active.fingerprints
        if fingerprints.admission_change_key != admission_change_key:
            return SourceAdmissionDecision.UPDATED
        if fingerprints.processing_fingerprint != processing_fingerprint:
            return SourceAdmissionDecision.FORCE_REPROCESS
        return SourceAdmissionDecision.UNCHANGED

    def after_normalization(
        self,
        *,
        active: DocumentVersionSnapshot | None,
        fingerprints: ChangeFingerprints,
    ) -> SourceAdmissionDecision:
        """Classify a normalized document against the active version.

        A content change outranks a processing change, which outranks a
        retrieval metadata change.

        Args:
            active: The active version's snapshot, or ``None`` for a new item.
            fingerprints: Fingerprints of the normalized candidate.

        Returns:
            NEW, UPDATED, FORCE_REPROCESS, METADATA_CHANGED or UNCHANGED.
        """
        if active is None:
            return SourceAdmissionDecision.NEW
        previous = active.fingerprints
        if previous.canonical_content_hash != fingerprints.canonical_content_hash:
            return SourceAdmissionDecision.UPDATED
        if previous.processing_fingerprint != fingerprints.processing_fingerprint:
            return SourceAdmissionDecision.FORCE_REPROCESS
        if previous.retrieval_metadata_hash != fingerprints.retrieval_metadata_hash:
            return SourceAdmissionDecision.METADATA_CHANGED
        return SourceAdmissionDecision.UNCHANGED


@dataclass(frozen=True, slots=True)
class PlannedDocumentVersion:
    """Canonical document plus deterministic identities and fingerprints.

    Attributes:
        document: The document rebound to its new IDs.
        candidate: The version's IDs, source identity and fingerprints.
    """

    document: Document
    candidate: DocumentVersionCandidate


class CanonicalVersionPlanner:
    """Apply stable identity and split evidence from retrieval metadata.

    Args:
        identities: Derives document and version IDs. Defaults to the standard builder.
        fingerprints: Builds change fingerprints. Defaults to the standard builder.
    """

    def __init__(
        self,
        *,
        identities: DocumentIdentityBuilder | None = None,
        fingerprints: ChangeFingerprintBuilder | None = None,
    ) -> None:
        self._identities = identities or DocumentIdentityBuilder()
        self._fingerprints = fingerprints or ChangeFingerprintBuilder()

    def plan(
        self,
        *,
        document: Document,
        source_identity: SourceIdentity,
        admission: AdmissionSnapshot,
        processing: ProcessingProfile,
    ) -> PlannedDocumentVersion:
        """Derive the document's IDs and fingerprints and rebind it to its version.

        Args:
            document: The normalized document.
            source_identity: Where the document came from.
            admission: The source version recorded at admission.
            processing: Processing profile of this run.

        Returns:
            The rebound document and its version candidate.
        """
        document_id = self._identities.document_id(
            tenant_id=source_identity.tenant_id,
            connector_type=source_identity.connector_type,
            connection_id=source_identity.connection_id,
            source_item_id=source_identity.source_item_id,
        )
        version_fingerprints = self._fingerprints.build(
            admission=admission,
            canonical_evidence=self.evidence_view(document),
            retrieval_metadata=self.retrieval_metadata_view(
                document,
                source_identity=source_identity,
            ),
            processing=processing,
        )
        document_version_id = self._identities.document_version_id(
            document_id=document_id,
            canonical_content_hash=version_fingerprints.canonical_content_hash,
            retrieval_metadata_hash=version_fingerprints.retrieval_metadata_hash,
            processing_fingerprint=version_fingerprints.processing_fingerprint,
        )
        canonical_document = self.reidentify(
            document,
            document_id=document_id,
            document_version_id=document_version_id,
            source_identity=source_identity,
            processing=processing,
        )
        return PlannedDocumentVersion(
            document=canonical_document,
            candidate=DocumentVersionCandidate(
                document_id=document_id,
                document_version_id=document_version_id,
                source_identity=source_identity,
                fingerprints=version_fingerprints,
            ),
        )

    @staticmethod
    def evidence_view(document: Document) -> dict[str, object]:
        """Return the parts of a document hashed into its canonical content hash."""
        provenance = document.provenance
        extra = {
            key: value
            for key, value in provenance.extra.items()
            if key in _EVIDENCE_METADATA_FIELDS
        }
        return {
            "content": to_jsonable_python(document.content),
            "blocks": to_jsonable_python(document.blocks),
            "tables": to_jsonable_python(document.table_artifacts),
            "evidence_metadata": extra,
        }

    @staticmethod
    def retrieval_metadata_view(
        document: Document,
        *,
        source_identity: SourceIdentity,
    ) -> dict[str, object]:
        """Return the metadata hashed into a document's retrieval metadata hash.

        A change here without a content change is METADATA_CHANGED.

        Args:
            document: The normalized document.
            source_identity: Where the document came from.

        Returns:
            Title, source, URL, tags, relations, selected metadata, table
            captions, and the document fields chunks carry into vector payloads.
        """
        provenance = document.provenance
        extra = {
            key: value
            for key, value in provenance.extra.items()
            if key in _RETRIEVAL_METADATA_FIELDS
        }
        return {
            "title": document.title,
            "content_type": document.content_type,
            "connector_type": source_identity.connector_type.value,
            "source_scope_id": source_identity.source_scope_id,
            "source_item_id": source_identity.source_item_id,
            "url": provenance.url,
            "tags": tuple(provenance.tags),
            "relations": to_jsonable_python(document.relations),
            "metadata": extra,
            "table_captions": tuple(
                table.caption for table in document.table_artifacts if table.caption
            ),
            **_projected_source_fields(document, source_identity),
        }

    @staticmethod
    def reidentify(
        document: Document,
        *,
        document_id: DocumentId,
        document_version_id: DocumentVersionId,
        source_identity: SourceIdentity,
        processing: ProcessingProfile | None = None,
    ) -> Document:
        """Bind canonical evidence to a new immutable processing version."""
        provenance = document.provenance
        binding = source_identity.binding
        source_extra = dict(provenance.extra)
        uses_portable_path = source_extra.get("relative_path") is not None
        if uses_portable_path:
            for runtime_path_field in ("accessed_at", "parent_path", "path"):
                source_extra.pop(runtime_path_field, None)
        extra = {
            **source_extra,
            "binding_kind": binding.kind.value,
            "connector_type": source_identity.connector_type.value,
            "connection_id": source_identity.connection_id,
            "source_scope_id": source_identity.source_scope_id,
            "source_item_id": source_identity.source_item_id,
        }
        if processing is not None:
            extra["processing_profile"] = processing.model_dump(mode="json")
        if provenance.created_at is not None:
            extra["source_created_at"] = provenance.created_at.isoformat()
        if provenance.updated_at is not None:
            extra["source_updated_at"] = provenance.updated_at.isoformat()
        if binding.parent_source_item_id is not None:
            extra["parent_source_item_id"] = binding.parent_source_item_id
        updated_provenance = replace(
            provenance,
            source=(source_identity.source_item_id if uses_portable_path else provenance.source),
            record_id=source_identity.source_item_id,
            url=provenance.url,
            extra=extra,
        )
        tables = tuple(
            table.model_copy(
                update={
                    "document_id": document_id,
                    "document_version_id": document_version_id,
                }
            )
            for table in document.table_artifacts
        )
        return replace(
            document,
            id=document_id,
            provenance=updated_provenance,
            table_artifacts=tables,
            raw=None,
        )


def source_version_from_document(document: Document) -> str:
    """Return a durable source version without using workflow/runtime fields."""
    metadata: dict[str, Any] = document.provenance.extra
    for key in ("source_version", "version", "checksum"):
        value = metadata.get(key)
        if value is not None and str(value).strip():
            return str(value).strip()
    if document.provenance.checksum:
        return document.provenance.checksum
    if document.provenance.updated_at is not None:
        return document.provenance.updated_at.isoformat()
    raise ValueError("canonical document has no durable source version")
