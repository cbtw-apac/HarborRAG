"""Identities and change fingerprints of ingested documents and their graph projections.

Every identifier comes from stable inputs through ``encoded_identifier``, so a repeated
run reproduces it and a changed input changes it. Change an input or its encoding only
with a plan for the identifiers already stored.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from hashlib import sha256
from urllib.parse import quote
from uuid import UUID, uuid5

from harborrag_core.chunking import ConnectorType, RelationType
from harborrag_core.chunking.identity import canonical_identity_payload, encoded_identifier
from harborrag_core.schemas.ids import DocumentId, DocumentVersionId
from harborrag_core.security.field_names import canonical_field_name

from .source_contracts import (
    AdmissionSnapshot,
    ChangeFingerprints,
    ProcessingProfile,
    SourceIdentity,
)
from .states import KnowledgeNodeKind

POINT_NAMESPACE = UUID("8e134e44-cb65-5d87-8777-4e897fc7f08d")
_LOSSY_SPACING = re.compile(r"[^\S ]| {2}|\A | \Z")

_RUNTIME_FIELDS = frozenset(
    {
        "temporal_workflow_id",
        "temporal_run_id",
        "workflow_id",
        "run_id",
        "activity_id",
        "task_queue",
        "worker_id",
        "retry_count",
        "attempt",
        "attempt_number",
        "heartbeat",
        "trace_id",
        "span_id",
        "request_id",
        "provider_request_id",
        "latency",
        "token_usage",
        "temporary_file_path",
        "temporary_status",
        "signed_url",
        "access_token",
        "cache_key",
        "lock_owner",
        "stack_trace",
    }
)


def is_runtime_field(name: object) -> bool:
    """Whether a key names runtime-only state that knowledge records must not carry."""
    return canonical_field_name(name) in _RUNTIME_FIELDS


def reject_runtime_fields(value: object) -> None:
    """Reject runtime-only keys before canonical or projection persistence."""
    if isinstance(value, Mapping):
        for raw_key, item in value.items():
            key = canonical_field_name(raw_key)
            if key in _RUNTIME_FIELDS:
                raise ValueError(f"runtime field is not allowed in knowledge records: {key}")
            reject_runtime_fields(item)
        return
    if isinstance(value, (list, tuple)):
        for item in value:
            reject_runtime_fields(item)


def _with_exact_spacing(identity: dict[str, str], key: str, value: str) -> dict[str, str]:
    """Add the exact form of a source value whose spacing identity encoding would change.

    ``encoded_identifier`` normalizes text. It trims a value and folds every run of
    whitespace into one space, so two file paths that differ only in spacing would
    share one identity. Their exact form, percent-encoded so normalization leaves it
    alone, keeps them apart. A value whose spacing normalization keeps gets no extra
    entry, so every identifier stored before keeps its value.
    """
    if _LOSSY_SPACING.search(value):
        identity[f"{key}_exact"] = quote(value, safe="")
    return identity


def _document_identity(
    *,
    tenant_id: str,
    connector_type: ConnectorType,
    connection_id: str,
    source_item_id: str,
) -> dict[str, str]:
    """Return the identity inputs of a document ID, before any exact-spacing entry."""
    identity = {
        "connector_type": connector_type.value,
        "connection_id": connection_id,
        "source_item_id": source_item_id,
    }
    if tenant_id != "DEFAULT":
        identity["tenant_id"] = tenant_id
    return identity


class DocumentIdentityBuilder:
    """Build every ingestion identity from stable knowledge inputs."""

    def document_id(
        self,
        *,
        tenant_id: str,
        connector_type: ConnectorType,
        connection_id: str,
        source_item_id: str,
    ) -> DocumentId:
        """Return the stable ID of one source item in one connection.

        The historic ``DEFAULT`` tenant is left out of the identity, so documents
        stored before tenancy became authoritative keep their IDs. Every other
        tenant is part of it.

        Args:
            tenant_id: Tenant that owns the connection.
            connector_type: Connector that produced the item.
            connection_id: Connection the item came from.
            source_item_id: The connector's ID of the item, such as a file path.

        Returns:
            The document ID.
        """
        identity = _document_identity(
            tenant_id=tenant_id,
            connector_type=connector_type,
            connection_id=connection_id,
            source_item_id=source_item_id,
        )
        return DocumentId(
            encoded_identifier(
                "document",
                _with_exact_spacing(identity, "source_item_id", source_item_id),
            )
        )

    def document_version_id(
        self,
        *,
        document_id: str,
        canonical_content_hash: str,
        retrieval_metadata_hash: str,
        processing_fingerprint: str,
    ) -> DocumentVersionId:
        """Return the ID of a document version from its content and processing hashes."""
        return DocumentVersionId(
            encoded_identifier(
                "document-version",
                {
                    "document_id": document_id,
                    "canonical_content_hash": canonical_content_hash,
                    "retrieval_metadata_hash": retrieval_metadata_hash,
                    "processing_fingerprint": processing_fingerprint,
                },
            )
        )

    def point_id(self, *, chunk_id: str) -> str:
        """Return the vector point UUID of a chunk."""
        return str(uuid5(POINT_NAMESPACE, chunk_id))

    def node_key(
        self,
        *,
        node_kind: KnowledgeNodeKind,
        entity_type: str | None = None,
        logical_id: str,
        document_version_id: str,
    ) -> str:
        """Return the key of a graph node that one document version owns."""
        return encoded_identifier(
            "graph-v2-node",
            {
                "node_kind": node_kind.value,
                "entity_type": entity_type,
                "logical_id": logical_id,
                "document_version_id": document_version_id,
            },
        )

    def tenant_node_key(self, *, tenant_id: str) -> str:
        """Return the key of a tenant's graph node."""
        return encoded_identifier("graph-v2-tenant", {"tenant_id": tenant_id})

    def data_source_node_key(self, *, tenant_id: str, source_scope_id: str) -> str:
        """Return the key of the graph node of one source scope of a tenant."""
        return encoded_identifier(
            "graph-v2-data-source",
            {"tenant_id": tenant_id, "source_scope_id": source_scope_id},
        )

    def source_entity_node_key(
        self,
        *,
        tenant_id: str,
        source_scope_id: str,
        entity_type: str,
        provider_id: str,
    ) -> str:
        """Return the key of a source item or container node inside one source scope."""
        return encoded_identifier(
            "graph-v2-source-entity",
            _with_exact_spacing(
                {
                    "tenant_id": tenant_id,
                    "source_scope_id": source_scope_id,
                    "entity_type": entity_type,
                    "provider_id": provider_id,
                },
                "provider_id",
                provider_id,
            ),
        )

    def external_source_entity_node_key(
        self,
        *,
        tenant_id: str,
        connector_type: str,
        connection_id: str,
        source_item_id: str,
    ) -> str:
        """Key a stand-in for a link target no ingested scope has published.

        Deliberately scope-free: a source item ID does not establish the scope that will
        one day ingest it, so two documents in different scopes that link to the same
        foreign item must reach one stub. The concrete node keeps its scoped key; when
        the target is ingested, relation repair re-points the links and the stub is
        pruned once nothing references it.
        """
        return encoded_identifier(
            "graph-v2-external-source-entity",
            _with_exact_spacing(
                {
                    "tenant_id": tenant_id,
                    "connector_type": connector_type,
                    "connection_id": connection_id,
                    "source_item_id": source_item_id,
                },
                "source_item_id",
                source_item_id,
            ),
        )

    def document_version_node_key(
        self,
        *,
        document_id: str,
        document_version_id: str,
    ) -> str:
        """Return the key of a document version's graph node."""
        return encoded_identifier(
            "graph-v2-document-version",
            {
                "document_id": document_id,
                "document_version_id": document_version_id,
            },
        )

    def relation_id(
        self,
        *,
        relation_type: RelationType,
        source_node_key: str,
        target_node_key: str,
        source_relation_version: str,
    ) -> str:
        """Return the ID of one graph relation between two nodes."""
        return encoded_identifier(
            "graph-relation",
            {
                "relation_type": relation_type.value,
                "source_node_key": source_node_key,
                "target_node_key": target_node_key,
                "source_relation_version": source_relation_version,
            },
        )


