"""Developer instructions injected before the tool-free final synthesis turn."""

from __future__ import annotations

from harborrag_core.ports.agent_runs import AgentStopReason

_SYNTHESIS_INSTRUCTIONS: dict[AgentStopReason, str] = {
    AgentStopReason.MAX_STEPS: (
        "The tool-call budget is exhausted. Answer now using only the evidence already "
        "returned by tools. State clearly when the evidence is insufficient."
    ),
    AgentStopReason.TIMEOUT: (
        "The time budget for tool use is exhausted. Answer now using only the evidence "
        "already returned by tools. State clearly when the evidence is insufficient."
    ),
    AgentStopReason.REPEATED_TOOL_CALL: (
        "The same tool call was repeated with identical arguments, so further tool use "
        "is blocked. Answer now using only the evidence already returned by tools. State "
        "clearly when the evidence is insufficient."
    ),
    AgentStopReason.TOKEN_BUDGET_EXCEEDED: (
        "The total token budget for this run is exhausted. Answer now using only the "
        "evidence already returned by tools. State clearly when the evidence is "
        "insufficient."
    ),
}


def synthesis_instruction(stop_reason: AgentStopReason) -> str:
    """Return the instruction telling the model why it must answer without tools now."""

    return _SYNTHESIS_INSTRUCTIONS[stop_reason]


__all__ = ["synthesis_instruction"]
