"""Tests for agent-run resume eligibility: status, failure class, and lease."""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta

import pytest
from agent_test_helpers import Chat, RaisingChat, Runs, Tools, checkpoint
from agent_test_helpers import response as _response

from harborrag_core.contracts.errors import (
    HarborConflictError,
    HarborNotFoundError,
    HarborRateLimitError,
)
from harborrag_core.models.chat import HarborChatMessage
from harborrag_core.models.errors import HarborChatInvalidRequestError, HarborChatTimeoutError
from harborrag_core.ports.agent_runs import AgentRunIdentity, AgentRunStatus
from harborrag_engine.agent import AgentEvent, AgentRunOptions, AgentService
from harborrag_engine.agent.run_lifecycle import (
    DEFAULT_AGENT_RUN_LEASE_SECONDS,
    is_retryable_failure,
    lease_seconds,
)

_IDENTITY = AgentRunIdentity("ACME", "reader-1", "session-1", "run-fixed", "reader-1")
_OPTIONS = AgentRunOptions(tenant_id="ACME", principal_id="reader-1", session_id="session-1")


def _service(runs: Runs) -> AgentService:
    return AgentService(Chat([_response(text="resumed answer")]), Tools(), runs=runs)


async def _resume_ok(runs: Runs) -> None:
    result = await _service(runs).resume("run-fixed", _OPTIONS)
    assert result.response.text == "resumed answer"
    persisted = runs.checkpoints["run-fixed"]
    assert persisted.status is AgentRunStatus.COMPLETED
    assert persisted.lease_owner is None
    assert persisted.lease_expires_at is None


@pytest.mark.asyncio
async def test_resume_from_cancelled_run_succeeds() -> None:
    runs = Runs()
    runs.checkpoints["run-fixed"] = checkpoint(_IDENTITY, status=AgentRunStatus.CANCELLED)
    await _resume_ok(runs)


@pytest.mark.asyncio
async def test_resume_from_retryable_failure_succeeds_but_permanent_failure_does_not() -> None:
    runs = Runs()
    runs.checkpoints["run-fixed"] = checkpoint(
        _IDENTITY, status=AgentRunStatus.FAILED, failure_retryable=True
    )
    await _resume_ok(runs)

    runs = Runs()
    runs.checkpoints["run-fixed"] = checkpoint(
        _IDENTITY, status=AgentRunStatus.FAILED, failure_retryable=False
    )
    with pytest.raises(HarborNotFoundError):
        await _service(runs).resume("run-fixed", _OPTIONS)


@pytest.mark.asyncio
async def test_resume_from_completed_run_is_not_found() -> None:
    runs = Runs()
    runs.checkpoints["run-fixed"] = checkpoint(_IDENTITY, status=AgentRunStatus.COMPLETED)
    with pytest.raises(HarborNotFoundError):
        await _service(runs).resume("run-fixed", _OPTIONS)


@pytest.mark.asyncio
async def test_resume_of_running_run_with_live_lease_conflicts() -> None:
    runs = Runs()
    runs.checkpoints["run-fixed"] = checkpoint(
        _IDENTITY,
        lease_owner="other-host:1:abc",
        lease_expires_at=datetime.now(UTC) + timedelta(minutes=5),
    )
    chat = Chat([])

    with pytest.raises(HarborConflictError):
        await AgentService(chat, Tools(), runs=runs).resume("run-fixed", _OPTIONS)

    assert chat.requests == []
    assert runs.checkpoints["run-fixed"].version == 2


@pytest.mark.asyncio
async def test_resume_of_running_run_with_expired_or_absent_lease_succeeds() -> None:
    runs = Runs()
    runs.checkpoints["run-fixed"] = checkpoint(
        _IDENTITY,
        lease_owner="crashed-host:1:abc",
        lease_expires_at=datetime.now(UTC) - timedelta(seconds=1),
    )
    await _resume_ok(runs)

    runs = Runs()
    runs.checkpoints["run-fixed"] = checkpoint(_IDENTITY)  # legacy row: no lease at all
    await _resume_ok(runs)


@pytest.mark.asyncio
async def test_resume_claims_the_lease_before_executing_so_a_racing_resumer_conflicts() -> None:
    runs = Runs()
    runs.checkpoints["run-fixed"] = checkpoint(_IDENTITY, status=AgentRunStatus.CANCELLED)
    seen: list[AgentEvent] = []

    class ClaimObservingRuns(Runs):
        async def save_step(self, cp):
            if cp.status is AgentRunStatus.RUNNING:
                seen.append(AgentEvent("claim", cp.identity.run_id, {"version": cp.version}))
                assert cp.lease_owner is not None
                assert cp.lease_expires_at is not None
                assert cp.lease_expires_at > datetime.now(UTC)
                assert cp.step == 1
            await super().save_step(cp)

    observing = ClaimObservingRuns()
    observing.checkpoints = runs.checkpoints
    await AgentService(Chat([_response(text="ok")]), Tools(), runs=observing).resume(
        "run-fixed", _OPTIONS
    )
    assert [event.data["version"] for event in seen] == [3]

    # A stale second resumer that read the CANCELLED checkpoint at version 2
    # collides with the claim rather than replaying the same step.
    runs2 = Runs()
    runs2.checkpoints["run-fixed"] = checkpoint(_IDENTITY, status=AgentRunStatus.CANCELLED)
    original_get = runs2.get

    async def get_then_advance(identity):
        cp = await original_get(identity)
        runs2.checkpoints["run-fixed"] = checkpoint(
            _IDENTITY,
            version=3,
            lease_owner="other",
            lease_expires_at=datetime.now(UTC) + timedelta(minutes=5),
        )
        return cp

    runs2.get = get_then_advance  # type: ignore[method-assign]
    with pytest.raises(HarborConflictError):
        await _service(runs2).resume("run-fixed", _OPTIONS)


