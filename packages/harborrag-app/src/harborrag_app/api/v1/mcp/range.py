"""Relative time-range parsing for ``GET /v1/mcp/queries?range=24h``.

No such parser existed anywhere in harborrag-app before ML4-P3 (there is
nothing to reuse from ``/metrics/ingestion``, which takes no range at all).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Annotated

from fastapi import Query

# Digits then a single unit letter (h=hours, d=days); FastAPI/pydantic rejects
# anything else with a 422 before this module ever sees the value.
RANGE_PATTERN = r"^[1-9][0-9]{0,5}[hd]$"

RangeQuery = Annotated[
    str,
    Query(
        pattern=RANGE_PATTERN,
        description="Relative time window: an integer followed by 'h' (hours) or 'd' (days).",
    ),
]


def range_start(range_expr: str, *, now: datetime | None = None) -> datetime:
    """The start of the window ``range_expr`` (e.g. ``24h``, ``7d``) names, ending at ``now``."""
    reference = now or datetime.now(UTC)
    amount, unit = int(range_expr[:-1]), range_expr[-1]
    delta = timedelta(hours=amount) if unit == "h" else timedelta(days=amount)
    return reference - delta
