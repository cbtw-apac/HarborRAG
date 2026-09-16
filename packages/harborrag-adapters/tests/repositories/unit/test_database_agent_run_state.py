"""Agent checkpoint JSON retains evidence provenance across resume."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime

from harborrag_adapters.repositories.database.control_plane.agent_runs import (
    _state_from_json,
    _state_to_json,
)
from harborrag_core.models.chat import HarborChatMessage, HarborChatUsage
from harborrag_core.ports.agent_runs import (
    AgentCheckpoint,
    AgentEvidenceReference,
    AgentRunIdentity,
    AgentRunStatus,
    AgentToolExecution,
)


def _checkpoint() -> AgentCheckpoint:
    now = datetime.now(UTC)
    return AgentCheckpoint(
        identity=AgentRunIdentity("ACME", "reader", "session", "run", "user"),
        status=AgentRunStatus.RUNNING,
        step=1,
        version=2,
        messages=(HarborChatMessage.user("question"),),
        executions=(
            AgentToolExecution(
                step=1,
                call_id="call-1",
                tool="vector_search",
                ok=True,
                arguments_digest="digest",
                evidence=(
                    AgentEvidenceReference(
                        "vector_search",
                        "chunk-1",
                        "doc-1",
                        0.91,
                        "Deployment Guide",
                        ("Operations", "Rollback"),
                        "lines 40–46",
                    ),
                ),
            ),
        ),
        usage=HarborChatUsage(total_tokens=2),
        stop_reason=None,
        response=None,
        created_at=now,
        updated_at=now,
    )


def test_checkpoint_evidence_round_trips_with_readable_provenance() -> None:
    checkpoint = _checkpoint()

    _, executions, _, _, _ = _state_from_json(_state_to_json(checkpoint))

    assert executions == checkpoint.executions
    assert "Deployment Guide" in executions[0].evidence[0].marker
    assert "Operations > Rollback" in executions[0].evidence[0].marker


def test_legacy_checkpoint_without_evidence_remains_readable() -> None:
    data = _state_to_json(_checkpoint())
    data["executions"][0].pop("evidence")

    _, executions, _, _, _ = _state_from_json(data)

    assert executions[0].evidence == ()


def test_checkpoint_preserves_an_exact_marker_from_an_earlier_formatter() -> None:
    checkpoint = _checkpoint()
    previous = checkpoint.executions[0].evidence[0]
    old_marker = '[Source: "Deployment Guide" — Operations (ref legacy123)]'
    evidence = (
        AgentEvidenceReference(
            previous.tool,
            previous.chunk_id,
            previous.document_id,
            previous.score,
            previous.document_title,
            previous.section_path,
            previous.location,
            old_marker,
        ),
    )
    execution = checkpoint.executions[0]
    checkpoint = replace(
        checkpoint,
        executions=(
            AgentToolExecution(
                execution.step,
                execution.call_id,
                execution.tool,
                execution.ok,
                execution.arguments_digest,
                evidence,
            ),
        ),
    )

    _, executions, _, _, _ = _state_from_json(_state_to_json(checkpoint))

    assert executions[0].evidence[0].marker == old_marker
