"""SqlScheduleRepository and the 0050 schedules migration against SQLite."""

from __future__ import annotations

import base64
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
import sqlalchemy as sa
from alembic import command

from harborrag_adapters.repositories.database.control_plane.migrations import (
    _build_config,
    run_migrations,
)
from harborrag_adapters.repositories.database.control_plane.schedules import (
    SqlScheduleRepository,
)
from harborrag_adapters.repositories.database.control_plane.schemas_schedules import ScheduleRow
from harborrag_adapters.repositories.database.control_plane.session import SessionFactory
from harborrag_core.contracts.errors import (
    HarborPreconditionFailedError,
    HarborValidationError,
    ScheduleIdTakenError,
)
from harborrag_core.domain.schedule import (
    QUEUE_WAIT_LIMIT,
    RECENT_SLOT_LIMIT,
    Schedule,
    ScheduleFilter,
    SchedulePause,
    ScheduleSlot,
)

pytestmark = [pytest.mark.integration, pytest.mark.whitebox]

_CREATED = datetime(2026, 11, 1, 8, 0, tzinfo=UTC)
_PAUSE = SchedulePause(reason="maintenance window", by="u_alex", at=_CREATED)


def _schedule(
    schedule_id: str,
    *,
    tenant_id: str = "acme",
    connection_id: str = "wiki",
    created_at: datetime = _CREATED,
) -> Schedule:
    return Schedule(
        tenant_id=tenant_id,
        schedule_id=schedule_id,
        project_id="demo",
        connection_id=connection_id,
        interval_seconds=3600,
        created_by="u_alex",
        updated_by="u_alex",
        created_at=created_at,
        updated_at=created_at,
    )


def _slot(minute: int, *, manual: bool = False) -> ScheduleSlot:
    return ScheduleSlot(
        due_at=_CREATED + timedelta(minutes=minute),
        outcome="started",
        manual=manual,
        ingestion_id=f"ing-{minute}",
    )


async def _ids(
    repository: SqlScheduleRepository, filters: ScheduleFilter, *, tenant_id: str = "acme"
) -> list[str]:
    page, _ = await repository.page(tenant_id=tenant_id, filters=filters, limit=50, cursor=None)
    return [schedule.schedule_id for schedule in page]


@pytest.mark.asyncio
async def test_put_creates_then_overwrite_keeps_runtime_state(sessions: SessionFactory) -> None:
    repository = SqlScheduleRepository(sessions)
    created, was_created = await repository.put(
        _schedule("nightly"), expected_version=None, create_only=True
    )
    assert (was_created, created.version, created.state) == (True, 1, "active")
    assert await repository.get(tenant_id="acme", schedule_id="nightly") == created

    next_run_at = _CREATED + timedelta(hours=1)
    await repository.set_next_run_at(
        tenant_id="acme", schedule_id="nightly", next_run_at=next_run_at
    )
    await repository.record_run_outcome(tenant_id="acme", schedule_id="nightly", failed=True)
    assert await repository.claim_queue(tenant_id="acme", schedule_id="nightly")
    await repository.record_slot(tenant_id="acme", schedule_id="nightly", slot=_slot(0))

    change = replace(
        _schedule("nightly"),
        connection_id="tickets",
        interval_seconds=None,
        cron="0 2 * * *",
        timezone="Europe/Paris",
        mode="force",
        description="Nightly full refresh",
        created_by="u_sam",
        updated_by="u_sam",
        created_at=_CREATED + timedelta(days=3),
    )
    stored, was_created = await repository.put(change, expected_version=1, create_only=False)

    assert not was_created
    assert stored == await repository.get(tenant_id="acme", schedule_id="nightly")
    assert (stored.connection_id, stored.cron, stored.interval_seconds) == (
        "tickets",
        "0 2 * * *",
        None,
    )
    assert (stored.timezone, stored.mode, stored.description) == (
        "Europe/Paris",
        "force",
        "Nightly full refresh",
    )
    assert (stored.version, stored.updated_by, stored.created_by) == (2, "u_sam", "u_alex")
    assert stored.created_at == _CREATED
    assert stored.updated_at > _CREATED
    assert (stored.consecutive_failures, stored.next_run_at) == (1, next_run_at)
    assert not await repository.claim_queue(tenant_id="acme", schedule_id="nightly")
    slots = await repository.recent_slots(tenant_id="acme", schedule_ids=["nightly"], limit=5)
    assert slots == {"nightly": [_slot(0)]}


