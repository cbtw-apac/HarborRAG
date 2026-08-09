"""Validation coverage for RetryFailuresInput, which had none before this file.

RetryFailuresWorkflow has no `continue_as_new` checkpoint (unlike
SourceIngestionWorkflow's batch_size x continue_after_batches), so an
unbounded document selection can exceed Temporal's workflow history limits
mid-run with no partial-progress recovery. `_MAX_RETRY_DOCUMENT_IDS` caps
that at construction time instead.
"""

from __future__ import annotations

import pytest

from harborrag_runtime.temporal.schemas import (
    _MAX_RETRY_DOCUMENT_IDS,
    RetryFailuresInput,
)


def _input(**overrides: object) -> RetryFailuresInput:
    values: dict[object, object] = {
        "retry_task_id": "retry-1",
        "original_task_id": "original-1",
        "tenant_id": "ACME",
        "document_ids": ("doc-1", "doc-2"),
    }
    values.update(overrides)
    return RetryFailuresInput(**values)  # type: ignore[arg-type]


def test_retry_failures_input_accepts_a_normal_selection() -> None:
    retry_input = _input()
    assert retry_input.document_ids == ("doc-1", "doc-2")


def test_retry_failures_input_rejects_more_than_the_cap() -> None:
    oversized = tuple(f"doc-{index}" for index in range(_MAX_RETRY_DOCUMENT_IDS + 1))

    with pytest.raises(ValueError, match="at most"):
        _input(document_ids=oversized)


def test_retry_failures_input_accepts_exactly_the_cap() -> None:
    exactly_at_cap = tuple(f"doc-{index}" for index in range(_MAX_RETRY_DOCUMENT_IDS))

    retry_input = _input(document_ids=exactly_at_cap)

    assert len(retry_input.document_ids) == _MAX_RETRY_DOCUMENT_IDS


def test_retry_failures_input_rejects_empty_document_ids() -> None:
    with pytest.raises(ValueError, match="non-empty"):
        _input(document_ids=())


def test_retry_failures_input_rejects_duplicate_document_ids() -> None:
    with pytest.raises(ValueError, match="unique"):
        _input(document_ids=("doc-1", "doc-1"))


def test_retry_failures_input_rejects_blank_identities() -> None:
    with pytest.raises(ValueError, match="non-empty"):
        _input(retry_task_id="  ")


def test_retry_failures_input_rejects_out_of_range_concurrency() -> None:
    with pytest.raises(ValueError, match="document_concurrency"):
        _input(document_concurrency=0)
    with pytest.raises(ValueError, match="document_concurrency"):
        _input(document_concurrency=101)
