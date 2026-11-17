"""Shared fakes for chat application-service tests."""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator, Sequence
from dataclasses import replace
from types import SimpleNamespace

from harborrag_core.config_store import SettingsLevel
from harborrag_core.config_store.resolution import EffectiveSettings, EffectiveValue
from harborrag_core.contracts.errors import HarborUnavailableError, HarborValidationError
from harborrag_core.domain.retrieval import RetrievalResult
from harborrag_core.models.chat import (
    HarborChatMessage,
    HarborChatRequest,
    HarborChatResponse,
    HarborChatStreamChunk,
    HarborChatUsage,
)
from harborrag_core.models.chat.enums import StreamEventType
from harborrag_core.ports.memory import MemoryOwner
from harborrag_core.ports.usage import ModelUsageRecord, ModelUsageTotals
from harborrag_runtime.chat import ChatPrompt
from harborrag_runtime.contracts import RetrievalResponse
from harborrag_runtime.memory import (
    MemoryContext,
    MemoryContextBuilder,
    MemoryContextRequest,
    MemoryPolicy,
)
from harborrag_runtime.sdk import RetrievalLane


class FakeUsageRepository:
    """``ModelUsageRepository`` double that records, or refuses to record."""

    def __init__(self, failure: Exception | None = None) -> None:
        self.failure = failure
        self.records: list[ModelUsageRecord] = []

    async def record(self, usage: ModelUsageRecord) -> None:
        if self.failure is not None:
            raise self.failure
        self.records.append(usage)

    async def totals(
        self,
        *,
        tenant_id: str,
        user_id: str | None = None,
        since: object = None,
    ) -> ModelUsageTotals:
        del since
        matching = [
            record
            for record in self.records
            if record.tenant_id == tenant_id and (user_id is None or record.user_id == user_id)
        ]
        return ModelUsageTotals(
            requests=len(matching),
            prompt_tokens=sum(record.prompt_tokens for record in matching),
            completion_tokens=sum(record.completion_tokens for record in matching),
            total_tokens=sum(record.total_tokens for record in matching),
            estimated_cost_usd=sum(record.estimated_cost_usd or 0.0 for record in matching),
        )


class FakeChatFacade:
    """A chat model that answers scope-gate calls with ``scope`` and every other call with ``answer``.

    Gate calls are recorded in ``gate_requests`` and answer calls in ``requests``, so a
    test can tell which of a turn's model calls happened. ``failure`` fails both.
    ``hold`` keeps every answer call waiting until it is set; ``answering`` is set as
    soon as one is waiting.
    """

    def __init__(  # noqa: PLR0913 - one behaviour of the fake per argument
        self,
        failure: Exception | None = None,
        *,
        stream_failure: Exception | None = None,
        cost: float | None = None,
        answer: str = "Hello",
        scope: str = "knowledge",
        hold: asyncio.Event | None = None,
    ) -> None:
        # Citations are reported only for the sources an answer cites, so a
        # test that needs them must have the model actually cite.
        self.answer = answer
        self.failure = failure
        self.stream_failure = stream_failure
        self.cost = cost
        self.scope = scope
        self.hold = hold
        self.answering = asyncio.Event()
        self.request: HarborChatRequest | None = None
        self.requests: list[HarborChatRequest] = []
        self.prompts: list[object] = []
        self.gate_requests: list[HarborChatRequest] = []
        # (model, tenant_id) for every validation the app asked the runtime
        # for, plus the names this fake accepts.
        self.validated: list[tuple[str | None, str]] = []
        self.allowed_models: set[str] = {"primary"}

    async def validate_model(self, model: str | None, *, tenant_id: str) -> None:
        self.validated.append((model, tenant_id))
        if model is not None and model not in self.allowed_models:
            raise HarborValidationError(f"model {model!r} is not available", {"field": "model"})

    async def complete(
        self,
        request: HarborChatRequest,
        *,
        prompt: object | None = None,
    ) -> HarborChatResponse:
        if self.failure is not None:
            raise self.failure
        if prompt is ChatPrompt.QUERY_GATE:
            self.gate_requests.append(request)
            text = json.dumps({"scope": self.scope})
        else:
            self.request = request
            self.requests.append(request)
            self.prompts.append(prompt)
            self.answering.set()
            if self.hold is not None:
                await self.hold.wait()
            text = self.answer
        return HarborChatResponse(
            id="chat-1",
            logical_model="primary",
            provider="mock",
            provider_model="mock-chat",
            deployment="internal-deployment",
            message=HarborChatMessage.assistant(text),
            finish_reason="stop",
            usage=HarborChatUsage(
                prompt_tokens=2,
                completion_tokens=1,
                total_tokens=3,
            ),
            estimated_cost_usd=self.cost,
            provider_metadata={"private": "must not cross the API boundary"},
        )

    def stream(
        self,
        request: HarborChatRequest,
        *,
        prompt: object | None = None,
    ) -> AsyncIterator[HarborChatStreamChunk]:
        del prompt
        self.request = request
        self.requests.append(request)
        return self._events()

    async def _events(self) -> AsyncIterator[HarborChatStreamChunk]:
        yield HarborChatStreamChunk(
            event=StreamEventType.TEXT_DELTA,
            logical_model="primary",
            provider="mock",
            provider_model="mock-chat",
            deployment="internal-deployment",
            text_delta="Hello",
        )
        if self.stream_failure is not None:
            raise self.stream_failure
        yield HarborChatStreamChunk(
            event=StreamEventType.COMPLETED,
            logical_model="primary",
            provider="mock",
            provider_model="mock-chat",
            deployment="internal-deployment",
            finish_reason="stop",
            usage=HarborChatUsage(prompt_tokens=2, completion_tokens=1, total_tokens=3),
            estimated_cost_usd=self.cost,
        )