@pytest.mark.asyncio
async def test_put_overwrite_keeps_a_pause(sessions: SessionFactory) -> None:
    repository = SqlScheduleRepository(sessions)
    await repository.put(_schedule("nightly"), expected_version=None, create_only=False)
    await repository.set_paused(
        tenant_id="acme", schedule_id="nightly", paused=_PAUSE, actor="u_alex"
    )

    stored, _ = await repository.put(
        replace(_schedule("nightly"), jitter_seconds=30), expected_version=2, create_only=False
    )

    assert (stored.version, stored.paused, stored.jitter_seconds) == (3, _PAUSE, 30)


@pytest.mark.asyncio
async def test_put_create_only_on_an_existing_schedule_is_refused(
    sessions: SessionFactory,
) -> None:
    repository = SqlScheduleRepository(sessions)
    await repository.put(_schedule("nightly"), expected_version=None, create_only=True)

    with pytest.raises(ScheduleIdTakenError):
        await repository.put(_schedule("nightly"), expected_version=None, create_only=True)

    stored = await repository.get(tenant_id="acme", schedule_id="nightly")
    assert stored is not None and stored.version == 1


@pytest.mark.asyncio
async def test_put_checks_the_expected_version(sessions: SessionFactory) -> None:
    repository = SqlScheduleRepository(sessions)
    with pytest.raises(HarborPreconditionFailedError):
        await repository.put(_schedule("nightly"), expected_version=1, create_only=False)
    assert await repository.get(tenant_id="acme", schedule_id="nightly") is None

    await repository.put(_schedule("nightly"), expected_version=None, create_only=False)
    with pytest.raises(HarborPreconditionFailedError):
        await repository.put(_schedule("nightly"), expected_version=7, create_only=False)

    stored, was_created = await repository.put(
        _schedule("nightly"), expected_version=1, create_only=False
    )
    assert (was_created, stored.version) == (False, 2)


@pytest.mark.asyncio
async def test_page_walks_newest_first_with_a_cursor(sessions: SessionFactory) -> None:
    repository = SqlScheduleRepository(sessions)
    later = _CREATED + timedelta(minutes=5)
    for schedule in (
        _schedule("oldest", created_at=_CREATED),
        _schedule("tie-b", created_at=later),
        _schedule("tie-a", created_at=later),
        _schedule("newest", created_at=_CREATED + timedelta(minutes=9)),
        _schedule("middle", created_at=_CREATED + timedelta(minutes=1)),
    ):
        await repository.put(schedule, expected_version=None, create_only=True)

    listed: list[list[str]] = []
    cursor: str | None = None
    while True:
        page, cursor = await repository.page(
            tenant_id="acme", filters=ScheduleFilter(), limit=2, cursor=cursor
        )
        listed.append([schedule.schedule_id for schedule in page])
        if cursor is None:
            break

    assert listed == [["newest", "tie-a"], ["tie-b", "middle"], ["oldest"]]


@pytest.mark.parametrize(
    "cursor",
    [
        "not a cursor",
        base64.urlsafe_b64encode(b'{"created_at":"2026-11-01"}').decode(),
        base64.urlsafe_b64encode(b'["2026-11-01T08:00:00","nightly"]').decode(),
        base64.urlsafe_b64encode(b'["2026-11-01T08:00:00+00:00",""]').decode(),
    ],
)
@pytest.mark.asyncio
async def test_page_refuses_a_malformed_cursor(sessions: SessionFactory, cursor: str) -> None:
    repository = SqlScheduleRepository(sessions)

    with pytest.raises(HarborValidationError) as caught:
        await repository.page(tenant_id="acme", filters=ScheduleFilter(), limit=2, cursor=cursor)

    assert caught.value.details == {"field": "cursor"}


