from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Literal

MAX_TOOL_RESULTS = 20


@dataclass(frozen=True, slots=True)
class ToolSpec:
    name: str
    description: str
    input_schema: dict[str, Any] = field(default_factory=lambda: {"type": "object"})
    capability: Literal["read", "write", "ingestion", "admin"] = "read"
    output_schema: dict[str, Any] | None = None
    annotations: dict[str, Any] | None = None


class BaseTool(ABC):
    """Contract for a service-level tool shared by agent and MCP transports."""

    spec: ToolSpec

    @abstractmethod
    async def call(
        self,
        arguments: dict[str, object],
        *,
        principal_id: str,
    ) -> dict[str, object]:
        raise NotImplementedError
