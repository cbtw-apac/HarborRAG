"""Worker process bootstrap: provider loading, group parsing, signal handling.

`harborrag_runtime.temporal.worker` is the entry point for the deployed worker
container. Everything here runs before a Temporal connection exists, so it is
testable in-process, and every failure path is a startup misconfiguration that
should surface as a `WorkerStartupError` rather than an obscure traceback.
"""

from __future__ import annotations

import asyncio
import signal

import pytest

from harborrag_runtime.errors import WorkerStartupError
from harborrag_runtime.temporal import worker as worker_module
from harborrag_runtime.temporal.dependencies import RuntimeDependencies
from harborrag_runtime.temporal.workers import WorkerGroup

# --------------------------------------------------------------------------
# Dependency provider loading
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "path",
    ["no-separator", ":missing_module", "module_only:", ""],
)
def test_provider_path_must_use_module_callable_syntax(path: str) -> None:
    with pytest.raises(WorkerStartupError, match="module:callable syntax"):
        worker_module._load_provider(path)


def test_provider_path_must_resolve_to_an_importable_module() -> None:
    with pytest.raises(WorkerStartupError, match="Could not load runtime dependency provider"):
        worker_module._load_provider("harborrag_runtime_does_not_exist:provider")


def test_provider_path_must_name_an_existing_attribute() -> None:
    with pytest.raises(WorkerStartupError, match="Could not load runtime dependency provider"):
        worker_module._load_provider("harborrag_runtime.temporal.worker:not_an_attribute")


def test_provider_must_be_callable() -> None:
    with pytest.raises(WorkerStartupError, match="is not callable"):
        worker_module._load_provider("harborrag_runtime.temporal.worker:logger")


def test_a_valid_provider_path_resolves() -> None:
    provider = worker_module._load_provider("harborrag_runtime.temporal.worker:_load_provider")

    assert provider is worker_module._load_provider


# --------------------------------------------------------------------------
# Signal handling
# --------------------------------------------------------------------------


def test_shutdown_signals_set_the_stop_event_and_are_restored() -> None:
    async def scenario() -> None:
        loop = asyncio.get_running_loop()
        stop_event = asyncio.Event()

        previous = worker_module._install_shutdown_signal_handlers(loop, stop_event)

        assert set(previous) <= {signal.SIGINT, signal.SIGTERM}
        if signal.SIGTERM in previous:
            loop.call_soon(stop_event.set)
            await asyncio.wait_for(stop_event.wait(), timeout=1)

        worker_module._restore_signal_handlers(loop, previous)

    asyncio.run(scenario())


def test_signal_installation_tolerates_an_unsupported_loop(monkeypatch) -> None:
    """Windows and some embedded loops cannot add signal handlers at all."""

    class _NoSignalLoop:
        def add_signal_handler(self, *args: object) -> None:
            raise NotImplementedError

    previous = worker_module._install_shutdown_signal_handlers(
        _NoSignalLoop(),  # type: ignore[arg-type]
        asyncio.Event(),
    )

    assert previous == {}


# --------------------------------------------------------------------------
# Configured bootstrap
# --------------------------------------------------------------------------


class _Recorder:
    def __init__(self) -> None:
        self.calls: list[tuple[object, ...]] = []

    async def __call__(self, config, dependencies, groups, *, stop_event=None) -> None:
        self.calls.append((config, dependencies, groups, stop_event))


@pytest.fixture
def dependencies() -> RuntimeDependencies:
    """A structurally valid dependency set; the bootstrap only type-checks it."""
    return RuntimeDependencies(
        connectors={"local": object()},  # type: ignore[dict-item]
        parser=object(),  # type: ignore[arg-type]
        normalizer=object(),  # type: ignore[arg-type]
        chunker=object(),  # type: ignore[arg-type]
        chunk_persistence=object(),  # type: ignore[arg-type]
        indexer=object(),  # type: ignore[arg-type]
        state=object(),  # type: ignore[arg-type]
    )


def _run_configured(monkeypatch, recorder: _Recorder) -> None:
    monkeypatch.setattr(worker_module, "run_configured_workers", recorder)


