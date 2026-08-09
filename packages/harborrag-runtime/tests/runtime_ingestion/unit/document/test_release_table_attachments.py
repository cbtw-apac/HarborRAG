"""Release-pipeline regression coverage for canonical attachment tables."""

from __future__ import annotations

from pathlib import Path

import pytest

from harborrag_adapters.repositories.object_store import MemoryObjectStore
from harborrag_core.chunking import ChunkKind, ConnectorType
from harborrag_core.domain import DocumentElement, ParsedDocument
from harborrag_core.domain.source import SourceRecord
from harborrag_core.ingestion import (
    AdmissionSnapshot,
    BindingKind,
    SourceBinding,
    SourceIdentity,
)
from harborrag_core.storage import StorageOperationContext
from harborrag_runtime.ingestion import DocumentReleaseRequest, DocumentReleaseService

from ...fixtures.connectors import DeterministicEmbedClient, SourceConnector, TextParser
from ...fixtures.release import (
    ReleaseResources,
    build_control_plane,
    build_dependencies,
    processing_profile,
)
from ...fixtures.storage import InMemoryKnowledgeGraph, InMemoryVectorRepository


class _SpreadsheetParser(TextParser):
    def parse(self, raw) -> ParsedDocument:
        del raw
        self.calls += 1
        return ParsedDocument(
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


@pytest.mark.asyncio
async def test_jira_spreadsheet_attachment_release_publishes_verified_tables(
    tmp_path: Path,
) -> None:
    control = build_control_plane(tmp_path)
    resources = ReleaseResources(
        control=control,
        store=MemoryObjectStore(),
        parser=_SpreadsheetParser(),
        embed=DeterministicEmbedClient(),
        vectors=InMemoryVectorRepository(),
        graph=InMemoryKnowledgeGraph(),
    )
    connector = SourceConnector()
    source_item_id = "jira://HARBOR/HARBOR-142/attachments/10001"
    request = DocumentReleaseRequest(
        tenant_id="default",
        connector_name="jira-main",
        source=SourceRecord(
            id=source_item_id,
            source_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            locator="https://jira.example.test/browse/HARBOR-142#attachment-10001",
            metadata={"filename": "evidence.xlsx"},
        ),
        source_identity=SourceIdentity(
            tenant_id="default",
            connector_type=ConnectorType.JIRA,
            connection_id="jira-main",
            source_item_id=source_item_id,
            source_scope_id="HARBOR",
            binding=SourceBinding(
                kind=BindingKind.ATTACHMENT,
                parent_source_item_id="jira://HARBOR/HARBOR-142",
            ),
        ),
        admission=AdmissionSnapshot(source_version="attachment-v1"),
        processing=processing_profile(),
    )

    async with control, resources.store:
        dependencies = build_dependencies(resources)
        outcome = await DocumentReleaseService(dependencies).release(request, connector)

        assert outcome.published is True
        assert outcome.document_version_id is not None
        manifest = await control.reliability.projection_manifest(outcome.document_version_id)
        snapshot = await control.document_versions.get_version(outcome.document_version_id)
        assert manifest is not None
        assert snapshot is not None
        assert snapshot.chunk_artifact is not None
        chunks = await dependencies.chunk_reader.get_all(
            snapshot.chunk_artifact,
            context=StorageOperationContext.system("default"),
        )
        chunk_table_ids = {
            chunk.table_locator.table_id
            for chunk in chunks
            if chunk.chunk_kind == ChunkKind.TABLE and chunk.table_locator is not None
        }
        assert chunk_table_ids == set(manifest.canonical_table_ids)
        assert chunk_table_ids
