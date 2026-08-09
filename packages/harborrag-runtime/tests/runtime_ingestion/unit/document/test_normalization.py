"""Connector-aware document normalization behavior."""

from __future__ import annotations

from harborrag_core.chunking import ChunkKind, ConnectorType, DocumentKind
from harborrag_core.domain import (
    DocumentElement,
    ParsedDocument,
    RawDocument,
)
from harborrag_engine.ingestion import (
    GraphProjectionBuilder,
    GraphProjectionInput,
)
from harborrag_engine.ingestion.chunking import ChunkingRequest
from harborrag_runtime.ingestion.composition import _chunker
from harborrag_runtime.ingestion.document.normalization import (
    build_source_document_normalizer,
)


def _parsed() -> ParsedDocument:
    return ParsedDocument(
        content="fallback",
        parser_name="html",
        elements=[
            DocumentElement(
                id="fallback:1",
                type="paragraph",
                content="fallback",
            )
        ],
    )


def test_confluence_normalization_preserves_structure_tables_and_comments() -> None:
    payload = {
        "id": "42",
        "title": "Deployment Guide",
        "type": "page",
        "space": {"id": "9", "key": "OPS"},
        "version": {"number": 7},
        "metadata": {"labels": {"results": [{"name": "release"}]}},
        "body": {
            "storage": {
                "value": (
                    "<h1>Verification</h1>"
                    "<p>Postgres publishes the version.</p>"
                    "<table><tbody>"
                    "<tr><th>Store</th><th>Check</th></tr>"
                    "<tr><td>Qdrant</td><td>vectors</td></tr>"
                    "</tbody></table>"
                )
            }
        },
    }
    raw = RawDocument(
        id="confluence://OPS/42",
        source="https://example.atlassian.net/wiki/spaces/OPS/pages/42",
        content="<p>fallback</p>",
        content_type="text/html",
        metadata={
            "source_system": "confluence",
            "space_key": "OPS",
            "title": "Deployment Guide",
            "version": 7,
            "comments": [
                {
                    "id": "101",
                    "body": "<p>Looks <strong>ready</strong>.</p>",
                    "author": "Reviewer",
                    "created_at": "2026-07-30T10:00:00Z",
                    "comment_kind": "PAGE_COMMENT",
                }
            ],
            "relations": [
                {
                    "predicate": "links_to",
                    "target_id": "confluence://OPS/43",
                    "target_type": "document",
                }
            ],
        },
        raw=payload,
    )

    document = build_source_document_normalizer().normalize(
        raw,
        _parsed(),
    )

    assert document.body_representation == "storage"
    assert len(document.table_artifacts) == 1
    assert document.table_artifacts[0].column_names == ("Store", "Check")
    comment = next(
        element for element in document.content if element.metadata.get("comment_id") == "101"
    )
    assert comment.content == "Looks ready."
    assert comment.metadata["role"] == "confluence.comment"
    assert any(
        relation.predicate == "links_to" and relation.target_id == "confluence://OPS/43"
        for relation in document.relations
    )


def test_unregistered_source_uses_generic_normalization() -> None:
    raw = RawDocument(
        id="file:///docs/runbook.md",
        source="file:///docs/runbook.md",
        content="Run it",
        content_type="text/markdown",
        metadata={"source_system": "local", "title": "Runbook"},
    )

    document = build_source_document_normalizer().normalize(
        raw,
        _parsed(),
    )

    assert document.title == "Runbook"
    assert document.content[0].content == "fallback"
    assert document.table_artifacts == ()


def test_confluence_comment_flows_to_comment_chunk_and_structural_graph() -> None:
    raw = RawDocument(
        id="confluence://OPS/42",
        source="https://example.atlassian.net/wiki/spaces/OPS/pages/42",
        content="<p>fallback</p>",
        content_type="text/html",
        metadata={
            "source_system": "confluence",
            "space_key": "OPS",
            "title": "Deployment Guide",
            "version": 7,
            "comments": [
                {
                    "id": "101",
                    "body": "<p>Looks ready.</p>",
                    "comment_kind": "PAGE_COMMENT",
                }
            ],
        },
        raw={
            "id": "42",
            "title": "Deployment Guide",
            "type": "page",
            "space": {"id": "9", "key": "OPS"},
            "version": {"number": 7},
            "body": {
                "storage": {
                    "value": ("<h1>Verification</h1><p>Postgres publishes the version.</p>")
                }
            },
        },
    )
    document = build_source_document_normalizer().normalize(raw, _parsed())
    chunks = (
        _chunker()
        .chunk(
            ChunkingRequest(
                tenant_id="tenant-1",
                document_version_id="document-version:1",
                document=document,
                connector_type="confluence",
                content_type="text/html",
            )
        )
        .chunks
    )
    graph = GraphProjectionBuilder().build(
        GraphProjectionInput(
            document=document,
            chunks=chunks,
            resolved_targets={},
            graph_projection_version="structural-graph-v1",
        )
    )

    comment = next(chunk for chunk in chunks if chunk.chunk_kind == ChunkKind.COMMENT)
    relation_types = {relation.relation_type.value for relation in graph.relations}
    assert comment.metadata["comment_id"] == "101"
    assert comment.content == "Looks ready."
    assert {"contains", "links_to"} <= relation_types


