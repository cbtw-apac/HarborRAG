"""Cross-connector graph relation repair through the control-plane repository."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest

from harborrag_adapters.connectors.descriptors import ConnectorDocumentDescriptor
from harborrag_core.chunking import ConnectorType, RelationType
from harborrag_core.domain.raw_document import RawDocument
from harborrag_core.domain.source import SourceRecord
from harborrag_core.ingestion import AdmissionSnapshot, DocumentIdentityBuilder, GraphEntityType
from harborrag_runtime.ingestion import DocumentReleaseService, SourceIngestionService

from ...fixtures.connectors import SourceConnector
from ...fixtures.release import (
    ReleaseResources,
    build_control_plane,
    build_dependencies,
    build_relation_repair_service,
    build_release_resources,
    source_request,
)


class _SingleSourceConnector(SourceConnector):
    def __init__(
        self,
        *,
        source_item_id: str,
        metadata: dict[str, object],
        target_id: str | None = None,
    ) -> None:
        super().__init__()
        self._source_item_id = source_item_id
        self._metadata = metadata
        self._target_id = target_id

    def discover(self, query):
        del query
        metadata = dict(self._metadata)
        if self._target_id is not None:
            metadata["relations"] = [
                {
                    "predicate": "links_to",
                    "target_id": self._target_id,
                    "target_type": "document",
                }
            ]
        yield SourceRecord(
            id=self._source_item_id,
            source_type="text/plain",
            locator=self._source_item_id,
            metadata=metadata,
        )

    def describe(self, record: SourceRecord) -> ConnectorDocumentDescriptor:
        return ConnectorDocumentDescriptor(
            source=record,
            admission=AdmissionSnapshot(source_version=f"{record.id}-v1"),
        )

    def load(self, record: SourceRecord) -> RawDocument:
        raw = super().load(record)
        return RawDocument(
            id=raw.id,
            source=raw.source,
            content=raw.content,
            content_type=raw.content_type,
            metadata={**raw.metadata, **record.metadata},
        )


@pytest.mark.asyncio
async def test_jira_link_repairs_to_the_authoritative_confluence_document(
    tmp_path: Path,
) -> None:
    """A schemed Jira relation crosses connectors through the real SQLite catalog."""

    page_id = "confluence://ENG/77"
    issue_id = "jira://OPS/OPS-3"
    control = build_control_plane(tmp_path)
    resources = build_release_resources(control)
    async with control, resources.store:
        dependencies = build_dependencies(resources)
        source_service = SourceIngestionService(
            control=control,
            documents=DocumentReleaseService(dependencies),
            relations=build_relation_repair_service(resources, dependencies),
        )

        await source_service.ingest(
            replace(
                source_request("task-confluence"),
                connector_name="wiki",
                connector_type=ConnectorType.CONFLUENCE,
                connection_id="wiki.example",
                source_scope_id="wiki-eng",
            ),
            _SingleSourceConnector(
                source_item_id=page_id,
                metadata={"space_id": "space-eng", "space_key": "ENG", "page_id": "77"},
            ),
        )

        catalog_target = await control.document_versions.resolve_unambiguous_active_sources(
            tenant_id="default",
            connector_type="confluence",
            source_item_ids=(page_id,),
        )
        assert catalog_target[page_id].source_scope_id == "wiki-eng"
        assert not await control.document_versions.resolve_unambiguous_active_sources(
            tenant_id="another-tenant",
            connector_type="confluence",
            source_item_ids=(page_id,),
        )

        outcome = await source_service.ingest(
            replace(
                source_request("task-jira"),
                connector_name="issues",
                connector_type=ConnectorType.JIRA,
                connection_id="jira.example",
                source_scope_id="jira-ops",
            ),
            _SingleSourceConnector(
                source_item_id=issue_id,
                metadata={"project_id": "project-ops", "project_key": "OPS", "issue_key": "OPS-3"},
                target_id=page_id,
            ),
        )

        assert outcome.unresolved_relations == 0
        endpoints = {
            (relation.source_node_key, relation.target_node_key) for relation in _links(resources)
        }
        assert (
            _key("jira-ops", GraphEntityType.JIRA_ISSUE, "OPS-3"),
            _key("wiki-eng", GraphEntityType.CONFLUENCE_PAGE, "77"),
        ) in endpoints


@pytest.mark.asyncio
async def test_cross_connector_catalog_rejects_an_ambiguous_connection(
    tmp_path: Path,
) -> None:
    page_id = "confluence://ENG/77"
    control = build_control_plane(tmp_path)
    resources = build_release_resources(control)
    async with control, resources.store:
        dependencies = build_dependencies(resources)
        source_service = SourceIngestionService(
            control=control,
            documents=DocumentReleaseService(dependencies),
            relations=build_relation_repair_service(resources, dependencies),
        )
        for index in (1, 2):
            await source_service.ingest(
                replace(
                    source_request(f"task-confluence-{index}"),
                    connector_name=f"wiki-{index}",
                    connector_type=ConnectorType.CONFLUENCE,
                    connection_id=f"wiki-{index}.example",
                    source_scope_id=f"wiki-eng-{index}",
                ),
                _SingleSourceConnector(
                    source_item_id=page_id,
                    metadata={
                        "space_id": "space-eng",
                        "space_key": "ENG",
                        "page_id": "77",
                    },
                ),
            )

        assert not await control.document_versions.resolve_unambiguous_active_sources(
            tenant_id="default",
            connector_type="confluence",
            source_item_ids=(page_id,),
        )


def _key(source_scope_id: str, entity_type: GraphEntityType, provider_id: str) -> str:
    return DocumentIdentityBuilder().source_entity_node_key(
        tenant_id="default",
        source_scope_id=source_scope_id,
        entity_type=entity_type.value,
        provider_id=provider_id,
    )


def _links(resources: ReleaseResources):
    return [
        relation
        for relation in resources.graph.relations.values()
        if relation.relation_type == RelationType.LINKS_TO
    ]
