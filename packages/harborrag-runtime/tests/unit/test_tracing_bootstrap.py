"""The tracing bootstrap installs one named provider and exports only when configured."""

import pytest
from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider

from harborrag_runtime.observability import tracing


@pytest.fixture(autouse=True)
def no_global_provider(monkeypatch: pytest.MonkeyPatch) -> None:
    installed: list[object] = []
    monkeypatch.setattr(trace, "get_tracer_provider", lambda: installed[-1] if installed else None)
    monkeypatch.setattr(trace, "set_tracer_provider", installed.append)
    for name in ("OTEL_EXPORTER_OTLP_TRACES_ENDPOINT", "OTEL_EXPORTER_OTLP_ENDPOINT"):
        monkeypatch.delenv(name, raising=False)


def test_bootstrap_names_the_service_and_is_idempotent() -> None:
    provider = tracing.bootstrap_tracing("harborrag-parser")

    assert isinstance(provider, TracerProvider)
    assert provider.resource.attributes["service.name"] == "harborrag-parser"
    assert tracing.bootstrap_tracing("harborrag-worker") is provider


def test_bootstrap_exports_over_otlp_only_when_an_endpoint_is_set(monkeypatch) -> None:
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://collector:4318")

    provider = tracing.bootstrap_tracing("harborrag-worker")

    processors = provider._active_span_processor._span_processors
    assert [type(processor).__name__ for processor in processors] == ["BatchSpanProcessor"]
    provider.shutdown()
