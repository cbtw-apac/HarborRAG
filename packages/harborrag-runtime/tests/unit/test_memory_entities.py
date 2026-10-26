"""Tests for resolving memory entity mentions against the knowledge graph."""

from __future__ import annotations

import logging

import pytest

from harborrag_core.ingestion import (
    GraphEntityType,
    GraphNodeRecord,
    GraphOwnershipScope,
    KnowledgeGraphTraversal,
    KnowledgeNodeKind,
)
from harborrag_engine.retrieval import AuthoritativeSubgraphResult, GraphSearchDiagnostics
from harborrag_runtime.config.settings import RuntimeSettings
from harborrag_runtime.memory.entities import (
    MAX_MENTIONS,
    GraphMemoryEntityResolver,
    build_memory_entity_resolver,
)


def _node(node_key: str, *, title: str | None = None, logical_id: str | None = None):
    return GraphNodeRecord(
        node_key=node_key,
        node_kind=KnowledgeNodeKind.DOCUMENT_VERSION,
        entity_type=GraphEntityType.DOCUMENT_VERSION,
        logical_id=logical_id or node_key,
        ownership_scope=GraphOwnershipScope.DOCUMENT_VERSION,
        owner_id="tenant-1",
        source_scope_id="scope-1",
        document_id="document-1",
        document_version_id="version-1",
        title=title,
    )


def _result(*nodes: GraphNodeRecord) -> AuthoritativeSubgraphResult:
    return AuthoritativeSubgraphResult(
        graph=KnowledgeGraphTraversal(nodes=tuple(nodes), relations=(), truncated=False),
        diagnostics=GraphSearchDiagnostics(
            candidate_count=len(nodes),
            accepted_count=len(nodes),
            stale_count=0,
            unpublished_count=0,
            projection_truncated=False,
        ),
    )


class _Lookup:
    """A graph that knows one node per lowercased title, plus by node key."""

    def __init__(self, nodes: dict[str, GraphNodeRecord], *, available: bool = True) -> None:
        self._nodes = nodes
        self.available = available
        self.calls: list[tuple[str, str]] = []

    @property
    def graph_retrieval_available(self) -> bool:
        return self.available

    async def search_graph_subgraph(self, query, *, access):
        self.calls.append((query.start_node, str(access.tenant_id)))
        node = self._nodes.get(query.start_node.casefold())
        return _result(*((node,) if node is not None else ()))


class _RaisingLookup(_Lookup):
    async def search_graph_subgraph(self, query, *, access):
        raise RuntimeError("graph is down")


@pytest.mark.asyncio
async def test_a_mention_matching_a_node_title_resolves_to_its_node_key() -> None:
    lookup = _Lookup({"atlas migration": _node("node-atlas", title="Atlas Migration")})
    resolver = GraphMemoryEntityResolver(lookup)

    resolved = await resolver.resolve_entities(("Atlas Migration",), tenant_id="tenant-1")

    assert [(item.mention, item.entity_id) for item in resolved] == [
        ("Atlas Migration", "node-atlas")
    ]
    # An exact title match, not merely a case-insensitive one.
    assert resolved[0].confidence == pytest.approx(0.9)


@pytest.mark.asyncio
async def test_confidence_falls_with_match_exactness_and_drops_below_the_floor() -> None:
    nodes = {
        "node-atlas": _node("node-atlas", title="Atlas Migration"),
        "atlas migration": _node("node-atlas", title="Atlas Migration"),
        "elsewhere": _node("node-other", title="Something Else"),
    }
    resolver = GraphMemoryEntityResolver(_Lookup(nodes))

    resolved = await resolver.resolve_entities(
        ("node-atlas", "atlas migration", "elsewhere"),
        tenant_id="tenant-1",
    )
    scores = {item.mention: item.confidence for item in resolved}

    # The mention *is* the node's identifier.
    assert scores["node-atlas"] == pytest.approx(1.0)
    # Only the lowercased title matched, which is what the lookup itself matches on.
    assert scores["atlas migration"] == pytest.approx(0.75)
    # A node came back but its title is not this mention at all, so it is no match.
    assert "elsewhere" not in scores


