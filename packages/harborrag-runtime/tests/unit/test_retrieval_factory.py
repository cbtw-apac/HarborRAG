from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from harborrag_core.topology.derived import ContextualIndexProfile
from harborrag_core.topology.retrieval_policy import TopologyRetrievalPolicy
from harborrag_runtime.config.settings import RuntimeSettings
from harborrag_runtime.retrieval import composition as retrieval_factory


class _Resource:
    def __init__(self, *, connect_error: Exception | None = None) -> None:
        self._connect_error = connect_error
        self.connect = AsyncMock(side_effect=self._connect)
        self.close = AsyncMock()

    async def _connect(self) -> None:
        if self._connect_error is not None:
            raise self._connect_error


class _ObjectStore(_Resource):
    def __init__(self, *, connect_error: Exception | None = None) -> None:
        super().__init__(connect_error=connect_error)
        self.ensure_buckets = AsyncMock()
        self.validate_buckets = AsyncMock()


class _Telemetry:
    def __init__(self) -> None:
        self.start = AsyncMock()
        self.close = AsyncMock()


def _settings() -> RuntimeSettings:
    return RuntimeSettings(
        model_config_path=Path("models.yaml"),
        embedding_model=None,
        embedding_dimensions=None,
        metrics_port=None,
        metrics_bind_address="127.0.0.1",
        langfuse_enabled=False,
        sparse_encoder_profile="bm25-v1",
        sparse_k=1.2,
        sparse_b=0.75,
        sparse_fixed_avg_len=128.0,
        retrieval_dense_weight=0.7,
        topology_retrieval_policy=TopologyRetrievalPolicy(),
    )


def _providers(monkeypatch, *, graph_error: Exception | None = None):
    embed = SimpleNamespace(aclose=AsyncMock())
    control = _Resource()
    control.document_versions = object()
    control.topology = object()
    control.summaries = object()
    objects = _ObjectStore()
    vectors = _Resource()
    graph = _Resource(connect_error=graph_error)
    telemetry = _Telemetry()
    embed_config = SimpleNamespace(default_model="embed-default")
    monkeypatch.setattr(
        retrieval_factory.HarborEmbedClientConfig,
        "from_file",
        Mock(return_value=embed_config),
    )
    monkeypatch.setattr(
        retrieval_factory.HarborEmbedClient,
        "from_config",
        Mock(return_value=embed),
    )
    monkeypatch.setattr(retrieval_factory, "embedding_dimensions", Mock(return_value=32))
    monkeypatch.setattr(
        retrieval_factory,
        "build_contextual_profile",
        Mock(
            return_value=ContextualIndexProfile(
                model="embed-default", dimension=32, deployment_revision="pinned"
            )
        ),
    )
    monkeypatch.setattr(
        retrieval_factory,
        "build_ingestion_control",
        Mock(return_value=control),
    )
    monkeypatch.setattr(
        retrieval_factory,
        "build_object_store",
        Mock(return_value=objects),
    )
    monkeypatch.setattr(
        retrieval_factory,
        "build_vector_repository",
        Mock(return_value=vectors),
    )
    monkeypatch.setattr(
        retrieval_factory,
        "build_knowledge_graph",
        Mock(return_value=graph),
    )
    monkeypatch.setattr(
        retrieval_factory,
        "IngestionTelemetry",
        Mock(return_value=telemetry),
    )
    monkeypatch.setattr(
        retrieval_factory,
        "build_model_telemetry",
        Mock(return_value=object()),
    )
    return embed, control, objects, vectors, graph, telemetry


def _with_summarization(tmp_path: Path, *, enabled: bool) -> RuntimeSettings:
    graph_build = tmp_path / "graph_build.yaml"
    graph_build.write_text(f"summarization:\n  enabled: {str(enabled).lower()}\n", encoding="utf-8")
    return _settings().model_copy(update={"graph_build_config_path": graph_build})


@pytest.mark.asyncio
@pytest.mark.parametrize("summarization", [True, False])
async def test_summary_views_are_wired_only_while_summarization_is_on(
    monkeypatch, tmp_path: Path, summarization: bool
) -> None:
    _, control, *_ = _providers(monkeypatch)
    service_factory = Mock(return_value=object())
    monkeypatch.setattr(retrieval_factory, "RuntimeRetrievalService", service_factory)

    await retrieval_factory.connect_retrieval_service(
        _with_summarization(tmp_path, enabled=summarization)
    )

    resources = service_factory.call_args.kwargs["resources"]
    expected = control.summaries if summarization else None
    assert resources.summary_repository is expected


@pytest.mark.asyncio
async def test_retrieval_factory_connects_and_owns_every_provider(
    monkeypatch, tmp_path: Path
) -> None:
    embed, control, objects, vectors, graph, telemetry = _providers(monkeypatch)
    service_factory = Mock(return_value=object())
    monkeypatch.setattr(
        retrieval_factory,
        "RuntimeRetrievalService",
        service_factory,
    )

    service = await retrieval_factory.connect_retrieval_service(
        _with_summarization(tmp_path, enabled=True)
    )

    assert service is service_factory.return_value
    assert all(resource.connect.await_count == 1 for resource in (control, objects, vectors, graph))
    objects.ensure_buckets.assert_not_awaited()
    objects.validate_buckets.assert_awaited_once_with((retrieval_factory.ARTIFACT_BUCKET,))
    resources = service_factory.call_args.kwargs["resources"]
    policy = service_factory.call_args.kwargs["policy"]
    assert resources.embed_client is embed
    assert resources.active_versions is control.document_versions
    assert resources.graph_repository is graph
    assert resources.topology_repository is control.topology
    assert resources.summary_repository is control.summaries
    assert isinstance(resources.contextual_search, retrieval_factory.ContextualEvidenceSearch)
    assert policy.embedding_model == "embed-default"
    assert policy.embedding_dimensions == 32
    assert telemetry.start.await_count == 1
    assert len(service_factory.call_args.kwargs["close_resources"]) == 6


@pytest.mark.asyncio
async def test_retrieval_factory_closes_connected_resources_after_failure(
    monkeypatch,
) -> None:
    embed, control, objects, vectors, graph, telemetry = _providers(
        monkeypatch,
        graph_error=ConnectionError("graph unavailable"),
    )

    with pytest.raises(ConnectionError, match="graph unavailable"):
        await retrieval_factory.connect_retrieval_service(_settings())

    assert graph.close.await_count == 0
    assert vectors.close.await_count == 1
    assert objects.close.await_count == 1
    assert control.close.await_count == 1
    assert embed.aclose.await_count == 1
    assert telemetry.close.await_count == 1


@pytest.mark.asyncio
async def test_reader_bucket_validation_failure_closes_all_connected_resources(
    monkeypatch,
) -> None:
    embed, control, objects, vectors, graph, telemetry = _providers(monkeypatch)
    objects.validate_buckets.side_effect = RuntimeError("required reader bucket unavailable")

    with pytest.raises(RuntimeError, match="required reader bucket unavailable"):
        await retrieval_factory.connect_retrieval_service(_settings())

    objects.ensure_buckets.assert_not_awaited()
    for resource in (control, objects, vectors, graph):
        resource.close.assert_awaited_once()
    embed.aclose.assert_awaited_once()
    telemetry.close.assert_awaited_once()
