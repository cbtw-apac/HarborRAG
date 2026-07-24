from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from redis.exceptions import WatchError

from harborrag_adapters.repositories.errors import (
    HarborStorageAlreadyExistsError,
    HarborStorageAuthorizationError,
    HarborStorageCheckpointConflictError,
    HarborStorageLeaseError,
    StorageErrorContext,
)
from harborrag_adapters.repositories.state.redis.config import RedisStateConfig
from harborrag_adapters.repositories.state.redis.repository import RedisStateBackend
from harborrag_adapters.repositories.state.redis.stores import (
    RedisCheckpointStore,
    RedisLeaseStore,
    RedisStateStore,
)
from harborrag_adapters.repositories.telemetry import RepositoryTelemetry
from harborrag_core.schemas.state import CheckpointRecord, LeaseRecord, WorkflowState
from harborrag_core.schemas.storage import HealthStatus, StorageFamily, StorageOperationContext

CONTEXT = StorageOperationContext(tenant_id="tenant-a")


class FakeStatePipeline:
    def __init__(
        self,
        *,
        get_return: str | None = None,
        execute_return: list[Any] | None = None,
        watch_error: bool = False,
    ) -> None:
        self.commands: list[tuple[str, tuple[Any, ...]]] = []
        self._get_return = get_return
        self._execute_return = execute_return if execute_return is not None else [1]
        self._watch_error = watch_error
        self.watched: list[str] = []

    async def __aenter__(self) -> FakeStatePipeline:
        return self

    async def __aexit__(self, *args: Any) -> None:
        del args

    async def watch(self, *keys: str) -> None:
        self.watched.extend(keys)

    async def get(self, key: str) -> str | None:
        del key
        return self._get_return

    def multi(self) -> None:
        self.commands.append(("multi", ()))

    def set(self, *args: Any, **kwargs: Any) -> None:
        self.commands.append(("set", (*args, kwargs)))

    def rpush(self, *args: Any) -> None:
        self.commands.append(("rpush", args))

    async def execute(self) -> list[Any]:
        if self._watch_error:
            raise WatchError("conflict")
        return self._execute_return


class FakeStateClient:
    def __init__(
        self,
        pipeline: FakeStatePipeline | None = None,
        *,
        set_return: bool = True,
        get_return: str | None = None,
        eval_results: list[Any] | None = None,
    ) -> None:
        self._pipeline = pipeline
        self.set_return = set_return
        self.get_return = get_return
        self.set_calls: list[tuple[Any, ...]] = []
        self.get_calls: list[str] = []
        self._eval_results = list(eval_results) if eval_results else [0]
        self.eval_calls: list[tuple[Any, ...]] = []

    def pipeline(self, *, transaction: bool) -> FakeStatePipeline:
        assert transaction is True
        assert self._pipeline is not None
        return self._pipeline

    async def set(self, key: str, value: str, *, nx: bool = False, exat: int | None = None) -> bool:
        self.set_calls.append((key, value, nx, exat))
        return self.set_return

    async def get(self, key: str) -> str | None:
        self.get_calls.append(key)
        return self.get_return

    async def eval(self, script: str, numkeys: int, *args: Any) -> Any:
        self.eval_calls.append((script, numkeys, args))
        if len(self._eval_results) > 1:
            return self._eval_results.pop(0)
        return self._eval_results[0]


class FakeStateBackend:
    def __init__(self, client: FakeStateClient) -> None:
        self.client = client
        self._telemetry = RepositoryTelemetry(None, family=StorageFamily.STATE, backend="redis")

    @staticmethod
    def key(context: StorageOperationContext, *parts: str) -> str:
        return ":".join([str(context.tenant_id), *parts])

    @staticmethod
    def error_context(
        operation: str, context: StorageOperationContext, resource: str
    ) -> StorageErrorContext:
        return StorageErrorContext(
            family=StorageFamily.STATE,
            backend="redis",
            instance_name="test",
            operation=operation,
            tenant_id=str(context.tenant_id),
            resource_name=resource,
        )


def make_workflow_state(**overrides: Any) -> WorkflowState:
    defaults: dict[str, Any] = {
        "workflow_id": "wf-1",
        "tenant_id": "tenant-a",
        "version": 1,
    }
    defaults.update(overrides)
    return WorkflowState(**defaults)


def make_checkpoint(**overrides: Any) -> CheckpointRecord:
    defaults: dict[str, Any] = {
        "id": "cp-1",
        "workflow_id": "wf-1",
        "tenant_id": "tenant-a",
        "step_name": "step-a",
        "state_version": 1,
    }
    defaults.update(overrides)
    return CheckpointRecord(**defaults)


# ---------------------------------------------------------------------------
# RedisStateStore
# ---------------------------------------------------------------------------


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


# ---------------------------------------------------------------------------
# RedisCheckpointStore
# ---------------------------------------------------------------------------


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


# ---------------------------------------------------------------------------
# RedisLeaseStore
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_lease_acquire_returns_lease_when_available() -> None:
    client = FakeStateClient(eval_results=[9])
    store = RedisLeaseStore(FakeStateBackend(client))  # type: ignore[arg-type]

    lease = await store.acquire("resource", "owner", timedelta(seconds=30), context=CONTEXT)

    assert lease is not None
    assert lease.fencing_token == 9
    assert lease.resource == "resource"


@pytest.mark.asyncio
async def test_lease_acquire_returns_none_when_unavailable() -> None:
    client = FakeStateClient(eval_results=[0])
    store = RedisLeaseStore(FakeStateBackend(client))  # type: ignore[arg-type]

    lease = await store.acquire("resource", "owner", timedelta(seconds=30), context=CONTEXT)

    assert lease is None


