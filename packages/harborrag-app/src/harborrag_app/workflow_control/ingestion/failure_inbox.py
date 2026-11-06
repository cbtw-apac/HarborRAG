"""Failure inbox: page the failed documents of a tenant and retry them through their root runs."""

from __future__ import annotations

import base64
import binascii
import json
from collections.abc import Awaitable, Callable, Mapping, Sequence
from datetime import datetime
from hashlib import sha256
from typing import Any, Protocol

from harborrag_core.domain.connection import Connection
from harborrag_core.ingestion import FailurePage, IngestionTask, StoredFailure
from harborrag_core.ports.control_plane import ConnectionRepositoryPort

from ..errors import IngestionCursorError, IngestionFailureIdError
from .operations import IngestionActions, connection_index, source_keys_for
from .stages import SPEC_STAGES, internal_stages, spec_stage

FAILURE_ID_PREFIX = "fail_"
MAX_RETRY_FAILURES = 1000
_CAPTURE_STAGE = "capture"
_UNKNOWN_CATEGORY = "unknown"


class FailureRuns(Protocol):
    """The run registry the inbox needs."""

    async def get(self, task_id: str) -> IngestionTask | None: ...

    async def failure_page(  # noqa: PLR0913 - the registry's query filters
        self,
        *,
        tenant_id: str,
        source_keys: frozenset[str] | None,
        error_category: str | None,
        internal_stages: frozenset[str] | None,
        resolved: bool,
        q: str | None,
        before: tuple[datetime, str, str] | None,
        limit: int,
        include_total: bool,
        known_stages: frozenset[str] | None = None,
    ) -> FailurePage: ...

    async def failures_for_retry(  # noqa: PLR0913 - the registry's query filters
        self,
        *,
        tenant_id: str,
        keys: Sequence[tuple[str, str]] | None,
        source_keys: frozenset[str] | None,
        error_category: str | None,
        internal_stages: frozenset[str] | None,
        limit: int = 1000,
        known_stages: frozenset[str] | None = None,
    ) -> tuple[StoredFailure, ...]: ...


def failure_id(task_id: str, document_id: str) -> str:
    """Return the public ID of a failure: the run and the document that failed in it."""
    raw = f"{task_id}\n{document_id}".encode()
    return FAILURE_ID_PREFIX + base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def parse_failure_id(value: str) -> tuple[str, str]:
    """Return ``(task_id, document_id)`` of a public failure ID.

    Raises:
        IngestionFailureIdError: ``value`` is not an ID ``failure_id`` produced.
    """
    try:
        if not value.startswith(FAILURE_ID_PREFIX):
            raise ValueError
        encoded = value[len(FAILURE_ID_PREFIX) :]
        raw = base64.urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4)).decode("utf-8")
        task_id, separator, document_id = raw.partition("\n")
        if not separator or not task_id or not document_id:
            raise ValueError
    except (ValueError, binascii.Error) as error:
        raise IngestionFailureIdError("Failure ID is invalid.", {"field": "ids"}) from error
    return task_id, document_id


class FailureInboxService:
    """Page and retry the failed documents of one tenant."""

    def __init__(
        self,
        *,
        connections: Callable[[], ConnectionRepositoryPort],
        runs: Callable[[], Awaitable[FailureRuns]],
        actions: IngestionActions,
    ) -> None:
        self._connections = connections
        self._runs = runs
        self._actions = actions

    async def page(  # noqa: PLR0913 - the spec's inbox filters plus paging
        self,
        *,
        tenant_id: str,
        project_id: str | None,
        connection_ref: str | None,
        stage: str | None,
        error_category: str | None,
        resolved: bool,
        q: str | None,
        cursor: str | None,
        limit: int,
        include_total: bool,
    ) -> dict[str, Any]:
        """Return a newest-first page of failures.

        Args:
            tenant_id: Tenant to read.
            project_id: Keep the failures of one project.
            connection_ref: Keep the failures of one connection.
            stage: Keep one spec stage; ``capture`` includes failures with no known stage.
            error_category: Keep one error category; ``unknown`` means no error code.
            resolved: List resolved failures instead of unresolved ones.
            q: Case-insensitive substring of document ID, item ID or error code.
            cursor: ``next_cursor`` of the previous page, for the same filters.
            limit: Page size.
            include_total: Also count every matching failure.

        Returns:
            ``{"items", "next_cursor", "counts", "total"}``; ``counts`` covers all
            matches per error category.

        Raises:
            IngestionCursorError: ``cursor`` is malformed or belongs to other filters.
        """
        fingerprint = _fingerprint(project_id, connection_ref, stage, error_category, resolved, q)
        position = _decode_cursor(cursor, fingerprint) if cursor else None
        connections = self._connections()
        index = await connection_index(connections, tenant_id=tenant_id)
        stored = await (await self._runs()).failure_page(
            tenant_id=tenant_id,
            source_keys=await source_keys_for(
                connections,
                tenant_id=tenant_id,
                project_id=project_id,
                connection_ref=connection_ref,
            ),
            error_category=error_category,
            resolved=resolved,
            q=q,
            before=position,
            limit=limit,
            include_total=include_total,
            **_stage_filter(stage),
        )
        next_cursor = None
        if stored.has_more and stored.items:
            last = stored.items[-1]
            next_cursor = _encode_cursor(
                (last.failed_at, last.task_id, last.document_id), fingerprint
            )
        return {
            "items": [_failure_item(item, index) for item in stored.items],
            "next_cursor": next_cursor,
            "counts": stored.counts,
            "total": stored.total,
        }

    async def retry(
        self,
        *,
        tenant_id: str,
        ids: Sequence[str] | None,
        filter: Mapping[str, str] | None,  # noqa: A002 - the request field's name
        actor: str,
        actor_type: str | None = None,
    ) -> dict[str, Any]:
        """Retry unresolved, retryable failures: one retry run per root run.

        Args:
            tenant_id: Tenant to read.
            ids: Failure IDs to retry; exclusive with ``filter``.
            filter: ``connection_ref``, ``error_category`` and ``stage`` to select by.
            actor: Principal id of the caller.
            actor_type: ``user`` or ``api_key``.

        Returns:
            ``{"ingestion_id": first new run or None, "ingestion_ids": [...], "queued":
            documents}``; nothing selected starts no run.

        Raises:
            IngestionFailureIdError: An entry of ``ids`` is malformed.
        """
        runs = await self._runs()
        if ids is not None:
            keys = [parse_failure_id(value) for value in ids]
            selected = await runs.failures_for_retry(
                tenant_id=tenant_id,
                keys=keys,
                source_keys=None,
                error_category=None,
                internal_stages=None,
                limit=MAX_RETRY_FAILURES,
            )
        else:
            criteria = filter or {}
            selected = await runs.failures_for_retry(
                tenant_id=tenant_id,
                keys=None,
                source_keys=await source_keys_for(
                    self._connections(),
                    tenant_id=tenant_id,
                    project_id=None,
                    connection_ref=criteria.get("connection_ref"),
                ),
                error_category=criteria.get("error_category"),
                limit=MAX_RETRY_FAILURES,
                **_stage_filter(criteria.get("stage")),
            )
        by_root = await _group_by_root(runs, selected)
        started: list[str] = []
        queued = 0
        for root_id, document_ids in by_root.items():
            retry = await self._actions.retry_failures(
                task_id=root_id, document_ids=document_ids, actor=actor, actor_type=actor_type
            )
            started.append(str(retry["retry_task_id"]))
            queued += int(str(retry["accepted_document_count"]))
        return {
            "ingestion_id": started[0] if started else None,
            "ingestion_ids": started,
            "queued": queued,
        }


