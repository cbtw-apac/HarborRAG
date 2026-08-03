from __future__ import annotations

from harborrag_core.chunking import ConnectorType, DocumentKind
from harborrag_core.domain.document import Document
from harborrag_core.domain.element import DocumentElement
from harborrag_core.domain.provenance import DocumentProvenance
from harborrag_core.ingestion import (
    AdmissionSnapshot,
    ProcessingProfile,
    SourceBinding,
    SourceIdentity,
)
from harborrag_engine.ingestion import (
    CanonicalVersionPlanner,
    ChunkingRequest,
)

from .chunking_helpers import (
    make_document,
    make_profile,
    make_request,
    make_service,
)


def test_chunk_records_use_canonical_source_identity_for_attachments() -> None:
    source = SourceIdentity(
        connector_type=ConnectorType.JIRA,
        connection_id="jira-prod",
        source_item_id="attachment-100",
        source_scope_id="project-eng",
        binding=SourceBinding(
            kind="ATTACHMENT",
            parent_source_item_id="ENG-42",
        ),
    )
    planned = CanonicalVersionPlanner().plan(
        document=Document(
            id="jira://ENG/ENG-42/attachment/100",
            title="runbook.pdf",
            content=[
                DocumentElement(
                    id="page-1",
                    type="paragraph",
                    content="Production rollback instructions.",
                )
            ],
            content_type="application/pdf",
            provenance=DocumentProvenance(
                source="jira",
                record_id="attachment-100",
                extra={"source_version": "3"},
            ),
        ),
        source_identity=source,
        admission=AdmissionSnapshot(source_version="3"),
        processing=ProcessingProfile(
            parser_profile="pdf-v1",
            normalizer_version="canonical-v1",
            chunk_strategy="route-evidence-v3",
            dense_encoder_profile="dense-v1",
            sparse_encoder_profile="bm25-v1",
            graph_projection_version="graph-v1",
        ),
    )
    result = make_service(
        make_profile(
            name="jira",
            strategy="jira",
            minimum=1,
            target=128,
            maximum=256,
        ),
        configuration_version="3",
        create_route_chunks=True,
    ).chunk(
        ChunkingRequest(
            tenant_id="default",
            document_version_id=str(planned.candidate.document_version_id),
            document=planned.document,
            connector_type="jira",
        )
    )

    assert result.chunks
    assert {record.connector_type for record in result.chunks} == {ConnectorType.JIRA}
    assert {record.document_kind for record in result.chunks} == {DocumentKind.ATTACHMENT}
    assert {record.connection_id for record in result.chunks} == {"jira-prod"}
    assert {record.source_scope_id for record in result.chunks} == {"project-eng"}
    assert {record.source_item_id for record in result.chunks} == {"attachment-100"}
    assert {record.source_version for record in result.chunks} == {"3"}
    assert {record.metadata["source_version"] for record in result.chunks} == {"3"}


def test_chunk_records_accept_a_registered_future_connector_identifier() -> None:
    document = make_document(
        [DocumentElement("p1", "paragraph", "Notion page content")],
        source="notion",
        content_type="text/plain",
    )
    request = ChunkingRequest(
        tenant_id="tenant-1",
        document_version_id="version-1",
        document=document,
        connector_type="notion",
    )

    result = make_service(make_profile(minimum=1)).chunk(request)

    assert {record.connector_type.value for record in result.chunks} == {"notion"}
    assert {record.document_kind.value for record in result.chunks} == {"notion_document"}


def test_chunk_metadata_preserves_local_provider_source_version() -> None:
    document = make_document(
        [
            DocumentElement(
                id="paragraph-1",
                type="paragraph",
                content="Immutable local evidence.",
            )
        ],
        extra={"source_version": "mtime-1722384000"},
    )

    result = make_service(make_profile()).chunk(
        make_request(
            document,
            document_version_id="document-version-local",
        )
    )

    assert {record.source_version for record in result.chunks} == {"mtime-1722384000"}
    assert {record.metadata["source_version"] for record in result.chunks} == {"mtime-1722384000"}
