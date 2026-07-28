from __future__ import annotations

from dataclasses import replace
from unittest.mock import AsyncMock

import pytest

from harborrag_runtime.temporal.schemas import (
    ArtifactActivityResult,
    ArtifactReference,
    ArtifactStage,
    ArtifactStageState,
    ArtifactStatus,
    ArtifactWorkflowInput,
    PendingResolution,
    ResolutionDecision,
    ResolutionReceipt,
    WorkflowOptions,
)
from harborrag_runtime.temporal.workflows.artifact import ArtifactIngestionWorkflow


def _input() -> ArtifactWorkflowInput:
    return ArtifactWorkflowInput(
        run_id="run-1",
        tenant_id="tenant-1",
        manifest_id="manifest-1",
        artifact=ArtifactReference(
            artifact_id="artifact-1",
            source_ref="source://1",
            source_kind="local",
            connector_name="local",
            artifact_revision_id="revision-1",
        ),
        generation_id="generation-1",
        options=WorkflowOptions(),
    )


@pytest.mark.asyncio
async def test_artifact_workflow_runs_all_stages(monkeypatch) -> None:
    transitions = {
        ArtifactStage.PREFLIGHT: ArtifactStage.FETCH,
        ArtifactStage.FETCH: ArtifactStage.PARSE,
        ArtifactStage.PARSE: ArtifactStage.CHUNK,
        ArtifactStage.CHUNK: ArtifactStage.INDEX,
        ArtifactStage.INDEX: ArtifactStage.VALIDATE,
        ArtifactStage.VALIDATE: ArtifactStage.FINALIZE,
    }
    calls = []

    async def execute(name, request, activity_class, options):
        calls.append(name)
        stage = request.state.stage
        status = (
            ArtifactStatus.SUCCEEDED if stage is ArtifactStage.FINALIZE else ArtifactStatus.RUNNING
        )
        next_stage = transitions.get(stage, stage)
        return ArtifactActivityResult(status, replace(request.state, stage=next_stage))

    monkeypatch.setattr(
        "harborrag_runtime.temporal.workflows.artifact.execute_artifact_stage",
        execute,
    )

    result = await ArtifactIngestionWorkflow().run(_input())

    assert result.status is ArtifactStatus.SUCCEEDED
    assert calls == [
        "harborrag.preflight_artifact",
        "harborrag.fetch_artifact",
        "harborrag.parse_artifact",
        "harborrag.chunk_artifact",
        "harborrag.index_artifact",
        "harborrag.validate_artifact",
        "harborrag.finalize_artifact",
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [ArtifactStatus.FAILED, ArtifactStatus.QUARANTINED])
async def test_artifact_workflow_returns_isolated_terminal_status(monkeypatch, status) -> None:
    async def execute(name, request, activity_class, options):
        return ArtifactActivityResult(status, request.state, error_type="isolated")

    monkeypatch.setattr(
        "harborrag_runtime.temporal.workflows.artifact.execute_artifact_stage",
        execute,
    )

    result = await ArtifactIngestionWorkflow().run(_input())

    assert result.status is status


@pytest.mark.asyncio
async def test_manual_resolution_validates_persists_and_resumes(monkeypatch) -> None:
    instance = ArtifactIngestionWorkflow()
    request = _input()
    pending = PendingResolution(
        artifact_id="artifact-1",
        request_ref="resolution://1",
        reason="ambiguous revision",
        resume_stage=ArtifactStage.FETCH,
    )
    instance._input = request
    instance._state = ArtifactStageState(request.artifact, request.generation_id)
    instance._pending = pending
    instance._status = ArtifactStatus.WAITING_FOR_RESOLUTION
    decision = ResolutionDecision(
        artifact_id="artifact-1",
        request_ref="resolution://1",
        decision="accept_remote",
        actor_id="member-1",
        submitted_at="2026-07-22T10:00:00Z",
    )
    receipt = ResolutionReceipt(
        artifact_id="artifact-1",
        decision_ref="decision://1",
        accepted=True,
        resume_stage=ArtifactStage.FETCH,
    )
    persist = AsyncMock(return_value=receipt)
    notify = AsyncMock()
    monkeypatch.setattr(
        "harborrag_runtime.temporal.workflows.artifact.execute_resolution",
        persist,
    )
    monkeypatch.setattr(instance, "_notify_parent", notify)
    monkeypatch.setattr(
        "harborrag_runtime.temporal.workflows.artifact.workflow.now",
        lambda: __import__("datetime").datetime.fromisoformat("2026-07-22T12:00:00+00:00"),
    )

    instance.validate_resolution(decision)
    result = await instance.submit_resolution(decision)

    assert result is receipt
    assert instance.get_pending_resolution() is None
    assert instance.get_status() is ArtifactStatus.RUNNING
    assert instance._state.stage is ArtifactStage.FETCH
    persist.assert_awaited_once()


def test_manual_resolution_rejects_stale_request() -> None:
    instance = ArtifactIngestionWorkflow()
    instance._pending = PendingResolution(
        artifact_id="artifact-1",
        request_ref="resolution://current",
        reason="ambiguous revision",
        resume_stage=ArtifactStage.FETCH,
    )
    decision = ResolutionDecision(
        artifact_id="artifact-1",
        request_ref="resolution://stale",
        decision="accept_remote",
        actor_id="member-1",
        submitted_at="2026-07-22T10:00:00Z",
    )

    with pytest.raises(ValueError, match="stale"):
        instance.validate_resolution(decision)
