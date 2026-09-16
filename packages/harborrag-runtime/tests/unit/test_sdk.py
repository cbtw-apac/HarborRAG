from __future__ import annotations

import asyncio
from dataclasses import dataclass
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from harborrag_core.chunking import RelationType
from harborrag_core.domain.retrieval import RetrievalResult
from harborrag_core.indexing import FilterOperator
from harborrag_core.ingestion import (
    ExecutionCapabilityError,
    GraphEdgeRecord,
    GraphEntityType,
    GraphNodeRecord,
    GraphOwnershipScope,
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
from harborrag_core.topology.search import EvidenceBundle
from harborrag_engine.retrieval import RetrievalLane
from harborrag_runtime.chat import ChatPrompt, RuntimeChatService
from harborrag_runtime.sdk import (
    EvidenceFetchRequest,
    ExecutionMode,
    GraphTripletRequest,
    HarborRAG,
    HarborRAGConfig,
    IngestionRequest,
    RetrievalRequest,
)
from harborrag_runtime.sdk.facades import (
    GraphFacade,
    IngestionFacade,
    KnowledgeFacade,
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
@pytest.mark.parametrize("failure_type", [OSError, asyncio.CancelledError])
async def test_sdk_rebuilds_executor_after_failed_startup(monkeypatch, failure_type) -> None:
    from harborrag_runtime import plugins

    discover = Mock()
    monkeypatch.setattr(plugins, "discover_runtime_plugins", discover)
    failed = SimpleNamespace(start=AsyncMock(side_effect=failure_type()), aclose=AsyncMock())
    healthy = SimpleNamespace(start=AsyncMock(), aclose=AsyncMock())
    factory = Mock(side_effect=[failed, healthy])
    harbor = HarborRAG(HarborRAGConfig(discover_plugins=True), executor_factory=factory)

    with pytest.raises(failure_type):
        await harbor.start()
    failed.aclose.assert_awaited_once_with()

    await harbor.start()
    await harbor.start()

    assert factory.call_count == 2
    discover.assert_called_once_with()
    healthy.start.assert_awaited_once_with()
    await harbor.aclose()
    healthy.aclose.assert_awaited_once_with()


@pytest.mark.asyncio
async def test_sdk_concurrent_startup_waits_for_executor_readiness() -> None:
    entered = asyncio.Event()
    release = asyncio.Event()

    async def start() -> None:
        entered.set()
        await release.wait()

    executor = SimpleNamespace(start=AsyncMock(side_effect=start), aclose=AsyncMock())
    factory = Mock(return_value=executor)
    harbor = HarborRAG(HarborRAGConfig(discover_plugins=False), executor_factory=factory)
    first = asyncio.create_task(harbor.start())
    await entered.wait()
    second = asyncio.create_task(harbor.start())
    try:
        await asyncio.sleep(0)
        assert not second.done()
    finally:
        release.set()
        await asyncio.gather(first, second)

    factory.assert_called_once()
    executor.start.assert_awaited_once_with()
    await harbor.aclose()


@pytest.mark.asyncio
async def test_sdk_retains_executor_when_startup_cleanup_fails() -> None:
    failed = SimpleNamespace(
        start=AsyncMock(side_effect=OSError("startup failed")),
        aclose=AsyncMock(side_effect=[OSError("close failed"), None]),
    )
    healthy = SimpleNamespace(start=AsyncMock(), aclose=AsyncMock())
    factory = Mock(side_effect=[failed, healthy])
    harbor = HarborRAG(HarborRAGConfig(discover_plugins=False), executor_factory=factory)

    with pytest.raises(ExceptionGroup, match="startup and cleanup failed") as failure:
        await harbor.start()
    assert [str(error) for error in failure.value.exceptions] == [
        "startup failed",
        "close failed",
    ]

    await harbor.start()

    assert failed.aclose.await_count == 2
    healthy.start.assert_awaited_once_with()
    await harbor.aclose()


@pytest.mark.asyncio
async def test_sdk_shutdown_waits_for_startup_and_retries_failed_resources() -> None:
    entered = asyncio.Event()
    release = asyncio.Event()

    async def start() -> None:
        entered.set()
        await release.wait()

    executor = SimpleNamespace(
        start=AsyncMock(side_effect=start),
        aclose=AsyncMock(side_effect=[OSError("executor close failed"), None]),
    )
    retrieval = SimpleNamespace(
        aclose=AsyncMock(side_effect=[OSError("retrieval close failed"), None])
    )
    harbor = HarborRAG(
        HarborRAGConfig(discover_plugins=False), executor_factory=Mock(return_value=executor)
    )
    harbor._retrieval = retrieval
    startup = asyncio.create_task(harbor.start())
    await entered.wait()
    shutdown = asyncio.create_task(harbor.aclose())
    try:
        await asyncio.sleep(0)
        executor.aclose.assert_not_awaited()
    finally:
        release.set()
        await startup
        with pytest.raises(ExceptionGroup, match="resource close failed") as failure:
            await shutdown
    assert len(failure.value.exceptions) == 2

    await harbor.aclose()
    await harbor.aclose()

    assert executor.aclose.await_count == 2
    assert retrieval.aclose.await_count == 2


@pytest.mark.asyncio
async def test_sdk_shutdown_closes_retrieval_that_is_still_connecting() -> None:
    entered = asyncio.Event()
    release = asyncio.Event()
    retrieval = SimpleNamespace(aclose=AsyncMock())

    async def connect(_settings):
        entered.set()
        await release.wait()
        return retrieval

    harbor = HarborRAG(HarborRAGConfig(discover_plugins=False), retrieval_factory=connect)
    startup = asyncio.create_task(harbor._retrieval_service())
    await entered.wait()
    shutdown = asyncio.create_task(harbor.aclose())
    try:
        await asyncio.sleep(0)
        assert not shutdown.done()
    finally:
        release.set()
        await asyncio.gather(startup, shutdown)

    retrieval.aclose.assert_awaited_once_with()
    await harbor.aclose()
    retrieval.aclose.assert_awaited_once_with()


@pytest.mark.asyncio
async def test_sdk_discovers_plugins_before_retrieval_without_starting_ingestion(
    monkeypatch,
) -> None:
    from harborrag_runtime import plugins

    calls = []
    monkeypatch.setattr(plugins, "discover_runtime_plugins", lambda: calls.append("plugins"))
    retrieval = SimpleNamespace(aclose=AsyncMock())

    async def connect(_settings):
        calls.append("retrieval")
        return retrieval

    executor = SimpleNamespace(start=AsyncMock(), aclose=AsyncMock())
    factory = Mock(return_value=executor)
    harbor = HarborRAG(
        HarborRAGConfig(discover_plugins=True),
        retrieval_factory=connect,
        executor_factory=factory,
    )

    assert await harbor._retrieval_service() is retrieval
    assert await harbor._retrieval_service() is retrieval
    factory.assert_not_called()
    await harbor.start()

    assert calls == ["plugins", "retrieval"]
    await harbor.aclose()


@pytest.mark.asyncio
async def test_direct_sdk_rejects_durable_controls() -> None:
    executor = SimpleNamespace(start=AsyncMock())
    harbor = HarborRAG(HarborRAGConfig(), executor_factory=Mock(return_value=executor))

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
            evidence=EvidenceBundle(coverage_gaps=("fixture-gap",)),
        )

    async def aclose(self) -> None:
        self.closed = True

    async def search_graph_triplets(self, query, *, access):
        self.graph_call = (query, access)
        subject = GraphNodeRecord(
            node_key="node-a",
            node_kind=KnowledgeNodeKind.SOURCE_ENTITY,
            entity_type=GraphEntityType.GENERIC_SOURCE_ITEM,
            logical_id="document-a",
            ownership_scope=GraphOwnershipScope.SOURCE_SCOPE,
            owner_id="tenant-1",
            source_scope_id="scope-1",
        )
        object_node = GraphNodeRecord(
            node_key="node-b",
            node_kind=KnowledgeNodeKind.SOURCE_ENTITY,
            entity_type=GraphEntityType.GENERIC_SOURCE_ITEM,
            logical_id="document-b",
            ownership_scope=GraphOwnershipScope.SOURCE_SCOPE,
            owner_id="tenant-1",
            source_scope_id="scope-1",
        )
        relation = GraphEdgeRecord(
            relation_id="relation-1",
            relation_type=RelationType.LINKS_TO,
            source_node_key="node-a",
            target_node_key="node-b",
            ownership_scope=GraphOwnershipScope.SOURCE_SCOPE,
            owner_id="tenant-1",
            source_scope_id="scope-1",
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
async def test_sdk_aclose_still_closes_other_resources_when_one_raises() -> None:
    # A single failing resource must not prevent aclose() from attempting to
    # close the others, and the failure must still surface to the caller
    # rather than being silently swallowed.
    class _RaisingChatClient:
        async def achat(self, **kwargs):
            raise AssertionError("not used in this test")

        async def aclose(self) -> None:
            raise RuntimeError("chat client close failed")

    harbor = HarborRAG(HarborRAGConfig())
    harbor._chat_runtime = RuntimeChatService(
        harbor.config.runtime,
        client_builder=lambda _settings: _RaisingChatClient(),
    )
    # Force the chat runtime to actually build its client so aclose() has
    # something to fail on.
    await harbor._chat_runtime._configured_client()
    retrieval_service = _RetrievalService()
    harbor._retrieval = retrieval_service

    with pytest.raises(ExceptionGroup, match="HarborRAG resource close failed"):
        await harbor.aclose()

    assert retrieval_service.closed is True


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
            filters={"source_scope_id": ["scope-1", "scope-2"], "tenant_id": "tenant-1"},
            lane=RetrievalLane.DENSE,
            observe_graph=False,
        )
    )

    assert response.request_id == "request-1"
    assert response.evidence.coverage_gaps == ("fixture-gap",)
    assert service.call is not None
    _, kwargs = service.call
    assert kwargs["access"] is access
    assert kwargs["tenant_id"] == "tenant-1"
    conditions = {item.field: item for item in kwargs["options"].filters.must}
    assert conditions["source_scope_id"].operator == FilterOperator.IN
    assert conditions["source_scope_id"].value == ["scope-1", "scope-2"]
    assert conditions["tenant_id"].operator == FilterOperator.EQUALS
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


