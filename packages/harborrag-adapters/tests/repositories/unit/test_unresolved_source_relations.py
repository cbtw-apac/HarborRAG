"""Unresolved source links are remembered so a later run can re-repair their declarers."""

from __future__ import annotations

from pathlib import Path

import pytest
from sqlalchemy import insert

from harborrag_adapters.repositories.database.ingestion_control import (
    IngestionControlPlaneDatabase,
    UnresolvedSourceRelation,
)
from harborrag_adapters.repositories.database.ingestion_control.schema import (
    DOCUMENTS,
    SOURCE_ITEMS,
    SOURCE_SCOPES,
)
from harborrag_core.base import utc_now

from .ingestion_control_fixtures import make_control_plane


async def _document(  # noqa: PLR0913
    control: IngestionControlPlaneDatabase,
    document_id: str,
    source_item_id: str,
    *,
    scope: str = "scope-a",
    connection_id: str = "jira-main",
    active: bool = True,
    item_active: bool = True,
) -> None:
    now = utc_now()
    async with control._client.sessions.begin() as session:
        if not (
            await session.execute(
                SOURCE_SCOPES.select().where(SOURCE_SCOPES.c.source_scope_id == scope)
            )
        ).first():
            await session.execute(
                insert(SOURCE_SCOPES).values(
                    source_scope_id=scope,
                    tenant_id="tenant-1",
                    connector_type="jira",
                    connection_id=connection_id,
                    configuration_fingerprint="fingerprint",
                    created_at=now,
                    updated_at=now,
                )
            )
        await session.execute(
            insert(DOCUMENTS).values(
                document_id=document_id,
                tenant_id="tenant-1",
                source_scope_id=scope,
                connector_type="jira",
                connection_id=connection_id,
                source_item_id=source_item_id,
                active_document_version_id=f"{document_id}-v1" if active else None,
                created_at=now,
                updated_at=now,
            )
        )
        await session.execute(
            insert(SOURCE_ITEMS).values(
                source_scope_id=scope,
                source_item_id=source_item_id,
                document_id=document_id,
                source_version="1",
                binding_kind="ROOT",
                admission_change_key="admission",
                last_seen_scan_sequence=1,
                is_active=item_active,
                descriptor={},
                updated_at=now,
            )
        )


def _link(target: str, predicate: str = "relates_to") -> UnresolvedSourceRelation:
    return UnresolvedSourceRelation(
        target_source_item_id=target,
        target_connector_type="jira",
        predicate=predicate,
        relation_type=predicate,
    )


async def _remember(
    control: IngestionControlPlaneDatabase,
    document_id: str,
    *links: UnresolvedSourceRelation,
    connection_id: str = "jira-main",
) -> None:
    await control.unresolved_relations.replace_unresolved(
        tenant_id="tenant-1",
        declaring_document_id=document_id,
        declaring_document_version_id=f"{document_id}-v1",
        connector_type="jira",
        connection_id=connection_id,
        relations=links,
    )


@pytest.mark.asyncio
async def test_replacement_reconciles_the_whole_set_including_an_empty_one(
    tmp_path: Path,
) -> None:
    async with make_control_plane(tmp_path) as control:
        await _document(control, "declarer", "jira://ENG/ENG-1")
        await _remember(
            control,
            "declarer",
            _link("jira://RHR/RHR-1"),
            _link("jira://RHR/RHR-2", "is_blocked_by"),
            # The same link declared twice is one row.
            _link("jira://RHR/RHR-1"),
        )
        rows = await control.unresolved_relations.unresolved_for("declarer")
        assert [(row.target_source_item_id, row.predicate) for row in rows] == [
            ("jira://RHR/RHR-1", "relates_to"),
            ("jira://RHR/RHR-2", "is_blocked_by"),
        ]

        await _remember(control, "declarer", _link("jira://RHR/RHR-2", "is_blocked_by"))
        assert len(await control.unresolved_relations.unresolved_for("declarer")) == 1

        await _remember(control, "declarer")
        assert await control.unresolved_relations.unresolved_for("declarer") == ()


@pytest.mark.asyncio
async def test_declarers_become_resolvable_only_once_their_target_is_published(
    tmp_path: Path,
) -> None:
    async with make_control_plane(tmp_path) as control:
        await _document(control, "declarer", "jira://ENG/ENG-1")
        await _document(control, "inactive-declarer", "jira://ENG/ENG-9", active=False)
        await _remember(control, "declarer", _link("jira://RHR/RHR-1"))
        await _remember(control, "inactive-declarer", _link("jira://RHR/RHR-1"))
        resolvable = control.unresolved_relations.resolvable_declaring_documents

        assert await resolvable(tenant_id="tenant-1") == ()

        # Published in another scope of the same connection: that resolves.
        await _document(control, "target", "jira://RHR/RHR-1", scope="scope-b")

        # An inactive declarer has nothing for repair to rebuild.
        assert await resolvable(tenant_id="tenant-1") == ("declarer",)
        assert await resolvable(tenant_id="tenant-1", after="declarer") == ()
        assert await resolvable(tenant_id="other-tenant") == ()


@pytest.mark.asyncio
async def test_a_same_connector_target_in_another_connection_does_not_resolve(
    tmp_path: Path,
) -> None:
    async with make_control_plane(tmp_path) as control:
        await _document(control, "declarer", "jira://ENG/ENG-1")
        await _remember(control, "declarer", _link("jira://RHR/RHR-1"))
        await _document(
            control,
            "elsewhere",
            "jira://RHR/RHR-1",
            scope="scope-other-site",
            connection_id="jira-other-site",
        )

        assert (
            await control.unresolved_relations.resolvable_declaring_documents(tenant_id="tenant-1")
            == ()
        )


@pytest.mark.asyncio
async def test_a_deleted_declarer_is_never_offered_for_repair(tmp_path: Path) -> None:
    async with make_control_plane(tmp_path) as control:
        await _document(control, "declarer", "jira://ENG/ENG-1")
        await _remember(control, "declarer", _link("jira://RHR/RHR-1"))
        async with control._client.sessions.begin() as session:
            await session.execute(DOCUMENTS.delete().where(DOCUMENTS.c.document_id == "declarer"))

        # Postgres cascades the row away with its document (ON DELETE CASCADE); SQLite
        # only does with foreign keys on, so assert the effect that matters either way.
        assert (
            await control.unresolved_relations.resolvable_declaring_documents(tenant_id="tenant-1")
            == ()
        )
