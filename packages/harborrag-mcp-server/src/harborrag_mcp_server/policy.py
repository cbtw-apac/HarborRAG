"""MCP's compiled safety ceilings, applied before and after every dispatch."""

from __future__ import annotations

from dataclasses import dataclass

from harborrag_runtime.tools.base import ToolSpec
from harborrag_runtime.tools.budgets import ToolBudget


@dataclass(frozen=True, slots=True)
class McpToolPolicy:
    """MCP's view of the shared tool ceilings.

    The limits themselves are transport-neutral and live with the tools, so the
    agent transport enforces the same ones; this type stays because MCP's
    configuration layer resolves per-tenant values into it and because the
    ``MCP ...`` message wording is part of this transport's reported contract.
    """

    max_results: int = 20
    max_argument_bytes: int = 64 * 1024
    max_output_bytes: int = 1024 * 1024
    allow_ingestion: bool = False

    def _budget(self) -> ToolBudget:
        return ToolBudget(
            label="MCP",
            max_results=self.max_results,
            max_argument_bytes=self.max_argument_bytes,
            max_output_bytes=self.max_output_bytes,
            allow_ingestion=self.allow_ingestion,
        )

    def check_call(self, spec: ToolSpec, arguments: dict[str, object]) -> None:
        self._budget().check_call(spec, arguments)

    def check_results(self, count: int) -> None:
        self._budget().check_results(count)

    def check_output(self, result: dict[str, object]) -> None:
        self._budget().check_output(result)

    def check_output_schema(
        self,
        result: dict[str, object],
        schema: dict[str, object] | None,
    ) -> None:
        self._budget().check_output_schema(result, schema)
