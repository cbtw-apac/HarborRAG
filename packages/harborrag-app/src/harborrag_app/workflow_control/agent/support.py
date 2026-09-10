"""Budget derivation and result projection shared by the agent application service."""

from __future__ import annotations

from dataclasses import dataclass, fields

from harborrag_core.contracts.errors import HarborConfigurationError
from harborrag_core.models.chat import HarborChatMessage, HarborChatRequest, HarborChatResponse
from harborrag_runtime.agent import AgentRunOptions, AgentRunResult
from harborrag_runtime.chat import ChatFacade, ChatPrompt

from .options import AgentExecutionOptions

# Matches ApiSettings.api_request_timeout_seconds' default; the HTTP transport
# always passes its configured deadline explicitly (AgentExecutionOptions).
_DEFAULT_DEADLINE_SECONDS = 120.0
# Headroom between the agent's graceful stop and the transport deadline, so the
# TIMEOUT stop reason is observable before the HTTP layer gives up.
_DEADLINE_MARGIN_SECONDS = 5.0
# ~8 tool results x 16k chars is roughly 32k tokens per step at chars/4; the
# budget must hold several such steps plus the conversation.
_DEFAULT_AGENT_TOKEN_BUDGET = 120_000


@dataclass(frozen=True, slots=True)
class DefaultPromptChat:
    """Apply the server-owned prompt, and the run's chosen model, to every step.

    The engine builds its own ``HarborChatRequest`` and knows nothing about
    model selection, so the chosen logical model is stamped here rather than
    threaded through the engine's run options. ``None`` leaves the field unset
    and the client resolves its configured default.
    """

    facade: ChatFacade
    model: str | None = None

    async def complete(self, request: HarborChatRequest) -> HarborChatResponse:
        if self.model is not None:
            request = request.model_copy(update={"logical_model": self.model})
        return await self.facade.complete(request, prompt=ChatPrompt.DEFAULT)


def _engine_synthesis_window_seconds() -> float:
    """The engine's default tool-free synthesis window, read from its contract.

    After the run guard expires the engine still spends up to this long on one
    final synthesis turn; the run timeout must leave room for it.
    """

    (field,) = (f for f in fields(AgentRunOptions) if f.name == "synthesis_timeout_seconds")
    default = field.default
    return float(default) if isinstance(default, (int, float)) else 0.0


def agent_timeout_seconds(deadline_seconds: float | None) -> float:
    """Derive the engine run timeout from the transport deadline.

    ``deadline - synthesis window - margin`` so the engine can stop gracefully,
    synthesize, and still return inside the HTTP/stream deadline.
    """

    deadline = _DEFAULT_DEADLINE_SECONDS if deadline_seconds is None else deadline_seconds
    timeout = deadline - _engine_synthesis_window_seconds() - _DEADLINE_MARGIN_SECONDS
    if timeout <= 0:
        raise HarborConfigurationError(
            f"agent deadline {deadline:.0f}s leaves no run budget after the "
            f"{_engine_synthesis_window_seconds():.0f}s synthesis window and "
            f"{_DEADLINE_MARGIN_SECONDS:.0f}s margin"
        )
    return timeout


def run_options(
    tenant_id: str,
    principal_id: str,
    options: AgentExecutionOptions,
    *,
    history: tuple[HarborChatMessage, ...] = (),
    memory_summary: str | None = None,
) -> AgentRunOptions:
    """Translate transport-neutral options into the engine's run options.

    ``history`` and ``memory_summary`` come from the application's memory
    policy, so the engine replays a budgeted window instead of recalling a
    fixed number of turns itself.
    """

    return AgentRunOptions(
        tenant_id=tenant_id,
        principal_id=principal_id,
        session_id=options.session_id,
        graph_search=options.graph_search,
        max_steps=options.max_steps,
        timeout_seconds=agent_timeout_seconds(options.deadline_seconds),
        max_total_tokens=(
            _DEFAULT_AGENT_TOKEN_BUDGET if options.token_budget is None else options.token_budget
        ),
        history=history,
        memory_summary=memory_summary,
        user_id=options.user_id,
    )


def result_data(
    result: AgentRunResult,
    *,
    session_id: str,
    project_id: str | None = None,
) -> dict[str, object]:
    """Project one finished run without leaking deployment metadata."""

    response = result.response
    return {
        "id": response.id,
        "run_id": result.run_id,
        "model": response.logical_model,
        "provider": response.provider,
        "provider_model": response.provider_model,
        "message": {"role": "assistant", "content": response.text},
        "finish_reason": str(response.finish_reason),
        "stop_reason": result.stop_reason.value,
        "usage": result.usage.model_dump(mode="json"),
        "turns": result.turns,
        "tool_call_count": len(result.executions),
        "tool_calls": [
            {
                "step": execution.step,
                "tool": execution.tool,
                "ok": execution.ok,
            }
            for execution in result.executions
        ],
        "session_id": session_id,
        "project_id": project_id,
    }


__all__ = ["DefaultPromptChat", "agent_timeout_seconds", "result_data", "run_options"]
