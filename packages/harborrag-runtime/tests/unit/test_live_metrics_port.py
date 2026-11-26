from __future__ import annotations

import logging
import socket
from collections.abc import Iterator
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from harborrag_core.topology.derived import ContextualIndexProfile
from harborrag_runtime.config.settings import RuntimeSettings
from harborrag_runtime.retrieval import composition as retrieval_factory

pytestmark = pytest.mark.unit

_LOGGER = "harborrag.runtime.retrieval.composition"


class _Resource:
    def __init__(self) -> None:
        self.connect = AsyncMock()
        self.close = AsyncMock()
        self.validate_buckets = AsyncMock()


@pytest.fixture
def busy_port() -> Iterator[int]:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.bind(("127.0.0.1", 0))
        listener.listen()
        yield listener.getsockname()[1]


def _providers(monkeypatch: pytest.MonkeyPatch) -> None:
    control = _Resource()
    control.document_versions = object()
    control.topology = object()
    control.summaries = object()
    monkeypatch.setattr(
        retrieval_factory.HarborEmbedClientConfig,
        "from_file",
        Mock(return_value=SimpleNamespace(default_model="embed-default")),
    )
    monkeypatch.setattr(
        retrieval_factory.HarborEmbedClient,
        "from_config",
        Mock(return_value=SimpleNamespace(aclose=AsyncMock())),
    )
    monkeypatch.setattr(retrieval_factory, "embedding_dimensions", Mock(return_value=32))
    monkeypatch.setattr(retrieval_factory, "has_rerank_model", Mock(return_value=False))
    monkeypatch.setattr(
        retrieval_factory,
        "build_contextual_profile",
        Mock(
            return_value=ContextualIndexProfile(
                model="embed-default", dimension=32, deployment_revision="pinned"
            )
        ),
    )
    monkeypatch.setattr(retrieval_factory, "build_ingestion_control", Mock(return_value=control))
    monkeypatch.setattr(
        retrieval_factory, "build_object_store", Mock(side_effect=lambda _settings: _Resource())
    )
    monkeypatch.setattr(
        retrieval_factory,
        "build_vector_repository",
        Mock(side_effect=lambda _settings: _Resource()),
    )
    monkeypatch.setattr(
        retrieval_factory, "build_knowledge_graph", Mock(side_effect=lambda _settings: _Resource())
    )
    monkeypatch.setattr(retrieval_factory, "build_model_telemetry", Mock(return_value=object()))
    monkeypatch.setattr(retrieval_factory, "RuntimeRetrievalService", Mock(return_value=object()))


def _settings(tmp_path: Path, port: int) -> RuntimeSettings:
    graph_build = tmp_path / "build.yaml"
    graph_build.write_text("summarization:\n  enabled: false\n", encoding="utf-8")
    return RuntimeSettings(
        model_config_path=Path("models.yaml"),
        graph_build_config_path=graph_build,
        metrics_port=port,
        metrics_bind_address="127.0.0.1",
        langfuse_enabled=False,
    )


@pytest.mark.asyncio
async def test_retrieval_composition_with_a_busy_metrics_port_warns_once_and_connects(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
    busy_port: int,
) -> None:
    _providers(monkeypatch)
    monkeypatch.setattr(retrieval_factory, "_UNBOUND_METRICS_PORTS", set())
    settings = _settings(tmp_path, busy_port)

    with caplog.at_level(logging.WARNING, logger=_LOGGER):
        first = await retrieval_factory.connect_retrieval_service(settings)
        second = await retrieval_factory.connect_retrieval_service(settings)

    assert first is not None
    assert second is not None
    warnings = [record for record in caplog.records if record.levelno == logging.WARNING]
    assert len(warnings) == 1
    assert str(busy_port) in warnings[0].getMessage()
    assert "127.0.0.1" not in warnings[0].getMessage()
