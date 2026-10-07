"""With summarization off, publishing and retiring leave the summary tables alone."""

from pathlib import Path

import pytest
from sqlalchemy import func, select

from harborrag_adapters.repositories.database import IngestionControlPlaneDatabase
from harborrag_adapters.repositories.database.ingestion_control.summary_schema import (
    SUMMARY_SCOPES,
    SUMMARY_TENANTS,
)
from harborrag_adapters.repositories.database.sqlite.client import SQLiteDBClient

from .ingestion_control_fixtures import advance_to_verified, candidate
from .topology_fixtures import permit


def _control(tmp_path: Path, *, summaries_enabled: bool) -> IngestionControlPlaneDatabase:
    return IngestionControlPlaneDatabase(
        SQLiteDBClient(database=str(tmp_path / "control-plane.db")),
        create_schema=True,
        summaries_enabled=summaries_enabled,
    )


async def _summary_rows(control: IngestionControlPlaneDatabase) -> tuple[int, int]:
    async with control._client.sessions.begin() as session:
        scopes = (await session.execute(select(func.count()).select_from(SUMMARY_SCOPES))).scalar()
        tenants = (
            await session.execute(select(func.count()).select_from(SUMMARY_TENANTS))
        ).scalar()
    return scopes, tenants


@pytest.mark.asyncio
@pytest.mark.parametrize(("summaries_enabled", "expect_rows"), [(True, True), (False, False)])
async def test_publication_marks_summary_scopes_only_while_summarization_is_on(
    tmp_path: Path, summaries_enabled: bool, expect_rows: bool
) -> None:
    async with _control(tmp_path, summaries_enabled=summaries_enabled) as control:
        version = candidate("one")
        await permit(control, str(version.document_id))
        await advance_to_verified(control, version)

        await control.publisher.publish(
            document_id=str(version.document_id),
            candidate_document_version_id=str(version.document_version_id),
        )
        await control.publisher.retire_removed(document_id=str(version.document_id))

        scopes, tenants = await _summary_rows(control)
        assert (scopes > 0 and tenants > 0) is expect_rows
        assert (scopes == 0 and tenants == 0) is not expect_rows
