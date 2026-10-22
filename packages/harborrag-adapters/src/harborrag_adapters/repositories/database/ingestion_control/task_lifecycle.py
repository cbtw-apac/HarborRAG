from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime, timedelta

from sqlalchemy import ColumnElement, and_, or_, select, update

from harborrag_adapters.repositories.backends.sqlalchemy import SQLAlchemyDBClient
from harborrag_core.base import utc_now
from harborrag_core.contracts import HarborConflictError, HarborNotFoundError
from harborrag_core.ingestion import IngestionTaskState, reject_runtime_fields

from .row_values import required_mapping
from .schema import INGESTION_TASKS

_TASK_TRANSITIONS = {
    IngestionTaskState.PENDING: frozenset(
        {
            IngestionTaskState.RUNNING,
            IngestionTaskState.FAILED,
            IngestionTaskState.CANCELLED,
        }
    ),
    IngestionTaskState.RUNNING: frozenset(
        {
            IngestionTaskState.PAUSED,
            IngestionTaskState.COMPLETED,
            IngestionTaskState.PARTIAL,
            IngestionTaskState.FAILED,
            IngestionTaskState.CANCELLED,
        }
    ),
    IngestionTaskState.PAUSED: frozenset(
        {
            IngestionTaskState.RUNNING,
            IngestionTaskState.FAILED,
            IngestionTaskState.CANCELLED,
        }
    ),
    IngestionTaskState.COMPLETED: frozenset(),
    IngestionTaskState.PARTIAL: frozenset(),
    IngestionTaskState.FAILED: frozenset(),
    IngestionTaskState.CANCELLED: frozenset(),
}
_RETRYABLE_FINALIZATION_STAGES = frozenset({"relation_repair", "removal_reconciliation"})
SUBMISSION_STATE = "submission_state"
SUBMISSION_CLAIMED_AT = "submission_claimed_at"
# Outbox recovery claims a row under its lock before it starts the workflow, so
# nothing can fail the row between that start and the "submitted" confirmation.
SUBMISSION_CLAIMED = "submitting"
# A claim older than this belongs to a recovery pass that died mid-submission; the
# row is offered to recovery again instead of being stranded PENDING.
SUBMISSION_CLAIM_STALE_AFTER = timedelta(minutes=5)
# The start was confirmed, or the row belongs to an execution already in progress.
_SUBMISSION_SETTLED = ("submitted", "executor")
# Submission markers of PENDING rows that are not awaiting a Temporal start. A claim
# is in flight; ``pending_submissions`` still returns one once it is stale.
NOT_AWAITING_SUBMISSION = (*_SUBMISSION_SETTLED, SUBMISSION_CLAIMED)


def submission_claim_timestamp(moment: datetime) -> str:
    """Fixed-width UTC ISO text, so stored claims order correctly as strings in SQL."""

    return moment.astimezone(UTC).isoformat(timespec="microseconds")


def _submission_state() -> ColumnElement[str]:
    accessor: ColumnElement[str] = INGESTION_TASKS.c.summary[SUBMISSION_STATE].as_string()
    return accessor


def _submission_claimed_at() -> ColumnElement[str]:
    accessor: ColumnElement[str] = INGESTION_TASKS.c.summary[SUBMISSION_CLAIMED_AT].as_string()
    return accessor


def awaiting_submission() -> ColumnElement[bool]:
    """SQL: the row carries no submission marker that rules out a start.

    ``NOT IN`` is NULL for an unmarked row, hence the explicit ``IS NULL`` arm.
    """

    state = _submission_state()
    return or_(state.is_(None), state.not_in(NOT_AWAITING_SUBMISSION))


def stale_submission_claim(stale_before: datetime) -> ColumnElement[bool]:
    """SQL: a claim older than ``stale_before``, or one without a timestamp.

    Claim timestamps are fixed-width UTC ISO text, so this is a string comparison
    on every backend.
    """

    claimed_at = _submission_claimed_at()
    return and_(
        _submission_state() == SUBMISSION_CLAIMED,
        or_(claimed_at.is_(None), claimed_at < submission_claim_timestamp(stale_before)),
    )


