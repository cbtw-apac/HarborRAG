"""A source result carries a bounded removal sample and the full count."""

from __future__ import annotations

import pytest

from harborrag_runtime.ingestion_contracts import IngestionExecutionResult
from harborrag_runtime.temporal.schemas import (
    MAX_REPORTED_REMOVAL_CANDIDATES,
    SourceIngestionResult,
    reported_removal_candidates,
)


def _result(**overrides: object) -> SourceIngestionResult:
    values: dict[str, object] = {
        "task_id": "task-1",
        "scan_id": "scan-1",
        "discovered": 3,
        "published": 1,
        "unchanged": 0,
        "failed": 0,
        "removal_candidates": ("document:a", "document:b"),
        "unresolved_relations": 0,
    }
    values.update(overrides)
    return SourceIngestionResult(**values)  # type: ignore[arg-type]


def test_reported_removals_are_capped_below_the_payload_limit() -> None:
    removals = tuple(f"document:{number:064x}" for number in range(30_000))

    reported = reported_removal_candidates(removals)

    assert len(reported) == MAX_REPORTED_REMOVAL_CANDIDATES
    assert reported == removals[:MAX_REPORTED_REMOVAL_CANDIDATES]
    # The whole list is what would have overrun Temporal's 2 MB payload.
    assert sum(len(item) for item in removals) > 2_000_000 > sum(len(item) for item in reported)


def test_result_count_defaults_to_the_named_removals_for_older_histories() -> None:
    # A history recorded before removal_count existed deserializes without it.
    assert _result().removal_count == 2
    assert _result(removal_count=30_000).removal_count == 30_000


def test_result_rejects_an_unbounded_list_or_an_understated_count() -> None:
    with pytest.raises(ValueError, match="more removals"):
        _result(
            removal_candidates=tuple(str(n) for n in range(MAX_REPORTED_REMOVAL_CANDIDATES + 1))
        )
    with pytest.raises(ValueError, match="below"):
        _result(removal_count=1)


def test_execution_result_carries_the_count_through_the_gateway_contract() -> None:
    result = IngestionExecutionResult(
        task_id="task-1",
        scan_id="scan-1",
        discovered=3,
        published=1,
        unchanged=0,
        failed=0,
        removal_candidates=("document:a",),
        unresolved_relations=0,
    )

    assert result.removal_count == 1