@pytest.mark.asyncio
async def test_configured_main_uses_a_custom_sync_provider(
    monkeypatch,
    dependencies: RuntimeDependencies,
) -> None:
    recorder = _Recorder()
    _run_configured(monkeypatch, recorder)
    monkeypatch.setenv("HARBORRAG_TEMPORAL_DEPENDENCY_PROVIDER", "test:provider")
    monkeypatch.setenv("HARBORRAG_TEMPORAL_WORKER_GROUPS", "discovery")
    monkeypatch.setattr(worker_module, "_load_provider", lambda path: lambda s: dependencies)

    await worker_module._configured_main()

    assert len(recorder.calls) == 1
    assert recorder.calls[0][1] is dependencies
    assert recorder.calls[0][2] == (WorkerGroup("discovery"),)


@pytest.mark.asyncio
async def test_configured_main_awaits_an_async_provider(
    monkeypatch,
    dependencies: RuntimeDependencies,
) -> None:
    recorder = _Recorder()
    _run_configured(monkeypatch, recorder)
    monkeypatch.setenv("HARBORRAG_TEMPORAL_DEPENDENCY_PROVIDER", "test:provider")
    monkeypatch.setenv("HARBORRAG_TEMPORAL_WORKER_GROUPS", "discovery")

    async def _async_provider(settings):
        del settings
        return dependencies

    monkeypatch.setattr(worker_module, "_load_provider", lambda path: _async_provider)

    await worker_module._configured_main()

    assert recorder.calls[0][1] is dependencies


@pytest.mark.asyncio
async def test_configured_main_parses_multiple_worker_groups(
    monkeypatch,
    dependencies: RuntimeDependencies,
) -> None:
    recorder = _Recorder()
    _run_configured(monkeypatch, recorder)
    monkeypatch.setenv("HARBORRAG_TEMPORAL_DEPENDENCY_PROVIDER", "test:provider")
    monkeypatch.setenv("HARBORRAG_TEMPORAL_WORKER_GROUPS", " discovery , processing ")
    monkeypatch.setattr(worker_module, "_load_provider", lambda path: lambda s: dependencies)

    await worker_module._configured_main()

    assert recorder.calls[0][2] == (WorkerGroup("discovery"), WorkerGroup("processing"))


@pytest.mark.asyncio
async def test_configured_main_rejects_a_provider_returning_the_wrong_type(
    monkeypatch,
) -> None:
    _run_configured(monkeypatch, _Recorder())
    monkeypatch.setenv("HARBORRAG_TEMPORAL_DEPENDENCY_PROVIDER", "test:provider")
    monkeypatch.setattr(worker_module, "_load_provider", lambda path: lambda s: {"not": "deps"})

    with pytest.raises(WorkerStartupError, match="returned an invalid value"):
        await worker_module._configured_main()


@pytest.mark.asyncio
async def test_configured_main_rejects_an_unknown_worker_group(
    monkeypatch,
    dependencies: RuntimeDependencies,
) -> None:
    _run_configured(monkeypatch, _Recorder())
    monkeypatch.setenv("HARBORRAG_TEMPORAL_DEPENDENCY_PROVIDER", "test:provider")
    monkeypatch.setenv("HARBORRAG_TEMPORAL_WORKER_GROUPS", "not-a-real-group")
    monkeypatch.setattr(worker_module, "_load_provider", lambda path: lambda s: dependencies)

    with pytest.raises(WorkerStartupError, match="worker group configuration is invalid"):
        await worker_module._configured_main()


@pytest.mark.asyncio
async def test_configured_main_requires_at_least_one_group(
    monkeypatch,
    dependencies: RuntimeDependencies,
) -> None:
    _run_configured(monkeypatch, _Recorder())
    monkeypatch.setenv("HARBORRAG_TEMPORAL_DEPENDENCY_PROVIDER", "test:provider")
    monkeypatch.setenv("HARBORRAG_TEMPORAL_WORKER_GROUPS", " , ")
    monkeypatch.setattr(worker_module, "_load_provider", lambda path: lambda s: dependencies)

    with pytest.raises(WorkerStartupError, match="At least one Temporal worker group"):
        await worker_module._configured_main()
