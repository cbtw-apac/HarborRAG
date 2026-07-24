from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True, slots=True)
class McpToolSpec:
    name: str
    description: str
    input_schema: dict[str, Any] = field(default_factory=lambda: {"type": "object"})


class BaseMcpTool(ABC):
    """Contract for a service-level MCP tool."""

    spec: McpToolSpec

    @abstractmethod
    def call(self, arguments: dict[str, object]) -> dict[str, object]:
        raise NotImplementedError