class TaskLifecycleMixin:
    """Advance an ingestion task's durable state machine."""

    _client: SQLAlchemyDBClient

    async def transition(
        self,
        task_id: str,
        target: IngestionTaskState,
        *,
        summary: Mapping[str, object] | None = None,
    ) -> None:
        safe_summary = dict(summary or {})
        reject_runtime_fields(safe_summary)
        now = utc_now()
        async with self._client.sessions.begin() as session:
            result = await session.execute(
                select(INGESTION_TASKS)
                .where(INGESTION_TASKS.c.task_id == task_id)
                .with_for_update()
            )
            row = result.mappings().one_or_none()
            if row is None:
                raise HarborNotFoundError(f"ingestion task does not exist: {task_id}")
            current = IngestionTaskState(row["status"])
            if current == target:
                return
            if target not in _TASK_TRANSITIONS[current]:
                raise HarborConflictError(
                    f"invalid ingestion task transition: {current.value} -> {target.value}"
                )
            values: dict[str, object] = {
                "status": target.value,
                "summary": safe_summary,
            }
            if target == IngestionTaskState.RUNNING:
                values["started_at"] = now
            if target in {
                IngestionTaskState.COMPLETED,
                IngestionTaskState.PARTIAL,
                IngestionTaskState.FAILED,
                IngestionTaskState.CANCELLED,
            }:
                values["completed_at"] = now
            await session.execute(
                update(INGESTION_TASKS).where(INGESTION_TASKS.c.task_id == task_id).values(**values)
            )

    async def fail_if_unsubmitted(
        self,
        task_id: str,
        *,
        summary: Mapping[str, object],
    ) -> IngestionTaskState | None:
        """Fail a PENDING task whose start was never submitted, atomically.

        A caller that could not reach the execution provider decides from a stale
        snapshot; meanwhile another submitter may have started or claimed the run,
        or an earlier start may already have been confirmed. The condition lives in
        the UPDATE itself (compare-and-set), so it holds on sqlite too, where
        ``FOR UPDATE`` is ignored: only a still-unsubmitted PENDING task fails.
        A claim, even a stale one, may already have started the workflow, so it is
        never failed here. Returns the task's status afterwards, or None when it is
        missing.
        """

        safe_summary = dict(summary)
        reject_runtime_fields(safe_summary)
        async with self._client.sessions.begin() as session:
            result = await session.execute(
                update(INGESTION_TASKS)
                .where(
                    INGESTION_TASKS.c.task_id == task_id,
                    INGESTION_TASKS.c.status == IngestionTaskState.PENDING.value,
                    awaiting_submission(),
                )
                .values(
                    status=IngestionTaskState.FAILED.value,
                    summary=safe_summary,
                    completed_at=utc_now(),
                )
            )
            if _changed_one(result):
                return IngestionTaskState.FAILED
            current = await session.scalar(
                select(INGESTION_TASKS.c.status).where(INGESTION_TASKS.c.task_id == task_id)
            )
            return None if current is None else IngestionTaskState(current)

    async def claim_submission(
        self,
        task_id: str,
        *,
        stale_after: timedelta = SUBMISSION_CLAIM_STALE_AFTER,
    ) -> str | None:
        """Claim a PENDING, unsubmitted task for an outbox start, atomically.

        Returns the claim token (the ``submission_claimed_at`` written) when this
        caller now owns the start: the row was still PENDING, its start was not
        confirmed, and no other claim younger than ``stale_after`` holds it; None
        otherwise. The caller then starts the workflow and confirms it with
        ``submission_state: submitted``, or releases the claim with its token.
        The read only builds the new summary (and locks the row on Postgres); the
        UPDATE re-checks every condition, so a concurrent claim or start wins.
        """

        now = utc_now()
        token = submission_claim_timestamp(now)
        async with self._client.sessions.begin() as session:
            result = await session.execute(
                select(INGESTION_TASKS)
                .where(INGESTION_TASKS.c.task_id == task_id)
                .with_for_update()
            )
            row = result.mappings().one_or_none()
            if row is None or IngestionTaskState(row["status"]) is not IngestionTaskState.PENDING:
                return None
            summary = required_mapping(row, "summary")
            summary[SUBMISSION_STATE] = SUBMISSION_CLAIMED
            summary[SUBMISSION_CLAIMED_AT] = token
            claimed = await session.execute(
                update(INGESTION_TASKS)
                .where(
                    INGESTION_TASKS.c.task_id == task_id,
                    INGESTION_TASKS.c.status == IngestionTaskState.PENDING.value,
                    or_(awaiting_submission(), stale_submission_claim(now - stale_after)),
                )
                .values(summary=summary)
            )
            return token if _changed_one(claimed) else None

    async def release_submission_claim(self, task_id: str, token: str) -> bool:
        """Drop this caller's outbox claim after a start that cannot have happened.

        Only the claim ``token`` names is removed: a slow first claimer must not
        release a later pass's live claim, and a start another submitter has
        confirmed since must survive. Returns whether the claim was released.
        """

        async with self._client.sessions.begin() as session:
            result = await session.execute(
                select(INGESTION_TASKS)
                .where(INGESTION_TASKS.c.task_id == task_id)
                .with_for_update()
            )
            row = result.mappings().one_or_none()
            if row is None:
                return False
            summary = required_mapping(row, "summary")
            summary.pop(SUBMISSION_STATE, None)
            summary.pop(SUBMISSION_CLAIMED_AT, None)
            released = await session.execute(
                update(INGESTION_TASKS)
                .where(
                    INGESTION_TASKS.c.task_id == task_id,
                    _submission_state() == SUBMISSION_CLAIMED,
                    _submission_claimed_at() == token,
                )
                .values(summary=summary)
            )
            return _changed_one(released)

    async def update_summary(
        self,
        task_id: str,
        values: Mapping[str, object],
    ) -> None:
        """Merge safe progress fields without changing the task lifecycle state."""

        safe_values = dict(values)
        reject_runtime_fields(safe_values)
        async with self._client.sessions.begin() as session:
            result = await session.execute(
                select(INGESTION_TASKS)
                .where(INGESTION_TASKS.c.task_id == task_id)
                .with_for_update()
            )
            row = result.mappings().one_or_none()
            if row is None:
                raise HarborNotFoundError(f"ingestion task does not exist: {task_id}")
            summary = required_mapping(row, "summary")
            summary.update(safe_values)
            await session.execute(
                update(INGESTION_TASKS)
                .where(INGESTION_TASKS.c.task_id == task_id)
                .values(summary=summary)
            )

    async def finalize(
        self,
        task_id: str,
        target: IngestionTaskState,
        *,
        summary: Mapping[str, object],
    ) -> None:
        """Converge a retried finalization activity on its durable outcome."""

        if target not in {
            IngestionTaskState.COMPLETED,
            IngestionTaskState.PARTIAL,
            IngestionTaskState.FAILED,
        }:
            raise ValueError("task finalization requires a terminal outcome")
        safe_summary = dict(summary)
        reject_runtime_fields(safe_summary)
        now = utc_now()
        async with self._client.sessions.begin() as session:
            result = await session.execute(
                select(INGESTION_TASKS)
                .where(INGESTION_TASKS.c.task_id == task_id)
                .with_for_update()
            )
            row = result.mappings().one_or_none()
            if row is None:
                raise HarborNotFoundError(f"ingestion task does not exist: {task_id}")
            current = IngestionTaskState(row["status"])
            if current in {
                IngestionTaskState.COMPLETED,
                IngestionTaskState.PARTIAL,
            }:
                if target == current:
                    return
                raise HarborConflictError("a finalized ingestion task cannot change outcome")
            if current == IngestionTaskState.FAILED:
                previous_summary = required_mapping(row, "summary")
                failed_stage = previous_summary.get("failed_stage")
                if failed_stage not in _RETRYABLE_FINALIZATION_STAGES:
                    if target == current:
                        return
                    raise HarborConflictError("a terminal ingestion failure cannot be completed")
            elif current != IngestionTaskState.RUNNING:
                raise HarborConflictError(
                    f"invalid ingestion task finalization from {current.value}"
                )
            await session.execute(
                update(INGESTION_TASKS)
                .where(INGESTION_TASKS.c.task_id == task_id)
                .values(
                    status=target.value,
                    summary=safe_summary,
                    completed_at=now,
                )
            )


def _changed_one(result: object) -> bool:
    """Whether a compare-and-set UPDATE matched its row."""

    return getattr(result, "rowcount", 0) == 1
