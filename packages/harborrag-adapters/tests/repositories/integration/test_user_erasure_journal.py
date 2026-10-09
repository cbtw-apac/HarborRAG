"""The SQL erasure journal records a run's start and its final outcome."""

from __future__ import annotations

from dataclasses import replace

import pytest

from harborrag_adapters.repositories.database.control_plane.erasure_journal import (
    SqlUserErasureJournal,
)
from harborrag_adapters.repositories.database.control_plane.session import SessionFactory
from harborrag_core.base import utc_now
from harborrag_core.ports.erasure import UserErasureRecord

pytestmark = [pytest.mark.integration, pytest.mark.whitebox]


def _record() -> UserErasureRecord:
    return UserErasureRecord(tenant_id="ACME", subject_user_id="user-1", actor="admin-1")


@pytest.mark.asyncio
async def test_a_journal_row_records_the_start_and_the_final_outcome(
    sessions: SessionFactory,
) -> None:
    journal = SqlUserErasureJournal(sessions)
    started = _record()
    await journal.start(started)

    running = await journal.get(started.erasure_id)
    assert running is not None
    assert (running.status, running.tenant_id, running.subject_user_id, running.actor) == (
        "running",
        "ACME",
        "user-1",
        "admin-1",
    )

    await journal.finish(
        replace(
            started,
            status="failed",
            counts={"sessions": 2, "memories": 0},
            failed_steps=("agent_runs",),
            finished_at=utc_now(),
        )
    )

    finished = await journal.get(started.erasure_id)
    assert finished is not None
    assert finished.status == "failed"
    assert dict(finished.counts) == {"sessions": 2, "memories": 0}
    assert finished.failed_steps == ("agent_runs",)
    assert finished.finished_at is not None


@pytest.mark.asyncio
async def test_finishing_an_erasure_that_never_started_is_refused(
    sessions: SessionFactory,
) -> None:
    with pytest.raises(ValueError, match="never started"):
        await SqlUserErasureJournal(sessions).finish(_record())
    assert await SqlUserErasureJournal(sessions).get("erasure-unknown") is None
