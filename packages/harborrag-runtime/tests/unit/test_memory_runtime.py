from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from harborrag_core.ports.memory import MemoryOwner, MemoryScope
from harborrag_memory import MemoryContext, MemoryPolicy
from harborrag_runtime.composition.control_plane import ControlPlaneRepositories
from harborrag_runtime.config.settings import RuntimeSettings
from harborrag_runtime.memory import (
    MemoryContextRequest,
    MemoryFacade,
    RuntimeMemoryContextService,
    build_memory_chat_model,
    close_memory_chat_model,
    memory_policy_from_settings,
)
from harborrag_runtime.memory import context_service as context_service_module
from harborrag_runtime.sdk import HarborRAG, HarborRAGConfig

_MEMORY_MODEL = """
    memory:
      deployments:
        - name: openai-memory
          provider: openai
          model: openai/gpt-4o-mini
          api_key: test-key
          capabilities:
            streaming: false
            structured_output: true
            json_mode: true
            tools: false
"""


def _write_catalog(path: Path, *, with_memory_profile: bool) -> Path:
    document = """chat:
  default_model: primary
  models:
    primary:
      provider: openai
      model: openai/gpt-4o
      api_key: test-key
"""
    if with_memory_profile:
        document += _MEMORY_MODEL
    catalog = path / "models.yaml"
    catalog.write_text(document, encoding="utf-8")
    return catalog


def test_memory_policy_maps_every_settings_field() -> None:
    settings = RuntimeSettings(
        memory_enabled=True,
        memory_recent_max_messages=20,
        memory_recent_max_tokens=3_500,
        memory_summary_trigger_fraction=0.55,
        memory_summary_keep_messages=6,
        memory_recall_top_k=9,
        memory_recall_scopes="session,user",
        memory_recall_recency_half_life_hours=48.0,
        memory_block_budget_fraction=0.25,
        memory_type_affinity_weight=1.25,
        memory_query_rewrite=False,
    )

    policy = memory_policy_from_settings(settings)

    assert policy.enabled is True
    assert policy.recent_max_messages == 20
    assert policy.recent_max_tokens == 3_500
    assert policy.summary_trigger_fraction == pytest.approx(0.55)
    assert policy.summary_keep_messages == 6
    assert policy.recall_top_k == 9
    assert policy.recall_scopes == (MemoryScope.SESSION, MemoryScope.USER)
    assert policy.recency_half_life_hours == pytest.approx(48.0)
    assert policy.block_budget_fraction == pytest.approx(0.25)
    assert policy.type_affinity_weight == pytest.approx(1.25)
    assert policy.query_rewrite is False


def test_memory_policy_is_disabled_when_memory_is_off() -> None:
    settings = RuntimeSettings(memory_enabled=False, memory_recall_top_k=9)

    policy = memory_policy_from_settings(settings)

    assert policy == MemoryPolicy.disabled()
    assert policy.enabled is False
    assert policy.recall_top_k == 0
    assert policy.query_rewrite is False


