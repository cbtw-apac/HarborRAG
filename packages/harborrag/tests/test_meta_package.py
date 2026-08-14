from __future__ import annotations

import subprocess
import sys
import tomllib
from pathlib import Path

import harborrag
from harborrag import (
    AccessContext,
    ChatPrompt,
    Document,
    ExecutionMode,
    GraphPathQuery,
    GraphPathRequest,
    GraphSubgraphQuery,
    GraphSubgraphRequest,
    GraphTripletQuery,
    GraphTripletRequest,
    HarborChatMessage,
    HarborChatRequest,
    HarborRAG,
    HarborRAGConfig,
    RelationType,
    RetrievalLane,
)

PACKAGE_ROOT = Path(__file__).resolve().parents[1]


def test_meta_exports_implemented_public_facade() -> None:
    assert Document.__name__ == "Document"
    assert AccessContext.__name__ == "AccessContext"
    assert HarborRAG.__name__ == "HarborRAG"
    assert HarborRAGConfig.__name__ == "HarborRAGConfig"
    assert ExecutionMode.DIRECT == "direct"
    assert RetrievalLane.HYBRID == "hybrid"
    assert ChatPrompt.CONCISE == "concise"
    assert RelationType.LINKS_TO == "links_to"
    assert "CompositionRoot" not in harborrag.__all__


def test_every_declared_public_export_resolves() -> None:
    assert set(harborrag.__all__) <= set(dir(harborrag))
    assert all(getattr(harborrag, name) is not None for name in harborrag.__all__)


def test_chat_sdk_request_is_constructible_from_public_exports() -> None:
    request = HarborChatRequest(messages=(HarborChatMessage.user("Hello"),))

    assert request.messages[0].content == "Hello"


def test_graph_sdk_requests_are_constructible_from_public_exports() -> None:
    access = AccessContext(principal_id="user-1", tenant_id="tenant-1")

    triplet = GraphTripletRequest(
        access=access,
        query=GraphTripletQuery(subject="document-1"),
    )
    path = GraphPathRequest(
        access=access,
        query=GraphPathQuery(start_node="document-1", end_node="document-2"),
    )
    subgraph = GraphSubgraphRequest(
        access=access,
        query=GraphSubgraphQuery(start_node="document-1"),
    )

    assert triplet.access == path.access == subgraph.access == access


def test_base_install_declares_only_directly_imported_packages() -> None:
    project = tomllib.loads((PACKAGE_ROOT / "pyproject.toml").read_text(encoding="utf-8"))[
        "project"
    ]

    assert project["version"] == "0.1.0"
    assert project["dependencies"] == [
        "harborrag-core==0.1.0",
        "harborrag-runtime==0.1.0",
    ]
    assert project["optional-dependencies"]["chat"] == ["harborrag-adapters[llm]==0.1.0"]
    assert project["optional-dependencies"]["local"] == [
        "harborrag-adapters[chunking,control-plane,falkordb,llm,parsers,"
        "pdf-docling,qdrant,s3,tables]==0.1.0"
    ]
    assert "harborrag-adapters[parsers-all]==0.1.0" in project["optional-dependencies"]["all"]


def test_base_facade_import_does_not_load_optional_providers() -> None:
    script = """
import sys
import harborrag
from harborrag import HarborRAG

assert HarborRAG.__name__ == "HarborRAG"
for module in ("aioboto3", "falkordb", "litellm", "qdrant_client", "temporalio"):
    assert module not in sys.modules, module
"""

    subprocess.run([sys.executable, "-c", script], check=True, timeout=15)
