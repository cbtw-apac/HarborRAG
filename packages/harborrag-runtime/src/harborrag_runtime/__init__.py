from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from harborrag_runtime.composition import CompositionRoot
    from harborrag_runtime.temporal.client import TemporalRuntimeClient
    from harborrag_runtime.temporal.lifecycle import RuntimeLifecycle

__all__ = ["CompositionRoot", "RuntimeLifecycle", "TemporalRuntimeClient"]


def __getattr__(name: str) -> Any:
    """Load the composition root lazily so configuration utilities stay standalone."""
    if name == "CompositionRoot":
        from harborrag_runtime.composition import CompositionRoot

        return CompositionRoot
    if name == "RuntimeLifecycle":
        from harborrag_runtime.temporal.lifecycle import RuntimeLifecycle

        return RuntimeLifecycle
    if name == "TemporalRuntimeClient":
        from harborrag_runtime.temporal.client import TemporalRuntimeClient

        return TemporalRuntimeClient
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
