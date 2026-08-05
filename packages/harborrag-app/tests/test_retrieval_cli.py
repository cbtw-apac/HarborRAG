from __future__ import annotations

import json
from types import SimpleNamespace

import pytest
from app_test_fixtures import MockAppService

from harborrag_app.cli import main as cli
from harborrag_app.cli import runner as cli_runner
from harborrag_app.workflow_control.client import AppService, AppServiceFactories
from harborrag_app.workflow_control.retrieval_response import retrieval_response
from harborrag_core.domain.retrieval import RetrievalResult
from harborrag_core.retrieval import GraphPathQuery, GraphSubgraphQuery, GraphTripletQuery
from harborrag_runtime.sdk import RetrievalLane, RetrievalResponse


class FakeComposition:
    def diagnostics(self):
        return {"runtime": {"ready": True}}

    async def aclose(self) -> None:
        return None


class FakeRetrievalFacade:
    def __init__(self) -> None:
        self.calls = []

    async def search(self, request):
        self.calls.append(request)
        return RetrievalResponse(
            request_id="retrieval-safe",
            lane=request.lane,
            results=(
                RetrievalResult(
                    "revision-hash",
                    "private document text",
                    0.75,
                    {"retrieval_source": "qdrant"},
                ),
            ),
            diagnostics={
                "candidate_hits": 3,
                "stale_candidates": 4,
                "unpublished_candidates": 2,
                "malformed_candidates": 1,
                "search_window": 20,
                "graph_nodes": 0,
                "graph_relations": 0,
                "graph_truncated": False,
                "duration_ms": 12.5,
            },
        )


class FakeGraphFacade:
    def __init__(self) -> None:
        self.calls = []

    async def search_triplets(self, request):
        self.calls.append(("triplets", request))
        return SimpleNamespace(
            triplets=({"subject": "document:1"},),
            diagnostics={"accepted_count": 1},
        )

    async def find_paths(self, request):
        self.calls.append(("paths", request))
        return SimpleNamespace(
            paths=({"nodes": ["document:1", "section:1"]},),
            diagnostics={"accepted_count": 1},
        )

    async def expand_subgraph(self, request):
        self.calls.append(("subgraph", request))
        return SimpleNamespace(
            nodes=({"node_key": "document:1"},),
            relations=({"relation_id": "relation:1"},),
            diagnostics={"accepted_count": 1},
        )


class FakeRetrievalRuntime:
    def __init__(self) -> None:
        self.retrieval = FakeRetrievalFacade()
        self.graph = FakeGraphFacade()
        self.closed = False

    async def aclose(self) -> None:
        self.closed = True


def test_retrieval_response_enforces_top_k_even_if_backend_over_returns() -> None:
    response = RetrievalResponse(
        request_id="retrieval-over-return",
        lane=RetrievalLane.HYBRID,
        results=tuple(
            RetrievalResult(f"chunk-{index}", f"text-{index}", 1.0 - index / 10, {})
            for index in range(4)
        ),
        diagnostics={},
    )

    projected = retrieval_response(
        response,
        include_content=True,
        include_metadata=False,
        top_k=2,
    )

    assert [item["id"] for item in projected.data["results"]] == ["chunk-0", "chunk-1"]
    assert "task_id" not in projected.data


@pytest.mark.asyncio
async def test_app_retrieval_omits_content_by_default_and_closes_resources() -> None:
    retrieval = FakeRetrievalRuntime()

    def factory(settings):
        del settings
        return retrieval

    service = AppService(
        FakeComposition(),  # type: ignore[arg-type]
        factories=AppServiceFactories(
            retrieval_runtime=factory,  # type: ignore[arg-type]
        ),
    )

    response = await service.retrieve(
        "release acceptance",
        tenant_id="tenant-1",
        top_k=3,
    )
    await service.aclose()

    assert response.ok is True
    assert response.data["results"] == [
        {
            "rank": 1,
            "id": "revision-hash",
            "score": 0.75,
            "source": "qdrant",
        }
    ]
    assert "private document text" not in str(response.data)
    request = retrieval.retrieval.calls[0]
    assert request.query == "release acceptance"
    assert request.access.tenant_id == "tenant-1"
    assert request.top_k == 3
    assert retrieval.closed is True


@pytest.mark.asyncio
async def test_app_retrieval_includes_content_only_when_requested() -> None:
    retrieval = FakeRetrievalRuntime()

    def factory(settings):
        del settings
        return retrieval

    service = AppService(
        FakeComposition(),  # type: ignore[arg-type]
        factories=AppServiceFactories(
            retrieval_runtime=factory,  # type: ignore[arg-type]
        ),
    )

    response = await service.retrieve(
        "release acceptance",
        tenant_id="tenant-1",
        include_content=True,
    )

    assert response.data["results"][0]["content"] == "private document text"


