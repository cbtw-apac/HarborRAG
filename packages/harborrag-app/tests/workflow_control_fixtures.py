from __future__ import annotations

from harborrag_app.workflow_control.composition.factories import AppServiceFactories
from harborrag_app.workflow_control.composition.service import AppService
from harborrag_runtime.config.settings import RuntimeSettings
from harborrag_runtime.temporal.identity import RuntimeWorkflowRef
from harborrag_runtime.temporal.schemas import (
    ProcessingProfileInput,
    SourceIngestionInput,
    SourceIngestionStatus,
)
from harborrag_runtime.temporal.submission import SourceSubmission


class FakeRuntimeClient:
    """Record ingestion runtime calls and optionally fail every operation."""

    def __init__(self, *, failure: Exception | None = None) -> None:
        self.calls: list[tuple[str, tuple[object, ...]]] = []
        self._failure = failure

    def _record(self, name: str, *args: object) -> None:
        self.calls.append((name, args))
        if self._failure is not None:
            raise self._failure

    async def health(self) -> bool:
        self._record("health")
        return True

    async def start_ingestion(self, request):
        self._record("start_ingestion", request)
        return RuntimeWorkflowRef(request.task_id, "wf-1", "execution-1")

    async def result(self, run_id: str):
        self._record("result", run_id)
        return {"discovered": 2, "processed": 2}

    async def get_status(self, run_id: str):
        self._record("get_status", run_id)
        return SourceIngestionStatus(
            task_id=run_id,
            status="RUNNING",
            paused=False,
            cancel_requested=False,
        )

    async def execution_status(self, run_id: str) -> str:
        self._record("execution_status", run_id)
        return "running"

    async def get_progress(self, run_id: str):
        self._record("get_progress", run_id)
        return {"discovered": 2, "processed": 1}

    async def pause(self, run_id: str) -> None:
        self._record("pause", run_id)

    async def resume(self, run_id: str) -> None:
        self._record("resume", run_id)

    async def cancel(self, run_id: str) -> None:
        self._record("cancel", run_id)


class FakeComposition:
    mode = "test"

    def __init__(self, diagnostics: object) -> None:
        self._diagnostics = diagnostics
        self.closed = False

    def diagnostics(self):
        return self._diagnostics

    async def aclose(self) -> None:
        self.closed = True


class FakeTaskRegistry:
    def __init__(self) -> None:
        self.registered: list[SourceIngestionInput] = []

    async def register(self, source: SourceIngestionInput) -> None:
        self.registered.append(source)

    async def close(self) -> None:
        return None


def build_service(
    client: FakeRuntimeClient | None = None,
    *,
    diagnostics: object | None = None,
) -> tuple[AppService, FakeRuntimeClient, list[int]]:
    client = client or FakeRuntimeClient()
    factory_calls: list[int] = []

    async def factory(config):
        del config
        factory_calls.append(1)
        return client

    async def registry_factory(_settings: RuntimeSettings) -> FakeTaskRegistry:
        return FakeTaskRegistry()

    service = AppService(
        FakeComposition(diagnostics if diagnostics is not None else {"runtime": {"ready": True}}),
        factories=AppServiceFactories(
            client=factory,
            source_input_builder=source_input,
            task_registry=registry_factory,
        ),
    )
    return service, client, factory_calls


def source_input(
    _settings: RuntimeSettings,
    submission: SourceSubmission,
) -> SourceIngestionInput:
    return SourceIngestionInput(
        task_id=submission.task_id,
        tenant_id=submission.tenant_id,
        connector_name=submission.connector_name,
        connector_type="local",
        connection_id=submission.connection_id or submission.connector_name,
        source_scope_id=submission.source_scope_id or "scope-1",
        configuration_fingerprint="config-v1",
        processing=ProcessingProfileInput(
            parser_profile="parser-v1",
            normalizer_version="normalizer-v1",
            chunk_strategy="chunks-v1",
            dense_encoder_profile="dense-v1",
            sparse_encoder_profile="sparse-v1",
            graph_projection_version="graph-v1",
        ),
        query=submission.query,
        force_reprocess=submission.force_reprocess,
    )
