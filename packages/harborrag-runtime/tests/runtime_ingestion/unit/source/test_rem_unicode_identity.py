"""Local file names that differ only in Unicode form stay separate documents."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from harborrag_adapters.connectors import LocalFileConfig, LocalFileConnector
from harborrag_core.chunking import ConnectorType
from harborrag_core.ingestion import (
    ChangeFingerprintBuilder,
    GraphEntityType,
    ProcessingProfile,
    ReindexJobState,
    SourceIdentity,
)
from harborrag_core.ingestion import identity as identity_module
from harborrag_core.ingestion.identity import identity_for_source, legacy_identity_for_source
from harborrag_engine.ingestion import VectorProjectionPolicy, VectorProjectionStore
from harborrag_engine.ingestion.maintenance.cleanup import ProjectionCleanupService
from harborrag_engine.ingestion.maintenance.reindex import DocumentReindexService, ReindexRequest
from harborrag_engine.ingestion.release.service import DocumentReleaseService
from harborrag_runtime.ingestion import SourceIngestionService

from ...fixtures.release import (
    ReleaseResources,
    build_control_plane,
    build_dependencies,
    build_release_resources,
    processing_profile,
    source_request,
)

pytestmark = pytest.mark.unit

_COMPOSED = "caf\u00e9.md"
_DECOMPOSED = "cafe\u0301.md"
_LOSSY_SPACING = re.compile(r"[^\S ]| {2}|\A | \Z")


def _source(source_item_id: str) -> SourceIdentity:
    return SourceIdentity(
        tenant_id="default",
        connector_type=ConnectorType.LOCAL,
        connection_id="local-docs",
        source_item_id=source_item_id,
        source_scope_id="docs",
    )


def _earlier_release(monkeypatch: pytest.MonkeyPatch) -> None:
    """Give identities only the exact-spacing entry, as 2.0.0a2 and later builds did."""
    monkeypatch.setattr(
        identity_module,
        "_normalization_changes",
        lambda value: bool(_LOSSY_SPACING.search(value)),
    )


def _reindexed_profile() -> ProcessingProfile:
    return processing_profile().model_copy(update={"graph_projection_version": "graph-v2"})


def _write(root: Path, name: str, text: str) -> None:
    (root / name).write_text(text, encoding="utf-8")


def _root(tmp_path: Path) -> Path:
    root = tmp_path / "docs"
    root.mkdir()
    return root


def _service(resources: ReleaseResources) -> SourceIngestionService:
    return SourceIngestionService(
        control=resources.control,
        documents=DocumentReleaseService(build_dependencies(resources)),
    )


def _file_node_keys(resources: ReleaseResources) -> set[str]:
    return {
        key
        for key, node in resources.graph.nodes.items()
        if node.entity_type == GraphEntityType.LOCAL_FILE
    }


def _version_linked_node_keys(resources: ReleaseResources) -> set[str]:
    """Return the nodes a relation owned by a document version links."""
    return {
        key
        for relation in resources.graph.relations.values()
        if relation.document_version_id is not None
        for key in (relation.source_node_key, relation.target_node_key)
    }


def _projected_versions(resources: ReleaseResources) -> set[str]:
    vector_versions = {
        str(point.payload.get("document_version_id"))
        for points in resources.vectors.points.values()
        for point in points.values()
    }
    graph_versions = {str(node.document_version_id) for node in resources.graph.nodes.values()}
    relation_versions = {
        str(relation.document_version_id) for relation in resources.graph.relations.values()
    }
    return vector_versions | graph_versions | relation_versions


@pytest.mark.asyncio
async def test_composed_and_decomposed_names_are_two_documents_and_two_file_nodes(
    tmp_path: Path,
) -> None:
    root = _root(tmp_path)
    _write(root, _COMPOSED, "# Demo menu\n\nEspresso is served until noon.\n")
    _write(root, _DECOMPOSED, "# Demo rota\n\nThe night shift starts at nine.\n")
    resources = build_release_resources(build_control_plane(tmp_path))
    connector = LocalFileConnector(LocalFileConfig(source_path=str(root)))
    async with resources.control, resources.store:
        service = _service(resources)
        first = await service.ingest(source_request("task-1"), connector)
        second = await service.ingest(source_request("task-2"), connector)
        composed = await resources.control.source_scans.source_item(
            source_scope_id="docs", source_item_id=_COMPOSED
        )
        decomposed = await resources.control.source_scans.source_item(
            source_scope_id="docs", source_item_id=_DECOMPOSED
        )

    assert (first.published, first.failed) == (2, 0)
    assert (second.published, second.unchanged, second.failed) == (0, 2, 0)
    assert composed is not None
    assert decomposed is not None
    assert composed.document_id == identity_for_source(_source(_COMPOSED))
    assert decomposed.document_id == identity_for_source(_source(_DECOMPOSED))
    assert composed.document_id != decomposed.document_id
    assert len(_file_node_keys(resources)) == 2
    assert resources.parser.calls == 2


@pytest.mark.asyncio
async def test_a_decomposed_name_published_under_its_collapsed_id_moves_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _root(tmp_path)
    _write(root, _DECOMPOSED, "# Demo rota\n\nThe night shift starts at nine.\n")
    resources = build_release_resources(build_control_plane(tmp_path))
    connector = LocalFileConnector(LocalFileConfig(source_path=str(root)))
    control = resources.control
    collapsed = legacy_identity_for_source(_source(_DECOMPOSED))
    exact = identity_for_source(_source(_DECOMPOSED))
    cleanup = ProjectionCleanupService(
        control=control,
        vector_store=VectorProjectionStore(resources.vectors, VectorProjectionPolicy(dimension=3)),
        graph_store=resources.graph,
    )
    async with control, resources.store:
        service = _service(resources)
        with monkeypatch.context() as earlier:
            _earlier_release(earlier)
            earlier_run = await service.ingest(source_request("task-1"), connector)
        old_version = await control.document_versions.active_snapshot(collapsed)
        assert old_version is not None
        old_file_nodes = _file_node_keys(resources)

        moved = await service.ingest(source_request("task-2"), connector)
        both_active = await control.document_versions.active_snapshot(collapsed)
        settled = await service.ingest(source_request("task-3"), connector)
        cleaned = await cleanup.run_scope(tenant_id="default", source_scope_id="docs")
        stored = await control.source_scans.source_item(
            source_scope_id="docs", source_item_id=_DECOMPOSED
        )
        retired = await control.document_versions.active_snapshot(collapsed)
        current = await control.document_versions.active_snapshot(exact)
        replay = await service.ingest(source_request("task-4"), connector)

    assert collapsed == identity_for_source(_source(_COMPOSED))
    assert collapsed != exact
    assert earlier_run.published == 1
    assert (moved.published, moved.failed, moved.removal_candidates) == (1, 0, ())
    assert both_active is not None
    assert (settled.published, settled.unchanged, settled.failed) == (0, 1, 0)
    assert settled.removal_candidates == (str(collapsed),)
    assert (cleaned.completed, cleaned.failed) == (1, 0)
    assert stored is not None
    assert stored.document_id == exact
    assert retired is None
    assert current is not None
    assert str(old_version.document_version_id) not in _projected_versions(resources)
    assert _version_linked_node_keys(resources).isdisjoint(old_file_nodes)
    assert len(_file_node_keys(resources) - old_file_nodes) == 1
    assert _file_node_keys(resources) - old_file_nodes <= _version_linked_node_keys(resources)
    assert (replay.published, replay.unchanged, replay.failed) == (0, 1, 0)
    assert replay.removal_candidates == ()
    assert resources.parser.calls == 2


@pytest.mark.asyncio
async def test_reindex_rebuilds_a_decomposed_name_under_its_exact_id(tmp_path: Path) -> None:
    root = _root(tmp_path)
    _write(root, _DECOMPOSED, "# Demo rota\n\nThe night shift starts at nine.\n")
    resources = build_release_resources(build_control_plane(tmp_path))
    connector = LocalFileConnector(LocalFileConfig(source_path=str(root)))
    exact = identity_for_source(_source(_DECOMPOSED))
    target = _reindexed_profile()
    async with resources.control, resources.store:
        await _service(resources).ingest(source_request("task-1"), connector)
        job = await DocumentReindexService(build_dependencies(resources)).run(
            ReindexRequest(reindex_job_id="reindex-1", tenant_id="default", processing=target)
        )
        active = await resources.control.document_versions.active_snapshot(exact)

    assert job.status == ReindexJobState.COMPLETED
    assert (job.scanned_count, job.published_count, job.failure_count) == (1, 1, 0)
    assert active is not None
    assert active.fingerprints.processing_fingerprint == (
        ChangeFingerprintBuilder().processing_fingerprint(profile=target)
    )


@pytest.mark.asyncio
async def test_reindex_skips_a_document_still_stored_under_its_collapsed_id(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The next scan replaces it, and a candidate under the collapsed ID is refused."""
    root = _root(tmp_path)
    _write(root, _DECOMPOSED, "# Demo rota\n\nThe night shift starts at nine.\n")
    resources = build_release_resources(build_control_plane(tmp_path))
    connector = LocalFileConnector(LocalFileConfig(source_path=str(root)))
    async with resources.control, resources.store:
        with monkeypatch.context() as earlier:
            _earlier_release(earlier)
            await _service(resources).ingest(source_request("task-1"), connector)
        job = await DocumentReindexService(build_dependencies(resources)).run(
            ReindexRequest(
                reindex_job_id="reindex-1", tenant_id="default", processing=_reindexed_profile()
            )
        )

    assert job.status == ReindexJobState.COMPLETED
    assert (job.scanned_count, job.skipped_count, job.failure_count) == (1, 1, 0)
