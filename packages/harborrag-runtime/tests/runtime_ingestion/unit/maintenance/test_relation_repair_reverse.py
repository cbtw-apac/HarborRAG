"""A link to something not yet ingested heals when a later run publishes the target."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest

from harborrag_runtime.ingestion import DocumentReleaseService, SourceIngestionService

from ...fixtures.release import (
    build_control_plane,
    build_dependencies,
    build_relation_repair_service,
    build_release_resources,
    source_request,
)
from .test_relation_repair_scopes import _key, _links, _OneDocumentConnector


@pytest.mark.asyncio
async def test_a_later_run_repoints_an_earlier_documents_stub_link(tmp_path: Path) -> None:
    """The reverse trigger: the declarer is outside the run that publishes its target.

    Links are resolved when the *declaring* document's source runs. A target another
    scope ingests afterwards used to leave the link unresolved for good; now the run
    that publishes it re-repairs the declarers its persisted unresolved rows name.
    """

    control = build_control_plane(tmp_path)
    resources = build_release_resources(control)
    async with control, resources.store:
        dependencies = build_dependencies(resources)
        service = SourceIngestionService(
            control=control,
            documents=DocumentReleaseService(dependencies),
            relations=build_relation_repair_service(resources, dependencies),
        )

        linker_outcome = await service.ingest(
            source_request("task-linker"), _OneDocumentConnector("docs/a.txt")
        )
        linker = _key("docs", "docs/a.txt")
        [stub_link] = _links(resources)
        stub = resources.graph.nodes[stub_link.target_node_key]
        # The edge to the stub is owned by the declaring document's version.
        declarer = str(stub_link.document_id)

        assert linker_outcome.unresolved_relations == 1
        assert stub_link.source_node_key == linker
        assert stub.attributes == {"placeholder": True, "external": True}
        assert [
            row.target_source_item_id
            for row in await control.unresolved_relations.unresolved_for(declarer)
        ] == ["docs/b.txt"]

        # The target arrives in another scope, in a run that does not plan docs/a.txt.
        await service.ingest(
            replace(source_request("task-target"), source_scope_id="archive"),
            _OneDocumentConnector("docs/b.txt"),
        )

        endpoints = {(link.source_node_key, link.target_node_key) for link in _links(resources)}
        assert endpoints == {(linker, _key("archive", "docs/b.txt"))}
        # The stub is orphaned and pruned, and nothing is left to re-repair.
        assert stub.node_key not in resources.graph.nodes
        assert not [
            node for node in resources.graph.nodes.values() if node.attributes.get("external")
        ]
        assert await control.unresolved_relations.unresolved_for(declarer) == ()
