"""Unresolved source links are remembered so a later run can re-repair their declarers."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import insert
from sqlalchemy.dialects import postgresql
from sqlalchemy.ext.asyncio import AsyncSession

from harborrag_adapters.repositories.database.ingestion_control import (
    IngestionControlPlaneDatabase,
    UnresolvedSourceRelation,
)
from harborrag_adapters.repositories.database.ingestion_control.schema import (
    DOCUMENTS,
    SOURCE_ITEMS,
    SOURCE_SCOPES,
)
from harborrag_adapters.repositories.database.ingestion_control.unresolved_relations import (
    _declarer_lock,
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
    connector_type: str = "jira",
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
                    connector_type=connector_type,
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
                connector_type=connector_type,
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
    connector_type: str = "jira",
) -> None:
    await control.unresolved_relations.replace_unresolved(
        tenant_id="tenant-1",
        declaring_document_id=document_id,
        declaring_document_version_id=f"{document_id}-v1",
        connector_type=connector_type,
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


async def _confluence_page_linking_to(control: IngestionControlPlaneDatabase, target: str) -> None:
    await _document(
        control,
        "page",
        "confluence://WIKI/page-1",
        scope="wiki-scope",
        connection_id="wiki",
        connector_type="confluence",
    )
    await _remember(
        control, "page", _link(target), connection_id="wiki", connector_type="confluence"
    )


@pytest.mark.asyncio
async def test_a_cross_connector_target_in_two_connections_is_never_offered(
    tmp_path: Path,
) -> None:
    # Repair declines a target two connections hold, so offering its declarer would
    # re-repair it on every run, to the same unresolved row, and starve later declarers.
    async with make_control_plane(tmp_path) as control:
        await _confluence_page_linking_to(control, "jira://ABC/ABC-1")
        await _document(control, "in-a", "jira://ABC/ABC-1", scope="jira-a", connection_id="a")
        await _document(control, "in-b", "jira://ABC/ABC-1", scope="jira-b", connection_id="b")

        assert (
            await control.unresolved_relations.resolvable_declaring_documents(tenant_id="tenant-1")
            == ()
        )


@pytest.mark.asyncio
async def test_a_cross_connector_target_in_one_connection_resolves_whatever_its_scope_rows(
    tmp_path: Path,
) -> None:
    async with make_control_plane(tmp_path) as control:
        await _confluence_page_linking_to(control, "jira://ABC/ABC-1")
        await _document(control, "target", "jira://ABC/ABC-1", scope="jira-a", connection_id="a")
        # The same document discovered by a second scope of its connection is still one
        # identity, so it stays unambiguous.
        async with control._client.sessions.begin() as session:
            await session.execute(
                insert(SOURCE_SCOPES).values(
                    source_scope_id="jira-a-2",
                    tenant_id="tenant-1",
                    connector_type="jira",
                    connection_id="a",
                    configuration_fingerprint="fingerprint",
                    created_at=utc_now(),
                    updated_at=utc_now(),
                )
            )
            await session.execute(
                insert(SOURCE_ITEMS).values(
                    source_scope_id="jira-a-2",
                    source_item_id="jira://ABC/ABC-1",
                    document_id="target",
                    source_version="1",
                    binding_kind="ROOT",
                    admission_change_key="admission",
                    last_seen_scan_sequence=1,
                    is_active=True,
                    descriptor={},
                    updated_at=utc_now(),
                )
            )
        # A copy in another connection whose source item is no longer active does not
        # count either: the resolver ignores it too.
        await _document(
            control,
            "stale-copy",
            "jira://ABC/ABC-1",
            scope="jira-b",
            connection_id="b",
            item_active=False,
        )

        assert await control.unresolved_relations.resolvable_declaring_documents(
            tenant_id="tenant-1"
        ) == ("page",)


@pytest.mark.asyncio
async def test_the_resolvable_query_agrees_with_the_unambiguous_resolver(tmp_path: Path) -> None:
    async with make_control_plane(tmp_path) as control:
        await _confluence_page_linking_to(control, "jira://ABC/ABC-1")
        resolvable = control.unresolved_relations.resolvable_declaring_documents

        async def resolved() -> set[str]:
            return set(
                await control.document_versions.resolve_unambiguous_active_sources(
                    tenant_id="tenant-1",
                    connector_type="jira",
                    source_item_ids=["jira://ABC/ABC-1"],
                )
            )

        await _document(control, "in-a", "jira://ABC/ABC-1", scope="jira-a", connection_id="a")
        assert (await resolvable(tenant_id="tenant-1"), await resolved()) == (
            ("page",),
            {"jira://ABC/ABC-1"},
        )
        await _document(control, "in-b", "jira://ABC/ABC-1", scope="jira-b", connection_id="b")
        assert (await resolvable(tenant_id="tenant-1"), await resolved()) == ((), set())


def test_reconciliation_locks_the_declarer_row_on_postgres() -> None:
    # Two concurrent reconciliations of one declarer otherwise race DELETE-then-INSERT
    # into a primary-key violation under READ COMMITTED.
    statement = str(_declarer_lock("declarer").compile(dialect=postgresql.dialect()))

    assert "FROM documents" in statement
    assert statement.rstrip().endswith("FOR NO KEY UPDATE")


@pytest.mark.asyncio
async def test_reconciliation_takes_the_declarer_lock_before_deleting(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    executed: list[str] = []
    original = AsyncSession.execute

    async def recording(self: AsyncSession, statement: Any, *args: Any, **kwargs: Any) -> Any:
        executed.append(str(statement.compile(dialect=postgresql.dialect())))
        return await original(self, statement, *args, **kwargs)

    async with make_control_plane(tmp_path) as control:
        await _document(control, "declarer", "jira://ENG/ENG-1")
        monkeypatch.setattr(AsyncSession, "execute", recording)
        await _remember(control, "declarer", _link("jira://RHR/RHR-1"))

    assert executed[0].rstrip().endswith("FOR NO KEY UPDATE")
    assert executed[1].startswith("DELETE FROM unresolved_source_relations")
