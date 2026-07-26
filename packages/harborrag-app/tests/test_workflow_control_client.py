"""Envelope behaviour of the Temporal-backed application service.

`AppService` is the app package's only translation layer between CLI/API calls
and the runtime client. Its contract is that every failure becomes a stable
`AppResponse` envelope rather than a raised exception, and that the runtime
client is created once and reused. Neither was covered.
"""

from __future__ import annotations

import asyncio

import pytest

from harborrag_app.workflow_control.client import AppService
from harborrag_runtime.temporal.identity import RuntimeWorkflowRef


class FakeRuntimeClient:
    """Records calls and can be told to fail a specific operation."""

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
        return RuntimeWorkflowRef(request.run_id, "wf-1", "execution-1")

    async def result(self, run_id: str):
        self._record("result", run_id)
        return {"discovered": 2, "processed": 2}

    async def get_status(self, run_id: str):
        self._record("get_status", run_id)
        return {"run_id": run_id, "status": "running"}

    async def execution_status(self, run_id: str) -> str:
        self._record("execution_status", run_id)
        return "running"

    async def get_progress(self, run_id: str):
        self._record("get_progress", run_id)
        return {"discovered": 2, "processed": 1}

    async def get_failed_artifacts(self, run_id: str):
        self._record("get_failed_artifacts", run_id)
        return ["doc-1"]

    async def get_quarantined_artifacts(self, run_id: str):
        self._record("get_quarantined_artifacts", run_id)
        return []

    async def get_pending_resolutions(self, run_id: str):
        self._record("get_pending_resolutions", run_id)
        return []

    async def pause(self, run_id: str) -> None:
        self._record("pause", run_id)

    async def resume(self, run_id: str) -> None:
        self._record("resume", run_id)

    async def cancel(self, run_id: str, *, graceful: bool) -> None:
        self._record("cancel", run_id, graceful)

    async def retry_failed(self, run_id: str, artifact_ids) -> None:
        self._record("retry_failed", run_id, tuple(artifact_ids))


class FakeComposition:
    def __init__(self, diagnostics: object) -> None:
        self._diagnostics = diagnostics
        self.closed = False

    def diagnostics(self):
        return self._diagnostics

    async def aclose(self) -> None:
        self.closed = True


