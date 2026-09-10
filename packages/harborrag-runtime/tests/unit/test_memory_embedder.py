"""Memory embedder composition and best-effort long-term extraction."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

import pytest

import harborrag_memory
from harborrag_core.ports.memory import MemoryOwner
from harborrag_runtime.config.settings import RuntimeSettings
from harborrag_runtime.memory import (
    MemoryContextRequest,
    RuntimeMemoryContextService,
    build_memory_embedder,
    close_memory_embedder,
)
from harborrag_runtime.sdk import HarborRAG, HarborRAGConfig

_MEMORY_EMBED_MODEL = """    memory:
      deployments:
        - name: openai-memory-embedding
          provider: openai
          model: openai/text-embedding-3-small
          api_key: test-key
          expected_dimensions: 768
          capabilities:
            batch: true
"""


def _write_catalog(path: Path, *, with_memory_profile: bool) -> Path:
    document = """embed:
  default_model: primary
  models:
    primary:
      deployments:
        - name: openai-embedding
          provider: openai
          model: openai/text-embedding-3-small
          api_key: test-key
          expected_dimensions: 1536
          capabilities:
            batch: true
"""
    if with_memory_profile:
        document += _MEMORY_EMBED_MODEL
    catalog = path / "models.yaml"
    catalog.write_text(document, encoding="utf-8")
    return catalog


async def _no_embedder(settings: RuntimeSettings) -> None:
    del settings
    return None


@pytest.mark.asyncio
async def test_memory_embedder_is_none_for_a_missing_catalog(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    settings = RuntimeSettings(model_config_path=tmp_path / "absent.yaml")

    with caplog.at_level(logging.WARNING, logger="harborrag.runtime.memory"):
        assert await build_memory_embedder(settings) is None

    assert "Memory embedder unavailable" in caplog.text


@pytest.mark.asyncio
async def test_memory_embedder_is_none_for_an_unreadable_catalog(tmp_path: Path) -> None:
    catalog = tmp_path / "models.yaml"
    catalog.write_text("embed: [not, a, mapping]\n", encoding="utf-8")

    assert await build_memory_embedder(RuntimeSettings(model_config_path=catalog)) is None


@pytest.mark.asyncio
async def test_memory_embedder_prefers_the_configured_profile(tmp_path: Path) -> None:
    settings = RuntimeSettings(
        model_config_path=_write_catalog(tmp_path, with_memory_profile=True),
        memory_embed_profile="memory",
    )

    embedder = await build_memory_embedder(settings)

    assert embedder is not None
    assert embedder._logical_model == "memory"  # type: ignore[attr-defined]
    assert embedder._dimensions == 768  # type: ignore[attr-defined]
    await close_memory_embedder(embedder)


@pytest.mark.asyncio
async def test_memory_embedder_uses_the_catalog_default_without_a_profile(tmp_path: Path) -> None:
    settings = RuntimeSettings(model_config_path=_write_catalog(tmp_path, with_memory_profile=True))

    embedder = await build_memory_embedder(settings)

    assert embedder is not None
    assert embedder._logical_model == "primary"  # type: ignore[attr-defined]
    assert embedder._dimensions == 1536  # type: ignore[attr-defined]
    await close_memory_embedder(embedder)


@pytest.mark.asyncio
async def test_memory_embedder_falls_back_when_the_profile_is_absent(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    settings = RuntimeSettings(
        model_config_path=_write_catalog(tmp_path, with_memory_profile=False),
        memory_embed_profile="memory",
    )

    with caplog.at_level(logging.INFO, logger="harborrag.runtime.memory"):
        embedder = await build_memory_embedder(settings)

    assert embedder is not None
    assert embedder._logical_model == "primary"  # type: ignore[attr-defined]
    assert "falling back to the default logical model=primary" in caplog.text
    await close_memory_embedder(embedder)


@pytest.mark.asyncio
async def test_close_memory_embedder_disposes_the_client_and_tolerates_none(
    tmp_path: Path,
) -> None:
    settings = RuntimeSettings(model_config_path=_write_catalog(tmp_path, with_memory_profile=True))
    embedder = await build_memory_embedder(settings)
    assert embedder is not None
    closed: list[bool] = []

    async def _aclose() -> None:
        closed.append(True)

    embedder.client.aclose = _aclose  # type: ignore[attr-defined]

    await close_memory_embedder(embedder)
    await close_memory_embedder(None)

    assert closed == [True]


@pytest.mark.asyncio
async def test_context_service_builds_its_embedder_once() -> None:
    calls: list[int] = []
    sentinel = object()

    async def _builder(settings: RuntimeSettings) -> object:
        del settings
        calls.append(1)
        return sentinel

    service = RuntimeMemoryContextService(RuntimeSettings(), embedder_builder=_builder)  # type: ignore[arg-type]

    assert await service.embedder() is sentinel
    assert await service.embedder() is sentinel
    assert calls == [1]


@pytest.mark.asyncio
async def test_context_service_skips_the_embedder_when_disabled() -> None:
    async def _unreachable(settings: RuntimeSettings) -> object:
        del settings
        raise AssertionError("a disabled policy must not build an embedder")

    service = RuntimeMemoryContextService(
        RuntimeSettings(memory_enabled=False),
        embedder_builder=_unreachable,  # type: ignore[arg-type]
    )

    assert await service.embedder() is None


@pytest.mark.asyncio
async def test_context_service_aclose_disposes_the_embedder(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from harborrag_runtime.memory import context_service as module

    closed: list[object] = []
    sentinel = object()

    async def _close(embedder: object) -> None:
        closed.append(embedder)

    async def _builder(settings: RuntimeSettings) -> object:
        del settings
        return sentinel

    monkeypatch.setattr(module, "close_memory_embedder", _close)
    service = RuntimeMemoryContextService(RuntimeSettings(), embedder_builder=_builder)  # type: ignore[arg-type]
    assert await service.embedder() is sentinel

    await service.aclose()

    assert closed == [sentinel]
    assert await service.embedder() is sentinel


@pytest.mark.asyncio
async def test_builder_threads_the_index_and_the_cached_embedder_through(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from harborrag_runtime.memory import context_service as module

    captured: list[dict[str, Any]] = []
    embedder = object()

    async def _builder(settings: RuntimeSettings) -> object:
        del settings
        return embedder

    monkeypatch.setattr(module, "MemoryContextBuilder", lambda **kwargs: captured.append(kwargs))
    service = RuntimeMemoryContextService(RuntimeSettings(), embedder_builder=_builder)  # type: ignore[arg-type]
    index = object()

    await service.builder(messages=object(), index=index)  # type: ignore[arg-type]

    assert captured[0]["index"] is index
    assert captured[0]["embedder"] is embedder


def _request() -> MemoryContextRequest:
    return MemoryContextRequest(
        tenant_id="tenant-1",
        principal_id="principal-1",
        user_id="user-1",
        session_id="session-1",
        question="what did we decide?",
        project_id="project-1",
    )


def _harbor(settings: RuntimeSettings, *, model: object = None) -> HarborRAG:
    async def _model_builder(selected: RuntimeSettings) -> object:
        del selected
        return model

    harbor = HarborRAG(HarborRAGConfig())
    harbor._memory_runtime = RuntimeMemoryContextService(
        settings,
        model_builder=_model_builder,  # type: ignore[arg-type]
        embedder_builder=_no_embedder,
    )
    return harbor


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "settings",
    [
        RuntimeSettings(memory_enabled=False),
        RuntimeSettings(memory_extraction_enabled=False),
    ],
    ids=["memory-disabled", "extraction-disabled"],
)
async def test_extract_returns_nothing_when_switched_off(
    monkeypatch: pytest.MonkeyPatch,
    settings: RuntimeSettings,
) -> None:
    def _unreachable(**kwargs: Any) -> object:
        raise AssertionError("a switched-off policy must not build an extractor")

    monkeypatch.setattr(harborrag_memory, "MemoryExtractor", _unreachable, raising=False)
    harbor = _harbor(settings, model=object())

    assert await harbor.memory.extract(_request(), messages=(), memories=object()) == ()  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_extract_returns_nothing_without_a_memory_model(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _unreachable(**kwargs: Any) -> object:
        raise AssertionError("extraction without a model must not build an extractor")

    monkeypatch.setattr(harborrag_memory, "MemoryExtractor", _unreachable, raising=False)
    harbor = _harbor(RuntimeSettings(), model=None)

    assert await harbor.memory.extract(_request(), messages=(), memories=object()) == ()  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_extract_runs_the_extractor_over_the_supplied_messages(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    built: list[dict[str, Any]] = []
    calls: list[tuple[MemoryOwner, tuple[object, ...]]] = []

    class _Extractor:
        def __init__(self, **kwargs: Any) -> None:
            built.append(kwargs)

        async def extract(
            self,
            owner: MemoryOwner,
            *,
            messages: tuple[object, ...],
        ) -> tuple[object, ...]:
            calls.append((owner, messages))
            return ("memory",)

    monkeypatch.setattr(harborrag_memory, "MemoryExtractor", _Extractor, raising=False)
    model = object()
    harbor = _harbor(RuntimeSettings(), model=model)
    memories = object()
    index = object()

    result = await harbor.memory.extract(
        _request(),
        messages=("message",),  # type: ignore[arg-type]
        memories=memories,  # type: ignore[arg-type]
        index=index,  # type: ignore[arg-type]
    )

    assert result == ("memory",)
    assert built[0]["memories"] is memories
    assert built[0]["model"] is model
    assert built[0]["index"] is index
    assert built[0]["embedder"] is None
    owner, messages = calls[0]
    assert owner.tenant_id == "tenant-1"
    assert owner.user_id == "user-1"
    assert messages == ("message",)