@pytest.mark.asyncio
async def test_running_run_refreshes_its_lease_on_every_persisted_step() -> None:
    chat = Chat(
        [
            _response(call=("call-1", "vector_search", '{"query":"x"}')),
            _response(text="final"),
        ]
    )
    leases: list[tuple[str | None, datetime | None]] = []

    class LeaseRecordingRuns(Runs):
        async def create(self, cp):
            leases.append((cp.lease_owner, cp.lease_expires_at))
            await super().create(cp)

        async def save_step(self, cp):
            leases.append((cp.lease_owner, cp.lease_expires_at))
            await super().save_step(cp)

    runs = LeaseRecordingRuns()
    await AgentService(chat, Tools(), runs=runs).run([HarborChatMessage.user("q")], _OPTIONS)

    owners = {owner for owner, _ in leases[:-1]}
    assert len(owners) == 1 and None not in owners
    assert all(expires is not None for _, expires in leases[:-1])
    assert leases[-1] == (None, None)  # COMPLETED releases the lease


@pytest.mark.asyncio
async def test_failed_run_records_whether_its_failure_is_retryable() -> None:
    for error, expected in (
        (HarborChatTimeoutError("provider timeout"), True),
        (HarborRateLimitError("slow down"), True),
        (HarborChatInvalidRequestError("bad request"), False),
        (RuntimeError("bug"), False),
    ):
        runs = Runs()
        with pytest.raises(type(error)):
            await AgentService(RaisingChat(error), Tools(), runs=runs).run(
                [HarborChatMessage.user("q")], _OPTIONS
            )
        persisted = next(iter(runs.checkpoints.values()))
        assert persisted.status is AgentRunStatus.FAILED
        assert persisted.failure_retryable is expected, error
        assert persisted.lease_owner is None


def test_is_retryable_failure_classification() -> None:
    assert is_retryable_failure(TimeoutError()) is True
    assert is_retryable_failure(ConnectionResetError()) is True
    assert is_retryable_failure(HarborChatTimeoutError("t", retryable=False)) is False
    assert is_retryable_failure(HarborConflictError("stale")) is False
    assert is_retryable_failure(ValueError("x")) is False


def test_lease_seconds_covers_the_run_deadline_or_falls_back_to_default() -> None:
    assert lease_seconds(_OPTIONS) == DEFAULT_AGENT_RUN_LEASE_SECONDS
    bounded = AgentRunOptions(
        tenant_id="ACME",
        principal_id="reader-1",
        session_id="session-1",
        timeout_seconds=10.0,
        synthesis_timeout_seconds=5.0,
    )
    assert lease_seconds(bounded) == 45.0


@pytest.mark.asyncio
async def test_memory_failure_is_logged_at_error_with_run_identity(
    caplog: pytest.LogCaptureFixture,
) -> None:
    class BrokenMemory:
        async def recent(self, identity, *, limit=2):
            del identity, limit
            return ()

        async def recent_messages(self, identity, *, limit):
            del identity, limit
            return ()

        async def append_messages(self, identity, messages):
            del identity, messages
            raise RuntimeError("memory unavailable")

        async def clear(self, identity):
            del identity

    events: list[AgentEvent] = []

    async def sink(event: AgentEvent) -> None:
        events.append(event)

    runs = Runs()
    with caplog.at_level(logging.ERROR, logger="harborrag.engine.agent"):
        result = await AgentService(
            Chat([_response(text="answer")]), Tools(), memory=BrokenMemory(), runs=runs
        ).run([HarborChatMessage.user("question")], _OPTIONS, events=sink)

    assert result.response.text == "answer"
    assert runs.checkpoints[result.run_id].status is AgentRunStatus.COMPLETED
    records = [r for r in caplog.records if "memory update failed" in r.getMessage()]
    assert len(records) == 1
    record = records[0]
    assert record.levelno == logging.ERROR
    assert record.run_id == result.run_id  # type: ignore[attr-defined]
    assert record.tenant_id == "ACME"  # type: ignore[attr-defined]
    assert record.session_id == "session-1"  # type: ignore[attr-defined]
    assert record.exc_info is not None
    assert result.run_id in record.getMessage()
    assert [e.kind for e in events][-2:] == ["run.memory_failed", "run.completed"]