@pytest.mark.asyncio
async def test_page_applies_every_filter(sessions: SessionFactory) -> None:
    repository = SqlScheduleRepository(sessions)
    for schedule in (
        _schedule("wiki-hourly"),
        _schedule("tickets-hourly", connection_id="tickets"),
        replace(_schedule("other-project"), project_id="sales"),
    ):
        await repository.put(schedule, expected_version=None, create_only=True)
    await repository.set_next_run_at(
        tenant_id="acme", schedule_id="wiki-hourly", next_run_at=_CREATED + timedelta(minutes=10)
    )
    await repository.set_next_run_at(
        tenant_id="acme", schedule_id="tickets-hourly", next_run_at=_CREATED + timedelta(hours=5)
    )
    await repository.set_paused(
        tenant_id="acme", schedule_id="other-project", paused=_PAUSE, actor="u_alex"
    )

    assert sorted(await _ids(repository, ScheduleFilter(project_id="demo"))) == [
        "tickets-hourly",
        "wiki-hourly",
    ]
    assert await _ids(repository, ScheduleFilter(project_id="demo", connection_id="tickets")) == [
        "tickets-hourly"
    ]
    assert len(await _ids(repository, ScheduleFilter(operation="start_ingestion"))) == 3
    assert await _ids(repository, ScheduleFilter(state="paused")) == ["other-project"]
    assert sorted(await _ids(repository, ScheduleFilter(state="active"))) == [
        "tickets-hourly",
        "wiki-hourly",
    ]
    assert await _ids(repository, ScheduleFilter(due_before=_CREATED + timedelta(hours=1))) == [
        "wiki-hourly"
    ]


@pytest.mark.asyncio
async def test_set_paused_changes_state_once_and_resume_clears_failures(
    sessions: SessionFactory,
) -> None:
    repository = SqlScheduleRepository(sessions)
    await repository.put(_schedule("nightly"), expected_version=None, create_only=True)
    await repository.set_next_run_at(
        tenant_id="acme", schedule_id="nightly", next_run_at=_CREATED + timedelta(hours=1)
    )
    await repository.record_run_outcome(tenant_id="acme", schedule_id="nightly", failed=True)

    paused = await repository.set_paused(
        tenant_id="acme", schedule_id="nightly", paused=_PAUSE, actor="u_sam"
    )
    assert paused is not None
    assert (paused.state, paused.paused, paused.next_run_at) == ("paused", _PAUSE, None)
    assert (paused.version, paused.updated_by, paused.consecutive_failures) == (2, "u_sam", 1)

    again = await repository.set_paused(
        tenant_id="acme",
        schedule_id="nightly",
        paused=replace(_PAUSE, reason="another reason"),
        actor="u_kim",
    )
    assert again == paused

    resumed = await repository.set_paused(
        tenant_id="acme", schedule_id="nightly", paused=None, actor="u_kim"
    )
    assert resumed is not None
    assert (resumed.state, resumed.paused, resumed.consecutive_failures) == ("active", None, 0)
    assert (resumed.version, resumed.updated_by) == (3, "u_kim")

    unchanged = await repository.set_paused(
        tenant_id="acme", schedule_id="nightly", paused=None, actor="u_alex"
    )
    assert unchanged == resumed
    assert (
        await repository.set_paused(
            tenant_id="acme", schedule_id="missing", paused=_PAUSE, actor="u_alex"
        )
        is None
    )


@pytest.mark.asyncio
async def test_record_run_outcome_counts_failures_in_a_row(sessions: SessionFactory) -> None:
    repository = SqlScheduleRepository(sessions)
    await repository.put(_schedule("nightly"), expected_version=None, create_only=True)

    counts = [
        await repository.record_run_outcome(tenant_id="acme", schedule_id="nightly", failed=failed)
        for failed in (True, True, False, True)
    ]

    assert counts == [1, 2, 0, 1]
    assert (
        await repository.record_run_outcome(tenant_id="acme", schedule_id="missing", failed=True)
        == 0
    )


@pytest.mark.asyncio
async def test_claim_queue_lets_one_slot_wait_until_released(sessions: SessionFactory) -> None:
    repository = SqlScheduleRepository(sessions)
    await repository.put(_schedule("nightly"), expected_version=None, create_only=True)

    assert await repository.claim_queue(tenant_id="acme", schedule_id="nightly")
    assert not await repository.claim_queue(tenant_id="acme", schedule_id="nightly")
    await repository.release_queue(tenant_id="acme", schedule_id="nightly")
    assert await repository.claim_queue(tenant_id="acme", schedule_id="nightly")

    assert not await repository.claim_queue(tenant_id="acme", schedule_id="missing")
    await repository.release_queue(tenant_id="acme", schedule_id="missing")


