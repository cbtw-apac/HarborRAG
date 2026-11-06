"""Failure inbox: the latest failed result of each document, with its history.

Handles:
- paging unresolved or resolved failures with per-category counts
- selecting the retryable failures a retry run should pick up

The inbox is computed from ``task_document_results`` in one pass of window
functions per query; nothing is stored beside the results.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from typing import Any

from sqlalchemy import (
    ColumnElement,
    Select,
    Subquery,
    and_,
    case,
    func,
    not_,
    or_,
    select,
    tuple_,
)

from harborrag_adapters.repositories.backends.sqlalchemy import SQLAlchemyDBClient
from harborrag_core.ingestion import FailurePage, StoredFailure

from .row_values import (
    DatabaseRow,
    required_datetime,
    required_int,
    required_mapping,
    required_text,
)
from .schema import INGESTION_TASKS, SOURCE_SCOPES, TASK_DOCUMENT_RESULTS

_FAILED = "failed"
_RESOLVING_STATUSES = ("published", "success", "unchanged", "skipped", "removed")
_UNKNOWN_CATEGORY = "unknown"
_MAX_PAGE_LIMIT = 200

FailureCursor = tuple[datetime, str, str]


class FailureInboxRepository:
    """Read-only inbox over the document results of a tenant."""

    def __init__(self, client: SQLAlchemyDBClient) -> None:
        self._client = client

    async def page(  # noqa: PLR0913 - query filters are keyword-only
        self,
        *,
        tenant_id: str,
        source_keys: frozenset[str] | None,
        error_category: str | None,
        internal_stages: frozenset[str] | None,
        resolved: bool,
        q: str | None,
        before: FailureCursor | None,
        limit: int,
        include_total: bool,
    ) -> FailurePage:
        """Read one keyset page of failures, newest first.

        Args:
            tenant_id: Tenant whose documents are listed.
            source_keys: Connection source keys to keep; ``None`` keeps all.
            error_category: Keep failures with this ``safe_error_code``.
            internal_stages: Keep failures at these internal pipeline stages.
            resolved: List resolved failures instead of unresolved ones.
            q: Case-insensitive substring of document ID, source item ID or error code.
            before: ``(failed_at, task_id, document_id)`` of the last item already seen.
            limit: Page size, 1 to 200.
            include_total: Set ``total`` to the number of matching items.

        Returns:
            The page, whether more items follow, the matching items per error
            category, and the total when requested.

        Raises:
            ValueError: If ``limit`` is out of range.
        """
        if not 1 <= limit <= _MAX_PAGE_LIMIT:
            raise ValueError("failure page limit must be between 1 and 200")
        ranked = _ranked_results(tenant_id, source_keys)
        conditions = _item_conditions(
            ranked,
            error_category=error_category,
            internal_stages=internal_stages,
            resolved=resolved,
            q=q,
        )
        category = func.coalesce(_text(ranked, "safe_error_code"), _UNKNOWN_CATEGORY)
        counts_statement = (
            select(category.label("category"), func.count().label("item_count"))
            .where(*conditions)
            .group_by(category)
        )
        page_statement = (
            _item_columns(select(ranked), ranked)
            .where(*conditions, *_before_conditions(ranked, before))
            .order_by(*_newest_first(ranked))
            .limit(limit + 1)
        )
        async with self._client.sessions() as session:
            count_rows = (await session.execute(counts_statement)).mappings().all()
            page_rows = (await session.execute(page_statement)).mappings().all()
        counts = {str(row["category"]): int(row["item_count"]) for row in count_rows}
        return FailurePage(
            items=tuple(_stored_failure(row) for row in page_rows[:limit]),
            has_more=len(page_rows) > limit,
            counts=counts,
            total=sum(counts.values()) if include_total else None,
        )

    async def select(  # noqa: PLR0913 - query filters are keyword-only
        self,
        *,
        tenant_id: str,
        keys: Sequence[tuple[str, str]] | None,
        source_keys: frozenset[str] | None,
        error_category: str | None,
        internal_stages: frozenset[str] | None,
        limit: int = 1000,
    ) -> tuple[StoredFailure, ...]:
        """Select unresolved failures that are marked retryable, newest first.

        Args:
            tenant_id: Tenant whose documents are listed.
            keys: ``(task_id, document_id)`` pairs to keep; ``None`` keeps all.
            source_keys: Connection source keys to keep; ``None`` keeps all.
            error_category: Keep failures with this ``safe_error_code``.
            internal_stages: Keep failures at these internal pipeline stages.
            limit: Most failures to return.

        Returns:
            Matching failures; a keyed pair matches only if it is still the
            document's latest failure.
        """
        if keys is not None and not keys:
            return ()
        ranked = _ranked_results(tenant_id, source_keys)
        conditions = _item_conditions(
            ranked,
            error_category=error_category,
            internal_stages=internal_stages,
            resolved=False,
            q=None,
        )
        statement = (
            _item_columns(select(ranked), ranked)
            .where(*conditions, ranked.c.result["retryable"].as_boolean().is_(True))
            .order_by(*_newest_first(ranked))
            .limit(limit)
        )
        if keys is not None:
            statement = statement.where(
                tuple_(ranked.c.task_id, ranked.c.document_id).in_(list(keys))
            )
        async with self._client.sessions() as session:
            rows = (await session.execute(statement)).mappings().all()
        return tuple(_stored_failure(row) for row in rows)


def _ranked_results(tenant_id: str, source_keys: frozenset[str] | None) -> Subquery:
    """Tenant results with, per document, the failed rank, attempts and last resolution."""
    status = func.lower(TASK_DOCUMENT_RESULTS.c.status)
    is_failed = status == _FAILED
    by_document = [TASK_DOCUMENT_RESULTS.c.document_id]
    # TODO: ``task_document_results`` has no ``document_id`` index, so each query
    # scans the tenant's results; add one in a migration if the inbox gets slow.
    statement = (
        select(
            TASK_DOCUMENT_RESULTS.c.task_id,
            TASK_DOCUMENT_RESULTS.c.document_id,
            TASK_DOCUMENT_RESULTS.c.result,
            TASK_DOCUMENT_RESULTS.c.completed_at,
            status.label("result_status"),
            SOURCE_SCOPES.c.connection_id.label("source_key"),
            func.row_number()
            .over(
                partition_by=[*by_document, case((is_failed, 0), else_=1)],
                order_by=[
                    TASK_DOCUMENT_RESULTS.c.completed_at.desc(),
                    TASK_DOCUMENT_RESULTS.c.task_id.desc(),
                ],
            )
            .label("failed_rank"),
            func.sum(case((is_failed, 1), else_=0))
            .over(partition_by=by_document)
            .label("attempts"),
            func.max(case((status.in_(_RESOLVING_STATUSES), TASK_DOCUMENT_RESULTS.c.completed_at)))
            .over(partition_by=by_document)
            .label("last_resolved_at"),
        )
        .join(INGESTION_TASKS, INGESTION_TASKS.c.task_id == TASK_DOCUMENT_RESULTS.c.task_id)
        .join(SOURCE_SCOPES, SOURCE_SCOPES.c.source_scope_id == INGESTION_TASKS.c.source_scope_id)
        .where(INGESTION_TASKS.c.tenant_id == tenant_id)
    )
    if source_keys is not None:
        statement = statement.where(SOURCE_SCOPES.c.connection_id.in_(sorted(source_keys)))
    return statement.subquery("ranked")


def _text(ranked: Subquery, key: str) -> ColumnElement[Any]:
    accessor: ColumnElement[Any] = ranked.c.result[key].as_string()
    return accessor


def _is_resolved(ranked: Subquery) -> ColumnElement[bool]:
    return and_(
        ranked.c.last_resolved_at.is_not(None),
        ranked.c.last_resolved_at > ranked.c.completed_at,
    )


def _item_conditions(
    ranked: Subquery,
    *,
    error_category: str | None,
    internal_stages: frozenset[str] | None,
    resolved: bool,
    q: str | None,
) -> list[ColumnElement[bool]]:
    conditions: list[ColumnElement[bool]] = [
        ranked.c.result_status == _FAILED,
        ranked.c.failed_rank == 1,
        _is_resolved(ranked) if resolved else not_(_is_resolved(ranked)),
    ]
    if error_category is not None:
        conditions.append(_text(ranked, "safe_error_code") == error_category)
    if internal_stages is not None:
        conditions.append(_text(ranked, "failure_stage").in_(sorted(internal_stages)))
    if q:
        needle = q.lower()
        conditions.append(
            or_(
                func.lower(ranked.c.document_id).contains(needle, autoescape=True),
                func.lower(_text(ranked, "source_item_id")).contains(needle, autoescape=True),
                func.lower(_text(ranked, "safe_error_code")).contains(needle, autoescape=True),
            )
        )
    return conditions


def _before_conditions(ranked: Subquery, before: FailureCursor | None) -> list[ColumnElement[bool]]:
    if before is None:
        return []
    failed_at, task_id, document_id = before
    return [
        or_(
            ranked.c.completed_at < failed_at,
            and_(ranked.c.completed_at == failed_at, ranked.c.task_id < task_id),
            and_(
                ranked.c.completed_at == failed_at,
                ranked.c.task_id == task_id,
                ranked.c.document_id < document_id,
            ),
        )
    ]


def _newest_first(ranked: Subquery) -> list[ColumnElement[Any]]:
    return [ranked.c.completed_at.desc(), ranked.c.task_id.desc(), ranked.c.document_id.desc()]


def _item_columns(statement: Select[Any], ranked: Subquery) -> Select[Any]:
    return statement.with_only_columns(
        ranked.c.task_id,
        ranked.c.document_id,
        ranked.c.result,
        ranked.c.completed_at,
        ranked.c.source_key,
        ranked.c.attempts,
        case((_is_resolved(ranked), 1), else_=0).label("resolved"),
    )


def _stored_failure(row: DatabaseRow) -> StoredFailure:
    return StoredFailure(
        task_id=required_text(row, "task_id"),
        document_id=required_text(row, "document_id"),
        source_key=required_text(row, "source_key"),
        result=required_mapping(row, "result"),
        failed_at=required_datetime(row, "completed_at"),
        attempts=required_int(row, "attempts"),
        resolved=required_int(row, "resolved") == 1,
    )