def test_jira_normalization_separates_prose_comments_and_typed_attributes() -> None:
    payload = {
        "id": "10001",
        "key": "HARBOR-142",
        "fields": {
            "summary": "Reject stale projection writes",
            "description": "Keep the active version visible until verification succeeds.",
            "project": {"id": "10", "key": "HARBOR"},
            "issuetype": {"id": "20", "name": "Bug"},
            "customfield_10010": (
                "Given a staged version, when verification fails, the previous version "
                "remains authoritative and visible to retrieval."
            ),
            "customfield_10011": {"value": "Platform"},
            "customfield_10012": 13,
        },
        "names": {
            "customfield_10010": "Acceptance Criteria",
            "customfield_10011": "Impact Area",
            "customfield_10012": "Story Points",
        },
        "schema": {
            "customfield_10010": {
                "type": "string",
                "custom": "com.atlassian.jira.plugin.system.customfieldtypes:textarea",
            },
            "customfield_10011": {
                "type": "option",
                "custom": "com.atlassian.jira.plugin.system.customfieldtypes:select",
            },
            "customfield_10012": {"type": "number"},
        },
    }
    raw = RawDocument(
        id="jira://HARBOR/HARBOR-142",
        source="https://jira.example.test/browse/HARBOR-142",
        content="rendered source capture",
        content_type="text/markdown",
        metadata={
            "source_system": "jira",
            "issue_key": "HARBOR-142",
            "project_id": "10",
            "project_key": "HARBOR",
            "title": "Reject stale projection writes",
            "comments": [
                {"id": "c1", "body": "First observation", "author": "Ada"},
                {
                    "id": "c2",
                    "body": "Follow-up observation",
                    "parent_comment_id": "c1",
                },
            ],
            "attachments": [{"id": "a1", "title": "evidence.txt", "text": "not parent evidence"}],
        },
        raw=payload,
    )

    document = build_source_document_normalizer().normalize(raw, _parsed())
    chunks = (
        _chunker()
        .chunk(
            ChunkingRequest(
                tenant_id="tenant-1",
                document_version_id="document-version:jira-1",
                document=document,
                connector_type="jira",
                content_type=document.content_type,
            )
        )
        .chunks
    )

    evidence = [chunk for chunk in chunks if chunk.record_kind.value == "evidence"]
    custom = next(chunk for chunk in evidence if chunk.metadata.get("field_id"))
    comments = [chunk for chunk in evidence if chunk.chunk_kind == ChunkKind.COMMENT]
    custom_fields = document.provenance.extra["custom_fields"]
    assert custom.metadata["field_id"] == "customfield_10010"
    assert custom.chunk_kind == ChunkKind.JIRA_FIELD
    assert {item["field_id"] for item in custom_fields} == {
        "customfield_10010",
        "customfield_10011",
        "customfield_10012",
    }
    assert {item["value_kind"] for item in custom_fields} == {
        "prose",
        "option",
        "number",
    }
    assert len(comments) == 2
    assert comments[1].metadata["parent_comment_id"] == "c1"
    assert "not parent evidence" not in " ".join(chunk.content for chunk in evidence)


def test_jira_spreadsheet_attachment_chunks_reference_every_canonical_table() -> None:
    raw = RawDocument(
        id="jira://HARBOR/HARBOR-142/attachments/10001",
        source="https://jira.example.test/browse/HARBOR-142#attachment-10001",
        content=b"spreadsheet bytes",
        content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        metadata={
            "binding_kind": "ATTACHMENT",
            "connector_type": "jira",
            "connection_id": "jira-main",
            "source_scope_id": "HARBOR",
            "source_item_id": "jira://HARBOR/HARBOR-142/attachments/10001",
            "parent_source_item_id": "jira://HARBOR/HARBOR-142",
            "filename": "evidence.xlsx",
            "source_version": "attachment-v1",
        },
    )
    parsed = ParsedDocument(
        content="Status\tOwner\nPassed\tAda",
        parser_name="excel",
        parser_version="1",
        elements=[
            DocumentElement(
                id="sheet:Evidence",
                type="table",
                content="Status\tOwner\nPassed\tAda",
                metadata={"sheet": "Evidence", "tab_path": ("Evidence",)},
            )
        ],
    )

    document = build_source_document_normalizer().normalize(raw, parsed)
    chunking = _chunker().chunk(
        ChunkingRequest(
            tenant_id="tenant-1",
            document_version_id="document-version:jira-attachment-v1",
            document=document,
            connector_type="jira",
            content_type=document.content_type,
        )
    )

    canonical_table_ids = {table.table_id for table in document.table_artifacts}
    table_chunks = tuple(
        chunk
        for chunk in chunking.chunks
        if chunk.chunk_kind == ChunkKind.TABLE and chunk.table_locator is not None
    )
    chunk_table_ids = {chunk.table_locator.table_id for chunk in table_chunks}

    assert chunking.strategy == "canonical"
    assert canonical_table_ids
    assert chunk_table_ids == canonical_table_ids
    assert all(chunk.connector_type == ConnectorType.JIRA for chunk in table_chunks)
    assert all(chunk.document_kind == DocumentKind.ATTACHMENT for chunk in table_chunks)
