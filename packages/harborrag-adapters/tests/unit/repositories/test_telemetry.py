from __future__ import annotations

import logging
from typing import Any

import pytest

from harborrag_adapters.repositories.telemetry import (
    LoggingStorageTelemetryHook,
    NullStorageTelemetryHook,
    OperationTimer,
    RepositoryTelemetry,
    StorageTelemetryHook,
    traced_repository_operation,
)
from harborrag_core.schemas.storage import StorageFamily, StorageOperationContext
from harborrag_core.schemas.telemetry import (
    StorageOperationCompleted,
    StorageOperationFailed,
    StorageOperationStarted,
)


def make_context(tenant: str = "tenant-a") -> StorageOperationContext:
    return StorageOperationContext(tenant_id=tenant)


def make_started(operation: str = "op") -> StorageOperationStarted:
    return StorageOperationStarted(
        family=StorageFamily.DATABASE,
        backend="test",
        operation=operation,
        context=make_context(),
    )


class _RecordingHook(StorageTelemetryHook):
    def __init__(self) -> None:
        self.started: list[StorageOperationStarted] = []
        self.completed: list[StorageOperationCompleted] = []
        self.failed: list[StorageOperationFailed] = []

    async def on_operation_start(self, event: StorageOperationStarted) -> None:
        self.started.append(event)

    async def on_operation_end(self, event: StorageOperationCompleted) -> None:
        self.completed.append(event)

    async def on_operation_error(self, event: StorageOperationFailed) -> None:
        self.failed.append(event)


@pytest.mark.asyncio
async def test_null_storage_telemetry_hook_accepts_all_events_without_error() -> None:
    hook = NullStorageTelemetryHook()
    started = make_started()
    await hook.on_operation_start(started)
    await hook.on_operation_end(StorageOperationCompleted(**started.model_dump(), duration_ms=1.0))
    await hook.on_operation_error(
        StorageOperationFailed(
            **started.model_dump(),
            duration_ms=1.0,
            error_type="RuntimeError",
            retryable=False,
        )
    )


class _StubLogger:
    def __init__(self) -> None:
        self.debug_calls: list[tuple[Any, ...]] = []
        self.info_calls: list[tuple[Any, ...]] = []
        self.warning_calls: list[tuple[Any, ...]] = []

    def debug(self, *args: Any, **kwargs: Any) -> None:
        self.debug_calls.append((args, kwargs))

    def info(self, *args: Any, **kwargs: Any) -> None:
        self.info_calls.append((args, kwargs))

    def warning(self, *args: Any, **kwargs: Any) -> None:
        self.warning_calls.append((args, kwargs))


@pytest.mark.asyncio
async def test_logging_hook_uses_explicit_logger_and_emits_all_event_kinds() -> None:
    stub = _StubLogger()
    hook = LoggingStorageTelemetryHook(logger=stub)  # type: ignore[arg-type]
    started = make_started("write")

    await hook.on_operation_start(started)
    await hook.on_operation_end(
        StorageOperationCompleted(**started.model_dump(), duration_ms=12.5, attributes={"n": 1})
    )
    await hook.on_operation_error(
        StorageOperationFailed(
            **started.model_dump(),
            duration_ms=3.0,
            error_type="ValueError",
            retryable=True,
        )
    )

    assert len(stub.debug_calls) == 1
    assert stub.debug_calls[0][0][0] == "storage_operation_started"
    assert len(stub.info_calls) == 1
    assert stub.info_calls[0][0][0] == "storage_operation_completed"
    assert len(stub.warning_calls) == 1
    assert stub.warning_calls[0][0][0] == "storage_operation_failed"


@pytest.mark.asyncio
async def test_logging_hook_defaults_to_module_logger_when_none_supplied() -> None:
    hook = LoggingStorageTelemetryHook()
    assert isinstance(hook._logger, logging.Logger)
    assert hook._logger.name == "harborrag.storage"
    # Exercise the default-logger path end to end without requiring caplog wiring.
    await hook.on_operation_start(make_started())


@pytest.mark.asyncio
async def test_operation_timer_success_records_duration_and_attributes() -> None:
    hook = _RecordingHook()
    started = make_started("read")
    async with OperationTimer(hook, started) as timer:
        await timer.success(rows=3)
    assert len(hook.started) == 1
    assert len(hook.completed) == 1
    assert hook.completed[0].attributes == {"rows": 3}
    assert hook.failed == []


