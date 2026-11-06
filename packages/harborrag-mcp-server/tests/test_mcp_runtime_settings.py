"""``vector_search`` takes its default ``top_k`` from the live ``retrieval.top_k`` override."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

import harborrag_mcp_server
from harborrag_core.config_store import SettingsLevel
from harborrag_core.config_store.resolution import EffectiveSettings, EffectiveValue
from harborrag_core.domain.retrieval import RetrievalResult
from harborrag_mcp_server.audit import McpAuditLog
from harborrag_mcp_server.configuration import McpConfigurationStore
from harborrag_mcp_server.server.server import McpServer

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
