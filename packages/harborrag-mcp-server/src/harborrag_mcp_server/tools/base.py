from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Literal


@dataclass(frozen=True, slots=True)
class McpToolSpec:
    name: str
    description: str
    input_schema: dict[str, Any] = field(default_factory=lambda: {"type": "object"})
    capability: Literal["read", "ingestion", "admin"] = "read"
    output_schema: dict[str, Any] | None = None
    annotations: dict[str, Any] | None = None


class BaseMcpTool(ABC):
    """Contract for a service-level MCP tool."""

    spec: McpToolSpec

    @abstractmethod
    async def call(
        self,
        arguments: dict[str, object],
        *,
        principal_id: str,
    ) -> dict[str, object]:
        raise NotImplementedError
