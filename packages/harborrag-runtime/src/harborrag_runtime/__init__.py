from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from harborrag_runtime.composition import CompositionRoot
    from harborrag_runtime.sdk import HarborRAG, HarborRAGConfig

__all__ = ["CompositionRoot", "HarborRAG", "HarborRAGConfig"]


def __getattr__(name: str) -> Any:
    """Load the composition root lazily so configuration utilities stay standalone."""
    if name == "CompositionRoot":
        from harborrag_runtime.composition import CompositionRoot

        return CompositionRoot
    if name in {"HarborRAG", "HarborRAGConfig"}:
        from harborrag_runtime.sdk import HarborRAG, HarborRAGConfig

        return {"HarborRAG": HarborRAG, "HarborRAGConfig": HarborRAGConfig}[name]
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
