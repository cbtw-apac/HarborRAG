from __future__ import annotations

from datetime import timedelta
from pathlib import Path

import pytest

from harborrag_core.base import utc_now
from harborrag_core.ingestion import SourceCatalogQuery
from harborrag_core.security import AccessContext
from harborrag_core.topology.permissions import ResolvedPermissionSnapshot

from .ingestion_control_fixtures import make_control_plane


@pytest.mark.asyncio
async def test_source_catalog_returns_only_readable_scopes_without_connection_details(
    tmp_path: Path,
) -> None:
    async with make_control_plane(tmp_path) as control:
        for scope, connection in (("public-source", "secret-a"), ("private-source", "secret-b")):
            await control.source_scans.register_scope(
                source_scope_id=scope,
                connector_type="confluence",
                connection_id=connection,
                configuration_fingerprint="fingerprint",
            )
            scan = await control.source_scans.start(scope)
            await control.source_scans.complete(scan)
        now = utc_now()
        for scope, public in (("public-source", True), ("private-source", False)):
            await control.topology.set_permissions(
                ResolvedPermissionSnapshot(
                    tenant_id="DEFAULT",
                    resource_kind="source",
                    resource_id=scope,
                    revision="acl-1",
                    resolved_at=now,
                    expires_at=now + timedelta(hours=1),
                    known=True,
                    processing_allowed=True,
                    public=public,
                    allowed_principal_ids=("private-reader",) if not public else (),
                )
            )

        public = await control.source_scans.list_readable_sources(
            SourceCatalogQuery(
                tenant_id="DEFAULT",
                access=AccessContext(tenant_id="DEFAULT", principal_id="public-reader"),
            )
        )
        private = await control.source_scans.list_readable_sources(
            SourceCatalogQuery(
                tenant_id="DEFAULT",
                access=AccessContext(tenant_id="DEFAULT", principal_id="private-reader"),
            )
        )

    assert [item.source_scope_id for item in public] == ["public-source"]
    assert [item.source_scope_id for item in private] == ["private-source", "public-source"]
    assert public[0].ingestion_state == "COMPLETED"
    assert public[0].last_successful_source_check_at is not None
    assert "secret-a" not in public[0].model_dump_json()