@pytest.mark.asyncio
async def test_a_node_below_the_configured_floor_is_not_returned() -> None:
    lookup = _Lookup({"atlas migration": _node("node-atlas", title="Atlas Migration")})
    resolver = GraphMemoryEntityResolver(lookup, min_confidence=0.95)

    assert await resolver.resolve_entities(("atlas migration",), tenant_id="tenant-1") == ()


@pytest.mark.asyncio
async def test_resolution_is_tenant_scoped_and_never_crosses_tenants() -> None:
    lookup = _Lookup({"atlas": _node("node-atlas", title="Atlas")})
    resolver = GraphMemoryEntityResolver(lookup)

    await resolver.resolve_entities(("Atlas",), tenant_id="tenant-a")
    await resolver.resolve_entities(("Atlas",), tenant_id="tenant-b")

    # Every lookup carried exactly the tenant it was asked for; nothing else.
    assert [tenant for _, tenant in lookup.calls] == ["tenant-a", "tenant-b"]


@pytest.mark.asyncio
async def test_a_blank_tenant_resolves_nothing_without_touching_the_graph() -> None:
    lookup = _Lookup({"atlas": _node("node-atlas", title="Atlas")})

    assert (
        await GraphMemoryEntityResolver(lookup).resolve_entities(("Atlas",), tenant_id="  ") == ()
    )
    assert lookup.calls == []


@pytest.mark.asyncio
async def test_mentions_are_deduplicated_case_insensitively_and_capped() -> None:
    lookup = _Lookup({})
    resolver = GraphMemoryEntityResolver(lookup)

    await resolver.resolve_entities(
        ("Atlas", "atlas", " ATLAS ", "", "   ", *[f"other-{index}" for index in range(40)]),
        tenant_id="tenant-1",
    )

    assert len(lookup.calls) == MAX_MENTIONS
    # The first spelling seen is the one queried, and it is queried once.
    assert [mention for mention, _ in lookup.calls][:2] == ["Atlas", "other-0"]


@pytest.mark.asyncio
async def test_a_failing_graph_yields_no_anchors_and_logs_a_warning(
    caplog: pytest.LogCaptureFixture,
) -> None:
    resolver = GraphMemoryEntityResolver(_RaisingLookup({}))

    with caplog.at_level(logging.WARNING, logger="harborrag.runtime.memory"):
        resolved = await resolver.resolve_entities(("Atlas",), tenant_id="tenant-1")

    assert resolved == ()
    assert "Resolving memory entity mentions failed" in caplog.text


@pytest.mark.asyncio
async def test_the_builder_returns_none_when_graph_retrieval_is_unavailable(
    caplog: pytest.LogCaptureFixture,
) -> None:
    lookup = _Lookup({}, available=False)

    async def provider():
        return lookup

    with caplog.at_level(logging.WARNING, logger="harborrag.runtime.memory"):
        resolver = await build_memory_entity_resolver(RuntimeSettings(), provider)

    assert resolver is None
    assert "no graph retrieval" in caplog.text


@pytest.mark.asyncio
async def test_the_builder_returns_none_when_retrieval_cannot_be_reached(
    caplog: pytest.LogCaptureFixture,
) -> None:
    async def provider():
        raise RuntimeError("no vector store configured")

    with caplog.at_level(logging.WARNING, logger="harborrag.runtime.memory"):
        resolver = await build_memory_entity_resolver(RuntimeSettings(), provider)

    assert resolver is None
    assert "retrieval error_type=RuntimeError" in caplog.text


@pytest.mark.asyncio
async def test_the_entity_linking_setting_gates_the_resolver_off() -> None:
    calls = 0

    async def provider():
        nonlocal calls
        calls += 1
        return _Lookup({})

    settings = RuntimeSettings(memory_entity_linking=False)

    assert await build_memory_entity_resolver(settings, provider) is None
    # Switched off means retrieval is never even asked for.
    assert calls == 0


@pytest.mark.asyncio
async def test_the_builder_returns_a_resolver_when_the_graph_is_configured() -> None:
    lookup = _Lookup({"atlas": _node("node-atlas", title="Atlas")})

    async def provider():
        return lookup

    resolver = await build_memory_entity_resolver(RuntimeSettings(), provider)

    assert resolver is not None
    resolved = await resolver.resolve_entities(("Atlas",), tenant_id="tenant-1")
    assert [item.entity_id for item in resolved] == ["node-atlas"]