@pytest.mark.asyncio
async def test_lease_acquire_rejects_non_positive_duration() -> None:
    client = FakeStateClient(eval_results=[1])
    store = RedisLeaseStore(FakeStateBackend(client))  # type: ignore[arg-type]

    with pytest.raises(ValueError, match="lease_duration must be positive"):
        await store.acquire("resource", "owner", timedelta(0), context=CONTEXT)


@pytest.mark.asyncio
async def test_lease_renew_succeeds() -> None:
    client = FakeStateClient(eval_results=[1])
    store = RedisLeaseStore(FakeStateBackend(client))  # type: ignore[arg-type]
    lease = LeaseRecord(
        resource="resource",
        owner_token="owner",
        fencing_token=1,
        acquired_at=datetime.now(UTC),
        expires_at=datetime.now(UTC),
    )

    renewed = await store.renew(lease, timedelta(seconds=60), context=CONTEXT)

    assert renewed.expires_at > lease.expires_at


@pytest.mark.asyncio
async def test_lease_renew_rejects_non_positive_duration() -> None:
    client = FakeStateClient(eval_results=[1])
    store = RedisLeaseStore(FakeStateBackend(client))  # type: ignore[arg-type]
    lease = LeaseRecord(
        resource="resource",
        owner_token="owner",
        fencing_token=1,
        acquired_at=datetime.now(UTC),
        expires_at=datetime.now(UTC),
    )

    with pytest.raises(ValueError, match="lease_duration must be positive"):
        await store.renew(lease, timedelta(0), context=CONTEXT)


@pytest.mark.asyncio
async def test_lease_renew_raises_lease_error_when_not_owned() -> None:
    client = FakeStateClient(eval_results=[0])
    store = RedisLeaseStore(FakeStateBackend(client))  # type: ignore[arg-type]
    lease = LeaseRecord(
        resource="resource",
        owner_token="owner",
        fencing_token=1,
        acquired_at=datetime.now(UTC),
        expires_at=datetime.now(UTC),
    )

    with pytest.raises(HarborStorageLeaseError):
        await store.renew(lease, timedelta(seconds=30), context=CONTEXT)


@pytest.mark.asyncio
async def test_lease_release_true_and_false() -> None:
    lease = LeaseRecord(
        resource="resource",
        owner_token="owner",
        fencing_token=1,
        acquired_at=datetime.now(UTC),
        expires_at=datetime.now(UTC),
    )

    released_true = await RedisLeaseStore(
        FakeStateBackend(FakeStateClient(eval_results=[1]))  # type: ignore[arg-type]
    ).release(lease, context=CONTEXT)
    released_false = await RedisLeaseStore(
        FakeStateBackend(FakeStateClient(eval_results=[0]))  # type: ignore[arg-type]
    ).release(lease, context=CONTEXT)

    assert released_true is True
    assert released_false is False


# ---------------------------------------------------------------------------
# RedisStateBackend (composition, lifecycle, key helpers)
# ---------------------------------------------------------------------------


class FakeDatabaseClient:
    def __init__(self, *, connected: bool = False, ping_error: Exception | None = None) -> None:
        self._connected = connected
        self.connect_calls = 0
        self.close_calls = 0
        self.ping_calls = 0
        self._ping_error = ping_error

    @property
    def backend(self) -> str:
        return "redis"

    @property
    def is_connected(self) -> bool:
        return self._connected

    @property
    def raw(self) -> Any:
        if not self._connected:
            raise RuntimeError("Redis database client is not connected")
        return self

    async def connect(self) -> None:
        self.connect_calls += 1
        self._connected = True

    async def close(self) -> None:
        self.close_calls += 1
        self._connected = False

    async def ping(self) -> None:
        self.ping_calls += 1
        if self._ping_error is not None:
            raise self._ping_error


def make_backend(fake_client: FakeDatabaseClient) -> RedisStateBackend:
    config = RedisStateConfig(url="redis://localhost:6379/0", instance_name="primary")
    return RedisStateBackend(config, client=fake_client)  # type: ignore[arg-type]


def test_state_backend_client_property_delegates_to_database_raw() -> None:
    fake_client = FakeDatabaseClient(connected=True)
    backend = make_backend(fake_client)

    assert backend.client is fake_client


@pytest.mark.asyncio
async def test_state_backend_connect_and_close_delegate() -> None:
    fake_client = FakeDatabaseClient(connected=False)
    backend = make_backend(fake_client)

    await backend.connect()
    assert fake_client.connect_calls == 1

    await backend.close()
    assert fake_client.close_calls == 1


@pytest.mark.asyncio
async def test_state_backend_health_unknown_when_not_connected() -> None:
    backend = make_backend(FakeDatabaseClient(connected=False))

    health = await backend.health()

    assert health.status == HealthStatus.UNKNOWN
    assert health.backend == "redis"
    assert health.instance_name == "primary"


@pytest.mark.asyncio
async def test_state_backend_health_healthy_when_connected_and_ping_ok() -> None:
    backend = make_backend(FakeDatabaseClient(connected=True))

    health = await backend.health()

    assert health.status == HealthStatus.HEALTHY


def test_state_backend_key_joins_prefix_tenant_and_escaped_parts() -> None:
    backend = make_backend(FakeDatabaseClient())

    key = backend.key(CONTEXT, "workflow", "wf:1")

    assert key == "harborrag:v1:state:tenant-a:workflow:wf%3A1"


def test_state_backend_error_context_includes_tenant_and_resource() -> None:
    backend = make_backend(FakeDatabaseClient())

    error_context = backend.error_context("state_get", CONTEXT, "wf-1")

    assert error_context.operation == "state_get"
    assert error_context.tenant_id == "tenant-a"
    assert error_context.resource_name == "wf-1"
    assert error_context.instance_name == "primary"
    assert error_context.family == StorageFamily.STATE