@pytest.mark.asyncio
async def test_claim_queue_takes_over_a_mark_older_than_the_wait_limit(
    sessions: SessionFactory,
) -> None:
    repository = SqlScheduleRepository(sessions)
    await repository.put(_schedule("nightly"), expected_version=None, create_only=True)
    assert await repository.claim_queue(tenant_id="acme", schedule_id="nightly")

    async with sessions.begin() as session:
        await session.execute(
            sa.update(ScheduleRow).values(
                queued_at=datetime.now(UTC) - QUEUE_WAIT_LIMIT - timedelta(hours=2)
            )
        )

    assert await repository.claim_queue(tenant_id="acme", schedule_id="nightly")
    assert not await repository.claim_queue(tenant_id="acme", schedule_id="nightly")


@pytest.mark.asyncio
async def test_runtime_writes_leave_the_version_alone(sessions: SessionFactory) -> None:
    repository = SqlScheduleRepository(sessions)
    await repository.put(_schedule("nightly"), expected_version=None, create_only=True)

    await repository.set_next_run_at(tenant_id="acme", schedule_id="nightly", next_run_at=_CREATED)
    await repository.record_run_outcome(tenant_id="acme", schedule_id="nightly", failed=True)
    await repository.claim_queue(tenant_id="acme", schedule_id="nightly")
    await repository.release_queue(tenant_id="acme", schedule_id="nightly")
    await repository.record_slot(tenant_id="acme", schedule_id="nightly", slot=_slot(0))

    stored = await repository.get(tenant_id="acme", schedule_id="nightly")
    assert stored is not None and stored.version == 1


@pytest.mark.asyncio
async def test_record_slot_keeps_the_newest_slots(sessions: SessionFactory) -> None:
    repository = SqlScheduleRepository(sessions)
    await repository.put(_schedule("nightly"), expected_version=None, create_only=True)
    await repository.put(_schedule("hourly"), expected_version=None, create_only=True)
    total = RECENT_SLOT_LIMIT + 5
    slots = [_slot(minute, manual=minute % 2 == 0) for minute in range(total)]
    for slot in slots:
        await repository.record_slot(tenant_id="acme", schedule_id="nightly", slot=slot)
    await repository.record_slot(tenant_id="acme", schedule_id="hourly", slot=_slot(99))
    await repository.record_slot(tenant_id="acme", schedule_id="missing", slot=_slot(0))

    every = await repository.recent_slots(tenant_id="acme", schedule_ids=["nightly"], limit=total)
    newest = await repository.recent_slots(
        tenant_id="acme", schedule_ids=["nightly", "hourly", "missing"], limit=2
    )

    assert every == {"nightly": slots[:4:-1]}
    assert newest == {"nightly": slots[:-3:-1], "hourly": [_slot(99)]}
    assert await repository.recent_slots(tenant_id="acme", schedule_ids=[], limit=2) == {}
    async with sessions() as session:
        stored = await session.scalar(sa.text("SELECT count(*) FROM schedule_slots"))
    assert stored == RECENT_SLOT_LIMIT + 1


@pytest.mark.asyncio
async def test_delete_removes_the_schedule_and_its_slots(sessions: SessionFactory) -> None:
    repository = SqlScheduleRepository(sessions)
    await repository.put(_schedule("nightly"), expected_version=None, create_only=True)
    await repository.record_slot(tenant_id="acme", schedule_id="nightly", slot=_slot(0))

    assert await repository.delete(tenant_id="acme", schedule_id="nightly")
    assert not await repository.delete(tenant_id="acme", schedule_id="nightly")

    assert await repository.get(tenant_id="acme", schedule_id="nightly") is None
    async with sessions() as session:
        stored = await session.scalar(sa.text("SELECT count(*) FROM schedule_slots"))
    assert stored == 0


