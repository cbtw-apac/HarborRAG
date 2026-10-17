from __future__ import annotations

from datetime import timedelta
from pathlib import Path

import pytest

from harborrag_adapters.repositories.database.ingestion_control import (
    IngestionControlPlaneDatabase,
)
from harborrag_core.base import utc_now
from harborrag_core.chunking import ConnectorType
from harborrag_core.ingestion import SourceCatalogQuery, SourceIdentity
from harborrag_core.ports.access import ConnectionScopeReader
from harborrag_core.security import AccessContext
from harborrag_core.topology.permissions import ResolvedPermissionSnapshot

from .ingestion_control_fixtures import (
    advance_to_verified,
    candidate,
    make_control_plane,
    publish_over_active,
    source_identity,
)

ENGINEERING = "scope-engineering"
HR = "scope-hr"


def hr_identity() -> SourceIdentity:
    return SourceIdentity(
        tenant_id="DEFAULT",
        connector_type=ConnectorType.CONFLUENCE,
        connection_id="wiki.hr",
        source_item_id="page-hr",
        source_scope_id=HR,
    )


async def publish_both(control: IngestionControlPlaneDatabase) -> tuple[str, str]:
    """Publish one document in each datasource and return their ids, engineering first."""

    document_ids: list[str] = []
    for source in (source_identity("page-eng"), hr_identity()):
        version = candidate(f"content of {source.source_item_id}", source=source)
        await advance_to_verified(control, version)
        await publish_over_active(
            control, str(version.document_id), str(version.document_version_id)
        )
        document_ids.append(str(version.document_id))
    return document_ids[0], document_ids[1]


async def grant_reader(control: IngestionControlPlaneDatabase, *resources: tuple[str, str]) -> None:
    """Let ``reader`` read each ``(kind, resource_id)`` through a current, non-public snapshot."""

    now = utc_now()
    for kind, resource_id in resources:
        await control.topology.set_permissions(
            ResolvedPermissionSnapshot(
                tenant_id="DEFAULT",
                resource_kind=kind,
                resource_id=resource_id,
                revision="acl-1",
                resolved_at=now,
                expires_at=now + timedelta(hours=1),
                known=True,
                processing_allowed=True,
                public=False,
                allowed_principal_ids=("reader",),
            )
        )


def shared_access(entitled: frozenset[str] | None) -> AccessContext:
    return AccessContext(
        principal_id="reader",
        tenant_id="DEFAULT",
        corpus_mode="tenant_shared",
        entitled_source_scopes=entitled,
    )


def acl_access(entitled: frozenset[str] | None) -> AccessContext:
    return AccessContext(
        principal_id="reader", tenant_id="DEFAULT", entitled_source_scopes=entitled
    )


@pytest.mark.asyncio
async def test_authorized_documents_keep_only_entitled_datasources(tmp_path: Path) -> None:
    async with make_control_plane(tmp_path) as control:
        eng, hr = await publish_both(control)
        shared = shared_access(frozenset({ENGINEERING}))

        assert await control.topology.authorized_document_ids(
            "DEFAULT", (eng, hr), access=shared
        ) == {eng}
        assert await control.topology.allowed_document_ids("DEFAULT", access=shared) == (eng,)


@pytest.mark.asyncio
async def test_scope_lists_keep_only_entitled_datasources(tmp_path: Path) -> None:
    async with make_control_plane(tmp_path) as control:
        await publish_both(control)
        shared = shared_access(frozenset({ENGINEERING}))

        assert await control.topology.allowed_source_scope_ids("DEFAULT", access=shared) == (
            ENGINEERING,
        )
        assert await control.topology.authorized_source_scope_ids(
            "DEFAULT", (ENGINEERING, HR), access=shared
        ) == {ENGINEERING}


@pytest.mark.asyncio
async def test_shared_document_page_keeps_only_entitled_datasources(tmp_path: Path) -> None:
    async with make_control_plane(tmp_path) as control:
        eng, _ = await publish_both(control)
        shared = shared_access(frozenset({ENGINEERING}))

        assert await control.topology.published_document_page(
            "DEFAULT", access=shared, after="", limit=10
        ) == (eng,)


@pytest.mark.asyncio
async def test_readable_sources_keep_only_entitled_datasources(tmp_path: Path) -> None:
    async with make_control_plane(tmp_path) as control:
        await publish_both(control)
        shared = shared_access(frozenset({ENGINEERING}))

        sources = await control.source_scans.list_readable_sources(
            SourceCatalogQuery(tenant_id="DEFAULT", access=shared, limit=10)
        )

        assert [source.source_scope_id for source in sources] == [ENGINEERING]


@pytest.mark.asyncio
async def test_source_acl_mode_also_applies_entitlements(tmp_path: Path) -> None:
    async with make_control_plane(tmp_path) as control:
        eng, hr = await publish_both(control)
        await grant_reader(
            control, ("source", ENGINEERING), ("source", HR), ("document", eng), ("document", hr)
        )
        hr_only = acl_access(frozenset({HR}))

        assert await control.topology.authorized_document_ids(
            "DEFAULT", (eng, hr), access=hr_only
        ) == {hr}
        assert await control.topology.allowed_document_ids("DEFAULT", access=hr_only) == (hr,)
        assert await control.topology.allowed_source_scope_ids("DEFAULT", access=hr_only) == (HR,)
        assert await control.topology.authorized_source_scope_ids(
            "DEFAULT", (ENGINEERING, HR), access=hr_only
        ) == {HR}


