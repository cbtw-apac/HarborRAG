from pathlib import Path

import pytest
from sqlalchemy import select

from harborrag_adapters.repositories.database.ingestion_control.schema import DOCUMENTS

from .ingestion_control_fixtures import (
    advance_to_verified,
    candidate,
    make_control_plane,
    source_identity,
)
from .topology_fixtures import permit, policy, publish


@pytest.mark.asyncio
async def test_publishing_new_scope_updates_authority_and_enqueues_only_new_policy(
    tmp_path: Path,
) -> None:
    async with make_control_plane(tmp_path) as control:
        await publish(control)
        await control.topology.configure_policy(
            policy().model_copy(update={"source_scope_id": "scope-new"})
        )
        moved = candidate(
            "moved", source=source_identity().model_copy(update={"source_scope_id": "scope-new"})
        )
        await permit(control, str(moved.document_id), "scope-new")
        await advance_to_verified(control, moved)
        # Staging a new scope must not change active document authority.
        async with control._client.sessions() as session:
            before = (
                await session.execute(
                    select(DOCUMENTS.c.source_scope_id).where(
                        DOCUMENTS.c.document_id == str(moved.document_id),
                    )
                )
            ).scalar_one()
        assert before == "scope-engineering"
        await control.publisher.publish(
            document_id=str(moved.document_id),
            candidate_document_version_id=str(moved.document_version_id),
        )
        async with control._client.sessions() as session:
            after = (
                await session.execute(
                    select(DOCUMENTS.c.source_scope_id).where(
                        DOCUMENTS.c.document_id == str(moved.document_id),
                    )
                )
            ).scalar_one()
        assert after == "scope-new"
        jobs = await control.topology.list_jobs("DEFAULT")
        assert len(jobs) == 1 and jobs[0].source_scope_id == "scope-new"
        assert await control.topology.claim("DEFAULT") is not None