@pytest.mark.asyncio
async def test_list_for_connection_returns_only_that_connection(sessions: SessionFactory) -> None:
    repository = SqlScheduleRepository(sessions)
    for schedule in (
        _schedule("wiki-hourly"),
        _schedule("wiki-nightly", created_at=_CREATED + timedelta(minutes=1)),
        _schedule("tickets-hourly", connection_id="tickets"),
        replace(_schedule("sales-wiki"), project_id="sales"),
    ):
        await repository.put(schedule, expected_version=None, create_only=True)

    listed = await repository.list_for_connection(
        tenant_id="acme", project_id="demo", connection_id="wiki"
    )

    assert [schedule.schedule_id for schedule in listed] == ["wiki-hourly", "wiki-nightly"]


@pytest.mark.asyncio
async def test_next_runs_takes_the_earliest_active_slot_of_each_connection(
    sessions: SessionFactory,
) -> None:
    repository = SqlScheduleRepository(sessions)
    soon, later = _CREATED + timedelta(minutes=10), _CREATED + timedelta(hours=2)
    for schedule, next_run_at in (
        (_schedule("wiki-hourly"), later),
        (_schedule("wiki-nightly"), soon),
        (_schedule("wiki-unset"), None),
        (_schedule("tickets-paused", connection_id="tickets"), soon),
        (_schedule("files-unset", connection_id="files"), None),
        (_schedule("wiki-theirs", tenant_id="globex"), _CREATED),
        (replace(_schedule("sales-wiki"), project_id="sales"), _CREATED),
    ):
        await repository.put(schedule, expected_version=None, create_only=True)
        await repository.set_next_run_at(
            tenant_id=schedule.tenant_id,
            schedule_id=schedule.schedule_id,
            next_run_at=next_run_at,
        )
    await repository.set_paused(
        tenant_id="acme", schedule_id="tickets-paused", paused=_PAUSE, actor="u_alex"
    )
    await repository.set_next_run_at(
        tenant_id="acme", schedule_id="tickets-paused", next_run_at=soon
    )

    found = await repository.next_runs(
        tenant_id="acme",
        connections=[("demo", "wiki"), ("demo", "tickets"), ("demo", "files"), ("demo", "none")],
    )

    assert found == {("demo", "wiki"): soon}
    assert found[("demo", "wiki")].tzinfo is not None
    assert await repository.next_runs(tenant_id="acme", connections=[]) == {}


@pytest.mark.asyncio
async def test_each_tenant_sees_only_its_own_schedule(sessions: SessionFactory) -> None:
    repository = SqlScheduleRepository(sessions)
    await repository.put(_schedule("nightly"), expected_version=None, create_only=True)
    await repository.put(
        _schedule("nightly", tenant_id="globex"), expected_version=None, create_only=True
    )
    await repository.record_slot(tenant_id="acme", schedule_id="nightly", slot=_slot(1))
    await repository.record_slot(tenant_id="globex", schedule_id="nightly", slot=_slot(2))

    assert await repository.delete(tenant_id="globex", schedule_id="nightly")

    acme = await repository.get(tenant_id="acme", schedule_id="nightly")
    assert acme is not None and acme.tenant_id == "acme"
    assert await repository.get(tenant_id="globex", schedule_id="nightly") is None
    assert await _ids(repository, ScheduleFilter()) == ["nightly"]
    assert await _ids(repository, ScheduleFilter(), tenant_id="globex") == []
    assert await repository.recent_slots(tenant_id="acme", schedule_ids=["nightly"], limit=5) == {
        "nightly": [_slot(1)]
    }
    assert (
        await repository.recent_slots(tenant_id="globex", schedule_ids=["nightly"], limit=5) == {}
    )


def test_0050_downgrade_drops_the_schedule_tables_and_upgrade_restores_them(
    tmp_path: Path,
) -> None:
    dsn = f"sqlite+aiosqlite:///{tmp_path / 'control.db'}"
    run_migrations(dsn)
    engine = sa.create_engine(f"sqlite:///{tmp_path / 'control.db'}")
    tables = {"schedules", "schedule_slots"}
    try:
        assert tables <= set(sa.inspect(engine).get_table_names())

        command.downgrade(_build_config(dsn), "0049")
        assert not tables & set(sa.inspect(engine).get_table_names())

        command.upgrade(_build_config(dsn), "head")
        assert tables <= set(sa.inspect(engine).get_table_names())
    finally:
        engine.dispose()