def _service(
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

    service = AppService(
        FakeComposition(diagnostics if diagnostics is not None else {"runtime": {"ready": True}}),
        client_factory=factory,
    )
    return service, client, factory_calls


# --------------------------------------------------------------------------
# Composition-level health
# --------------------------------------------------------------------------


def test_health_reports_ready_from_composition_diagnostics() -> None:
    service, _, _ = _service()

    response = service.health()

    assert response.ok is True
    assert response.error is None
    assert response.data["diagnostics"] == {"runtime": {"ready": True}}


@pytest.mark.parametrize(
    "diagnostics",
    [{"runtime": {"ready": False}}, {"runtime": "not-a-mapping"}, {}],
)
def test_health_reports_not_ready_for_any_unusable_diagnostics(diagnostics: object) -> None:
    service, _, _ = _service(diagnostics=diagnostics)

    response = service.health()

    assert response.ok is False
    assert response.error == "runtime not ready"


def test_ingest_once_directs_the_caller_to_the_workflow_command() -> None:
    service, _, _ = _service()

    response = service.ingest_once()

    assert response.ok is False
    assert "ingest start" in str(response.error)


# --------------------------------------------------------------------------
# Runtime calls
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_runtime_health_reports_a_ready_target() -> None:
    service, _, _ = _service()

    response = await service.runtime_health()

    assert response.ok is True
    assert response.data["runtime"]["provider"] == "temporal"
    assert "target" in response.data["runtime"]


@pytest.mark.asyncio
async def test_start_ingestion_generates_identifiers_and_returns_the_reference() -> None:
    service, client, _ = _service()

    response = await service.start_ingestion(tenant_id="tenant-1", connector_name="local_file")

    assert response.ok is True
    assert response.data["run"]["tenant_id"] == "tenant-1"
    assert response.data["run"]["run_id"].startswith("ingestion-")
    assert response.data["workflow"]["workflow_id"] == "wf-1"
    assert "result" not in response.data
    assert [name for name, _ in client.calls] == ["start_ingestion"]


@pytest.mark.asyncio
async def test_start_ingestion_honours_explicit_identifiers_and_waiting() -> None:
    service, client, _ = _service()

    response = await service.start_ingestion(
        tenant_id="tenant-1",
        connector_name="local_file",
        run_id="run-explicit",
        manifest_id="manifest-explicit",
        generation_id="generation-explicit",
        max_artifacts=3,
        wait=True,
    )

    assert response.data["run"]["run_id"] == "run-explicit"
    assert response.data["run"]["manifest_id"] == "manifest-explicit"
    assert response.data["run"]["options"]["max_artifacts"] == 3
    assert response.data["result"]["processed"] == 2
    assert [name for name, _ in client.calls] == ["start_ingestion", "result"]


@pytest.mark.asyncio
async def test_ingestion_status_aggregates_every_query() -> None:
    service, client, _ = _service()

    response = await service.ingestion_status("run-1")

    assert response.ok is True
    assert response.data["failed_artifacts"] == ["doc-1"]
    assert response.data["quarantined_artifacts"] == []
    assert response.data["status"]["status"] == "running"
    assert response.data["execution_status"] == "running"
    assert len(client.calls) == 6


@pytest.mark.asyncio
async def test_ingestion_result_returns_the_run_summary() -> None:
    service, _, _ = _service()

    response = await service.ingestion_result("run-1")

    assert response.ok is True
    assert response.data["result"]["discovered"] == 2


# --------------------------------------------------------------------------
# Control actions
# --------------------------------------------------------------------------


@pytest.mark.parametrize("action", ["pause", "resume", "cancel"])
@pytest.mark.asyncio
async def test_control_actions_are_forwarded(action: str) -> None:
    service, client, _ = _service()

    response = await service.control_ingestion("run-1", action)

    assert response.ok is True
    assert response.data["action"] == action
    assert client.calls[0][0] == action


@pytest.mark.asyncio
async def test_retry_forwards_its_artifact_ids() -> None:
    service, client, _ = _service()

    response = await service.control_ingestion("run-1", "retry", artifact_ids=("a-1", "a-2"))

    assert response.ok is True
    assert response.data["artifact_ids"] == ["a-1", "a-2"]
    assert client.calls[0] == ("retry_failed", ("run-1", ("a-1", "a-2")))


@pytest.mark.asyncio
async def test_retry_without_artifacts_is_refused() -> None:
    service, _, _ = _service()

    response = await service.control_ingestion("run-1", "retry")

    assert response.ok is False
    assert response.data["error_type"] == "ValueError"
    assert "at least one artifact id" in str(response.error)


@pytest.mark.asyncio
async def test_an_unsupported_action_is_refused() -> None:
    service, _, _ = _service()

    response = await service.control_ingestion("run-1", "detonate")

    assert response.ok is False
    assert "unsupported ingestion action" in str(response.error)


# --------------------------------------------------------------------------
# Failure envelope and client reuse
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_runtime_failures_become_a_stable_envelope() -> None:
    service, _, _ = _service(FakeRuntimeClient(failure=RuntimeError("temporal is down")))

    for response in (
        await service.runtime_health(),
        await service.ingestion_status("run-1"),
        await service.ingestion_result("run-1"),
        await service.control_ingestion("run-1", "pause"),
        await service.start_ingestion(tenant_id="t", connector_name="c"),
    ):
        assert response.ok is False
        assert response.data["error_type"] == "RuntimeError"
        assert response.error == "RuntimeError"


@pytest.mark.asyncio
async def test_an_exception_without_a_message_still_names_its_type() -> None:
    service, _, _ = _service(FakeRuntimeClient(failure=RuntimeError()))

    response = await service.ingestion_result("run-1")

    assert response.error == "RuntimeError"


@pytest.mark.asyncio
async def test_the_runtime_client_is_created_once_and_reused() -> None:
    service, _, factory_calls = _service()

    await service.ingestion_result("run-1")
    await service.ingestion_result("run-2")
    await asyncio.gather(
        service.ingestion_result("run-3"),
        service.ingestion_result("run-4"),
    )

    assert len(factory_calls) == 1


@pytest.mark.asyncio
async def test_aclose_closes_the_composition_root() -> None:
    service, _, _ = _service()

    await service.aclose()

    assert service._composition.closed is True
