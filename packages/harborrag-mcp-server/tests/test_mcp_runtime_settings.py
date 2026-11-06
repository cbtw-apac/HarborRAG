"""``vector_search`` takes its default ``top_k`` from the live ``retrieval.top_k`` override."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

import harborrag_mcp_server
from harborrag_core.config_store import SettingsLevel, SettingsSnapshot
from harborrag_core.config_store.resolution import EffectiveSettings, EffectiveValue
from harborrag_core.contracts.reader import RetrievalLane
from harborrag_core.domain.retrieval import RetrievalResult
from harborrag_mcp_server.audit import McpAuditLog
from harborrag_mcp_server.configuration import McpConfigurationStore
from harborrag_mcp_server.server.server import McpServer
from harborrag_runtime.composition.readers import open_reader_application
from harborrag_runtime.config.settings import RuntimeSettings
from harborrag_runtime.config_store import CachedConfigProvider
from harborrag_runtime.config_store.defaults import registry_for
from harborrag_runtime.retrieval.contracts import RetrievalDiagnostics, RuntimeRetrievalReport

pytestmark = pytest.mark.unit

PACKAGED_CONFIG = Path(harborrag_mcp_server.__file__).parent / "defaults" / "mcp.yaml"


class FakeConfigProvider:
    """Reports one tenant-level ``retrieval.top_k`` override, or the default origin."""

    def __init__(self, override: int | None) -> None:
        self.override = override
        self.levels: list[str] = []

    async def effective(self, tenant_id: str, level: SettingsLevel) -> EffectiveSettings:
        del tenant_id
        self.levels.append(str(level))
        if self.override is None:
            value = EffectiveValue(5, "default")
        else:
            value = EffectiveValue(self.override, "tenant")
        return EffectiveSettings(1, {"retrieval.top_k": value})


class RecordingRetrieval:
    def __init__(self, hits: int) -> None:
        self.hits = hits
        self.requests: list[object] = []

    async def search(self, request):
        self.requests.append(request)
        results = tuple(
            RetrievalResult(
                id=f"chunk-{index}",
                text=f"text {index}",
                score=1.0 - index / 100,
                metadata={
                    "document_id": "document-1",
                    "document_version_id": "version-1",
                    "record_kind": "evidence",
                    "chunk_kind": "text",
                    "connector_type": "local",
                    "citation_locator": {},
                    "retrieval_source": "qdrant-authoritative",
                    "document_title": "Document One",
                    "section_path": ["Section 1"],
                },
            )
            for index in range(self.hits)
        )
        return SimpleNamespace(
            request_id="retrieval-1",
            lane=request.lane,
            results=results[: request.top_k],
            diagnostics={
                "candidate_hits": self.hits,
                "stale_candidates": 0,
                "unpublished_candidates": 0,
                "malformed_candidates": 0,
                "search_window": self.hits,
                "graph_nodes": 0,
                "graph_relations": 0,
                "graph_truncated": False,
                "duration_ms": 0.0,
                "graph_documents": [],
            },
        )


def _server(provider: FakeConfigProvider, retrieval: RecordingRetrieval) -> McpServer:
    runtime = SimpleNamespace(retrieval=retrieval)
    server = McpServer(runtime=runtime, audit=McpAuditLog(), config_provider=provider)
    server.configuration = McpConfigurationStore.load(
        path=PACKAGED_CONFIG, specs=server.list_tools(), audit=server.audit
    )
    return server


async def _search(server: McpServer, **arguments: object) -> dict[str, object]:
    return await server.call_tool(
        "vector_search", {"query": "harbor", "tenant_id": "demo", **arguments}
    )


@pytest.mark.asyncio
async def test_vector_search_uses_tenant_top_k_override() -> None:
    provider, retrieval = FakeConfigProvider(3), RecordingRetrieval(10)

    result = await _search(_server(provider, retrieval))

    assert retrieval.requests[0].top_k == 3
    assert len(result["results"]) <= 3
    assert provider.levels == ["tenant"]


@pytest.mark.asyncio
async def test_default_origin_keeps_mcp_yaml_default() -> None:
    retrieval = RecordingRetrieval(10)

    await _search(_server(FakeConfigProvider(None), retrieval))

    assert retrieval.requests[0].top_k == 5


@pytest.mark.asyncio
async def test_explicit_top_k_wins() -> None:
    retrieval = RecordingRetrieval(10)

    await _search(_server(FakeConfigProvider(3), retrieval), top_k=8)

    assert retrieval.requests[0].top_k == 8


@pytest.mark.asyncio
async def test_override_clamped_to_policy() -> None:
    retrieval = RecordingRetrieval(30)

    await _search(_server(FakeConfigProvider(50), retrieval))

    assert retrieval.requests[0].top_k == 20


@pytest.mark.asyncio
async def test_override_change_applies_without_restart() -> None:
    provider, retrieval = FakeConfigProvider(None), RecordingRetrieval(10)
    server = _server(provider, retrieval)

    await _search(server)
    provider.override = 3
    await _search(server)

    assert [request.top_k for request in retrieval.requests] == [5, 3]


class TenantTopKStore:
    """Settings store holding one tenant-level ``retrieval.top_k`` override."""

    def __init__(self, tenant_id: str, top_k: int) -> None:
        self.snapshot = SettingsSnapshot(tenant_id, 1, {"tenant": {"retrieval.top_k": top_k}})

    async def load(self, tenant_id: str) -> SettingsSnapshot:
        del tenant_id
        return self.snapshot

    async def latest_revision(self, tenant_id: str) -> int:
        del tenant_id
        return self.snapshot.revision

    async def ping(self) -> None:
        return None


class RecordingRetrievalService:
    def __init__(self) -> None:
        self.top_k: list[int] = []

    async def retrieve(self, query: str, *, tenant_id: str, top_k: int, **_: object):
        del query, tenant_id
        self.top_k.append(top_k)
        diagnostics = RetrievalDiagnostics(0, 0, 0, 0, top_k, 0, 0, False, 0.0)
        return RuntimeRetrievalReport("retrieval-1", RetrievalLane.HYBRID, (), diagnostics)

    async def aclose(self) -> None:
        return None


@pytest.mark.asyncio
async def test_server_built_before_the_reader_starts_applies_the_tenant_override(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("HARBORRAG_TOOL_EXECUTION_AUDIT_PATH", str(tmp_path / "execution.jsonl"))
    service = RecordingRetrievalService()

    async def connect_retrieval_service(_settings: RuntimeSettings) -> RecordingRetrievalService:
        return service

    def build_config_provider(settings: RuntimeSettings, **_: object) -> CachedConfigProvider:
        return CachedConfigProvider(TenantTopKStore("demo", 3), registry_for(settings))

    monkeypatch.setattr(
        "harborrag_runtime.retrieval.composition.connect_retrieval_service",
        connect_retrieval_service,
    )
    monkeypatch.setattr(
        "harborrag_runtime.composition.config_store.build_config_provider", build_config_provider
    )
    settings = RuntimeSettings(control_db_url="postgresql+asyncpg://reader@127.0.0.1:1/control")
    runtime = open_reader_application(settings)
    server = McpServer(
        invoker=runtime.invoker,
        references=runtime.references,
        audit=McpAuditLog(),
        corpus_mode=settings.corpus_access_mode,
        shared_tenant_id=settings.corpus_shared_tenant_id,
        config_provider=runtime.config_provider,
    )
    server.configuration = McpConfigurationStore.load(
        path=PACKAGED_CONFIG, specs=server.list_tools(), audit=server.audit
    )

    await runtime.start()
    try:
        await _search(server)
    finally:
        await runtime.aclose()

    assert service.top_k == [3]
