"""Source listings count only the documents the reader may read."""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path

import pytest

from harborrag_core.base import utc_now
from harborrag_core.ingestion import SourceCatalogQuery
from harborrag_core.security import AccessContext
from harborrag_core.topology.permissions import ResolvedPermissionSnapshot

from .ingestion_control_fixtures import (
    advance_to_verified,
    candidate,
    make_control_plane,
    source_identity,
)

pytestmark = pytest.mark.unit

TENANT = "DEFAULT"
SCOPE = "scope-engineering"


def _snapshot(kind: str, resource_id: str, *, public: bool) -> ResolvedPermissionSnapshot:
    now = utc_now()
    return ResolvedPermissionSnapshot(
        tenant_id=TENANT,
        resource_kind=kind,  # type: ignore[arg-type]
        resource_id=resource_id,
        revision="acl-1",
        resolved_at=now,
        expires_at=now + timedelta(hours=1),
        known=True,
        processing_allowed=True,
        public=public,
    )


@pytest.mark.asyncio
async def test_list_readable_sources_under_source_acl_counts_only_readable_documents(
    tmp_path: Path,
) -> None:
    async with make_control_plane(tmp_path) as control:
        await control.source_scans.register_scope(
            source_scope_id=SCOPE,
            connector_type="confluence",
            connection_id="wiki.example",
            configuration_fingerprint="fingerprint",
        )
        await control.topology.set_permissions(_snapshot("source", SCOPE, public=True))
        for item, readable in (("page-open", True), ("page-hidden", False)):
            version = candidate(f"{item} body", source=source_identity(item))
            await advance_to_verified(control, version)
            await control.publisher.publish(
                document_id=str(version.document_id),
                candidate_document_version_id=str(version.document_version_id),
            )
            await control.topology.set_permissions(
                _snapshot("document", str(version.document_id), public=readable)
            )

        strict = await control.source_scans.list_readable_sources(
            SourceCatalogQuery(
                tenant_id=TENANT, access=AccessContext(tenant_id=TENANT, principal_id="reader")
            )
        )
        shared = await control.source_scans.list_readable_sources(
            SourceCatalogQuery(
                tenant_id=TENANT,
                access=AccessContext(
                    tenant_id=TENANT, principal_id="reader", corpus_mode="tenant_shared"
                ),
            )
        )

    assert [(item.source_scope_id, item.active_document_count) for item in strict] == [(SCOPE, 1)]
    assert [(item.source_scope_id, item.active_document_count) for item in shared] == [(SCOPE, 2)]
    readable_at = strict[0].last_successful_ingestion_at
    newest_at = shared[0].last_successful_ingestion_at
    assert readable_at is not None
    assert newest_at is not None
    assert readable_at < newest_at
