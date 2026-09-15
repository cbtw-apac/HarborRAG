"""Preview-only retention and policy recovery for replaceable summary records."""

from datetime import timedelta

import pytest
from sqlalchemy import update

from harborrag_adapters.repositories.database.ingestion_control.summary_schema import SUMMARY_CACHE
from harborrag_core.base import utc_now
from harborrag_core.summaries import SummaryCard

from .ingestion_control_fixtures import make_control_plane
from .test_summary_projection import binding, node, prepare


@pytest.mark.asyncio
async def test_cleanup_preserves_live_bindings_and_requires_explicit_apply(tmp_path):
    async with make_control_plane(tmp_path) as control:
        version = await prepare(control)
        lease = await control.summaries.claim("DEFAULT")
        snapshot = await control.summaries.snapshot(lease)
        value = binding(lease, snapshot, node(version))
        await control.summaries.put_card("DEFAULT", value.generation_key, value.card)
        await control.summaries.put_card("DEFAULT", "unused", SummaryCard(description="Unused."))
        await control.summaries.accept(lease, snapshot, value, node(version))
        assert (await control.summaries.cleanup("DEFAULT", apply=True))[
            "deferred"
        ] == "summary_work_running"
        await control.summaries.finish(lease)
        async with control._client.sessions.begin() as session:
            await session.execute(
                update(SUMMARY_CACHE).values(created_at=utc_now() - timedelta(days=40))
            )
        preview = await control.summaries.cleanup("DEFAULT")
        assert preview["cache_candidates"] == 1 and preview["removed"] == 0
        assert await control.summaries.get_card("DEFAULT", "unused") is not None
        applied = await control.summaries.cleanup("DEFAULT", apply=True)
        assert applied["removed"] == 1 and applied["canonical_artifacts_retained"]
        assert await control.summaries.get_card("DEFAULT", "unused") is None
        assert await control.summaries.get_card("DEFAULT", value.generation_key) == value.card


@pytest.mark.asyncio
async def test_backfill_enqueues_a_new_revision_without_generating(tmp_path):
    async with make_control_plane(tmp_path) as control:
        await prepare(control)
        first = await control.summaries.claim("DEFAULT")
        await control.summaries.finish(first)
        assert await control.summaries.backfill("DEFAULT", "scope-engineering") == 1
        second = await control.summaries.claim("DEFAULT")
        assert second.revision > first.revision