def replayed(request: HarborChatRequest) -> list[str]:
    """Message contents, with the final turn reduced to the question it carries.

    Everything before the last message is replayed history, compared
    verbatim -- that is what a window assertion is about. The last message is
    the assembled prompt, whose wording (evidence framing, routing guidance)
    belongs to the prompt layer and has its own tests; comparing it verbatim
    here would make every window test a prompt test too.
    """

    contents = [str(message.content) for message in request.messages]
    contents[-1] = contents[-1].rsplit("Question: ", 1)[-1].strip()
    return contents


class FakeRetrievalFacade:
    def __init__(
        self,
        results: tuple[RetrievalResult, ...] = (),
        *,
        diagnostics: dict[str, object] | None = None,
    ) -> None:
        self.results = results
        self.request = None
        self.requests: list[object] = []
        self.diagnostics = diagnostics if diagnostics is not None else {}

    async def search(self, request: object) -> RetrievalResponse:
        self.request = request
        self.requests.append(request)
        return RetrievalResponse(
            request_id="retrieval-1",
            lane=RetrievalLane.HYBRID,
            results=self.results,
            diagnostics=dict(self.diagnostics),
        )


def graph_diagnostics(*node_keys: str) -> dict[str, object]:
    """The ``graph_documents`` provenance shape a graph-observing search returns."""

    return {
        "graph_documents": [
            {
                "document_id": "document-1",
                "title": "Atlas Migration",
                "sections": ["Overview"],
                "related_results": [
                    {
                        "result_id": "chunk-1",
                        "nodes": [{"node_key": key, "node_kind": "Structure"} for key in node_keys],
                        "relations": [],
                    }
                ],
            }
        ]
    }


