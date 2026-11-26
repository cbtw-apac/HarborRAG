"""A decomposed source item stored under its collapsed ID moves its document to the NFC spelling."""

from __future__ import annotations

import re
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

import pytest
from sqlalchemy import select

from harborrag_adapters.repositories.backends.sqlalchemy import SQLAlchemyDBClient
from harborrag_adapters.repositories.database.ingestion_control import (
    IngestionControlPlaneDatabase,
)
from harborrag_adapters.repositories.database.ingestion_control.schema import DOCUMENTS
from harborrag_core.chunking import ConnectorType
from harborrag_core.ingestion import (
    DiscoveredSourceItem,
    DocumentVersionCandidate,
    DocumentVersionState,
    SourceAdmissionDecision,
    SourceIdentity,
    identity_for_source,
)
from harborrag_core.ingestion import identity as identity_module
from harborrag_core.schemas.ids import DocumentId

from .ingestion_control_fixtures import advance_to_verified, candidate, sqlite_client

pytestmark = pytest.mark.unit

_SCOPE = "scope-docs"
_COMPOSED = "docs/caf\u00e9.md"
_DECOMPOSED = "docs/cafe\u0301.md"
_LOSSY_SPACING = re.compile(r"[^\S ]| {2}|\A | \Z")


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
    client: SQLAlchemyDBClient,
) -> AsyncIterator[IngestionControlPlaneDatabase]:
    async with IngestionControlPlaneDatabase(client, create_schema=True) as control_plane:
        await control_plane.source_scans.register_scope(
            tenant_id="acme",
            source_scope_id=_SCOPE,
            connector_type="local",
            connection_id="local-docs",
            configuration_fingerprint="config-v1",
        )
        yield control_plane


async def _scan(
    control_plane: IngestionControlPlaneDatabase, *items: DiscoveredSourceItem
) -> tuple[tuple[SourceAdmissionDecision, ...], tuple[str, ...]]:
    scans = control_plane.source_scans
    scan_id = await scans.start(_SCOPE)
    registrations = await scans.record_seen_many(scan_id=scan_id, items=items)
    await scans.complete(scan_id)
    removed = await scans.reconcile_removals(scan_id)
    return tuple(registration.decision for registration in registrations), removed


async def _publish_collapsed(
    control_plane: IngestionControlPlaneDatabase,
    monkeypatch: pytest.MonkeyPatch,
    owner: SourceIdentity,
    *discovered: SourceIdentity,
) -> DocumentVersionCandidate:
    """Scan and publish as 2.0.0a2 did, which gave every spelling the collapsed ID."""
    with monkeypatch.context() as earlier:
        earlier.setattr(
            identity_module,
            "_normalization_changes",
            lambda value: bool(_LOSSY_SPACING.search(value)),
        )
        published = candidate("release checklist", source=owner)
        await _scan(control_plane, *(_item(source) for source in discovered))
        await advance_to_verified(control_plane, published)
        await control_plane.publisher.publish(
            document_id=str(published.document_id),
            candidate_document_version_id=str(published.document_version_id),
        )
    return published


async def _document_owner(client: SQLAlchemyDBClient, document_id: str) -> str:
    async with client.sessions() as session:
        result = await session.execute(
            select(DOCUMENTS.c.source_item_id).where(DOCUMENTS.c.document_id == document_id)
        )
        return str(result.scalar_one())


async def _stored_document_id(
    control_plane: IngestionControlPlaneDatabase, source_item_id: str
) -> str:
    stored = await control_plane.source_scans.source_item(
        source_scope_id=_SCOPE, source_item_id=source_item_id
    )
    assert stored is not None
    return str(stored.document_id)


@pytest.mark.asyncio
async def test_a_published_decomposed_item_is_registered_new_and_its_document_retired_once_missed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    client = sqlite_client(tmp_path / "control-plane.db")
    async with _control_plane(client) as control_plane:
        decomposed = _source(_DECOMPOSED)
        collapsed = await _publish_collapsed(control_plane, monkeypatch, decomposed, decomposed)

        first_decisions, first_removed = await _scan(control_plane, _item(decomposed))
        new_state = await control_plane.document_versions.create_candidate(
            candidate("release checklist", source=decomposed)
        )
        second_decisions, removed = await _scan(control_plane, _item(decomposed))
        retired = await control_plane.publisher.retire_removed(document_id=removed[0])
        cleanup = await control_plane.reliability.pending_cleanup_jobs(
            tenant_id="acme", source_scope_id=_SCOPE
        )

        assert first_decisions == (SourceAdmissionDecision.NEW,)
        assert first_removed == ()
        assert new_state == DocumentVersionState.PENDING
        assert second_decisions == (SourceAdmissionDecision.UNCHANGED,)
        assert removed == (str(collapsed.document_id),)
        assert retired.retired_document_version_id == collapsed.document_version_id
        assert [job.document_version_id for job in cleanup] == [collapsed.document_version_id]
        assert await _stored_document_id(control_plane, _DECOMPOSED) == str(
            identity_for_source(decomposed)
        )
        assert await _stored_document_id(control_plane, _COMPOSED) == str(collapsed.document_id)
        assert await _document_owner(client, str(collapsed.document_id)) == _COMPOSED


@pytest.mark.asyncio
async def test_a_live_nfc_twin_keeps_the_collapsed_document_and_can_update_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    client = sqlite_client(tmp_path / "control-plane.db")
    async with _control_plane(client) as control_plane:
        composed, decomposed = _source(_COMPOSED), _source(_DECOMPOSED)
        collapsed = await _publish_collapsed(
            control_plane, monkeypatch, decomposed, composed, decomposed
        )

        decisions, removed = await _scan(control_plane, _item(decomposed), _item(composed))
        _, later_removed = await _scan(control_plane, _item(decomposed), _item(composed))
        twin_state = await control_plane.document_versions.create_candidate(
            candidate("release checklist v2", source=composed)
        )

        assert decisions == (SourceAdmissionDecision.NEW, SourceAdmissionDecision.UNCHANGED)
        assert (removed, later_removed) == ((), ())
        assert twin_state == DocumentVersionState.PENDING
        assert await _stored_document_id(control_plane, _COMPOSED) == str(collapsed.document_id)
        assert await _document_owner(client, str(collapsed.document_id)) == _COMPOSED


@pytest.mark.asyncio
async def test_a_document_the_nfc_twin_owns_stays_bound_to_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    client = sqlite_client(tmp_path / "control-plane.db")
    async with _control_plane(client) as control_plane:
        composed, decomposed = _source(_COMPOSED), _source(_DECOMPOSED)
        collapsed = await _publish_collapsed(
            control_plane, monkeypatch, composed, composed, decomposed
        )

        decisions, removed = await _scan(control_plane, _item(composed), _item(decomposed))
        new_state = await control_plane.document_versions.create_candidate(
            candidate("release checklist", source=decomposed)
        )

        assert decisions == (SourceAdmissionDecision.UNCHANGED, SourceAdmissionDecision.NEW)
        assert removed == ()
        assert new_state == DocumentVersionState.PENDING
        assert await _document_owner(client, str(collapsed.document_id)) == _COMPOSED
        assert await _stored_document_id(control_plane, _DECOMPOSED) == str(
            identity_for_source(decomposed)
        )
