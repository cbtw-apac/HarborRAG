from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from collections.abc import Awaitable, Callable
from functools import wraps
from time import perf_counter
from typing import Any, TypeVar, cast

from harborrag_core.schemas.storage import StorageFamily, StorageOperationContext
from harborrag_core.schemas.telemetry import (
    StorageOperationCompleted,
    StorageOperationFailed,
    StorageOperationStarted,
)


class StorageTelemetryHook(ABC):
    """Defines provider-neutral telemetry callbacks for repository operations."""

    @abstractmethod
    async def on_operation_start(self, event: StorageOperationStarted) -> None:
        """Handle the start of a sanitized storage operation."""

    @abstractmethod
    async def on_operation_end(self, event: StorageOperationCompleted) -> None:
        """Handle successful completion of a storage operation."""

    @abstractmethod
    async def on_operation_error(self, event: StorageOperationFailed) -> None:
        """Handle a failed storage operation without sensitive payloads."""


class NullStorageTelemetryHook(StorageTelemetryHook):
    """Accepts telemetry events without exporting them."""

    async def on_operation_start(self, event: StorageOperationStarted) -> None:
        del event

    async def on_operation_end(self, event: StorageOperationCompleted) -> None:
        del event

    async def on_operation_error(self, event: StorageOperationFailed) -> None:
        del event


class LoggingStorageTelemetryHook(StorageTelemetryHook):
    """Exports sanitized storage telemetry through structured Python logging."""

    def __init__(self, logger: logging.Logger | None = None) -> None:
        self._logger = logger or logging.getLogger("harborrag.storage")

    async def on_operation_start(self, event: StorageOperationStarted) -> None:
        self._logger.debug(
            "storage_operation_started",
            extra={
                "family": event.family.value,
                "backend": event.backend,
                "operation": event.operation,
                "tenant_id": str(event.context.tenant_id),
                "request_id": event.context.request_id,
                "trace_id": event.context.trace_id,
            },
        )

    async def on_operation_end(self, event: StorageOperationCompleted) -> None:
        self._logger.info(
            "storage_operation_completed",
            extra={
                "family": event.family.value,
                "backend": event.backend,
                "operation": event.operation,
                "duration_ms": event.duration_ms,
                **event.attributes,
            },
        )

    async def on_operation_error(self, event: StorageOperationFailed) -> None:
        self._logger.warning(
            "storage_operation_failed",
            extra={
                "family": event.family.value,
                "backend": event.backend,
                "operation": event.operation,
                "duration_ms": event.duration_ms,
                "error_type": event.error_type,
                "retryable": event.retryable,
                **event.attributes,
            },
        )


class OperationTimer:
    """Emits start, success, and failure events around one storage operation."""

    def __init__(
        self,
        hook: StorageTelemetryHook,
        started: StorageOperationStarted,
    ) -> None:
        self._hook = hook
        self._started = started
        self._clock = 0.0
        self._finished = False

    async def __aenter__(self) -> OperationTimer:
        self._clock = perf_counter()
        await self._hook.on_operation_start(self._started)
        return self

    async def success(self, **attributes: Any) -> None:
        """Emit a successful completion event with sanitized attributes."""
        self._finished = True
        await self._hook.on_operation_end(
            StorageOperationCompleted(
                **self._started.model_dump(),
                duration_ms=(perf_counter() - self._clock) * 1000,
                attributes=attributes,
            )
        )

    async def failure(self, exc: Exception, *, retryable: bool = False) -> None:
        """Emit a failure event without serializing exception payloads."""
        self._finished = True
        await self._hook.on_operation_error(
            StorageOperationFailed(
                **self._started.model_dump(),
                duration_ms=(perf_counter() - self._clock) * 1000,
                error_type=type(exc).__name__,
                retryable=retryable,
            )
        )

    async def __aexit__(self, exc_type: object, exc: object, traceback: object) -> None:
        del exc_type, traceback
        if isinstance(exc, Exception) and not self._finished:
            await self.failure(exc)
        elif exc is None and not self._finished:
            await self.success()


class RepositoryTelemetry:
    """Builds sanitized operation timers for one concrete repository backend."""

    def __init__(
        self,
        hook: StorageTelemetryHook | None,
        *,
        family: StorageFamily,
        backend: str,
    ) -> None:
        self._hook = hook or NullStorageTelemetryHook()
        self._family = family
        self._backend = backend

    def operation(
        self,
        name: str,
        context: StorageOperationContext,
    ) -> OperationTimer:
        return OperationTimer(
            self._hook,
            StorageOperationStarted(
                family=self._family,
                backend=self._backend,
                operation=name,
                context=context,
            ),
        )


_AsyncCallable = TypeVar("_AsyncCallable", bound=Callable[..., Awaitable[Any]])


def traced_repository_operation(
    operation: str,
) -> Callable[[_AsyncCallable], _AsyncCallable]:
    """Emit telemetry around an async method with a keyword-only context."""

    def decorate(function: _AsyncCallable) -> _AsyncCallable:
        @wraps(function)
        async def wrapped(*args: Any, **kwargs: Any) -> Any:
            emitter = getattr(args[0], "_telemetry", None) if args else None
            context = kwargs.get("context")
            if not isinstance(emitter, RepositoryTelemetry) or not isinstance(
                context, StorageOperationContext
            ):
                return await function(*args, **kwargs)
            async with emitter.operation(operation, context):
                return await function(*args, **kwargs)

        return cast(_AsyncCallable, wrapped)

    return decorate