@pytest.mark.asyncio
async def test_operation_timer_failure_records_error_type_without_payload() -> None:
    hook = _RecordingHook()
    started = make_started("write")
    with pytest.raises(ValueError):
        async with OperationTimer(hook, started) as timer:
            try:
                raise ValueError("boom")
            except ValueError:
                await timer.failure(ValueError("boom"), retryable=True)
                raise
    assert len(hook.failed) == 1
    assert hook.failed[0].error_type == "ValueError"
    assert hook.failed[0].retryable is True
    assert hook.completed == []


@pytest.mark.asyncio
async def test_operation_timer_implicitly_succeeds_on_clean_exit() -> None:
    """When neither success() nor failure() is called explicitly and no exception
    propagates, __aexit__ must emit an implicit success event exactly once."""
    hook = _RecordingHook()
    started = make_started("implicit")
    async with OperationTimer(hook, started):
        pass
    assert len(hook.completed) == 1
    assert hook.failed == []


@pytest.mark.asyncio
async def test_operation_timer_implicitly_fails_on_propagating_exception() -> None:
    hook = _RecordingHook()
    started = make_started("implicit-fail")
    with pytest.raises(RuntimeError):
        async with OperationTimer(hook, started):
            raise RuntimeError("kaboom")
    assert len(hook.failed) == 1
    assert hook.completed == []


@pytest.mark.asyncio
async def test_operation_timer_does_not_double_emit_when_success_called_before_clean_exit() -> None:
    """Covers the __aexit__ branch where the block exits cleanly (exc is None) but
    self._finished is already True because success() was called manually inside the
    block -- the elif must be skipped rather than emitting a second completion."""
    hook = _RecordingHook()
    started = make_started("manual-success")
    async with OperationTimer(hook, started) as timer:
        await timer.success(manual=True)
        # Falling through here with exc=None and _finished=True must not re-emit.
    assert len(hook.completed) == 1
    assert hook.completed[0].attributes == {"manual": True}


def test_repository_telemetry_defaults_to_null_hook_when_none_supplied() -> None:
    telemetry = RepositoryTelemetry(None, family=StorageFamily.CACHE, backend="memory")
    assert isinstance(telemetry._hook, NullStorageTelemetryHook)


@pytest.mark.asyncio
async def test_repository_telemetry_operation_builds_started_event_from_family_and_backend() -> (
    None
):
    hook = _RecordingHook()
    telemetry = RepositoryTelemetry(hook, family=StorageFamily.VECTOR, backend="qdrant")
    context = make_context("tenant-z")
    async with telemetry.operation("search", context) as timer:
        await timer.success()
    assert len(hook.started) == 1
    event = hook.started[0]
    assert event.family == StorageFamily.VECTOR
    assert event.backend == "qdrant"
    assert event.operation == "search"
    assert event.context.tenant_id == "tenant-z"


class _Traced:
    def __init__(self, telemetry: RepositoryTelemetry | None) -> None:
        self._telemetry = telemetry
        self.calls = 0

    @traced_repository_operation("do_thing")
    async def do_thing(self, *, context: StorageOperationContext) -> str:
        del context
        self.calls += 1
        return "ok"


@pytest.mark.asyncio
async def test_traced_repository_operation_wraps_call_when_telemetry_and_context_present() -> None:
    hook = _RecordingHook()
    telemetry = RepositoryTelemetry(hook, family=StorageFamily.DATABASE, backend="sqlite")
    instance = _Traced(telemetry)
    result = await instance.do_thing(context=make_context())
    assert result == "ok"
    assert instance.calls == 1
    assert len(hook.started) == 1
    assert len(hook.completed) == 1


@pytest.mark.asyncio
async def test_traced_repository_operation_bypasses_telemetry_when_attribute_missing() -> None:
    instance = _Traced(None)
    result = await instance.do_thing(context=make_context())
    assert result == "ok"
    assert instance.calls == 1


@pytest.mark.asyncio
async def test_traced_repository_operation_bypasses_telemetry_when_context_missing_kwarg() -> None:
    hook = _RecordingHook()
    telemetry = RepositoryTelemetry(hook, family=StorageFamily.DATABASE, backend="sqlite")

    class _NoContextKwarg:
        def __init__(self, telemetry: RepositoryTelemetry) -> None:
            self._telemetry = telemetry

        @traced_repository_operation("no_context")
        async def run(self) -> str:
            return "ran"

    result = await _NoContextKwarg(telemetry).run()
    assert result == "ran"
    assert hook.started == []