@pytest.mark.asyncio
async def test_source_acl_readable_sources_keep_only_entitled_datasources(
    tmp_path: Path,
) -> None:
    async with make_control_plane(tmp_path) as control:
        eng, hr = await publish_both(control)
        # Publication does not write source_scopes; the snapshot branch lists from it.
        for scope, connection in ((ENGINEERING, "wiki.example"), (HR, "wiki.hr")):
            await control.source_scans.register_scope(
                source_scope_id=scope,
                connector_type="confluence",
                connection_id=connection,
                configuration_fingerprint="fingerprint",
            )
        await grant_reader(
            control, ("source", ENGINEERING), ("source", HR), ("document", eng), ("document", hr)
        )

        async def listed(entitled: frozenset[str] | None) -> list[str]:
            sources = await control.source_scans.list_readable_sources(
                SourceCatalogQuery(tenant_id="DEFAULT", access=acl_access(entitled), limit=10)
            )
            return [source.source_scope_id for source in sources]

        assert await listed(None) == [ENGINEERING, HR]
        assert await listed(frozenset({HR})) == [HR]
        assert await listed(frozenset()) == []


@pytest.mark.asyncio
async def test_no_entitlement_set_changes_nothing(tmp_path: Path) -> None:
    async with make_control_plane(tmp_path) as control:
        eng, hr = await publish_both(control)
        shared = shared_access(None)

        assert await control.topology.authorized_document_ids(
            "DEFAULT", (eng, hr), access=shared
        ) == {eng, hr}
        assert await control.topology.allowed_document_ids("DEFAULT", access=shared) == tuple(
            sorted((eng, hr))
        )
        assert await control.topology.allowed_source_scope_ids("DEFAULT", access=shared) == (
            ENGINEERING,
            HR,
        )
        assert await control.topology.authorized_source_scope_ids(
            "DEFAULT", (ENGINEERING, HR), access=shared
        ) == {ENGINEERING, HR}
        assert await control.topology.published_document_page(
            "DEFAULT", access=shared, after="", limit=10
        ) == tuple(sorted((eng, hr)))
        sources = await control.source_scans.list_readable_sources(
            SourceCatalogQuery(tenant_id="DEFAULT", access=shared, limit=10)
        )
        assert [source.source_scope_id for source in sources] == [ENGINEERING, HR]


@pytest.mark.asyncio
async def test_empty_entitlement_reads_nothing(tmp_path: Path) -> None:
    async with make_control_plane(tmp_path) as control:
        eng, hr = await publish_both(control)
        await grant_reader(
            control, ("source", ENGINEERING), ("source", HR), ("document", eng), ("document", hr)
        )
        for access in (shared_access(frozenset()), acl_access(frozenset())):
            assert (
                await control.topology.authorized_document_ids("DEFAULT", (eng, hr), access=access)
                == set()
            )
            assert await control.topology.allowed_document_ids("DEFAULT", access=access) == ()
            assert await control.topology.allowed_source_scope_ids("DEFAULT", access=access) == ()
            assert (
                await control.topology.authorized_source_scope_ids(
                    "DEFAULT", (ENGINEERING, HR), access=access
                )
                == set()
            )
        assert (
            await control.topology.published_document_page(
                "DEFAULT", access=shared_access(frozenset()), after="", limit=10
            )
            == ()
        )
        assert (
            await control.source_scans.list_readable_sources(
                SourceCatalogQuery(tenant_id="DEFAULT", access=shared_access(frozenset()), limit=10)
            )
            == ()
        )


@pytest.mark.asyncio
async def test_connection_lookup(tmp_path: Path) -> None:
    """The lookup reads ``source_scopes``, which only scope registration writes."""

    async with make_control_plane(tmp_path) as control:
        scans: ConnectionScopeReader = control.source_scans
        registrar = control.source_scans
        for tenant, scope, connection in (
            ("DEFAULT", ENGINEERING, "wiki.example"),
            ("DEFAULT", HR, "wiki.hr"),
            ("OTHER", "scope-other-tenant", "wiki.other"),
        ):
            await registrar.register_scope(
                tenant_id=tenant,
                source_scope_id=scope,
                connector_type="confluence",
                connection_id=connection,
                configuration_fingerprint="fingerprint",
            )

        assert await scans.source_scope_ids_by_connection(
            "DEFAULT", ["wiki.example", "wiki.hr", "missing", "wiki.other"]
        ) == {"wiki.example": frozenset({ENGINEERING}), "wiki.hr": frozenset({HR})}
        assert await scans.source_scope_ids_by_connection("DEFAULT", []) == {}
        # Another tenant's rows never appear, and its own are found under its own id.
        assert (
            await scans.source_scope_ids_by_connection(
                "OTHER", ["wiki.example", "wiki.hr", "missing"]
            )
            == {}
        )
        assert await scans.source_scope_ids_by_connection("OTHER", ["wiki.other"]) == {
            "wiki.other": frozenset({"scope-other-tenant"})
        }

        await registrar.register_scope(
            source_scope_id="scope-engineering-archive",
            connector_type="confluence",
            connection_id="wiki.example",
            configuration_fingerprint="fingerprint",
        )

        assert await scans.source_scope_ids_by_connection("DEFAULT", ["wiki.example"]) == {
            "wiki.example": frozenset({ENGINEERING, "scope-engineering-archive"})
        }
