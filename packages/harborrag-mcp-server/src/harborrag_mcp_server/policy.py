"""MCP's compiled safety ceilings, applied before and after every dispatch."""

from __future__ import annotations

from dataclasses import dataclass

from harborrag_engine.tools.budgets import ToolBudget


@dataclass(frozen=True, slots=True)
class McpToolPolicy(ToolBudget):
    """MCP's view of the shared tool ceilings.

    The limits themselves are transport-neutral and live with the tools, so the
    agent transport enforces the same ones. This type stays because MCP's
    configuration layer resolves per-tenant values into it and because the
    ``MCP ...`` message wording is part of this transport's reported contract --
    but it only overrides the label, so a ceiling added to ``ToolBudget`` is
    enforced here the day it lands rather than the day someone remembers to
    mirror it.
    """

    label: str = "MCP"
