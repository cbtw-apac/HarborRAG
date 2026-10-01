"""Classify backend failures for tool responses without leaking details.

Tool responses never carry exception text: hosts, ports, credentials and
provider payloads stay in the server log. They do carry a coarse error class
and the component that failed, so an MCP client can tell an authentication
problem on the graph store from an embedding provider outage without reading
that log.
"""

from __future__ import annotations

import asyncio
from typing import Final

ERROR_CLASSES: Final[tuple[str, ...]] = (
    "authentication",
    "connection",
    "timeout",
    "configuration",
    "invalid_request",
    "internal",
)

_COMPONENT_BY_MODULE_PREFIX: Final[tuple[tuple[str, str], ...]] = (
    ("harborrag_adapters.models.embed", "embedding"),
    ("harborrag_adapters.models.chat", "chat_model"),
    ("harborrag_adapters.models.rerank", "reranker"),
    ("harborrag_adapters.models", "model_provider"),
    ("litellm", "model_provider"),
    ("qdrant_client", "vector_store"),
    ("harborrag_adapters.repositories.vector", "vector_store"),
    ("falkordb", "graph_store"),
    ("redis", "graph_store"),
    ("harborrag_adapters.repositories.graph", "graph_store"),
    ("asyncpg", "control_plane"),
    ("sqlalchemy", "control_plane"),
    ("alembic", "control_plane"),
    ("aioboto3", "object_store"),
    ("aiobotocore", "object_store"),
    ("botocore", "object_store"),
    ("httpx", "network"),
    ("aiohttp", "network"),
)

_COMPONENT_BY_NAME_TOKEN: Final[tuple[tuple[str, str], ...]] = (
    ("embed", "embedding"),
    ("qdrant", "vector_store"),
    ("falkor", "graph_store"),
    ("graph", "graph_store"),
)


def _chain(exc: BaseException) -> list[BaseException]:
    seen: list[BaseException] = []
    current: BaseException | None = exc
    while current is not None and current not in seen and len(seen) < 8:
        seen.append(current)
        current = current.__cause__ or current.__context__
    return seen


def _class_of(exc: BaseException) -> str:
    name = type(exc).__name__.lower()
    if isinstance(exc, (asyncio.TimeoutError, TimeoutError)) or "timeout" in name:
        return "timeout"
    if any(token in name for token in ("auth", "permission", "forbidden", "unauthorized")):
        return "authentication"
    if isinstance(exc, (ConnectionError, OSError)) or any(
        token in name for token in ("connect", "unavailable", "unreachable", "refused")
    ):
        return "connection"
    if any(token in name for token in ("config", "validation", "notfound", "missing")):
        return "configuration"
    if any(token in name for token in ("invalidrequest", "badrequest", "unsupported")):
        return "invalid_request"
    return "internal"


def _component_of(exc: BaseException) -> str | None:
    module = type(exc).__module__ or ""
    for prefix, component in _COMPONENT_BY_MODULE_PREFIX:
        if module == prefix or module.startswith(prefix + "."):
            return component
    name = type(exc).__name__.lower()
    for token, component in _COMPONENT_BY_NAME_TOKEN:
        if token in name:
            return component
    return None


def classify_backend_error(exc: BaseException, *, default_component: str) -> dict[str, str]:
    """Return ``{"error_class", "component"}`` for ``exc`` and its cause chain.

    The first exception in the chain with a recognisable class wins for
    ``error_class``; the first with a recognisable origin wins for
    ``component``. Nothing from the exception text is returned.
    """

    chain = _chain(exc)
    error_class = next((c for c in (_class_of(e) for e in chain) if c != "internal"), "internal")
    component = next((c for c in (_component_of(e) for e in chain) if c), default_component)
    return {"error_class": error_class, "component": component}
