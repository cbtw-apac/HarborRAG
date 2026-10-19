from __future__ import annotations

from datetime import timedelta
from pathlib import Path

import pytest

from harborrag_core.base import utc_now
from harborrag_core.ingestion import SourceCatalogQuery
from harborrag_core.security import AccessContext
from harborrag_core.summaries import SummaryFacet, SummaryPolicy
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


@pytest.mark.asyncio
async def test_source_catalog_lists_the_entity_facets_the_applied_summary_policy_declares(
    tmp_path: Path,
) -> None:
    """find_entities takes facets by name; this is where a caller learns them."""

    async with make_control_plane(tmp_path) as control:
        for scope in ("faceted-source", "plain-source"):
            await control.source_scans.register_scope(
                source_scope_id=scope,
                connector_type="jira",
                connection_id="connection",
                configuration_fingerprint="fingerprint",
            )
        now = utc_now()
        for scope in ("faceted-source", "plain-source"):
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
                    public=True,
                )
            )
        await control.summaries.configure(
            "DEFAULT",
            "faceted-source",
            SummaryPolicy(
                model_fingerprint="model-v1",
                facets=(
                    SummaryFacet(name="skill_set", field="Skill Set"),
                    SummaryFacet(name="years", field="Years of experience", kind="integer"),
                ),
            ),
        )
        await control.summaries.configure("DEFAULT", "plain-source", None)

        sources = await control.source_scans.list_readable_sources(
            SourceCatalogQuery(
                tenant_id="DEFAULT",
                access=AccessContext(tenant_id="DEFAULT", principal_id="reader"),
            )
        )

    by_id = {item.source_scope_id: item for item in sources}
    faceted = by_id["faceted-source"]
    assert [(facet.name, facet.type, facet.field) for facet in faceted.entity_facets] == [
        ("skill_set", "text", "Skill Set"),
        ("years", "integer", "Years of experience"),
    ]
    assert faceted.entity_summaries == "queued"
    # No policy: its entities get no cards, so find_entities cannot reach them.
    assert by_id["plain-source"].entity_facets == ()
    assert by_id["plain-source"].entity_summaries == "disabled"
