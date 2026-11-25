"""Reading a local file changes its access time only; that is no change to ingest."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from harborrag_adapters.connectors import LocalFileConfig, LocalFileConnector
from harborrag_adapters.connectors.local import filesystem
from harborrag_core.domain.raw_document import RawDocument
from harborrag_core.domain.source import SourceRecord
from harborrag_engine.ingestion.release.service import DocumentReleaseService
from harborrag_runtime.ingestion import SourceIngestionService

from ...fixtures.release import (
    build_control_plane,
    build_dependencies,
    build_release_resources,
    source_request,
)

pytestmark = pytest.mark.unit

_NOTES = "notes.md"


class _CountingConnector(LocalFileConnector):
    """The local connector, counting the files it fetches."""

    def __init__(self, config: LocalFileConfig) -> None:
        super().__init__(config)
        self.loads = 0

    def load(self, record: SourceRecord) -> RawDocument:
        self.loads += 1
        return super().load(record)


def _source(tmp_path: Path) -> tuple[Path, Path]:
    root = tmp_path / "docs"
    root.mkdir()
    notes = root / _NOTES
    notes.write_text("# Demo notes\n\nThe worker timeout is 30 seconds.\n", encoding="utf-8")
    return root, notes


def _read_later(monkeypatch: pytest.MonkeyPatch, path: Path, *, seconds: float) -> None:
    """Make ``path`` look read ``seconds`` later: its access time moves, nothing else does."""
    original = Path.stat

    def stat(self: Path, *, follow_symlinks: bool = True) -> os.stat_result:
        result = original(self, follow_symlinks=follow_symlinks)
        if self != path:
            return result
        accessed = result.st_atime + seconds
        fields = list(result[:10])
        fields[7] = int(accessed)
        return os.stat_result(
            fields,
            {
                "st_atime": accessed,
                "st_mtime": result.st_mtime,
                "st_ctime": result.st_ctime,
                "st_atime_ns": result.st_atime_ns + int(seconds * 1_000_000_000),
                "st_mtime_ns": result.st_mtime_ns,
                "st_ctime_ns": result.st_ctime_ns,
            },
        )

    monkeypatch.setattr(Path, "stat", stat)


@pytest.mark.asyncio
async def test_an_access_time_only_change_is_unchanged_and_not_fetched(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root, notes = _source(tmp_path)
    control = build_control_plane(tmp_path)
    resources = build_release_resources(control)
    connector = _CountingConnector(LocalFileConfig(source_path=str(root)))
    async with control, resources.store:
        documents = DocumentReleaseService(build_dependencies(resources))
        await documents.provision(tenant_id="default")
        service = SourceIngestionService(control=control, documents=documents)
        first = await service.ingest(source_request("task-1"), connector)
        _read_later(monkeypatch, notes, seconds=3600)

        second = await service.ingest(source_request("task-2"), connector)

    assert (first.published, first.unchanged) == (1, 0)
    assert (second.published, second.unchanged) == (0, 1)
    assert connector.loads == 1
    assert resources.parser.calls == 1


@pytest.mark.asyncio
async def test_a_descriptor_stored_with_an_access_time_is_refetched_once_without_a_new_version(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The upgrade path: descriptors stored before carry ``accessed_at``."""
    root, notes = _source(tmp_path)
    control = build_control_plane(tmp_path)
    resources = build_release_resources(control)
    connector = _CountingConnector(LocalFileConfig(source_path=str(root)))
    current_record = filesystem.build_source_record

    def record_with_access_time(
        path: Path, *, root_path: Path, checksum: str | None, is_symlink: bool
    ) -> SourceRecord:
        record = current_record(path, root_path=root_path, checksum=checksum, is_symlink=is_symlink)
        record.metadata["accessed_at"] = "2026-11-01T00:00:00+00:00"
        return record

    async with control, resources.store:
        documents = DocumentReleaseService(build_dependencies(resources))
        await documents.provision(tenant_id="default")
        service = SourceIngestionService(control=control, documents=documents)
        with monkeypatch.context() as earlier_release:
            earlier_release.setattr(filesystem, "build_source_record", record_with_access_time)
            await service.ingest(source_request("task-1"), connector)
        stored = await control.source_scans.source_item(
            source_scope_id="docs", source_item_id=_NOTES
        )
        assert stored is not None
        active_before = await control.document_versions.active_snapshot(stored.document_id)

        upgraded = await service.ingest(source_request("task-2"), connector)
        active_after = await control.document_versions.active_snapshot(stored.document_id)
        _read_later(monkeypatch, notes, seconds=3600)
        settled = await service.ingest(source_request("task-3"), connector)

    assert (upgraded.published, upgraded.unchanged) == (0, 1)
    assert (settled.published, settled.unchanged) == (0, 1)
    assert active_before is not None
    assert active_after is not None
    assert active_after.document_version_id == active_before.document_version_id
    assert connector.loads == 2