@pytest.mark.asyncio
async def test_app_retrieval_applies_score_threshold_and_reranks_results() -> None:
    retrieval = FakeRetrievalRuntime()
    service = AppService(
        FakeComposition(),  # type: ignore[arg-type]
        factories=AppServiceFactories(
            retrieval_runtime=lambda settings: retrieval,  # type: ignore[arg-type]
        ),
    )

    response = await service.retrieve(
        "release acceptance",
        tenant_id="tenant-1",
        score_threshold=0.8,
    )

    assert response.ok is True
    assert response.data["results"] == []


@pytest.mark.asyncio
async def test_app_retrieval_forwards_lane_filters_graph_and_metadata() -> None:
    retrieval = FakeRetrievalRuntime()
    service = AppService(
        FakeComposition(),  # type: ignore[arg-type]
        factories=AppServiceFactories(
            retrieval_runtime=lambda settings: retrieval,  # type: ignore[arg-type]
        ),
    )

    response = await service.retrieve(
        "release acceptance",
        tenant_id="tenant-1",
        principal_id="reader-1",
        filters={"category": "release"},
        lane=RetrievalLane.SPARSE,
        observe_graph=False,
        include_metadata=True,
    )

    request = retrieval.retrieval.calls[0]
    assert request.access.principal_id == "reader-1"
    assert request.filters == {"category": "release"}
    assert request.lane == RetrievalLane.SPARSE
    assert request.observe_graph is False
    assert response.data["lane"] == "sparse"
    assert response.data["results"][0]["metadata"]["retrieval_source"] == "qdrant"


@pytest.mark.asyncio
async def test_app_graph_retrieval_uses_shared_runtime_facade_and_access_context() -> None:
    retrieval = FakeRetrievalRuntime()
    service = AppService(
        FakeComposition(),  # type: ignore[arg-type]
        factories=AppServiceFactories(
            retrieval_runtime=lambda settings: retrieval,  # type: ignore[arg-type]
        ),
    )

    triplets = await service.retrieve_graph_triplets(
        GraphTripletQuery(subject="document:1"),
        tenant_id="tenant-1",
        principal_id="reader-1",
    )
    paths = await service.retrieve_graph_paths(
        GraphPathQuery(start_node="document:1", end_node="section:1"),
        tenant_id="tenant-1",
        principal_id="reader-1",
    )
    subgraph = await service.retrieve_graph_subgraph(
        GraphSubgraphQuery(start_node="document:1"),
        tenant_id="tenant-1",
        principal_id="reader-1",
    )

    assert triplets.data["triplets"] == [{"subject": "document:1"}]
    assert paths.data["paths"][0]["nodes"] == ["document:1", "section:1"]
    assert subgraph.data["nodes"] == [{"node_key": "document:1"}]
    for _operation, request in retrieval.graph.calls:
        assert request.access.tenant_id == "tenant-1"
        assert request.access.principal_id == "reader-1"


def test_retrieval_cli_has_secret_safe_json_by_default(monkeypatch, capsys) -> None:
    monkeypatch.setattr(cli_runner, "runtime_app_service", MockAppService)

    exit_code = cli.main(
        [
            "retrieve",
            "release acceptance",
            "--tenant",
            "tenant-1",
            "--top-k",
            "3",
            "--json",
        ]
    )

    payload = json.loads(capsys.readouterr().out)
    assert exit_code == 0
    assert payload["ok"] is True
    assert payload["data"]["diagnostics"]["vector_hits"] == 1
    assert "content" not in payload["data"]["results"][0]


def test_retrieval_cli_accepts_lane_filters_and_graph_control(monkeypatch, capsys) -> None:
    service = MockAppService()
    monkeypatch.setattr(cli_runner, "runtime_app_service", lambda: service)

    exit_code = cli.main(
        [
            "retrieve",
            "release acceptance",
            "--tenant",
            "tenant-1",
            "--lane",
            "sparse",
            "--filters-json",
            '{"category":"release"}',
            "--no-graph",
            "--include-metadata",
            "--json",
        ]
    )

    assert exit_code == 0
    assert service.retrieval_calls[0]["lane"] == RetrievalLane.SPARSE
    assert service.retrieval_calls[0]["filters"] == {"category": "release"}
    assert service.retrieval_calls[0]["observe_graph"] is False
    assert "metadata" in json.loads(capsys.readouterr().out)["data"]["results"][0]


def test_unexpected_provider_error_message_is_not_public(monkeypatch, capsys) -> None:
    def fail_to_build():
        raise RuntimeError("private-url?token=private")

    monkeypatch.setattr(cli_runner, "runtime_app_service", fail_to_build)

    exit_code = cli.main(["retrieve", "query", "--tenant", "tenant-1", "--json"])

    payload = json.loads(capsys.readouterr().out)
    assert exit_code == 1
    assert payload["error"] == "RuntimeError"
    assert "private" not in str(payload)
