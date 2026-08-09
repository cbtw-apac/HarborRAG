from __future__ import annotations

import inspect
from datetime import UTC, datetime, timedelta
from math import inf, nan

import pytest
from pydantic import ValidationError

from harborrag_core.chunking import RelationType
from harborrag_core.domain.provider import Provider
from harborrag_core.domain.source_config import SourceConfig
from harborrag_core.ingestion import (
    GraphEdgeRecord,
    GraphEntityType,
    GraphNodeRecord,
    GraphOwnershipScope,
    KnowledgeNodeKind,
)
from harborrag_core.models.errors import HarborModelError
from harborrag_core.ports.control_plane import TenantScopedRepositoryProvider
from harborrag_core.ports.memory import (
    Memory,
    MemoryOwner,
    MemoryQuery,
    MemoryScope,
    MemoryType,
)
from harborrag_core.retrieval.graph import GraphPath
from harborrag_core.schemas.state import WorkflowState
from harborrag_core.security.url_policy import URLPolicy, URLPolicyError


def test_url_policy_rejects_missing_and_non_global_resolved_hosts() -> None:
    with pytest.raises(URLPolicyError, match="include a host"):
        URLPolicy().validate("http:///internal")

    seen: list[tuple[str, int]] = []

    def private_resolver(host: str, port: int) -> tuple[str, ...]:
        seen.append((host, port))
        return ("127.0.0.1",)

    for url in (
        "http://2130706433/",
        "http://017700000001/",
        "http://0x7f000001/",
        "https://internal.example/",
    ):
        with pytest.raises(URLPolicyError, match="not allowed"):
            URLPolicy(resolver=private_resolver).validate(url)
    assert seen[-1] == ("internal.example", 443)


def test_url_policy_normalizes_denylist_and_accepts_only_global_resolution() -> None:
    with pytest.raises(URLPolicyError, match="denied"):
        URLPolicy(
            denied_hosts={"BLOCKED.EXAMPLE"},
            resolver=lambda _host, _port: ("93.184.216.34",),
        ).validate("https://blocked.example./path")
    URLPolicy(resolver=lambda _host, _port: ("93.184.216.34",)).validate(
        "https://public.example/path"
    )


def test_control_plane_defines_a_tenant_bound_repository_capability() -> None:
    signature = inspect.signature(TenantScopedRepositoryProvider.for_access)
    assert signature.parameters["access"].default is inspect.Parameter.empty


def test_tenant_owned_aggregates_and_secret_references_are_validated() -> None:
    provider = Provider(
        id="provider-1",
        name="Provider",
        family="chat",
        tenant_id="tenant-a",
        config={"token": {"secret_ref": "secret://vault/provider"}},
    )
    assert provider.tenant_id == "tenant-a"
    with pytest.raises(ValueError, match="secret reference"):
        Provider(
            id="provider-1",
            tenant_id="DEFAULT",
            name="Provider",
            family="chat",
            config={"api-key": "raw-secret"},
        )
    with pytest.raises(ValueError, match="secret reference"):
        SourceConfig(
            id="source-1",
            tenant_id="DEFAULT",
            project_id="project-1",
            source_type="github",
            name="Repository",
            config={"nested": {"password": "raw-secret"}},
        )

    for config in (
        {"clientSecret": "raw-secret"},
        {"outer": [[{"api_key": "raw-secret"}]]},
        {"accessToken": "raw-secret"},
    ):
        with pytest.raises(ValueError, match="secret reference"):
            Provider(
                id="provider-2",
                tenant_id="DEFAULT",
                name="Provider",
                family="chat",
                config=config,
            )


def test_model_error_diagnostics_are_sanitized_recursively() -> None:
    error = HarborModelError(
        "provider failed api_key=message-secret",
        metadata={
            "nested": {"access-token": "metadata-secret"},
            "clientSecret": "camel-secret",
            "detail": "password=detail-secret",
        },
    )
    payload = error.to_dict()
    rendered = repr(payload)
    assert "message-secret" not in str(error)
    assert "metadata-secret" not in rendered
    assert "detail-secret" not in rendered
    assert "camel-secret" not in rendered


def test_strict_models_recursively_freeze_mutable_containers() -> None:
    state = WorkflowState(
        workflow_id="workflow-1",
        tenant_id="tenant-a",
        payload={"nested": {"items": [1]}},
    )
    with pytest.raises(TypeError, match="immutable"):
        state.payload["new"] = 2
    with pytest.raises(TypeError, match="immutable"):
        state.payload["nested"]["items"].append(2)


@pytest.mark.parametrize("number", [nan, inf, -inf])
def test_graph_attributes_reject_non_finite_numbers(number: float) -> None:
    with pytest.raises(ValidationError, match="finite"):
        _tenant_node("node-1", attributes={"ordinal": number})


def test_graph_attributes_reject_excessive_sequence_depth() -> None:
    value: object = "leaf"
    for _ in range(6):
        value = [value]
    with pytest.raises(ValidationError, match="nesting"):
        _tenant_node("node-1", attributes={"ordinal": value})


def test_graph_path_requires_each_relation_to_connect_adjacent_nodes() -> None:
    left = _tenant_node("left")
    right = _tenant_node("right")
    unrelated = GraphEdgeRecord(
        relation_id="edge-1",
        relation_type=RelationType.CONTAINS,
        source_node_key="other-left",
        target_node_key="other-right",
        ownership_scope=GraphOwnershipScope.TENANT,
        owner_id="tenant-a",
        source_relation_version="1",
        source_explicit=True,
    )
    with pytest.raises(ValidationError, match="adjacent"):
        GraphPath(nodes=(left, right), relations=(unrelated,))


def test_memory_models_use_aware_time_and_validate_bounds() -> None:
    owner = MemoryOwner(tenant_id="tenant-a", principal_id="user-1")
    memory = Memory(
        memory_id="memory-1",
        scope=MemoryScope.USER,
        memory_type=MemoryType.FACT,
        owner=owner,
        content="fact",
    )
    assert memory.created_at.tzinfo is UTC
    with pytest.raises(ValueError, match="importance"):
        Memory(
            memory_id="memory-2",
            scope=MemoryScope.USER,
            memory_type=MemoryType.FACT,
            owner=owner,
            content="fact",
            importance=1.1,
        )
    with pytest.raises(ValueError, match="timezone-aware"):
        Memory(
            memory_id="memory-3",
            scope=MemoryScope.USER,
            memory_type=MemoryType.FACT,
            owner=owner,
            content="fact",
            created_at=datetime.now(),
            updated_at=datetime.now(),
        )
    with pytest.raises(ValueError, match="between 1 and 1000"):
        MemoryQuery(owner=owner, limit=0)
    with pytest.raises(ValueError, match="follow"):
        Memory(
            memory_id="memory-4",
            scope=MemoryScope.USER,
            memory_type=MemoryType.FACT,
            owner=owner,
            content="fact",
            expires_at=datetime.now(UTC) - timedelta(seconds=1),
        )


def _tenant_node(node_key: str, *, attributes: dict[str, object] | None = None) -> GraphNodeRecord:
    return GraphNodeRecord(
        node_key=node_key,
        node_kind=KnowledgeNodeKind.TENANT,
        entity_type=GraphEntityType.TENANT,
        logical_id=node_key,
        ownership_scope=GraphOwnershipScope.TENANT,
        owner_id="tenant-a",
        attributes=attributes or {},
    )