@pytest.mark.asyncio
async def test_ingestion_facade_delegates_every_lifecycle_operation() -> None:
    result = object()
    reference = object()
    status = object()
    owner = SimpleNamespace(
        _ingestion_run=AsyncMock(return_value=result),
        _ingestion_submit=AsyncMock(return_value=reference),
        _ingestion_status=AsyncMock(return_value=status),
        _ingestion_control=AsyncMock(),
    )
    facade = IngestionFacade(owner)
    request = SimpleNamespace()

    assert await facade.run(request) is result
    assert await facade.submit(request) is reference
    assert await facade.status("task") is status
    await facade.pause("task")
    await facade.resume("task")
    await facade.cancel("task")

    assert [call.args for call in owner._ingestion_control.await_args_list] == [
        ("task", "pause"),
        ("task", "resume"),
        ("task", "cancel"),
    ]


@pytest.mark.asyncio
async def test_graph_facade_delegates_paths_and_subgraphs() -> None:
    access = AccessContext(principal_id="reader", tenant_id="tenant")
    diagnostics = _Diagnostics()
    service = SimpleNamespace(
        search_graph_paths=AsyncMock(
            return_value=SimpleNamespace(paths=("path",), diagnostics=diagnostics)
        ),
        search_graph_subgraph=AsyncMock(
            return_value=SimpleNamespace(
                graph=SimpleNamespace(nodes=(), relations=()), diagnostics=diagnostics
            )
        ),
    )
    owner = SimpleNamespace(_retrieval_service=AsyncMock(return_value=service))
    facade = GraphFacade(owner)
    request = SimpleNamespace(query="query", access=access)

    paths = await facade.find_paths(request)
    subgraph = await facade.expand_subgraph(request)

    assert paths.paths == ("path",)
    assert subgraph.nodes == () and subgraph.relations == ()
    service.search_graph_paths.assert_awaited_once_with("query", access=access)
    service.search_graph_subgraph.assert_awaited_once_with("query", access=access)


