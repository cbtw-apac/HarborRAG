"""Cancellation joins SQL cleanup instead of racing the next canonical writer."""

import asyncio
from pathlib import Path

import aiosqlite
import pytest
from sqlalchemy import insert, select
from sqlalchemy.ext.asyncio import AsyncSessionTransaction

from harborrag_adapters.repositories.database.ingestion_control.topology.schema import (
    TOPOLOGY_RESOLUTION_HEADS,
)
from harborrag_adapters.repositories.database.ingestion_control.topology.transactions import (
    _drain_exit,
    topology_transaction,
)
from harborrag_core.topology.config import TenantIndexingConfig

from .ingestion_control_fixtures import make_control_plane


@pytest.mark.asyncio
@pytest.mark.parametrize("rollback", [False, True], ids=["commit", "rollback"])
async def test_repeated_cancellation_drains_exit_before_next_writer(
    tmp_path: Path,
    monkeypatch,
    rollback: bool,
) -> None:
    entered = asyncio.Event()
    release = asyncio.Event()
    original = AsyncSessionTransaction.__aexit__

    async def gated_exit(transaction, error_type, error, traceback):
        if transaction.session.info.get("cancellation_gate"):
            entered.set()
            await release.wait()
        return await original(transaction, error_type, error, traceback)

    monkeypatch.setattr(AsyncSessionTransaction, "__aexit__", gated_exit)
    async with make_control_plane(tmp_path) as control:

        async def write():
            async with topology_transaction(control._client) as session:
                session.info["cancellation_gate"] = True
                await session.execute(
                    insert(TOPOLOGY_RESOLUTION_HEADS).values(tenant_id="probe", revision=1)
                )
                if rollback:
                    raise ValueError("rollback the partial write")

        task = asyncio.create_task(write())
        await asyncio.wait_for(entered.wait(), timeout=5)
        task.cancel()
        await asyncio.sleep(0)
        task.cancel()
        await asyncio.sleep(0)
        assert not task.done(), "cancellation must not detach unfinished SQL cleanup"
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        # No retry or increased SQLite busy timeout: the next writer is immediately safe.
        await control.topology.configure_indexing(TenantIndexingConfig(tenant_id="probe"))
        async with control._client.sessions() as session:
            revision = (
                await session.execute(
                    select(TOPOLOGY_RESOLUTION_HEADS.c.revision).where(
                        TOPOLOGY_RESOLUTION_HEADS.c.tenant_id == "probe",
                    )
                )
            ).scalar_one_or_none()
        assert revision == (None if rollback else 1)


@pytest.mark.asyncio
async def test_cleanup_database_errors_are_not_suppressed_after_cancellation() -> None:
    entered = asyncio.Event()
    release = asyncio.Event()

    async def failed_exit():
        entered.set()
        await release.wait()
        raise RuntimeError("database cleanup failed")

    task = asyncio.create_task(_drain_exit(failed_exit()))
    await entered.wait()
    task.cancel()
    await asyncio.sleep(0)
    release.set()
    with pytest.raises(RuntimeError, match="database cleanup failed"):
        await task


@pytest.mark.asyncio
async def test_cancellation_during_returning_fetch_drains_cursor_before_rollback(
    tmp_path, monkeypatch
):
    entered = asyncio.Event()
    release = asyncio.Event()
    original = aiosqlite.Cursor.fetchall
    intercepted = False
    driver_cancelled = asyncio.Event()

    async def gated_fetch(cursor):
        nonlocal intercepted
        if not intercepted:
            intercepted = True
            entered.set()
            try:
                await release.wait()
            except asyncio.CancelledError:
                driver_cancelled.set()
                raise
        return await original(cursor)

    async with make_control_plane(tmp_path) as control:
        monkeypatch.setattr(aiosqlite.Cursor, "fetchall", gated_fetch)

        async def write():
            async with topology_transaction(control._client) as session:
                await session.execute(
                    insert(TOPOLOGY_RESOLUTION_HEADS)
                    .values(
                        tenant_id="probe",
                        revision=1,
                    )
                    .returning(TOPOLOGY_RESOLUTION_HEADS.c.revision)
                )

        task = asyncio.create_task(write())
        await asyncio.wait_for(entered.wait(), timeout=5)
        task.cancel()
        await asyncio.sleep(0)
        task.cancel()
        await asyncio.sleep(0)
        try:
            assert not driver_cancelled.is_set(), "cancellation must not strand the driver's cursor"
            assert not task.done(), "RETURNING cursor must close before cancellation escapes"
        finally:
            release.set()
        with pytest.raises(asyncio.CancelledError) as failure:
            await task
        assert failure.value.__traceback__ is not None
        await control.topology.configure_indexing(TenantIndexingConfig(tenant_id="probe"))
        async with control._client.sessions() as session:
            assert (
                await session.execute(
                    select(TOPOLOGY_RESOLUTION_HEADS.c.revision).where(
                        TOPOLOGY_RESOLUTION_HEADS.c.tenant_id == "probe",
                    )
                )
            ).scalar_one_or_none() is None
