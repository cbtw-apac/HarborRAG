from __future__ import annotations

from dataclasses import dataclass
from types import SimpleNamespace

import pytest

from harborrag_core.chunking import RelationType
from harborrag_core.domain.retrieval import RetrievalResult
from harborrag_core.ingestion import (
    ExecutionCapabilityError,
    GraphEdgeRecord,
    GraphNodeRecord,
    KnowledgeNodeKind,
)
from harborrag_core.models.chat import (
    HarborChatMessage,
    HarborChatRequest,
    HarborChatResponse,
    HarborChatUsage,
)
from harborrag_core.retrieval import GraphTriplet, GraphTripletQuery
from harborrag_core.security import AccessContext
from harborrag_engine.retrieval import RetrievalLane
from harborrag_runtime.chat import ChatPrompt, RuntimeChatService
from harborrag_runtime.sdk import (
    ExecutionMode,
    GraphTripletRequest,
    HarborRAG,
    HarborRAGConfig,
    IngestionRequest,
    RetrievalRequest,
)


def test_sdk_config_file_is_strict_and_defaults_to_direct(tmp_path) -> None:
    path = tmp_path / "harborrag.yaml"
    path.write_text(
        'discover_plugins: "false"\nruntime:\n  env: dev\n',
        encoding="utf-8",
    )

    config = HarborRAGConfig.from_file(path)

    assert config.execution_mode == ExecutionMode.DIRECT
    assert config.discover_plugins is False
    assert config.runtime.env == "dev"


@pytest.mark.asyncio
async def test_direct_sdk_rejects_durable_controls() -> None:
    harbor = HarborRAG(HarborRAGConfig())
    harbor._executor = SimpleNamespace()

    with pytest.raises(ExecutionCapabilityError, match="ingestion.run"):
        await harbor.ingestion.submit(
            IngestionRequest(
                access=AccessContext(principal_id="user-1", tenant_id="tenant-1"),
                connector_name="docs",
            )
        )


@dataclass
class _Diagnostics:
    candidate_hits: int = 1
    stale_candidates: int = 0
    unpublished_candidates: int = 0
    malformed_candidates: int = 0
    search_window: int = 1
    graph_nodes: int = 0
    graph_relations: int = 0
    graph_truncated: bool = False
    duration_ms: float = 1.0


class _RetrievalService:
    def __init__(self) -> None:
        self.call = None
        self.closed = False

    async def retrieve(self, query, **kwargs):
        self.call = (query, kwargs)
        return SimpleNamespace(
            request_id="request-1",
            lane=kwargs["options"].lane,
            results=(RetrievalResult("chunk-1", "text", 0.9),),
            diagnostics=_Diagnostics(),
        )

    async def aclose(self) -> None:
        self.closed = True

    async def search_graph_triplets(self, query, *, access):
        self.graph_call = (query, access)
        subject = GraphNodeRecord(
            node_key="node-a",
            node_kind=KnowledgeNodeKind.DOCUMENT,
            logical_id="document-a",
            document_id="document-a",
            document_version_id="version-a",
            source_scope_id="scope-1",
        )
        object_node = GraphNodeRecord(
            node_key="node-b",
            node_kind=KnowledgeNodeKind.DOCUMENT,
            logical_id="document-b",
            document_id="document-b",
            document_version_id="version-b",
            source_scope_id="scope-1",
        )
        relation = GraphEdgeRecord(
            relation_id="relation-1",
            relation_type=RelationType.LINKS_TO,
            source_node_key="node-a",
            target_node_key="node-b",
            document_version_id="version-a",
            source_relation_version="source-v1",
            source_explicit=True,
        )
        return SimpleNamespace(
            triplets=(
                GraphTriplet(
                    subject=subject,
                    predicate=relation,
                    object=object_node,
                ),
            ),
            diagnostics=_Diagnostics(),
        )


class _ChatClient:
    def __init__(self) -> None:
        self.request = None
        self.closed = False

    async def achat(self, messages=None, *, request=None, model=None, **kwargs):
        del messages, model, kwargs
        self.request = request
        return HarborChatResponse(
            id="chat-1",
            logical_model="primary",
            provider="mock",
            provider_model="mock-chat",
            deployment="mock-primary",
            message=HarborChatMessage.assistant("Hello from Harbor"),
            finish_reason="stop",
            usage=HarborChatUsage(
                prompt_tokens=3,
                completion_tokens=3,
                total_tokens=6,
            ),
        )

    async def aclose(self) -> None:
        self.closed = True


@pytest.mark.asyncio
async def test_sdk_chat_facade_uses_and_closes_the_configured_client() -> None:
    harbor = HarborRAG(HarborRAGConfig())
    client = _ChatClient()
    harbor._chat_runtime = RuntimeChatService(
        harbor.config.runtime,
        client_builder=lambda _settings: client,
    )
    request = HarborChatRequest(messages=(HarborChatMessage.user("Hello"),))

    response = await harbor.chat.complete(request, prompt=ChatPrompt.CONCISE)

    assert response.text == "Hello from Harbor"
    assert client.request.messages[0].role.value == "system"
    assert "concisely" in client.request.messages[0].content
    assert client.request.messages[1:] == request.messages
    await harbor.aclose()
    assert client.closed is True


@pytest.mark.asyncio
async def test_sdk_retrieval_preserves_access_and_builds_filters() -> None:
    harbor = HarborRAG(HarborRAGConfig())
    service = _RetrievalService()
    harbor._retrieval = service
    access = AccessContext(principal_id="user-1", tenant_id="tenant-1")

    response = await harbor.retrieval.search(
        RetrievalRequest(
            access=access,
            query="retention policy",
            top_k=3,
            filters={"source_scope_id": "scope-1"},
            lane=RetrievalLane.DENSE,
            observe_graph=False,
        )
    )

    assert response.request_id == "request-1"
    assert service.call is not None
    _, kwargs = service.call
    assert kwargs["access"] is access
    assert kwargs["tenant_id"] == "tenant-1"
    assert kwargs["options"].filters.must[0].field == "source_scope_id"
    assert kwargs["options"].lane == RetrievalLane.DENSE
    assert kwargs["options"].observe_graph is False
    assert response.lane == RetrievalLane.DENSE
    await harbor.aclose()
    assert service.closed is True


@pytest.mark.asyncio
async def test_sdk_graph_facade_preserves_access_context() -> None:
    harbor = HarborRAG(HarborRAGConfig())
    service = _RetrievalService()
    harbor._retrieval = service
    access = AccessContext(principal_id="reader-1", tenant_id="tenant-1")

    response = await harbor.graph.search_triplets(
        GraphTripletRequest(
            access=access,
            query=GraphTripletQuery(predicate=RelationType.LINKS_TO),
        )
    )

    assert response.triplets[0].predicate.relation_type == RelationType.LINKS_TO
    assert service.graph_call[1] is access
