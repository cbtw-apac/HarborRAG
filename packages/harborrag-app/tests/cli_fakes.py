"""Test doubles for the CLI kernel: the operations port with canned answers."""

from __future__ import annotations

from collections.abc import AsyncIterator, Awaitable, Callable, Mapping
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from dataclasses import dataclass
from typing import Any

from harborrag_app.operations.contract import OPERATIONS
from harborrag_runtime.errors import RuntimeConnectionError


@dataclass(frozen=True, slots=True)
class Call:
    """One operation call a command made.

    Args:
        operation_id: The port method called.
        args: Its positional arguments.
        kwargs: Its keyword arguments.
    """

    operation_id: str
    args: tuple[Any, ...]
    kwargs: dict[str, Any]


class FakeOperations:
    """The operations port answering canned bodies, or raising the error set for an operation.

    Every operationId of ``OPERATIONS`` is a method; calling one without a canned body or
    error fails the test with ``KeyError``.

    Args:
        results: Body each operation returns.
        errors: Exception each operation raises instead.
        has_data: What ``tenant_has_data`` answers.
    """

    def __init__(
        self,
        *,
        results: Mapping[str, object] | None = None,
        errors: Mapping[str, BaseException] | None = None,
        has_data: bool = True,
    ) -> None:
        self.results = dict(results or {})
        self.errors = dict(errors or {})
        self.has_data = has_data
        self.calls: list[Call] = []
        self.tenant_checks: list[str] = []

    def __getattr__(self, name: str) -> Callable[..., Awaitable[object]]:
        if name not in OPERATIONS:
            raise AttributeError(name)

        async def operation(*args: Any, **kwargs: Any) -> object:
            self.calls.append(Call(name, args, kwargs))
            if name in self.errors:
                raise self.errors[name]
            return self.results[name]

        return operation

    async def tenant_has_data(self, tenant_id: str) -> bool:
        self.tenant_checks.append(tenant_id)
        return self.has_data


def serving(
    operations: FakeOperations,
) -> Callable[[], AbstractAsyncContextManager[FakeOperations]]:
    """Return a stand-in for ``invoke.local_operations`` that yields ``operations``."""

    @asynccontextmanager
    async def local_operations() -> AsyncIterator[FakeOperations]:
        yield operations

    return local_operations


def raised_in_temporal_client() -> RuntimeConnectionError:
    """A connection error raised with a ``temporalio.client`` frame on its traceback."""
    namespace: dict[str, object] = {
        "__name__": "temporalio.client",
        "error": RuntimeConnectionError,
    }
    exec(compile("def connect():\n    raise error('refused')", "client.py", "exec"), namespace)  # noqa: S102
    connect = namespace["connect"]
    assert callable(connect)
    try:
        connect()
    except RuntimeConnectionError as exc:
        return exc
    raise AssertionError("connect did not raise")
