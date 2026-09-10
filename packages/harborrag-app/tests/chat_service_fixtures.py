"""Shared fakes for chat application-service tests."""

from __future__ import annotations

from collections.abc import AsyncIterator, Sequence
from dataclasses import replace

from harborrag_core.contracts.errors import HarborValidationError
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
    def __init__(
        self,
        failure: Exception | None = None,
        *,
        stream_failure: Exception | None = None,
        cost: float | None = None,
        answer: str = "Hello",
    ) -> None:
        # Citations are reported only for the sources an answer cites, so a
        # test that needs them must have the model actually cite.
        self.answer = answer
        self.failure = failure
        self.stream_failure = stream_failure
        self.cost = cost
        self.request: HarborChatRequest | None = None
        self.requests: list[HarborChatRequest] = []
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
        del prompt
        if self.failure is not None:
            raise self.failure
        self.request = request
        self.requests.append(request)
        return HarborChatResponse(
            id="chat-1",
            logical_model="primary",
            provider="mock",
            provider_model="mock-chat",
            deployment="internal-deployment",
            message=HarborChatMessage.assistant(self.answer),
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


class FakeRuntime:
    def __init__(
        self,
        chat: FakeChatFacade,
        retrieval: FakeRetrievalFacade | None = None,
        *,
        memory: FakeMemoryFacade | None = None,
    ) -> None:
        self.chat = chat
        self.retrieval = retrieval or FakeRetrievalFacade()
        self.memory = memory or FakeMemoryFacade()

    async def aclose(self) -> None:
        return None
