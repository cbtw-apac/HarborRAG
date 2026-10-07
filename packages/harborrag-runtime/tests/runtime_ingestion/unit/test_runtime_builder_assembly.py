"""The ingestion runtime builder wires one graph from injected infrastructure."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace

import pytest

from harborrag_runtime.config.settings import RuntimeSettings
from harborrag_runtime.ingestion import runtime_builder
from harborrag_runtime.ingestion.observability import IngestionTelemetry
from harborrag_runtime.ingestion.runtime_builder import (
    IngestionRuntimeBuilder,
    build_ingestion_runtime,
)


@dataclass(frozen=True)
class _Sentinel:
    name: str


@dataclass
class _FakeEmbedConfig:
    default_model: str = "embed-default"


class _FakeEmbedClient:
    def __init__(self, config: _FakeEmbedConfig, telemetry: object) -> None:
        self.config = config
        self.telemetry = telemetry


class _FakeNormalizerBuilder:
    def __init__(self) -> None:
        self.normalizer = _Sentinel("normalizer")

    def build(self) -> _Sentinel:
        return self.normalizer


@pytest.fixture
def wired(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> SimpleNamespace:
    """Replace every infrastructure factory with an inert sentinel."""

    infra = SimpleNamespace(
        parser=_Sentinel("parser"),
        control=_Sentinel("control"),
        object_store=_Sentinel("object-store"),
        vectors=_Sentinel("vectors"),
        graph=_Sentinel("graph"),
        dimension_requests=[],
        model_telemetry_flags=[],
        config_paths=[],
    )
    catalog = SimpleNamespace(build_harbor_parser=lambda: infra.parser)
    monkeypatch.setattr(runtime_builder, "load_parser_catalog", lambda _path: catalog)
    monkeypatch.setattr(runtime_builder, "build_ingestion_control", lambda _s: infra.control)
    monkeypatch.setattr(runtime_builder, "build_object_store", lambda _s: infra.object_store)
    monkeypatch.setattr(runtime_builder, "build_vector_repository", lambda _s: infra.vectors)
    monkeypatch.setattr(runtime_builder, "build_knowledge_graph", lambda _s: infra.graph)

    def from_file(path: Path) -> _FakeEmbedConfig:
        infra.config_paths.append(path)
        return _FakeEmbedConfig()

    def dimensions(config: _FakeEmbedConfig, model: str) -> int:
        infra.dimension_requests.append(model)
        return 8

    def model_telemetry(config: _FakeEmbedConfig, *, langfuse_enabled: bool) -> str:
        infra.model_telemetry_flags.append(langfuse_enabled)
        return "model-telemetry"

    monkeypatch.setattr(
        runtime_builder,
        "HarborEmbedClientConfig",
        SimpleNamespace(from_file=from_file),
    )
    monkeypatch.setattr(
        runtime_builder,
        "HarborEmbedClient",
        SimpleNamespace(
            from_config=lambda config, telemetry, telemetry_ownership: _FakeEmbedClient(
                config, telemetry
            )
        ),
    )
    monkeypatch.setattr(runtime_builder, "embedding_dimensions", dimensions)
    monkeypatch.setattr(runtime_builder, "build_model_telemetry", model_telemetry)

    source_dir = tmp_path / "docs"
    source_dir.mkdir()
    connector_config = tmp_path / "connectors.yaml"
    connector_config.write_text(
        f"""
        version: 1
        connectors:
          local-docs:
            provider: local
            settings:
              source_path: {source_dir}
        """,
        encoding="utf-8",
    )
    infra.connector_config = connector_config
    return infra


def test_build_wires_injected_infrastructure_into_every_service(
    wired: SimpleNamespace,
) -> None:
    normalizers = _FakeNormalizerBuilder()
    settings = RuntimeSettings(
        connector_config_path=wired.connector_config,
        model_config_path=Path("models-under-test.yaml"),
        retired_version_retention_days=9,
    )

    runtime = IngestionRuntimeBuilder(settings, normalizer_builder=normalizers).build()

    assert set(runtime.connectors) == {"local-docs"}
    assert set(runtime.connector_fingerprints) == {"local-docs"}
    assert runtime.connector_errors == {}
    assert runtime.control is wired.control
    assert runtime.object_store is wired.object_store
    assert runtime.vector_repository is wired.vectors
    assert runtime.graph_repository is wired.graph
    assert isinstance(runtime.embed_client, _FakeEmbedClient)
    assert runtime.embed_client.telemetry == "model-telemetry"
    assert isinstance(runtime.telemetry, IngestionTelemetry)
    # Unset model settings fall back to the model catalog's default and its size.
    assert wired.config_paths == [Path("models-under-test.yaml")]
    assert wired.dimension_requests == ["embed-default"]
    assert wired.model_telemetry_flags == [settings.langfuse_enabled]
    assert runtime.retention is not None
    assert runtime.sources is not None and runtime.reindex is not None


def test_explicit_embedding_settings_skip_the_catalog_defaults(
    wired: SimpleNamespace,
) -> None:
    settings = RuntimeSettings(
        connector_config_path=wired.connector_config,
        embedding_model="explicit-model",
        embedding_dimensions=16,
    )

    client, model, dimensions = IngestionRuntimeBuilder(settings)._embedding_client()

    assert (model, dimensions) == ("explicit-model", 16)
    assert wired.dimension_requests == []
    assert isinstance(client, _FakeEmbedClient)


def test_build_ingestion_runtime_uses_the_supplied_settings(
    wired: SimpleNamespace,
) -> None:
    runtime = build_ingestion_runtime(
        RuntimeSettings(connector_config_path=wired.connector_config),
        normalizer_builder=_FakeNormalizerBuilder(),
    )

    assert runtime.control is wired.control
    assert set(runtime.connectors) == {"local-docs"}


def test_document_normalizer_comes_from_the_configured_builder() -> None:
    normalizers = _FakeNormalizerBuilder()
    builder = IngestionRuntimeBuilder(RuntimeSettings(), normalizer_builder=normalizers)

    assert builder.build_document_normalizer() is normalizers.normalizer


def test_rate_limit_waits_are_recorded_on_the_ingestion_telemetry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}

    def fake_limiter(settings: RuntimeSettings, *, on_wait: object) -> str:
        captured["settings"] = settings
        captured["on_wait"] = on_wait
        return "limiter"

    monkeypatch.setattr(runtime_builder, "build_connector_rate_limiter", fake_limiter)
    settings = RuntimeSettings()
    telemetry = IngestionTelemetry()

    limiter = IngestionRuntimeBuilder(settings)._rate_limiter(telemetry)
    on_wait = captured["on_wait"]
    assert callable(on_wait)
    on_wait(SimpleNamespace(connector_type="confluence"), 0.25)

    assert limiter == "limiter"
    assert captured["settings"] is settings
    events = telemetry.registry.get_sample_value(
        "harborrag_ingestion_connector_rate_limit_events_total",
        {"connector_type": "confluence"},
    )
    assert events == 1.0


def test_connector_kwargs_map_registry_dependencies_to_runtime_objects() -> None:
    parser, limiter = object(), object()

    kwargs = IngestionRuntimeBuilder._connector_kwargs(
        "confluence",
        {"attachment_parser": parser, "rate_limiter": limiter},
    )

    assert kwargs == {"parser": parser, "rate_limiter": limiter}


def test_connector_kwargs_reject_a_provider_whose_dependencies_are_unavailable() -> None:
    with pytest.raises(ValueError, match=r"'jira' requires .*attachment_parser, rate_limiter"):
        IngestionRuntimeBuilder._connector_kwargs("jira", {})
