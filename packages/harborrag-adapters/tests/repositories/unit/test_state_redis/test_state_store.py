from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from harborrag_adapters.repositories.errors import (
    HarborStorageAlreadyExistsError,
    HarborStorageAuthorizationError,
    HarborStorageCheckpointConflictError,
)
from harborrag_adapters.repositories.state.redis.stores import RedisStateStore

from .fakes import (
    CONTEXT,
    FakeStateBackend,
    FakeStateClient,
    FakeStatePipeline,
    make_workflow_state,
)


@pytest.mark.asyncio
async def test_state_create_rejects_cross_tenant_state() -> None:
    client = FakeStateClient()
    store = RedisStateStore(FakeStateBackend(client))  # type: ignore[arg-type]
    state = make_workflow_state(tenant_id="tenant-b")

    with pytest.raises(HarborStorageAuthorizationError):
        await store.create(state, context=CONTEXT)

    assert client.set_calls == []


@pytest.mark.asyncio
async def test_state_create_success_sets_nx_true() -> None:
    client = FakeStateClient(set_return=True)
    store = RedisStateStore(FakeStateBackend(client))  # type: ignore[arg-type]
    state = make_workflow_state()

    created = await store.create(state, context=CONTEXT)

    assert created == state
    assert len(client.set_calls) == 1
    _key, _value, nx, exat = client.set_calls[0]
    assert nx is True
    assert exat is None


@pytest.mark.asyncio
async def test_state_create_with_expiry_computes_exat() -> None:
    client = FakeStateClient(set_return=True)
    store = RedisStateStore(FakeStateBackend(client))  # type: ignore[arg-type]
    expires_at = datetime.now(UTC) + timedelta(hours=1)
    state = make_workflow_state(expires_at=expires_at)

    await store.create(state, context=CONTEXT)

    _key, _value, _nx, exat = client.set_calls[0]
    assert exat == int(expires_at.timestamp())


@pytest.mark.asyncio
async def test_state_create_raises_when_already_exists() -> None:
    client = FakeStateClient(set_return=False)
    store = RedisStateStore(FakeStateBackend(client))  # type: ignore[arg-type]
    state = make_workflow_state()

    with pytest.raises(HarborStorageAlreadyExistsError):
        await store.create(state, context=CONTEXT)


@pytest.mark.asyncio
async def test_state_get_returns_parsed_state_when_found() -> None:
    state = make_workflow_state()
    client = FakeStateClient(get_return=state.model_dump_json())
    store = RedisStateStore(FakeStateBackend(client))  # type: ignore[arg-type]

    result = await store.get(state.workflow_id, context=CONTEXT)

    assert result is not None
    assert result.workflow_id == state.workflow_id


@pytest.mark.asyncio
async def test_state_get_returns_none_when_missing() -> None:
    client = FakeStateClient(get_return=None)
    store = RedisStateStore(FakeStateBackend(client))  # type: ignore[arg-type]

    result = await store.get("missing-workflow", context=CONTEXT)

    assert result is None


@pytest.mark.asyncio
async def test_state_save_rejects_cross_tenant_state() -> None:
    client = FakeStateClient()
    store = RedisStateStore(FakeStateBackend(client))  # type: ignore[arg-type]
    state = make_workflow_state(tenant_id="tenant-b")

    with pytest.raises(HarborStorageAuthorizationError):
        await store.save(state, expected_version=1, context=CONTEXT)


@pytest.mark.asyncio
async def test_state_save_succeeds_and_bumps_version() -> None:
    state = make_workflow_state(version=1)
    pipeline = FakeStatePipeline(get_return=state.model_dump_json())
    client = FakeStateClient(pipeline)
    store = RedisStateStore(FakeStateBackend(client))  # type: ignore[arg-type]

    saved = await store.save(state, expected_version=1, context=CONTEXT)

    assert saved.version == 2
    assert [name for name, _ in pipeline.commands] == ["multi", "set"]


@pytest.mark.asyncio
async def test_state_save_conflict_when_state_missing() -> None:
    state = make_workflow_state(version=1)
    pipeline = FakeStatePipeline(get_return=None)
    client = FakeStateClient(pipeline)
    store = RedisStateStore(FakeStateBackend(client))  # type: ignore[arg-type]

    with pytest.raises(HarborStorageCheckpointConflictError):
        await store.save(state, expected_version=1, context=CONTEXT)


@pytest.mark.asyncio
async def test_state_save_conflict_on_version_mismatch() -> None:
    state = make_workflow_state(version=1)
    existing = make_workflow_state(version=5)
    pipeline = FakeStatePipeline(get_return=existing.model_dump_json())
    client = FakeStateClient(pipeline)
    store = RedisStateStore(FakeStateBackend(client))  # type: ignore[arg-type]

    with pytest.raises(HarborStorageCheckpointConflictError):
        await store.save(state, expected_version=1, context=CONTEXT)


@pytest.mark.asyncio
async def test_state_save_conflict_on_watch_error() -> None:
    state = make_workflow_state(version=1)
    pipeline = FakeStatePipeline(get_return=state.model_dump_json(), watch_error=True)
    client = FakeStateClient(pipeline)
    store = RedisStateStore(FakeStateBackend(client))  # type: ignore[arg-type]

    with pytest.raises(HarborStorageCheckpointConflictError):
        await store.save(state, expected_version=1, context=CONTEXT)