def _stage_filter(stage: str | None) -> dict[str, frozenset[str] | None]:
    """Repository arguments for a spec stage; ``capture`` also takes unmapped stages."""
    if stage is None:
        return {"internal_stages": None, "known_stages": None}
    return {
        "internal_stages": internal_stages(stage),
        "known_stages": frozenset(SPEC_STAGES) if stage == _CAPTURE_STAGE else None,
    }


async def _group_by_root(
    runs: FailureRuns, failures: Sequence[StoredFailure]
) -> dict[str, list[str]]:
    roots: dict[str, str] = {}
    grouped: dict[str, list[str]] = {}
    for failure in failures:
        if failure.task_id not in roots:
            roots[failure.task_id] = await _root_run(runs, failure.task_id)
        grouped.setdefault(roots[failure.task_id], []).append(failure.document_id)
    return grouped


async def _root_run(runs: FailureRuns, task_id: str) -> str:
    """Follow ``request.retry_of`` to the run that is not itself a retry."""
    seen = {task_id}
    current = task_id
    while True:
        task = await runs.get(current)
        parent = task.request.get("retry_of") if task is not None else None
        if not isinstance(parent, str) or not parent or parent in seen:
            return current
        seen.add(parent)
        current = parent


def _failure_item(failure: StoredFailure, index: Mapping[str, Connection]) -> dict[str, Any]:
    result = failure.result
    code = result.get("safe_error_code")
    category = code if isinstance(code, str) and code else _UNKNOWN_CATEGORY
    connection = index.get(failure.source_key)
    stage = result.get("failure_stage")
    return {
        "failure_id": failure_id(failure.task_id, failure.document_id),
        "ingestion_id": failure.task_id,
        "connection_ref": connection.ref if connection is not None else failure.source_key,
        "document_ref": str(result.get("source_item_id") or failure.document_id),
        "stage": spec_stage(stage if isinstance(stage, str) else None),
        "error_category": category,
        "message": f"Document ingestion failed: {category}",
        "attempts": failure.attempts,
        "retryable": result.get("retryable") is True,
        "resolved": failure.resolved,
        "created_at": failure.failed_at,
    }


def _fingerprint(*parts: object) -> str:
    return sha256("|".join(str(part or "") for part in parts).encode()).hexdigest()[:16]


def _encode_cursor(position: tuple[datetime, str, str], fingerprint: str) -> str:
    failed_at, task_id, document_id = position
    payload = json.dumps([failed_at.isoformat(), task_id, document_id], separators=(",", ":"))
    return f"{fingerprint}.{base64.urlsafe_b64encode(payload.encode()).decode().rstrip('=')}"


def _decode_cursor(cursor: str, fingerprint: str) -> tuple[datetime, str, str]:
    bound, _, packed = cursor.partition(".")
    try:
        if bound != fingerprint:
            raise ValueError
        raw = base64.urlsafe_b64decode(packed + "=" * (-len(packed) % 4))
        failed_at, task_id, document_id = json.loads(raw)
        position = (datetime.fromisoformat(failed_at), str(task_id), str(document_id))
    except (ValueError, TypeError, binascii.Error) as error:
        raise IngestionCursorError(
            "Cursor is invalid or does not match the filters.", {"field": "cursor"}
        ) from error
    return position
