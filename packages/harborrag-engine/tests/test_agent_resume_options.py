"""A resume continues the run that was started, not a differently configured one."""

from __future__ import annotations

from datetime import UTC, datetime

from harborrag_core.models.chat import HarborChatUsage
from harborrag_core.ports.agent_runs import (
    AgentCheckpoint,
    AgentRunIdentity,
    AgentRunStatus,
)
from harborrag_engine.agent.schemas import AgentRunOptions
from harborrag_engine.agent.service import _resumed_options

NOW = datetime(2026, 1, 1, tzinfo=UTC)


def _checkpoint(**overrides: object) -> AgentCheckpoint:
    fields: dict[str, object] = {
        "identity": AgentRunIdentity("T", "P", "S", "run-1", "U"),
        "status": AgentRunStatus.FAILED,
        "step": 2,
        "version": 3,
        "messages": (),
        "executions": (),
        "usage": HarborChatUsage(),
        "stop_reason": None,
        "response": None,
        "created_at": NOW,
        "updated_at": NOW,
    }
    fields.update(overrides)
    return AgentCheckpoint(**fields)  # type: ignore[arg-type]


def _options(**overrides: object) -> AgentRunOptions:
    fields: dict[str, object] = {
        "tenant_id": "T",
        "principal_id": "P",
        "session_id": "S",
    }
    fields.update(overrides)
    return AgentRunOptions(**fields)  # type: ignore[arg-type]


def test_a_resumer_cannot_grant_tools_the_run_never_had() -> None:
    """Only logical_model was restored, so graph_search came from the caller.

    Resuming with it on handed the model graph tools that the earlier steps of
    the same transcript never had.
    """

    resumed = _resumed_options(
        _options(graph_search=True),
        _checkpoint(graph_search=False, logical_model="m"),
    )

    assert resumed.graph_search is False
    assert resumed.logical_model == "m"


def test_a_resumer_cannot_rebudget_a_run_in_progress() -> None:
    resumed = _resumed_options(
        _options(max_steps=8, max_total_tokens=99_000, timeout_seconds=600),
        _checkpoint(max_steps=3, max_total_tokens=1_000, timeout_seconds=30),
    )

    assert resumed.max_steps == 3
    assert resumed.max_total_tokens == 1_000
    assert resumed.timeout_seconds == 30


def test_a_checkpoint_predating_these_fields_keeps_the_callers_values() -> None:
    """``None`` means the run was recorded before the field existed."""

    caller = _options(graph_search=True, max_steps=5)

    resumed = _resumed_options(caller, _checkpoint())

    assert resumed.graph_search is True
    assert resumed.max_steps == 5
