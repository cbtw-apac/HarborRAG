from __future__ import annotations

import pytest

from harborrag_adapters.repositories.errors import (
    HarborStorageAuthorizationError,
    HarborStorageCheckpointConflictError,
)
from harborrag_adapters.repositories.state.redis.stores import RedisCheckpointStore

from .fakes import CONTEXT, FakeStateBackend, FakeStateClient, FakeStatePipeline, make_checkpoint


@pytest.mark.asyncio
async def test_checkpoint_load_latest_found() -> None:
    checkpoint = make_checkpoint()
    client = FakeStateClient(get_return=checkpoint.model_dump_json())
    store = RedisCheckpointStore(FakeStateBackend(client))  # type: ignore[arg-type]

    result = await store.load_latest(checkpoint.workflow_id, context=CONTEXT)

    assert result is not None
    assert result.id == checkpoint.id


@pytest.mark.asyncio
async def test_checkpoint_load_latest_missing() -> None:
    client = FakeStateClient(get_return=None)
    store = RedisCheckpointStore(FakeStateBackend(client))  # type: ignore[arg-type]

    result = await store.load_latest("wf-1", context=CONTEXT)

    assert result is None


@pytest.mark.asyncio
async def test_checkpoint_save_rejects_cross_tenant() -> None:
    client = FakeStateClient()
    store = RedisCheckpointStore(FakeStateBackend(client))  # type: ignore[arg-type]
    checkpoint = make_checkpoint(tenant_id="tenant-b")

    with pytest.raises(HarborStorageAuthorizationError):
        await store.save(checkpoint, expected_version=None, context=CONTEXT)


@pytest.mark.asyncio
async def test_checkpoint_save_first_checkpoint_succeeds() -> None:
    checkpoint = make_checkpoint(state_version=1)
    pipeline = FakeStatePipeline(get_return=None)
    client = FakeStateClient(pipeline)
    store = RedisCheckpointStore(FakeStateBackend(client))  # type: ignore[arg-type]

    saved = await store.save(checkpoint, expected_version=None, context=CONTEXT)

    assert saved == checkpoint
    assert [name for name, _ in pipeline.commands] == ["multi", "set", "rpush"]


@pytest.mark.asyncio
async def test_checkpoint_save_conflict_on_version_mismatch() -> None:
    checkpoint = make_checkpoint(state_version=3)
    existing = make_checkpoint(state_version=1)
    pipeline = FakeStatePipeline(get_return=existing.model_dump_json())
    client = FakeStateClient(pipeline)
    store = RedisCheckpointStore(FakeStateBackend(client))  # type: ignore[arg-type]

    with pytest.raises(HarborStorageCheckpointConflictError):
        await store.save(checkpoint, expected_version=1, context=CONTEXT)


@pytest.mark.asyncio
async def test_checkpoint_save_conflict_on_watch_error() -> None:
    checkpoint = make_checkpoint(state_version=1)
    pipeline = FakeStatePipeline(get_return=None, watch_error=True)
    client = FakeStateClient(pipeline)
    store = RedisCheckpointStore(FakeStateBackend(client))  # type: ignore[arg-type]

    with pytest.raises(HarborStorageCheckpointConflictError):
        await store.save(checkpoint, expected_version=None, context=CONTEXT)
