"""The MCP policy is the shared tool budget, relabelled -- nothing more."""

from __future__ import annotations

from harborrag_engine.tools.base import MAX_TOOL_RESULTS
from harborrag_engine.tools.budgets import MAX_ARGUMENT_BYTES, MAX_OUTPUT_BYTES, ToolBudget
from harborrag_mcp_server.configuration.models import PolicyConfiguration
from harborrag_mcp_server.policy import McpToolPolicy


def test_the_mcp_policy_cannot_drift_from_the_shared_ceilings() -> None:
    """MCP restates no ceiling of its own, so none can fall out of step.

    Every advertised input-schema maximum is derived from ``MAX_TOOL_RESULTS``.
    When the policy re-declared the same number as a literal, raising the shared
    constant made the transport reject calls its own advertised schema declared
    valid.
    """

    policy = McpToolPolicy()

    assert isinstance(policy, ToolBudget)
    assert policy.max_results == MAX_TOOL_RESULTS
    assert policy.label == "MCP"
    assert policy.detail_in_errors is False


def test_the_configured_ceiling_matches_the_advertised_schema_maximum() -> None:
    """An unconfigured server must not reject what its own schema advertised.

    ``vector_search``'s ``top_k`` maximum is derived from ``MAX_TOOL_RESULTS``.
    While the policy's own ceiling was a literal, raising that constant left the
    transport accepting the call, doing the work, and then failing the result
    budget afterwards.
    """

    configured = PolicyConfiguration()

    assert configured.max_results == MAX_TOOL_RESULTS
    assert configured.max_argument_bytes == MAX_ARGUMENT_BYTES
    assert configured.max_output_bytes == MAX_OUTPUT_BYTES
    assert McpToolPolicy(**configured.model_dump()).max_results == MAX_TOOL_RESULTS