class FakeMemoryFacade:
    """The real context builder over the caller's store, with no memory model.

    Using the production ``MemoryContextBuilder`` here keeps these tests honest
    about the policy: window size and token trimming are exercised for real.
    Passing no model disables rewriting and summarization, which have their own
    tests in the memory package.
    """

    def __init__(self, policy: MemoryPolicy | None = None) -> None:
        self.policy = policy or MemoryPolicy()
        self.requests: list[MemoryContextRequest] = []
        self.extractions: list[tuple[MemoryContextRequest, tuple[object, ...]]] = []
        self.extract_failure: Exception | None = None
        self.anchored: list[tuple[MemoryContextRequest, tuple[str, ...]]] = []
        self.anchor_failure: Exception | None = None

    async def build_context(
        self,
        request: MemoryContextRequest,
        *,
        messages: object,
        memories: object = None,
        index: object = None,
    ) -> MemoryContext:
        self.requests.append(request)
        builder = MemoryContextBuilder(
            policy=self.policy,
            messages=messages,  # type: ignore[arg-type]
            memories=memories,  # type: ignore[arg-type]
            index=index,  # type: ignore[arg-type]
        )
        owner = MemoryOwner(
            tenant_id=request.tenant_id,
            principal_id=request.principal_id,
            user_id=request.user_id,
            session_id=request.session_id,
            project_id=request.project_id,
        )
        return await builder.build(owner, request.question)

    async def recall_anchored(
        self,
        request: MemoryContextRequest,
        context: MemoryContext,
        *,
        memories: object,
        index: object = None,
        anchor_entity_ids: Sequence[str] = (),
    ) -> MemoryContext:
        """Record the anchors and echo them back; the real re-rank has its own tests."""

        del memories, index
        anchors = tuple(anchor_entity_ids)
        self.anchored.append((request, anchors))
        if self.anchor_failure is not None:
            raise self.anchor_failure
        return replace(context, anchor_entity_ids=anchors)

    async def extract(
        self,
        request: MemoryContextRequest,
        *,
        messages: Sequence[object],
        memories: object,
        index: object = None,
    ) -> tuple[object, ...]:
        """Record one extraction request; the real extractor has its own tests."""

        del memories, index
        if self.extract_failure is not None:
            raise self.extract_failure
        self.extractions.append((request, tuple(messages)))
        return ()


def fake_runtime_config(
    *,
    corpus_access_mode: str = "source_acl",
    corpus_shared_tenant_id: str | None = None,
) -> SimpleNamespace:
    """The slice of ``RuntimeSettings`` chat retrieval reads off the runtime.

    Chat resolves a turn's corpus mode from ``runtime.config.runtime``, so any
    double standing in for the runtime has to carry it. The defaults mirror ``RuntimeSettings``: a tenant is
    ``source_acl`` unless it is *the* shared one, and there is no shared tenant
    unless a deployment names one.
    """

    return SimpleNamespace(
        runtime=SimpleNamespace(
            corpus_access_mode=corpus_access_mode,
            corpus_shared_tenant_id=corpus_shared_tenant_id,
        )
    )


class FakeRuntime:
    def __init__(
        self,
        chat: FakeChatFacade,
        retrieval: FakeRetrievalFacade | None = None,
        *,
        memory: FakeMemoryFacade | None = None,
        config: SimpleNamespace | None = None,
    ) -> None:
        self.chat = chat
        self.retrieval = retrieval or FakeRetrievalFacade()
        self.memory = memory or FakeMemoryFacade()
        self.config = config or fake_runtime_config()

    async def aclose(self) -> None:
        return None


class FakeConfigProvider:
    """Resolves ``retrieval.top_k`` and ``retrieval.rerank`` by level label, project before tenant."""

    def __init__(
        self, overrides: dict[str, int] | None = None, rerank: dict[str, bool] | None = None
    ) -> None:
        self.overrides = overrides or {}
        self.rerank = rerank or {}
        self.unavailable = False

    async def effective(self, tenant_id: str, level: SettingsLevel) -> EffectiveSettings:
        del tenant_id
        if self.unavailable:
            raise HarborUnavailableError("settings store is unavailable")
        values = {
            "retrieval.top_k": _resolved(self.overrides, level, default=5),
            "retrieval.rerank": _resolved(self.rerank, level, default=False),
        }
        overridden = any(value.origin != "default" for value in values.values())
        return EffectiveSettings(1 if overridden else 0, values)


def _resolved(
    overrides: dict[str, int] | dict[str, bool], level: SettingsLevel, *, default: object
) -> EffectiveValue:
    for candidate in reversed(level.chain()):
        if str(candidate) in overrides:
            return EffectiveValue(overrides[str(candidate)], candidate.kind)
    return EffectiveValue(default, "default")
