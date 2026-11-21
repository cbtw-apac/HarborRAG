"""Relation repair selects only the requested tenant's documents."""

from __future__ import annotations

from pathlib import Path

import pytest

from harborrag_adapters.repositories.database.ingestion_control import (
    IngestionControlPlaneDatabase,
)
from harborrag_core.chunking import ConnectorType
from harborrag_core.ingestion import DiscoveredSourceItem, SourceIdentity

from .ingestion_control_fixtures import advance_to_verified, candidate, make_control_plane

pytestmark = pytest.mark.unit


async def _publish_for(control_plane: IngestionControlPlaneDatabase, tenant_id: str) -> str:
    scope = f"scope-{tenant_id}"
    source = SourceIdentity(
        tenant_id=tenant_id,
        connector_type=ConnectorType.CONFLUENCE,
        connection_id="wiki.example",
        source_item_id="page-1",
        source_scope_id=scope,
    )
    version = candidate("release guide body", source=source)
    scans = control_plane.source_scans
    await scans.register_scope(
        tenant_id=tenant_id,
        source_scope_id=scope,
        connector_type="confluence",
        connection_id="wiki.example",
        configuration_fingerprint="config-v1",
    )
    scan_id = await scans.start(scope)
    await scans.record_seen(
        scan_id=scan_id,
        item=DiscoveredSourceItem(
            source_identity=source,
            document_id=version.document_id,
            source_version="1",
            admission_change_key=version.fingerprints.admission_change_key,
        ),
    )
    await scans.complete(scan_id)
    await advance_to_verified(control_plane, version)
    await control_plane.publisher.publish(
        document_id=str(version.document_id),
        candidate_document_version_id=str(version.document_version_id),
    )
    return str(version.document_id)


@pytest.mark.asyncio
async def test_active_relation_document_ids_with_a_tenant_never_returns_another_tenants_document(
    tmp_path: Path,
) -> None:
    control_plane = make_control_plane(tmp_path)
    async with control_plane:
        documents = {
            tenant: await _publish_for(control_plane, tenant) for tenant in ("acme", "globex")
        }
        fingerprint = candidate("release guide body").fingerprints.processing_fingerprint
        versions = control_plane.document_versions

        selected = {
            tenant: await versions.active_relation_document_ids(
                tenant_id=tenant, processing_fingerprint=fingerprint, limit=1
            )
            for tenant in documents
        }

    assert selected == {tenant: (document_id,) for tenant, document_id in documents.items()}


@pytest.mark.asyncio
async def test_active_relation_document_ids_without_a_tenant_is_refused(tmp_path: Path) -> None:
    control_plane = make_control_plane(tmp_path)
    async with control_plane:
        with pytest.raises(ValueError, match="tenant_id"):
            await control_plane.document_versions.active_relation_document_ids(
                processing_fingerprint="fingerprint"
            )
