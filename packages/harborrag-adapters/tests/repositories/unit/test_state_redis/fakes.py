from __future__ import annotations

from typing import Any

from redis.exceptions import WatchError

from harborrag_adapters.repositories.errors import StorageErrorContext
from harborrag_adapters.repositories.state.redis.config import RedisStateConfig
from harborrag_adapters.repositories.state.redis.repository import RedisStateBackend
from harborrag_adapters.repositories.telemetry import RepositoryTelemetry
from harborrag_core.schemas.state import CheckpointRecord, WorkflowState
from harborrag_core.schemas.storage import StorageFamily, StorageOperationContext

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