@pytest.mark.asyncio
async def test_knowledge_facade_delegates_canonical_reads() -> None:
    response = object()
    service = SimpleNamespace(
        fetch_evidence=AsyncMock(return_value=response),
        read_evidence=AsyncMock(return_value=response),
        get_document_context=AsyncMock(return_value=response),
        list_readable_sources=AsyncMock(return_value=response),
        resolve_graph_nodes=AsyncMock(return_value=response),
        resolve_entities=AsyncMock(return_value=response),
        lookup_entities=AsyncMock(return_value=("mention",)),
        find_semantic_relations=AsyncMock(return_value=response),
        find_semantic_paths=AsyncMock(return_value=response),
    )
    owner = SimpleNamespace(_retrieval_service=AsyncMock(return_value=service))
    facade = KnowledgeFacade(owner)
    access = AccessContext(principal_id="reader", tenant_id="tenant")
    request = SimpleNamespace(
        access=access,
        chunk_ids=("chunk",),
        name="entity",
        limit=3,
        entity_id="entity-id",
        predicates=("owns",),
        direction="outgoing",
    )

    assert await facade.fetch_evidence(request) is response
    assert await facade.read_evidence(request) is response
    assert await facade.get_document_context(request) is response
    assert await facade.list_sources(request) is response
    assert await facade.resolve_graph_nodes(request) is response
    assert await facade.resolve_entities(request) is response
    assert await facade.lookup_entities(
        access=access, entity_ids=("entity-id",), chunk_ids=("chunk",)
    ) == ("mention",)
    assert await facade.find_relations(request) is response
    assert await facade.find_paths(request) is response

    service.resolve_entities.assert_awaited_once_with("entity", limit=3, access=access)
    service.find_semantic_relations.assert_awaited_once_with(
        "entity-id",
        predicates=("owns",),
        direction="outgoing",
        limit=3,
        access=access,
    )