@pytest.mark.asyncio
async def test_memory_chat_model_is_none_for_a_missing_catalog(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    settings = RuntimeSettings(model_config_path=tmp_path / "absent.yaml")

    with caplog.at_level(logging.WARNING, logger="harborrag.runtime.memory"):
        assert await build_memory_chat_model(settings) is None

    assert "Memory chat model unavailable" in caplog.text


@pytest.mark.asyncio
async def test_memory_chat_model_is_none_for_an_unreadable_catalog(tmp_path: Path) -> None:
    catalog = tmp_path / "models.yaml"
    catalog.write_text("chat: [not, a, mapping]\n", encoding="utf-8")
    settings = RuntimeSettings(model_config_path=catalog)

    assert await build_memory_chat_model(settings) is None


@pytest.mark.asyncio
async def test_memory_chat_model_prefers_the_memory_profile(tmp_path: Path) -> None:
    settings = RuntimeSettings(model_config_path=_write_catalog(tmp_path, with_memory_profile=True))

    model = await build_memory_chat_model(settings)

    assert model is not None
    assert getattr(model, "logical_model", None) == "memory"
    assert getattr(model, "sensitive", None) is True
    await close_memory_chat_model(model)


@pytest.mark.asyncio
async def test_memory_chat_model_falls_back_to_the_default_profile(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    settings = RuntimeSettings(
        model_config_path=_write_catalog(tmp_path, with_memory_profile=False)
    )

    with caplog.at_level(logging.INFO, logger="harborrag.runtime.memory"):
        model = await build_memory_chat_model(settings)

    assert model is not None
    assert getattr(model, "logical_model", None) == "primary"
    assert "falling back to the default logical model=primary" in caplog.text
    await close_memory_chat_model(model)


@pytest.mark.asyncio
async def test_close_memory_chat_model_disposes_the_client(tmp_path: Path) -> None:
    settings = RuntimeSettings(model_config_path=_write_catalog(tmp_path, with_memory_profile=True))
    model = await build_memory_chat_model(settings)
    assert model is not None
    closed: list[bool] = []

    async def _aclose() -> None:
        closed.append(True)

    model.client.aclose = _aclose  # type: ignore[method-assign]

    await close_memory_chat_model(model)
    await close_memory_chat_model(None)

    assert closed == [True]


@dataclass
class _RecordedBuilder:
    policy: MemoryPolicy
    messages: object
    memories: object
    model: object
    index: object
    embedder: object
    built: list[tuple[MemoryOwner, str]]

    async def build(self, owner: MemoryOwner, question: str) -> MemoryContext:
        self.built.append((owner, question))
        return MemoryContext(
            messages=(),
            summary="rolling summary",
            recalled=(),
            standalone_query=question,
            rewritten=False,
            summary_written=False,
        )


def _capture_builders(monkeypatch: pytest.MonkeyPatch) -> list[_RecordedBuilder]:
    captured: list[_RecordedBuilder] = []

    def _factory(**kwargs: Any) -> _RecordedBuilder:
        builder = _RecordedBuilder(built=[], **kwargs)
        captured.append(builder)
        return builder

    monkeypatch.setattr(context_service_module, "MemoryContextBuilder", _factory)
    return captured


async def _no_model(settings: RuntimeSettings) -> None:
    del settings
    return None


def _service(
    settings: RuntimeSettings,
    *,
    model_builder: Any = _no_model,
) -> RuntimeMemoryContextService:
    return RuntimeMemoryContextService(
        settings,
        model_builder=model_builder,
        embedder_builder=_no_model,
    )


def _request() -> MemoryContextRequest:
    return MemoryContextRequest(
        tenant_id="tenant-1",
        principal_id="principal-1",
        user_id="user-1",
        session_id="session-1",
        question="what did we decide?",
        project_id="project-1",
    )


@pytest.mark.asyncio
async def test_memory_facade_passes_the_owner_through_and_returns_the_context(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured = _capture_builders(monkeypatch)
    harbor = HarborRAG(HarborRAGConfig())
    harbor._memory_runtime = _service(harbor.config.runtime)
    store = object()
    memories = object()

    context = await harbor.memory.build_context(
        _request(),
        messages=store,  # type: ignore[arg-type]
        memories=memories,  # type: ignore[arg-type]
    )

    assert context.summary == "rolling summary"
    assert context.standalone_query == "what did we decide?"
    builder = captured[0]
    assert builder.messages is store
    assert builder.memories is memories
    assert builder.policy == memory_policy_from_settings(harbor.config.runtime)
    owner, question = builder.built[0]
    assert owner == MemoryOwner(
        tenant_id="tenant-1",
        project_id="project-1",
        user_id="user-1",
        principal_id="principal-1",
        session_id="session-1",
    )
    assert question == "what did we decide?"


@pytest.mark.asyncio
async def test_memory_facade_degrades_to_a_window_without_a_model(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured = _capture_builders(monkeypatch)
    harbor = HarborRAG(HarborRAGConfig())
    harbor._memory_runtime = _service(harbor.config.runtime)

    await harbor.memory.build_context(_request(), messages=object())  # type: ignore[arg-type]

    assert captured[0].model is None
    assert captured[0].memories is None
    await harbor.aclose()


@pytest.mark.asyncio
async def test_runtime_aclose_disposes_the_memory_model(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    closed: list[object] = []

    async def _close(model: object) -> None:
        closed.append(model)

    sentinel = object()

    async def _builder(settings: RuntimeSettings) -> object:
        del settings
        return sentinel

    monkeypatch.setattr(context_service_module, "close_memory_chat_model", _close)
    harbor = HarborRAG(HarborRAGConfig())
    service = _service(harbor.config.runtime, model_builder=_builder)
    harbor._memory_runtime = service
    assert await service.model() is sentinel

    await harbor.aclose()

    assert closed == [sentinel]
    assert service._model is None


@pytest.mark.asyncio
async def test_memory_context_service_builds_its_model_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _capture_builders(monkeypatch)
    calls: list[int] = []
    sentinel = object()

    async def _builder(settings: RuntimeSettings) -> object:
        del settings
        calls.append(1)
        return sentinel

    service = _service(RuntimeSettings(), model_builder=_builder)

    assert await service.model() is sentinel
    assert await service.model() is sentinel
    assert calls == [1]
    await service.aclose()
    assert await service.model() is sentinel
    assert calls == [1, 1]


@pytest.mark.asyncio
async def test_memory_context_service_skips_the_model_when_disabled() -> None:
    async def _unreachable(settings: RuntimeSettings) -> object:
        del settings
        raise AssertionError("a disabled policy must not build a chat model")

    service = _service(RuntimeSettings(memory_enabled=False), model_builder=_unreachable)

    assert await service.model() is None


def test_control_plane_repositories_memories_default_to_none() -> None:
    fields = ControlPlaneRepositories.__dataclass_fields__
    assert "memories" in fields
    assert fields["memories"].default is None


def test_memory_facade_is_reachable_from_the_runtime() -> None:
    harbor = HarborRAG(HarborRAGConfig())

    assert isinstance(harbor.memory, MemoryFacade)
