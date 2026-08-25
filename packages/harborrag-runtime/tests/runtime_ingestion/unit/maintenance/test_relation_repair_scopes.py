"""Relation repair across the sibling scopes of one connection."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest

from harborrag_core.chunking import RelationType
from harborrag_core.ingestion import DocumentIdentityBuilder, GraphEntityType
from harborrag_runtime.ingestion import DocumentReleaseService, SourceIngestionService

from ...fixtures.connectors import LinkedDocumentsConnector
from ...fixtures.release import (
    ReleaseResources,
    build_control_plane,
    build_dependencies,
    build_relation_repair_service,
    build_release_resources,
    source_request,
)


class _OneDocumentConnector(LinkedDocumentsConnector):
    """Yield a single document of the linked pair, so each lands in its own scope."""

    def __init__(self, keep: str) -> None:
        super().__init__()
        self._keep = keep

    def discover(self, query):
        yield from (record for record in super().discover(query) if record.id == self._keep)


@pytest.mark.asyncio
async def test_repair_resolves_a_target_published_under_a_sibling_scope(
    tmp_path: Path,
) -> None:
    """A link into another scope of the same connection has to reach the real node.

    Scopes are opaque digests of the connector query, so a relation target carries no
    hint of the scope that owns it and the projection can only stamp the linking
    document's own scope on its placeholder. Resolution was scope-limited too, so a
    cross-scope target stayed unresolved forever and the edge was pinned to a stub keyed
    under the wrong scope -- a node the target's own projection never writes.
    """

    control = build_control_plane(tmp_path)
    resources = build_release_resources(control)
    async with control, resources.store:
        dependencies = build_dependencies(resources)
        source_service = SourceIngestionService(
            control=control,
            documents=DocumentReleaseService(dependencies),
            relations=build_relation_repair_service(resources, dependencies),
        )

        # The target publishes first, under "archive"; the linking document follows in
        # "docs" and its repair pass runs at the end of that batch.
        await source_service.ingest(
            replace(source_request("task-target"), source_scope_id="archive"),
            _OneDocumentConnector("docs/b.txt"),
        )
        outcome = await source_service.ingest(
            source_request("task-linker"),
            _OneDocumentConnector("docs/a.txt"),
        )

        assert outcome.unresolved_relations == 0
        linker = _key("docs", "docs/a.txt")
        endpoints = {
            (relation.source_node_key, relation.target_node_key) for relation in _links(resources)
        }
        # The edge reaches the target's real node under "archive".
        assert (linker, _key("archive", "docs/b.txt")) in endpoints
        # The first projection's stub, keyed under the linking document's own scope, is
        # still there: repair adds the resolved edge but has no way to retract the one it
        # supersedes. `KnowledgeGraphRepositoryPort` writes relations by MERGE and deletes
        # only whole versions, source items and scopes -- retracting one superseded edge
        # needs a relation-scoped primitive that does not exist yet. Pinned so the future
        # supersede pass that adds it has a failing assertion here.
        assert (linker, _key("docs", "docs/b.txt")) in endpoints


def _key(source_scope_id: str, source_item_id: str) -> str:
    return DocumentIdentityBuilder().source_entity_node_key(
        tenant_id="default",
        source_scope_id=source_scope_id,
        entity_type=GraphEntityType.LOCAL_FILE.value,
        provider_id=source_item_id,
    )


def _links(resources: ReleaseResources):
    return [
        relation
        for relation in resources.graph.relations.values()
        if relation.relation_type == RelationType.LINKS_TO
    ]
