"""Agent cost includes tool turns, synthesis, and work preceding a resume."""

from __future__ import annotations

from dataclasses import replace

import pytest
from agent_test_helpers import Chat, Runs, Tools, checkpoint, response

from harborrag_core.models.chat import HarborChatMessage
from harborrag_core.models.cost import ModelCost
from harborrag_core.ports.agent_runs import AgentRunIdentity
from harborrag_engine.agent import AgentRunOptions, AgentService


@pytest.mark.asyncio
@pytest.mark.parametrize("max_steps", [1, 4])
@pytest.mark.parametrize("first_cost", [0.01, None])
async def test_every_generation_turn_contributes_to_cost(max_steps, first_cost) -> None:
    runs = Runs()
    chat = Chat(
        [
            response(call=("call-1", "vector_search", "{}")).model_copy(
                update={"estimated_cost_usd": first_cost}
            ),
            response(text="answer").model_copy(update={"estimated_cost_usd": 0.02}),
        ]
    )
    result = await AgentService(chat, Tools(), runs=runs).run(
        [HarborChatMessage.user("question")],
        AgentRunOptions(
            tenant_id="ACME", principal_id="reader", session_id="session", max_steps=max_steps
        ),
    )

    assert result.cost.amount_usd == pytest.approx(0.02 + (first_cost or 0))
    assert result.cost.model_calls == 2
    assert result.cost.complete is (first_cost is not None)
    assert runs.checkpoints[result.run_id].cost == result.cost


@pytest.mark.asyncio
@pytest.mark.parametrize("legacy", [False, True])
async def test_resume_preserves_prior_cost_or_marks_legacy_prices_unknown(legacy) -> None:
    identity = AgentRunIdentity("ACME", "reader", "session", "run-1", "reader")
    runs = Runs()
    runs.checkpoints[identity.run_id] = replace(
        checkpoint(identity), cost=ModelCost() if legacy else ModelCost().add_call(0.04)
    )
    chat = Chat([response().model_copy(update={"estimated_cost_usd": 0.02})])
    result = await AgentService(chat, Tools(), runs=runs).resume(
        identity.run_id,
        AgentRunOptions(tenant_id="ACME", principal_id="reader", session_id="session"),
    )

    assert result.cost.amount_usd == pytest.approx(0.02 if legacy else 0.06)
    assert result.cost.model_calls == 2
    assert result.cost.complete is not legacy
    assert runs.checkpoints[identity.run_id].cost == result.cost
