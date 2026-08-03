from __future__ import annotations

from uuid import UUID

import pytest

from harborrag_core.chunking import ConnectorType, RelationType
from harborrag_core.ingestion import (
    AdmissionSnapshot,
    BindingKind,
    ChangeFingerprintBuilder,
    DocumentIdentityBuilder,
    ProcessingProfile,
    SourceBinding,
    SourceIdentity,
    SourceObjectVersion,
    SourceRelationDescriptor,
    identity_for_source,
    reject_runtime_fields,
)


def processing_profile(*, chunk_strategy: str = "section-v1") -> ProcessingProfile:
    return ProcessingProfile(
        parser_profile="confluence-storage-v1",
        normalizer_version="canonical-v1",
        chunk_strategy=chunk_strategy,
        dense_encoder_profile="dense-v1",
        sparse_encoder_profile="bm25-v1",
        graph_projection_version="graph-v1",
    )


def admission_snapshot(*, reversed_order: bool = False) -> AdmissionSnapshot:
    comments = (
        SourceObjectVersion(source_item_id="comment-1", source_version="1"),
        SourceObjectVersion(source_item_id="comment-2", source_version="3"),
    )
    attachments = (
        SourceObjectVersion(source_item_id="attachment-1", source_version="2"),
        SourceObjectVersion(source_item_id="attachment-2", source_version="1"),
    )
    relations = (
        SourceRelationDescriptor(
            relation_type=RelationType.LINKS_TO,
            target_source_item_id="page-2",
            source_relation_version="1",
        ),
        SourceRelationDescriptor(
            relation_type=RelationType.INCLUDES,
            target_source_item_id="page-3",
            source_relation_version="4",
        ),
    )
    if reversed_order:
        comments = tuple(reversed(comments))
        attachments = tuple(reversed(attachments))
        relations = tuple(reversed(relations))
    return AdmissionSnapshot(
        source_version="8",
        comments=comments,
        attachments=attachments,
        relations=relations,
    )


def test_document_and_version_identities_are_deterministic() -> None:
    source = SourceIdentity(
        connector_type=ConnectorType.CONFLUENCE,
        connection_id="wiki.example",
        source_item_id="page-42",
        source_scope_id="engineering",
    )
    identity = DocumentIdentityBuilder()
    fingerprints = ChangeFingerprintBuilder().build(
        admission=admission_snapshot(),
        canonical_evidence={"body": ["first", "second"]},
        retrieval_metadata={"labels": ["release", "runbook"], "title": "Deploy"},
        processing=processing_profile(),
    )

    document_id = identity_for_source(source)
    repeated_document_id = identity.document_id(
        connector_type=source.connector_type,
        connection_id=source.connection_id,
        source_item_id=source.source_item_id,
    )
    version_id = identity.document_version_id(
        document_id=document_id,
        canonical_content_hash=fingerprints.canonical_content_hash,
        retrieval_metadata_hash=fingerprints.retrieval_metadata_hash,
        processing_fingerprint=fingerprints.processing_fingerprint,
    )

    assert document_id == repeated_document_id
    assert version_id == identity.document_version_id(
        document_id=document_id,
        canonical_content_hash=fingerprints.canonical_content_hash,
        retrieval_metadata_hash=fingerprints.retrieval_metadata_hash,
        processing_fingerprint=fingerprints.processing_fingerprint,
    )


def test_document_identity_is_tenant_scoped_without_changing_legacy_default() -> None:
    identity = DocumentIdentityBuilder()
    values = {
        "connector_type": ConnectorType.CONFLUENCE,
        "connection_id": "wiki.example",
        "source_item_id": "page-42",
    }

    legacy = identity.document_id(**values)
    default = identity.document_id(tenant_id="DEFAULT", **values)
    tenant_a = identity.document_id(tenant_id="tenant-a", **values)
    tenant_b = identity.document_id(tenant_id="tenant-b", **values)

    assert legacy == default
    assert len({tenant_a, tenant_b, default}) == 3


def test_descriptor_order_does_not_change_admission_identity() -> None:
    builder = ChangeFingerprintBuilder()

    assert builder.admission_change_key(
        snapshot=admission_snapshot()
    ) == builder.admission_change_key(snapshot=admission_snapshot(reversed_order=True))


def test_content_order_and_processing_changes_create_new_version_inputs() -> None:
    builder = ChangeFingerprintBuilder()

    assert builder.canonical_content_hash(
        {"body": ["first", "second"]}
    ) != builder.canonical_content_hash({"body": ["second", "first"]})
    assert builder.processing_fingerprint(
        profile=processing_profile()
    ) != builder.processing_fingerprint(profile=processing_profile(chunk_strategy="section-v2"))


def test_retrieval_metadata_mapping_order_does_not_change_hash() -> None:
    builder = ChangeFingerprintBuilder()

    assert builder.retrieval_metadata_hash(
        {"title": "Runbook", "labels": ["ops"]}
    ) == builder.retrieval_metadata_hash({"labels": ["ops"], "title": "Runbook"})


@pytest.mark.parametrize(
    "runtime_key",
    [
        "workflow_id",
        "trace_id",
        "retry_count",
        "signed_url",
        "stack_trace",
        "token_usage",
    ],
)
def test_runtime_fields_are_rejected_recursively(runtime_key: str) -> None:
    with pytest.raises(ValueError, match="runtime field"):
        reject_runtime_fields({"canonical": [{"metadata": {runtime_key: "secret"}}]})


def test_root_binding_rejects_placeholder_parent_and_attachment_requires_parent() -> None:
    with pytest.raises(ValueError, match="must not have a parent"):
        SourceBinding(kind=BindingKind.ROOT, parent_source_item_id="")
    with pytest.raises(ValueError, match="require a parent"):
        SourceBinding(kind=BindingKind.ATTACHMENT)


def test_qdrant_point_identity_is_stable_uuid_v5() -> None:
    identity = DocumentIdentityBuilder()

    first = identity.point_id(chunk_id="chunk:abc")
    second = identity.point_id(chunk_id="chunk:abc")

    assert first == second
    assert UUID(first).version == 5
