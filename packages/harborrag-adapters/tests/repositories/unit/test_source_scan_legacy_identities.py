"""Source items stored under 2.0.0a2 document IDs are re-keyed lazily by the next scan."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

import pytest
from sqlalchemy import insert

from harborrag_adapters.repositories.backends.sqlalchemy import SQLAlchemyDBClient
from harborrag_adapters.repositories.database.ingestion_control import (
    IngestionControlPlaneDatabase,
)
from harborrag_adapters.repositories.database.ingestion_control.schema import DOCUMENTS
from harborrag_core.base import utc_now
from harborrag_core.chunking import ConnectorType
from harborrag_core.contracts import HarborConflictError
from harborrag_core.ingestion import (
    DiscoveredSourceItem,
    SourceAdmissionDecision,
    SourceIdentity,
    identity_for_source,
)
from harborrag_core.schemas.ids import DocumentId

from .ingestion_control_fixtures import advance_to_verified, candidate, sqlite_client

pytestmark = pytest.mark.unit

_SCOPE = "scope-docs"
_A2_DOCUMENT_ID = "document:76572d4b57589540e079c66303a6c8c487c718c5d50cc2f5cac29c65a6eaa21d"
"""ID that 2.0.0a2 gave ``docs/Q3<NBSP>plan.md`` of tenant ``acme``, connection ``local-docs``."""


def _source(source_item_id: str) -> SourceIdentity:
    return SourceIdentity(
        tenant_id="acme",
        connector_type=ConnectorType.LOCAL,
        connection_id="local-docs",
        source_item_id=source_item_id,
        source_scope_id=_SCOPE,
    )


def _item(source: SourceIdentity, document_id: str | None = None) -> DiscoveredSourceItem:
    return DiscoveredSourceItem(
        source_identity=source,
        document_id=DocumentId(document_id or identity_for_source(source)),
        source_version="1",
        admission_change_key="admission-1",
    )


@asynccontextmanager
async def _control_plane(
    tmp_path: Path,
) -> AsyncIterator[tuple[IngestionControlPlaneDatabase, SQLAlchemyDBClient]]:
    client = sqlite_client(tmp_path / "control-plane.db")
    async with IngestionControlPlaneDatabase(client, create_schema=True) as control_plane:
        await control_plane.source_scans.register_scope(
            tenant_id="acme",
            source_scope_id=_SCOPE,
            connector_type="local",
            connection_id="local-docs",
            configuration_fingerprint="config-v1",
        )
        yield control_plane, client


async def _scan(
    control_plane: IngestionControlPlaneDatabase, *items: DiscoveredSourceItem
) -> tuple[SourceAdmissionDecision, ...]:
    decisions, _ = await _scan_and_reconcile(control_plane, *items)
    return decisions


async def _scan_and_reconcile(
    control_plane: IngestionControlPlaneDatabase, *items: DiscoveredSourceItem
) -> tuple[tuple[SourceAdmissionDecision, ...], tuple[str, ...]]:
    scans = control_plane.source_scans
    scan_id = await scans.start(_SCOPE)
    registrations = await scans.record_seen_many(scan_id=scan_id, items=items)
    await scans.complete(scan_id)
    removed = await scans.reconcile_removals(scan_id)
    return tuple(registration.decision for registration in registrations), removed


async def _stored_document_id(
    control_plane: IngestionControlPlaneDatabase, source_item_id: str
) -> str | None:
    stored = await control_plane.source_scans.source_item(
        source_scope_id=_SCOPE, source_item_id=source_item_id
    )
    return None if stored is None else str(stored.document_id)


@pytest.mark.asyncio
async def test_an_item_stored_under_its_2_0_0a2_id_is_rekeyed_and_registered_as_new(
    tmp_path: Path,
) -> None:
    async with _control_plane(tmp_path) as (control_plane, _):
        source = _source("docs/Q3 plan.md")
        await _scan(control_plane, _item(source, _A2_DOCUMENT_ID))

        decisions = await _scan(control_plane, _item(source))

        assert decisions == (SourceAdmissionDecision.NEW,)
        assert await _stored_document_id(control_plane, source.source_item_id) == str(
            identity_for_source(source)
        )


@pytest.mark.asyncio
async def test_a_legacy_id_that_holds_a_document_still_conflicts(tmp_path: Path) -> None:
    async with _control_plane(tmp_path) as (control_plane, client):
        source = _source("docs/Q3 plan.md")
        await _scan(control_plane, _item(source, _A2_DOCUMENT_ID))
        async with client.sessions.begin() as session:
            await session.execute(
                insert(DOCUMENTS).values(
                    document_id=_A2_DOCUMENT_ID,
                    tenant_id="acme",
                    source_scope_id=_SCOPE,
                    connector_type="local",
                    connection_id="local-docs",
                    source_item_id=source.source_item_id,
                    created_at=utc_now(),
                    updated_at=utc_now(),
                )
            )

        with pytest.raises(HarborConflictError, match="legacy source identity"):
            await _scan(control_plane, _item(source))
        assert await _stored_document_id(control_plane, source.source_item_id) == _A2_DOCUMENT_ID


@pytest.mark.asyncio
async def test_a_stored_id_that_is_neither_current_nor_legacy_conflicts(tmp_path: Path) -> None:
    async with _control_plane(tmp_path) as (control_plane, _):
        source = _source("docs/Q3 plan.md")
        await _scan(control_plane, _item(source, "document:unrelated"))

        with pytest.raises(HarborConflictError, match="different document identity"):
            await _scan(control_plane, _item(source))
        assert await _stored_document_id(control_plane, source.source_item_id) == (
            "document:unrelated"
        )


@pytest.mark.asyncio
async def test_a_published_item_stored_under_its_trimmed_local_path_is_retired_as_removed(
    tmp_path: Path,
) -> None:
    async with _control_plane(tmp_path) as (control_plane, _):
        trimmed = candidate("release checklist", source=_source("notes.md"))
        exact = _source(" notes.md")
        await _scan(control_plane, _item(trimmed.source_identity))
        await advance_to_verified(control_plane, trimmed)
        await control_plane.publisher.publish(
            document_id=str(trimmed.document_id),
            candidate_document_version_id=str(trimmed.document_version_id),
        )

        first_decisions, first_removed = await _scan_and_reconcile(control_plane, _item(exact))
        _, removed = await _scan_and_reconcile(control_plane, _item(exact))
        retired = await control_plane.publisher.retire_removed(document_id=removed[0])

        assert first_decisions == (SourceAdmissionDecision.NEW,)
        assert first_removed == ()
        assert removed == (str(trimmed.document_id),)
        assert retired.retired_document_version_id == trimmed.document_version_id
        assert await _stored_document_id(control_plane, " notes.md") == str(
            identity_for_source(exact)
        )


@pytest.mark.asyncio
async def test_a_real_file_at_the_trimmed_path_keeps_its_row_and_id(tmp_path: Path) -> None:
    async with _control_plane(tmp_path) as (control_plane, _):
        trimmed = _source("notes.md")
        exact = _source(" notes.md")
        await _scan(control_plane, _item(trimmed))

        decisions = await _scan(control_plane, _item(exact), _item(trimmed))

        assert decisions == (SourceAdmissionDecision.NEW, SourceAdmissionDecision.UNCHANGED)
        assert await _stored_document_id(control_plane, "notes.md") == str(
            identity_for_source(trimmed)
        )
