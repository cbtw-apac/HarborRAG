"""Regression coverage for the activity-stage lookup that gates every Temporal activity.

A stage name used in a `boundary(...)` call that has no entry in
`_ACTIVITY_STAGES` used to raise a bare, unclassified `KeyError` on the very
first line of the activity -- exactly what happened to every retry activity
(`PrepareRetryFailures`, `RetryDocumentRelease`, `RecordRetryDocumentFailure`,
`FinalizeRetryFailures`) until this fix. These tests catch that class of bug
directly, independent of building a full fake `IngestionRuntime`.
"""

from __future__ import annotations

import inspect
import re

import pytest

from harborrag_core.invariants import HarborInvariantError
from harborrag_runtime.ingestion.observability import IngestionTelemetry
from harborrag_runtime.temporal import ingestion_activities, retry_activities
from harborrag_runtime.temporal.activity_observability import (
    _ACTIVITY_STAGES,
    ActivityObservability,
)


def _stage_names_used_in(module) -> set[str]:
    source = inspect.getsource(module)
    return set(re.findall(r'boundary\(\s*["\']([A-Za-z]+)["\']', source))


@pytest.mark.parametrize("module", [retry_activities, ingestion_activities])
def test_every_boundary_call_has_a_stage_mapping(module) -> None:
    used = _stage_names_used_in(module)
    assert used, "expected at least one boundary(...) call in this module"
    missing = used - _ACTIVITY_STAGES.keys()
    assert not missing, f"unmapped activity stage(s) in {module.__name__}: {missing}"


@pytest.mark.parametrize(
    "stage",
    [
        "PrepareRetryFailures",
        "RetryDocumentRelease",
        "RecordRetryDocumentFailure",
        "FinalizeRetryFailures",
    ],
)
def test_retry_activity_stages_enter_the_boundary_without_raising(stage: str) -> None:
    observability = ActivityObservability(IngestionTelemetry())
    with observability.boundary(stage):
        pass


def test_unmapped_stage_raises_actionable_invariant_error_not_bare_keyerror() -> None:
    observability = ActivityObservability(IngestionTelemetry())
    with pytest.raises(HarborInvariantError, match="NotARealStage"):
        with observability.boundary("NotARealStage"):
            pass
