"""The completion-claim stale window outlives the longest legitimate request."""

from __future__ import annotations

import inspect

from annotated_types import Le

from harborrag_app.api.settings import ApiSettings
from harborrag_app.workflow_control.memory.locks import SessionLocks
from harborrag_core.ports.completion_requests import STALE_CLAIM_SECONDS


def _upper_bound(field: str) -> float:
    bounds = [item.le for item in ApiSettings.model_fields[field].metadata if isinstance(item, Le)]
    assert bounds, f"{field} must keep an upper bound for the claim window to be safe"
    return float(bounds[0])


def test_a_live_claim_is_never_taken_over_at_the_largest_accepted_timeouts() -> None:
    """R1a-03: a slow stream kept its claim for less than its own deadline.

    Nothing refreshes an ``in_progress`` claim, so a retry arriving after the stale
    window took over a stream still running under a longer configured deadline and
    paid for the completion a second time.
    """

    turn_wait = inspect.signature(SessionLocks).parameters["acquire_timeout"].default
    longest_hold = (
        _upper_bound("api_request_timeout_seconds")
        + _upper_bound("api_stream_timeout_seconds")
        + float(turn_wait)
    )

    assert longest_hold < STALE_CLAIM_SECONDS
