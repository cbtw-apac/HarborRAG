"""mark_verified must not overwrite a status another writer set after its read."""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import event

from harborrag_adapters.repositories.database.ingestion_control import (
    IngestionControlPlaneDatabase,
)
from harborrag_adapters.repositories.database.sqlite.client import SQLiteDBClient
from harborrag_core.contracts import HarborConflictError
from harborrag_core.ingestion import DocumentVersionState, ProjectionManifest

from .ingestion_control_fixtures import candidate

pytestmark = pytest.mark.unit

_STAGES = (
    DocumentVersionState.RAW_CAPTURED,
    DocumentVersionState.CANONICAL_READY,
    DocumentVersionState.CHUNKS_READY,
    DocumentVersionState.REPRESENTATIONS_READY,
    DocumentVersionState.PROJECTIONS_STAGED,
)


def _fail_after_status_read(client: SQLiteDBClient, database: Path, version_id: str) -> None:
    """Commit a move to FAILED from another connection right after the status read."""
    fired: list[bool] = []

    def concurrent_failure(*args: Any) -> None:
        statement = str(args[2])
        if fired or not statement.lstrip().startswith("SELECT document_versions.status"):
            return
        fired.append(True)
        other = sqlite3.connect(database, timeout=5)
        try:
            other.execute(
                "UPDATE document_versions SET status = ? WHERE document_version_id = ?",
                (DocumentVersionState.FAILED.value, version_id),
            )
            other.commit()
        finally:
            other.close()

    event.listen(client.raw.sync_engine, "after_cursor_execute", concurrent_failure)


@pytest.mark.asyncio
async def test_mark_verified_after_a_concurrent_failure_keeps_failed(tmp_path: Path) -> None:
    database = tmp_path / "control-plane.db"
    client = SQLiteDBClient(database=str(database))
    control_plane = IngestionControlPlaneDatabase(client, create_schema=True)
    version = candidate("release guide body")
    version_id = str(version.document_version_id)
    async with control_plane:
        versions = control_plane.document_versions
        await versions.create_candidate(version)
        for state in _STAGES:
            await versions.transition(version_id, state)
        await versions.save_projection_manifest(
            ProjectionManifest(
                document_id=version.document_id,
                document_version_id=version.document_version_id,
                route_point_ids=("route-1",),
                evidence_point_ids=("evidence-1",),
                graph_node_keys=("document-node",),
                chunk_ids=("route-chunk", "evidence-chunk"),
            )
        )
        _fail_after_status_read(client, database, version_id)

        with pytest.raises(HarborConflictError):
            await versions.mark_verified(version_id)

        snapshot = await versions.get_version(version_id)
        manifest = await control_plane.reliability.projection_manifest(version_id)
    assert snapshot is not None
    assert snapshot.state == DocumentVersionState.FAILED
    assert manifest is not None
