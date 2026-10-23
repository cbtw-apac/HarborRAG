from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Literal

from harborrag_core.security import AccessContext

MAX_TOOL_RESULTS = 20


@dataclass(frozen=True, slots=True)
class ToolBehavior:
    read_only: bool = False
    destructive: bool = False
    idempotent: bool = False
    open_world: bool = False


@dataclass(frozen=True, slots=True)
class ToolSpec:
    name: str
    description: str
    input_schema: dict[str, Any] = field(default_factory=lambda: {"type": "object"})
    capability: Literal["read", "write", "ingestion", "admin"] = "read"
    output_schema: dict[str, Any] | None = None
    # Compatibility for third-party tool specifications that still provide
    # protocol annotations directly. Built-in reader tools use behavior.
    annotations: dict[str, Any] | None = None
    behavior: ToolBehavior | None = None


class BaseTool(ABC):
    """Contract for a service-level tool shared by agent and MCP transports."""

    spec: ToolSpec

    @abstractmethod
    async def call(
        self,
        arguments: dict[str, object],
        *,
        principal_id: str,
        context: ToolInvocationContext | None = None,
    ) -> dict[str, object]:
        raise NotImplementedError


@dataclass(frozen=True, slots=True)
class ToolInvocationContext:
    """Identity established by the caller's trusted transport or runtime."""

    access: AccessContext
    invocation_id: str | None = None
