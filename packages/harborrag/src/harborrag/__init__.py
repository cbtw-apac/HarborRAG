"""Stable, lightweight public facade for HarborRAG."""

from __future__ import annotations

from importlib import import_module
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from harborrag_core import Document
    from harborrag_core.chunking import RelationType
    from harborrag_core.models.chat import (
        HarborChatMessage,
        HarborChatRequest,
        HarborChatResponse,
    )
    from harborrag_core.retrieval import (
        GraphDirection,
        GraphPathQuery,
        GraphSubgraphQuery,
        GraphTripletQuery,
    )
    from harborrag_core.security import AccessContext
    from harborrag_runtime.chat import ChatPrompt
    from harborrag_runtime.sdk import (
        ExecutionMode,
        GraphPathRequest,
        GraphPathResponse,
        GraphSubgraphRequest,
        GraphSubgraphResponse,
        GraphTripletRequest,
        GraphTripletResponse,
        HarborRAG,
        HarborRAGConfig,
        IngestionRequest,
        IngestionResult,
        IngestionStatus,
        IngestionTaskReference,
        RetrievalLane,
        RetrievalRequest,
        RetrievalResponse,
    )

__all__ = [
    "AccessContext",
    "ChatPrompt",
    "Document",
    "ExecutionMode",
    "GraphDirection",
    "GraphPathQuery",
    "GraphPathRequest",
    "GraphPathResponse",
    "GraphSubgraphQuery",
    "GraphSubgraphRequest",
    "GraphSubgraphResponse",
    "GraphTripletQuery",
    "GraphTripletRequest",
    "GraphTripletResponse",
    "HarborChatMessage",
    "HarborChatRequest",
    "HarborChatResponse",
    "HarborRAG",
    "HarborRAGConfig",
    "IngestionRequest",
    "IngestionResult",
    "IngestionStatus",
    "IngestionTaskReference",
    "RelationType",
    "RetrievalLane",
    "RetrievalRequest",
    "RetrievalResponse",
]

_EXPORT_MODULES = {
    "AccessContext": "harborrag_core.security",
    "ChatPrompt": "harborrag_runtime.chat",
    "Document": "harborrag_core",
    "GraphDirection": "harborrag_core.retrieval",
    "GraphPathQuery": "harborrag_core.retrieval",
    "GraphSubgraphQuery": "harborrag_core.retrieval",
    "GraphTripletQuery": "harborrag_core.retrieval",
    "HarborChatMessage": "harborrag_core.models.chat",
    "HarborChatRequest": "harborrag_core.models.chat",
    "HarborChatResponse": "harborrag_core.models.chat",
    "RelationType": "harborrag_core.chunking",
    **dict.fromkeys(
        (
            "ExecutionMode",
            "GraphPathRequest",
            "GraphPathResponse",
            "GraphSubgraphRequest",
            "GraphSubgraphResponse",
            "GraphTripletRequest",
            "GraphTripletResponse",
            "HarborRAG",
            "HarborRAGConfig",
            "IngestionRequest",
            "IngestionResult",
            "IngestionStatus",
            "IngestionTaskReference",
            "RetrievalLane",
            "RetrievalRequest",
            "RetrievalResponse",
        ),
        "harborrag_runtime.sdk",
    ),
}


def __getattr__(name: str) -> Any:
    """Load each public contract only when a caller first requests it."""
    try:
        module_name = _EXPORT_MODULES[name]
    except KeyError as error:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}") from error

    value = getattr(import_module(module_name), name)
    globals()[name] = value
    return value


def __dir__() -> list[str]:
    """Include lazy public exports in interactive discovery."""
    return sorted(set(globals()) | set(__all__))
