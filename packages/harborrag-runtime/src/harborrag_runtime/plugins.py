from __future__ import annotations

from dataclasses import dataclass
from importlib.metadata import EntryPoint, entry_points
from typing import Any

PLUGIN_GROUPS = (
    "harborrag.connectors",
    "harborrag.parsers",
    "harborrag.model_providers",
    "harborrag.vector_repositories",
    "harborrag.graph_repositories",
    "harborrag.object_stores",
)


@dataclass(frozen=True, slots=True)
class RuntimePlugin:
    group: str
    name: str
    product: object


def discover_runtime_plugins() -> tuple[RuntimePlugin, ...]:
    """Explicitly load and validate configured Python entry-point providers."""

    discovered: list[RuntimePlugin] = []
    identities: set[tuple[str, str]] = set()
    available = entry_points()
    for group in PLUGIN_GROUPS:
        selected: tuple[EntryPoint, ...] = tuple(available.select(group=group))
        for entry_point in selected:
            identity = (group, entry_point.name)
            if identity in identities:
                raise ValueError(
                    f"duplicate HarborRAG plugin {entry_point.name!r} in group {group!r}"
                )
            identities.add(identity)
            product: Any = entry_point.load()
            if isinstance(product, type):
                product = product()
            if callable(product) and not hasattr(product, "register"):
                product = product()
            capabilities = getattr(product, "capabilities", None)
            if capabilities is None:
                raise ValueError(
                    f"HarborRAG plugin {entry_point.name!r} does not declare capabilities"
                )
            register = getattr(product, "register", None)
            if not callable(register):
                raise ValueError(
                    f"HarborRAG plugin {entry_point.name!r} does not provide register()"
                )
            register()
            discovered.append(RuntimePlugin(group, entry_point.name, product))
    return tuple(discovered)
