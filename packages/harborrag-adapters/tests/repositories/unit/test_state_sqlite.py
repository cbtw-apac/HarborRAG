from __future__ import annotations

import asyncio
from datetime import timedelta
from pathlib import Path

import pytest

from harborrag_adapters.repositories.errors import (
    HarborStorageAlreadyExistsError,
    HarborStorageCheckpointConflictError,
    HarborStorageLeaseError,
)
from harborrag_adapters.repositories.state.sqlite.config import SQLiteStateConfig
from harborrag_adapters.repositories.state.sqlite.repository import SQLiteStateBackend
from harborrag_core.schemas.state import CheckpointRecord, WorkflowState
from harborrag_core.schemas.storage import StorageOperationContext


def make_context(tenant: str = "tenant-a") -> StorageOperationContext:
    return StorageOperationContext(tenant_id=tenant)


def make_backend(tmp_path: Path) -> SQLiteStateBackend:
    return SQLiteStateBackend(
        SQLiteStateConfig(database=str(tmp_path / "state.db"), create_schema=True)
    )


@pytest.mark.asyncio
async def test_workflow_state_create_get_save_round_trip(tmp_path: Path) -> None:
    backend = make_backend(tmp_path)
    async with backend:
        context = make_context()
        state = WorkflowState(workflow_id="wf-1", tenant_id=context.tenant_id)
        created = await backend.state.create(state, context=context)
        assert created.version == 1

        loaded = await backend.state.get("wf-1", context=context)
        assert loaded is not None

        saved = await backend.state.save(
            loaded.model_copy(update={"current_step": "step-2"}),
            expected_version=loaded.version,
            context=context,
        )
        assert saved.version == loaded.version + 1
        assert saved.current_step == "step-2"


@pytest.mark.asyncio
async def test_workflow_state_create_rejects_duplicate(tmp_path: Path) -> None:
    backend = make_backend(tmp_path)
    async with backend:
        context = make_context()
        state = WorkflowState(workflow_id="wf-1", tenant_id=context.tenant_id)
        await backend.state.create(state, context=context)
        with pytest.raises(HarborStorageAlreadyExistsError):
            await backend.state.create(state, context=context)


@pytest.mark.asyncio
async def test_workflow_state_save_detects_version_conflict(tmp_path: Path) -> None:
    backend = make_backend(tmp_path)
    async with backend:
        context = make_context()
        state = WorkflowState(workflow_id="wf-1", tenant_id=context.tenant_id)
        await backend.state.create(state, context=context)
        with pytest.raises(HarborStorageCheckpointConflictError):
            await backend.state.save(state, expected_version=99, context=context)


@pytest.mark.asyncio
async def test_checkpoint_append_only_with_optimistic_versioning(
    tmp_path: Path,
) -> None:
    backend = make_backend(tmp_path)
    async with backend:
        context = make_context()
        first = CheckpointRecord(
            id="chk-1",
            workflow_id="wf-1",
            tenant_id=context.tenant_id,
            step_name="step-1",
            state_version=1,
        )
        await backend.checkpoints.save(first, expected_version=None, context=context)

        latest = await backend.checkpoints.load_latest("wf-1", context=context)
        assert latest is not None
        assert latest.state_version == 1

        second = CheckpointRecord(
            id="chk-2",
            workflow_id="wf-1",
            tenant_id=context.tenant_id,
            step_name="step-2",
            state_version=2,
        )
        await backend.checkpoints.save(second, expected_version=1, context=context)

        with pytest.raises(HarborStorageCheckpointConflictError):
            await backend.checkpoints.save(second, expected_version=1, context=context)


@pytest.mark.asyncio
async def test_lease_acquire_renew_release_with_fencing(tmp_path: Path) -> None:
    backend = make_backend(tmp_path)
    async with backend:
        context = make_context()
        lease = await backend.leases.acquire(
            "resource", "owner-a", timedelta(seconds=30), context=context
        )
        assert lease is not None
        assert lease.fencing_token == 1

        blocked = await backend.leases.acquire(
            "resource", "owner-b", timedelta(seconds=30), context=context
        )
        assert blocked is None

        renewed = await backend.leases.renew(lease, timedelta(seconds=60), context=context)
        assert renewed.expires_at > lease.expires_at

        assert await backend.leases.release(renewed, context=context) is True

        with pytest.raises(HarborStorageLeaseError):
            await backend.leases.renew(renewed, timedelta(seconds=60), context=context)

        reacquired = await backend.leases.acquire(
            "resource", "owner-c", timedelta(seconds=30), context=context
        )
        assert reacquired is not None
        assert reacquired.fencing_token == 2


@pytest.mark.asyncio
async def test_concurrent_lease_acquisition_has_exactly_one_winner(
    tmp_path: Path,
) -> None:
    backend = make_backend(tmp_path)
    async with backend:
        context = make_context()
        contenders = await asyncio.gather(
            *(
                backend.leases.acquire(
                    "contended",
                    f"owner-{index}",
                    timedelta(seconds=30),
                    context=context,
                )
                for index in range(4)
            )
        )

        winners = [lease for lease in contenders if lease is not None]
        assert len(winners) == 1
        assert winners[0].fencing_token == 1


@pytest.mark.asyncio
async def test_expired_lease_cannot_be_renewed(tmp_path: Path) -> None:
    backend = make_backend(tmp_path)
    async with backend:
        context = make_context()
        lease = await backend.leases.acquire(
            "short", "owner", timedelta(milliseconds=1), context=context
        )
        assert lease is not None
        await asyncio.sleep(0.01)

        with pytest.raises(HarborStorageLeaseError):
            await backend.leases.renew(lease, timedelta(seconds=30), context=context)