@pytest.mark.asyncio
async def test_sdk_starts_once_runs_ingestion_and_closes_the_executor(monkeypatch) -> None:
    from harborrag_runtime import plugins

    executor = SimpleNamespace(
        start=AsyncMock(),
        run=AsyncMock(return_value="ingested"),
        aclose=AsyncMock(),
    )
    build = Mock(return_value=executor)
    discover = Mock()
    monkeypatch.setattr(plugins, "discover_runtime_plugins", discover)
    harbor = HarborRAG(HarborRAGConfig(discover_plugins=True), executor_factory=build)
    request = SimpleNamespace()

    assert await harbor.ingestion.run(request) == "ingested"
    await harbor.start()
    await harbor.aclose()

    discover.assert_called_once_with()
    build.assert_called_once_with(ExecutionMode.DIRECT, harbor.config.runtime)
    executor.start.assert_awaited_once_with()
    executor.run.assert_awaited_once_with(request)
    executor.aclose.assert_awaited_once_with()


@pytest.mark.asyncio
async def test_sdk_from_config_supports_async_context_management(monkeypatch, tmp_path) -> None:
    config = HarborRAGConfig()
    loaded = Mock(return_value=config)
    monkeypatch.setattr(HarborRAGConfig, "from_file", loaded)
    harbor = HarborRAG.from_config(tmp_path / "harborrag.yaml")
    start = AsyncMock()
    close = AsyncMock()
    monkeypatch.setattr(harbor, "start", start)
    monkeypatch.setattr(harbor, "aclose", close)

    async with harbor as entered:
        assert entered is harbor

    loaded.assert_called_once_with(tmp_path / "harborrag.yaml")
    start.assert_awaited_once_with()
    close.assert_awaited_once_with()


@pytest.mark.asyncio
async def test_temporal_sdk_delegates_durable_lifecycle_operations() -> None:
    executor = SimpleNamespace(
        start=AsyncMock(),
        submit=AsyncMock(return_value="reference"),
        status=AsyncMock(return_value="running"),
        pause=AsyncMock(),
        resume=AsyncMock(),
        cancel=AsyncMock(),
    )
    harbor = HarborRAG(
        HarborRAGConfig(execution_mode=ExecutionMode.TEMPORAL),
        executor_factory=Mock(return_value=executor),
    )
    request = SimpleNamespace()

    assert await harbor.ingestion.submit(request) == "reference"
    assert await harbor.ingestion.status("task") == "running"
    await harbor.ingestion.pause("task")
    await harbor.ingestion.resume("task")
    await harbor.ingestion.cancel("task")

    executor.submit.assert_awaited_once_with(request)
    executor.status.assert_awaited_once_with("task")
    executor.pause.assert_awaited_once_with("task")
    executor.resume.assert_awaited_once_with("task")
    executor.cancel.assert_awaited_once_with("task")


@pytest.mark.asyncio
async def test_sdk_connects_retrieval_once_and_exposes_chat_stream() -> None:
    response = object()
    service = SimpleNamespace(fetch_evidence=AsyncMock(return_value=response), aclose=AsyncMock())
    connect = AsyncMock(return_value=service)
    stream = object()
    chat = SimpleNamespace(stream=Mock(return_value=stream), aclose=AsyncMock())
    harbor = HarborRAG(
        HarborRAGConfig(),
        retrieval_factory=connect,
        chat_runtime_factory=Mock(return_value=chat),
    )
    access = AccessContext(principal_id="reader", tenant_id="tenant")
    request = EvidenceFetchRequest(access, ("chunk",))

    assert await harbor.knowledge.fetch_evidence(request) is response
    assert await harbor.knowledge.fetch_evidence(request) is response
    chat_request = SimpleNamespace()
    assert harbor.chat.stream(chat_request, prompt=ChatPrompt.CONCISE) is stream
    await harbor.aclose()

    connect.assert_awaited_once_with(harbor.config.runtime)
    service.aclose.assert_awaited_once_with()


@pytest.mark.asyncio
async def test_sdk_close_surfaces_fatal_resource_failures() -> None:
    class FatalResourceError(BaseException):
        pass

    chat = SimpleNamespace(aclose=AsyncMock(side_effect=FatalResourceError("fatal close")))
    harbor = HarborRAG(
        HarborRAGConfig(),
        chat_runtime_factory=Mock(return_value=chat),
    )

    with pytest.raises(BaseExceptionGroup, match="resource close failed"):
        await harbor.aclose()