class ChangeFingerprintBuilder:
    """Build the four independent fingerprints used by admission."""

    def admission_change_key(
        self,
        *,
        snapshot: AdmissionSnapshot,
    ) -> str:
        """Return the key that changes when the source reports a changed item."""
        raw_value = {
            "source_version": snapshot.source_version,
            "comments": tuple(item.model_dump(mode="json") for item in snapshot.comments),
            "attachments": tuple(item.model_dump(mode="json") for item in snapshot.attachments),
            "relations": tuple(item.model_dump(mode="json") for item in snapshot.relations),
        }
        reject_runtime_fields(raw_value)
        value = {
            "source_version": raw_value["source_version"],
            "comments": tuple(
                sorted(canonical_identity_payload(item) for item in raw_value["comments"])
            ),
            "attachments": tuple(
                sorted(canonical_identity_payload(item) for item in raw_value["attachments"])
            ),
            "relations": tuple(
                sorted(canonical_identity_payload(item) for item in raw_value["relations"])
            ),
        }
        return encoded_identifier("admission-change", value)

    def canonical_content_hash(self, evidence: object) -> str:
        """Return the SHA-256 of a document's canonical evidence."""
        reject_runtime_fields(evidence)
        payload = canonical_identity_payload(evidence).encode("utf-8")
        return sha256(payload).hexdigest()

    def retrieval_metadata_hash(self, metadata: Mapping[str, object]) -> str:
        """Return the hash of a document's retrieval metadata."""
        reject_runtime_fields(metadata)
        return encoded_identifier("retrieval-metadata", dict(metadata))

    def processing_fingerprint(
        self,
        *,
        profile: ProcessingProfile,
    ) -> str:
        """Return the fingerprint of the parser, chunker and encoder profiles in use."""
        return encoded_identifier(
            "processing",
            {
                "parser_profile": profile.parser_profile,
                "normalizer_version": profile.normalizer_version,
                "chunk_strategy": profile.chunk_strategy,
                "dense_encoder_profile": profile.dense_encoder_profile,
                "sparse_encoder_profile": profile.sparse_encoder_profile,
                "graph_projection_version": profile.graph_projection_version,
                "vector_projection_schema": profile.vector_projection_schema,
            },
        )

    def build(
        self,
        *,
        admission: AdmissionSnapshot,
        canonical_evidence: object,
        retrieval_metadata: Mapping[str, object],
        processing: ProcessingProfile,
    ) -> ChangeFingerprints:
        """Return the four fingerprints of one document version."""
        return ChangeFingerprints(
            admission_change_key=self.admission_change_key(snapshot=admission),
            canonical_content_hash=self.canonical_content_hash(canonical_evidence),
            retrieval_metadata_hash=self.retrieval_metadata_hash(retrieval_metadata),
            processing_fingerprint=self.processing_fingerprint(profile=processing),
        )


def identity_for_source(source: SourceIdentity) -> DocumentId:
    """Return the document ID of a source identity."""
    return DocumentIdentityBuilder().document_id(
        tenant_id=source.tenant_id,
        connector_type=source.connector_type,
        connection_id=source.connection_id,
        source_item_id=source.source_item_id,
    )


def legacy_identity_for_source(source: SourceIdentity) -> DocumentId:
    """Return the document ID that 2.0.0a2 and earlier gave a source identity.

    Those releases had no exact-spacing entry, so source item IDs that differ only
    in spacing shared one ID. For every other source item ID it equals
    ``identity_for_source``.
    """
    return DocumentId(
        encoded_identifier(
            "document",
            _document_identity(
                tenant_id=source.tenant_id,
                connector_type=source.connector_type,
                connection_id=source.connection_id,
                source_item_id=source.source_item_id,
            ),
        )
    )
