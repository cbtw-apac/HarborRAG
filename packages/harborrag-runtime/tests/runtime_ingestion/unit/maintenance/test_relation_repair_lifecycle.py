"""Repair replaces the declaring version's supports in either edge direction."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest

from harborrag_core.chunking import RelationType
from harborrag_runtime.ingestion import DocumentReleaseService, SourceIngestionService

from ...fixtures.release import (
    build_control_plane,
    build_dependencies,
    build_relation_repair_service,
    build_release_resources,
    source_request,
)
from .test_relation_repair_scopes import _key, _OneDocumentConnector


class LinkConnector(_OneDocumentConnector):
    def __init__(self, predicate: str) -> None:
        super().__init__("docs/a.txt")
        self.predicate = predicate

    def describe(self, record):
        descriptor = super().describe(record)
        record.metadata["relations"] = [
            {
                "predicate": self.predicate,
                "target_id": "docs/b.txt",
                "target_type": "document",
            }
        ]
        return descriptor


@pytest.mark.asyncio
@pytest.mark.parametrize("predicate", ["links_to", "is_blocked_by"])
async def test_repair_replaces_a_previously_resolved_endpoint_when_resolution_moves(
    tmp_path: Path, predicate: str
) -> None:
    control = build_control_plane(tmp_path)
    resources = build_release_resources(control)
    async with control, resources.store:
        dependencies = build_dependencies(resources)
        repair = build_relation_repair_service(resources, dependencies)
        service = SourceIngestionService(
            control=control,
            documents=DocumentReleaseService(dependencies),
            relations=repair,
        )
        await service.ingest(
            replace(source_request("target-original"), source_scope_id="archive"),
            _OneDocumentConnector("docs/b.txt"),
        )
        await service.ingest(source_request("link-original"), LinkConnector(predicate))
        discovery = await service.discover(source_request("link-repair"), LinkConnector(predicate))
        await service.ingest(
            replace(source_request("target-moved"), source_scope_id="moved", force_reprocess=True),
            _OneDocumentConnector("docs/b.txt"),
        )

        await repair.repair(discovery.planned, tenant_id="default")
        first_ids = set(resources.graph.relations)
        await repair.repair(discovery.planned, tenant_id="default")

        assert set(resources.graph.relations) == first_ids
        kind = RelationType.LINKS_TO if predicate == "links_to" else RelationType.BLOCKS
        links = [edge for edge in resources.graph.relations.values() if edge.relation_type == kind]
        assert len(links) == 1
        endpoints = {links[0].source_node_key, links[0].target_node_key}
        assert endpoints == {_key("docs", "docs/a.txt"), _key("moved", "docs/b.txt")}
        assert _key("archive", "docs/b.txt") not in endpoints
        if predicate == "is_blocked_by":
            assert links[0].source_node_key == _key("moved", "docs/b.txt")


@pytest.mark.asyncio
async def test_repair_retracts_resolved_link_when_target_loses_active_version(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    control = build_control_plane(tmp_path)
    resources = build_release_resources(control)
    async with control, resources.store:
        dependencies = build_dependencies(resources)
        repair = build_relation_repair_service(resources, dependencies)
        service = SourceIngestionService(
            control=control,
            documents=DocumentReleaseService(dependencies),
            relations=repair,
        )
        await service.ingest(
            replace(source_request("target-original"), source_scope_id="archive"),
            _OneDocumentConnector("docs/b.txt"),
        )
        await service.ingest(source_request("link-original"), LinkConnector("links_to"))
        discovery = await service.discover(source_request("link-repair"), LinkConnector("links_to"))

        # The control plane's resolver is the authority for target availability.
        async def missing_targets(**kwargs):
            del kwargs
            return {}

        monkeypatch.setattr(control.document_versions, "resolve_active_sources", missing_targets)
        result = await repair.repair(discovery.planned, tenant_id="default")

        assert result.unresolved_relations == 1
        assert not [
            edge
            for edge in resources.graph.relations.values()
            if edge.relation_type == RelationType.LINKS_TO
        ]
