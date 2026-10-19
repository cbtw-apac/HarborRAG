from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Mapping

from harborrag_core.contracts.tools import ToolSpec


class BaseMcpServer(ABC):
    """Contract for an MCP server exposing only audited service-level tools."""

    @abstractmethod
    def list_tools(self, tenant_id: str | None = None) -> list[ToolSpec]:
        raise NotImplementedError

    @abstractmethod
    async def call_tool(
        self,
        name: str,
        arguments: dict[str, object] | None = None,
        *,
        principal_id: str = "in-process",
    ) -> dict[str, object]:
        raise NotImplementedError


def tool_reported_error(result: dict[str, object]) -> bool:
    """Whether a tool's own payload says the call failed.

    One predicate for both readers. The audit treated ``status == "error"`` as
    a failure while the MCP handler raised only on ``ok is False``, so the two
    could disagree about the same result -- a call recorded as failed and
    returned as successful.
    """

    return result.get("ok") is False or result.get("status") == "error"


def declares_tenant(input_schema: Mapping[str, object]) -> bool:
    """Whether a tool's input schema binds it to a tenant.

    Fail closed: a schema whose property list cannot be read counts as scoped, so a
    malformed spec can only ever refuse a call, never widen one.
    """

    properties = input_schema.get("properties")
    return not isinstance(properties, dict) or "tenant_id" in properties
