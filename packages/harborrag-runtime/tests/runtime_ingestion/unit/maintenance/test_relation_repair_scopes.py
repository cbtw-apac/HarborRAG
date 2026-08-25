"""Relation repair across the sibling scopes of one connection."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest

from harborrag_core.chunking import RelationType
from harborrag_core.domain.source import SourceRecord
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


class _SharedTargetConnector(LinkedDocumentsConnector):
    """Two linking documents in one scope, both pointing at the same absent target."""

    def discover(self, query):
        for record in super().discover(query):
            if record.id == "docs/b.txt":
                continue
            yield record
            yield SourceRecord(
                id="docs/c.txt",
                source_type=record.source_type,
                locator="file:///docs/c.txt",
                metadata={"relative_path": "docs/c.txt"},
            )

    def describe(self, record):
        record.metadata["relations"] = [
            {"predicate": "links_to", "target_id": "docs/b.txt", "target_type": "document"}
        ]
        return super().describe(record)


class _TwoLinkConnector(_OneDocumentConnector):
    """Link the kept document at a second target that never publishes."""

    def describe(self, record):
        descriptor = super().describe(record)
        record.metadata["relations"] = [
            *record.metadata.get("relations", ()),
            {
                "predicate": "links_to",
                "target_id": "docs/missing.txt",
                "target_type": "document",
            },
        ]
        return descriptor


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
        # And the stub it supersedes is gone. The first projection could only stamp the
        # linking document's own scope on a target it could not resolve, so that edge
        # points at a node no projection ever writes; left beside the resolved one, a
        # traversal returns the real target and a target that does not exist.
        assert (linker, _key("docs", "docs/b.txt")) not in endpoints
        # The stub node itself stays, deliberately. Deleting it would mean testing its
        # degree in one write and deleting it in another, and another document linking to
        # the same unresolved target may have staged that very node without its relation
        # yet -- so the retraction takes the edge only, and the tenant-wide prune on
        # version cleanup reaps the node. Nothing reads it meanwhile: every graph query
        # starts from a node and walks at least one relationship.
        assert _key("docs", "docs/b.txt") in resources.graph.nodes
        assert not [
            relation
            for relation in resources.graph.relations.values()
            if _key("docs", "docs/b.txt") in (relation.source_node_key, relation.target_node_key)
        ]


@pytest.mark.asyncio
async def test_repair_keeps_the_other_relations_of_a_repaired_document(
    tmp_path: Path,
) -> None:
    """Only the edge whose far end moved is retracted.

    A document holds more than one link, and the ones repair could not resolve still
    belong to it. Discriminating on the relation rather than on the target node key --
    "same type and source, different target, therefore superseded" -- marks every other
    link of the same document for deletion the moment one of them resolves, which on a
    real page is dozens of live edges lost per repair.

    The assertion is on what repair *asked* to retract, not only on the graph it left
    behind: the resolved projection is written straight after the retraction, so an
    over-broad delete of an edge repair is about to rewrite would be invisible in the
    final endpoints and would surface only once the two steps were ever reordered.
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

        await source_service.ingest(
            replace(source_request("task-target"), source_scope_id="archive"),
            _OneDocumentConnector("docs/b.txt"),
        )
        outcome = await source_service.ingest(
            source_request("task-linker"),
            _TwoLinkConnector("docs/a.txt"),
        )

        linker = _key("docs", "docs/a.txt")
        endpoints = {
            (relation.source_node_key, relation.target_node_key) for relation in _links(resources)
        }
        # docs/b.txt resolved into "archive" and its stub is retracted.
        assert (linker, _key("archive", "docs/b.txt")) in endpoints
        assert (linker, _key("docs", "docs/b.txt")) not in endpoints
        # docs/missing.txt never published, so its stub is all this link has -- it is
        # unresolved, not superseded, and it stays.
        assert (linker, _key("docs", "docs/missing.txt")) in endpoints
        assert outcome.unresolved_relations == 1
        retracted = {relation.target_node_key for relation in resources.graph.retracted_relations}
        assert retracted == {_key("docs", "docs/b.txt")}


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


@pytest.mark.asyncio
async def test_two_documents_sharing_one_unresolved_target_both_repair(
    tmp_path: Path,
) -> None:
    """Two linking documents, one shared placeholder, repaired in the same batch.

    This is the shape that makes a synchronous prune of the retracted far end unsafe:
    the placeholder both documents link through is one node, so the first repair to
    retract its edge sees it edgeless while the second is still writing. Repair retracts
    the edge only, so both documents must come out with the resolved edge and neither may
    fail verification.

    The fan-out is real -- ``_repair`` gathers over its targets -- but the in-memory graph
    writes nodes and relations inside one method, so this pins the observable contract
    rather than a true interleaving. What forbids the unsafe write structurally is
    ``test_relation_cleanup_deletes_relations_and_never_a_node``.
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

        await source_service.ingest(
            replace(source_request("task-target"), source_scope_id="archive"),
            _OneDocumentConnector("docs/b.txt"),
        )
        outcome = await source_service.ingest(
            source_request("task-linkers"),
            _SharedTargetConnector(),
        )

        assert outcome.unresolved_relations == 0
        endpoints = {
            (relation.source_node_key, relation.target_node_key) for relation in _links(resources)
        }
        resolved = _key("archive", "docs/b.txt")
        for linker in ("docs/a.txt", "docs/c.txt"):
            assert (_key("docs", linker), resolved) in endpoints
            assert (_key("docs", linker), _key("docs", "docs/b.txt")) not in endpoints
        # Both retractions name the one shared stub, and it survives them.
        assert {relation.target_node_key for relation in resources.graph.retracted_relations} == {
            _key("docs", "docs/b.txt")
        }
        assert _key("docs", "docs/b.txt") in resources.graph.nodes
