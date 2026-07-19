from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from harborrag_runtime.composition import CompositionRoot

__all__ = ["CompositionRoot"]


def __getattr__(name: str) -> Any:
    """Load the composition root lazily so configuration utilities stay standalone."""
    if name == "CompositionRoot":
        from harborrag_runtime.composition import CompositionRoot

        return CompositionRoot
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
