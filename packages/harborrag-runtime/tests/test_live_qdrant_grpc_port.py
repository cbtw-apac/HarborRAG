from __future__ import annotations

import logging

import pytest
from pydantic import ValidationError

from harborrag_adapters.repositories.vector.qdrant import client as qdrant_client_module
from harborrag_runtime.composition import resources
from harborrag_runtime.composition.resources import build_vector_repository, resource_targets
from harborrag_runtime.config.settings import RuntimeSettings

pytestmark = pytest.mark.whitebox


class _RecordingAsyncQdrantClient:
    instances: list[_RecordingAsyncQdrantClient] = []

    def __init__(self, **kwargs: object) -> None:
        self.kwargs = kwargs
        _RecordingAsyncQdrantClient.instances.append(self)

    async def get_collections(self) -> list[str]:
        return []

    async def close(self) -> None:
        pass


@pytest.fixture
def recorded_clients(monkeypatch: pytest.MonkeyPatch) -> list[_RecordingAsyncQdrantClient]:
    monkeypatch.setattr(_RecordingAsyncQdrantClient, "instances", [])
    monkeypatch.setattr(qdrant_client_module, "AsyncQdrantClient", _RecordingAsyncQdrantClient)
    monkeypatch.setattr(resources, "_logged_vector_targets", set())
    return _RecordingAsyncQdrantClient.instances


async def _connected_client_kwargs(
    settings: RuntimeSettings, clients: list[_RecordingAsyncQdrantClient]
) -> dict[str, object]:
    repository = build_vector_repository(settings)
    await repository.connect()
    await repository.close()
    return clients[-1].kwargs


def test_moved_qdrant_url_port_without_grpc_port_refuses_to_start() -> None:
    with pytest.raises(ValidationError) as failure:
        RuntimeSettings(qdrant_url="http://localhost:16333")

    message = str(failure.value)
    assert "HARBORRAG_QDRANT_GRPC_PORT" in message
    assert "HARBORRAG_QDRANT_PREFER_GRPC=false" in message
    assert "16333" not in message


def test_moved_qdrant_url_port_is_accepted_over_http_or_another_vector_provider() -> None:
    RuntimeSettings(qdrant_url="http://localhost:16333", qdrant_prefer_grpc=False)
    RuntimeSettings(qdrant_url="http://localhost:16333", vector_provider="memory")


@pytest.mark.parametrize("blank", ["", "  "])
def test_blank_qdrant_grpc_port_counts_as_unset(
    blank: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HARBORRAG_QDRANT_GRPC_PORT", blank)

    assert RuntimeSettings().qdrant_grpc_port is None
    monkeypatch.setenv("HARBORRAG_QDRANT_URL", "http://localhost:16333")
    with pytest.raises(ValidationError, match="HARBORRAG_QDRANT_GRPC_PORT"):
        RuntimeSettings()


@pytest.mark.parametrize("port", [0, 65_536])
def test_qdrant_grpc_port_outside_the_tcp_range_is_refused(port: int) -> None:
    with pytest.raises(ValidationError):
        RuntimeSettings(qdrant_grpc_port=port)


@pytest.mark.asyncio
async def test_explicit_qdrant_grpc_port_reaches_the_client(
    recorded_clients: list[_RecordingAsyncQdrantClient],
) -> None:
    settings = RuntimeSettings(qdrant_url="http://localhost:16333", qdrant_grpc_port=16334)

    kwargs = await _connected_client_kwargs(settings, recorded_clients)

    assert kwargs["url"] == "http://localhost:16333"
    assert kwargs["prefer_grpc"] is True
    assert kwargs["grpc_port"] == 16334
    assert resource_targets(settings)["vector"] == "Qdrant gRPC localhost:16334"


@pytest.mark.asyncio
async def test_default_qdrant_url_keeps_the_default_grpc_port(
    recorded_clients: list[_RecordingAsyncQdrantClient],
) -> None:
    settings = RuntimeSettings()

    kwargs = await _connected_client_kwargs(settings, recorded_clients)

    assert settings.qdrant_grpc_port is None
    assert kwargs["url"] == "http://localhost:6333"
    assert "grpc_port" not in kwargs
    assert resource_targets(settings)["vector"] == "Qdrant gRPC localhost:6334"


def test_vector_target_is_logged_once_per_process(
    recorded_clients: list[_RecordingAsyncQdrantClient], caplog: pytest.LogCaptureFixture
) -> None:
    """The host, gRPC port and transport are logged once; the credentials stay out."""
    settings = RuntimeSettings(
        qdrant_url="http://u:hunter2@localhost:16333", qdrant_grpc_port=16334
    )

    with caplog.at_level(logging.INFO, logger="harborrag.runtime.composition"):
        build_vector_repository(settings)
        build_vector_repository(settings)

    records = [
        record for record in caplog.records if record.name == "harborrag.runtime.composition"
    ]
    (record,) = records
    assert record.__dict__["event"] == "composition.vector_store_selected"
    assert record.__dict__["backend"] == "qdrant"
    assert record.__dict__["transport_kind"] == "grpc"
    assert record.__dict__["server_address"] == "localhost"
    assert record.__dict__["server_port"] == 16334
    assert "dropped_fields" not in record.__dict__
    assert "hunter2" not in caplog.text
    assert "u:" not in caplog.text
