"""Connection construction and failure translation for the Temporal client.

`TemporalRuntimeClient.connect` is where the S4 TLS policy becomes actual SDK
arguments, and where transport failures are translated into HarborRAG's stable
error types instead of leaking `temporalio` exceptions to the app layer.
"""

from __future__ import annotations

from typing import Any

import pytest
from temporalio.service import RPCError, RPCStatusCode

from harborrag_runtime.config.temporal import (
    TemporalConnectionConfig,
    TemporalRuntimeConfig,
    TemporalTLSConfig,
)
from harborrag_runtime.errors import RuntimeConnectionError, WorkflowSubmissionError
from harborrag_runtime.temporal import client as client_module
from harborrag_runtime.temporal.client import TemporalRuntimeClient
from harborrag_runtime.temporal.schemas import IngestionRunInput


def _rpc_error() -> RPCError:
    return RPCError("unavailable", RPCStatusCode.UNAVAILABLE, b"")


def _input() -> IngestionRunInput:
    return IngestionRunInput(
        run_id="run-1",
        tenant_id="tenant-1",
        connector_name="local",
        manifest_id="manifest-1",
        generation_id="generation-1",
    )


class _StubConnect:
    """Capture the keyword arguments handed to `Client.connect`."""

    def __init__(self, *, error: Exception | None = None) -> None:
        self.kwargs: dict[str, Any] = {}
        self.target: str | None = None
        self._error = error

    async def __call__(self, target: str, **kwargs: Any) -> object:
        self.target = target
        self.kwargs = kwargs
        if self._error is not None:
            raise self._error
        return object()


@pytest.mark.asyncio
async def test_connect_passes_plaintext_configuration_without_tls(monkeypatch) -> None:
    stub = _StubConnect()
    monkeypatch.setattr(client_module.Client, "connect", stub)
    config = TemporalRuntimeConfig()

    connected = await TemporalRuntimeClient.connect(config)

    assert isinstance(connected, TemporalRuntimeClient)
    assert stub.target == config.connection.target
    assert stub.kwargs["namespace"] == config.connection.namespace
    assert stub.kwargs["tls"] is None


@pytest.mark.asyncio
async def test_connect_builds_a_tls_config_when_tls_is_enabled(monkeypatch) -> None:
    stub = _StubConnect()
    monkeypatch.setattr(client_module.Client, "connect", stub)
    config = TemporalRuntimeConfig(
        connection=TemporalConnectionConfig(
            target="temporal.example.com:7233",
            tls=TemporalTLSConfig(
                enabled=True,
                domain="temporal.example.com",
                server_root_ca_cert=b"ca",
                client_cert=b"cert",
                client_private_key=b"key",
            ),
        )
    )

    await TemporalRuntimeClient.connect(config)

    tls = stub.kwargs["tls"]
    assert tls is not None and tls is not False
    assert tls.domain == "temporal.example.com"
    assert tls.server_root_ca_cert == b"ca"
    assert tls.client_cert == b"cert"


@pytest.mark.asyncio
@pytest.mark.parametrize("error", [_rpc_error(), OSError("no route to host")])
async def test_connect_translates_transport_failures(monkeypatch, error: Exception) -> None:
    monkeypatch.setattr(client_module.Client, "connect", _StubConnect(error=error))

    with pytest.raises(RuntimeConnectionError, match="Could not connect to Temporal target"):
        await TemporalRuntimeClient.connect(TemporalRuntimeConfig())


# --------------------------------------------------------------------------
# Operation failures
# --------------------------------------------------------------------------


class _FailingClient:
    def __init__(self, error: Exception) -> None:
        self._error = error
        self.service_client = self

    async def start_workflow(self, *args: object, **kwargs: object) -> object:
        raise self._error

    async def check_health(self) -> bool:
        raise self._error


@pytest.mark.asyncio
async def test_start_ingestion_wraps_a_submission_failure() -> None:
    client = TemporalRuntimeClient(_FailingClient(_rpc_error()), TemporalRuntimeConfig())

    with pytest.raises(WorkflowSubmissionError, match="Could not start ingestion run"):
        await client.start_ingestion(_input())


@pytest.mark.asyncio
async def test_start_ingestion_wraps_an_already_started_workflow() -> None:
    already = client_module.WorkflowAlreadyStartedError("run-1", "harborrag.ingestion_run")
    client = TemporalRuntimeClient(_FailingClient(already), TemporalRuntimeConfig())

    with pytest.raises(WorkflowSubmissionError, match="Could not start ingestion run"):
        await client.start_ingestion(_input())


@pytest.mark.asyncio
@pytest.mark.parametrize("error", [_rpc_error(), OSError("connection reset")])
async def test_health_failures_become_a_connection_error(error: Exception) -> None:
    client = TemporalRuntimeClient(_FailingClient(error), TemporalRuntimeConfig())

    with pytest.raises(RuntimeConnectionError, match="health check failed"):
        await client.health()
